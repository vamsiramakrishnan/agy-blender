from __future__ import annotations

import statistics
import time
from pathlib import Path

import numpy as np
import pytest
from pxr import Usd

from antigravity3d.ir import (
    IRPatch,
    MaterialIR,
    NodeIR,
    RemoveNode,
    SceneIR,
    SetParam,
    SetTransform,
    Transform,
    UpsertNode,
)
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager

ASSET = "Crate"


def _scene() -> SceneIR:
    return SceneIR(
        asset_name=ASSET,
        materials=[MaterialIR(id="wood", base_color=(0.6, 0.4, 0.2))],
        nodes=[
            NodeIR(id="root", kind="group", transform=Transform(translate=(10, 0, 0))),
            NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, parent="root", material_id="wood"),
            NodeIR(
                id="leg",
                kind="cylinder",
                params={"radius": 0.1, "height": 0.5},
                parent="root",
                transform=Transform(translate=(0, 0, -0.75), scale=(2, 2, 1)),
                collider=False,
            ),
        ],
    )


@pytest.fixture
def sm(tmp_path: Path) -> StageManager:
    m = StageManager.create(tmp_path / "crate.usda", ASSET)
    m.apply_ir(_scene())
    return m


def _active(sm: StageManager) -> set[str]:
    return {str(p.GetPath()) for p in Usd.PrimRange(sm.asset_prim)}


def test_create_and_round_trip(tmp_path: Path) -> None:
    m = StageManager.create(tmp_path / "a.usda", ASSET)
    assert m.scene_ir() == SceneIR(asset_name=ASSET)
    assert m.stage.GetDefaultPrim().GetName() == ASSET
    tid = m.apply_ir(_scene(), tx_id="t1")
    assert tid == "t1" and m.tx_ids == ["t1"]
    assert m.tx_stack[0].endswith("tx_t1.usda") or "tx_t1" in m.tx_stack[0]
    assert m.scene_ir() == _scene()
    assert {"/Crate/root/body", "/Crate/root/leg", "/Crate/Looks/wood"} <= _active(m)
    with pytest.raises(ValueError):
        m.apply_ir(SceneIR(asset_name="Other"))


def test_patch_then_rollback_restores(sm: StageManager) -> None:
    before_ir, before_prims = sm.scene_ir(), _active(sm)
    before_pts = {d.node_id: d.points for d in sm.extract_meshes()}
    sm.apply_patch(
        IRPatch(
            ops=[
                SetParam(node_id="body", name="size", value=(3.0, 3.0, 3.0)),
                UpsertNode(node=NodeIR(id="lid", kind="sphere", params={"radius": 0.4})),
            ]
        )
    )
    assert "/Crate/lid" in _active(sm) and sm.scene_ir() != before_ir
    sm.rollback()
    assert sm.scene_ir() == before_ir and _active(sm) == before_prims
    after = {d.node_id: d.points for d in sm.extract_meshes()}
    assert after.keys() == before_pts.keys()
    assert all(np.allclose(after[k], before_pts[k]) for k in after)


def test_nested_rollback(sm: StageManager) -> None:
    ir0 = sm.scene_ir()
    t1 = sm.apply_patch(IRPatch(ops=[SetTransform(node_id="root", transform=Transform(translate=(1, 1, 1)))]))
    ir1 = sm.scene_ir()
    sm.apply_patch(IRPatch(ops=[UpsertNode(node=NodeIR(id="a", kind="box", params={"size": (1, 1, 1)}))]))
    sm.apply_patch(IRPatch(ops=[UpsertNode(node=NodeIR(id="b", kind="box", params={"size": (1, 1, 1)}))]))
    assert len(sm.tx_stack) == 4
    sm.rollback()
    assert {n.id for n in sm.scene_ir().nodes} == {"root", "body", "leg", "a"}
    t2 = sm.tx_ids[-1]
    sm.rollback(t2)
    assert sm.scene_ir() == ir1
    t3 = sm.apply_patch(IRPatch(ops=[UpsertNode(node=NodeIR(id="c", kind="box", params={"size": (1, 1, 1)}))]))
    assert sm.rollback(t1) == [t1, t3]  # non-top rollback drops everything above too
    assert sm.scene_ir() == ir0 and len(sm.tx_stack) == 1
    assert sm.stage.GetEditTarget().GetLayer().identifier == sm.tx_stack[-1]
    assert list(sm.stage.GetSessionLayer().subLayerPaths) == sm.tx_stack


def test_removed_node_disappears_then_reappears(sm: StageManager) -> None:
    sm.apply_patch(IRPatch(ops=[RemoveNode(node_id="leg")]))
    leg = sm.stage.GetPrimAtPath("/Crate/root/leg")
    assert not leg.IsActive() and "/Crate/root/leg" not in _active(sm)
    assert "leg" not in {d.node_id for d in sm.extract_meshes()}
    assert "leg" not in sm.manifest()
    sm.rollback()
    assert sm.stage.GetPrimAtPath("/Crate/root/leg").IsActive()
    assert "leg" in {d.node_id for d in sm.extract_meshes()}


def test_readd_after_remove_in_later_tx(sm: StageManager) -> None:
    leg = next(n for n in _scene().nodes if n.id == "leg")
    sm.apply_patch(IRPatch(ops=[RemoveNode(node_id="leg")]))
    sm.apply_patch(IRPatch(ops=[UpsertNode(node=leg)]))
    assert sm.stage.GetPrimAtPath("/Crate/root/leg").IsActive()
    assert sm.scene_ir() == _scene()


def test_transaction_context_rolls_back_on_exception(sm: StageManager) -> None:
    before = sm.scene_ir()
    depth = len(sm.tx_stack)
    with pytest.raises(RuntimeError, match="boom"), sm.transaction():
        sm.apply_patch(IRPatch(ops=[RemoveNode(node_id="leg")]))  # nested tx above ours
        raise RuntimeError("boom")
    assert len(sm.tx_stack) == depth and sm.scene_ir() == before


def test_failed_patch_rolls_back(sm: StageManager) -> None:
    depth = len(sm.tx_stack)
    with pytest.raises(ValueError, match="still has children"):
        sm.apply_patch(IRPatch(ops=[RemoveNode(node_id="root")]))
    bad = NodeIR(id="bad", kind="sphere", params={"radius": -1.0})
    with pytest.raises(ValueError, match="^bad: "):
        sm.apply_patch(IRPatch(ops=[UpsertNode(node=bad)]))
    assert len(sm.tx_stack) == depth and "bad" not in {n.id for n in sm.scene_ir().nodes}
    assert not sm.stage.GetPrimAtPath("/Crate/bad")


def test_variants_add_select_promote(sm: StageManager) -> None:
    base = sm.scene_ir()
    nodes = [n for n in base.nodes if n.id != "leg"]
    nodes[1] = nodes[1].model_copy(update={"params": {"size": (2.0, 1.0, 1.0)}})
    tall = base.model_copy(update={"nodes": [*nodes, NodeIR(id="lid", kind="sphere", params={"radius": 0.4})]})
    sm.add_variant("shape", "tall", tall)
    sm.add_variant("shape", "same", base)
    assert sm.list_variants("shape") == ["same", "tall"] and sm.list_variants("nope") == []
    assert sm.scene_ir() == base  # nothing selected yet

    sm.select_variant("shape", "tall")
    assert sm.scene_ir() == tall
    assert "/Crate/lid" in _active(sm) and "/Crate/root/leg" not in _active(sm)
    ext = {d.node_id: d.points for d in sm.extract_meshes()}
    assert np.ptp(ext["body"][:, 0]) == pytest.approx(2.0)  # variant overrides a base node
    with pytest.raises(RuntimeError, match="variant selected"):
        sm.apply_ir(base)

    sm.select_variant("shape", "same")
    assert sm.scene_ir() == base and "/Crate/lid" not in _active(sm)

    sm.promote_variant("shape", "tall")
    assert sm.scene_ir() == tall and sm.base_ir() == tall
    assert sm.list_variants("shape") == []
    assert "/Crate/lid" in _active(sm) and "/Crate/root/leg" not in _active(sm)
    sm.apply_patch(IRPatch(ops=[RemoveNode(node_id="lid")]))  # base is editable again
    assert "/Crate/lid" not in _active(sm)


def test_variant_holds_only_delta(sm: StageManager) -> None:
    base = sm.scene_ir()
    lid = NodeIR(id="lid", kind="sphere", params={"radius": 0.4})
    sm.add_variant("opt", "lid", base.model_copy(update={"nodes": [*base.nodes, lid]}))
    layer = sm.stage.GetEditTarget().GetLayer()
    vspec = layer.GetPrimAtPath("/Crate{opt=lid}")
    assert vspec is not None and set(vspec.nameChildren.keys()) == {"lid"}


def test_world_space_extraction(sm: StageManager) -> None:
    meshes = {d.node_id: d for d in sm.extract_meshes()}
    assert set(meshes) == {"body", "leg"}
    body, leg = meshes["body"], meshes["leg"]
    assert isinstance(body, MeshData) and body.points.dtype == np.float64 and body.triangles.dtype == np.int32
    assert np.allclose(body.points.min(0), (9.5, -0.5, -0.5)) and np.allclose(body.points.max(0), (10.5, 0.5, 0.5))
    # leg: parent translate (10,0,0) + own translate (0,0,-0.75) and xy scale 2 on radius 0.1
    assert np.allclose(leg.points.min(0), (9.8, -0.2, -1.0), atol=1e-6)
    assert np.allclose(leg.points.max(0), (10.2, 0.2, -0.5), atol=1e-6)
    assert body.material_id == "wood" and leg.material_id is None
    assert body.collider and not leg.collider
    assert body.prim_path == "/Crate/root/body"


def test_manifest(sm: StageManager) -> None:
    text = sm.manifest()
    lines = text.splitlines()
    assert lines[0].startswith("# Crate nodes=3 mats=1 tris=108")
    assert "body box [9.5,-0.5,-0.5]..[10.5,0.5,0.5] tris=12 mat=wood" in lines
    assert len(lines) == 4 and len(text) < 400


@pytest.mark.parametrize("ext", [".usda", ".usdc", ".usdz"])
def test_export_and_reopen(sm: StageManager, tmp_path: Path, ext: str) -> None:
    out = tmp_path / f"out{ext}"
    sm.export(out)
    plain = Usd.Stage.Open(str(out))
    assert plain.GetDefaultPrim().GetName() == ASSET
    assert not plain.GetPrimAtPath("/__agy_base")
    assert plain.GetPrimAtPath("/Crate/root/body").GetTypeName() == "Mesh"
    reopened = StageManager.open(out)
    assert reopened.scene_ir() == _scene()
    assert {d.node_id for d in reopened.extract_meshes()} == {"body", "leg"}


def test_commit_and_flatten_reopen(sm: StageManager, tmp_path: Path) -> None:
    sm.apply_patch(IRPatch(ops=[RemoveNode(node_id="leg")]))
    expected = sm.scene_ir()
    snapshot = tmp_path / "snap.usda"
    sm.flatten_to(snapshot)
    assert StageManager.open(snapshot).scene_ir() == expected
    sm.commit_all()
    assert sm.tx_stack == [] and sm.scene_ir() == expected
    path = Path(sm.stage.GetRootLayer().realPath)
    reopened = StageManager.open(path)
    assert reopened.scene_ir() == expected
    assert not reopened.stage.GetPrimAtPath("/Crate/root/leg").IsActive()
    with pytest.raises(RuntimeError):
        reopened.rollback()


def test_rollback_latency(tmp_path: Path) -> None:
    nodes = [NodeIR(id="grp", kind="group")] + [
        NodeIR(
            id=f"n{i}",
            kind=("box", "cylinder", "sphere", "cone", "capsule")[i % 5],
            params={"size": (1, 1, 1)} if i % 5 == 0 else {"radius": 0.2, "height": 0.5},
            parent="grp" if i % 2 else None,
            transform=Transform(translate=(i, 0, 0)),
        )
        for i in range(49)
    ]
    m = StageManager.create(tmp_path / "big.usda", ASSET)
    m.apply_ir(SceneIR(asset_name=ASSET, nodes=nodes))
    base = m.scene_ir()
    edited = SceneIR(
        asset_name=ASSET,
        nodes=[
            n.model_copy(update={"transform": Transform(translate=(0, n.transform.translate[0], 1))}) for n in nodes
        ],
    )
    times = []
    for _ in range(15):
        m.apply_ir(edited)
        t0 = time.perf_counter()
        m.rollback()
        times.append((time.perf_counter() - t0) * 1e3)
        assert m.scene_ir() == base
    median = statistics.median(times)
    print(f"\nmedian rollback of a 50-node edit: {median:.3f} ms (min {min(times):.3f}, max {max(times):.3f})")
    assert median < 5.0


def test_add_variant_requires_other_sets_unselected(sm: StageManager) -> None:
    sm.add_variant("a", "x", sm.scene_ir())
    sm.select_variant("a", "x")
    with pytest.raises(RuntimeError, match="variant selected"):
        sm.add_variant("b", "y", sm.scene_ir())
    sm.add_variant("a", "z", sm.scene_ir())  # same set is fine
    assert sm.list_variants("a") == ["x", "z"]
