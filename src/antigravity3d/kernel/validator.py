"""USD validation (real pxr.UsdValidation) and per-engine compliance rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pxr import Sdf, Usd, UsdGeom, UsdShade, UsdValidation

from antigravity3d.kernel.fast_ops import Issue
from antigravity3d.kernel.types import MeshData
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
    collision_names = lambda p: bool(hull_re and hull_re.search(p.GetName()))  # noqa: E731
    for prim in mesh_prims:
        if collision_names(prim):
            continue
        pv = UsdGeom.PrimvarsAPI(prim)
        has_uv = any(pv.GetPrimvar(n) and pv.GetPrimvar(n).HasValue() for n in ("st", "st0", "UVMap"))
        if not has_uv:
            add("uv_missing", "critical" if any_tex else "warning", prim.GetName(), "no st primvar")

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
