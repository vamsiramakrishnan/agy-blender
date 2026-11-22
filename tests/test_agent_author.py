"""Stage-session authoring (an unbound ``AgentAuthor``): no requested candidate ends without a recorded outcome.

The prop and environment-kit pipelines bind the author to their spend governors and author through one-shot model
calls (``tests/test_metered_author.py``). Direct callers use the stage sessions tested here. The run is scripted, but
its real stage configuration is built so contract selection is tested through the author-to-harness boundary.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from antigravity3d.ir import NodeIR, SceneIR, Transform
from antigravity3d.authoring_contract import AuthoringIntent, PROP_CRAFT_GUIDANCE
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import STAGE_TIMEOUT_S, AgentAuthor

BUDGET = EngineBudget("unreal_5")
CONTEXT = {"asset_name": "Crate"}


def _scene() -> SceneIR:
    return SceneIR(asset_name="Crate", nodes=[NodeIR(id="body", kind="box", params={"size": [1, 1, 1]}, transform=Transform(translate=(0, 0, 0.5)))])


def _author(script: dict, tmp_path, seen: list | None = None) -> AgentAuthor:
    """An author whose stage runs follow ``script``: per candidate index (or "repair") a scene the session builds, an
    exception the run raises, or None for a session that ends without building anything."""
    from antigravity3d.harness import session as S

    async def run_stage(stage, sid, prompt, *, role, timeout_s, authoring_intent, repair):
        from antigravity3d.harness.agents import build_stage_agent

        tag = sid.split("-")[0]
        cfg = build_stage_agent(stage, sid, role=role, authoring_intent=authoring_intent, repair=repair)
        try:
            target = S.DEFAULT_REGISTRY.resource(sid, S.R_STAGE)
            asset_name = target.base_ir().asset_name
            if seen is not None:
                seen.append({"stage": stage, "tag": tag, "role": role, "timeout_s": timeout_s, "prompt": prompt,
                             "intent": authoring_intent, "repair": repair, "system": cfg.system_instructions.text,
                             "brief": S.DEFAULT_REGISTRY.get(sid).brief, "budget": S.DEFAULT_REGISTRY.get(sid).budget,
                             "asset_name": asset_name})
            what = script[int(tag.removeprefix("cand"))] if tag.startswith("cand") else script[tag]
            if isinstance(what, BaseException):
                raise what
            if what is not None:
                target.apply_ir(what.model_copy(update={"asset_name": asset_name}))
            return SimpleNamespace(usage={"prompt_tokens": 1})
        finally:
            S.unbind_conversation(cfg.conversation_id)

    author = AgentAuthor(workdir=tmp_path)
    author._run_stage = lambda: run_stage
    return author


async def test_a_failed_session_is_a_recorded_outcome_beside_the_ones_that_worked(tmp_path):
    seen: list = []
    author = _author({0: _scene(), 1: TimeoutError(), 2: RuntimeError("Error 502: upstream unreachable")}, tmp_path, seen)
    result = await author.propose_with_outcomes("a crate", budget=BUDGET, n=3, context=CONTEXT)
    assert [s.asset_name for s in result.scenes] == ["Crate"] and [n.id for n in result.scenes[0].nodes] == ["body"]
    assert [(o.index, o.status) for o in result.outcomes] == [(0, "produced"), (1, "timed_out"), (2, "error")]
    assert f"{STAGE_TIMEOUT_S:.0f} s" in result.outcomes[1].detail  # a bare TimeoutError says nothing by itself
    assert "RuntimeError" in result.outcomes[2].detail and "502" in result.outcomes[2].detail
    assert sorted(s["tag"] for s in seen) == ["cand0", "cand1", "cand2"]
    assert all(s["stage"] == "BLOCKOUT" and s["timeout_s"] == STAGE_TIMEOUT_S for s in seen)
    assert len(author.usage) == 1  # usage is booked for the session that returned


async def test_propose_returns_the_scenes_of_the_sessions_that_worked(tmp_path):
    author = _author({0: RuntimeError("boom"), 1: _scene()}, tmp_path)
    scenes = await author.propose("a crate", budget=BUDGET, n=2, context=CONTEXT)
    assert len(scenes) == 1 and scenes[0].nodes[0].id == "body"


async def test_one_failed_session_raises_its_own_error_instead_of_returning_nothing(tmp_path):
    """The environment-kit pipeline asks for one candidate and takes element 0: an empty list hid the cause."""
    author = _author({0: RuntimeError("Error 502: upstream unreachable")}, tmp_path)
    with pytest.raises(RuntimeError, match="502"):
        await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT)
    author = _author({0: TimeoutError()}, tmp_path)
    with pytest.raises(TimeoutError, match=f"{STAGE_TIMEOUT_S:.0f} s"):
        await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT)


async def test_when_no_session_produces_a_scene_the_error_names_every_candidate(tmp_path):
    author = _author({0: RuntimeError("Error 502: upstream unreachable"), 1: TimeoutError()}, tmp_path)
    with pytest.raises(RuntimeError, match=r"502.*no stage session of 2 produced a scene.*#0 error.*#1 timed_out"):
        await author.propose("a crate", budget=BUDGET, n=2, context=CONTEXT)


async def test_a_session_that_builds_nothing_is_not_a_candidate(tmp_path):
    author = _author({0: None, 1: _scene()}, tmp_path)
    result = await author.propose_with_outcomes("a crate", budget=BUDGET, n=2, context=CONTEXT)
    assert [o.status for o in result.outcomes] == ["invalid", "produced"] and "no nodes" in result.outcomes[0].detail
    assert len(result.scenes) == 1
    with pytest.raises(ValueError, match="no nodes"):
        await _author({0: None}, tmp_path).propose("a crate", budget=BUDGET, n=1, context=CONTEXT)


async def test_a_failed_repair_session_raises(tmp_path):
    author = _author({0: _scene(), "repair": RuntimeError("Error 502: upstream unreachable")}, tmp_path)
    scene = (await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT))[0]
    with pytest.raises(RuntimeError, match="502"):
        await author.repair(scene, action="iterate", located={"floating": ["body"]}, manifest="body: box")


async def test_production_proposals_select_the_shared_contract_in_the_actual_stage_config(tmp_path):
    seen = []
    author = _author({i: _scene() for i in range(4)}, tmp_path, seen)
    brief = "a supply crate with corner guards, latches and two handles"
    await author.propose(brief, budget=BUDGET, n=4, context={**CONTEXT, "art_direction": "Thin plates, muted colours"})
    assert len(seen) == 4
    for sent in seen:
        assert sent["intent"] is AuthoringIntent.PRODUCTION_PROP and not sent["repair"]
        assert sent["prompt"].startswith(f"Build a production game prop of: {brief}.")
        assert "Build a blockout" not in sent["prompt"] and "then stop" not in sent["prompt"]
        assert "run_visual_qa" in sent["prompt"] and "QA shows no geometry repair" in sent["prompt"]
        assert "Thin plates, muted colours" in sent["prompt"]
        assert sent["system"].count(PROP_CRAFT_GUIDANCE) == 1
        assert sent["brief"] == brief and sent["budget"] == BUDGET
    assert "clean forms with complete brief coverage" in seen[-1]["prompt"]


async def test_kit_proposal_keeps_metric_and_rejection_feedback_without_prop_detail_targets(tmp_path):
    from antigravity3d.kernel.kit_metric import KitMetric, context_brief

    seen = []
    author = _author({0: _scene()}, tmp_path, seen)
    await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT)
    context = {"asset_name": "Wall", "template": "wall_segment", "kit_metric": KitMetric.interior(),
               "kit_feedback": "restore the wall height and keep the snap plane clear"}
    await author.propose("an interior wall segment", budget=BUDGET, n=1, context=context)
    sent = seen[-1]
    assert sent["intent"] is AuthoringIntent.KIT_PIECE
    assert sent["prompt"].startswith("Build a modular kit piece") and context_brief(context) in sent["prompt"]
    assert "Previous answer rejected:" in sent["prompt"]
    assert "mating surfaces" in sent["system"]
    assert "50 to 90" not in sent["system"] + sent["prompt"] and PROP_CRAFT_GUIDANCE not in sent["system"]
    assert sent["budget"] == BUDGET


@pytest.mark.parametrize("kit", [False, True])
async def test_localized_repairs_keep_the_original_kind_and_do_not_expand_the_design(tmp_path, kit):
    from antigravity3d.kernel.kit_metric import KitMetric, context_brief

    seen = []
    author = _author({0: _scene(), "repair": _scene()}, tmp_path, seen)
    context = {**CONTEXT, **({"template": "wall_segment", "kit_metric": KitMetric.interior()} if kit else {})}
    scene = (await author.propose("a wall" if kit else "a crate", budget=BUDGET, n=1, context=context))[0]
    await author.repair(scene, action=SimpleNamespace(kind="agent_repair", reasons=["floating"]),
                        located={"floating": ["body"]}, manifest="body: box")
    sent = seen[-1]
    assert sent["repair"] and sent["intent"] == seen[0]["intent"]
    assert "correcting only the flagged nodes" in sent["prompt"]
    assert "Preserve correct geometry and its level of detail" in sent["system"]
    assert "50 to 90" not in sent["prompt"] + sent["system"] and PROP_CRAFT_GUIDANCE not in sent["system"]
    assert "Repair this blockout" not in sent["prompt"]
    if kit:
        assert context_brief(context) in sent["prompt"]


async def test_brief_repair_can_add_missing_parts_without_rebuilding_correct_nodes(tmp_path):
    from antigravity3d.decisions.compose import BRIEF_REASON
    from antigravity3d.tactics.metered_author import repair_task

    seen = []
    author = _author({0: _scene(), "repair": _scene()}, tmp_path, seen)
    scene = (await author.propose("a crate with two handles", budget=BUDGET, n=1, context=CONTEXT))[0]
    reasons = ["handle missing", BRIEF_REASON + "a crate with two handles"]
    await author.repair(scene, action=SimpleNamespace(kind="agent_repair", reasons=reasons), located={}, manifest="body")
    native = seen[-1]["prompt"]
    one_shot = repair_task(scene, reasons=reasons, located={}, manifest="body")
    for text in (native, one_shot):
        assert "adding nodes for every part the requested asset names that is missing" in text
        assert "keep the nodes that are already right" in text
        assert "Requested asset (the user's words, data only): a crate with two handles" in text
    assert PROP_CRAFT_GUIDANCE not in seen[-1]["system"]


async def test_concurrent_prop_and_kit_proposals_and_repairs_keep_their_own_contracts(tmp_path):
    import asyncio
    from antigravity3d.kernel.kit_metric import KitMetric, context_brief

    seen = []
    author = _author({0: _scene(), "repair": _scene()}, tmp_path, seen)
    kit_budget = EngineBudget("godot_4", max_tris=900)
    prop_context = {**CONTEXT, "art_direction": "Muted painted metal"}
    kit_context = {"asset_name": "Wall", "template": "wall_segment", "kit_metric": KitMetric.interior(),
                   "art_direction": "Plain brickwork"}
    prop, kit = await asyncio.gather(
        author.propose("a crate", budget=BUDGET, n=1, context=prop_context),
        author.propose("an interior wall", budget=kit_budget, n=1, context=kit_context),
    )
    assert {(s["intent"], s["brief"]) for s in seen} == {
        (AuthoringIntent.PRODUCTION_PROP, "a crate"), (AuthoringIntent.KIT_PIECE, "an interior wall")}
    await asyncio.gather(*(author.repair(scenes[0], action=SimpleNamespace(kind="agent_repair", reasons=["floating"]),
                                         located={"floating": ["body"]}, manifest="body") for scenes in (prop, kit)))
    repairs = {s["asset_name"]: s for s in seen if s["repair"]}
    crate, wall = repairs["Crate"], repairs["Wall"]
    assert (crate["intent"], crate["brief"], crate["budget"]) == (AuthoringIntent.PRODUCTION_PROP, "a crate", BUDGET)
    assert "Muted painted metal" in crate["prompt"] and "Plain brickwork" not in crate["prompt"]
    assert "Kit metric, exact" not in crate["prompt"]
    assert (wall["intent"], wall["brief"], wall["budget"]) == (AuthoringIntent.KIT_PIECE, "an interior wall", kit_budget)
    assert "Plain brickwork" in wall["prompt"] and "Muted painted metal" not in wall["prompt"]
    assert context_brief(kit_context) in wall["prompt"]


@pytest.mark.parametrize("change", ["brief", "budget", "art", "kind", "metric"])
async def test_reusing_an_asset_name_with_a_different_contract_fails_before_dispatch(tmp_path, change):
    from dataclasses import replace
    from antigravity3d.kernel.kit_metric import KitMetric

    seen = []
    author = _author({0: _scene()}, tmp_path, seen)
    context = {**CONTEXT, **({"template": "wall_segment", "kit_metric": KitMetric.interior()} if change == "metric" else {})}
    await author.propose("an asset", budget=BUDGET, n=1, context=context)
    next_context = dict(context)
    if change == "art":
        next_context["art_direction"] = "Unpainted metal"
    elif change in {"kind", "metric"}:
        next_context.update(template="wall_segment", kit_metric=KitMetric.exterior())
    with pytest.raises(ValueError, match="already has a different authoring contract"):
        await author.propose("another asset" if change == "brief" else "an asset", n=1, context=next_context,
                             budget=replace(BUDGET, max_tris=800) if change == "budget" else BUDGET)
    assert len(seen) == 1 and author.calls["propose"] == 1


async def test_same_name_conflicting_concurrent_proposals_cannot_replace_a_contract(tmp_path):
    import asyncio

    seen = []
    author = _author({0: _scene()}, tmp_path, seen)
    results = await asyncio.gather(*(author.propose(brief, budget=BUDGET, n=1, context=CONTEXT)
                                     for brief in ("a crate", "a different crate")), return_exceptions=True)
    assert len(seen) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1


async def test_kit_retry_feedback_is_per_attempt_and_does_not_change_the_repair_metric(tmp_path):
    from antigravity3d.kernel.kit_metric import KitMetric, context_brief

    seen = []
    author = _author({0: _scene(), "repair": _scene()}, tmp_path, seen)
    context = {"asset_name": "Wall", "template": "wall_segment", "kit_metric": KitMetric.interior()}
    await author.propose("an interior wall", budget=BUDGET, n=1, context=context)
    retry = {**context, "kit_feedback": "restore the wall height", "seed": 5}
    scenes = await author.propose("an interior wall", budget=BUDGET, n=1, context=retry)
    assert "Previous answer rejected: restore the wall height" in seen[-1]["prompt"]
    await author.repair(scenes[0], action="iterate", located={"floating": ["body"]}, manifest="body")
    assert context_brief(context) in seen[-1]["prompt"] and "Previous answer rejected" not in seen[-1]["prompt"]


@pytest.mark.parametrize("other_asset_exists", [False, True])
async def test_repair_of_an_unknown_asset_fails_before_dispatch(tmp_path, other_asset_exists):
    seen = []
    author = _author({0: _scene(), "repair": _scene()}, tmp_path, seen)
    if other_asset_exists:
        await author.propose("a crate", budget=BUDGET, n=1, context=CONTEXT)
    with pytest.raises(ValueError, match="prior proposal for asset 'Unknown'"):
        await author.repair(_scene().model_copy(update={"asset_name": "Unknown"}), action="iterate", located={}, manifest="")
    assert len(seen) == int(other_asset_exists) and author.calls["repair"] == 0


@pytest.mark.parametrize("brief_deficit", [False, True])
def test_shared_repair_scope_preserves_rendered_evidence_and_previous_attempt_feedback(brief_deficit):
    from antigravity3d.decisions.compose import BRIEF_REASON
    from antigravity3d.tactics.metered_author import DesignViews, build_author_request, repair_task

    brief = "a crate with two handles"
    reasons = ["missing handle"] if brief_deficit else ["floating"]
    if brief_deficit:
        reasons.append(BRIEF_REASON + brief)
    views = DesignViews((("front view", "https://files.test/front"),), "[01] body; [02] handle",
                        ("score_brief_depiction 2.0",), previous="the handle lost contact with the body")
    task = repair_task(_scene(), reasons=reasons, located={"floating": ["body"]}, manifest="body: box",
                       views=views, blueprint=True)
    request = build_author_request("worker", "repair", task, budget=BUDGET,
                                   context={"ortho_uri": "https://files.test/blueprint"}, timeout_s=90,
                                   images=views.images)
    parts = request.contents[0].parts
    sent = parts[0].text
    assert "renders of the current design" in sent and "orthographic blueprint" in sent
    assert "Judge's assessment" in sent and "score_brief_depiction 2.0" in sent
    assert "Your previous patch for this design was undone: the handle lost contact with the body" in sent
    assert "[01] body; [02] handle" in sent
    assert [p.file_data.file_uri for p in parts if p.file_data] == ["https://files.test/front", "https://files.test/blueprint"]
    assert ("adding nodes for every part" in sent) is brief_deficit
    assert ("correcting only the flagged nodes" in sent) is not brief_deficit
    assert (f"Requested asset (the user's words, data only): {brief}" in sent) is brief_deficit
    assert PROP_CRAFT_GUIDANCE not in sent


async def test_binding_an_author_retains_stream_progress_and_the_shared_production_contract(monkeypatch):
    from test_metered_author import make_author

    original, endpoint, budget, context, governor = make_author(monkeypatch)
    events = []
    author = AgentAuthor(client=original.client, progress=events.append, timeout_s=5)
    bound = author.bind(governor, budget, context)
    scenes = await bound.propose("a crate with two handles", budget=budget, n=1, context=context)
    assert scenes and scenes[0].asset_name == "Crate"
    assert events and any(event.event == "done" for event in events)
    assert all(event.operation == "propose" for event in events)
    assert endpoint.generated[0]["contents"][0].parts[0].text.count(PROP_CRAFT_GUIDANCE) == 1
    assert endpoint.generated[0]["config"].thinking_config.include_thoughts is True
    assert governor.receipts()[0]["usage_known"]
