"""Survey: what the reviewed EL4CTEO/blender-skills checks say about what this pipeline ships (perf_engines section).

Measure before gating. Nothing here gates anything: every upstream verdict is recorded next to what the project's
own checks say about the same asset, and where both measure the same quantity the two numbers are put side by side.

Subjects, each run through the worker's ``inspect_usd`` (import into an empty scene, then one primitive):
  * props (crate, barrel, sphere) packaged for each target engine: the package USD per engine, and ``<asset>.glb``
    (written in world space with no node transforms; one call set per distinct GLB). ``qa_check``, ``texel_density``
    and ``mesh_stats``;
  * synthetic defects with known answers, through the same package path (and, for "no UVs", the welded working
    geometry written with the stage writer, since every package mesh is unwrapped);
  * an offline environment kit (unreal_5, Z-up, as ``agy3d kit --offline`` builds it; and godot_4, Y-up, for the
    importer's up-axis conversion): ``kit_check`` on every piece file, with upstream's defaults and with the
    origin convention the piece templates imply (the project states none).
Topology checks run twice: on the scene as imported, and with ``weld=True`` (the worker merges coincident vertices
of each imported mesh; UVs are per face corner, so only connectivity changes). The package writes one vertex per
(position, UV) and the GLB splits at every seam and hard edge too, so the difference between the two runs is the
part of upstream's verdict that is about how the file is written rather than about the asset.

Every call records its kwargs, file, triangle count, duration and outcome: ``ok``, ``error`` (with the error),
``checks_skipped`` (qa_check above its own ``max_tris`` skips z-fighting and floating and still says ``pass``: never
counted as a pass), or ``skipped`` (time budget).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

SURVEY_SHARE = 0.8  # of the benchmark's soft budget: later calls are recorded as skipped (Godot runs after the Blender section)
SURVEY_ENGINES = ("unreal_5", "unity_urp", "godot_4", "roblox_r15")
QUICK_ENGINES = ("unreal_5", "roblox_r15")
QA_PROFILE = {"roblox_r15": "roblox"}  # everything else: "game"
MESH_BUDGET = {
    "unreal_5": ("console", "Unreal's main targets are PC and console; 'console' is the stricter of the two and its 4-material limit "
                            "equals the project's max_draw_calls (4, the same for every engine)"),
    "unity_urp": ("mobile", "URP is Unity's scalable pipeline whose reason to exist is mobile and low-end hardware; 'mobile' is the "
                            "strictest preset, so it shows where the project's budget (the same 15,000 tris for every engine) is looser"),
    "godot_4": ("pc", "Godot 4's default renderer (Forward+) is desktop-class and the project verifies packages with Godot's "
                      "desktop build"),
    "roblox_r15": ("roblox", "exact match"),
}
KIT_ENGINES = ("unreal_5", "godot_4")
KIT_SHEET_PX = 1024  # what `agy3d kit --offline` uses
KIT_CONVENTION = {
    "stated": None,
    "searched": ["dags/environment_kit.py", "kernel/trim_atlas.py", "tactics/authoring.py (piece templates)",
                 "plugin/antigravity-3d/skills/parametric-kitbash/SKILL.md", "README.md"],
    "finding": "no grid or pivot convention is stated anywhere. The piece templates (tactics/authoring.py _wall, _floor, _pillar, "
               "_doorway) centre each piece on its origin in X/Y with its base at z=0; widths are 2.0 m, heights are random "
               "(2.6-3.2 m). A piece is several mesh objects (slab, trims...), while kit_check judges every object as a piece.",
    "runs": {"upstream_defaults": {"grid": 0.5, "pivot": "corner"},
             "template_origin": {"grid": 0.5, "pivot": "bottom-center"}},
}
FILE_READERS = (
    "The project's engine validators are all handed <asset>.glb (tactics/pyramid.py: validator.validate(res.artifacts['glb'])): "
    "Godot imports it headless, Unreal through AssetImportTask, Unity after staging it under Assets/; the three.js verifier loads "
    "the GLB and compares it with the USD silhouette. No validator feeds the package USD to an engine; it is the reviewed "
    "OpenUSD deliverable (USD-aware importers: Unreal's USD importer, Omniverse, usdview). Roblox has no headless verifier.")

# upstream message -> kind (qa_check.py and kit_check.py wording at the pinned commit)
KINDS = (
    ("checks_skipped", r"z-fighting/floating checks skipped"), ("zfighting", r"^z-fighting:"), ("floating", r"floating/detached part"),
    ("back_to_back", r"hidden back-to-back faces"), ("enclosed", r"fully enclosed"), ("inverted", r"normals pointing inward"),
    ("inconsistent_winding", r"inconsistent face winding"), ("non_manifold", r"non-manifold edges"),
    ("open_boundary", r"open boundary edges"), ("degenerate", r"degenerate faces"), ("duplicate_verts", r"duplicate verts"),
    ("ngons", r"n-gons"), ("negative_scale", r"negative scale"), ("unapplied_scale", r"unapplied scale"),
    ("unapplied_rotation", r"unapplied rotation"), ("no_uv", r"no UV map|^no UVs$"), ("uv_zero_area", r"zero-area UV faces"),
    ("uv_out_of_bounds", r"UVs outside 0-1"), ("uv_overlap", r"UV overlap"), ("unused_material_slot", r"unused material slot"),
    ("material_slot", r"empty/missing material slot"), ("missing_texture", r"missing texture file"),
    ("colorspace", r"Non-Color"), ("image_without_file", r"has no image"), ("weights", r"deform weights|bone influences"),
    ("tri_limit", r"tris > .* limit"), ("not_grounded", r"not resting on ground plane"),
    ("kit_footprint_off_grid", r"footprint .* is not a multiple"), ("kit_height_off_grid", r"height .* is off-grid"),
    ("kit_pivot", r"pivot is not at the"), ("kit_placed_off_grid", r"placed off-grid"), ("kit_inner_open_edges", r"open-edge verts inside"),
    ("kit_seam_off_grid", r"seam verts off the grid"), ("kit_name_prefix", r"name lacks"), ("kit_texel_spread", r"vs kit median"),
)
# upstream kind -> tier1_qa issue code ("" = the project has no counterpart)
PROJECT_COUNTERPART = {
    "floating": "floating_part", "inverted": "inverted_normals", "inconsistent_winding": "inconsistent_winding",
    "open_boundary": "not_watertight", "non_manifold": "non_manifold", "degenerate": "degenerate_tris", "not_grounded": "ungrounded",
    "tri_limit": "tri_budget", "zfighting": "", "back_to_back": "", "unapplied_scale": "", "duplicate_verts": "", "enclosed": "",
}

# Planted defects. "should" are the kinds upstream ought to report for the defect, "should_not" what it must not report
# (back-to-back faces are normal in kit-bashed props and must not be an error). "project" is the tier1_qa code that
# the project ought to raise ("" = no counterpart exists).
DEFECTS = {
    "zfight": {"what": "two boxes whose top faces are coplanar (z=0.5), face +Z and overlap in area",
               "should": ["zfighting"], "should_not": [], "project": ""},
    "back_to_back": {"what": "two 1 m boxes pressed face to face at x=0.5",
                     "should": ["back_to_back"], "should_not": ["zfighting", "error:back_to_back"], "project": ""},
    "floating": {"what": "a 0.3 m box 0.35 m clear above a grounded 1 m box",
                 "should": ["floating"], "should_not": [], "project": "floating_part"},
    "inverted": {"what": "one closed box (mesh_ref of an OBJ) with every triangle's winding reversed",
                 "should": ["inverted"], "should_not": [], "project": "inverted_normals"},
    "no_uv": {"what": "a box without UVs: only the working-stage geometry can lack them (every package mesh is unwrapped)",
              "should": ["no_uv"], "should_not": [], "project": ""},
}


def classify(msg: str) -> str:
    for kind, pattern in KINDS:
        if re.search(pattern, msg):
            return kind
    return "other"


def kinds_of(result: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    """{"errors": {kind: n}, "warnings": {kind: n}} of a qa_check result or a kit_check result (all pieces)."""
    out = {"errors": Counter(), "warnings": Counter()}
    if not result:
        return {"errors": {}, "warnings": {}}
    if "pieces" in result:
        for piece in result["pieces"].values():
            for sev in ("errors", "warnings"):
                out[sev].update(classify(m) for m in piece.get(sev, []))
    else:
        for sev in ("errors", "warnings"):
            out[sev].update(classify(m) for m in result.get(sev, []))
    return {sev: dict(sorted(c.items())) for sev, c in out.items()}


def outcome(primitive: str, result: dict[str, Any]) -> str:
    if primitive == "qa_check" and any(classify(w) == "checks_skipped" for w in result.get("warnings", [])):
        return "checks_skipped"
    return "ok"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


# ------------------------------------------------------------------------------------------------ offline facts

def _topology(points: Any, tris: Any) -> dict[str, Any]:
    """What a connectivity-based check will see in these vertex arrays, as written."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    pts, tri = np.asarray(points, np.float64), np.asarray(tris, np.int64).reshape(-1, 3)
    n = len(pts)
    a, b = tri.ravel(), tri[:, [1, 2, 0]].ravel()
    comps = int(connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n)), directed=False)[0]) if n else 0
    edges = np.sort(np.stack([a, b], 1), 1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    _, pos_counts = np.unique(pts.round(6), axis=0, return_counts=True)
    return {"points": n, "tris": len(tri), "components": comps, "boundary_edges": int((counts == 1).sum()),
            "points_sharing_a_position": int(pos_counts[pos_counts > 1].sum())}


def usd_topology(path: Path) -> dict[str, Any]:
    """Per mesh prim of a package USD (default variants): topology as written, local transform, purpose."""
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(str(path))
    out: dict[str, Any] = {"up_axis": str(UsdGeom.GetStageUpAxis(stage)), "meters_per_unit": UsdGeom.GetStageMetersPerUnit(stage),
                           "root_xform_ops": [op.GetOpName() + "=" + str(op.Get()) for op in UsdGeom.Xformable(stage.GetDefaultPrim()).GetOrderedXformOps()],
                           "meshes": {}}
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        pts, idx = mesh.GetPointsAttr().Get(), mesh.GetFaceVertexIndicesAttr().Get()
        if not pts or not idx:
            continue
        entry = _topology(pts, idx)
        ops = {op.GetOpName(): op.Get() for op in UsdGeom.Xformable(prim).GetOrderedXformOps()}
        entry["xform_ops"] = {k: [round(float(c), 4) for c in v] if hasattr(v, "__len__") else _r(v) for k, v in ops.items()}
        entry["purpose"] = str(UsdGeom.Imageable(prim).GetPurposeAttr().Get())
        out["meshes"][prim.GetName()] = entry
    return out


def glb_topology(uvs: list[Any]) -> dict[str, Any]:
    """The GLB is written from the unwrap's seam-split arrays (package.py: _write_glb_level), in world space."""
    return {"meshes": {u.node_id: _topology(u.points, u.triangles) for u in uvs}, "node_transforms": "none (world-space vertices)",
            "note": "per scene node; when nodes exceed max_draw_calls the GLB merges them into one node per material (manifest "
                    "glb_batches), so Blender's objects are the batches and per-object rows cannot be matched to nodes"}


def project_facts(meshes: list[Any], bundle: Any, engine: str, manifest: dict[str, Any] | None) -> dict[str, Any]:
    from antigravity3d.kernel.fast_ops import tier1_qa, uv_overlap_fraction
    from antigravity3d.state import EngineBudget

    t1 = tier1_qa(meshes, EngineBudget(engine))  # type: ignore[arg-type]
    out: dict[str, Any] = {
        "tier1_qa": {"ok": t1.ok, "issues": [{"code": i.code, "severity": i.severity, "node": i.node_id, "detail": i.detail} for i in t1.issues],
                     "stats": {k: t1.stats[k] for k in ("tris", "verts", "draw_calls", "bbox")}},
        "tris": int(sum(len(m.triangles) for m in meshes)),
        "materials": len({m.material_id for m in meshes}),
        "per_node": {m.node_id: {"tris": int(len(m.triangles))} for m in meshes},
    }
    if bundle is not None:
        for u in bundle.uvs:
            out["per_node"].setdefault(u.node_id, {}).update(
                texel_density_px_per_m=_r(u.texel_density, 1), atlas_px=int(u.resolution), uv_area_distortion=_r(u.stretch_mean),
                uv_angle_distortion=_r(u.angle_distortion), uv_overlap_pct=_r(100 * uv_overlap_fraction([u]), 2), chart_source=u.chart_source)
    if manifest is not None:
        out["manifest"] = {k: manifest.get(k) for k in ("tris", "draw_calls", "glb_batches", "texel_density", "units", "colliders")}
        out["manifest"]["validation_ok"] = manifest.get("validation", {}).get("package_ok")
    return out


# ------------------------------------------------------------------------------------------------ subjects

def _package(sm: Any, bundle: Any, engine: str, out: Path, asset: str) -> tuple[Any, dict[str, Any]]:
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.state import EngineBudget

    man = write_engine_package(sm, bundle, EngineBudget(engine), out, asset_name=asset, qa={"tier1_ok": True})  # type: ignore[arg-type]
    return man, man.data


def build_props(scenes: dict[str, tuple[Any, str]], workdir: Path, engines: tuple[str, ...]) -> dict[str, Any]:
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget

    props: dict[str, Any] = {}
    for name, (scene, asset) in scenes.items():
        t = time.perf_counter()
        d = workdir / "props" / name
        d.mkdir(parents=True)
        sm = StageManager.create(d / f"{asset}.usda", asset)
        sm.apply_ir(scene)
        sm.commit_all()
        meshes = sm.extract_meshes()
        bundle = fast_ops.run_export_pipeline(meshes, EngineBudget("unreal_5"), asset)  # same budget numbers for every engine
        per_engine = {}
        for engine in engines:
            man, data = _package(sm, bundle, engine, d / engine, asset)
            per_engine[engine] = {"usd": man.out_dir / f"{asset}.usd", "glb": man.out_dir / f"{asset}.glb", "manifest": data,
                                  "glb_sha256": sha256(man.out_dir / f"{asset}.glb")}
        props[name] = {"asset": asset, "meshes": meshes, "bundle": bundle, "engines": per_engine, "build_s": round(time.perf_counter() - t, 2)}
    return props


def write_stage_usd(path: Path, asset: str, meshes: list[Any]) -> Path:
    """Welded world-space meshes without UVs, written with the stage writer (compile_ir.author_mesh_data), one material."""
    from pxr import Sdf, Usd, UsdGeom, UsdShade

    from antigravity3d.kernel.compile_ir import author_mesh_data

    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, f"/{asset}")
    stage.SetDefaultPrim(root.GetPrim())
    mat = UsdShade.Material.Define(stage, f"/{asset}/Looks/m0")
    shader = UsdShade.Shader.Define(stage, f"/{asset}/Looks/m0/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set((0.8, 0.8, 0.8))
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    for m in meshes:
        prim = UsdGeom.Mesh.Define(stage, f"/{asset}/{m.node_id}").GetPrim()
        author_mesh_data(prim, m.points, m.triangles, None, normals=False)
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat)
    stage.GetRootLayer().Save()
    return path


def build_defects(workdir: Path) -> dict[str, Any]:
    import numpy as np
    import trimesh

    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget

    root = workdir / "defects"
    root.mkdir(parents=True)
    inverted = trimesh.creation.box((0.6, 0.6, 0.6))
    inverted.apply_translation((0.0, 0.0, 0.3))
    trimesh.Trimesh(np.asarray(inverted.vertices), np.asarray(inverted.faces)[:, ::-1], process=False).export(root / "inverted_box.obj")

    def box(nid: str, size: tuple[float, float, float], at: tuple[float, float, float]) -> NodeIR:
        return NodeIR(id=nid, kind="box", params={"size": size}, material_id="m0", transform=Transform(translate=at))
    nodes = {
        "zfight": [box("base", (1.0, 1.0, 0.5), (0.0, 0.0, 0.25)), box("overlap", (0.6, 0.6, 0.5), (0.5, 0.0, 0.25))],
        "back_to_back": [box("left", (1.0, 1.0, 1.0), (0.0, 0.0, 0.5)), box("right", (1.0, 1.0, 1.0), (1.0, 0.0, 0.5))],
        "floating": [box("base", (1.0, 1.0, 1.0), (0.0, 0.0, 0.5)), box("hover", (0.3, 0.3, 0.3), (0.0, 0.0, 1.5))],
        "inverted": [NodeIR(id="inverted", kind="mesh_ref", params={"path": "../inverted_box.obj"}, material_id="m0")],
        "no_uv": [box("plain", (1.0, 1.0, 1.0), (0.0, 0.0, 0.5))],
    }
    budget = EngineBudget("unreal_5")
    out: dict[str, Any] = {}
    for name, ns in nodes.items():
        asset = f"Defect_{name}"
        d = root / name
        d.mkdir()
        sm = StageManager.create(d / f"{asset}.usda", asset)
        sm.apply_ir(SceneIR(asset_name=asset, materials=[MaterialIR(id="m0")], nodes=ns))
        sm.commit_all()
        meshes = sm.extract_meshes()
        files: dict[str, Path] = {}
        bundle = None
        manifest = None
        if name == "no_uv":
            files["stage_usd"] = write_stage_usd(d / f"{asset}_stage.usda", asset, meshes)
        else:
            bundle = fast_ops.run_export_pipeline(meshes, budget, asset)
            man, manifest = _package(sm, bundle, "unreal_5", d / "package", asset)
            files["usd"], files["glb"] = man.out_dir / f"{asset}.usd", man.out_dir / f"{asset}.glb"
        out[name] = {"asset": asset, "meshes": meshes, "bundle": bundle, "manifest": manifest, "files": files}
    return out


async def build_kit(workdir: Path, engine: str) -> dict[str, Any]:
    from antigravity3d.cli import make_collaborators
    from antigravity3d.dags.environment_kit import EnvironmentKitDAG
    from antigravity3d.state import EngineBudget

    _, forge, author, _ = await make_collaborators(True, need_plane=False)
    out = workdir / f"kit_{engine}"
    rep = await EnvironmentKitDAG(forge=forge, author=author, workdir=out, budget=EngineBudget(engine),  # type: ignore[arg-type]
                                  sheet_resolution=KIT_SHEET_PX).run("ruined stone temple", 4)
    mapped = rep.results.get("map_trim") or []
    report = rep.results.get("report") or {}
    project = {}
    for mp in mapped:
        p = mp.piece
        project[p.name] = {
            "template": p.template, "tier1_ok": (report.get("tier1_ok") or {}).get(p.name), "trim_issues": mp.issues,
            "parts": {m.node_id: {"bounds_min": [round(float(v), 4) for v in m.points.min(0)], "bounds_max": [round(float(v), 4) for v in m.points.max(0)],
                                  "dims_m": [round(float(v), 4) for v in m.points.max(0) - m.points.min(0)]} for m in p.meshes},
            "trim_texel_px_per_m": {r.node_id: _r(r.texel_density, 1) for r in mp.results},
            "trim_reports": [{"node": r.node_id, "mapped_area_fraction": _r(r.report.mapped_area_fraction), "max_density_dev": _r(r.report.max_density_dev)}
                             for r in mp.results if r.report]}
    pieces = {name: Path(path) for name, path in (rep.extras.get("pieces") or {}).items()}
    return {"engine": engine, "ok": rep.ok, "gates_ok": rep.extras.get("gates_ok"), "pieces": pieces, "project": project,
            "piece_min_z": rep.extras.get("piece_min_z"), "sheet_px": KIT_SHEET_PX}


# ------------------------------------------------------------------------------------------------ comparisons

def _tier1_codes(project: dict[str, Any]) -> set[str]:
    return {i["code"] for i in project["tier1_qa"]["issues"]}


def agreement(qa: dict[str, Any] | None, project: dict[str, Any]) -> list[dict[str, Any]]:
    """Per check kind: did upstream (as imported) report it, did the project, and do they agree."""
    if not qa:
        return []
    k = kinds_of(qa)
    fired = set(k["errors"]) | set(k["warnings"])
    codes = _tier1_codes(project)
    rows = []
    for kind, code in PROJECT_COUNTERPART.items():
        up = kind in fired
        if code:
            rows.append({"kind": kind, "upstream": up, "project_code": code, "project": code in codes, "agree": up == (code in codes)})
        elif up:
            rows.append({"kind": kind, "upstream": True, "project_code": None, "project": None, "agree": None,
                         "note": "the project has no counterpart check"})
    return rows


def side_by_side(entry: dict[str, Any], project: dict[str, Any]) -> list[dict[str, Any]]:
    """Same quantity measured by both sides: upstream number, project number, difference (upstream - project)."""
    rows: list[dict[str, Any]] = []

    def add(quantity: str, up: Any, pr: Any, note: str = "") -> None:
        if up is None and pr is None:
            return
        diff = _r(up - pr, 4) if isinstance(up, (int, float)) and isinstance(pr, (int, float)) and not isinstance(up, bool) else None
        rows.append({"quantity": quantity, "upstream": up, "project": pr, "difference": diff, **({"note": note} if note else {})})

    calls = entry.get("calls", {})
    ms = (calls.get("mesh_stats") or {}).get("result")
    man = project.get("manifest") or {}
    if ms:
        add("triangles", ms.get("total_tris"), project["tris"])
        add("draw calls", ms.get("draw_calls_est"), man.get("draw_calls"),
            "upstream: one per object (or per material slot); project: GLB nodes, batched by material when nodes exceed max_draw_calls")
        add("materials", ms.get("unique_materials"), project["materials"])
    qa = (calls.get("qa_check") or {}).get("result")
    if qa:
        bbox = project["tier1_qa"]["stats"].get("bbox")
        add("lowest point z (m)", qa.get("ground_z"), bbox[0][2] if bbox else None)
        for obj, m in qa.get("objects", {}).items():
            node = project["per_node"].get(obj)
            if node:
                add(f"{obj}: triangles", m.get("tris"), node.get("tris"))
                if "uv_overlap_pct" in m:
                    add(f"{obj}: UV overlap %", m.get("uv_overlap_pct"), node.get("uv_overlap_pct"),
                        "upstream: 128x128 cell centres per object; project: uv_overlap_fraction, 512 px strict interior")
    td = (calls.get("texel_density") or {}).get("result")
    if td:
        for obj, m in td.get("objects", {}).items():
            node = project["per_node"].get(obj)
            if node and "px_per_m" in m:
                add(f"{obj}: texel density px/m", m.get("px_per_m"), node.get("texel_density_px_per_m"))
    return rows


# ------------------------------------------------------------------------------------------------ the section

class Survey:
    def __init__(self, run: Any, pool: Any, doc: dict[str, Any]):
        self.run, self.pool, self.doc = run, pool, doc
        self.calls = 0

    async def call(self, store: dict[str, Any], key: str, path: Path, primitive: str, kwargs: dict[str, Any], *, weld: bool,
                   tris: int | None, label: str) -> dict[str, Any] | None:
        entry: dict[str, Any] = {"primitive": primitive, "kwargs": kwargs, "weld": weld, "file": path.name, "tris": tris}
        store[key] = entry
        if self.run.over(SURVEY_SHARE):
            entry.update(status="skipped", reason=f"time budget ({self.run.budget_s:.0f}s) used up")
            return None
        self.calls += 1
        t = time.perf_counter()
        try:
            result = await self.pool.call("inspect_usd", path=str(path), primitive=primitive, kwargs=kwargs, weld=weld, _timeout=180.0)
            entry.update(status=outcome(primitive, result), result=result)
            objs = (result.get("agy_inspect") or {}).get("objects") or []
            entry["colliders_imported"] = any(o.startswith("UCX_") for o in objs)
        except Exception as e:  # noqa: BLE001 - recorded with its error, the survey goes on
            entry.update(self.run.error_entry(self.run.record_error(f"upstream_checks.{label}.{key}", e), e))
            result = None
        entry["wall_s"] = round(time.perf_counter() - t, 3)
        return result

    async def topology_pair(self, store: dict[str, Any], path: Path, primitive: str, kwargs: dict[str, Any], tris: int | None, label: str) -> None:
        for weld in (False, True):
            await self.call(store, f"{primitive}{'_welded' if weld else ''}", path, primitive, kwargs, weld=weld, tris=tris, label=label)

    async def file_entry(self, path: Path, tris: int, engine: str, res: int | None, label: str, *, budget: bool = True) -> dict[str, Any]:
        calls: dict[str, Any] = {}
        await self.topology_pair(calls, path, "qa_check", {"profile": QA_PROFILE.get(engine, "game")}, tris, label)
        if res:
            await self.topology_pair(calls, path, "texel_density", {"res": res}, tris, label)
        if budget:
            await self.call(calls, "mesh_stats", path, "mesh_stats", {"budget": MESH_BUDGET[engine][0]}, weld=False, tris=tris, label=label)
        return {"calls": calls}


def summarise(entry: dict[str, Any], project: dict[str, Any]) -> None:
    calls = entry["calls"]
    entry["kinds"] = {key: kinds_of(c.get("result")) for key, c in calls.items() if key.startswith(("qa_check", "kit_check"))}
    entry["outcomes"] = {key: c.get("status") for key, c in calls.items()}
    entry["agreement_as_imported"] = agreement((calls.get("qa_check") or {}).get("result"), project)
    entry["agreement_welded"] = agreement((calls.get("qa_check_welded") or {}).get("result"), project)
    entry["side_by_side"] = side_by_side(entry, project)


async def survey_section(run: Any, pool: Any, scenes: dict[str, tuple[Any, str]], workdir: Path) -> None:
    from antigravity3d.adapters.blender_5_3 import skills

    doc: dict[str, Any] = {"what": __doc__.split("\n\n")[0], "status": "ok"}
    run.doc["upstream_checks"] = doc
    t0 = time.perf_counter()
    doc["upstream"] = {"repository": skills.UPSTREAM_REPOSITORY, "commit": skills.UPSTREAM_COMMIT, "release": skills.UPSTREAM_RELEASE,
                       "scripts": {n: {"path": rel, "sha256": d} for n, (rel, d) in skills.PINNED_SCRIPTS.items()},
                       "helper": {"path": skills.HELPER[0], "sha256": skills.HELPER[1]},
                       "contract": {n: sorted(k) for n, k in skills.PRIMITIVE_KWARGS.items()},
                       "contract_note": "objects is filled in by the worker (the mesh objects the import produced); weld=True merges vertices closer than 1e-6 m per imported mesh"}
    try:
        version = await pool.call("version")
    except Exception as e:  # noqa: BLE001
        doc.update(status="error", error=run.record_error("upstream_checks.version", e))
        return
    have = set(version.get("primitives") or [])
    doc["upstream"]["source"] = version.get("primitives_source")
    doc["upstream"]["load_error"] = version.get("primitives_error")
    if not have:
        doc.update(status="skipped", reason=f"the worker has no primitives ({version.get('primitives_error') or 'none loaded'})")
        return
    if missing := set(skills.PINNED_SCRIPTS) - have:
        doc["missing_primitives"] = sorted(missing)
    doc["mesh_stats_budget_for_engine"] = {e: {"budget": b, "why": why} for e, (b, why) in MESH_BUDGET.items()}
    doc["qa_profile_for_engine"] = {e: QA_PROFILE.get(e, "game") for e in SURVEY_ENGINES}
    doc["file_readers"] = FILE_READERS
    survey = Survey(run, pool, doc)
    engines = QUICK_ENGINES if run.quick else SURVEY_ENGINES
    doc["engines_run"], doc["engines_not_run"] = [], {e: "--quick" for e in SURVEY_ENGINES if e not in engines}

    t = time.perf_counter()
    props = build_props(scenes, workdir / "survey", engines)
    defects = build_defects(workdir / "survey")
    doc["build_s"] = {"props_and_defects": round(time.perf_counter() - t, 2)}

    out_props: dict[str, Any] = {}
    doc["props"] = out_props

    async def props_for(engine: str) -> None:
        if run.over(SURVEY_SHARE):
            doc["engines_not_run"][engine] = f"time budget ({run.budget_s:.0f}s) used up"
            return
        doc["engines_run"].append(engine)
        for name, sub in props.items():
            pe = sub["engines"][engine]
            res = max(int(u.resolution) for u in sub["bundle"].uvs)
            project = project_facts(sub["meshes"], sub["bundle"], engine, pe["manifest"])
            subject = out_props.setdefault(name, {"asset": sub["asset"], "build_s": sub["build_s"], "files": {}})
            usd = await survey.file_entry(pe["usd"], project["tris"], engine, res, f"props.{name}.{engine}.usd")
            usd.update(file="package USD", engine=engine, texture_px=res, topology=usd_topology(pe["usd"]), project=project)
            summarise(usd, project)
            subject["files"][f"usd:{engine}"] = usd
            # one GLB call set per distinct file (the GLB does not depend on the up axis)
            seen = next((k for k, v in subject["files"].items() if k.startswith("glb:") and v.get("sha256") == pe["glb_sha256"]), None)
            if seen:
                subject["files"][seen].setdefault("same_file_for", []).append(engine)
                continue
            glb = await survey.file_entry(pe["glb"], project["tris"], engine, res, f"props.{name}.{engine}.glb")
            glb.update(file="package GLB", engine=engine, sha256=pe["glb_sha256"], texture_px=res, topology=glb_topology(sub["bundle"].uvs),
                       project=project)
            summarise(glb, project)
            subject["files"][f"glb:{engine}"] = glb

    await props_for(engines[0])

    out_defects: dict[str, Any] = {}
    doc["defects"] = out_defects
    for name, d in defects.items():
        spec = DEFECTS[name]
        project = project_facts(d["meshes"], d["bundle"], "unreal_5", d["manifest"])
        entry: dict[str, Any] = {"what": spec["what"], "expected": {"upstream_should": spec["should"], "upstream_should_not": spec["should_not"],
                                                                     "project_should": spec["project"] or None},
                                 "project": project, "files": {}}
        for label, path in d["files"].items():
            calls: dict[str, Any] = {}
            await survey.topology_pair(calls, path, "qa_check", {"profile": "game"}, project["tris"], f"defects.{name}.{label}")
            fe = {"calls": calls}
            summarise(fe, project)
            fe["verdict"] = {key: defect_verdict(spec, (calls.get(key) or {}).get("result")) for key in ("qa_check", "qa_check_welded")}
            if label != "glb" and label != "stage_usd":
                fe["topology"] = usd_topology(path)
            entry["files"][label] = fe
        code = spec["project"]
        entry["project_verdict"] = None if not code else ("found" if code in _tier1_codes(project) else "missed")
        out_defects[name] = entry

    doc["kit"] = {"convention": KIT_CONVENTION, "file_choice":
                  "pieces/<piece>.usda, one piece per import: its meshes are written with points in piece space and no xformOps "
                  "(tactics/package.py write_package_usd), so each Blender object's local coordinates are the piece's own. kit.usda "
                  "was not used: it places each piece by a translate on a referencing Xform, which would only add a parent offset. "
                  "The Y-up kit stores native Y-up points with no root rotation, so Blender's own up-axis conversion of the import is "
                  "what kit_check sees there.", "kits": {}}
    for engine in KIT_ENGINES:
        if run.over(SURVEY_SHARE):
            doc["kit"]["kits"][engine] = {"status": "skipped", "reason": f"time budget ({run.budget_s:.0f}s) used up"}
            continue
        try:
            t = time.perf_counter()
            kit = await build_kit(workdir / "survey", engine)
            build_s = round(time.perf_counter() - t, 2)
        except Exception as e:  # noqa: BLE001
            doc["kit"]["kits"][engine] = {"status": "error", "error": run.record_error(f"upstream_checks.kit.{engine}.build", e)}
            continue
        kentry: dict[str, Any] = {"status": "ok", "build_s": build_s, "gates_ok": kit["gates_ok"], "piece_min_z": kit["piece_min_z"], "pieces": {}}
        for piece, path in kit["pieces"].items():
            proj = kit["project"].get(piece, {})
            topo = usd_topology(path)
            tris = sum(m["tris"] for m in topo["meshes"].values() if m["purpose"] != "guide")
            calls: dict[str, Any] = {}
            for conv, kw in KIT_CONVENTION["runs"].items():
                kwargs = {**kw, "texture": kit["sheet_px"]}
                for weld in (False, True):
                    await survey.call(calls, f"kit_check_{conv}{'_welded' if weld else ''}", path, "kit_check", kwargs, weld=weld, tris=tris,
                                      label=f"kit.{engine}.{piece}")
            pe: dict[str, Any] = {"file": path.name, "calls": calls, "project": proj, "topology": topo}
            pe["kinds"] = {key: kinds_of(c.get("result")) for key, c in calls.items()}
            pe["outcomes"] = {key: c.get("status") for key, c in calls.items()}
            pe["pass"] = {key: (c.get("result") or {}).get("pass") for key, c in calls.items()}
            pe["side_by_side"] = kit_side_by_side(calls.get("kit_check_upstream_defaults"), proj)
            kentry["pieces"][piece] = pe
        doc["kit"]["kits"][engine] = kentry

    for engine in engines[1:]:
        await props_for(engine)
    doc["cost"] = {"calls": survey.calls, "wall_s": round(time.perf_counter() - t0, 2),
                   "per_call": [{"where": w, "primitive": c["primitive"], "weld": c["weld"], "tris": c.get("tris"), "wall_s": c.get("wall_s"),
                                 "status": c.get("status")} for w, c in iter_calls(doc)]}


def defect_verdict(spec: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    k = kinds_of(result)
    fired = set(k["errors"]) | set(k["warnings"])
    errors = set(k["errors"])
    found = {s: s in fired for s in spec["should"]}
    wrong = {s: (s.split(":", 1)[1] in errors) if s.startswith("error:") else (s in fired) for s in spec["should_not"]}
    return {"found": found, "wrongly_reported": wrong, "as_expected": all(found.values()) and not any(wrong.values()), "pass": result.get("pass")}


def kit_side_by_side(call: dict[str, Any] | None, project: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    result = (call or {}).get("result") or {}
    for obj, m in (result.get("pieces") or {}).items():
        part = (project.get("parts") or {}).get(obj)
        if part:
            rows.append({"quantity": f"{obj}: dims m", "upstream": m.get("dims_m"), "project": part["dims_m"],
                         "note": "project dims are world Z-up; upstream reads the object's local vertices"})
        tex = (project.get("trim_texel_px_per_m") or {}).get(obj)
        if tex is not None or m.get("texel_px_per_m") is not None:
            up = m.get("texel_px_per_m")
            rows.append({"quantity": f"{obj}: texel density px/m", "upstream": up, "project": tex,
                         "difference": round(up - tex, 1) if isinstance(up, (int, float)) and isinstance(tex, (int, float)) else None})
    return rows


def iter_calls(doc: dict[str, Any]):
    for name, sub in (doc.get("props") or {}).items():
        for f, fe in sub["files"].items():
            for key, c in fe["calls"].items():
                yield f"props.{name}.{f}.{key}", c
    for name, d in (doc.get("defects") or {}).items():
        for f, fe in d["files"].items():
            for key, c in fe["calls"].items():
                yield f"defects.{name}.{f}.{key}", c
    for engine, k in ((doc.get("kit") or {}).get("kits") or {}).items():
        for piece, pe in (k.get("pieces") or {}).items():
            for key, c in pe["calls"].items():
                yield f"kit.{engine}.{piece}.{key}", c


# ------------------------------------------------------------------------------------------------ markdown

def _fmt_kinds(k: dict[str, int] | None) -> str:
    return ", ".join(f"{kind} {n}" for kind, n in (k or {}).items()) or "none"


def markdown(doc: dict[str, Any]) -> list[str]:
    s = doc.get("upstream_checks")
    if not s:
        return []
    lines = ["", "### Upstream checks survey (EL4CTEO/blender-skills, measurement only)", ""]
    up = s.get("upstream", {})
    src = up.get("source") or {}
    lines.append(f"Commit `{str(up.get('commit'))[:12]}` ({up.get('release')}), scripts from `{src.get('kind')}`. Nothing here gates anything.")
    if s.get("status") != "ok":
        return lines + ["", f"Survey {s.get('status')}: {s.get('reason') or s.get('error')}"]
    if src.get("kind") == "fake":
        lines.append("FAKE worker: placeholder results (no scene), so every verdict below is meaningless; this shows the plumbing only.")
    if s.get("engines_not_run"):
        lines.append(f"Engines run: {', '.join(s.get('engines_run', []))}; not run: " + "; ".join(f"{e} ({why})" for e, why in s["engines_not_run"].items()) + ".")
    lines += ["", "Errors / warnings by kind, as imported and after the worker merged coincident vertices. "
              "Disagreements: upstream kind vs the project's tier1_qa code (as imported).", "",
              "| subject | file | as imported: errors | warnings | merged: errors | warnings | disagrees with project |", "|---|---|---|---|---|---|---|"]

    def row(subject: str, label: str, fe: dict[str, Any], a: str = "qa_check", b: str = "qa_check_welded") -> None:
        k0, k1 = fe.get("kinds", {}).get(a), fe.get("kinds", {}).get(b)
        st0, st1 = fe["calls"].get(a, {}).get("status"), fe["calls"].get(b, {}).get("status")
        c0 = f"{_fmt_kinds((k0 or {}).get('errors'))}" if st0 in ("ok", "checks_skipped") else str(st0)
        c1 = f"{_fmt_kinds((k1 or {}).get('errors'))}" if st1 in ("ok", "checks_skipped") else str(st1)
        dis = [f"{r['kind']} (upstream {'yes' if r['upstream'] else 'no'}, {r['project_code']} {'yes' if r['project'] else 'no'})"
               for r in fe.get("agreement_as_imported", []) if r.get("agree") is False]
        lines.append(f"| {subject} | {label} | {c0} | {_fmt_kinds((k0 or {}).get('warnings'))} | {c1} | {_fmt_kinds((k1 or {}).get('warnings'))} | {'; '.join(dis) or 'none'} |")

    for name, sub in s.get("props", {}).items():
        for f, fe in sub["files"].items():
            row(name, f + (f" (= {', '.join(fe['same_file_for'])})" if fe.get("same_file_for") else ""), fe)
    lines += ["", "Planted defects (expected vs actual):", "", "| defect | file | upstream as imported | upstream merged | project |", "|---|---|---|---|---|"]
    for name, d in s.get("defects", {}).items():
        for f, fe in d["files"].items():
            v0, v1 = fe["verdict"].get("qa_check"), fe["verdict"].get("qa_check_welded")

            def says(v: Any) -> str:
                if not v:
                    return "n/a"
                return ("as expected" if v["as_expected"] else "NOT as expected") + f" (found {v['found']}, wrongly {v['wrongly_reported']})"
            lines.append(f"| {name} | {f} | {says(v0)} | {says(v1)} | {d.get('project_verdict') or 'no counterpart'} |")
    kits = (s.get("kit") or {}).get("kits", {})
    if kits:
        lines += ["", "Kit pieces (kit_check pass, as imported / merged). The project states no grid or pivot convention; "
                  "`defaults` = grid 0.5 m, corner pivot; `template origin` = grid 0.5 m, bottom-center.", "",
                  "| kit | piece | defaults | template origin | errors by kind (defaults, as imported) | errors (defaults, merged) |", "|---|---|---|---|---|---|"]
        for engine, k in kits.items():
            if k.get("status") != "ok":
                lines.append(f"| {engine} | - | {k.get('status')} | | {k.get('reason') or k.get('error')} | |")
                continue
            for piece, pe in k["pieces"].items():
                p = pe["pass"]
                lines.append(f"| {engine} | {piece} | {p.get('kit_check_upstream_defaults')} / {p.get('kit_check_upstream_defaults_welded')} | "
                             f"{p.get('kit_check_template_origin')} / {p.get('kit_check_template_origin_welded')} | "
                             f"{_fmt_kinds(pe['kinds'].get('kit_check_upstream_defaults', {}).get('errors'))} | "
                             f"{_fmt_kinds(pe['kinds'].get('kit_check_upstream_defaults_welded', {}).get('errors'))} |")
    cost = s.get("cost", {})
    statuses = Counter(c.get("status") for c in cost.get("per_call", []))
    lines += ["", f"{cost.get('calls')} calls in {cost.get('wall_s')} s; outcomes: {dict(statuses)}. Full results, per-object metrics and the "
              "side-by-side numbers are in the JSON under `upstream_checks`."]
    return lines


if __name__ == "__main__":  # pragma: no cover - this module is a section of perf_engines.py
    raise SystemExit("run benchmarks/perf_engines.py")
