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


# --------------------------------------------------------------------------- pairwise validation (an uncalibrated judge)


class Pairwise:
    """The judge plane as an uncalibrated one: every call goes to the scripted plane except the pairwise question
    (``DecisionControlPlane.compare_states``), answered from ``decide(n)``, the verdict of the n-th comparison ("after" |
    "before" | "neither" for the state shown second in the first call of the pair). pyramid.compare_shape asks each
    comparison twice, order swapped (the first call: before as state A; the second: after as state A)."""

    calibrated = False

    def __init__(self, plane: Any, decide) -> None:
        self.plane, self.decide, self.asked = plane, decide, []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.plane, name)

    async def compare_states(self, first, second, *, aspect: str, brief: str | None = None, governor: Any = None) -> Any:
        self.asked.append((tuple(first), tuple(second), aspect))
        n, before_first = (len(self.asked) + 1) // 2, len(self.asked) % 2 == 1
        v = self.decide(n)
        closer = "neither" if v == "neither" else ("B" if (v == "after") == before_first else "A")
        return SimpleNamespace(closer=closer, probabilities={closer: 0.9}, reason="the later state has the pads a crate lid has")


def _pairwise(r: Rounds, decide) -> Pairwise:
    r.plane = Pairwise(r.plane, decide)
    return r.plane


async def test_rounds_the_judge_prefers_in_both_orders_stand_whatever_one_absolute_score_says(tmp_path: Path) -> None:
    """The live crate: 44 -> 66 parts restored on 2.81 against 2.75, and rounds undone on one judgement each (2.90 ->
    2.69). With the comparison, a single absolute score falling by more than the band undoes nothing."""
    noisy = lambda k, seen: {"score_brief_depiction": 3.4 - (0.3 if k % 2 else 0.0)}  # noqa: E731 - one judgement's noise
    r = Rounds(tmp_path, noisy)
    plane = _pairwise(r, lambda n: "after")
    res = await r.run()
    assert r.pads() == 5 and plane.asked and all(a == "shape" for *_, a in plane.asked)
    assert not any(s.status == "warned" for s in res.steps if s.name.startswith(("improve_A", "validate_A")))
    assert "5 round(s) kept (pairwise validation)" in res.stopped["A"]


async def test_split_or_neither_leaves_rounds_provisional_and_restores_them_at_the_end(tmp_path: Path) -> None:
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 3.0 + 0.1 * k})
    _pairwise(r, lambda n: "neither")
    res = await r.run()
    steps = {s.name: s for s in res.steps}
    assert steps["validate_A2"].status == "skipped" and "not decided yet" in steps["validate_A2"].detail
    assert r.pads() == 0 and "not shown better at the end" in steps["validate_A5"].detail


async def test_the_validated_design_preferred_in_both_orders_is_restored(tmp_path: Path) -> None:
    plan = replace(plan_for(2), improve_rounds=4, improve_patience=3, improve_target=4.0)
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 3.0}, plan=plan)
    _pairwise(r, lambda n: "before" if n == 1 else "after")
    res = await r.run()
    steps = {s.name: s for s in res.steps}
    assert steps["validate_A2"].status == "warned" and "preferred in both orders" in steps["validate_A2"].detail
    assert steps["validate_A4"].status == "passed" and r.pads() == 2  # pads 0 and 1 again, then validated


async def test_rounds_without_their_own_review_are_decided_by_the_comparison(tmp_path: Path) -> None:
    """``SpendPlan.improve_review_rounds = False``: no judge call per round; the shipped design gets one review."""
    plan = replace(plan_for(2), improve_rounds=4, improve_patience=2, improve_target=4.0, improve_review_rounds=False)
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 3.0 + 0.1 * k}, plan=plan)
    plane = _pairwise(r, lambda n: "after")
    res = await r.run()
    assert r.pads() == 4 and len(plane.asked) == 4  # two validations, each asked in both orders
    assert sum(r.seen.values()) == 2 and r.seen == {0: 1, 4: 1}  # the start and the design that ships, once each
    assert res.scores["score_brief_depiction"] == pytest.approx(3.4)  # the shipped design's own verdict
    assert all("not reviewed" in s.detail for s in res.steps if s.name.startswith("improve_A"))


@pytest.mark.parametrize("preferred, kept", [("after", True), ("before", False)])
async def test_a_repair_the_kernel_shows_better_is_undone_only_if_the_judge_prefers_the_design_before(
        tmp_path: Path, preferred: str, kept: bool) -> None:
    """The live chair: repairs that took floating parts 6 -> fewer were undone on a judge-only "clipping" (spindles
    socketed into the seat), twice, and no improvement round ran."""
    from test_pyramid import LID_DOWN, floating_scene
    from antigravity3d.tactics.authoring import CallableAuthor

    before = {"defect_floating": 0.9, "defect_faceted": 0.9}
    after = {"defect_clipping": 0.9, "defect_faceted": 0.9}
    lid_down = lambda: any(n.id == "lid" and n.transform.translate == (0, 0, 1.025) for n in sm.base_ir().nodes)  # noqa: E731
    sm, plane = setup(tmp_path, Script(blockout=lambda n: dict(after if lid_down() else before)), floating_scene())
    plane = Pairwise(plane, lambda n: preferred)
    author = CallableAuthor(lambda *a, **k: [], lambda *a, **k: LID_DOWN)
    res = await run_a(sm, plane, author, tmp_path, max_rounds=2)
    step = next(s for s in res.steps if s.name == "compare_A2")
    assert lid_down() is kept and len(plane.asked) == 2
    if kept:
        assert step.status == "passed" and "floating parts 1->0" in step.detail and "defect_clipping no longer counted" in step.detail
    else:
        assert res.rounds[-1].rolled_back and "compared: judge: before" in step.detail


async def test_a_part_is_not_settled_onto_a_part_that_itself_floats(tmp_path: Path) -> None:
    """The live chair: the crest rail dropped 216 mm down the spindles onto one that floats (settle_pre), and the chair
    shipped with its back rail at seat height and a new floating group."""
    scene = SceneIR(asset_name="Prop", materials=[MaterialIR(id="wood")], nodes=[
        NodeIR(id="seat", kind="box", params={"size": (0.6, 0.6, 0.45)}, transform=Transform(translate=(0, 0, 0.225)), material_id="wood"),
        # 4 mm beside the seat, 12 mm above the ground: left to the author (kernel.settle.beside_a_part)
        NodeIR(id="post", kind="box", params={"size": (0.04, 0.04, 0.8)}, transform=Transform(translate=(0.324, 0, 0.412)), material_id="wood"),
        NodeIR(id="rail", kind="box", params={"size": (0.1, 0.1, 0.04)}, transform=Transform(translate=(0.324, 0, 1.1)), material_id="wood")])
    sm = _stage(tmp_path, scene)
    r = P._Run.__new__(P._Run)
    r.sm, r.budget = sm, EngineBudget("godot_4", max_tris=4000)
    t1 = tier1_qa(sm.extract_meshes(), r.budget)
    assert {"post", "rail"} <= P.floating_ids(t1)
    patch, _ = r.settle_patch(t1)
    assert patch is not None and {o.node_id for o in patch.ops} == {"rail"}  # what settle_pre was given live
    sm2, plane = setup(tmp_path / "c", Script(), scene)
    res = await run_a(sm2, plane, SimpleNamespace(propose=None, repair=None), tmp_path, settle_first=True, max_rounds=1)
    step = next(s for s in res.steps if s.name == "settle_pre")
    assert step.status == "skipped" and "would land on a part that floats): rail" in step.detail
    assert next(n for n in sm2.base_ir().nodes if n.id == "rail").transform.translate == (0.324, 0, 1.1)


def test_the_built_in_plans_are_the_measured_ones() -> None:
    """Measured live 2026-11-22 (r5/live/m3_loop2): validations of two rounds preferred the new state 4 of 4, a fifth
    round validated alone was "neither" 4 of 4. Rounds come in pairs, on the worker tier, and stop at the first
    validation that restores."""
    from antigravity3d.tactics.governor import PLANS

    assert [PLANS[i].improve_rounds for i in sorted(PLANS)] == [2, 2, 4, 4]
    assert all(p.improve_validate_every == 2 and p.improve_patience == 1 and p.improve_role == "worker" and p.improve_review_rounds
               and p.improve_pairwise for p in PLANS.values())


@pytest.mark.parametrize("decide, pads, improves", [
    (lambda n: "before", 0, 2),  # the first validation restores: the rounds stop (patience counts validations)
    (lambda n: "neither" if n == 1 else "after", 4, 4),  # not decided yet stops nothing: four rounds, validated together
])
async def test_the_rounds_stop_at_the_first_validation_that_restores(tmp_path: Path, decide, pads: int, improves: int) -> None:
    r = Rounds(tmp_path, lambda k, seen: {"score_brief_depiction": 3.0}, plan=replace(plan_for(2), improve_target=4.0))
    _pairwise(r, decide)
    res = await r.run()
    assert r.pads() == pads and r.improve_calls == improves, res.stopped["A"]


async def test_a_design_whose_repairs_used_pass_a_s_rounds_still_gets_its_improvement_rounds(tmp_path: Path) -> None:
    """The live chair (r5/live/m3_loop2): repairs took the floating parts 6 -> 0 and used every round of pass A
    ("rounds exhausted"); no improvement round ran on the brief where the proportions mattered most."""
    from test_pyramid import LID_DOWN, floating_scene

    lid_down = lambda: any(n.id == "lid" and n.transform.translate == (0, 0, 1.025) for n in sm.base_ir().nodes)  # noqa: E731
    # before: floating and faceted (a repair); after: faceted only, still a gate, so pass A does not converge
    sm, plane = setup(tmp_path, Script(blockout=lambda n: {"defect_faceted": 0.9, **({} if lid_down() else {"defect_floating": 0.9})}),
                      floating_scene())
    improved: list[int] = []

    async def repair(*a: Any, **k: Any) -> IRPatch:
        return LID_DOWN

    async def improve(scene: SceneIR, **kw: Any) -> IRPatch:
        improved.append(1)
        pad = NodeIR(id=f"pad_{len(improved)}", kind="box", params={"size": (0.1, 0.1, 0.02)},
                     transform=Transform(translate=(-0.4 + 0.15 * len(improved), 0, 1.06)), material_id="wood")
        return IRPatch(ops=[UpsertNode(node=pad)])

    author = SimpleNamespace(improve=improve, repair=repair, propose=None)
    plan = replace(plan_for(2), improve_rounds=2, improve_target=4.0)
    res = await run_a(sm, plane, author, tmp_path, prompt=CRATE, governor=Governor(plan), max_rounds=2)
    assert lid_down() and len(improved) >= 1
    assert res.stopped["A"].startswith("rounds exhausted; 1 repair(s) kept: improvement rounds")


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
