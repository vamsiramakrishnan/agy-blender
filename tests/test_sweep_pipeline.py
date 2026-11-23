"""Swept rods use the existing USD, UV, LOD, collision and package gates without exceptions or model calls."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import trimesh

from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, SetParam, Transform
from antigravity3d.kernel import fast_ops
from antigravity3d.kernel.compile_ir import primitive_mesh, triangle_count
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import _is_convex, validate_package
from antigravity3d.state import EngineBudget


def rail_scene() -> SceneIR:
    """A hand-authored arch fixture, not a generated chair or evidence of an aesthetic benchmark win."""
    # Enough samples for the unchanged screen-space LOD gate to remove geometry without changing the outline.
    path = [(float(0.4 * np.cos(a)), 0.0, float(0.4 * np.sin(a))) for a in np.linspace(0, np.pi, 33)]
    rail = NodeIR(id="rail", kind="sweep", params={"path": path, "radius": 0.025, "segments": 12}, material_id="wood")
    low = float(primitive_mesh(rail)[0][:, 2].min())
    rail = rail.model_copy(update={"transform": Transform(translate=(0, 0, -low))})
    return SceneIR(asset_name="SweptRail", nodes=[rail],
                   materials=[MaterialIR(id="wood", base_color=(0.2, 0.08, 0.025), roughness=0.65)])


def test_sweep_usd_round_trip_and_path_edit_are_local_and_reversible(tmp_path: Path) -> None:
    scene = rail_scene()
    scene.nodes.append(NodeIR(id="untouched", kind="box", params={"size": (0.1, 0.1, 0.1)},
                              transform=Transform(translate=(2, 0, 0.05)), material_id="wood"))
    sm = StageManager.create(tmp_path / "work.usda", scene.asset_name)
    sm.apply_ir(scene)
    sm.commit_all()
    sm = StageManager.open(tmp_path / "work.usda")
    assert sm.scene_ir() == scene
    before = {m.node_id: m for m in sm.extract_meshes()}
    assert before["rail"].uv_charts is not None
    assert before["rail"].collider_points is None  # the concave rod uses the existing decomposition, never a filled arch proxy
    path = [(x, 0.03 * (1 - (x / 0.4) ** 2), z) for x, _, z in scene.nodes[0].params["path"]]
    sm.apply_patch(IRPatch(ops=[SetParam(node_id="rail", name="path", value=path)]))
    after = {m.node_id: m for m in sm.extract_meshes()}
    assert before["rail"].points.tobytes() != after["rail"].points.tobytes()
    for field in ("points", "triangles", "uv_charts"):
        assert getattr(before["untouched"], field).tobytes() == getattr(after["untouched"], field).tobytes()
    sm.rollback()
    assert sm.scene_ir() == scene
    assert sm.extract_meshes()[0].points.tobytes() == before["rail"].points.tobytes()


def test_sweep_uses_existing_manifold_winding_and_budget_gates(tmp_path: Path) -> None:
    scene = rail_scene()
    sm = StageManager.create(tmp_path / "work.usda", scene.asset_name)
    sm.apply_ir(scene)
    (mesh,) = sm.extract_meshes()
    budget = EngineBudget("godot_4", max_tris=5000, require_manifold=True)
    assert fast_ops.tier1_qa([mesh], budget).ok
    opened = replace(mesh, triangles=mesh.triangles[:-1], uv_charts=None)
    inverted = replace(mesh, triangles=mesh.triangles[:, [0, 2, 1]])
    for damaged in (opened, inverted):
        rep = fast_ops.tier1_qa([damaged], budget)
        assert not rep.ok and any(i.severity == "critical" for i in rep.issues), rep.one_line()
    rep = fast_ops.tier1_qa([mesh], replace(budget, max_tris=triangle_count(scene.nodes[0]) - 1))
    assert any(i.code == "tri_budget" and i.severity == "critical" for i in rep.issues)


@pytest.mark.parametrize("engine", ["godot_4", "roblox_r15"])
def test_sweep_exports_valid_uv_lods_colliders_and_a_cold_checked_package(tmp_path: Path, engine: str) -> None:
    scene = rail_scene()
    sm = StageManager.create(tmp_path / "work.usda", scene.asset_name)
    sm.apply_ir(scene)
    budget = EngineBudget(engine, max_tris=5000, require_manifold=True)
    bundle = fast_ops.run_export_pipeline(sm.extract_meshes(), budget, scene.asset_name)
    for uv in bundle.uvs:
        assert uv.chart_source == "analytic" and uv.fitted and uv.within(budget)
        assert uv.stretch_mean < 1e-5 and uv.angle_distortion < 1e-5
        assert uv.texel_density == pytest.approx(budget.texel_density_px_per_m)
    levels = bundle.lods["rail"]
    counts = [fast_ops.shipped(level).tri_count for level in levels]
    assert len(counts) >= 2 and all(a > b for a, b in zip(counts, counts[1:])), counts
    assert 0 < len(bundle.colliders) <= budget.max_collision_hulls
    for hull in bundle.colliders:
        assert len(hull.points) <= 255 and _is_convex(hull.points.astype(np.float32).astype(np.float64), hull.triangles)
    pm = write_engine_package(sm, bundle, budget, tmp_path / "package", asset_name=scene.asset_name)
    assert not validate_package(pm.out_dir)
    exported = trimesh.load(pm.out_dir / f"{scene.asset_name}.glb")
    assert sum(len(g.faces) for g in exported.geometry.values()) == triangle_count(scene.nodes[0])
    for geometry in exported.geometry.values():
        welded = geometry.copy()
        welded.merge_vertices(merge_tex=True, merge_norm=True)  # UV and normal seams do not open the geometric surface
        assert welded.is_watertight and welded.is_winding_consistent and welded.volume > 0
