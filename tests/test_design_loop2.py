"""The design loop survives real designs (loop2): what the first full live measurement lost, as failing tests first.

Measured live 2026-11-22 (r5/live/m2_worker, worker tier, five rounds): the crate kept 1 round of 5 (2.64 -> 2.74), the
hydrant nothing in 12.5 minutes (a director repair 7 % over budget, then a truncated retry), the chair nothing (two repairs
each left a part floating). Pinned here:
* a patch gets the clean-up a first design gets, on what it changed only (``pyramid.tidy``): the crate's four lid pads
  of improvement round 4 (the recorded patch on the recorded design) settle onto the lid; a part over the budget loses
  segments; a part far above everything is left to the author;
* the judge's answer on a defect the kernel checks counts only when the kernel finds it (the crate's round 1: inverted
  faces 0.85 on a design with none); a defect only the judge sees that a round newly brings is asked about again;
* rounds are kept provisionally and validated by k judgements in parallel: gains of 0.05 a round add up, a drift of
  -0.10 a round is caught;
* an unsure verdict ("ambiguous defects") goes to an improvement round on the plan's tier, not to a director repair;
* a call cut off with most of its output spent thinking is asked again once at a lower thinking level.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from antigravity3d.dags.edit_asset import changed_nodes
from antigravity3d.ir import IRPatch, MaterialIR, NodeIR, SceneIR, Transform, UpsertNode
from antigravity3d.kernel.fast_ops import tier1_qa
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics import pyramid as P
from antigravity3d.tactics.governor import Governor, plan_for

from test_pyramid import Script, run_a, setup

LIVE = Path(__file__).resolve().parent / "data" / "loop2"
CRATE = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"


def _stage(tmp_path: Path, scene: SceneIR) -> StageManager:
    sm = StageManager.create(tmp_path / "s.usda", scene.asset_name)
    sm.apply_ir(scene)
    return sm


# --------------------------------------------------------------------------- clean-up of a patch


def test_the_crate_pads_a_round_left_floating_are_settled_onto_the_lid(tmp_path: Path) -> None:
    """The recorded design before the crate's improvement round 4 and that round's recorded patch (18 ops)."""
    scene = SceneIR.model_validate_json((LIVE / "crate_before_round4.json").read_text())
    patch = IRPatch.model_validate(json.loads((LIVE / "crate_round4_patch.json").read_text()))
    budget = EngineBudget("godot_4", max_tris=3000)
    sm = _stage(tmp_path, scene)
    before = sm.base_ir()
    sm.apply_patch(patch)
    pads = {f"lid_stack_pad_{c}" for c in ("bl", "br", "fl", "fr")}
    assert P.floating_ids(tier1_qa(sm.extract_meshes(), budget)) == pads  # live: "Tier-1 finds a new floating part"
    untouched = {n.id: n for n in sm.base_ir().nodes if n.id not in changed_nodes(before, sm.base_ir())}
    notes = P.tidy(sm, budget, changed_nodes(before, sm.base_ir()))
    assert P.floating_ids(tier1_qa(sm.extract_meshes(), budget)) == set() and notes[0].startswith("settled 4 part(s)")
    assert all(n == untouched[n.id] for n in sm.base_ir().nodes if n.id in untouched)  # nothing it did not change moved


def _box(nid: str, size: tuple[float, float, float], z: float, **kw: Any) -> NodeIR:
    return NodeIR(id=nid, kind="box", params={"size": size}, transform=Transform(translate=(0, 0, z)), material_id="m", **kw)


def test_a_part_over_the_budget_loses_segments_and_a_part_far_above_is_left(tmp_path: Path) -> None:
    scene = SceneIR(asset_name="X", materials=[MaterialIR(id="m")], nodes=[_box("body", (1, 1, 1), 0.5)])
    budget = EngineBudget("godot_4", max_tris=600)
    sm = _stage(tmp_path, scene)
    before = sm.base_ir()
    knob = NodeIR(id="knob", kind="sphere", params={"radius": 0.1, "segments": 64, "rings": 32},
                  transform=Transform(translate=(0, 0, 1.1)), material_id="m")
    sm.apply_patch(IRPatch(ops=[UpsertNode(node=knob), UpsertNode(node=_box("lamp", (0.1, 0.1, 0.1), 3.0))]))
    assert tier1_qa(sm.extract_meshes(), budget).stats["tris"] > 600
    notes = P.tidy(sm, budget, changed_nodes(before, sm.base_ir()))
    t1 = tier1_qa(sm.extract_meshes(), budget)
    assert t1.stats["tris"] <= 600 and any("fewer segments" in n for n in notes)
    assert next(n for n in sm.base_ir().nodes if n.id == "body") == before.nodes[0]
    # the lamp is 1.9 m above the box: not a clean-up, the author's correction says it floats
    assert "lamp" in P.floating_ids(t1)
    assert "these parts float" in P.tidy_findings(tier1_qa(_stage(tmp_path / "b", before).extract_meshes(), budget), t1,
                                                  {"knob", "lamp"}, budget)


# --------------------------------------------------------------------------- the judge against the kernel


class Rounds:
    """A pass A run on a box with an author that adds one pad per improvement round, and a fake judge whose answers are
    a function of the design (pads on it) and of how often this design was judged: the order of parallel calls does not
    matter."""

    def __init__(self, tmp_path: Path, answers, *, plan: Any = None, repair: Any = None) -> None:
        self.answers, self.seen = answers, {}
        scene = SceneIR(asset_name="Prop", materials=[MaterialIR(id="wood")], nodes=[
            NodeIR(id="body", kind="box", params={"size": (1, 1, 1)}, transform=Transform(translate=(0, 0, 0.5)), material_id="wood")])
        self.sm, self.plane = setup(tmp_path, Script(blockout=self.judge), scene)
        self.plan = plan or replace(plan_for(2), improve_rounds=5, improve_patience=2, improve_target=4.0)
        self.tmp, self.improve_calls, self.repair_calls = tmp_path, 0, 0
        self.repair = repair

    def pads(self) -> int:
        return sum(n.id.startswith("pad_") for n in self.sm.base_ir().nodes)

    def judge(self, n: int) -> dict:
        k = self.pads()
        self.seen[k] = self.seen.get(k, 0) + 1
        return {"score_silhouette": 3.6, **self.answers(k, self.seen[k])}

    async def improve(self, scene: SceneIR, **kw: Any) -> IRPatch:
        self.improve_calls += 1
        i = sum(n.id.startswith("pad_") for n in scene.nodes)
        pad = NodeIR(id=f"pad_{i}", kind="box", params={"size": (0.1, 0.1, 0.02)},
                     transform=Transform(translate=(-0.4 + 0.15 * i, 0, 1.01)), material_id="wood")
        return IRPatch(ops=[UpsertNode(node=pad)])

    async def run(self) -> Any:
        author = SimpleNamespace(improve=self.improve, repair=self._repair, propose=None)
        return await run_a(self.sm, self.plane, author, self.tmp, prompt=CRATE, governor=Governor(self.plan))

    async def _repair(self, *a: Any, **k: Any) -> Any:
        self.repair_calls += 1
        return None


async def test_small_gains_add_up_across_rounds(tmp_path: Path) -> None:
    """+0.05 depiction a pad: under one judgement's noise each, the live crate lost rounds of +0.05 and +0.10 that way."""
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 2.64 + 0.05 * k})
    res = await r.run()
    steps = {s.name: s for s in res.steps}
    assert steps["validate_A2"].status == "skipped"  # +0.10 over two rounds: not decided yet
    assert steps["validate_A4"].status == "passed"  # +0.20 >= the band for 3 judgements (0.163)
    assert P.validation_band(3) == pytest.approx(0.163) and P.validation_band(1) == pytest.approx(0.283)
    assert r.pads() == 4 and res.scores["score_brief_depiction"] == pytest.approx(2.84)  # round 5 alone (+0.05) did not hold
    assert steps["validate_A5"].status == "warned" and "did not rise by the band" in steps["validate_A5"].detail


async def test_a_downward_drift_of_small_steps_is_caught(tmp_path: Path) -> None:
    # from 3.2, so that no step crosses the brief's own gate (2.5): only the drift can catch it
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 3.2 - 0.1 * k})
    res = await r.run()
    assert r.pads() == 0 and res.scores["score_brief_depiction"] == pytest.approx(3.2)
    assert "fell by the band" in next(s for s in res.steps if s.name.startswith("validate_A")).detail


async def test_a_judge_alarm_the_kernel_does_not_confirm_undoes_nothing(tmp_path: Path) -> None:
    """The crate's round 1 live: inverted faces 0.85 and holes 0.65 newly reported, Tier-1 found neither."""
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 2.6 + 0.1 * k,
                                          **({"defect_inverted_faces": 0.85, "defect_visible_holes": 0.65} if k else {})})
    res = await r.run()
    assert r.pads() >= 2 and not any("inverted" in s.detail for s in res.steps if s.name.startswith("improve_A"))


@pytest.mark.parametrize("second, kept", [(0.05, True), (0.9, False)])
async def test_a_defect_only_the_judge_sees_is_asked_about_again(tmp_path: Path, second: float, kept: bool) -> None:
    def answers(k: int, seen: int) -> dict:
        d = {"score_brief_depiction": 2.6 + 0.3 * k}
        if k == 1:  # the round's first judgement reports clipping; the second agrees or not
            d["defect_clipping"] = 0.9 if seen == 1 else second
        return d

    plan = replace(plan_for(2), improve_rounds=1, improve_patience=1, improve_target=4.0)
    r = Rounds(tmp_path, answers, plan=plan)
    res = await r.run()
    step = next(s for s in res.steps if s.name == "improve_A1")
    assert "asked again about defect_clipping 0.90 then" in step.detail
    assert (r.pads() == 1) is kept and (step.status == "passed") is kept


async def test_an_unsure_judge_sends_the_design_to_an_improvement_round_not_the_director(tmp_path: Path) -> None:
    """The hydrant live: clipping 0.65 and faceted 0.65, "ambiguous defects with no high-confidence signal"."""
    # the start fails its gates (silhouette 2.4, depiction 2.4) and the judge is unsure about two defects: a repair would
    # escalate to the director
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 2.4 + 0.3 * k, "score_silhouette": 2.4 + 0.3 * k,
                                          **({"defect_clipping": 0.65, "defect_faceted": 0.65} if k == 0 else {})})
    res = await r.run()
    assert r.repair_calls == 0 and r.improve_calls >= 1
    assert "the judge is unsure" in res.stopped["A"] and "instead of a director repair" in res.stopped["A"]


# --------------------------------------------------------------------------- less thinking after a cut-off answer


async def test_a_cut_off_answer_mostly_thinking_is_asked_again_with_less_thinking(monkeypatch) -> None:
    from antigravity3d.tactics.metered_author import LESS_THINKING, MeteredAuthor

    answer = json.dumps({"ops": [{"op": "set_param", "node_id": "body", "name": "size", "value": [1, 1, 1.1]}]})
    ep = _BlockingThinking(answer)
    gov = Governor(replace(plan_for(3), improve_role="director"), token_ceiling=500_000)
    author = MeteredAuthor(SimpleNamespace(aio=SimpleNamespace(models=ep)), gov, EngineBudget("godot_4"), {"asset_name": "X"})
    from antigravity3d.tactics.metered_author import DesignViews

    scene = SceneIR(asset_name="X", materials=[MaterialIR(id="m")], nodes=[_box("body", (1, 1, 1), 0.5)])
    patch = await author.improve(scene, brief="a crate", views=DesignViews((), "", ()), manifest="", triangles=12)
    assert patch is not None and ep.levels == ["HIGH", LESS_THINKING["HIGH"]]
    statuses = [r.get("status") for r in gov.receipts() if r["category"] == "author.improve"]
    assert statuses == ["AuthorTruncated", "ok"]


def _blocking_thinking_class():
    from stream_fakes import StreamFromBlocking

    class Blocking(StreamFromBlocking):
        """The same endpoint as a blocking fake, streamed by the project's own adapter (tests/stream_fakes.py)."""

        def __init__(self, answer: str) -> None:
            self.levels: list[str] = []
            self.answer = answer

        async def count_tokens(self, **kw: Any) -> Any:
            return SimpleNamespace(total_tokens=100)

        async def generate_content(self, **kw: Any) -> Any:
            from google.genai import types as gt

            tl = kw["config"].thinking_config.thinking_level
            level = str(getattr(tl, "name", tl)).upper()
            self.levels.append(level)
            cap = kw["config"].max_output_tokens
            if level == "HIGH":
                usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=cap - 27_000, thoughts_token_count=27_000,
                                        total_token_count=100 + cap)
                return SimpleNamespace(text="", usage_metadata=usage, prompt_feedback=None,
                                       candidates=[SimpleNamespace(finish_reason=gt.FinishReason("MAX_TOKENS"))])
            usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=300, thoughts_token_count=2_000,
                                    total_token_count=2_400)
            return SimpleNamespace(text=self.answer, usage_metadata=usage, prompt_feedback=None,
                                   candidates=[SimpleNamespace(finish_reason=gt.FinishReason("STOP"))])

    return Blocking


_BlockingThinking = _blocking_thinking_class()
