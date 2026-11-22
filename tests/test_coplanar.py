"""Coplanar contact between parts: hidden contact faces left out of what ships, and z-fighting in Tier-1.

The rule: a triangle of a closed opaque part covered completely by opposite-facing coplanar faces of other closed
opaque parts of the same asset is not on the surface of the union of the solids, so it is not shipped. The working
stage, Tier-1 and the colliders keep the closed parts; LOD chains are the same triangles as without the rule.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import coplanar
from antigravity3d.kernel.compile_ir import _box
from antigravity3d.kernel.fast_ops import run_export_pipeline, shipped, tier1_qa
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget

WOOD = MaterialIR(id="wood", base_color=(0.5, 0.35, 0.2))
IRON = MaterialIR(id="iron", base_color=(0.3, 0.3, 0.3), metallic=1.0)
GLASS = MaterialIR(id="glass", base_color=(0.8, 0.9, 1.0), alpha_mode="BLEND", opacity=0.4)


def _mesh(nid: str, lo, hi, mat: str = "wood") -> MeshData:
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    p, t = _box(*(hi - lo))
    return MeshData(nid, f"/{nid}", np.asarray(p, float) + (lo + hi) / 2, np.asarray(t), mat)


def _hidden(*meshes: MeshData) -> list[int]:
    parts = [coplanar.Part(m.node_id, m.points, m.triangles) for m in meshes]
    return [int(h.sum()) for h in coplanar.hidden_triangles(parts, [True] * len(parts))]


def test_exact_cover_hides_both_faces_and_the_ground_face_stays():
    assert _hidden(_mesh("a", (0, 0, 0), (1, 1, 0.5)), _mesh("b", (0, 0, 0.5), (1, 1, 1))) == [2, 2]


def test_partial_cover_hides_only_the_smaller_face():
    # 99%: a's top keeps a 1 cm strip in view, b's bottom lies wholly on a
    assert _hidden(_mesh("a", (0, 0, 0), (1, 1, 0.5)), _mesh("b", (0, 0, 0.5), (0.99, 1, 1))) == [0, 2]


def test_two_parts_together_cover_a_face_neither_covers_alone():
    a, b1, b2 = _mesh("a", (0, 0, 0), (1, 1, 0.5)), _mesh("b1", (0, 0, 0.5), (0.4, 1, 0.8)), _mesh("b2", (0.4, 0, 0.5), (1, 1, 0.7))
    assert _hidden(a, b1, b2) == [2, 2, 4]  # b2's side against the taller b1 is covered too
    slit = _mesh("b2", (0.401, 0, 0.5), (1, 1, 0.7))  # a 1 mm slit between the covers keeps a's top
    assert _hidden(a, b1, slit) == [0, 2, 2]


def test_gap_penetration_and_edge_contact_hide_nothing():
    a = _mesh("a", (0, 0, 0), (1, 1, 0.5))
    assert _hidden(a, _mesh("b", (0.2, 0.2, 0.50005), (0.8, 0.8, 1))) == [0, 0]  # 50 um gap: a slit to see into
    assert _hidden(a, _mesh("b", (0.2, 0.2, 0.49995), (0.8, 0.8, 1))) == [0, 0]  # sunk 50 um: not coplanar contact
    assert _hidden(a, _mesh("b", (1, 0, 0.5), (2, 1, 1))) == [0, 0]  # meet along an edge only


def test_open_or_see_through_parts_neither_lose_nor_hide_faces():
    a, b = _mesh("a", (0, 0, 0), (1, 1, 0.5)), _mesh("b", (0, 0, 0.5), (1, 1, 1))
    parts = [coplanar.Part(m.node_id, m.points, m.triangles) for m in (a, b)]
    assert [int(h.sum()) for h in coplanar.hidden_triangles(parts, [True, False])] == [0, 0]
    assert [int(h.sum()) for h in coplanar.hidden_triangles(parts, [True, True], [True, False])] == [0, 0]


def _scene(nodes, mats=(WOOD, IRON)) -> SceneIR:
    return SceneIR(asset_name="Prop", nodes=nodes, materials=list(mats))


def _node(i, lo, hi, mat="wood"):
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    return NodeIR(id=i, kind="box", params={"size": tuple((hi - lo).tolist())},
                  transform=Transform(translate=tuple(((lo + hi) / 2).tolist())), material_id=mat)


STACK = [_node("base", (-0.5, -0.5, 0), (0.5, 0.5, 0.4)), _node("lid", (-0.5, -0.5, 0.4), (0.5, 0.5, 0.5), "iron"),
         NodeIR(id="post", kind="cylinder", params={"radius": 0.1, "height": 0.6, "segments": 16},
                transform=Transform(translate=(0, 0, 0.8)), material_id="iron")]


def _package(tmp: Path, scene: SceneIR, engine: str = "unreal_5", rule: bool = True):
    sm = StageManager.create(tmp / "s.usda", scene.asset_name)
    sm.apply_ir(scene)
    # rule=False ships the closed parts through the budget's own switch (keep_contact_faces); the materials stay known,
    # so both runs share one atlas layout (EngineBudget.shared_atlas) and differ only in the rule
    budget = EngineBudget(engine, keep_contact_faces=not rule)
    b = run_export_pipeline(sm.extract_meshes(), budget, scene.asset_name, opaque_materials=coplanar.opaque_materials(scene))
    m = write_engine_package(sm, b, budget, tmp / "pkg", asset_name=scene.asset_name)
    return sm, b, m


def test_package_ships_the_union_surface_and_the_stage_stays_closed(tmp_path):
    sm, b, m = _package(tmp_path, _scene(STACK))
    authored = sum(len(u.triangles) for u in b.uvs)
    removed = m.data["contact_faces"]["removed_per_part"]
    # base top and lid bottom (exact cover), the post's bottom cap; the lid's top is only partly under the post
    assert removed == {"base": 2, "lid": 2, "post": 16}
    assert m.data["tris"] == authored - 20 and m.data["contact_faces"]["render_meshes_closed"] is False
    assert m.ok, [i for i in m.issues if i.severity == "critical"]
    report = tier1_qa(sm.extract_meshes(), EngineBudget("unreal_5"))  # the working stage, UVs authored: closed
    assert report.ok and not [i for i in report.issues if i.code in ("not_watertight", "non_manifold")]
    # the shipped atlas spends nothing on charts that are hidden whole: every shipped uv lies in [0, 1]
    for u in b.uvs:
        s = shipped(u)
        assert s.uvs.min() >= 0.0 and s.uvs.max() <= 1.0


def test_lod_chains_are_the_triangles_they_were_without_the_rule(tmp_path):
    scene = _scene(STACK)
    _, on, _ = _package(tmp_path / "on", scene)
    _, off, _ = _package(tmp_path / "off", scene, rule=False)
    for n, chain in off.lods.items():
        assert len(chain) == len(on.lods[n])
        for a, b in zip(chain, on.lods[n]):
            assert np.array_equal(a.points, b.points) and np.array_equal(a.triangles, b.triangles)


def test_roblox_ships_closed_meshes(tmp_path):
    _, b, m = _package(tmp_path, _scene(STACK), "roblox_r15")
    assert all(u.hidden is None for u in b.uvs)
    assert m.data["contact_faces"]["engine_requires_closed_meshes"] is True
    assert m.data["contact_faces"]["removed_tris"] == 0


def test_see_through_cover_keeps_the_face_behind_it(tmp_path):
    scene = _scene([_node("a", (0, 0, 0), (1, 1, 0.5)), _node("b", (0, 0, 0.5), (1, 1, 1), "glass")], (WOOD, GLASS))
    _, _, m = _package(tmp_path, scene)
    assert m.data["contact_faces"]["removed_tris"] == 0


def test_same_scene_gives_byte_identical_packages(tmp_path):
    def digest(root: Path) -> dict[str, str]:
        man = json.loads((root / "pkg" / "manifest.json").read_text())
        return {f: hashlib.sha256((root / "pkg" / f).read_bytes()).hexdigest() for f in man["files"]} | {
            "manifest": hashlib.sha256(json.dumps({k: v for k, v in man.items() if k != "versions"}).encode()).hexdigest()}
    _package(tmp_path / "a", _scene(STACK))
    _package(tmp_path / "b", _scene(STACK))
    assert digest(tmp_path / "a") == digest(tmp_path / "b")


def test_cache_round_trip_keeps_the_hidden_masks():
    from antigravity3d.tactics.cached import _bundle_from_bytes, _bundle_to_bytes

    meshes = [_mesh("a", (0, 0, 0), (1, 1, 0.5)), _mesh("b", (0, 0, 0.5), (1, 1, 1), "iron")]
    b = run_export_pipeline(meshes, EngineBudget("unreal_5"), "Prop", opaque_materials={"wood", "iron"})
    r = _bundle_from_bytes(_bundle_to_bytes(b))
    assert [None if u.hidden is None else u.hidden.tolist() for u in r.uvs] == [u.hidden.tolist() for u in b.uvs]
    assert all(np.array_equal(x.hidden, y.hidden) for n in b.lods for x, y in zip(b.lods[n], r.lods[n]) if x.hidden is not None)


@pytest.mark.parametrize("second, fires", [
    (((0.5, 0, 0.5), (1.5, 1, 1)), True),          # overlapping volumes with coplanar tops
    (((0, 0, 0), (1, 1, 1)), True),                # an identical duplicate
    (((0.2, 0.2, 0.9996), (0.8, 0.8, 1.0001)), True),  # a slab 0.1 mm proud of the top
    (((0.2, 0.2, 1.0), (0.8, 0.8, 1.02)), False),  # a plate on top: back to back, not z-fighting
    (((1.0, 0, 0), (2, 1, 1)), False),             # side by side, level tops that only share an edge
    (((0.2, 0.2, 1.0005), (0.8, 0.8, 1.002)), False),  # floating 0.5 mm above, faces opposite
])
def test_tier1_reports_same_facing_coplanar_overlap(second, fires):
    a, b = _mesh("A", (0, 0, 0), (1, 1, 1)), _mesh("B", *second)
    report = tier1_qa([a, b], EngineBudget("unreal_5"))
    got = [i for i in report.issues if i.code == "z_fighting"]
    assert bool(got) is fires
    assert all(i.severity == "warning" for i in got)
