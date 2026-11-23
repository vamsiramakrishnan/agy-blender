"""Defects an independent review of round seven's surface work found by running it (2026-11-23), one test each, every
one shown to fail on the reviewed code before its fix: hard edges along a round part smoothed away, a wooden disc
painted as end grain, ``grain=`` moving the axis roundness is judged about, wood re-rolled by an unrelated part, an
unstable fallback frame, the run leaking into text a model reads, round edges dropped below an art direction's crease,
runs going stale or lost on edits, the thickness cap wrong, USD and GLB normals disagreeing under non-uniform scale."""

from __future__ import annotations

import json

import numpy as np
import pytest

from antigravity3d import ir
from antigravity3d.build import shapes
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, SetParam, Transform, UpsertNode
from antigravity3d.kernel import compile_ir
from antigravity3d.kernel import mesh_masks as mm
from antigravity3d.kernel import smart_materials as sm
from antigravity3d.kernel import surface_frame as sf
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import _canon, run_export_pipeline
from antigravity3d.kernel.types import Run
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

LUM = np.array([0.2126, 0.7152, 0.0722])


def _ang(a, b):
    return np.degrees(np.arccos(np.clip((a * b).sum(-1), -1.0, 1.0)))


def _spread(points, tris, normals, at, faces=None):
    """Largest angle between the corner normals that meet at each welded vertex in ``at`` (0: smooth there), over the
    corners of ``faces`` (a triangle mask; all by default)."""
    wid = _canon(np.asarray(points, np.float64))[np.asarray(tris)].ravel()
    use = np.ones(len(wid), bool) if faces is None else np.repeat(np.asarray(faces, bool), 3)
    n = normals.reshape(-1, 3)
    out = []
    for w in np.unique(_canon(np.asarray(points, np.float64))[at]):
        g = n[(wid == w) & use]
        if len(g):
            out.append(_ang(g[:, None], g[None]).max())
    assert out, "no vertex measured"
    return np.array(out)


def _side_faces(points, tris, axis):
    p = np.asarray(points, np.float64)
    fn = np.cross(p[tris[:, 1]] - p[tris[:, 0]], p[tris[:, 2]] - p[tris[:, 0]])
    fn /= np.linalg.norm(fn, axis=1, keepdims=True)
    return np.abs(fn @ np.asarray(axis, np.float64)) < 0.5


def _design(tmp_path, monkeypatch, build, surface="oak wood"):
    from agy3d import Asset

    from antigravity3d.build import handoff

    monkeypatch.setenv("AGY3D_BUILD_OUT", str(tmp_path / "out"))
    a = Asset("T", "godot_4")
    m = a.material("mat", "#6e4224", surface=surface)
    build(a, m)
    scene = handoff.read(a.save()).scene()
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    return stage.extract_meshes(), scene, stage


def _paint(meshes, scene, px=1024):
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4", shared_atlas=False), "T", opaque_materials=opaque_materials(scene))
    lay = mm.layouts_from_uvs(bundle.uvs, px)[0]
    m = scene.materials[0]
    masks = mm.compute_masks(lay, meshes)
    layer, c = sm.paint_material(sm.classify(m), m, masks, {x.node_id: x for x in meshes})
    return layer, c, masks


def _run_of(node):
    r = compile_ir.node_run(node)
    return Run(np.asarray(r.path, np.float64), np.asarray(r.radius or [0.0] * len(r.path), np.float64), r.round, r.closed)


# ----------------------------------------------------------------------------------------- 1, 7: one rule, only along


BEAD = [(0.0, 0.30), (0.05, 0.30), (0.05, 0.20), (0.05 + 0.02 * np.tan(np.radians(29)), 0.18), (0.05, 0.16), (0.05, 0.0), (0.0, 0.0)]


def test_a_ridge_round_a_turned_part_and_a_kink_in_a_rod_stay_hard():
    """Smoothing a round part's facets is about the lines along its run only: a turned bead (58 degrees) and a 60 degree
    kink in a 12-sided rod are real edges, in the shipped normals and in the wear masks alike."""
    node = NodeIR(id="leg", kind="lathe", params={"profile": [list(map(float, p)) for p in BEAD], "segments": 24})
    p, t = compile_ir.primitive_mesh(node)
    p, t = np.asarray(p, np.float64), np.asarray(t)
    run = _run_of(node)
    assert run.round
    ridge = np.flatnonzero((np.abs(p[:, 2] - 0.18) < 1e-6) & (np.hypot(p[:, 0], p[:, 1]) > 0.055))
    assert _spread(p, t, compile_ir.crease_normals(p, t, run=run), ridge).min() > 50.0
    e = mm.mesh_edges(p, t, run=run)
    on_ridge = np.abs(e.segments[:, :, 2] - 0.18).max(axis=1) < 1e-6
    assert on_ridge.sum() >= 24 and np.degrees(np.abs(e.turn[on_ridge])).min() > 50.0
    k = shapes.tube("k", [(0, 0, 0), (0, 0, 0.2), (0, 0.2 * np.sin(np.radians(60)), 0.2 + 0.2 * np.cos(np.radians(60)))],
                    0.01, 12, False, False)
    rk = Run(k.centre, np.full(len(k.centre), 0.01), True)
    kink = np.flatnonzero(np.abs(k.points[:, 2] - 0.2) < 0.012)
    assert _spread(k.points, k.tris, compile_ir.crease_normals(k.points, k.tris, run=rk), kink).max() > 55.0
    # and the facet lines along it are smooth (the first leg's sides, at its start)
    start = np.flatnonzero(k.points[:, 2] < 1e-6)
    assert _spread(k.points, k.tris, compile_ir.crease_normals(k.points, k.tris, run=rk), start,
                   _side_faces(k.points, k.tris, (0, 0, 1))).max() < 1.0


def test_below_an_art_directions_crease_the_drawn_edges_and_masks_keep_the_facets():
    from antigravity3d.kernel.raster import _feature_edge_list
    from antigravity3d.kernel.types import MeshData

    t = shapes.tube("s", [(0, 0, 0), (0, 0, 0.4)], 0.01, 8, False, False)
    run = Run(t.centre, np.full(len(t.centre), 0.01), True)
    m = MeshData("s", "", t.points, t.tris, run=run)
    smooth = compile_ir.crease_normals(t.points, t.tris, 40.0, run=run)
    facet = compile_ir.crease_normals(t.points, t.tris, 20.0, run=run)
    ring = np.arange(len(t.points))
    side = _side_faces(t.points, t.tris, (0, 0, 1))
    assert _spread(t.points, t.tris, smooth, ring, side).max() < 1.0 and _spread(t.points, t.tris, facet, ring, side).min() > 40.0
    lines40 = len(_feature_edge_list([m], t.points, t.tris, 40.0))
    lines20 = len(_feature_edge_list([m], t.points, t.tris, 20.0))
    assert lines20 >= lines40 + 8                        # the 8 facet lines are drawn where the facets ship
    assert len(mm.mesh_edges(t.points, t.tris, run=run, crease_deg=20.0).turn) >= 8 + len(mm.mesh_edges(t.points, t.tris, run=run).turn)


# ----------------------------------------------------------------------------------------- 2, 3: grain is not the round axis


def test_a_wooden_disc_is_painted_with_its_grain_across_its_face(tmp_path, monkeypatch):
    """A tabletop is a round part whose axis is its thickness: the wood runs across it, end grain shows on its rim."""
    meshes, scene, _ = _design(tmp_path, monkeypatch, lambda a, m: a.cylinder("top", radius=0.4, height=0.03, segments=32,
                                                                            material=m, at=(0, 0, 0.7)))
    layer, c, _ = _paint(meshes, scene)
    top = c.nrm[:, 2] > 0.9
    assert sm.wood_grain(c)["end"][top].mean() < 0.1
    assert np.median(c.thickness[top]) == pytest.approx(0.03, rel=0.05)


def test_grain_never_moves_the_axis_roundness_is_judged_about(tmp_path, monkeypatch):
    meshes, *_ = _design(tmp_path, monkeypatch, lambda a, m: a.cylinder("top", radius=0.2, height=0.03, segments=8,
                                                                      material=m, grain="x"))
    m = meshes[0]
    assert np.allclose(np.abs(m.run.points[1] - m.run.points[0]), [0, 0, 0.03], atol=1e-6)   # its own axis
    n = compile_ir.crease_normals(m.points, m.triangles, run=m.run)
    assert _spread(m.points, m.triangles, n, np.arange(len(m.points))).max() > 80.0   # the rim's corner with the caps stays hard
    fn = np.cross(m.points[m.triangles[:, 1]] - m.points[m.triangles[:, 0]], m.points[m.triangles[:, 2]] - m.points[m.triangles[:, 0]])
    side = np.abs(fn[:, 2]) < 1e-9
    corners = n.reshape(-1, 3, 3)[side]
    # every rim corner smooth: one normal per rim vertex all round (none faceted)
    per = {}
    for tri, nn in zip(m.triangles[side], corners):
        for v, x in zip(tri, nn):
            per.setdefault(tuple(np.round(m.points[v], 5)), set()).add(tuple(np.round(x, 3)))
    assert all(len(s) == 1 for s in per.values())


# ----------------------------------------------------------------------------------------- 4: a part's wood is its own


def test_adding_a_part_does_not_repaint_another(tmp_path, monkeypatch):
    def board(extra):
        def b(a, m):
            if extra:
                a.box("aa_new", size=(0.3, 0.05, 0.02), material=m, at=(2, 2, 0.01))
            a.box("board", size=(0.6, 0.12, 0.02), material=m, at=(0, 0, 0.5))
        meshes, scene, _ = _design(tmp_path / str(extra), monkeypatch, b)
        layer, c, masks = _paint(meshes, scene)
        k = [p.node_id for p in masks.texels.layout.parts].index("board")
        sel = (c.part == k) & (c.nrm[:, 2] > 0.9)
        o = np.lexsort(np.round(c.pos[sel], 4).T)
        return c.pos[sel][o], sm.wood_grain(c)["late"][sel][o]
    p1, l1 = board(False)
    p2, l2 = board(True)
    from scipy.spatial import cKDTree

    d, j = cKDTree(p1).query(p2)
    ok = d < 1e-3
    assert ok.mean() > 0.9 and np.abs(l1[j][ok] - l2[ok]).mean() < 0.01


# ----------------------------------------------------------------------------------------- 5: a stable frame


def test_a_square_posts_frame_does_not_swing_on_a_billionth_of_a_metre():
    p, _ = compile_ir.primitive_mesh(NodeIR(id="p", kind="box", params={"size": [0.05, 0.05, 0.7]}))
    p = np.asarray(p, np.float64)
    rng = np.random.default_rng(0)
    runs = [sf.fallback_run(p + rng.normal(0, 1e-9, p.shape)) for _ in range(6)]
    assert len({round(float(r.radii[0]), 6) for r, _ in runs}) == 1
    assert len({tuple(np.round(np.abs(u), 6)) for _, u in runs}) == 1


# ----------------------------------------------------------------------------------------- 6: never in a model's text


def test_handoff_fields_never_reach_text_a_model_reads():
    from antigravity3d import ir_schema

    n = NodeIR(id="leg", kind="mesh_ref", params={"path": "x.ply"}, run=ir.RunIR(path=[(0, 0, 0), (0, 0, 1)], round=True))
    if "grain" in NodeIR.model_fields:
        n = n.model_copy(update={"grain": (1.0, 0.0, 0.0)})
    s = SceneIR(asset_name="T", nodes=[n])
    assert '"run"' not in ir_schema.written_json(s) and '"grain"' not in ir_schema.written_json(s)
    assert "run" not in json.dumps(ir_schema.as_written(s))
    # what a model reads of the node, echoed back whole, parses (with the run in it, it was refused: extra input)
    echoed = {**json.loads(ir_schema.written_json(s))["nodes"][0], "transform": {"translate": [0, 0, 0]}}
    assert ir_schema.parse_tool_scene("T", [echoed], []).nodes[0].id == "leg"


# ----------------------------------------------------------------------------------------- 8: edits


def test_an_edit_never_leaves_a_stale_or_lost_run(tmp_path, monkeypatch):
    # a post the author gave grain=: its height edited afterwards, its axis follows the new height
    _, _, stage = _design(tmp_path / "g", monkeypatch, lambda a, m: a.cylinder("post", radius=0.02, height=0.3, segments=12,
                                                                             grain="x", material=m))
    stage.apply_patch(ir.IRPatch(ops=[SetParam(node_id="post", name="height", value=0.6)]))
    run = stage.extract_meshes()[0].run
    assert float(np.ptp(run.points[:, 2])) == pytest.approx(0.6, abs=1e-6)
    # a model's upsert of a script tube (which cannot carry a run) keeps the tube's run when its mesh is the same
    tube = NodeIR(id="rod", kind="mesh_ref", params={"path": "rod.ply"}, run=ir.RunIR(path=[(0, 0, 0), (0, 0, 1)], round=True))
    s2 = SceneIR(asset_name="T", nodes=[tube])
    up = ir.apply_patch(s2, ir.IRPatch(ops=[UpsertNode(node=NodeIR(id="rod", kind="mesh_ref", params={"path": "rod.ply"},
                                                                     transform=Transform(translate=(0.1, 0, 0))))]))
    assert up.nodes[0].run == tube.run
    moved = ir.apply_patch(s2, ir.IRPatch(ops=[UpsertNode(node=NodeIR(id="rod", kind="mesh_ref", params={"path": "other.ply"}))]))
    assert moved.nodes[0].run is None                     # another mesh: its run is not the old one


# ----------------------------------------------------------------------------------------- 9: one thickness


def test_thickness_is_the_parts_own_and_caps_the_summed_relief(tmp_path, monkeypatch):
    meshes, scene, _ = _design(tmp_path, monkeypatch, lambda a, m: a.box("seat", size=(0.45, 0.40, 0.03), grain="x",
                                                                       material=m, at=(0, 0, 0.45)))
    _, c, _ = _paint(meshes, scene)
    top = c.nrm[:, 2] > 0.9
    assert np.median(c.thickness[top]) == pytest.approx(0.03, rel=0.05)         # the board's own, not 103 mm
    # a 1 mm painted tube: everything together stays within the cap
    meshes, scene, _ = _design(tmp_path / "t", monkeypatch, lambda a, m: a.tube("wire", path=[(0, 0, 0.1), (0.2, 0, 0.1)],
                                                                              radius=0.0005, sides=6, material=m),
                               surface="worn painted steel, chipped")
    layer, c, _ = _paint(meshes, scene, px=4096)
    assert len(layer.height) and -layer.height.min() <= sm.RELIEF_PER_THICKNESS * 0.001 + 1e-9
    # a sheet with no thickness keeps its relief (nothing to protect)
    meshes, scene, _ = _design(tmp_path / "s", monkeypatch, lambda a, m: a.mesh("sheet", vertices=[(0, 0, 0), (0.3, 0, 0), (0.3, 0.3, 0), (0, 0.3, 0)],
                                                                              faces=[(0, 1, 2, 3)], material=m),
                               surface="worn painted steel, chipped")
    layer, c, _ = _paint(meshes, scene, px=512)
    assert -layer.height.min() > 5e-4


# ----------------------------------------------------------------------------------------- 10: one space for the decision


def test_usd_and_glb_normals_agree_under_non_uniform_scale(tmp_path):
    import pygltflib
    from pxr import Usd, UsdGeom

    from antigravity3d.kernel.package import write_engine_package

    scene = SceneIR(asset_name="T", nodes=[NodeIR(id="c", kind="cylinder", params={"radius": 0.05, "height": 0.1, "segments": 8},
                                                  transform=Transform(translate=(0, 0, 0.05), scale=(3.0, 1.0, 1.0)))])
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    budget = EngineBudget("godot_4")
    bundle = run_export_pipeline(stage.extract_meshes(), budget, "T", opaque_materials=opaque_materials(scene))
    write_engine_package(stage, bundle, budget, tmp_path / "pkg", asset_name="T")
    g = pygltflib.GLTF2().load(str(tmp_path / "pkg" / "T.glb"))
    prim = g.meshes[0].primitives[0]
    blob = g.binary_blob()

    def acc(i, k):
        a = g.accessors[i]
        bv = g.bufferViews[a.bufferView]
        s = bv.byteOffset + (a.byteOffset or 0)
        return np.frombuffer(blob[s:s + a.count * 4 * k], np.float32 if k == 3 else np.uint32).reshape(-1, k)
    gp, gn = acc(prim.attributes.POSITION, 3), acc(prim.attributes.NORMAL, 3)
    st = Usd.Stage.Open(str(tmp_path / "pkg" / "T.usd"))
    p = next(x for x in st.Traverse() if x.GetName() == "c")
    mesh = UsdGeom.Mesh(p)
    M = np.array(UsdGeom.XformCache().GetLocalToWorldTransform(p))
    up = UsdGeom.GetStageUpAxis(st)
    pts = np.asarray(mesh.GetPointsAttr().Get(), np.float64) @ M[:3, :3] + M[3, :3]
    fvi = np.asarray(mesh.GetFaceVertexIndicesAttr().Get())
    nr = np.asarray(mesh.GetNormalsAttr().Get(), np.float64) @ np.linalg.inv(M[:3, :3]).T
    nr /= np.linalg.norm(nr, axis=1, keepdims=True)
    up_pts = pts[fvi]
    if up != UsdGeom.Tokens.y:   # the GLB is Y-up
        up_pts = np.stack([up_pts[:, 0], up_pts[:, 2], -up_pts[:, 1]], 1)
        nr = np.stack([nr[:, 0], nr[:, 2], -nr[:, 1]], 1)
    from scipy.spatial import cKDTree

    tree = cKDTree(up_pts)
    worst = 0.0
    for q, n in zip(gp, gn):
        idx = tree.query_ball_point(q, 1e-5)
        worst = max(worst, min(_ang(n, nr[i]) for i in idx))
    assert worst < 1.0


# ----------------------------------------------------------------------------------------- 12: a prism is an extrusion


def test_an_extruded_octagon_ships_hard_and_wears_its_corners():
    ang = (np.arange(8) + 0.5) * np.pi / 4
    node = NodeIR(id="col", kind="extrude", params={"outline": [[float(0.05 * np.cos(a)), float(0.05 * np.sin(a))] for a in ang],
                                                   "depth": 0.6})
    assert not compile_ir.is_round(node) and compile_ir.node_run(node) is None
    p, t = compile_ir.primitive_mesh(node)
    p, t = np.asarray(p, np.float64), np.asarray(t)
    n = compile_ir.crease_normals(p, t)
    side = _side_faces(p, t, (0, 0, 1))
    assert _spread(p, t, n, np.arange(len(p)), side).min() > 40.0
    e = mm.mesh_edges(p, t)
    assert (np.degrees(e.turn) > 40).sum() >= 8
