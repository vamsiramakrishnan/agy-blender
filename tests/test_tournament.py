from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, SetParam
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics import tournament as T
from antigravity3d.tactics.authoring import CallableAuthor, TemplateAuthor
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.tournament import Candidate, geometry_tournament, ortho_tournament, pbr_tournament, run_tournament


def plane_of(**kw) -> tuple[DecisionControlPlane, FakeDecisionsClient]:
    c = FakeDecisionsClient(**kw)
    return DecisionControlPlane(c), c


def tag_calls(p: DecisionControlPlane, tag: str) -> int:
    return p.stats.by_tag.get(tag, 0)


async def test_generations_really_run_concurrently() -> None:
    plane, client = plane_of(latency_s=0.05)
    running = peak = 0

    async def gen(i: int) -> Candidate:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1
        return Candidate(i, f"fake://c/{i}")

    r = await run_tournament("ortho", gen, 4, plane=plane, governor=Governor(plan_for(2)))
    assert peak == 4 == r.max_in_flight
    assert len(client.calls) == 1 and client.calls[0]["n_images"] == 4
    assert r.wall_ms < 0.1 * 4 * 1000 * 0.75  # well under sequential time
    assert len(r.ranking) == 4 and r.winner is not None


async def test_concurrency_bound_is_respected() -> None:
    plane, _ = plane_of()

    async def gen(i: int) -> Candidate:
        await asyncio.sleep(0.02)
        return Candidate(i, f"fake://c/{i}")

    r = await run_tournament("ortho", gen, 4, plane=plane, governor=Governor(plan_for(2)), concurrency=2)
    assert r.max_in_flight == 2


async def test_failed_generation_is_reported_and_failed_batch_cannot_promote() -> None:
    plane, _ = plane_of(errors=[ValueError("grader exploded")])

    async def gen(i: int) -> Candidate:
        if i == 1:
            raise RuntimeError("generation exploded")
        return Candidate(i, f"fake://c/{i}")

    r = await run_tournament("ortho", gen, 4, plane=plane, governor=Governor(plan_for(2)))
    assert "RuntimeError" in r.failures[1]
    assert set(r.failures) == {0, 1, 2, 3}
    assert all("grader exploded" in r.failures[i] for i in (0, 2, 3))
    assert r.winner is None and not r.ranking


async def test_governor_reduces_n_under_a_tight_ceiling() -> None:
    plane, _ = plane_of()
    forge_backend = FakeImageBackend()
    gov = Governor(plan_for(2))  # standard: N=4, ceiling 0.85
    gov.charge("earlier", 0.79)  # 0.06 remains for one nominal 2K candidate and its grade
    r = await ortho_tournament("crate", ImageForge(forge_backend), plane=plane, governor=gov)
    assert r.n_planned == 1 and len(forge_backend.calls) == 1
    roomy = Governor(plan_for(2))
    r2 = await ortho_tournament("crate", ImageForge(FakeImageBackend()), plane=plane, governor=roomy)
    assert r2.n_planned == 4
    assert roomy.spent == pytest.approx(r2.cost_usd) and roomy.spent == 0  # fake judge and image backend


async def test_early_stop_cancels_pending_candidates() -> None:
    plane, _ = plane_of(latency_s=0.01)
    started: list[int] = []

    async def gen(i: int) -> Candidate:
        started.append(i)
        await asyncio.sleep(0.05)
        return Candidate(i, f"fake://c/{i}")

    r = await run_tournament("ortho", gen, 4, plane=plane, governor=Governor(plan_for(3)), concurrency=1, early_stop=0.5)
    assert r.winner is not None and r.winner.index == 0
    assert r.cancelled == [1, 2, 3] and len(started) <= 2  # queued candidates never generated, the in-flight one was cancelled
    assert len(r.ranking) == 1


async def test_pbr_derives_maps_only_for_the_winner_and_gate_disqualifies(monkeypatch: pytest.MonkeyPatch) -> None:
    derived: list[int] = []
    real = T.derive_pbr
    monkeypatch.setattr(T, "derive_pbr", lambda img, **kw: (derived.append(1), real(img, **kw))[1])
    plane, client = plane_of(answers={"policy_legible_text__2": 0.9})
    r = await pbr_tournament("oak planks", ImageForge(FakeImageBackend()), plane=plane, governor=Governor(plan_for(2)), material_id="wood", size=128)
    assert len(derived) == 1  # ORM + normal only for the winner
    assert r.maps is not None and r.albedo_png
    assert len(r.tournament.disqualified) == 1 and "policy gate" in next(iter(r.tournament.disqualified.values()))
    assert tag_calls(plane, "bon:pbr") == 1 and len(client.calls) == 1
    assert "albedo prep" in r.prep


def crate(n_seg: int | None = None, name: str = "Prop") -> SceneIR:
    nodes = [NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, material_id="wood")]
    nodes[0] = nodes[0].model_copy(update={"transform": nodes[0].transform.model_copy(update={"translate": (0, 0, 0.5)})})
    if n_seg:
        nodes.append(NodeIR(id="drum", kind="cylinder", params={"radius": 0.3, "height": 0.5, "segments": n_seg}, material_id="wood",
                            transform=nodes[0].transform.model_copy(update={"translate": (3, 0, 0.25)})))
    return SceneIR(asset_name=name, nodes=nodes, materials=[MaterialIR(id="wood")])


async def test_geometry_tier1_failures_never_reach_vision_and_winner_is_promoted(tmp_path: Path) -> None:
    plane, _ = plane_of()
    sm = StageManager.create(tmp_path / "p.usda", "Prop")
    scenes = [crate(64), crate(), crate(), crate(64)]  # 64-seg drum = 256 tris: over the 200-tri budget
    budget = EngineBudget("unreal_5", max_tris=200)
    uploads: list[int] = []

    async def up(data: bytes) -> str:
        uploads.append(len(data))
        return f"fake://sheet/{len(uploads)}"

    r = await geometry_tournament(CallableAuthor(lambda *a, **k: scenes), sm, "prop", plane=plane, governor=Governor(plan_for(2)), budget=budget,
                                  uploader=up, scenes=scenes)
    assert sorted(r.disqualified) == [0, 3]
    assert all("tri" in v.lower() for v in r.disqualified.values())
    assert tag_calls(plane, "bon:geometry") == 1 and len(uploads) == 2  # only the survivors cost a vision call / sheet
    assert r.winner is not None and r.winner.index in (1, 2)
    # promoted: the variant set is gone, base scene is the winner, and the stage is editable again
    assert sm.list_variants("blockout") == []
    assert sm.base_ir() == scenes[r.winner.index]
    sm.apply_patch(IRPatch(ops=[SetParam(node_id="body", name="size", value=(2.0, 1.0, 1.0))]))
    assert sm.base_ir().nodes[0].params["size"] == (2.0, 1.0, 1.0)


async def test_geometry_all_disqualified_promotes_least_bad_for_repair(tmp_path: Path) -> None:
    plane, _ = plane_of()
    sm = StageManager.create(tmp_path / "p.usda", "Prop")
    scenes = [crate(64), crate(96)]
    r = await geometry_tournament(CallableAuthor(lambda *a, **k: scenes), sm, "prop", plane=plane, governor=Governor(plan_for(2)),
                                  budget=EngineBudget("unreal_5", max_tris=50), uploader=lambda b: asyncio.sleep(0, "fake://x"), scenes=scenes)
    assert len(r.disqualified) == 2 and "fallback_promoted" in r.notes
    assert tag_calls(plane, "bon:geometry") == 0
    assert len(sm.base_ir().nodes) == 2  # something to repair, not an empty stage


async def test_geometry_candidate_failure_is_isolated(tmp_path: Path) -> None:
    plane, _ = plane_of()
    sm = StageManager.create(tmp_path / "p.usda", "Prop")
    scenes = [crate(name="WrongName"), crate(), crate()]  # asset_name mismatch: add_variant raises for candidate 0
    r = await geometry_tournament(CallableAuthor(lambda *a, **k: scenes), sm, "prop", plane=plane, governor=Governor(plan_for(2)),
                                  budget=EngineBudget("unreal_5"), uploader=lambda b: asyncio.sleep(0, "fake://x"), scenes=scenes)
    assert 0 in r.failures and r.winner is not None and r.winner.index in (1, 2)


async def test_geometry_with_template_author_end_to_end(tmp_path: Path) -> None:
    plane, _ = plane_of()
    sm = StageManager.create(tmp_path / "b.usda", "Barrel")
    gov = Governor(plan_for(2))
    r = await geometry_tournament(TemplateAuthor(), sm, "oak barrel with hoops", plane=plane, governor=gov, budget=EngineBudget("unreal_5"),
                                  uploader=lambda b: asyncio.sleep(0, "fake://x"), context={"asset_name": "Barrel"})
    assert r.n_planned == 4 and len(r.ranking) == 4
    assert {n.id for n in sm.base_ir().nodes} >= {"body", "hoop_0"}
    assert gov.by_category["geometry_vision"] == 0  # fake judge
