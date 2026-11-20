"""USD validation (real pxr.UsdValidation) and per-engine compliance rules."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pxr import Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, UsdValidation

from antigravity3d.kernel.fast_ops import Issue
from antigravity3d.kernel.types import MeshData, bounds_issue, gltf_world_bounds
from antigravity3d.state import EngineBudget, TargetEngine


def validate_usd(path_or_stage: str | Path | Usd.Stage) -> list[Issue]:
    """Runs every validator registered with the UsdValidation registry on the stage."""
    if isinstance(path_or_stage, Usd.Stage):
        stage = path_or_stage
    else:
        try:
            stage = Usd.Stage.Open(str(path_or_stage))
        except Exception as e:  # noqa: BLE001 - pxr raises Tf.ErrorException
            return [Issue("usd_open_failed", "critical", None, str(e)[:200])]
    registry = UsdValidation.ValidationRegistry()
    ctx = UsdValidation.ValidationContext(registry.GetOrLoadAllValidators())
    out: list[Issue] = []
    for err in ctx.Validate(stage):
        t = err.GetType()
        if t == UsdValidation.ValidationErrorType.Error:
            sev = "critical"
        elif t == UsdValidation.ValidationErrorType.Warn:
            sev = "warning"
        else:
            continue  # Info / None: not actionable
        node = None
        for site in err.GetSites():
            if site.IsPrim() or site.IsProperty():
                path = site.GetPrim().GetPath() if site.IsPrim() else site.GetProperty().GetPrim().GetPath()
                if path != Sdf.Path.absoluteRootPath:
                    node = path.name
                break
        out.append(Issue(str(err.GetIdentifier()),
                         sev, node, err.GetMessage()[:300]))
    return out


# --------------------------------------------------------------------------- rule table


@dataclass(frozen=True)
class EngineRules:
    native_up_axis: str                       # engine's own convention
    allowed_up_axes: tuple[str, ...]          # others are rejected outright
    meters_per_unit: tuple[float, ...]        # accepted stage scales (first = native)
    name_regex: str                           # allowed prim/node names
    hull_prim_regex: str | None               # how collision hulls are recognised in the stage
    max_tris_per_mesh: int | None             # hard engine cap, None = no cap we are sure of
    max_texture_px: int | None                # per-side hint
    max_textures_per_material: int | None
    require_pow2_textures: bool
    notes: str = ""


# Studio-auditable. Entries marked UNCERTAIN are conservative hints, not documented hard limits.
ENGINE_RULES: dict[TargetEngine, EngineRules] = {
    "unreal_5": EngineRules(
        native_up_axis="Z",
        allowed_up_axes=("Z", "Y"),          # UE USD importer re-orients Y-up; Z is native
        meters_per_unit=(0.01, 1.0),         # UE units are cm; importer honours metersPerUnit
        name_regex=r"^[A-Za-z0-9_]+$",
        hull_prim_regex=r"^UCX_[A-Za-z0-9_]+_\d{2}$",  # UE static-mesh collision convention (UCX_<Mesh>_NN)
        max_tris_per_mesh=None,
        max_texture_px=8192,                 # UNCERTAIN: practical import hint (larger needs VT)
        max_textures_per_material=None,
        require_pow2_textures=True,          # mip streaming wants pow2
    ),
    "unity_urp": EngineRules(
        native_up_axis="Y",
        allowed_up_axes=("Y", "Z"),          # UNCERTAIN: USD path depends on the importer package
        meters_per_unit=(1.0,),
        name_regex=r"^[A-Za-z0-9_]+$",       # studio convention; Unity itself is permissive
        hull_prim_regex=None,                # colliders are components, not named geometry
        max_tris_per_mesh=None,              # 16-bit index buffers cap at 65535 verts; not enforced here
        max_texture_px=8192,                 # UNCERTAIN: default importer Max Size
        max_textures_per_material=None,
        require_pow2_textures=False,         # NPOT works but loses compression on some targets
    ),
    "godot_4": EngineRules(
        native_up_axis="Y",
        allowed_up_axes=("Y", "Z"),
        meters_per_unit=(1.0,),
        name_regex=r'^[^.:@/"%]+$',          # Godot node names cannot contain . : @ / " %
        hull_prim_regex=r"-(conv)?col(only)?$",  # Godot import suffix convention (-col, -convcolonly, ...)
        max_tris_per_mesh=None,
        max_texture_px=16384,                # UNCERTAIN: GPU-dependent
        max_textures_per_material=None,
        require_pow2_textures=False,
    ),
    "roblox_r15": EngineRules(
        native_up_axis="Y",
        allowed_up_axes=("Y",),              # UNCERTAIN: Roblox importer is Y-up
        meters_per_unit=(1.0, 0.28),         # UNCERTAIN: 1 stud ~ 0.28 m
        name_regex=r"^[A-Za-z0-9_]+$",
        hull_prim_regex=None,
        max_tris_per_mesh=10_000,            # MeshPart import limit
        max_texture_px=1024,                 # larger textures are downscaled
        max_textures_per_material=4,         # SurfaceAppearance: Color/Normal/Metalness/Roughness
        require_pow2_textures=False,
    ),
}


def _texture_assets(stage: Usd.Stage) -> dict[str, list[str]]:
    """material path -> resolved/authored texture file paths."""
    out: dict[str, list[str]] = {}
    for prim in stage.Traverse():
        if not prim.IsA(UsdShade.Material):
            continue
        files: list[str] = []
        for p in Usd.PrimRange(prim):
            if p.IsA(UsdShade.Shader):
                for inp in UsdShade.Shader(p).GetInputs():
                    v = inp.Get()
                    if isinstance(v, Sdf.AssetPath) and v.path:
                        files.append(v.resolvedPath or v.path)
        out[str(prim.GetPath())] = files
    return out


def is_collider_prim(prim: Usd.Prim, hull_re: re.Pattern | None = None) -> bool:
    """Collision geometry: named per the engine's hull convention, UCX_-prefixed, or physics-tagged."""
    name = prim.GetName()
    return bool(
        (hull_re and hull_re.search(name))
        or name.startswith("UCX_")
        or prim.HasAPI(UsdPhysics.CollisionAPI)
    )


def _material_files(prim: Usd.Prim) -> list[str]:
    files: list[str] = []
    for p in Usd.PrimRange(prim):
        if p.IsA(UsdShade.Shader):
            for inp in UsdShade.Shader(p).GetInputs():
                v = inp.Get()
                if isinstance(v, Sdf.AssetPath) and v.path:
                    files.append(v.path)
    return files


def _image_size(path: str) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 - unresolvable / non-image asset
        return None


def check_engine_compliance(stage: Usd.Stage | None, meshes: list[MeshData], budget: EngineBudget) -> list[Issue]:
    rules = ENGINE_RULES[budget.target_engine]
    eng = budget.target_engine
    out: list[Issue] = []

    def add(code: str, sev: str, node: str | None, detail: str) -> None:
        out.append(Issue(f"{eng}:{code}", sev, node, detail))  # type: ignore[arg-type]

    name_ok = re.compile(rules.name_regex)
    for m in meshes:
        if not name_ok.match(m.node_id):
            add("bad_name", "critical", m.node_id, f"name violates {rules.name_regex}")
        if rules.max_tris_per_mesh and len(m.triangles) > rules.max_tris_per_mesh:
            add("mesh_tri_cap", "critical", m.node_id, f"{len(m.triangles)} tris > {rules.max_tris_per_mesh} per mesh")
    total = sum(len(m.triangles) for m in meshes)
    if total > budget.max_tris:
        add("tri_budget", "critical", None, f"{total} tris > {budget.max_tris}")
    draws = len({m.material_id for m in meshes})
    if draws > budget.max_draw_calls:
        add("draw_call_budget", "critical", None, f"{draws} draw calls > {budget.max_draw_calls}")

    if stage is None:
        return out

    # stage metadata
    if stage.HasAuthoredMetadata("upAxis"):
        axis = str(UsdGeom.GetStageUpAxis(stage))
        if axis not in rules.allowed_up_axes:
            add("up_axis", "critical", None, f"upAxis {axis} not accepted (allowed {rules.allowed_up_axes})")
        elif axis != rules.native_up_axis:
            add("up_axis_reorient", "warning", None, f"upAxis {axis}; engine native {rules.native_up_axis}, importer must rotate")
    else:
        add("up_axis_missing", "critical", None, "upAxis not authored")
    if stage.HasAuthoredMetadata("metersPerUnit"):
        mpu = UsdGeom.GetStageMetersPerUnit(stage)
        if not any(abs(mpu - a) < 1e-9 for a in rules.meters_per_unit):
            add("meters_per_unit", "warning", None, f"metersPerUnit {mpu} not in {rules.meters_per_unit}")
    else:
        add("meters_per_unit_missing", "critical", None, "metersPerUnit not authored")

    # prims: names, hulls, UVs
    hull_re = re.compile(rules.hull_prim_regex) if rules.hull_prim_regex else None
    hulls = 0
    mesh_prims = []
    for prim in stage.Traverse():
        name = prim.GetName()
        if not name_ok.match(name) and not (hull_re and hull_re.search(name)):
            add("bad_name", "critical", name, f"prim name violates {rules.name_regex}")
        if eng == "unreal_5" and name.startswith(("UCX_", "UBX_", "USP_", "UCP_")):
            if not re.match(rules.hull_prim_regex or "", name):
                add("hull_name", "critical", name, "collision prim must be UCX_<asset>_<nn>")
            hulls += name.startswith("UCX_")
        elif hull_re and hull_re.search(name):
            hulls += 1
        if prim.IsA(UsdGeom.Mesh):
            mesh_prims.append(prim)
    if hulls > budget.max_collision_hulls:
        add("hull_count", "critical", None, f"{hulls} hulls > {budget.max_collision_hulls}")

    textures = _texture_assets(stage)
    any_tex = any(textures.values())
    for prim in mesh_prims:
        if is_collider_prim(prim, hull_re):
            continue
        pv = UsdGeom.PrimvarsAPI(prim)
        has_uv = any(pv.GetPrimvar(n) and pv.GetPrimvar(n).HasValue() for n in ("st", "st0", "UVMap"))
        if not has_uv:
            # critical when the mesh's own material samples textures (nothing for the st reader to read);
            # an untextured mesh only warns, unless textures exist elsewhere and it is unbound (legacy rule)
            mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            textured = bool(mat and _material_files(mat.GetPrim())) or (mat is None and any_tex)
            add("uv_missing", "critical" if textured else "warning", prim.GetName(),
                "no st primvar" + (" but its material has texture maps" if textured else ""))

    for mat, files in textures.items():
        if rules.max_textures_per_material and len(set(files)) > rules.max_textures_per_material:
            add("texture_count", "warning", Sdf.Path(mat).name, f"{len(set(files))} maps > {rules.max_textures_per_material}")
        for f in set(files):
            size = _image_size(f)
            if size is None:
                continue
            if rules.max_texture_px and max(size) > rules.max_texture_px:
                add("texture_size", "warning", Path(f).name, f"{size} exceeds {rules.max_texture_px}px")
            if rules.require_pow2_textures and any(s & (s - 1) for s in size):
                add("texture_npot", "warning", Path(f).name, f"{size} not power of two")
    return out


# --------------------------------------------------------------------------- package validation


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_convex(points: np.ndarray, tris: np.ndarray, tol: float = 1e-3) -> bool:
    """A closed triangle mesh is convex iff it equals its hull: all points on the hull, same volume."""
    from scipy.spatial import ConvexHull, QhullError

    if len(points) < 4 or len(tris) < 4:
        return False
    try:
        hull = ConvexHull(points)
    except QhullError:
        return False
    p = points[tris]
    vol = abs(float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0))
    return len(np.unique(hull.simplices)) == len(np.unique(tris)) and abs(vol - hull.volume) <= tol * max(hull.volume, 1e-12)


def _mesh_tris(prim: Usd.Prim) -> int:
    counts = UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get()
    return int(np.maximum(np.asarray(counts, dtype=np.int64) - 2, 0).sum()) if counts else 0


def _glb_json(path: Path) -> dict:
    with path.open("rb") as stream:
        magic, version, size, length, chunk = struct.unpack("<4sIII4s", stream.read(20))
        if (magic, version, chunk) != (b"glTF", 2, b"JSON") or size != path.stat().st_size or length > size - 20:
            raise ValueError(f"invalid GLB header in {path.name}")
        return json.loads(stream.read(length))


def _validate_glb_materials(path: Path, suffix: str, bindings: dict[str, tuple[str, set[str]]]) -> list[Issue]:
    """All authored per-mesh PBR slots must reference images in this GLB's own binary chunk."""
    issues = []
    try:
        doc = _glb_json(path)
        nodes = {n.get("name"): n for n in doc["nodes"]}
        for node, (material, roles) in bindings.items():
            primitives = doc["meshes"][nodes[node + suffix]["mesh"]]["primitives"]
            for p in primitives:
                mat = doc["materials"][p["material"]]
                if mat.get("name") != material:
                    raise ValueError(f"{node} expected material {material}, got {mat.get('name')}")
                if roles and "TEXCOORD_0" not in p["attributes"]:
                    raise ValueError(f"{node} has no texture coordinates")
                pbr = mat.get("pbrMetallicRoughness", {})
                slots = {"albedo": [(pbr, "baseColorTexture")], "orm": [(pbr, "metallicRoughnessTexture"),
                         (mat, "occlusionTexture")], "normal": [(mat, "normalTexture")]}
                for role in roles:
                    for obj, slot in slots[role]:
                        texture = doc["textures"][obj[slot]["index"]]
                        image = doc["images"][texture["source"]]
                        view = doc["bufferViews"][image["bufferView"]]
                        if view["byteLength"] < 1 or "uri" in doc["buffers"][view["buffer"]]:
                            raise ValueError(f"{node}/{slot} must be an embedded image")
    except (IndexError, KeyError, TypeError, ValueError, OSError, struct.error) as e:
        issues.append(Issue("pkg:glb_material", "critical", path.name, str(e)[:300]))
    return issues


def validate_package(out_dir: str | Path) -> list[Issue]:
    """Cold re-open of a written engine package (see kernel/package.py) and consistency checks.

    Checks: manifest present, every file hash matches, USD opens with a default prim, units match the
    manifest, every mesh bound to a textured material has ``st`` (count matches its interpolation) in
    every LOD variant, every texture path is relative, resolves and stays inside the package, LOD
    triangle counts never increase, the GLB LOD0 matches the USD LOD0, collider prims are named per the
    engine rules, tagged ``UsdPhysics`` convexHull + purpose guide, and are really convex.
    """
    out = Path(out_dir)
    issues: list[Issue] = []

    def add(code: str, sev: str, node: str | None, detail: str) -> None:
        issues.append(Issue(f"pkg:{code}", sev, node, detail[:300]))  # type: ignore[arg-type]

    mpath = out / "manifest.json"
    if not mpath.is_file():
        return [Issue("pkg:manifest_missing", "critical", None, f"{mpath} not found")]
    try:
        man = json.loads(mpath.read_text())
    except ValueError as e:
        return [Issue("pkg:manifest_unreadable", "critical", None, str(e)[:200])]

    out_real = out.resolve()

    def package_file(rel: str) -> Path | None:
        if isinstance(rel, str) and rel and not Path(rel).is_absolute():
            file = out / rel
            if file.resolve().is_relative_to(out_real):
                return file
        add("file_outside", "critical", str(rel), "manifest references a path outside this package")
        return None

    for rel, meta in (man.get("file_hashes") or {}).items():
        f = package_file(rel)
        if f is None:
            continue
        if not f.is_file():
            add("file_missing", "critical", rel, "listed in manifest but not on disk")
        elif _sha256(f) != meta.get("sha256"):
            add("hash_mismatch", "critical", rel, "content hash differs from manifest")
    if not man.get("file_hashes"):
        add("no_hashes", "critical", None, "manifest carries no file hashes")

    asset = man.get("asset", "")
    if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", asset):
        add("asset_name", "critical", str(asset), "expected a USD asset identifier")
        return issues
    usd_path = package_file(man.get("usd") or f"{asset}.usd")
    if usd_path is None:
        return issues
    try:
        stage = Usd.Stage.Open(str(usd_path))
    except Exception as e:  # noqa: BLE001 - pxr raises Tf.ErrorException
        add("usd_open", "critical", usd_path.name, str(e))
        return issues
    root = stage.GetDefaultPrim()
    if not root or not root.IsValid():
        add("default_prim", "critical", usd_path.name, "no default prim")
        return issues

    engine = man.get("target_engine")
    rules = ENGINE_RULES.get(engine)
    units = man.get("units") or {}
    if units:
        if str(UsdGeom.GetStageUpAxis(stage)) != units.get("up_axis"):
            add("up_axis", "critical", None, f"stage upAxis {UsdGeom.GetStageUpAxis(stage)} != manifest {units.get('up_axis')}")
        if abs(UsdGeom.GetStageMetersPerUnit(stage) - float(units.get("meters_per_unit", 1.0))) > 1e-9:
            add("meters_per_unit", "critical", None, "stage metersPerUnit differs from manifest")

    # textures: relative, resolvable, inside the package
    for prim in stage.Traverse():
        if not prim.IsA(UsdShade.Shader) or UsdShade.Shader(prim).GetIdAttr().Get() != "UsdUVTexture":
            continue
        v = UsdShade.Shader(prim).GetInput("file").Get()
        label = f"{prim.GetParent().GetName()}/{prim.GetName()}"
        if not isinstance(v, Sdf.AssetPath) or not v.path:
            add("texture_empty", "critical", label, "UsdUVTexture without a file")
            continue
        if Path(v.path).is_absolute() or "://" in v.path:
            add("texture_not_relative", "critical", label, f"absolute texture path {v.path!r}: package is not relocatable")
        resolved = v.resolvedPath
        if not resolved:
            add("texture_missing", "critical", label, f"{v.path!r} does not resolve")
        elif not Path(resolved).is_file():
            add("texture_missing", "critical", label, f"{resolved} is not a file")
        elif not Path(resolved).resolve().is_relative_to(out_real):
            add("texture_outside", "critical", label, f"{resolved} is outside the package")

    # meshes (per LOD variant)
    vset = root.GetVariantSets().GetVariantSet("LOD") if root.GetVariantSets().HasVariantSet("LOD") else None
    lod_names = sorted(vset.GetVariantNames(), key=lambda n: int(re.sub(r"\D", "", n) or 0)) if vset else []
    stage.SetEditTarget(stage.GetSessionLayer())  # variant switching below stays in memory
    hull_re = re.compile(rules.hull_prim_regex) if rules and rules.hull_prim_regex else None
    lod_tris: list[int] = []
    colliders: list[Usd.Prim] = []
    bindings: dict[str, tuple[str, set[str]]] = {}
    transforms = UsdGeom.XformCache()
    usd_lod0_bounds = []
    for variant in lod_names or [None]:
        if vset is not None:
            vset.SetVariantSelection(variant)
        total = 0
        for prim in Usd.PrimRange(root):
            if not prim.IsA(UsdGeom.Mesh):
                continue
            if is_collider_prim(prim, hull_re):
                if variant is None or variant == lod_names[0]:  # no LOD set (one level) or LOD0
                    colliders.append(prim)
                continue
            mesh = UsdGeom.Mesh(prim)
            total += _mesh_tris(prim)
            if not lod_tris and "glb_bounds_m" in man:
                local = np.asarray(mesh.GetPointsAttr().Get(), dtype=float)
                if local.size:
                    xf = np.asarray(transforms.GetLocalToWorldTransform(prim))
                    world = local @ xf[:3, :3] + xf[3, :3]
                    usd_lod0_bounds.append(np.array([world.min(axis=0), world.max(axis=0)]))
            mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            textured = bool(mat and _material_files(mat.GetPrim()))
            if mat and variant in (None, lod_names[0] if lod_names else None):
                roles = {role for role in ("albedo", "orm", "normal") if mat.GetPrim().GetChild(role + "Tex")}
                bindings[prim.GetName()] = (mat.GetPrim().GetName(), roles)
            pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
            st = pv.Get() if pv else None
            tag = f"{prim.GetName()}" + (f"[{variant}]" if variant else "")
            if st is None:
                if textured:
                    add("uv_missing", "critical", tag, "mesh bound to a textured material has no st primvar")
                continue
            n_pts = len(mesh.GetPointsAttr().Get() or [])
            n_corners = len(mesh.GetFaceVertexIndicesAttr().Get() or [])
            interp = pv.GetInterpolation()
            want = {"vertex": n_pts, "varying": n_pts, "faceVarying": n_corners}.get(str(interp))
            if want is None or len(st) != want:
                add("uv_count", "critical", tag, f"st has {len(st)} values for {interp} interpolation (expected {want})")
            elif not np.isfinite(np.asarray(st, dtype=np.float64)).all():
                add("uv_nan", "critical", tag, "st contains NaN/inf")
        lod_tris.append(total)
    if any(b > a for a, b in zip(lod_tris, lod_tris[1:])):
        add("lod_order", "critical", None, f"LOD triangle counts must not increase: {lod_tris}")
    elif any(b == a for a, b in zip(lod_tris, lod_tris[1:])):  # a duplicate level costs a mesh and a file for nothing
        add("lod_duplicate", "warning", None, f"consecutive LOD levels have equal triangle counts: {lod_tris}")
    if man.get("lod_tris") and lod_tris and list(man["lod_tris"]) != lod_tris and len(lod_names) > 1:
        add("lod_manifest", "warning", None, f"manifest lod_tris {man['lod_tris']} != USD {lod_tris}")

    if "glb_bounds_m" in man and usd_lod0_bounds:
        usd_bounds = gltf_world_bounds(usd_lod0_bounds, z_up=UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z)
        if why := bounds_issue(man["glb_bounds_m"], usd_bounds):
            add("usd_bounds", "critical", usd_path.name, why)

    # GLB LOD0 vs USD LOD0
    glb = out / f"{asset}.glb"
    if glb.is_file() and lod_tris:
        try:
            import trimesh

            sc = trimesh.load(glb)
            geoms = list(sc.geometry.values()) if hasattr(sc, "geometry") else [sc]
            glb_tris = sum(len(g.faces) for g in geoms)
            if "glb_bounds_m" in man and (why := bounds_issue(man["glb_bounds_m"], sc.bounds)):
                add("glb_bounds", "critical", glb.name, why)
            draw_count = sum(len(m["primitives"]) for m in _glb_json(glb)["meshes"])
            if draw_count > man.get("budget", {}).get("max_draw_calls", draw_count):
                add("draw_budget", "critical", glb.name, f"{draw_count} render primitives exceed draw-call budget")
            if "glb_batches" in man and draw_count != man["draw_calls"]:
                add("draw_count", "critical", glb.name, "manifest draw count differs from the GLB")
            if glb_tris != lod_tris[0]:
                add("glb_tris", "critical", glb.name, f"GLB has {glb_tris} tris, USD LOD0 has {lod_tris[0]}")
        except Exception as e:  # noqa: BLE001 - unreadable GLB
            add("glb_open", "critical", glb.name, str(e))

    batches = man.get("glb_batches")
    if batches:
        actual_bindings = {}
        covered = []
        for batch, members in batches.items():
            covered.extend(members)
            slot = {bindings.get(m, (None, set()))[0] for m in members}
            if len(slot) != 1 or not members:
                add("glb_batch", "critical", batch, "one draw cannot bind multiple materials")
            elif members[0] in bindings:
                actual_bindings[batch] = bindings[members[0]]
        if sorted(covered) != sorted(p.GetName() for p in Usd.PrimRange(root) if p.IsA(UsdGeom.Mesh) and not is_collider_prim(p, hull_re)):
            add("glb_batch", "critical", None, "batches must partition all visual meshes")
        bindings = actual_bindings
    for level in range(max(1, len(lod_tris))):
        suffix = f"_LOD{level}" if level else ""
        issues.extend(_validate_glb_materials(out / f"{asset}{suffix}.glb", suffix, bindings))

    # colliders
    max_hulls = (man.get("budget") or {}).get("max_collision_hulls")
    # USD prim names are identifiers ('-' is illegal), so USD colliders always use UCX_<asset>_<nn>;
    # engine-specific suffix conventions (Godot "-convcolonly") can only apply to GLB node names.
    name_re = re.compile(r"^UCX_[A-Za-z0-9_]+_\d{2}$")
    if hull_re is not None:
        for node in man.get("collider_glb_nodes") or []:
            if not hull_re.search(node):
                add("collider_name", "critical", node, f"GLB collider node violates {hull_re.pattern} ({engine})")
    if max_hulls is not None and len(colliders) > max_hulls:
        add("hull_count", "critical", None, f"{len(colliders)} colliders > {max_hulls}")
    for prim in colliders:
        n = prim.GetName()
        if not name_re.search(n):
            add("collider_name", "critical", n, f"collider name violates {name_re.pattern} ({engine})")
        if not prim.HasAPI(UsdPhysics.CollisionAPI) or not prim.HasAPI(UsdPhysics.MeshCollisionAPI):
            add("collider_api", "critical", n, "needs UsdPhysics.CollisionAPI + MeshCollisionAPI")
        elif UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get() != "convexHull":
            add("collider_approx", "critical", n, "MeshCollisionAPI approximation must be convexHull")
        if UsdGeom.Imageable(prim).GetPurposeAttr().Get() != UsdGeom.Tokens.guide:
            add("collider_purpose", "critical", n, "collision hull must have purpose=guide")
        mesh = UsdGeom.Mesh(prim)
        pts = np.asarray(mesh.GetPointsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
        idx = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64).reshape(-1, 3)
        if not _is_convex(pts, idx):
            add("collider_convex", "critical", n, "collider mesh is not convex")
    want_names = man.get("colliders")
    if want_names is not None and sorted(want_names) != sorted(p.GetName() for p in colliders):
        add("collider_manifest", "warning", None, "manifest collider names differ from the USD collider prims")

    usdz = man.get("usdz")
    if usdz and (archive := package_file(usdz)) is not None:
        try:
            z = Usd.Stage.Open(str(archive))
            if not z.GetDefaultPrim():
                add("usdz_default_prim", "warning", usdz, "usdz has no default prim")
        except Exception as e:  # noqa: BLE001
            add("usdz_open", "warning", usdz, str(e))
    return issues
