"""Engine package writer: one implementation shared by the harness tool and the pipeline DAGs.

Layout of ``out_dir`` (everything referenced relative to the USD layer, so the directory can be moved)::

    <asset>.usd            default prim <asset>; LOD variant set; Colliders/UCX_*; relative texture paths
    <asset>.usdz           same content with textures inside (omitted, with a recorded reason, on failure)
    <asset>.glb            LOD0, identical topology/UVs to the USD (Y-up, glTF convention)
    <asset>_LOD<n>.glb     n >= 1
    <asset>_colliders.glb  convex hulls
    textures/              copies of the material maps (normal maps converted to the engine convention)
    manifest.json          schema_version, units, tri counts, textures, hashes, validation, versions

Per-engine decisions (see ``ENGINE_RULES`` for the rule table they come from):

* up axis: the package USD is written in the engine's *native* up axis. Working stage is Z-up metres;
  for Y-up engines (unity_urp, godot_4, roblox_r15) the asset root gets ``xformOp:rotateX = -90`` and
  ``upAxis = Y`` together (so (x, y, z) -> (x, z, -y), the same mapping the GLB uses). Unreal stays Z-up.
* units: always metres (``metersPerUnit = 1.0``) with no scale baked in; every engine's rules accept
  1.0 and importers honour the metadata. Not converting to centimetres for Unreal is deliberate:
  a baked x100 root scale would affect collider/LOD authoring and buys nothing the importer does not do.
* normal maps: unreal_5 consumes DirectX (green down); everything else OpenGL (green up). The package
  holds the engine's convention, whatever convention the material was authored in; the conversion is
  a green-channel inversion applied exactly when ``material.convention != engine convention``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image
from pxr import Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, UsdUtils

from antigravity3d.kernel.compile_ir import author_mesh_data
from antigravity3d.kernel.fast_ops import ExportBundle, Hull, Issue, UVResult, write_gltf
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.validator import (
    ENGINE_RULES,
    check_engine_compliance,
    validate_package,
    validate_usd,
)
from antigravity3d.state import EngineBudget

SCHEMA_VERSION = 1
TEXTURE_DIR = "textures"
COLLIDERS_SCOPE = "Colliders"
LOD_SET = "LOD"
# Convention the engine's importer expects for tangent-space normal maps.
NORMAL_CONVENTION = {"unreal_5": "directx", "unity_urp": "opengl", "godot_4": "opengl", "roblox_r15": "opengl"}


@dataclass
class PackageManifest:
    out_dir: Path
    data: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)  # validate_package result

    @property
    def files(self) -> list[str]:
        return list(self.data["files"])

    @property
    def ok(self) -> bool:
        return not any(i.severity == "critical" for i in self.issues)

    @property
    def n_lod_files(self) -> int:
        return sum(1 for f in self.data["files"] if re.search(r"_LOD\d+\.glb$", f))


# --------------------------------------------------------------------------- small helpers


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def collider_name(engine: str, asset: str, i: int, *, usd: bool = True) -> str:
    """Collision hull name. USD prim names are identifiers (no '-'), so the USD prims always use the
    ``UCX_<asset>_<nn>`` convention (Unreal's, and a generic marker; the physics API + purpose=guide are
    the machine-readable markers). GLB node names follow the engine: Godot's importer wants the
    ``-convcolonly`` suffix, which only glTF node names can carry."""
    a = sanitize(asset)
    if engine == "godot_4" and not usd:
        return f"{a}_{i:02d}-convcolonly"
    return f"UCX_{a}_{i:02d}"


def flip_normal_green(src: Path, dst: Path) -> Path:
    """Writes ``src`` with the green channel inverted (OpenGL <-> DirectX normal map)."""
    with Image.open(src) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        rgb = im.convert("RGBA" if has_alpha else "RGB")
    a = np.asarray(rgb).copy()
    a[..., 1] = 255 - a[..., 1]
    Image.fromarray(a).save(dst, format="PNG")
    return dst


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_file(uri: str) -> Path | None:
    p = Path(uri[7:] if uri.startswith("file://") else uri)
    return p if p.is_file() else None


def _version(dist: str) -> str | None:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return None


def _versions() -> dict[str, Any]:
    from pxr import Usd as _Usd

    return {
        "antigravity3d": _version("antigravity3d"),
        "python": platform.python_version(),
        "usd": ".".join(map(str, _Usd.GetVersion())),
        "usd-core": _version("usd-core"),
        "trimesh": _version("trimesh"),
        "xatlas": _version("xatlas"),
        "meshoptimizer": _version("meshoptimizer"),
        "coacd": _version("coacd"),
        "numpy": np.__version__,
        "platform": sys.platform,
    }


def _issue_dict(i: Issue) -> dict[str, Any]:
    return {"code": i.code, "severity": i.severity, "node": i.node_id, "detail": i.detail}


def _parts(b: ExportBundle | Mapping[str, Any]) -> tuple[list[UVResult], dict[str, list], list[Hull], dict[str, float]]:
    if isinstance(b, ExportBundle):
        return b.uvs, b.lods, b.colliders, b.timings_ms
    return list(b["uvs"]), dict(b.get("lods") or {}), list(b.get("colliders") or []), dict(b.get("timings_ms") or {})


def _clean_previous(out: Path) -> None:
    """Removes what an earlier export into this directory wrote (listed in its manifest)."""
    old = out / "manifest.json"
    if not old.is_file():
        return
    try:
        names = json.loads(old.read_text()).get("file_hashes", {}).keys()
    except ValueError:
        names = []
    for rel in [*names, "manifest.json"]:
        p = (out / rel).resolve()
        if p.is_relative_to(out.resolve()) and p.is_file():
            p.unlink()
    tex = out / TEXTURE_DIR
    if tex.is_dir() and not any(tex.iterdir()):
        tex.rmdir()


# --------------------------------------------------------------------------- USD authoring


def _package_textures(stage: Usd.Stage, out: Path, engine: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Copies map files into ``textures/``, converts normal maps, rewrites asset paths relative."""
    target = NORMAL_CONVENTION.get(engine, "opengl")
    records: list[dict[str, Any]] = []
    unresolved: list[str] = []
    done: dict[tuple[str, bool], str] = {}  # (source, flipped) -> relative path
    owners: dict[str, str] = {}  # relative path -> source it was written for (collision guard)
    for prim in stage.Traverse():
        if not prim.IsA(UsdShade.Shader) or UsdShade.Shader(prim).GetIdAttr().Get() != "UsdUVTexture":
            continue
        shader = UsdShade.Shader(prim)
        inp = shader.GetInput("file")
        v = inp.Get() if inp else None
        if not isinstance(v, Sdf.AssetPath) or not v.path:
            continue
        mat = prim.GetParent()
        mat_id = mat.GetName()
        is_normal = "normal" in prim.GetName().lower()
        src = _local_file(v.path)
        if src is None:
            unresolved.append(v.path)
            continue
        src_conv = str(mat.GetCustomDataByKey("agy:normalConvention") or "opengl")
        flip = is_normal and src_conv != target
        key = (str(src.resolve()), flip)
        if key not in done:
            if flip:
                name = f"{src.stem}_{target}.png"
            else:
                name = src.name
            rel = f"{TEXTURE_DIR}/{name}"
            if owners.get(rel, str(src.resolve())) != str(src.resolve()):  # same name, other file
                rel = f"{TEXTURE_DIR}/{src.stem}_{hashlib.sha256(str(src.resolve()).encode()).hexdigest()[:8]}{Path(name).suffix}"
            (out / TEXTURE_DIR).mkdir(exist_ok=True)
            if flip:
                flip_normal_green(src, out / rel)
            else:
                shutil.copy2(src, out / rel)
            owners[rel] = str(src.resolve())
            done[key] = rel
            with Image.open(out / rel) as im:
                width, height = im.size
            cs = shader.GetInput("sourceColorSpace")
            records.append({
                "path": rel,
                "role": "normal" if is_normal else ("orm" if "orm" in prim.GetName().lower() else "albedo"),
                "material": mat_id,
                "width": width,
                "height": height,
                "convention": (target if is_normal else None),
                "authored_convention": (src_conv if is_normal else None),
                "converted": flip,
                "color_space": str(cs.Get()) if cs and cs.Get() else None,
                "source": src.name,
            })
        inp.Set(Sdf.AssetPath(f"./{done[key]}"))  # relative to the layer: the package is relocatable
        if is_normal:
            mat.SetCustomDataByKey("agy:normalConvention", target)
    return records, unresolved


_GEOMETRY_PROPS = frozenset({
    "points", "faceVertexCounts", "faceVertexIndices", "extent", "normals", "primvars:st", "primvars:st:indices",
    "primvars:normals", "agy:uvWeldMap",
})


def _author_lod_variants(
    stage: Usd.Stage, asset: Usd.Prim, targets: list[tuple[str, np.ndarray, np.ndarray, list]]
) -> list[int]:
    """LOD structure: variant set ``LOD`` on the asset prim, variants ``LOD0..LODn``.

    Chosen over sibling prims (``<asset>_LOD1``...) because one switch retargets every mesh of the asset
    consistently, materials/transforms/colliders are shared instead of duplicated, and viewers that
    understand variants (usdview, Omniverse, UE's LOD variant import) expose it directly. Every variant,
    LOD0 included, holds the points/indices/st/normals of each mesh prim (see the LIVRPS note below). Default selection: ``LOD0``. Each ``targets`` entry is
    (prim path, M33, translate, [LOD0, LOD1...]); LOD points are world(asset)-space and are mapped back
    to the prim's local space with the inverse of its local-to-asset transform.
    Returns total triangles per level.
    """
    levels = min(len(t[3]) for t in targets)
    vset = asset.GetVariantSets().AddVariantSet(LOD_SET)
    for i in range(levels):
        vset.AddVariant(f"LOD{i}")
    # LIVRPS: local opinions beat variants, so the base geometry cannot stay on the prims. Move it into
    # variant LOD0 (a copy of the property specs) and strip it from the local spec; the prim keeps its
    # transform, material binding and schema type. (Hence "LOD0 is a variant", not an empty one.)
    layer = stage.GetRootLayer()
    apath = asset.GetPath()
    for path, *_ in targets:
        src = layer.GetPrimAtPath(path)
        vprim = Sdf.CreatePrimInLayer(layer, apath.AppendVariantSelection(LOD_SET, "LOD0").AppendPath(Sdf.Path(path).MakeRelativePath(apath)))
        vprim.specifier = Sdf.SpecifierOver
        for prop in [p for p in src.properties if p.name in _GEOMETRY_PROPS]:
            Sdf.CopySpec(layer, prop.path, layer, vprim.path.AppendProperty(prop.name))
            src.RemoveProperty(prop)
    for i in range(1, levels):
        vset.SetVariantSelection(f"LOD{i}")
        with vset.GetVariantEditContext():
            for path, inv33, trans, lods in targets:
                lod = lods[i]
                local = (np.asarray(lod.points, dtype=np.float64) - trans) @ inv33
                author_mesh_data(stage.GetPrimAtPath(path), local, lod.triangles, lod.uvs, weld_map=None)
    vset.SetVariantSelection("LOD0")
    return [sum(t[3][i].tri_count for t in targets) for i in range(levels)]


def _author_colliders(stage: Usd.Stage, asset: Usd.Prim, engine: str, asset_name: str, hulls: list[Hull]) -> list[str]:
    """Convex collision hulls ``<asset>/Colliders/UCX_<asset>_<nn>`` (engine naming), purpose=guide."""
    if not hulls:
        return []
    scope = UsdGeom.Scope.Define(stage, asset.GetPath().AppendChild(COLLIDERS_SCOPE))
    names: list[str] = []
    for i, h in enumerate(hulls):
        name = collider_name(engine, asset_name, i)
        prim = UsdGeom.Mesh.Define(stage, scope.GetPath().AppendChild(name)).GetPrim()
        author_mesh_data(prim, h.points, h.triangles, None, normals=False)
        UsdGeom.Imageable(prim).CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
        UsdPhysics.CollisionAPI.Apply(prim)
        UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr().Set("convexHull")
        names.append(name)
    return names


def _apply_units(stage: Usd.Stage, asset: Usd.Prim, engine: str) -> dict[str, Any]:
    native = ENGINE_RULES[engine].native_up_axis
    units: dict[str, Any] = {
        "up_axis": native, "meters_per_unit": 1.0, "source_up_axis": "Z", "source_meters_per_unit": 1.0,
        "root_transform": None,
    }
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if native == "Y" else UsdGeom.Tokens.z)
    if native == "Y":
        xf = UsdGeom.Xformable(asset)
        old = xf.GetOrderedXformOps()
        rot = xf.AddRotateXOp(opSuffix="zUpToYUp")
        rot.Set(-90.0)
        xf.SetXformOpOrder([rot, *old])  # outermost: applies after the asset's own ops
        units["root_transform"] = "rotateX(-90deg): (x,y,z) -> (x,z,-y)"
    return units


# --------------------------------------------------------------------------- the writer


def write_engine_package(
    stage_manager,
    bundle_or_parts: ExportBundle | Mapping[str, Any],
    budget: EngineBudget,
    out_dir: str | Path,
    *,
    asset_name: str,
    qa: dict | None = None,
) -> PackageManifest:
    """Writes the package (see module docstring) and validates it cold. Idempotent per directory.

    ``stage_manager`` is the working :class:`StageManager`; the bundle's UVs are authored into it first
    (overlay, not a transaction: see usd_stage.py), so the stage, the exported USD and the GLB carry the
    same unwrapped topology. ``bundle_or_parts`` is an ``ExportBundle`` or a mapping with ``uvs``,
    ``lods`` and ``colliders`` (and optional ``timings_ms``).
    """
    uvs, lods, hulls, timings = _parts(bundle_or_parts)
    qa = dict(qa or {})
    engine = budget.target_engine
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _clean_previous(out)
    notes: list[str] = []

    stage_manager.author_uvs(uvs)
    meshes = stage_manager.extract_meshes()
    xc = UsdGeom.XformCache()
    src_stage = stage_manager.stage

    layer = stage_manager.export_layer()
    stage = Usd.Stage.Open(layer)
    asset = stage.GetDefaultPrim()

    textures, unresolved = _package_textures(stage, out, engine)
    if unresolved:
        notes.append(f"{len(unresolved)} texture path(s) not found on disk, left unchanged: {unresolved[:3]}")

    # LODs (variant set) -- only when every mesh has a matching chain of >= 2 levels
    lod_targets = []
    for m, u in zip(meshes, uvs):
        chain = lods.get(u.node_id)
        if not chain or chain[0].tri_count != len(u.triangles):
            lod_targets = []
            break
        mat = np.array(xc.GetLocalToWorldTransform(src_stage.GetPrimAtPath(m.prim_path)))
        lod_targets.append((m.prim_path, np.linalg.inv(mat[:3, :3]), mat[3, :3], chain))
    levels = min((len(t[3]) for t in lod_targets), default=0)
    if levels > 1:
        lod_tris = _author_lod_variants(stage, asset, lod_targets)
    else:
        lod_tris = [sum(len(u.triangles) for u in uvs)]
        if lods and not lod_targets:
            notes.append("LOD chains do not match the unwrapped meshes; LODs omitted from the USD")
    names = _author_colliders(stage, asset, engine, asset_name, hulls)
    units = _apply_units(stage, asset, engine)

    files: list[str] = []
    usd_path = out / f"{asset_name}.usd"
    stage.GetRootLayer().Export(str(usd_path))
    files.append(usd_path.name)

    usdz_name: str | None = None
    usdz_skipped: str | None = None
    try:
        if UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(usd_path)), str(out / f"{asset_name}.usdz")):
            usdz_name = f"{asset_name}.usdz"
            files.append(usdz_name)
        else:
            usdz_skipped = "UsdUtils.CreateNewUsdzPackage returned False"
    except Exception as e:  # noqa: BLE001 - pxr raises Tf.ErrorException
        usdz_skipped = f"{type(e).__name__}: {e}"[:200]

    write_gltf(out / f"{asset_name}.glb", uvs)
    files.append(f"{asset_name}.glb")
    glb_levels = min((len(v) for v in lods.values()), default=0)
    for i in range(1, glb_levels):
        nodes = sorted(lods)
        write_gltf(out / f"{asset_name}_LOD{i}.glb", [lods[n][i] for n in nodes], [f"{n}_LOD{i}" for n in nodes])
        files.append(f"{asset_name}_LOD{i}.glb")
    if hulls:
        write_gltf(out / f"{asset_name}_colliders.glb", hulls, [collider_name(engine, asset_name, i, usd=False) for i in range(len(hulls))])
        files.append(f"{asset_name}_colliders.glb")
    files.extend(t["path"] for t in textures)

    # package-level validators (UsdValidation + engine rules) on the file we actually wrote
    mesh_data = [MeshData(u.node_id, f"/{asset.GetName()}/{u.node_id}", u.points, u.triangles, u.atlas_id) for u in uvs]
    usd_issues = validate_usd(usd_path)
    eng_issues = check_engine_compliance(Usd.Stage.Open(str(usd_path)), mesh_data, budget)

    dens = [u.texel_density for u in uvs if u.texel_density > 0]
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "asset": asset_name,
        "target_engine": engine,
        "usd": usd_path.name,
        "usdz": usdz_name,
        "usdz_skipped": usdz_skipped,
        "units": units,
        "normal_convention": NORMAL_CONVENTION.get(engine, "opengl"),
        "budget": {k: getattr(budget, k) for k in ("max_tris", "max_draw_calls", "max_collision_hulls", "texel_density_px_per_m")},
        "tris": sum(len(u.triangles) for u in uvs),
        "lod_levels": max(glb_levels, len(lod_tris)),
        "lod_tris": lod_tris,
        "lod_structure": f"variantSet '{LOD_SET}' on /{asset.GetName()}: LOD0 (base) .. LOD{len(lod_tris) - 1}" if len(lod_tris) > 1 else None,
        "draw_calls": len({u.atlas_id for u in uvs}),
        "texel_density": {
            "target_px_per_m": budget.texel_density_px_per_m,
            "mean_px_per_m": round(float(np.mean(dens)), 2) if dens else None,
            "min_px_per_m": round(float(np.min(dens)), 2) if dens else None,
            "uv_stretch_max": round(max((u.stretch_mean for u in uvs), default=0.0), 4),
        },
        "uv": {"primvar": "st", "interpolation": "vertex", "normals": "faceVarying (crease 40 deg)"},
        "colliders": names,
        "collider_glb_nodes": [collider_name(engine, asset_name, i, usd=False) for i in range(len(hulls))],
        "collider_structure": f"/{asset.GetName()}/{COLLIDERS_SCOPE}/<name>: purpose=guide, CollisionAPI+MeshCollisionAPI convexHull" if names else None,
        "textures": textures,
        "qa": {k: v for k, v in qa.items() if k != "audit"},
        "audit": qa.get("audit", []),
        "timings_ms": timings,
        "versions": _versions(),
        "notes": notes,
        "files": files,
        "validation": {},
    }
    data["file_hashes"] = {f: {"sha256": _sha256(out / f), "bytes": (out / f).stat().st_size} for f in files}
    mpath = out / "manifest.json"
    mpath.write_text(json.dumps(data, indent=1))
    pkg_issues = validate_package(out)  # cold re-open of what was just written
    data["validation"] = {
        "usd": [_issue_dict(i) for i in usd_issues],
        "engine": [_issue_dict(i) for i in eng_issues],
        "package": [_issue_dict(i) for i in pkg_issues],
        "package_ok": not any(i.severity == "critical" for i in pkg_issues),
    }
    mpath.write_text(json.dumps(data, indent=1))  # the manifest is not in file_hashes, so this is stable
    return PackageManifest(out, data, pkg_issues)
