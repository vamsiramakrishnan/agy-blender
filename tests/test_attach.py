"""A part that touches nothing: the kernel's rule is the Blender check's, a part beside another is attached by the
deterministic step settle could not be, and a design the gate would refuse stops after pass A.

Measured live 2026-11-22 (r5/live_scorecard/runs/r5_hydrant_C, tests/data/attach/hydrant_r5_C.json is its design as it
shipped, read back from the run's authoring USD): three chain links of "a cast-iron fire hydrant with bolted caps and a
chained nozzle cap" touched nothing; Tier-1 reported two of them (the third's edge came within 2 mm of its anchor's edge,
with no vertex near, which counted as touching here and not in the Blender check); settle left them ("beside another
part"); five repair rounds and two improvement rounds did not connect them, and the run went on through the look round,
the LOD checks, the export and two engine checks to be refused by the Blender check at its last step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel import attach as A
from antigravity3d.kernel.fast_ops import tier1_qa
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics import pyramid as P

DATA = Path(__file__).resolve().parent / "data" / "attach"
HYDRANT = EngineBudget("godot_4", max_tris=4000)


def _stage(tmp_path: Path, scene: SceneIR) -> StageManager:
    sm = StageManager.create(tmp_path / "s.usda", scene.asset_name)
    sm.apply_ir(scene)
    return sm


def _hydrant(tmp_path: Path) -> StageManager:
    return _stage(tmp_path, SceneIR.model_validate_json((DATA / "hydrant_r5_C.json").read_text()))


def _box(i: str, size: tuple, at: tuple, **kw: Any) -> NodeIR:
    return NodeIR(id=i, kind="box", params={"size": size, **kw}, transform=Transform(translate=at), material_id="m")


def _scene(*nodes: NodeIR) -> SceneIR:
    return SceneIR(asset_name="Prop", materials=[MaterialIR(id="m")], nodes=list(nodes))


def _floating(meshes: list[Any], budget: EngineBudget = HYDRANT) -> list[str]:
    return sorted(P.floating_ids(tier1_qa(meshes, budget)))


# --------------------------------------------------------------------------- the kernel's rule is the gate's


def test_the_hydrant_links_the_blender_check_refused_are_the_ones_tier1_reports(tmp_path: Path) -> None:
    """The Blender check refused three parts at [0.086, -0.125, 0.518], [0.08, -0.143, 0.494], [0.074, -0.161, 0.47]:
    chain_link_1, 2 and 3. Tier-1 now reports the same three, each with the move that would make it touch."""
    t1 = tier1_qa(_hydrant(tmp_path).extract_meshes(), HYDRANT)
    assert sorted(P.floating_ids(t1)) == ["chain_link_1", "chain_link_2", "chain_link_3"]
    link1 = next(i for i in t1.issues if i.node_id == "chain_link_1")
    assert "to touch it, move chain_link_1 by (" in link1.detail and "mm" in link1.detail


@pytest.mark.parametrize("nodes, floating", [
    # measured with a real Blender (scratch_abort/attach/blender_probe.py): all three pass there, the foot is refused
    ([_box("body", (1, 1, 1), (0, 0, 0.5)), _box("lid", (1.04, 1.04, 0.05), (0, 0, 1.025))], []),  # wider lid, flush
    # a full-width skid flush under a bevelled body: the script passed it here and failed its twin in r5_crate_C's file
    # (its top face goes as a hidden contact face; what is left meets the body only along a line): not counted
    ([_box("body", (0.86, 0.5, 0.32), (0, 0, 0.19), bevel=0.01), _box("skid", (0.08, 0.5, 0.03), (0.32, 0, 0.015), bevel=0.004)], ["skid"]),
    ([_box("body", (0.6, 0.4, 0.3), (0, 0, 0.15)), _box("foot", (0.05, 0.05, 0.02), (0.355, 0, 0.015))], ["foot"]),  # 5 mm up
    ([_box("body", (0.6, 0.4, 0.3), (0, 0, 0.15)), _box("cap", (0.1, 0.1, 0.02), (0, 0, 0.314))], ["cap"]),  # 4 mm above
    ([_box("body", (0.6, 0.4, 0.3), (0, 0, 0.15)), _box("cap", (0.1, 0.1, 0.02), (0, 0, 0.3115))], []),  # 1.5 mm above
])
def test_contact_is_what_the_blender_check_counts(tmp_path: Path, nodes: list[NodeIR], floating: list[str]) -> None:
    """A part touches when a vertex of it is within 2 mm of another part or a face of it lies on or crosses another's;
    a part off the ground by more than 2 mm and touching nothing is floating (it was silent here up to 10 mm)."""
    assert _floating(_stage(tmp_path, _scene(*nodes)).extract_meshes()) == floating


# --------------------------------------------------------------------------- attach


def _chain(gap: float) -> SceneIR:
    """A body, an anchor block on its +x face, and three links hanging beside it ``gap`` apart, the first ``gap`` from
    the anchor."""
    nodes = [_box("body", (0.3, 0.3, 0.6), (0, 0, 0.3)), _box("anchor", (0.02, 0.02, 0.02), (0.16, 0, 0.5))]
    x0 = 0.17
    for k in range(3):
        x = x0 + gap + 0.01 + k * (0.02 + gap)
        nodes.append(_box(f"link_{k + 1}", (0.02, 0.008, 0.008), (x, 0, 0.5)))
    return _scene(*nodes)


def test_a_chain_beside_the_body_closes_up_from_its_anchored_end(tmp_path: Path) -> None:
    sm = _stage(tmp_path, _chain(0.006))
    meshes = sm.extract_meshes()
    assert _floating(meshes) == ["link_1", "link_2", "link_3"]
    r = A.attach(meshes, HYDRANT, P.reach_of(meshes))
    assert [a.split(" moved")[0] for a in r.attached] == ["link_1", "link_2", "link_3"] and not r.left
    assert [a.split(" onto ")[1].split(" ")[0] for a in r.attached] == ["anchor", "link_1", "link_2"]
    sm.apply_patch(IRPatch(ops=r.ops(sm.base_ir())))
    assert _floating(sm.extract_meshes()) == []
    assert not [i for i in tier1_qa(sm.extract_meshes(), HYDRANT).issues if i.code == "interpenetration"]


def test_a_part_far_away_is_left_to_the_author_with_its_numbers(tmp_path: Path) -> None:
    sm = _stage(tmp_path, _scene(_box("body", (0.3, 0.3, 0.6), (0, 0, 0.3)), _box("lamp", (0.05, 0.05, 0.05), (0, 0, 0.9))))
    meshes = sm.extract_meshes()
    r = A.attach(meshes, HYDRANT, P.reach_of(meshes))
    assert not r.moves and r.left == ["lamp: no part within 28 mm of it"]  # 3 % of the 0.93 m asset


def test_a_move_that_would_bury_a_part_is_not_made(tmp_path: Path, monkeypatch) -> None:
    """A cap 3 mm above the body is put on it, 0.5 mm short. Made to try 3 mm into the body first (deeper than BURY_MAX,
    1.5 mm), that move is not made and the next one, flush, is."""
    scene = _scene(_box("body", (0.6, 0.4, 0.3), (0, 0, 0.15)), _box("cap", (0.1, 0.1, 0.02), (0, 0, 0.313)))
    meshes = _stage(tmp_path, scene).extract_meshes()
    assert _floating(meshes) == ["cap"]
    assert A.attach(meshes, HYDRANT, P.reach_of(meshes)).attached[0].startswith("cap moved 2.5 mm onto body")
    monkeypatch.setattr(A, "GAP_SHORT", -0.003)  # the first move tried goes 3 mm into the body
    r = A.attach(meshes, HYDRANT, P.reach_of(meshes))
    assert r.attached[0].startswith("cap moved 3.0 mm onto body")


def test_the_hydrant_under_the_rule(tmp_path: Path) -> None:
    """What the deterministic step does on the shipped hydrant: chain_link_1 onto its anchor, chain_link_2 onto the body;
    chain_link_3 is 26.8 mm from the body, past the reach (25 mm: 3 % of the 0.83 m asset), and 16.9-30.8 mm from the links
    beside it, which are themselves loose or move away: left to the author. The design's three gaps between links (17, 17
    and 31 mm) are more than any move within the reach can close."""
    sm = _hydrant(tmp_path)
    meshes = sm.extract_meshes()
    assert P.reach_of(meshes) == pytest.approx(0.0249, abs=1e-3)
    r = A.attach(meshes, HYDRANT, P.reach_of(meshes))
    assert [a.split(" moved")[0] for a in r.attached] == ["chain_link_1", "chain_link_2"]
    assert r.left == ["chain_link_3: no part within 25 mm of it"]
    sm.apply_patch(IRPatch(ops=r.ops(sm.base_ir())))
    assert _floating(sm.extract_meshes()) == ["chain_link_3"]


# --------------------------------------------------------------------------- the run


async def test_a_design_the_gate_would_refuse_stops_after_pass_a(tmp_path: Path) -> None:
    """No look round, no package, no engine checks: the refusal names the part with its numbers."""
    from test_pyramid import Script, setup

    from antigravity3d.tactics.authoring import CallableAuthor
    from antigravity3d.tactics.governor import Governor, plan_for
    from antigravity3d.tactics.pyramid import refine

    async def up(b: bytes) -> str:
        return "fake://x"

    scene = _scene(_box("body", (0.3, 0.3, 0.6), (0, 0, 0.3)), _box("lamp", (0.05, 0.05, 0.05), (0, 0, 0.9)))
    sm, plane = setup(tmp_path, Script(), scene)
    res = await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(2)),
                       budget=HYDRANT, uploader=up, workdir=tmp_path / "wd", settle_first=False, max_rounds=1)
    assert res.stopped["C"].startswith("not packaged: the Blender check would refuse it: 1 part(s) touch nothing after pass A: lamp (")
    steps = [s.name for s in res.steps]
    assert "detached_parts" in steps and "uv_lod_colliders" not in steps and "look_round" not in steps
    assert "package" not in res.objects and not res.ok


async def test_a_part_off_the_ground_beside_the_body_is_put_on_the_body(tmp_path: Path) -> None:
    """A foot 5 mm up and 10 mm beside the body, touching nothing: the gate refuses it wherever it is (on the ground too),
    so the clean-up does not settle it onto the floor, where it would still touch nothing, but attaches it to the body."""
    from test_pyramid import Script, run_a, setup

    scene = _scene(_box("body", (0.6, 0.4, 0.3), (0, 0, 0.15)), _box("foot", (0.05, 0.05, 0.02), (0.335, 0, 0.015)))
    sm, plane = setup(tmp_path, Script(), scene)
    assert _floating(sm.extract_meshes()) == ["foot"]
    from antigravity3d.tactics.authoring import CallableAuthor

    res = await run_a(sm, plane, CallableAuthor(lambda *a, **k: []), tmp_path, settle_first=True, max_rounds=1)
    assert _floating(sm.extract_meshes()) == []
    assert "attached foot moved" in next(s.detail for s in res.steps if s.name == "attach_pre")


# --------------------------------------------------------------------------- with a real Blender


@pytest.mark.blender
async def test_the_blender_check_agrees_with_the_kernel_after_attach(tmp_path: Path) -> None:
    """The closed chain passes the real Blender check; the hydrant after attach is refused for chain_link_3 alone, the
    one part Tier-1 still reports."""
    from antigravity3d.adapters.blender_5_3 import upstream_checks
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool, find_blender
    from antigravity3d.kernel import fast_ops
    from antigravity3d.kernel.package import write_engine_package

    if not find_blender():
        pytest.skip("no Blender binary ($BLENDER_BIN or PATH)")

    async def check(sm: StageManager, sub: str) -> tuple[str, list[str]]:
        sm.commit_all()
        meshes = sm.extract_meshes()
        name = sm.base_ir().asset_name
        pkg = write_engine_package(sm, fast_ops.run_export_pipeline(meshes, HYDRANT, name), HYDRANT, tmp_path / sub,
                                   asset_name=name, qa={"tier1_ok": True})
        status, _detail, data = await upstream_checks.asset_qa(pool, str(pkg.out_dir / f"{name}.glb"), "godot_4")
        return status, data.get("errors") or []

    async with BlenderPool(1, startup_timeout=180) as pool:
        for sub, sm in (("chain", _stage(tmp_path / "c", _chain(0.006))), ("hydrant", _hydrant(tmp_path / "h"))):
            meshes = sm.extract_meshes()
            r = A.attach(meshes, HYDRANT, P.reach_of(meshes))
            sm.apply_patch(IRPatch(ops=r.ops(sm.base_ir())))
            status, errors = await check(sm, sub)
            if sub == "chain":
                assert status == "passed", errors
            else:  # chain_link_3 sits at [0.074, -0.161, 0.47]
                assert status == "failed" and len(errors) == 1 and "[0.074, -0.161, 0.47]" in errors[0], errors
