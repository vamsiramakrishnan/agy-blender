"""USD validation (real pxr.UsdValidation) and per-engine compliance rules."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import numpy as np
from pxr import Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, UsdValidation

from antigravity3d.kernel import fbx_read, provenance
from antigravity3d.kernel.fast_ops import TEXEL_TARGET_WARN_FRACTION, HullLimit, Issue, hull_plane_count
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
    #: what one convex collision mesh may hold; ``generate_colliders`` caps hulls to it and ``validate_package`` checks it
    hull_limit: HullLimit | None = None
    #: a mesh object takes one material. Roblox: "Mesh objects can only have one material assigned." (texture specifications,
    #: https://create.roblox.com/docs/art/modeling/texture-specifications) and a glTF mesh arrives as "one MeshPart for each
    #: primitive" (https://create.roblox.com/docs/art/modeling/3d-importer): several materials arrive as several MeshParts.
    #: A warning: how a SurfaceAppearance binds to each part was not observed in Studio.
    one_material_per_part: bool = False
    # Meshes must be closed solids. Where True (Roblox), tier1_qa fails an open authored mesh (not_watertight is
    # critical; elsewhere a warning, see fast_ops.closed_meshes_required) and hidden contact faces between parts are
    # not removed from what ships (fast_ops.contact_faces_removable). Unreal, Unity and Godot document no such rule
    # (they import and draw open meshes, cards and planes, with back-face culling as a matter of course); Roblox
    # does (below).
    closed_render_meshes: bool = False


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
        # PROJECT CHOICE, not an Epic limit. Epic documents no vertex or face limit for UCX hulls (FBX Static Mesh Pipeline
        # page, https://dev.epicgames.com/documentation/en-us/unreal-engine/fbx-static-mesh-pipeline-in-unreal-engine),
        # and UE5 simulates with Chaos, not PhysX. The numbers are PhysX's, the engine UE4 imported UCX hulls into and the
        # source of the common "255" UCX rule: "The number of vertices and faces of a convex mesh in PhysX is limited to
        # 255." (https://nvidia-omniverse.github.io/PhysX/physx/5.6.1/docs/Geometry.html). Conservative: a hull within
        # it ships unchanged through any PhysX-derived path. No Epic or engine-source statement on simplification of UCX
        # hulls at import was found (only forum reports).
        hull_limit=HullLimit(vertices=255, planar_faces=255, label="unreal_5: PhysX 255 vertices, 255 faces (project choice)"),
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
        # "NPOT Texture sizes generally take slightly more memory and might be slower for the GPU to sample [...] If the
        # platform or GPU does not support NPOT Texture sizes, Unity scales and pads the Texture up to the next power of
        # two size." (Unity Manual, Importing textures,
        # https://docs.unity.com/en-us/engine/6000.6/manual/materials-and-shaders/textures/importing-textures). A warning.
        require_pow2_textures=True,
        # "Convex mesh colliders are limited to 255 triangles." (Unity Manual, Mesh Collider component reference,
        # https://docs.unity3d.com/Manual/class-MeshCollider.html). 255 triangles is at most 129 vertices.
        hull_limit=HullLimit(triangles=255, label="unity_urp: convex Mesh Collider 255 triangles"),
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
        # "Using power-of-two texture sizes is recommended, but is not required." (Godot docs, Importing images,
        # https://docs.godotengine.org/en/stable/tutorials/assets_pipeline/importing_images.html): no warning.
        require_pow2_textures=False,
        # ConvexPolygonShape3D documents no limit, and Godot Physics keeps every point (measured: a 1,986-point hull
        # imported by stock Godot 4.7.2 answers ray casts within 0.2 mm of the hull). But "By default, new projects will use [Jolt] as the physics engine"
        # (https://docs.godotengine.org/en/stable/tutorials/physics/using_jolt_physics.html), and Jolt's ConvexHullShape
        # has cMaxPointsInHull = 256; past it the hull builder stops early (MaxVerticesReached) and drops points
        # (Jolt/Physics/Collision/Shape/ConvexHullShape.h and .cpp, github.com/jrouwe/JoltPhysics). Measured in stock
        # Godot 4.7.2 with Jolt: that 1,986-point hull answers ray casts up to 7.1 mm inside itself; capped, within 0.7 mm.
        hull_limit=HullLimit(vertices=256, label="godot_4: Jolt 256 hull points"),
    ),
    "roblox_r15": EngineRules(
        native_up_axis="Y",
        # Roblox imports .fbx, .gltf and .obj, not USD (https://create.roblox.com/docs/art/modeling/3d-importer); the
        # GLB is Y-up by the glTF spec. The importer's World Up defaults to "Top". The USD axis is a package convention.
        allowed_up_axes=("Y",),
        # "Length 1 stud 28 cm" (https://create.roblox.com/docs/physics/units). The importer's "Scale Unit [...] By
        # default, this is set to Studs" (3D importer page); no other option is listed there. The GLB is in metres.
        meters_per_unit=(1.0, 0.28),
        name_regex=r"^[A-Za-z0-9_]+$",
        hull_prim_regex=None,
        # "Individual meshes can not exceed 20,000 triangles." (https://create.roblox.com/docs/art/modeling/specifications)
        max_tris_per_mesh=20_000,
        # "Roblox supports up to 1024x1024 pixel spaces for texture maps."
        # (https://create.roblox.com/docs/art/modeling/texture-specifications)
        max_texture_px=1024,
        # "apply up to four physically-based rendering (PBR) textures within a SurfaceAppearance"
        # (https://create.roblox.com/docs/parts/meshes): Color/Normal/Metalness/Roughness
        max_textures_per_material=4,
        # the texture specifications state no power-of-two requirement
        require_pow2_textures=False,
        # No documented limit, and nothing to limit: Roblox does not import collision meshes. A MeshPart's collision is
        # built from its render mesh by CollisionFidelity (Box, Hull, Default, PreciseConvexDecomposition)
        # (https://create.roblox.com/docs/workspace/collisions). The hulls in a Roblox package are left as computed.
        hull_limit=None,
        notes="Roblox does not read the package's collision hulls (CollisionFidelity rebuilds collision from the render "
              "mesh); they are left as computed, deliberately.",
        # Creator Hub "Mesh specifications": "Watertight - All geometry must be watertight without exposed holes or
        # backfaces." and "Volume - Meshes cannot be 0 thickness and must have some volume."
        closed_render_meshes=True,
        one_material_per_part=True,
    ),
}


def texel_density_issues(report: dict, engine: str) -> list[Issue]:
    """Warnings from ``fast_ops.texel_density_report``: a material whose atlas missed the target density, and a spread
    across materials above ``TEXEL_SPREAD_LIMIT``. Never a failure. A target missed by less than 10 % is recorded
    (``target_met`` False) but not warned about."""
    out: list[Issue] = []
    target = report["target_px_per_m"]
    mats = report["materials"]
    for name, m in mats.items():
        if not m["target_met"] and m["px_per_m"] < target * TEXEL_TARGET_WARN_FRACTION:
            need = m.get("needed_map_px") or m["needed_atlas_px"]
            cap = m.get("engine_max_texture_px")
            reach = (f"; {need} px is above this engine's {cap} px maximum, so a larger map cannot reach it: split the material or "
                     f"shrink the part") if cap and need > cap else f" ({m['needed_atlas_px_pow2']} px as a power of two)"
            out.append(Issue(f"{engine}:texel_density_target", "warning", name,
                             f"material '{name}' reaches {m['px_per_m']:g} px/m in its {m['atlas_px']} px atlas, below the target "
                             f"{target:g}; its maps would need {need} px to hold the charts at the target{reach}"
                             + (f". Shipped at {m['map_px']} px it is {m['shipped_px_per_m']:g} px/m" if m.get("map_px") else "")))
    spread = report.get("spread")
    if spread is not None and spread > report["spread_limit"] and len(mats) > 1:
        hi, lo = report["highest"], report["lowest"]
        out.append(Issue(f"{engine}:texel_density_spread", "warning", None,
                         f"texel density differs {spread:.2f}x across materials (limit {report['spread_limit']:g}x): "
                         f"'{hi}' {mats[hi]['px_per_m']:g} px/m against '{lo}' {mats[lo]['px_per_m']:g} px/m"))
    return out


def engine_texture_px(engine: str, default: int) -> int:
    """``default`` map side, never above the engine's documented ``max_texture_px`` (the image backend generates 1024, 2048
    or 4096 directly, so a map is generated at the size it ships at instead of being shrunk afterwards)."""
    cap = ENGINE_RULES[engine].max_texture_px
    return min(default, cap) if cap else default


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


#: Largest dimension, in metres, above or below which an asset is reported as implausibly scaled for a prop or a kit
#: piece (a warning with the numbers; never a gate, never a silent rescale). The project's own templates measure 0.65 m
#: (crate) to 3.6 m (lamp post) at their largest; upstream real-world references put a door at 2.0-2.13 m, a person at
#: 1.7-1.8 m and the smallest kitbash module at 0.05 m (arjun988 prop-artist SKILL.md, realistic-style SKILL.md,
#: prop-artist/references/kitbash-library.md). 20 m leaves room for large environment pieces (5x the largest template)
#: and is what any part of 20 cm or more becomes when its centimetres are read as metres; 1 cm is a fifth of the
#: smallest kitbash module. Formats without a unit (OBJ, PLY, STL) are where a wrong unit arrives.
SCALE_MAX_M = 20.0
SCALE_MIN_M = 0.01
#: where a unit conversion must land to be offered as the likely reading (kitbash module to the largest template)
SCALE_TYPICAL_M = (0.05, 4.0)
_UNIT_READINGS = (("centimetres", 0.01), ("millimetres", 0.001), ("inches", 0.0254))


def scale_issue(meshes: list[MeshData]) -> str:
    """Why the asset's size is implausible for a prop or a kit piece, with the unit readings that would make it plausible,
    or "" when its largest dimension lies within ``SCALE_MIN_M``..``SCALE_MAX_M``."""
    pts = [np.asarray(m.points, dtype=np.float64).reshape(-1, 3) for m in meshes if len(m.points)]
    if not pts:
        return ""
    allp = np.vstack(pts)
    size = allp.max(axis=0) - allp.min(axis=0)
    largest = float(size.max())
    if SCALE_MIN_M <= largest <= SCALE_MAX_M:
        return ""
    dims = f"(x {size[0]:.3g}, y {size[1]:.3g}, z {size[2]:.3g} m)"
    lo, hi = SCALE_TYPICAL_M
    if largest > SCALE_MAX_M:
        head = f"largest dimension {largest:.3g} m {dims} is above {SCALE_MAX_M:g} m, implausible for a prop or kit piece"
        readings = [(f"{unit} read as metres", f) for unit, f in _UNIT_READINGS]
    else:
        head = f"largest dimension {largest:.3g} m {dims} is below {SCALE_MIN_M:g} m, implausible for a prop or kit piece"
        readings = [("metres written as centimetres", 100.0), ("metres written as millimetres", 1000.0)]
    fits = [f"{why}: {largest * f:.3g} m (scale x{f:g})" for why, f in readings if lo <= largest * f <= hi]
    tail = ("; if " + "; or ".join(fits)) if fits else f"; no common unit conversion brings it to {lo:g}-{hi:g} m"
    return f"{head}{tail}. Not rescaled: check the source unit."


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
    if rules.one_material_per_part and draws > 1:
        add("material_per_part", "warning", None,
            f"{draws} materials: Roblox documents \"Mesh objects can only have one material assigned\" (texture specifications) and "
            f"imports a glTF mesh as \"one MeshPart for each primitive\" (3D importer), so this asset arrives as at least {draws} "
            f"MeshParts, one per material. How a SurfaceAppearance binds to each was not checked in Studio.")
    if why := scale_issue(meshes):
        add("implausible_scale", "warning", None, why)

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
            mesh = UsdGeom.Mesh(prim)
            if why := hull_limit_issue(eng, np.asarray(mesh.GetPointsAttr().Get() or [], dtype=np.float64),
                                       _triangle_indices(mesh)):
                add("hull_limit", "critical", prim.GetName(), f"convex collision mesh over the engine's limit: {why}")
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

    _double_sided_contact(mesh_prims, meshes, add)

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
    _material_checks(stage, add)
    return out


def _double_sided_contact(mesh_prims: list[Usd.Prim], meshes: list[MeshData], add) -> None:
    """Back-to-back faces of two shipped meshes (opposite-facing, within ZFIGHT_EPS of one plane, overlapping in
    area) where either mesh is drawn double-sided: under back-face culling they are invisible, drawn from both
    sides they are two surfaces at one depth and flicker. Hidden contact faces that ship nothing (see
    fast_ops.run_export_pipeline) are already gone from ``meshes``; what is reported here survived."""
    from antigravity3d.kernel import coplanar
    from antigravity3d.kernel.fast_ops import ZFIGHT_EPS

    sided = {p.GetName() for p in mesh_prims if UsdGeom.Mesh(p).GetDoubleSidedAttr().Get()}
    if not sided or len(meshes) < 2:
        return
    parts = [coplanar.Part(m.node_id, np.asarray(m.points, np.float64), np.asarray(m.triangles)) for m in meshes]
    c = coplanar.coplanar_contacts(parts, ZFIGHT_EPS, facing="opposite")
    seen = set()
    for i, d in enumerate(c.opposite):
        for lst in d.values():
            for j, _ in lst:
                pair = (min(i, j), max(i, j))
                if pair not in seen and (meshes[i].node_id in sided or meshes[j].node_id in sided):
                    seen.add(pair)
                    add("double_sided_contact", "warning", meshes[pair[0]].node_id,
                        f"{meshes[pair[0]].node_id}/{meshes[pair[1]].node_id}: back-to-back faces of a double-sided mesh overlap (z-fighting)")


def _material_checks(stage: Usd.Stage, add) -> None:
    """Warnings about authored materials whose parameters will not do what they say (via ``read_material``)."""
    from antigravity3d.kernel.compile_ir import read_material

    mixed: set[Sdf.Path] = set()
    gprims, materials = [], []
    for p in stage.Traverse():
        if p.IsA(UsdGeom.Gprim):
            gprims.append(p)
        elif p.IsA(UsdShade.Material):
            materials.append(p)
    if any(UsdGeom.Gprim(p).GetDoubleSidedAttr().Get() for p in gprims):  # nothing can disagree otherwise
        flags: dict[Sdf.Path, set[bool]] = {}
        for p in gprims:
            bound = UsdShade.MaterialBindingAPI(p).ComputeBoundMaterial()[0]
            if bound:
                flags.setdefault(bound.GetPath(), set()).add(bool(UsdGeom.Gprim(p).GetDoubleSidedAttr().Get()))
        mixed = {path for path, f in flags.items() if len(f) > 1}
    for prim in materials:
        if prim.GetPath() in mixed:
            add("material_double_sided", "warning", prim.GetName(),
                "bound meshes disagree on doubleSided; the GLB exports one material per setting")
        shader = UsdShade.Material(prim).ComputeSurfaceSource()[0]
        opacity = shader.GetInput("opacity") if shader else None
        if not (opacity and opacity.HasConnectedSource()):  # only a wired opacity can read an albedo map's alpha
            continue
        params = read_material(UsdShade.Material(prim), double_sided=False)
        albedo = params.maps.get("albedo")
        if params.alpha_mode != "OPAQUE" and params.alpha_from_albedo and albedo is not None:
            f = albedo.resolved or albedo.authored
            try:
                from PIL import Image

                with Image.open(f) as im:
                    has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            except (OSError, ValueError):
                continue  # an unreadable map is reported by the texture checks
            if not has_alpha:
                add("material_alpha", "warning", prim.GetName(),
                    f"alpha_mode {params.alpha_mode} reads the albedo map's alpha channel but {Path(f).name} has none: fully opaque")


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


def hull_limit_issue(engine: str | None, points: np.ndarray, triangles: np.ndarray) -> str:
    """What a convex collision mesh holds beyond its engine's hull limit (``EngineRules.hull_limit``), or "".

    Vertices are counted as distinct positions of the vertices the triangles use (an engine welds coincident ones);
    faces as triangles, or planar faces where the engine counts polygons."""
    rules = ENGINE_RULES.get(engine)  # type: ignore[arg-type]
    limit = rules.hull_limit if rules is not None else None
    tris = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if limit is None or not len(tris):
        return ""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    n_vertices = len(np.unique(pts[np.unique(tris)], axis=0))
    planes = hull_plane_count(pts, tris) if limit.planar_faces is not None else 0
    excess = limit.excess(n_vertices, len(tris), planes)
    return f"{excess} ({limit.label})" if excess else ""


def _triangle_indices(mesh: UsdGeom.Mesh) -> np.ndarray:
    """Triangle vertex indices of a mesh (polygons fanned), as an (n, 3) array."""
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get() or [], dtype=np.int64)
    idx = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64)
    if len(counts) and (counts == 3).all():
        return idx.reshape(-1, 3)
    out, start = [], 0
    for c in counts.tolist():
        out.extend((idx[start], idx[start + k], idx[start + k + 1]) for k in range(1, c - 1))
        start += c
    return np.asarray(out, dtype=np.int64).reshape(-1, 3)


def _mesh_tris(prim: Usd.Prim) -> int:
    counts = UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get()
    return int(np.maximum(np.asarray(counts, dtype=np.int64) - 2, 0).sum()) if counts else 0


def _glb_json(path: Path) -> dict:
    with path.open("rb") as stream:
        magic, version, size, length, chunk = struct.unpack("<4sIII4s", stream.read(20))
        if (magic, version, chunk) != (b"glTF", 2, b"JSON") or size != path.stat().st_size or length > size - 20:
            raise ValueError(f"invalid GLB header in {path.name}")
        return json.loads(stream.read(length))


#: the package directory that holds the material maps; the only place a GLB of the package may reference a file in
PACKAGE_TEXTURE_DIR = "textures"
_PNG_JPEG = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


def glb_images(path: Path) -> list[dict]:
    """Where each image of a GLB lives, in glTF image order.

    ``{"image": i, "storage": "embedded", "mime_type", "sha256", "bytes"}`` for an image in the GLB's binary chunk
    (``sha256`` of the embedded bytes), ``{"image": i, "storage": "uri", "mime_type", "uri"}`` for one the GLB
    references by URI (``uri`` exactly as written, percent-encoded). Raises ``ValueError`` on a malformed GLB.
    """
    return _glb_images_and_json(path)[0]


def _glb_images_and_json(path: Path) -> tuple[list[dict], dict]:
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError(f"{path.name} is too short for a GLB")
    magic, version, size = struct.unpack_from("<4sII", data, 0)
    jlen, jtype = struct.unpack_from("<I4s", data, 12)
    if (magic, version, jtype) != (b"glTF", 2, b"JSON") or size != len(data) or 20 + jlen > size:
        raise ValueError(f"invalid GLB header in {path.name}")
    doc = json.loads(data[20:20 + jlen])
    blob = memoryview(b"")
    off = 20 + jlen
    if off + 8 <= size:
        blen, btype = struct.unpack_from("<I4s", data, off)
        if btype == b"BIN\0":
            blob = memoryview(data)[off + 8:off + 8 + blen]
    out = []
    for i, image in enumerate(doc.get("images") or []):
        rec: dict = {"image": i, "mime_type": image.get("mimeType")}
        if "bufferView" in image:
            view = doc["bufferViews"][image["bufferView"]]
            if "uri" in doc["buffers"][view.get("buffer", 0)]:
                raise ValueError(f"{path.name}: image {i} lives in an external buffer")
            start = view.get("byteOffset", 0)
            chunk = blob[start:start + view["byteLength"]]
            if len(chunk) != view["byteLength"] or not len(chunk):
                raise ValueError(f"{path.name}: image {i} buffer view lies outside the binary chunk")
            rec.update(storage="embedded", sha256=hashlib.sha256(chunk).hexdigest(), bytes=len(chunk))
        elif isinstance(image.get("uri"), str) and image["uri"]:
            rec.update(storage="uri", uri=image["uri"])
        else:
            raise ValueError(f"{path.name}: image {i} has neither a buffer view nor a URI")
        out.append(rec)
    return out, doc


def glb_uri_target(glb: Path, uri: str) -> Path | None:
    """The file a relative image URI of ``glb`` names (RFC 3986 percent-decoding), or None for an absolute
    path, a scheme (``file:``, ``http:``, ``data:``...) or a query/fragment."""
    if not uri or "://" in uri or ":" in uri.split("/", 1)[0] or uri.startswith(("/", "\\")) or any(c in uri for c in "?#"):
        return None
    rel = unquote(uri)
    if Path(rel).is_absolute():
        return None
    return glb.parent / rel


def _material_textures(doc: dict, shas: list[str | None]) -> dict[str, dict[str, str | None]]:
    """``{material name: {slot path: image sha256}}`` for every texture slot of every material, extension slots
    included (any object with an ``index`` under a key ending in ``Texture``)."""
    out: dict[str, dict[str, str | None]] = {}

    def walk(value, prefix: str, slots: dict) -> None:
        if isinstance(value, dict):
            for key, v in value.items():
                if isinstance(v, dict) and "index" in v and key.endswith("Texture"):
                    texture = doc["textures"][v["index"]]
                    slots[prefix + key] = shas[texture["source"]]
                else:
                    walk(v, f"{prefix}{key}.", slots)

    for mat in doc.get("materials") or []:
        walk(mat, "", out.setdefault(mat.get("name") or "", {}))
    return out


def _validate_glb_materials(path: Path, suffix: str, bindings: dict[str, tuple[str, set[str]]], *,
                            allow_uri: bool = False) -> list[Issue]:
    """All authored per-mesh PBR slots must reference an image: in this GLB's own binary chunk, or with
    ``allow_uri`` (a far LOD file) by URI (where that URI leads is checked by ``validate_package``)."""
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
                         (mat, "occlusionTexture")], "normal": [(mat, "normalTexture")],
                         "emissive": [(mat, "emissiveTexture")]}
                for role in roles:
                    for obj, slot in slots[role]:
                        texture = doc["textures"][obj[slot]["index"]]
                        image = doc["images"][texture["source"]]
                        if allow_uri and "bufferView" not in image and image.get("uri"):
                            continue
                        view = doc["bufferViews"][image["bufferView"]]
                        if view["byteLength"] < 1 or "uri" in doc["buffers"][view["buffer"]]:
                            raise ValueError(f"{node}/{slot} must be an embedded image")
    except (IndexError, KeyError, TypeError, ValueError, OSError, struct.error) as e:
        issues.append(Issue("pkg:glb_material", "critical", path.name, str(e)[:300]))
    return issues


def _same(a, b) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= 1e-9
    return a == b


def _glb_material_record(mat: dict) -> dict:
    """A GLB material in the shape of a manifest ``materials[]`` record (glTF defaults filled in)."""
    pbr = mat.get("pbrMetallicRoughness", {})
    return {
        "name": mat.get("name"), "base_color_factor": pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0]),
        "metallic_factor": pbr.get("metallicFactor", 1.0), "roughness_factor": pbr.get("roughnessFactor", 1.0),
        "emissive_factor": mat.get("emissiveFactor", [0.0, 0.0, 0.0]), "alpha_mode": mat.get("alphaMode", "OPAQUE"),
        "alpha_cutoff": mat.get("alphaCutoff", 0.5) if mat.get("alphaMode") == "MASK" else None,
        "double_sided": bool(mat.get("doubleSided", False)),
        "extensions": {k: {f: v for f, v in body.items() if not isinstance(v, dict)} for k, body in (mat.get("extensions") or {}).items()},
    }


def _validate_material_inventory(path: Path, inventory: list[dict]) -> list[Issue]:
    """The manifest's ``materials`` must be exactly the materials the GLB declares, parameter for parameter,
    and every extension used must be listed in ``extensionsUsed`` and none in ``extensionsRequired``."""
    try:
        doc = _glb_json(path)
        actual = [_glb_material_record(m) for m in doc.get("materials", [])]
        want = [{k: v for k, v in rec.items() if k != "texture_slots"} for rec in inventory]
        left = list(actual)
        for rec in want:
            hit = next((i for i, a in enumerate(left) if _same(a, rec)), None)
            if hit is None:
                return [Issue("pkg:material_inventory", "critical", rec["name"],
                              f"manifest material {rec['name']!r} is not in {path.name} as stated: {rec}"[:300])]
            left.pop(hit)
        # a mesh without UVs gets a factor-only copy of its material (same name, no texture slots): not a stranger
        left = [a for a in left if a["name"] not in {rec["name"] for rec in want}]
        if left:
            return [Issue("pkg:material_inventory", "critical", path.name,
                          f"{len(left)} GLB material(s) missing from the manifest: {[a['name'] for a in left]}")]
        used = {e for rec in want for e in rec["extensions"]}
        declared = set(doc.get("extensionsUsed", []))
        if used - declared or doc.get("extensionsRequired"):
            return [Issue("pkg:material_extensions", "critical", path.name,
                          f"extensionsUsed {sorted(declared)} must cover {sorted(used)}; extensionsRequired must be empty")]
    except (IndexError, KeyError, TypeError, ValueError, OSError, struct.error) as e:
        return [Issue("pkg:material_inventory", "critical", path.name, str(e)[:300])]
    return []


#: engines that get an FBX, and the upstream export_asset preset used for each (Godot imports glTF; it gets none)
FBX_PRESET = {"unity_urp": "unity", "unreal_5": "unreal", "roblox_r15": "roblox"}


def glb_primitive_triangles(glb: Path) -> dict[str, int]:
    """Triangles per mesh primitive of a GLB, by mesh name (``Batch_0`` ...): what an importer makes one mesh of."""
    doc = _glb_json(glb)
    out: dict[str, int] = {}
    for i, mesh in enumerate(doc.get("meshes", [])):
        for j, prim in enumerate(mesh.get("primitives", [])):
            acc = doc["accessors"][prim["indices"]]["count"] if "indices" in prim else doc["accessors"][prim["attributes"]["POSITION"]]["count"]
            out[f"{mesh.get('name', f'mesh{i}')}" + (f"#{j}" if len(mesh["primitives"]) > 1 else "")] = acc // 3
    return out


def _check_fbx(out: Path, out_real: Path, man: dict, digests: dict[str, str], add) -> None:
    """An FBX listed in the manifest: present (hash checked with the other files), a binary FBX, described by an ``fbx``
    record that agrees with the engine, and whose texture references do what the record says: relative paths that
    resolve inside the package (unity, unreal) or embedded bytes (roblox). No FBX listed: nothing to check."""
    listed = [f for f in man.get("files") or [] if isinstance(f, str) and f.lower().endswith(".fbx")]
    rec = man.get("fbx")
    if not listed and rec is None:
        return
    if isinstance(rec, dict) and rec.get("status") == "failed":  # the FBX could not be made: nothing may ship under its name
        if listed:
            add("fbx_record", "critical", listed[0], "the fbx record says the export failed, but an FBX file is listed")
        return
    if len(listed) != 1 or not isinstance(rec, dict) or rec.get("file") != (listed[0] if listed else None):
        add("fbx_record", "critical", None, f"manifest lists FBX files {listed} but its fbx record is {'missing' if not isinstance(rec, dict) else repr(rec.get('file'))}")
        return
    name = listed[0]
    path = out / name
    engine = man.get("target_engine")
    if FBX_PRESET.get(engine) != rec.get("preset"):
        add("fbx_preset", "critical", name, f"preset {rec.get('preset')!r} is not the one for {engine} ({FBX_PRESET.get(engine)!r})")
    if not (rec.get("comparison") or {}).get("ok"):
        add("fbx_comparison", "critical", name, "the fbx record says the round-trip comparison did not pass")
    if not path.is_file():
        return  # file_missing is already reported by the hash loop
    try:
        refs = fbx_read.texture_references(path)
    except (fbx_read.FbxFormatError, OSError, struct.error) as e:
        add("fbx_not_binary", "critical", name, f"not a readable binary FBX: {e}"[:200])
        return
    mode = (rec.get("textures") or {}).get("mode")
    recorded = {r.get("relative"): r.get("sha256") for r in (rec.get("textures") or {}).get("references", [])}
    if len(refs) != len(recorded):
        add("fbx_textures", "critical", name, f"the file has {len(refs)} texture references, the record {len(recorded)}")
    for ref in refs:
        label = f"{name}:{ref['name']}"
        if mode == "embedded":
            if not ref["embedded_bytes"]:
                add("fbx_texture_not_embedded", "critical", label, "the record says the textures are embedded; this one carries no bytes")
            continue
        rel = ref["relative"] or ""
        if not rel or Path(rel).is_absolute() or "://" in rel or "\\" in rel:
            add("fbx_texture_not_relative", "critical", label, f"reference {rel!r} is not a relative path: the package is not relocatable")
            continue
        target = (out / rel).resolve()
        if not target.is_relative_to(out_real):
            add("fbx_texture_outside", "critical", label, f"reference {rel!r} leaves the package")
        elif not target.is_file():
            add("fbx_texture_missing", "critical", label, f"reference {rel!r} does not resolve inside the package")
        elif recorded.get(rel) and _sha256_file(target) != recorded[rel]:
            add("fbx_texture_hash", "critical", label, f"{rel} is not the file the FBX was compared with")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_glb_images(out: Path, man: dict, asset: str, digests: dict[str, str], add) -> None:
    """Where the package GLBs keep their images (manifest ``glb_images``).

    ``<asset>.glb`` (LOD0) is the single-file deliverable: every image embedded. ``<asset>_LOD<n>.glb`` (n >= 1)
    reference the package's ``textures/`` files by relative URI; each must resolve to a file inside ``textures/``
    that exists and has the content the manifest records for it. A far LOD may embed an image only when no
    package file holds those bytes (a source map in a format glTF does not allow, transcoded to PNG once, exactly
    as LOD0 embeds it). Every level binds, per material and slot, the same image content as LOD0. The manifest's
    record of each GLB must be what the file holds.

    Versions: the rule that far LODs must not embed images (and that ``glb_images`` must exist) applies from
    ``schema_version`` 2, whose contract introduced it; every other rule here applies to version 1 packages too.
    """
    version = int(man.get("schema_version") or 1)
    out_real = out.resolve()
    tex_root = (out / PACKAGE_TEXTURE_DIR).resolve()
    hashes = man.get("file_hashes") or {}
    texture_shas = {meta.get("sha256") for rel, meta in hashes.items() if rel.startswith(PACKAGE_TEXTURE_DIR + "/")}
    lod_re = re.compile(rf"^{re.escape(asset)}_LOD(\d+)\.glb$")
    glbs = [f for f in man.get("files") or [] if isinstance(f, str) and f.endswith(".glb")]
    recorded = man.get("glb_images")
    if recorded is None and version >= 2:
        add("glb_images_manifest", "critical", None, "manifest has no glb_images record")
    elif recorded is not None and sorted(recorded) != sorted(glbs):
        add("glb_images_manifest", "critical", None, f"glb_images covers {sorted(recorded)}, the package has {sorted(glbs)}")

    def digest_of(path: Path) -> str:
        rel = path.resolve().relative_to(out_real).as_posix()
        if rel not in digests:
            digests[rel] = _sha256(path)
        return digests[rel]

    def has_untranscodable_texture() -> bool:
        for rel in hashes:
            f = out / rel
            if rel.startswith(PACKAGE_TEXTURE_DIR + "/") and f.is_file():
                with f.open("rb") as stream:
                    if not stream.read(8).startswith(_PNG_JPEG):
                        return True
        return False

    by_level: dict[int, dict[str, dict[str, str | None]]] = {}
    lod0_embedded: set[str] = set()
    for name in sorted(glbs, key=lambda n: (0 if n == f"{asset}.glb" else 1, n)):  # LOD0 first
        path = out / name
        if not path.is_file():
            continue  # reported as file_missing
        m = lod_re.match(name)
        level = 0 if name == f"{asset}.glb" else int(m.group(1)) if m else None
        try:
            table, doc = _glb_images_and_json(path)
        except (IndexError, KeyError, TypeError, ValueError, OSError, struct.error) as e:
            add("glb_open", "critical", name, str(e))
            continue
        shas: list[str | None] = []
        for rec in table:
            if rec["storage"] == "embedded":
                shas.append(rec["sha256"])
                if level == 0:
                    lod0_embedded.add(rec["sha256"])
                elif level is not None and version >= 2 and (rec["sha256"] in texture_shas or rec["sha256"] not in lod0_embedded
                                            or not has_untranscodable_texture()):
                    add("lod_embeds_image", "critical", name,
                        f"image {rec['image']} is embedded; a far LOD references the package's textures/ instead")
                continue
            uri = rec["uri"]
            target = glb_uri_target(path, uri)
            shas.append(None)
            if level == 0:
                add("glb_not_self_contained", "critical", name, f"image {rec['image']} references {uri!r}; LOD0 must embed every image")
            if target is None or not target.resolve().is_relative_to(tex_root):
                add("glb_image_outside", "critical", name, f"image {rec['image']} references {uri!r}, outside {PACKAGE_TEXTURE_DIR}/")
                continue
            if not target.is_file():
                add("glb_image_missing", "critical", name, f"image {rec['image']} references {uri!r}, which does not exist")
                continue
            rel = target.resolve().relative_to(out_real).as_posix()
            actual = digest_of(target)
            shas[-1] = actual
            rec["sha256"] = actual
            want = (hashes.get(rel) or {}).get("sha256")
            if want is None:
                add("glb_image_unlisted", "critical", name, f"image {rec['image']} references {rel}, which the manifest does not list")
            elif actual != want:
                add("glb_image_hash", "critical", name, f"image {rec['image']} references {rel}, whose content differs from the manifest")
        if recorded is not None and name in recorded:
            stated = recorded[name]
            keys = ("image", "storage", "mime_type", "sha256", "uri")
            if not isinstance(stated, list) or len(stated) != len(table) or any(
                    {k: r.get(k) for k in keys} != {k: s.get(k) for k in keys} for r, s in zip(table, stated)):
                add("glb_images_manifest", "critical", name, "glb_images differs from the images the file holds")
            elif any(s.get("texture") is not None and (hashes.get(s["texture"]) or {}).get("sha256") != s.get("sha256") for s in stated):
                add("glb_images_manifest", "critical", name, "a glb_images record names a texture file with other content")
        if level is not None:
            try:
                by_level[level] = _material_textures(doc, shas)
            except (IndexError, KeyError, TypeError) as e:
                add("glb_material", "critical", name, str(e)[:200])
    base = by_level.get(0)
    for level, materials in sorted(by_level.items()):
        if not level or base is None:
            continue
        for mat, slots in materials.items():
            want = base.get(mat)
            if want is None:
                add("lod_texture_differs", "critical", f"{asset}_LOD{level}.glb", f"material {mat!r} is not in LOD0")
            elif slots != want:
                diff = sorted(k for k in set(slots) | set(want) if slots.get(k) != want.get(k))
                add("lod_texture_differs", "critical", f"{asset}_LOD{level}.glb",
                    f"material {mat!r} binds other image content than LOD0 in {diff[:4]}")


def _check_art_direction(out: Path, man: dict, root: Usd.Prim, hull_re: re.Pattern | None, add) -> None:
    """The manifest's ``art_direction`` record (schema_version 3) against the files it describes.

    Checks the triangle budget it states against the manifest's budget and triangle count, the texture filtering of
    every texture in every package GLB, the shading crease of every LOD0 USD mesh's normals (recomputed with
    ``compile_ir.crease_normals``), the size of every packaged map and the colour count of every albedo map. The record
    is optional from version 3 on (absent: no art direction was applied); a record in an older version is rejected
    (its contract did not have it).
    """
    version = int(man.get("schema_version") or 1)
    art = man.get("art_direction")
    if art is None:
        return
    if version < 3:
        add("art_direction_manifest", "critical", None, f"art_direction record needs schema_version 3, manifest has {version}")
        return
    try:
        eff = art["effective"]
        tag = f"art direction {art['id']}"
        budget = man.get("budget") or {}
        if eff["max_tris"] != budget.get("max_tris"):
            add("art_budget", "critical", None, f"{tag}: max_tris {eff['max_tris']} != manifest budget {budget.get('max_tris')}")
        if int(man.get("tris") or 0) > eff["max_tris"]:
            add("art_budget", "critical", None, f"{tag}: {man.get('tris')} tris > {eff['max_tris']}")
        want = (9728, 9984) if eff["texture_filter"] == "nearest" else (None, None)
        for name in [f for f in man.get("files") or [] if isinstance(f, str) and f.endswith(".glb")]:
            path = out / name
            if not path.is_file():
                continue
            doc = _glb_json(path)
            samplers = doc.get("samplers") or []
            for i, tex in enumerate(doc.get("textures") or []):
                smp = samplers[tex["sampler"]] if "sampler" in tex else {}
                got = (smp.get("magFilter"), smp.get("minFilter"))
                if got != want:
                    add("art_texture_filter", "critical", name, f"{tag}: texture {i} filters {got}, expected {want}")
        from antigravity3d.kernel.compile_ir import crease_normals

        crease = float(eff["shading_crease_deg"])
        for prim in Usd.PrimRange(root):
            if not prim.IsA(UsdGeom.Mesh) or is_collider_prim(prim, hull_re):
                continue
            mesh = UsdGeom.Mesh(prim)
            pts = np.asarray(mesh.GetPointsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
            tri = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64).reshape(-1, 3)
            normals = mesh.GetNormalsAttr().Get()
            if not len(tri) or normals is None:
                continue
            have = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
            expect = crease_normals(pts, tri, crease)
            if have.shape != expect.shape or np.abs(have - expect).max() > 1e-4:
                add("art_crease", "critical", prim.GetName(), f"{tag}: normals are not the {crease:g} deg crease normals")
        limit_px = eff["texture_px"]
        limit_colors = eff["palette_max_colors"]
        counted: set[str] = set()
        for t in man.get("textures") or []:
            rel = t["path"]
            if limit_px is not None and max(t["width"], t["height"]) > limit_px:
                add("art_texture_px", "critical", rel, f"{tag}: {t['width']}x{t['height']} > {limit_px} px")
            if limit_colors is not None and t["role"] == "albedo" and rel not in counted and (out / rel).is_file():
                counted.add(rel)
                from PIL import Image

                with Image.open(out / rel) as im:
                    a = np.asarray(im.convert("RGB"), dtype=np.uint32).reshape(-1, 3)
                n = len(np.unique((a[:, 0] << 16) | (a[:, 1] << 8) | a[:, 2]))
                if n > limit_colors:
                    add("art_palette", "critical", rel, f"{tag}: albedo has {n} colours > {limit_colors}")
    except (KeyError, IndexError, TypeError, ValueError, OSError, struct.error) as e:
        add("art_direction_manifest", "critical", None, f"unreadable art_direction record or file: {e}"[:300])


def validate_package(out_dir: str | Path) -> list[Issue]:
    """Cold re-open of a written engine package (see kernel/package.py) and consistency checks.

    Checks: manifest present, every file hash matches, USD opens with a default prim, units match the
    manifest, every mesh bound to a textured material has ``st`` (count matches its interpolation) in
    every LOD variant, every texture path is relative, resolves and stays inside the package, LOD
    triangle counts never increase, the GLB LOD0 matches the USD LOD0, collider prims are named per the
    engine rules, tagged ``UsdPhysics`` convexHull + purpose guide, are really convex, and stay within the engine's hull
    limit (``EngineRules.hull_limit``; in the USD and in ``<asset>_colliders.glb``). GLB images: LOD0 embeds
    every image; far LOD files reference ``textures/`` files that exist, lie inside ``textures/`` and hash as the
    manifest records, embed none, and bind the same image content as LOD0 (see ``_check_glb_images``). An
    ``art_direction`` record (schema_version 3) is checked against the files (see ``_check_art_direction``). From
    schema_version 4 the ``provenance`` record must account for every listed file and be what its own graph resolves to
    (``kernel.provenance.problems``).
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

    digests: dict[str, str] = {}  # package-relative path -> sha256 of the file on disk (each file hashed once)
    for rel, meta in (man.get("file_hashes") or {}).items():
        f = package_file(rel)
        if f is None:
            continue
        if not f.is_file():
            add("file_missing", "critical", rel, "listed in manifest but not on disk")
            continue
        digests[rel] = _sha256(f)
        if digests[rel] != meta.get("sha256"):
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
                roles = {role for role in ("albedo", "orm", "normal", "emissive") if mat.GetPrim().GetChild(role + "Tex")}
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
    if vset is not None and lod_names:
        vset.SetVariantSelection(lod_names[0])  # the checks below are about LOD0 (parts may be absent further out)

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

    # Materials baked into one texture set ship as one glTF material (manifest ``texture_atlas.gltf_materials``: the
    # shipped name and the USD materials it holds); the USD keeps one material per surface, all reading the baked maps.
    alias = {src: rec["material"] for rec in (man.get("texture_atlas") or {}).get("gltf_materials") or [] for src in rec.get("from") or []}
    if alias:
        bindings = {n: (alias.get(mat, mat), roles) for n, (mat, roles) in bindings.items()}
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
    per_level = man.get("lod_glb_nodes")  # parts absent at a level leave their (all-absent) batch out of that GLB
    for level in range(max(1, len(lod_tris))):
        suffix = f"_LOD{level}" if level else ""
        want = bindings
        if level and isinstance(per_level, list) and level < len(per_level):
            want = {n: b for n, b in bindings.items() if n in set(per_level[level])}
            try:
                names = {n.get("name") for n in _glb_json(out / f"{asset}{suffix}.glb")["nodes"] if n.get("mesh") is not None}
                expect = {n + suffix for n in per_level[level]}
                if names != expect:
                    add("lod_glb_nodes", "critical", f"{asset}{suffix}.glb", f"GLB nodes {sorted(names)} != manifest {sorted(expect)}")
            except (OSError, KeyError, ValueError, struct.error) as e:
                add("glb_open", "critical", f"{asset}{suffix}.glb", str(e)[:200])
        issues.extend(_validate_glb_materials(out / f"{asset}{suffix}.glb", suffix, want, allow_uri=bool(level)))
    if "materials" in man and glb.is_file():
        issues.extend(_validate_material_inventory(glb, man["materials"]))
    _check_glb_images(out, man, asset, digests, add)
    _check_fbx(out, out_real, man, digests, add)
    if rules is not None and rules.max_tris_per_mesh and glb.is_file():  # what ships, not what was authored (see package.batch_tri_cap_issues)
        try:
            for batch, tris in glb_primitive_triangles(glb).items():
                if tris > rules.max_tris_per_mesh:
                    add("glb_batch_tri_cap", "critical", batch, f"{tris} tris in one shipped GLB mesh > {rules.max_tris_per_mesh} per mesh")
        except (OSError, KeyError, ValueError, struct.error) as e:
            add("glb_open", "critical", glb.name, str(e)[:200])

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
        if why := hull_limit_issue(engine, pts, idx):
            add("collider_limit", "critical", n, f"convex collision mesh over the engine's limit: {why}")
    # the collider GLB carries the same hulls under engine names (what Godot's importer reads): check what it holds
    glb_hulls = out / f"{asset}_colliders.glb"
    if man.get("collider_glb_nodes") and glb_hulls.is_file():
        try:
            import trimesh

            scene = trimesh.load(glb_hulls, force="scene", process=False)
            for node in man["collider_glb_nodes"]:
                if node not in scene.graph.nodes_geometry:
                    add("collider_glb", "critical", node, f"{glb_hulls.name} has no mesh node {node!r}")
                    continue
                geom = scene.geometry[scene.graph[node][1]]
                if why := hull_limit_issue(engine, np.asarray(geom.vertices), np.asarray(geom.faces)):
                    add("collider_limit", "critical", node, f"{glb_hulls.name}: convex collision mesh over the engine's limit: {why}")
        except Exception as e:  # noqa: BLE001 - unreadable GLB
            add("glb_open", "critical", glb_hulls.name, str(e)[:200])
    _check_art_direction(out, man, root, hull_re, add)
    if int(man.get("schema_version") or 1) >= provenance.SCHEMA_VERSION:  # the record's contract: every listed file is accounted for
        for detail in provenance.problems(man):
            add("provenance", "critical", None, detail)
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
