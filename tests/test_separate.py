"""kernel/separate.py: coplanar same-facing faces of different parts moved apart without a model call."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, Transform, apply_patch
from antigravity3d.kernel.fast_ops import shipped_masks, tier1_qa
from antigravity3d.kernel.separate import MARGIN, separate_coplanar
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

BUDGET = EngineBudget("godot_4")          # leaves hidden contact faces out of the render meshes
CLOSED = EngineBudget("roblox_r15")       # ships closed meshes: contact faces ship


def _box(nid, size, at, rot=(0.0, 0.0, 0.0)):
    return NodeIR(id=nid, kind="box", params={"size": size}, transform=Transform(translate=at, rotate_euler_deg=rot),
                  material_id="paint")


def _scene(*nodes) -> SceneIR:
    return SceneIR(asset_name="Prop", materials=[MaterialIR(id="paint")], nodes=list(nodes))


def _meshes(tmp: Path, scene: SceneIR):
    sm = StageManager.create(tmp / f"s{len(list(tmp.iterdir()))}.usda", scene.asset_name)
    sm.apply_ir(scene)
    return sm.extract_meshes()


def _bounds(meshes, nid):
    m = next(m for m in meshes if m.node_id == nid)
    return m.points.min(axis=0), m.points.max(axis=0)


def _codes(meshes, budget=BUDGET):
    return sorted(i.code for i in tier1_qa(meshes, budget).issues)


def _separated(tmp: Path, scene: SceneIR, budget=BUDGET):
    meshes = _meshes(tmp, scene)
    r = separate_coplanar(scene, meshes, hidden=shipped_masks(meshes, budget))
    after = apply_patch(scene, IRPatch(ops=r.ops)) if r.ops else scene
    return r, after, _meshes(tmp, after)


BASE = _box("base", (1.0, 1.0, 0.2), (0.0, 0.0, 0.1))


def test_rib_crossing_a_panel_of_the_same_thickness_stands_proud(tmp_path: Path) -> None:
    # panel and rib both rest on the base, the rib crosses the panel; their tops share z = 0.21 (visible flicker),
    # their bottoms share the base's top (contact faces the exporter leaves out)
    scene = _scene(BASE, _box("panel", (0.4, 0.4, 0.01), (0.0, 0.0, 0.205)), _box("rib", (0.04, 0.6, 0.01), (0.0, 0.0, 0.205)))
    before = _meshes(tmp_path, scene)
    assert "z_fighting" in _codes(before)
    # Tier-1 describes what ships: on godot_4 the bottoms are left out, so only the tops are a finding
    assert [i.detail.split(":")[0] for i in tier1_qa(before, BUDGET).issues if i.code == "z_fighting"] == ["panel/rib"]
    r, after, meshes = _separated(tmp_path, scene)
    assert [(m.node_id, m.axis, m.sign) for m in r.moves] == [("rib", 2, 1)]  # 2/3 of the rib's top lies on the panel
    assert abs(r.moves[0].delta - MARGIN) < 1e-9
    lo, hi = _bounds(meshes, "rib")
    assert abs(hi[2] - (0.21 + MARGIN)) < 1e-6 and abs(lo[2] - 0.2) < 1e-6  # proud; still resting on the base
    assert [(u.a, u.b) for u in r.hidden_contact] == [("panel", "rib")] and not r.left
    assert "hidden contact faces, not shipped" in r.describe()
    assert not {"floating_part", "floating_group"} & set(_codes(meshes))
    assert "z_fighting" not in _codes(meshes)
    # where contact faces ship (closed render meshes) they are a finding, and the rib is sunk into the base as well
    assert "z_fighting" in _codes(meshes, CLOSED)
    r2, _, meshes2 = _separated(tmp_path, scene, CLOSED)
    assert not r2.unresolved and {(m.node_id, m.kind) for m in r2.moves} == {("rib", "visible"), ("rib", "contact")}
    assert abs(_bounds(meshes2, "rib")[0][2] - (0.2 - MARGIN)) < 1e-6
    assert "z_fighting" not in _codes(meshes2, CLOSED)


def test_two_plates_meeting_at_a_corner_sit_back(tmp_path: Path) -> None:
    # an L of two plates overlapping at the corner, standing on the base, their top and end faces flush
    scene = _scene(BASE, _box("plate_x", (0.01, 0.3, 0.2), (-0.2, 0.0, 0.3)), _box("plate_y", (0.3, 0.01, 0.2), (-0.055, -0.145, 0.3)))
    assert "z_fighting" in _codes(_meshes(tmp_path, scene))
    r, _, meshes = _separated(tmp_path, scene)
    assert {(m.node_id, m.axis, m.sign) for m in r.moves} == {("plate_y", 0, -1), ("plate_x", 1, -1), ("plate_x", 2, 1)}
    assert all(m.delta < 0 for m in r.moves)  # end faces sit back: nothing grows, the bottoms stay on the base
    assert not r.left and [(u.a, u.b) for u in r.hidden_contact] == [("plate_x", "plate_y")]
    assert "z_fighting" not in _codes(meshes)
    assert not {"floating_part", "floating_group"} & set(_codes(meshes))
    lo = np.min([m.points.min(axis=0) for m in meshes], axis=0)
    hi = np.max([m.points.max(axis=0) for m in meshes], axis=0)
    assert abs(hi[2] - 0.4) < 1e-6 and abs(lo[2]) < 1e-6  # the outline does not grow


def test_parts_sunk_into_a_body_move_deeper_and_stay_hidden(tmp_path: Path) -> None:
    # two plates sunk 4 mm into the body's front, one crossing the other: their back faces share y = -0.496 inside it
    body = _box("body", (1.0, 1.0, 1.0), (0.0, 0.0, 0.5))
    scene = _scene(body, _box("panel", (0.6, 0.012, 0.6), (0.0, -0.502, 0.5)), _box("rib", (0.05, 0.016, 0.8), (0.0, -0.504, 0.5)))
    assert "z_fighting" in _codes(_meshes(tmp_path, scene))
    r, _, meshes = _separated(tmp_path, scene)
    assert [m.kind for m in r.moves] == ["enclosed"] and not r.unresolved
    m = r.moves[0]
    lo, hi = _bounds(meshes, m.node_id)
    assert abs(abs(hi[1] + 0.496) - MARGIN) < 1e-6  # 2 mm from the other back face
    assert -0.5 < hi[1] < 0.5  # and still inside the body
    assert np.allclose(_bounds(meshes, "body"), _bounds(_meshes(tmp_path, scene), "body"))
    assert "z_fighting" not in _codes(meshes)


def test_bottoms_sharing_the_ground_plane_lift_the_smaller_part(tmp_path: Path) -> None:
    # a corner post overlapping the body, both standing on the ground: their bottoms share z = 0. The post (smaller
    # bottom) is lifted 2 mm, still attached to the body, which keeps the ground contact.
    scene = _scene(_box("body", (0.6, 0.6, 0.4), (0.0, 0.0, 0.2)), _box("post", (0.05, 0.05, 0.42), (0.29, 0.29, 0.21)))
    assert "z_fighting" in _codes(_meshes(tmp_path, scene))
    r, _, meshes = _separated(tmp_path, scene)
    ground = [m for m in r.moves if m.kind == "ground"]
    assert [(m.node_id, m.axis, m.sign) for m in ground] == [("post", 2, -1)] and abs(ground[0].delta + MARGIN) < 1e-9
    assert "lifted 1 part(s) 2 mm off the ground plane: post" in r.describe()
    assert abs(_bounds(meshes, "post")[0][2] - MARGIN) < 1e-6 and abs(_bounds(meshes, "body")[0][2]) < 1e-7
    assert "z_fighting" not in _codes(meshes) and not {"floating_part", "floating_group"} & set(_codes(meshes))
    # a part that would touch nothing once lifted is left alone (to the author): a small block sunk into a 1 mm mat
    scene = _scene(_box("mat", (1.0, 1.0, 0.001), (0.0, 0.0, 0.0005)), _box("block", (0.1, 0.1, 0.1), (0.0, 0.0, 0.05)))
    r, _, meshes = _separated(tmp_path, scene)
    assert not [m for m in r.moves if m.kind == "ground"]
    assert any("cannot be lifted" in u.why for u in r.left), r.describe()
    assert all(abs(m.points[:, 2].min()) < 1e-7 for m in meshes)


def test_rotated_parts_are_left_alone_and_reported(tmp_path: Path) -> None:
    rot = (30.0, 0.0, 0.0)
    scene = _scene(_box("slab", (0.6, 0.6, 0.1), (0.0, 0.0, 0.5), rot), _box("plate", (0.2, 0.2, 0.1), (0.0, 0.0, 0.5), rot))
    r = separate_coplanar(scene, _meshes(tmp_path, scene))
    assert not r.ops and r.left and all("not axis-aligned" in u.why for u in r.left)
    assert "unresolved" in r.describe()


def test_nothing_to_do(tmp_path: Path) -> None:
    scene = _scene(BASE, _box("crate", (0.3, 0.3, 0.3), (0.0, 0.0, 0.35)))
    r = separate_coplanar(scene, _meshes(tmp_path, scene))
    assert not r.ops and not r.moves and not r.unresolved and r.pairs_found == 0
    assert r.describe() == "no coplanar same-facing faces between parts"


def test_idempotent(tmp_path: Path) -> None:
    scene = _scene(BASE, _box("panel", (0.4, 0.4, 0.01), (0.0, 0.0, 0.205)), _box("rib", (0.04, 0.6, 0.01), (0.0, 0.0, 0.205)),
                   _box("plate_x", (0.01, 0.3, 0.2), (-0.3, 0.3, 0.3)), _box("plate_y", (0.3, 0.01, 0.2), (-0.155, 0.155, 0.3)))
    for budget in (BUDGET, CLOSED):
        r, after, meshes = _separated(tmp_path, scene, budget)
        assert r.ops
        again = separate_coplanar(after, meshes, hidden=shipped_masks(meshes, budget))
        assert not again.ops and not again.moves and not again.left


def test_cylinder_cap_flush_with_a_bracket(tmp_path: Path) -> None:
    # a handle grip (cylinder along y) whose caps are flush with the outer faces of its two brackets
    grip = NodeIR(id="grip", kind="cylinder", params={"radius": 0.012, "height": 0.16, "segments": 12},
                  transform=Transform(translate=(-0.525, 0.0, 0.1), rotate_euler_deg=(90.0, 0.0, 0.0)), material_id="paint")
    scene = _scene(BASE, grip, _box("br_f", (0.04, 0.02, 0.03), (-0.52, -0.07, 0.1)), _box("br_b", (0.04, 0.02, 0.03), (-0.52, 0.07, 0.1)))
    assert "z_fighting" in _codes(_meshes(tmp_path, scene))
    r, after, meshes = _separated(tmp_path, scene)
    assert {m.node_id for m in r.moves} <= {"grip", "br_f", "br_b"} and r.moves
    assert "z_fighting" not in _codes(meshes)
    if any(m.node_id == "grip" for m in r.moves):
        assert next(n for n in after.nodes if n.id == "grip").params["height"] != 0.16


# ------------------------------------------------------------------ three detailed designs written live (50-65 parts)

import json  # noqa: E402

import pytest  # noqa: E402

from antigravity3d import ir_schema  # noqa: E402
from antigravity3d.kernel import coplanar  # noqa: E402
from antigravity3d.kernel.fast_ops import ZFIGHT_EPS, run_export_pipeline, shipped  # noqa: E402

DESIGNS = sorted((Path(__file__).parent / "data" / "coplanar").glob("*.json"))
ENGINES = [EngineBudget("godot_4", max_tris=3000), EngineBudget("roblox_r15", max_tris=3000)]


def _design(path: Path) -> SceneIR:
    return ir_schema.parse_scene(path.read_text())


@pytest.mark.parametrize("budget", ENGINES, ids=lambda b: b.target_engine)
@pytest.mark.parametrize("path", DESIGNS, ids=lambda p: p.stem)
def test_tier1_excludes_exactly_the_pairs_the_exporter_leaves_out(tmp_path: Path, path: Path, budget: EngineBudget) -> None:
    """The check and the export agree: every same-facing pair Tier-1 leaves out has both faces absent from the
    exported LOD0, and every pair it reports has a face that ships."""
    scene = _design(path)
    meshes = _meshes(tmp_path, scene)
    bundle = run_export_pipeline(meshes, budget, scene.asset_name, opaque_materials=coplanar.opaque_materials(scene))
    by_id = {u.node_id: u for u in bundle.uvs}
    gone = [by_id[m.node_id].hidden if by_id[m.node_id].hidden is not None else np.zeros(len(m.triangles), bool) for m in meshes]
    for m, g in zip(meshes, gone):  # LOD0 as written: the shipped triangles are exactly the ones not marked
        assert len(shipped(by_id[m.node_id]).triangles) == len(m.triangles) - int(g.sum())
    parts = [coplanar.Part(m.node_id, m.points, m.triangles) for m in meshes]
    c = coplanar.coplanar_contacts(parts, ZFIGHT_EPS, facing="same")
    pairs: dict[tuple[str, str], list[bool]] = {}
    for i in range(len(parts)):
        for a, lst in c.same[i].items():
            for j, b in lst:
                if j > i:
                    pairs.setdefault((meshes[i].node_id, meshes[j].node_id), []).append(bool(gone[i][a] and gone[j][b]))
    reported = {tuple(i.detail.split(":")[0].split("/")) for i in tier1_qa(meshes, budget).issues if i.code == "z_fighting"}
    assert pairs  # these designs have coplanar faces
    for key, absent in pairs.items():
        assert (key in reported) == (not all(absent)), (key, absent)
    if budget.target_engine == "roblox_r15":  # closed meshes ship whole: every pair is a finding
        assert reported == set(pairs)


@pytest.mark.parametrize("budget", ENGINES, ids=lambda b: b.target_engine)
@pytest.mark.parametrize("path", DESIGNS, ids=lambda p: p.stem)
def test_detailed_designs_end_with_no_coplanar_findings(tmp_path: Path, path: Path, budget: EngineBudget) -> None:
    scene = _design(path)
    meshes = _meshes(tmp_path, scene)
    before = tier1_qa(meshes, budget)
    r = separate_coplanar(scene, meshes, hidden=shipped_masks(meshes, budget))
    assert r.ops and not r.left, r.describe()
    after_meshes = _meshes(tmp_path, apply_patch(scene, IRPatch(ops=r.ops)))
    after = tier1_qa(after_meshes, budget)
    assert not [i for i in after.issues if i.code == "z_fighting"]
    floats = lambda t: {(i.code, i.node_id) for i in t.issues if i.code in ("floating_part", "floating_group")}  # noqa: E731
    assert not floats(after) - floats(before)
    assert sum(i.severity == "critical" for i in after.issues) <= sum(i.severity == "critical" for i in before.issues)
    assert sum(len(m.triangles) for m in after_meshes) == sum(len(m.triangles) for m in meshes)
    lo0 = np.min([m.points.min(axis=0) for m in meshes], axis=0)
    hi0 = np.max([m.points.max(axis=0) for m in meshes], axis=0)
    lo1 = np.min([m.points.min(axis=0) for m in after_meshes], axis=0)
    hi1 = np.max([m.points.max(axis=0) for m in after_meshes], axis=0)
    assert (lo0 - lo1).max() <= MARGIN + 1e-9 and (hi1 - hi0).max() <= MARGIN + 1e-9
    again = separate_coplanar(apply_patch(scene, IRPatch(ops=r.ops)), after_meshes, hidden=shipped_masks(after_meshes, budget))
    assert not again.ops


def test_a_part_with_child_parts_is_not_moved(tmp_path: Path) -> None:
    # the rib of the rib/panel scene carries a child (a small box parented to it, standing on the base beside the
    # rib's end): moving the rib would move the child too, unchecked, so the rib is refused and the panel is tried
    rib = _box("rib", (0.04, 0.6, 0.01), (0.0, 0.0, 0.205))
    tag = NodeIR(id="tag", kind="box", params={"size": (0.03, 0.03, 0.03)}, parent="rib",
                 transform=Transform(translate=(0.0, 0.32, 0.01)), material_id="paint")  # on the base, past the rib's end
    scene = _scene(BASE, _box("panel", (0.4, 0.4, 0.01), (0.0, 0.0, 0.205)), rib, tag)
    before = _meshes(tmp_path, scene)
    r, _, meshes = _separated(tmp_path, scene)
    assert not [m for m in r.moves if m.node_id == "rib"]
    moved = {m.node_id for m in r.moves}
    assert moved <= {"panel"}
    assert moved or any("child parts" in u.why for u in r.left), r.describe()
    assert "z_fighting" not in _codes(meshes) and not {"floating_part", "floating_group"} & set(_codes(meshes))
    for m in before:
        if m.node_id not in moved:
            assert np.allclose(_bounds(before, m.node_id), _bounds(meshes, m.node_id)), m.node_id


def _barrel(nid, at, rot, radius=0.008, height=0.052, segments=10):
    return NodeIR(id=nid, kind="cylinder", params={"radius": radius, "height": height, "segments": segments},
                  transform=Transform(translate=at, rotate_euler_deg=rot), material_id="paint")


def test_a_hinge_barrel_whose_side_facets_lie_by_the_clasp_faces_grows_its_radius(tmp_path: Path) -> None:
    # a latch clasp (14 mm thick) with a 10-segment barrel along x through its top edge: the barrel's +y and -y facets
    # lie 8 cos(18 deg) = 7.6 mm from its axis, 0.6 mm proud of the clasp's faces at 7 mm
    scene = _scene(_box("clasp", (0.04, 0.014, 0.08), (0.0, 0.0, 0.04)), _barrel("hinge", (0.0, 0.0, 0.08), (0.0, 90.0, 0.0)))
    before = _meshes(tmp_path, scene)
    assert "z_fighting" in _codes(before)
    r, after, meshes = _separated(tmp_path, scene)
    assert [(m.node_id, m.param) for m in r.moves] == [("hinge", "radius")] and not r.unresolved, r.describe()
    assert abs(r.moves[0].delta - (MARGIN - (0.008 * np.cos(np.pi / 10) - 0.007))) < 1e-6  # continues the offset
    hinge = next(n for n in after.nodes if n.id == "hinge")
    assert hinge.params["radius"] > 0.008 and hinge.transform == next(n for n in scene.nodes if n.id == "hinge").transform
    assert np.allclose(_bounds(meshes, "clasp"), _bounds(before, "clasp"))  # nothing else moves
    assert "z_fighting" not in _codes(meshes) and not {"floating_part", "floating_group"} & set(_codes(meshes))
    again = separate_coplanar(after, meshes, hidden=shipped_masks(meshes, BUDGET))
    assert not again.ops


def test_a_barrel_resting_on_a_plate_by_a_facet_keeps_its_radius(tmp_path: Path) -> None:
    # a barrel along y lying on a plate by its -z facet, passing through a block whose top is flush with the barrel's
    # +z facet: a radius change would lift the resting facet off the plate too, so the barrel is not moved
    h = 0.008 * np.cos(np.pi / 10)
    scene = _scene(_box("plate", (0.4, 0.4, 0.02), (0.0, 0.0, 0.01)), _barrel("barrel", (0.0, 0.0, 0.02 + h), (90.0, 0.0, 0.0), height=0.2),
                   _box("block", (0.04, 0.03, 2 * h), (0.0, 0.0, 0.02 + h)))
    before = _meshes(tmp_path, scene)
    assert "z_fighting" in _codes(before)
    r, after, meshes = _separated(tmp_path, scene)
    assert not [m for m in r.moves if m.node_id == "barrel"], r.describe()
    assert next(n for n in after.nodes if n.id == "barrel").params["radius"] == 0.008
    assert np.allclose(_bounds(meshes, "barrel"), _bounds(before, "barrel"))
    assert r.moves or any("barrel" in (u.a, u.b) for u in r.unresolved), r.describe()
    assert not {"floating_part", "floating_group"} & set(_codes(meshes))


def test_unresolved_pairs_of_two_parts_sort_when_one_is_axis_aligned_and_one_is_not():
    """The same two parts with an axis-aligned pair and a slanted one: the keys hold an int axis and a None axis, and
    the report of what was left must not raise on ordering them (it did, in a live native session)."""
    from antigravity3d.kernel import separate as sep

    keys = [("a", "b", (None, (0.0, 0.6, 0.8))), ("a", "b", (2, 1)), ("a", "b", (0, -1)), ("a", "b", (0, 1)), ("a", "c", (1, 1))]
    ordered = sorted(keys, key=sep._key_order)
    assert ordered[:3] == [("a", "b", (0, -1)), ("a", "b", (0, 1)), ("a", "b", (2, 1))]  # the order axis-aligned keys always had
    assert ordered[3] == ("a", "b", (None, (0.0, 0.6, 0.8))) and ordered[4] == ("a", "c", (1, 1))
