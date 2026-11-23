"""A part tells the painter how it runs (round seven, step 2 layer 1): the run of a part (``ir.RunIR`` ->
``MeshData.run``), round parts shipped and painted as round, wood grown along the run, relief in proportion to the part.

Each test names the round-six defect it holds down. Real kernel, small atlases; no Blender.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from antigravity3d import ir
from antigravity3d.build import shapes
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, SetTransform, Transform
from antigravity3d.kernel import compile_ir
from antigravity3d.kernel import mesh_masks as mm
from antigravity3d.kernel import smart_materials as sm
from antigravity3d.kernel import surface_frame as sf
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.types import MeshData, Run
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

OAK = MaterialIR(id="oak", surface="oiled oak wood", base_color=(0.16, 0.08, 0.035), roughness=0.45)


def _bow(n: int = 17) -> np.ndarray:
    """A half circle of 0.25 m radius standing up: the steam-bent bow of a Windsor chair."""
    a = np.linspace(0.0, np.pi, n)
    return np.stack([0.25 * np.cos(a), np.zeros(n), 0.25 * np.sin(a)], 1)


def _painted(nodes, materials, tmp_path, px=512):
    """Stage -> extracted meshes -> unwrap -> the painter's layer and context for the first material."""
    scene = SceneIR(asset_name="T", nodes=nodes, materials=materials)
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    meshes = stage.extract_meshes()
    bundle = run_export_pipeline(meshes, EngineBudget("godot_4", shared_atlas=False), "T", opaque_materials=opaque_materials(scene))
    lay = [L for L in mm.layouts_from_uvs(bundle.uvs, px) if L.material_id == materials[0].id][0]
    masks = mm.compute_masks(lay, meshes)
    layer, c = sm.paint_material(sm.classify(materials[0]), materials[0], masks, {m.node_id: m for m in meshes})
    return meshes, masks, layer, c


def _tube_node(tmp_path, pid, path, radius, sides, material="oak"):
    """A tube as the script API writes it: its PLY and a mesh_ref node carrying the run."""
    t = shapes.tube(pid, path, radius, sides, False, False)
    f = tmp_path / f"{pid}.ply"
    f.write_bytes(shapes.ply_bytes(t.points, t.tris))
    run = ir.RunIR(path=[tuple(map(float, p)) for p in t.centre], radius=[float(radius)] * len(t.centre), round=True)
    return NodeIR(id=pid, kind="mesh_ref", params={"path": str(f)}, material_id=material, run=run), t


# ----------------------------------------------------------------------------------------- the run's trip


def test_a_document_without_runs_reads_and_writes_the_same_bytes():
    n = NodeIR(id="a", kind="box", params={"size": (1, 1, 1)})
    assert "run" not in n.model_dump_json() and "run" not in n.model_dump()
    from antigravity3d import ir_schema

    # the model-facing schema never shows it: only the script hand-off writes a run
    assert '"run"' not in json.dumps(ir_schema.NODE_BY_KIND["box"].model_json_schema())
    with pytest.raises(ValueError):
        ir_schema.parse_tool_scene("T", [{"id": "a", "kind": "box", "params": {"size": [1, 1, 1]},
                                          "run": {"path": [[0, 0, 0], [1, 0, 0]]}}], [])


def test_the_run_survives_hand_off_stage_extraction_and_an_edit(tmp_path, monkeypatch):
    from agy3d import Asset

    from antigravity3d.build import handoff

    monkeypatch.setenv("AGY3D_BUILD_OUT", str(tmp_path / "out"))
    a = Asset("Chair", "godot_4")
    m = a.material("ash", "#966136", surface="turned ash hardwood")
    a.tube("leg", path=[(0.2, 0, 0.4), (0.25, 0, 0.0)], radius=0.015, sides=6, material=m, mirror="x")
    a.box("seat", size=(0.45, 0.4, 0.04), grain="y", material=m, at=(0, 0, 0.42))
    build = handoff.read(a.save())
    scene = build.scene()
    by = {n.id: n for n in scene.nodes}
    assert by["leg"].run.round and by["leg__mx"].run.path[0][0] == pytest.approx(-0.2)
    assert by["seat"].run is None and by["seat"].grain == (0.0, 1.0, 0.0)   # a direction, never a line
    stage = StageManager.create(tmp_path / "Chair.usda", "Chair")
    stage.apply_ir(scene)
    got = {mm_.node_id: mm_ for mm_ in stage.extract_meshes()}
    leg = got["leg"].run
    assert leg.round and np.allclose(leg.points[[0, -1]], [[0.2, 0, 0.4], [0.25, 0, 0.0]], atol=1e-6)
    assert np.allclose(leg.radii, 0.015, atol=1e-7)
    assert np.allclose(got["leg__mx"].run.points[0], [-0.2, 0, 0.4], atol=1e-6)
    assert got["seat"].run is None and np.allclose(got["seat"].grain, [0, 1, 0])   # along y, as the author said
    # an edit (the part moved) moves its run with it; the scene format round-trips it
    stage.apply_patch(ir.IRPatch(ops=[SetTransform(node_id="leg", transform=Transform(translate=(0.0, 0.1, 0.0)))]))
    moved = {m_.node_id: m_ for m_ in stage.extract_meshes()}["leg"].run
    assert np.allclose(moved.points - leg.points, [0.0, 0.1, 0.0], atol=1e-6)
    assert {n.id: n for n in stage.scene_ir().nodes}["leg"].run == by["leg"].run


# ----------------------------------------------------------------------------------------- round parts


def test_round_lives_in_one_rule():
    cyl = lambda n: NodeIR(id="c", kind="cylinder", params={"radius": 0.01, "height": 0.3, "segments": n})  # noqa: E731
    # hex bolts and pentagonal nuts are prisms on purpose (round six's hydrant); 7+ segments is a budget, not a shape
    assert not compile_ir.is_round(cyl(5)) and not compile_ir.is_round(cyl(6))
    assert compile_ir.is_round(cyl(7)) and compile_ir.is_round(cyl(8)) and compile_ir.is_round(cyl(24))
    loft = NodeIR(id="l", kind="mesh_ref", params={"path": "x.ply"}, run=ir.RunIR(path=[(0, 0, 0), (1, 0, 0)]))
    assert not compile_ir.is_round(loft)


def test_a_six_sided_rod_is_not_painted_as_edge_and_ships_smooth():
    """Round six: a 6-sided 7 mm spindle read 102-110 degrees of convex turn on every texel (edge 100 %) and shipped as
    a prism under the 40-degree crease."""
    t = shapes.tube("s", [(0, 0, 0), (0, 0.02, 0.2), (0, 0, 0.4)], 0.007, 6, False, False)
    run = Run(t.centre, np.full(len(t.centre), 0.007), True)
    q = np.array([[0.007, 0.0, 0.1], [0.0, 0.017, 0.2]])   # on the side, away from the ends
    old = np.degrees(mm.turn_field(q, mm.mesh_edges(t.points, t.tris), mm.EDGE_RADIUS_M, +1.0))
    new = np.degrees(mm.turn_field(q, mm.mesh_edges(t.points, t.tris, run=run), mm.EDGE_RADIUS_M, +1.0))
    assert old.min() > 40.0 and new.max() < 1.0
    # the end caps are still edges
    cap = np.array(t.centre[0]) + [0.006, 0, 0]
    assert np.degrees(mm.turn_field(cap[None], mm.mesh_edges(t.points, t.tris, run=run), mm.EDGE_RADIUS_M, +1.0))[0] > 60.0
    # shipped normals: around the rod one normal per side vertex (smooth), caps hard
    n_old = compile_ir.crease_normals(t.points, t.tris)
    n_new = compile_ir.crease_normals(t.points, t.tris, run=run)
    side = sf.round_side_faces(t.points, t.tris, run)
    corners = np.repeat(side, 3)
    ring0 = np.repeat(t.tris[side][:, :], 1).ravel()
    for normals, smooth in ((n_old, False), (n_new, True)):
        per_vertex = {}
        for v, nv in zip(t.tris[side].ravel(), normals[corners]):
            per_vertex.setdefault(int(v), set()).add(tuple(np.round(nv, 4)))
        assert all(len(s) == 1 for s in per_vertex.values()) == smooth
    assert ring0.size
    # a hex bolt (cylinder of 6) stays a prism; a cylinder of 8 ships round through its derived run
    bolt = NodeIR(id="b", kind="cylinder", params={"radius": 0.01, "height": 0.02, "segments": 6})
    post = NodeIR(id="p", kind="cylinder", params={"radius": 0.01, "height": 0.4, "segments": 8})
    for node, smooth in ((bolt, False), (post, True)):
        p, tr = compile_ir.primitive_mesh(node)
        r = compile_ir.node_run(node)
        rr = Run(np.asarray(r.path), np.asarray(r.radius), r.round)
        assert sf.round_side_faces(p, tr, rr).any() == smooth


def test_a_round_part_ships_smooth_in_the_glb_and_the_usd(tmp_path):
    from antigravity3d.kernel.package import write_engine_package

    node, _ = _tube_node(tmp_path, "spindle", [(0, 0, 0.0), (0, 0, 0.4)], 0.007, 6)
    scene = SceneIR(asset_name="T", nodes=[node], materials=[OAK])
    stage = StageManager.create(tmp_path / "T.usda", "T")
    stage.apply_ir(scene)
    budget = EngineBudget("godot_4", generated_surfaces=False)
    bundle = run_export_pipeline(stage.extract_meshes(), budget, "T", opaque_materials=opaque_materials(scene))
    res = write_engine_package(stage, bundle, budget, tmp_path / "pkg", asset_name="T")
    import pygltflib

    g = pygltflib.GLTF2().load(str(tmp_path / "pkg" / "T.glb"))
    prim = g.meshes[0].primitives[0]
    acc = g.accessors[prim.attributes.NORMAL]
    blob = g.binary_blob()
    bv = g.bufferViews[acc.bufferView]
    n = np.frombuffer(blob[bv.byteOffset + (acc.byteOffset or 0): bv.byteOffset + (acc.byteOffset or 0) + acc.count * 12], np.float32).reshape(-1, 3)
    # glTF is Y-up: a vertical rod's side normals lie in the x-z plane. Faceted: 6 directions; smooth: one per vertex
    sides = n[np.abs(n[:, 1]) < 0.5]
    assert len(np.unique(np.round(sides, 3), axis=0)) > 6
    assert not [i for i in res.issues if i.severity == "critical"]


# ----------------------------------------------------------------------------------------- wood


def test_oiled_is_a_finish_on_wood_not_dirt():
    """Round six: "aged oiled oak" read as dirt 0.55 (the grime word list) and the chair was painted grimy."""
    wood = sm.classify(MaterialIR(id="a", surface="aged oiled oak wood grain"))
    assert wood.surface_class == "wood" and wood.dirt < 0.5
    assert sm.classify(MaterialIR(id="b", surface="oiled steel")).dirt == pytest.approx(0.55)


# ----------------------------------------------------------------------------------------- relief in proportion


def test_the_normal_map_uses_each_parts_own_texel_size():
    h = np.zeros((4, 8))
    h[:, :4] = np.arange(4) * 1e-4    # the same height step on two parts
    h[:, 4:] = np.arange(4) * 1e-4
    part = np.zeros((4, 8), np.int32)
    part[:, 4:] = 1
    n = sm.normal_map(h, np.ones((4, 8), bool), part, np.array([1e-3, 2e-3]))
    # a part packed at half the density has texels twice as large: half the slope
    assert abs(n[1, 1, 0]) > 1.8 * abs(n[1, 5, 0])


def test_run_fields_follow_a_bent_line():
    run = Run(_bow(), np.full(17, 0.01), True)
    a = np.linspace(0.0, np.pi, 9)[1:-1]
    p = np.stack([0.26 * np.cos(a), np.zeros(7), 0.26 * np.sin(a)], 1)   # 1 cm outside the bow
    f = sf.run_fields(p, run)
    assert np.allclose(f.radial_m, 0.01, atol=5e-4)
    assert np.allclose(f.along01, a / np.pi, atol=0.01)
    assert np.allclose(np.abs((f.direction * np.stack([-np.sin(a), np.zeros(7), np.cos(a)], 1)).sum(1)), 1.0, atol=0.01)
    assert np.allclose(f.thickness_m, 0.02)        # a rod's thickness is its diameter


def test_a_part_without_a_run_gets_its_principal_axis_and_thickness():
    p, _ = compile_ir.primitive_mesh(NodeIR(id="b", kind="box", params={"size": [0.45, 0.4, 0.04]}))
    run, u0 = sf.fallback_run(p)
    assert np.allclose(np.abs(run.points[1] - run.points[0]), [0.45, 0, 0], atol=1e-6)
    assert 2 * run.radii[0] == pytest.approx(0.04) and np.allclose(np.abs(u0), [0, 0, 1])
    m = MeshData("b", "/b", np.asarray(p, np.float64), np.zeros((0, 3), np.int32))
    assert 2 * sf.part_grain(m)[0].radii[0] == pytest.approx(0.04)


# ----------------------------------------------------------------------------------------- a loft's line


def _rail_sections(w=0.036, h=0.022):
    """A flat rail (w x h) along x for 0.4 m, then round a right angle into y for 0.4 m (sections square to the path)."""
    secs = []
    for t in np.linspace(0, 0.4, 5):                      # first leg, along x
        secs.append([(t, -w / 2, -h / 2), (t, w / 2, -h / 2), (t, w / 2, h / 2), (t, -w / 2, h / 2)])
    for k in range(1, 4):                                 # the corner, 22.5 degrees a step about (0.4, 0.1)
        a = k * np.pi / 8
        cx, cy = 0.4 + 0.1 * np.sin(a), 0.1 - 0.1 * np.cos(a)
        nx, ny = -np.sin(a), np.cos(a)                    # across the rail, in the plane
        secs.append([(cx - nx * w / 2, cy - ny * w / 2, -h / 2), (cx + nx * w / 2, cy + ny * w / 2, -h / 2),
                     (cx + nx * w / 2, cy + ny * w / 2, h / 2), (cx - nx * w / 2, cy - ny * w / 2, h / 2)])
    for t in np.linspace(0.1, 0.5, 5):                    # second leg, along y
        secs.append([(0.5 + w / 2, t, -h / 2), (0.5 - w / 2, t, -h / 2), (0.5 - w / 2, t, h / 2), (0.5 + w / 2, t, h / 2)])
    return [[tuple(map(float, p)) for p in s] for s in secs]


def _loft_ctx(tmp_path, monkeypatch, sections, **kw):
    from agy3d import Asset

    from antigravity3d.build import handoff

    monkeypatch.setenv("AGY3D_BUILD_OUT", str(tmp_path / "out"))
    a = Asset("T", "godot_4")
    m = a.material("oak", "#6e4224", surface="oak wood")
    a.loft("rail", sections, material=m, **kw)
    scene = handoff.read(a.save()).scene()
    _, masks, layer, c = (None, *_painted(scene.nodes, scene.materials, tmp_path)[1:])
    return scene.nodes[0], c


def test_a_bent_lofted_rail_has_its_grain_along_both_legs(tmp_path, monkeypatch):
    """A loft's sections' centres are the way it runs (round six's chair: the horseshoe arm rail, whose side arms got
    one straight grain line across them); the thickness is the sections' own (a 22 mm rail, not its 0.5 m span)."""
    node, c = _loft_ctx(tmp_path, monkeypatch, _rail_sections())
    assert node.run is not None and not node.run.round and not compile_ir.is_round(node)
    top = c.nrm[:, 2] > 0.9
    leg1 = top & (c.pos[:, 0] < 0.35)
    leg2 = top & (c.pos[:, 1] > 0.15)
    assert np.abs(c.run.direction[leg1, 0]).min() > 0.95      # along x on the first leg
    assert np.abs(c.run.direction[leg2, 1]).min() > 0.95      # along y on the second
    assert np.median(c.thickness[top]) == pytest.approx(0.022, rel=0.05)


def test_a_tray_that_folds_back_keeps_a_straight_grain_and_grain_wins(tmp_path, monkeypatch):
    def rect(w, d, z, y=0.0):
        return [(-w / 2, y - d / 2, z), (w / 2, y - d / 2, z), (w / 2, y + d / 2, z), (-w / 2, y + d / 2, z)]
    tray = [rect(0.34, 0.50, 0.36), rect(0.60, 0.86, 0.60, y=0.04), rect(0.57, 0.83, 0.60, y=0.04), rect(0.31, 0.47, 0.38)]
    node, c = _loft_ctx(tmp_path / "tray", monkeypatch, tray)       # the script page's wheelbarrow tray
    assert node.run is None                                          # up the outside, down the inside: no line
    d = c.run.direction
    assert np.allclose(np.abs(d), np.abs(d[0]), atol=1e-9)          # one straight direction over the whole tray
    _, c = _loft_ctx(tmp_path / "g", monkeypatch, _rail_sections(), grain="x")
    assert np.allclose(np.abs(c.run.direction), [1, 0, 0], atol=1e-9)   # the author's grain= wins
