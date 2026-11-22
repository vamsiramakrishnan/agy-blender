from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from antigravity3d.adapters.blender_5_3.pool import BlenderPool
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.decisions.questions import MARKED_DEFECT_PHRASES
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.genmedia.pbr_deriver import derive_pbr
from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, SetTransform, Transform
from antigravity3d.kernel.fast_ops import tier1_qa
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import CallableAuthor
from antigravity3d.tactics.distill import FixLog
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.pyramid import refine
from stream_fakes import StreamFromBlocking  # noqa: E402 - tests/ is on sys.path (rootdir conftest layout)

BUDGET = EngineBudget("unreal_5")


def floating_scene() -> SceneIR:
    return SceneIR(asset_name="Prop", materials=[MaterialIR(id="wood")], nodes=[
        NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, transform=Transform(translate=(0, 0, 0.5)), material_id="wood"),
        NodeIR(id="lid", kind="box", params={"size": (1.04, 1.04, 0.05)}, transform=Transform(translate=(0, 0, 1.325)), material_id="wood")])


LID_DOWN = IRPatch(ops=[SetTransform(node_id="lid", transform=Transform(translate=(0, 0, 1.025)))])


def sound_scene() -> SceneIR:
    """The same prop with its lid down: nothing floats. A run that is to reach pass C needs one since 2026-11-22: a part
    that touches nothing after pass A stops the run there (pyramid._Run.detached_refusal)."""
    s = floating_scene()
    s.nodes[1].transform = Transform(translate=(0, 0, 1.025))
    return s


class Script:
    """Programmable FakeDecisionsClient responder keyed by call kind (blockout QA / lookdev QA / locate)."""

    def __init__(self, blockout: Callable[[int], dict] = lambda n: {}, lookdev: Callable[[int], dict] = lambda n: {}, locate: float = 0.9) -> None:
        self.blockout, self.lookdev, self.locate, self.n = blockout, lookdev, locate, {"blockout": 0, "lookdev": 0}

    def __call__(self, req: dict) -> dict:
        qs = req["questions"]
        if any("__" in q for q in qs):  # set-of-mark localisation (LOD-pop items share the "__" syntax but are not marked defects)
            return {q: self.locate for q in qs if q.split("__")[0] in MARKED_DEFECT_PHRASES}
        if "score_silhouette" not in qs:
            return {}
        kind = "lookdev" if "defect_cg_plastic" in qs else "blockout"
        self.n[kind] += 1
        return (self.blockout if kind == "blockout" else self.lookdev)(self.n[kind])


def setup(tmp_path: Path, script: Script, scene: SceneIR | None = None) -> tuple[StageManager, DecisionControlPlane]:
    sm = StageManager.create(tmp_path / "p.usda", "Prop")
    sm.apply_ir(scene or floating_scene())
    return sm, DecisionControlPlane(FakeDecisionsClient(responder=script))


def maps() -> Any:
    rng = np.random.default_rng(3)
    return derive_pbr(Image.fromarray(rng.integers(60, 200, (64, 64, 3), dtype=np.uint8)))


async def up(data: bytes) -> str:
    return "fake://sheet"


async def run_a(sm: StageManager, plane: Any, author: CallableAuthor, tmp_path: Path, **kw: Any):
    kw = {"settle_first": False, "run_pass_b": False, "run_pass_c": False, **kw}
    return await refine(sm, plane=plane, author=author, governor=kw.pop("governor", Governor(plan_for(2))), budget=BUDGET, uploader=up,
                        workdir=tmp_path / "wd", **kw)


FLAGGED = {"defect_floating": 0.9, "defect_clipping": 0.9}  # two high defects: entangled, so agent_repair (not a deterministic fixer)


async def test_converges_when_the_repair_fixes_the_flagged_floating(tmp_path: Path) -> None:
    state: dict[str, Any] = {}

    def repair(scene: SceneIR, *, action: Any, located: dict, manifest: str) -> IRPatch:
        state.update(located=located, manifest=manifest, action=action)
        state["fixed"] = True
        return LID_DOWN

    script = Script(blockout=lambda n: {} if state.get("fixed") else dict(FLAGGED))
    sm, plane = setup(tmp_path, script)
    author = CallableAuthor(lambda *a, **k: [], repair)
    log = FixLog(tmp_path / "fix.jsonl")
    res = await run_a(sm, plane, author, tmp_path, fixlog=log)

    assert [r.action for r in res.rounds] == ["agent_repair", "approve"]
    assert res.converged["A"] and "converged in 2" in res.stopped["A"]
    assert state["action"].kind == "agent_repair" and "lid" in state["located"]["defect_floating"] and "lid" in state["located"]["tier1:floating_part"]
    assert "lid" in state["manifest"]
    assert not [i for i in tier1_qa(sm.extract_meshes(), BUDGET).issues if i.code == "floating_part"]
    assert sm.base_ir().nodes[1].transform.translate == (0, 0, 1.025)
    (rec,) = res.fix_records  # the improving repair is logged
    assert rec.score_after > rec.score_before and rec.ops == ["set_transform"] and rec.params_changed == ["lid.translate"]
    assert len(log.read()) == 1
    assert len(res.audit) == 2 and res.audit[0].startswith("A1 QA[blockout] agent_repair")
    assert res.tier15 > 3.5 and res.tier1_ok and res.author_calls == 1


async def test_regressing_repair_is_rolled_back_and_iteration_stops(tmp_path: Path) -> None:
    worse = {"defect_floating": 0.95, "defect_clipping": 0.95, "defect_inverted_faces": 0.95, "score_silhouette": 1.0}
    script = Script(blockout=lambda n: dict(FLAGGED) if n == 1 else dict(worse))
    sm, plane = setup(tmp_path, script)
    before, txs = sm.base_ir(), list(sm.tx_ids)
    bad = IRPatch(ops=[SetTransform(node_id="lid", transform=Transform(translate=(5, 0, 5)))])
    asked: list[Any] = []

    def repair(scene: SceneIR, *, action: Any, located: dict, manifest: str) -> IRPatch:
        asked.append(action)
        return bad

    author = CallableAuthor(lambda *a, **k: [], repair)
    res = await run_a(sm, plane, author, tmp_path, max_rounds=4)

    assert res.rounds[-1].rolled_back and "rolled back" in res.stopped["A"] and res.converged["A"] is False
    assert sm.base_ir() == before and sm.tx_ids == txs  # the transaction is gone, not just undone by a second patch
    # Round five: a rolled-back repair is asked for once more (REPAIR_PATIENCE = 2), told what its patch did and why it
    # was undone, from the kept design's own verdict (not judged again); a second rollback in a row stops the pass.
    assert author.calls["repair"] == 2 and len(res.rounds) == 3 and res.stopped["A"].startswith("repair rolled back 2 time(s)")
    assert "your previous patch for this design was undone: your patch had 1 op(s): set_transform lid" in asked[1].reasons[-1]
    # loop2: inverted faces are a defect the kernel checks itself (pyramid.KERNEL_CHECKED); the judge's 0.95 on a design
    # Tier-1 finds no inverted face on is not evidence. The silhouette collapsing (3.6 -> 1.0) is.
    assert "fell by more than the judge's noise band" in asked[1].reasons[-1] and "inverted" not in asked[1].reasons[-1]
    assert res.fix_records == []
    assert res.tier15 == res.rounds[0].tier15  # reported state is the pre-repair state


async def test_auto_fix_paths_make_zero_author_calls(tmp_path: Path) -> None:
    script = Script(blockout=lambda n: {"defect_floating": 0.9} if n == 1 else {}, lookdev=lambda n: {"defect_cg_plastic": 0.9} if n == 1 else {})
    sm, plane = setup(tmp_path, script)
    m = maps()
    orm_before = m.orm.tobytes()
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: LID_DOWN)
    res = await run_a(sm, plane, author, tmp_path, run_pass_b=True, textures={"wood": m})

    assert author.calls == {"propose": 0, "repair": 0} and res.author_calls == 0
    assert [(r.stage, r.action, r.fixer) for r in res.rounds] == [("A", "auto_fix", "reground"), ("A", "approve", None),
                                                                   ("B", "auto_fix", "roughness_breakup"), ("B", "approve", None)]
    assert not [i for i in tier1_qa(sm.extract_meshes(), BUDGET).issues if i.code == "floating_part"]  # settled deterministically
    assert res.objects["maps"]["wood"].orm.tobytes() != orm_before  # roughness breakup was applied to the real map
    assert [r.ops for r in res.fix_records] == [["set_transform"], ["roughness_breakup"]]
    assert (tmp_path / "wd" / "textures" / "wood_orm.png").exists()


async def test_budget_exhaustion_stops_before_a_repair(tmp_path: Path) -> None:
    sm, plane = setup(tmp_path, Script(blockout=lambda n: dict(FLAGGED)))
    gov = Governor(plan_for(2))
    gov.charge("spent", 0.849)
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: LID_DOWN)
    res = await run_a(sm, plane, author, tmp_path, governor=gov)
    assert res.stopped["A"] == "budget exhausted" and author.calls["repair"] == 0


class NoLocate:
    """A plane without locate_defects (an older control plane)."""

    def __init__(self, plane: Any) -> None:
        self._p = plane

    def __getattr__(self, name: str) -> Any:
        if name == "locate_defects":
            raise AttributeError(name)
        return getattr(self._p, name)


async def test_full_run_records_every_step_and_skips_carry_reasons(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    script = Script(blockout=lambda n: {} if state.get("fixed") else dict(FLAGGED))
    sm, plane = setup(tmp_path, script)
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: (state.update(fixed=True), LID_DOWN)[1])
    res = await refine(sm, plane=NoLocate(plane), author=author, governor=Governor(plan_for(2)), budget=BUDGET, uploader=up, workdir=tmp_path / "wd",
                       forge=ImageForge(FakeImageBackend()), settle_first=False)
    status = {s.name: s.status for s in res.steps}
    skipped = dict(res.skipped)
    assert not res.failed, res.failed
    for name in ("textures", "uv_lod_colliders", "usd_validate", "engine_compliance", "export_glb"):
        assert status[name] == "passed", (name, status)
    # an all-box prop has nothing to simplify within the screen-space error budget: LOD0 only, nothing to pop
    assert {"locate_defects", "render_tier", "paintover", "engine_import", "engine_parity", "lod_pop", "lod_pop_vision"} <= set(skipped)
    assert "LOD0 only" in skipped["lod_pop"]
    assert all(reason.strip() for reason in skipped.values())
    assert "no locate_defects" in skipped["locate_defects"] or "no locate_defects" in skipped["locate_defects"].replace("plane has ", "")
    assert "not found" in skipped["engine_import"] and "paintover_loop disabled" in skipped["paintover"] and "no Blender pool" in skipped["render_tier"]
    assert res.render_tier == "preview" and res.ok
    assert Path(res.artifacts["usd"]).exists() and Path(res.artifacts["glb"]).exists()
    assert res.to_dict()["skipped"] and all(r["reason"] for r in res.to_dict()["skipped"])
    # the shipped package is loaded in three.js when a browser exists; otherwise the step says what is missing
    assert status["web_runtime"] in ("passed", "skipped"), status
    if status["web_runtime"] == "passed":
        assert Path(res.artifacts["web_render"]).exists() and res.scores["web_silhouette_iou"] >= 0.98
        assert not list((tmp_path / "wd").glob("web_three_*")), "the verifier's scratch directory must not be left behind"
    else:
        assert "playwright" in skipped["web_runtime"] or "browser" in skipped["web_runtime"]


async def test_web_runtime_step_is_skipped_with_the_reason_or_fails_the_run(tmp_path: Path, monkeypatch) -> None:
    from antigravity3d.adapters.engines import web_three
    from antigravity3d.tactics import pyramid

    async def run(sub: str):
        sm, plane = setup(tmp_path / sub, Script(), sound_scene())
        return await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(2)), budget=BUDGET,
                            uploader=up, workdir=tmp_path / sub / "wd", forge=ImageForge(FakeImageBackend()), settle_first=False)

    # no browser: skipped with the verifier's own reason, and the run is still a pass
    monkeypatch.setattr(web_three.WebThreeValidator, "_unavailable", lambda self: "web_three browser not found (set $CHROMIUM_BIN)")
    res = await run("absent")
    assert dict(res.skipped)["web_runtime"] == "web_three browser not found (set $CHROMIUM_BIN)" and res.ok
    assert "web_render" not in res.artifacts

    # the runtime rejects the package: the step fails with the failing checks named, and so does the run
    class Rejecting(web_three.WebThreeValidator):
        async def validate(self, package_path, timeout=300, **kw):
            checks = (web_three.Check("no loader/console/page errors", "fail", "THREE.GLTFLoader: Unexpected end of JSON input"),
                      web_three.Check("triangles == manifest tris", "pass", "loaded 24, manifest 24"))
            return web_three.WebThreeReport(False, web_three.ENGINE, "FAIL", checks=checks)

    monkeypatch.setattr(pyramid, "WebThreeValidator", Rejecting)
    res = await run("rejected")
    failed = dict(res.failed)
    assert "Unexpected end of JSON input" in failed["web_runtime"] and "triangles ==" not in failed["web_runtime"]
    assert not res.ok


async def test_hero_plan_with_a_fake_pool_and_no_engine_render(tmp_path: Path) -> None:
    sm, plane = setup(tmp_path, Script(), sound_scene())
    pool = BlenderPool(1, fake=True)  # never started: a fake pool must not be used as a renderer
    forge = ImageForge(FakeImageBackend())
    res = await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(3)), budget=BUDGET, uploader=up,
                       workdir=tmp_path / "wd", forge=forge, pool=pool, textures={"wood": maps()}, settle_first=False)
    skipped = dict(res.skipped)
    assert "pool is fake" in skipped["render_tier"] and res.render_tier == "preview"
    assert next(s for s in res.steps if s.name == "paintover").status == "passed" and "paintover_uri" in res.artifacts
    assert "no engine render available" in skipped["engine_parity"]  # plan wants engine truth, but no engine render exists
    assert asyncio.iscoroutinefunction(pool.call)


# --------------------------------------------------------------------------- a failed author repair is a result


import json  # noqa: E402
from dataclasses import replace  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from google.genai import types as genai_types  # noqa: E402

from antigravity3d.models import tier  # noqa: E402
from antigravity3d.tactics.governor import BudgetExceeded  # noqa: E402
from antigravity3d.tactics.metered_author import AuthorNoAnswer, MeteredAuthor  # noqa: E402

LID_DOWN_JSON = json.dumps({"ops": [{"op": "set_transform", "node_id": "lid", "transform": {"translate": [0, 0, 1.025]}}]})


class TierEndpoint(StreamFromBlocking):  # MeteredAuthor streams: the scripted answer is delivered as a stream
    """Fake models endpoint: one scripted finish per model, so the worker and the director tier can differ."""

    def __init__(self, finishes: dict[str, str]) -> None:
        self.finishes, self.models, self.fixed = finishes, [], False

    async def count_tokens(self, **kw: Any) -> Any:
        return SimpleNamespace(total_tokens=100)

    async def generate_content(self, **kw: Any) -> Any:
        model = kw["model"]
        self.models.append(model)
        finish = self.finishes[model]
        self.fixed = self.fixed or finish == "STOP"
        usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=30, thoughts_token_count=20, total_token_count=150)
        text = LID_DOWN_JSON if finish == "STOP" else ""
        return SimpleNamespace(text=text, usage_metadata=usage, prompt_feedback=None,
                               candidates=[SimpleNamespace(finish_reason=genai_types.FinishReason(finish))])


def metered(monkeypatch: pytest.MonkeyPatch, ep: TierEndpoint, gov: Governor) -> MeteredAuthor:
    rates = {tier(role).model: [1, 2] for role in ("worker", "director")}
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", json.dumps(rates))
    return MeteredAuthor(SimpleNamespace(aio=SimpleNamespace(models=ep)), gov, BUDGET, {"asset_name": "Prop"})


def broken_script(ep: TierEndpoint) -> Script:
    return Script(blockout=lambda n: {} if ep.fixed else dict(FLAGGED))


async def test_truncated_worker_repair_is_retried_once_on_the_director_when_the_plan_allows(tmp_path: Path, monkeypatch) -> None:
    ep = TierEndpoint({tier("worker").model: "MAX_TOKENS", tier("director").model: "STOP"})
    gov = Governor(plan_for(2))  # standard: worker builder, allow_director
    sm, plane = setup(tmp_path, broken_script(ep))
    res = await run_a(sm, plane, metered(monkeypatch, ep, gov), tmp_path, governor=gov)
    assert ep.models == [tier("worker").model, tier("director").model]
    assert res.converged["A"] and res.author_calls == 2
    assert [s.status for s in res.steps if s.name == "author_repair"] == ["passed"]
    assert sm.base_ir().nodes[1].transform.translate == (0, 0, 1.025)


async def test_truncated_repair_without_director_permission_is_a_recorded_stop(tmp_path: Path, monkeypatch) -> None:
    ep = TierEndpoint({tier("worker").model: "MAX_TOKENS", tier("director").model: "STOP"})
    gov = Governor(replace(plan_for(2), allow_director=False))
    sm, plane = setup(tmp_path, broken_script(ep))
    before = sm.base_ir()
    res = await run_a(sm, plane, metered(monkeypatch, ep, gov), tmp_path, governor=gov)
    assert ep.models == [tier("worker").model]  # no escalation the plan does not grant
    assert "author repair failed" in res.stopped["A"] and "AuthorTruncated" in res.stopped["A"] and "not permitted" in res.stopped["A"]
    assert res.converged["A"] is False and sm.base_ir() == before
    assert [(s.name, s.status) for s in res.steps if s.name == "author_repair"] == [("author_repair", "warned")]


async def test_empty_answer_on_the_director_is_not_retried_and_names_the_cause(tmp_path: Path, monkeypatch) -> None:
    ep = TierEndpoint({tier("worker").model: "MAX_TOKENS", tier("director").model: "MAX_TOKENS"})
    gov = Governor(plan_for(2))
    sm, plane = setup(tmp_path, broken_script(ep))
    res = await run_a(sm, plane, metered(monkeypatch, ep, gov), tmp_path, governor=gov)
    assert ep.models == [tier("worker").model, tier("director").model]  # exactly one escalation
    assert "director retry also AuthorTruncated" in res.stopped["A"] and res.author_calls == 2


async def test_blocked_repair_is_not_escalated(tmp_path: Path, monkeypatch) -> None:
    ep = TierEndpoint({tier("worker").model: "SAFETY", tier("director").model: "STOP"})
    gov = Governor(plan_for(2))
    sm, plane = setup(tmp_path, broken_script(ep))
    res = await run_a(sm, plane, metered(monkeypatch, ep, gov), tmp_path, governor=gov)
    assert ep.models == [tier("worker").model] and "AuthorNoAnswer" in res.stopped["A"] and "SAFETY" in res.stopped["A"]


async def test_director_retry_that_cannot_be_admitted_is_recorded(tmp_path: Path, monkeypatch) -> None:
    ep = TierEndpoint({tier("worker").model: "MAX_TOKENS", tier("director").model: "STOP"})
    # The ceiling sits halfway between the two repair worst cases at the test's rates ($1/M in, $2/M out; 100 counted
    # input tokens plus the patch schema's bound), so the worker's fits and the director's does not. It was a fixed
    # 0.06, sized when the patch schema was 9,317 bytes (worker 0.0586); round five's schema (new shapes, shortcuts)
    # puts the worker at 0.0605 and the director at 0.0770, so it is derived from the schema now.
    from antigravity3d.ir_schema import patch_response_schema
    from antigravity3d.tactics.metered_author import output_budget, schema_input_token_bound

    worst = {r: ((100 + schema_input_token_bound(patch_response_schema())) * 1 + output_budget(r).max_output_tokens * 2) / 1e6
             for r in ("worker", "director")}
    assert worst["worker"] < worst["director"]
    gov = Governor(replace(plan_for(2), ceiling_usd=(worst["worker"] + worst["director"]) / 2))
    sm, plane = setup(tmp_path, broken_script(ep))
    res = await run_a(sm, plane, metered(monkeypatch, ep, gov), tmp_path, governor=gov)
    assert ep.models == [tier("worker").model] and "director retry not admitted" in res.stopped["A"]


@pytest.mark.parametrize("error", [AuthorNoAnswer("blocked", model="m", role="worker", operation="repair", finish_reason="SAFETY",
                                                  usage=None, max_output_tokens=1),
                                   BudgetExceeded("no room")])
async def test_typed_failures_from_any_author_end_the_pass_with_a_stop_reason(tmp_path: Path, error: Exception) -> None:
    def repair(*a: Any, **k: Any) -> IRPatch:
        raise error

    sm, plane = setup(tmp_path, Script(blockout=lambda n: dict(FLAGGED)))
    res = await run_a(sm, plane, CallableAuthor(lambda *a, **k: [], repair), tmp_path)
    assert res.stopped["A"].startswith("author repair failed") and res.converged["A"] is False and res.author_calls == 1


async def test_unexpected_exceptions_from_the_author_still_propagate(tmp_path: Path) -> None:
    def repair(*a: Any, **k: Any) -> IRPatch:
        raise RuntimeError("bug")

    sm, plane = setup(tmp_path, Script(blockout=lambda n: dict(FLAGGED)))
    with pytest.raises(RuntimeError, match="bug"):
        await run_a(sm, plane, CallableAuthor(lambda *a, **k: [], repair), tmp_path)


# --------------------------------------------------------------------------- web-runtime step: its browser's lifetime


class _Lease:
    """Stands in for the shared browser's lease: records when it was taken and how it was given back."""

    def __init__(self, log: list) -> None:
        self.log = log
        log.append("prestart")

    async def aclose(self, keep: bool = True) -> None:
        self.log.append(f"release keep={keep}")


def _record_web(monkeypatch, log: list, validate=None) -> None:
    from antigravity3d.adapters.engines import web_three
    from antigravity3d.tactics import pyramid

    monkeypatch.setattr(pyramid.web_three, "prestart", lambda: _Lease(log))
    if validate is not None:
        class Recording(web_three.WebThreeValidator):
            pass
        Recording.validate = validate
        monkeypatch.setattr(pyramid, "WebThreeValidator", Recording)


async def test_web_browser_starts_before_pass_a_and_is_kept_only_after_a_good_run(tmp_path: Path, monkeypatch) -> None:
    from antigravity3d.tactics import pyramid

    log: list = []
    _record_web(monkeypatch, log)
    real_a = pyramid._Run.pass_a

    async def pass_a(self):
        log.append("pass A")
        await real_a(self)
    monkeypatch.setattr(pyramid._Run, "pass_a", pass_a)
    sm, plane = setup(tmp_path / "ok", Script(), sound_scene())
    res = await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(2)), budget=BUDGET,
                       uploader=up, workdir=tmp_path / "ok" / "wd", forge=ImageForge(FakeImageBackend()), settle_first=False)
    assert res.ok and log == ["prestart", "pass A", f"release keep={True}"], log

    # a run that raises closes the browser it started (nothing is left running), and pass A/B-only runs never start one
    log.clear()

    def repair(*a: Any, **k: Any) -> IRPatch:
        raise RuntimeError("bug")

    sm, plane = setup(tmp_path / "bug", Script(blockout=lambda n: dict(FLAGGED)))
    with pytest.raises(RuntimeError, match="bug"):
        await refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: [], repair), governor=Governor(plan_for(2)), budget=BUDGET,
                     uploader=up, workdir=tmp_path / "bug" / "wd", settle_first=False)
    assert log == ["prestart", "pass A", "release keep=False"], log
    log.clear()
    sm, plane = setup(tmp_path / "a", Script())
    await run_a(sm, plane, CallableAuthor(lambda *a, **k: []), tmp_path / "a")
    assert log == ["pass A"], log


async def test_cancelling_pass_c_stops_the_running_web_verification(tmp_path: Path, monkeypatch) -> None:
    log: list = []
    started = asyncio.Event()

    async def validate(self, package_path, timeout=300, **kw):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            log.append("verification cancelled")
            raise

    _record_web(monkeypatch, log, validate)
    from antigravity3d.tactics import pyramid

    importing = asyncio.Event()

    class SlowEngine:  # the target engine's import is still running when the run is cancelled
        async def validate(self, glb):
            importing.set()
            await asyncio.sleep(3600)
    monkeypatch.setattr(pyramid, "validator_for", lambda engine: SlowEngine())
    sm, plane = setup(tmp_path, Script(), sound_scene())
    task = asyncio.ensure_future(refine(sm, plane=plane, author=CallableAuthor(lambda *a, **k: []), governor=Governor(plan_for(2)),
                                        budget=BUDGET, uploader=up, workdir=tmp_path / "wd", forge=ImageForge(FakeImageBackend()),
                                        settle_first=False))
    await asyncio.wait_for(asyncio.gather(started.wait(), importing.wait()), 60)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == ["prestart", "verification cancelled", "release keep=False"], log


async def test_settle_leaves_a_part_beside_another_instead_of_dropping_it_to_the_ground(tmp_path: Path) -> None:
    """The exact floating test flags a sphere beside a box corner (bounding boxes touching, surfaces 41 mm apart). The
    settle pass must not carry it 0.4 m down to the ground, away from the part it belongs to: it is left to the author."""
    scene = SceneIR(asset_name="Prop", materials=[MaterialIR(id="wood")], nodes=[
        NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, transform=Transform(translate=(0, 0, 0.5)), material_id="wood"),
        NodeIR(id="ball", kind="sphere", params={"radius": 0.1, "segments": 24, "rings": 12},
               transform=Transform(translate=(0.6, 0.6, 0.5)), material_id="wood")])
    assert any(i.code == "floating_part" and i.node_id == "ball" for i in tier1_qa(setup(tmp_path, Script(), scene)[0].extract_meshes(), BUDGET).issues)
    script = Script(blockout=lambda n: {"defect_floating": 0.9})
    sm, plane = setup(tmp_path, script, scene)
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: None)
    res = await run_a(sm, plane, author, tmp_path, max_rounds=2)
    assert "left to the author" in res.stopped["A"], res.stopped
    assert sm.base_ir().nodes[1].transform.translate == (0.6, 0.6, 0.5)   # not moved
    assert author.calls["repair"] == 0
