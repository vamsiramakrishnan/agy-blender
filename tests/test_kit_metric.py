"""One metric per modular kit: the pieces built to it snap, and pieces that do not are caught with their numbers."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.dags.environment_kit import _convex_groups
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.fast_ops import generate_colliders
from antigravity3d.kernel.kit_metric import (
    KIT_TEMPLATES, KitMetric, _Convex, check_piece, context_brief, kit_report, penetration, snap_test,
)
from antigravity3d.kernel.trim_atlas import TrimSheet, map_to_trim
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.tactics.authoring import KIT_TEMPLATE_FNS

STRIP = {"concrete": "concrete", "iron": "painted_metal"}
METRICS = {"interior": KitMetric.interior(), "exterior": KitMetric.exterior(), "bottom_center": KitMetric.parse("interior,pivot=bottom-center"),
           "four_metre_interior": KitMetric.parse("interior,module=4,wall_height=3.5")}


def meshes_of(scene: SceneIR, tmp: Path):
    sm = StageManager.create(tmp / f"{scene.asset_name}.usda", scene.asset_name)
    sm.apply_ir(scene)
    return sm.extract_meshes()


def piece(tpl: str, metric: KitMetric, tmp: Path, seed: int = 0, name: str | None = None):
    scene = KIT_TEMPLATE_FNS[tpl](random.Random(seed), name or f"{tpl}{seed}", "", metric)
    return scene, meshes_of(scene, tmp)


def box(i: str, lo, hi, mat: str = "concrete") -> NodeIR:
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    return NodeIR(id=i, kind="box", params={"size": tuple(float(v) for v in hi - lo)}, transform=Transform(translate=tuple(float(v) for v in (lo + hi) / 2)),
                  material_id=mat)


MATS = [MaterialIR(id="concrete"), MaterialIR(id="iron")]


def test_presets_parse_and_reject_metrics_that_cannot_snap() -> None:
    assert KitMetric.parse(None) == KitMetric.interior() and KitMetric.parse("exterior").module_m == 4.0
    m = KitMetric.parse("interior,module=4,wall_height=3.5,pivot=center")
    assert (m.module_m, m.wall_height_m, m.pivot, m.grid_m) == (4.0, 3.5, "center", 0.5)
    assert (KitMetric.interior().module_m - KitMetric.interior().door_width_m) / 2 == 0.5  # a 1 m door leaves 0.5 m jambs, on the grid
    for spec, words in (("interior,pillar=0.7", "pillar_m 0.7 is not a multiple"), ("interior,wall_height=2.9", "not on the 0.5 m grid"),
                        ("interior,door_width=0.9", "below 1 x 2.2"), ("interior,module=4,door_width=1.2", "door jambs of 1.4 m are off the 0.5 m grid"),
                        ("interior,pivot=middle", "pivot must be"), ("interior,relief=0.3", "relief_m"), ("castle", "unknown kit metric preset"),
                        ("interior,colour=red", "unknown kit metric key")):
        with pytest.raises(ValueError, match=words):
            KitMetric.parse(spec)


def test_the_face_limit_follows_the_sheet() -> None:
    m = KitMetric.interior()
    assert m.fitted_to([0.84, 0.97]).max_face_m == 0.8 and m.fitted_to([0.75]).max_face_m == 0.75


@pytest.mark.parametrize("which", sorted(METRICS))
@pytest.mark.parametrize("tpl", KIT_TEMPLATES)
def test_every_template_piece_obeys_its_metric_and_keeps_full_texel_density(tmp_path: Path, which: str, tpl: str) -> None:
    metric = METRICS[which].fitted_to([0.84])
    sheet = TrimSheet.standard(1024)
    for seed in range(3):
        scene, ms = piece(tpl, metric, tmp_path, seed)
        mat = {n.id: n.material_id for n in scene.nodes}
        trims = [map_to_trim(m, sheet, STRIP[mat[m.node_id]]) for m in ms]
        c = check_piece(scene.asset_name, tpl, ms, metric, trims)
        assert c.ok, c.errors
        want = metric.dims(tpl)
        assert c.dims_m[0] == pytest.approx(want[0]) and c.dims_m[2] == pytest.approx(want[2])
        assert c.texel["max_chart_dev"] < 0.01  # no chart shrunk to fit its strip


def test_the_old_templates_fail_with_their_numbers(tmp_path: Path) -> None:
    """The templates before the metric: a random height, trims proud of -Y, a 0.7 m pillar."""
    metric = KitMetric.interior()
    h = 2.8896
    wall = SceneIR(asset_name="OldWall", materials=MATS, nodes=[
        box("slab", (-1, -0.1, 0), (1, 0.1, h)), box("base_trim", (-1, -0.14, 0), (1, -0.1, 0.2), "iron"),
        box("cornice", (-1, -0.14, h - 0.15), (1, -0.1, h), "iron")])
    c = check_piece("OldWall", "wall_segment", meshes_of(wall, tmp_path), metric)
    text = "; ".join(c.errors)
    assert "height z 2.8896 m, kit 3 m" in text and "pivot is not at the corner: bounding-box min at (-1, -0.14, 0)" in text
    assert "face y- face covers" in text  # the trims stand proud of the mating face
    pillar = SceneIR(asset_name="OldPillar", materials=MATS, nodes=[
        NodeIR(id="plinth", kind="cylinder", params={"radius": 0.35, "height": 0.2, "segments": 16}, transform=Transform(translate=(0, 0, 0.1)), material_id="concrete")])
    c = check_piece("OldPillar", "pillar", meshes_of(pillar, tmp_path), metric)
    assert "footprint 0.7 x 0.7 m is not a multiple of the 0.5 m grid" in c.errors[0]


def test_relief_is_allowed_on_plus_y_and_refused_on_the_mating_faces(tmp_path: Path) -> None:
    metric = KitMetric.interior()
    core = [box("core", (0, 0, 0), (2, 0.25, 3))]
    front = SceneIR(asset_name="Front", materials=MATS, nodes=[*core, box("moulding", (0, 0.25, 1.0), (2, 0.29, 1.2), "iron")])
    assert check_piece("Front", "wall_segment", meshes_of(front, tmp_path), metric).ok
    deep = SceneIR(asset_name="Deep", materials=MATS, nodes=[*core, box("moulding", (0, 0.25, 1.0), (2, 0.35, 1.2), "iron")])
    assert any("plus at most 0.04 m of relief" in e for e in check_piece("Deep", "wall_segment", meshes_of(deep, tmp_path), metric).errors)
    back = SceneIR(asset_name="Back", materials=MATS, nodes=[box("core", (0, 0.04, 0), (2, 0.29, 3)), box("moulding", (0, 0, 1.0), (2, 0.04, 1.2), "iron")])
    errs = check_piece("Back", "wall_segment", meshes_of(back, tmp_path), metric).errors
    assert any(e.startswith("face y- face covers 6.7%") for e in errs)
    short_end = SceneIR(asset_name="Bevel", materials=MATS, nodes=[box("core", (0, 0, 0), (2, 0.25, 2.8)), box("cap", (0.1, 0, 2.8), (1.9, 0.25, 3))])
    errs = check_piece("Bevel", "wall_segment", meshes_of(short_end, tmp_path), metric).errors
    assert any(e.startswith("end x- face covers 93.3%") for e in errs)


def test_a_part_in_the_door_opening_is_named(tmp_path: Path) -> None:
    metric = KitMetric.interior().fitted_to([0.84])
    scene, _ = piece("doorway", metric, tmp_path)
    blocked = scene.model_copy(update={"asset_name": "Blocked", "nodes": [*scene.nodes, box("bar", (0.5, 0, 1.0), (1.5, 0.25, 1.1), "iron")]})
    errs = check_piece("Blocked", "doorway", meshes_of(blocked, tmp_path), metric).errors
    assert any("part bar reaches" in e and "1 x 2.5 m opening" in e for e in errs)


def kit_pieces(metric: KitMetric, tmp: Path, n: int = 8) -> dict:
    out = {}
    for k in range(n):
        tpl = ("wall_segment", "floor_tile", "pillar", "doorway")[k % 4]
        scene, ms = piece(tpl, metric, tmp, k, f"P{k:02d}")
        out[scene.asset_name] = (tpl, ms)
    return out


@pytest.mark.parametrize("which", sorted(METRICS))
def test_the_kit_snaps_in_every_layout_its_piece_types_allow(tmp_path: Path, which: str) -> None:
    metric = METRICS[which].fitted_to([0.84])
    res = snap_test(metric, kit_pieces(metric, tmp_path))
    assert res["pass"], {k: v["problems"] for k, v in res["layouts"].items()}
    assert set(res["layouts"]) == {"run", "corner", "room", "storey", "t_junction"}
    for lay in res["layouts"].values():
        assert lay["max_gap_m"] <= 1e-6 and lay["max_interpenetration_m"] <= 1e-6 and lay["min_face_covered"] >= 0.999
        if metric.pivot == "corner":
            assert lay["pivots_off_grid"] == []  # every pivot on a grid point: the pieces snap by increments
    run = res["layouts"]["run"]
    assert {(j["a"].split("#")[0][:3], j["axis"]) for j in run["joints"]} >= {("P00", "x"), ("P01", "z")}  # wall-wall and floor-under-wall
    assert any("corner piece" in m for m in res["not_expressible"])


def test_the_snap_test_measures_a_gap_a_slit_and_an_overlap(tmp_path: Path) -> None:
    metric = KitMetric.interior().fitted_to([0.84])
    good = kit_pieces(metric, tmp_path, 4)
    short = SceneIR(asset_name="Short", materials=MATS, nodes=[box("core", (0, 0, 0), (1.995, 0.25, 3))])
    res = snap_test(metric, {**good, "P00": ("wall_segment", meshes_of(short, tmp_path))})
    assert not res["pass"] and any("gap 5.0 mm along x" in p for p in res["layouts"]["run"]["problems"])
    long_ = SceneIR(asset_name="Long", materials=MATS, nodes=[box("core", (0, 0, 0), (2.01, 0.25, 3))])
    res = snap_test(metric, {**good, "P00": ("wall_segment", meshes_of(long_, tmp_path))})
    assert any("interpenetration 10.00 mm" in p for p in res["layouts"]["run"]["problems"])
    proud_back = SceneIR(asset_name="ProudBack", materials=MATS, nodes=[box("core", (0, 0.04, 0), (2, 0.25, 3)), box("trim", (0, 0, 0), (2, 0.04, 0.2), "iron")])
    res = snap_test(metric, {**good, "P00": ("wall_segment", meshes_of(proud_back, tmp_path))})
    assert not res["layouts"]["corner"]["pass"]  # the butting wall meets a recess: a see-through slit at the corner


def test_the_kit_report_gates_texel_density_against_the_median(tmp_path: Path) -> None:
    metric = KitMetric.interior().fitted_to([0.84])
    sheet = TrimSheet.standard(1024)
    checks = []
    for name, (tpl, ms) in kit_pieces(metric, tmp_path, 4).items():
        checks.append(check_piece(name, tpl, ms, metric, [map_to_trim(m, sheet, "concrete") for m in ms]))
    rep = kit_report(checks, metric)
    assert rep["pass"] and rep["texel_spread_from_median"] < 0.01
    slab = SceneIR(asset_name="Slab", materials=MATS, nodes=[box("slab", (0, 0, 0), (2, 0.25, 3))])  # one 2 x 3 m face: shrunk to fit
    ms = meshes_of(slab, tmp_path)
    c = check_piece("Slab", "wall_segment", ms, metric, [map_to_trim(m, sheet, "concrete") for m in ms])
    assert any("shrunk to" in e for e in c.errors)
    rep = kit_report([*checks, c], metric)
    assert not rep["pass"] and any("vs kit median" in e for e in rep["pieces"]["Slab"]["errors"])


@pytest.mark.parametrize("which", ["interior", "exterior"])
def test_a_door_sized_box_passes_through_the_doorway_colliders(tmp_path: Path, which: str) -> None:
    metric = METRICS[which].fitted_to([0.84])
    for seed in range(3):
        scene, ms = piece("doorway", metric, tmp_path, seed)
        hulls = generate_colliders(_convex_groups(ms), 4, asset_name=scene.asset_name)
        assert len(hulls) <= 4
        olo, ohi = metric.opening()
        tol = 1e-3
        # the swept volume of a door-sized box pushed through the wall along Y, from well in front to well behind
        sweep = _Convex.of(np.array([[x, y, z] for x in (olo[0] + tol, ohi[0] - tol) for y in (-1.0, metric.wall_thickness_m + 1.0)
                                     for z in (olo[2] + tol, ohi[2] - tol)]))
        assert all(penetration(_Convex.of(h.points), sweep) <= 1e-6 for h in hulls), [h.name for h in hulls]
        # and the colliders do close the wall either side of the opening
        assert sum(float(np.ptp(h.points[:, 0])) for h in hulls) >= metric.module_m - metric.door_width_m - 1e-6


def test_the_brief_is_only_for_kit_pieces_and_carries_feedback() -> None:
    m = KitMetric.interior()
    assert context_brief({"template": "crate"}) == "" and context_brief({"kit_metric": m}) == ""
    b = context_brief({"kit_metric": m, "template": "wall_segment", "kit_feedback": "height z 2.9 m, kit 3 m"})
    assert b.startswith("Kit metric, exact, metres: box x 0..2, y 0..0.25, z 0..3; origin at its min corner.")
    assert b.endswith("Previous answer rejected: height z 2.9 m, kit 3 m")
    assert all(len(m.brief(t).encode()) < 330 for t in KIT_TEMPLATES)  # sent with every call: bounded


# ------------------------------------------------------------------ what a piece ships


async def _offline_kit(tmp: Path, engine: str = "unreal_5", keep: bool = False, n: int = 4):
    from antigravity3d.dags.environment_kit import EnvironmentKitDAG
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    dag = EnvironmentKitDAG(forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=tmp,
                            budget=EngineBudget(engine, keep_contact_faces=keep), sheet_resolution=1024)  # type: ignore[arg-type]
    rep = await dag.run("ruined stone temple", n)
    assert rep.ok and rep.extras["gates_ok"], rep.summary()
    return rep


def _shipped_mesh(path: str):
    """(points, triangles) of the one render mesh a piece file ships, collision hulls left out."""
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(path)  # held: prims expire with their stage
    meshes = [UsdGeom.Mesh(p) for p in stage.Traverse() if p.IsA(UsdGeom.Mesh) and not p.GetName().startswith("UCX_")]
    assert len(meshes) == 1, [m.GetPrim().GetName() for m in meshes]
    return np.asarray(meshes[0].GetPointsAttr().Get(), np.float64), np.asarray(meshes[0].GetFaceVertexIndicesAttr().Get()).reshape(-1, 3)


def _open_edges(points: np.ndarray, tris: np.ndarray) -> int:
    """Edges used by exactly one triangle once coincident vertices are merged."""
    _, welded = np.unique(np.round(points / 1e-6).astype(np.int64), axis=0, return_inverse=True)
    t = welded.reshape(-1)[tris]
    edges = np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return int((counts == 1).sum())


async def test_a_piece_ships_without_the_faces_its_own_parts_press_together_and_stays_closed(tmp_path: Path) -> None:
    """Courses stacked full-depth have contact faces nothing can see. They do not ship (a quarter to a third of a
    piece's triangles), and what ships is still one closed surface once coincident vertices are merged: a face is
    left out only where the two sides of the contact both go, so no part's rim is left open inside the piece.
    Roblox requires closed render meshes part by part, and a run can ask for the closed parts: both keep every face."""
    shipped = await _offline_kit(tmp_path / "a")
    closed = await _offline_kit(tmp_path / "b", keep=True)
    roblox = await _offline_kit(tmp_path / "c", engine="roblox_r15")
    removed = shipped.nodes["export_pieces"].data["contact_faces_removed"]
    assert set(removed) == set(shipped.extras["pieces"]) and all(v > 0 for v in removed.values()), removed
    assert not any(closed.nodes["export_pieces"].data["contact_faces_removed"].values())
    assert not any(roblox.nodes["export_pieces"].data["contact_faces_removed"].values())
    for name, path in shipped.extras["pieces"].items():
        pts, tris = _shipped_mesh(path)
        full = len(_shipped_mesh(closed.extras["pieces"][name])[1])
        assert len(tris) == full - removed[name] and len(tris) <= 0.8 * full, (name, len(tris), full)
        assert _open_edges(pts, tris) == 0, name
        assert len(_shipped_mesh(roblox.extras["pieces"][name])[1]) == full


def test_a_narrow_part_on_a_wide_one_keeps_its_cap() -> None:
    """A shaft on a wider plinth: the shaft's bottom is hidden by the plinth, the plinth's top is not hidden by the
    shaft. Dropping the shaft's bottom alone would leave its rim open in the middle of the piece, so it stays; two
    boxes of one footprint lose both faces of their contact."""
    from antigravity3d.dags.environment_kit import ship_masks
    from antigravity3d.kernel.types import MeshData
    from antigravity3d.state import EngineBudget

    def cuboid(i: str, lo, hi) -> MeshData:
        (x0, y0, z0), (x1, y1, z1) = lo, hi
        p = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
        t = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
        return MeshData(i, f"/{i}", p, t, "kit_trim")

    budget = EngineBudget("unreal_5")
    same = ship_masks([cuboid("a", (0, 0, 0), (1, 1, 1)), cuboid("b", (0, 0, 1), (1, 1, 2))], budget)
    assert [int((~k).sum()) for k in same] == [2, 2]  # the top of a and the bottom of b
    narrow = ship_masks([cuboid("plinth", (0, 0, 0), (2, 2, 1)), cuboid("shaft", (0.5, 0.5, 1), (1.5, 1.5, 3))], budget)
    assert [int((~k).sum()) for k in narrow] == [0, 0]
