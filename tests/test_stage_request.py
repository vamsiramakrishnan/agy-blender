"""What a stage agent actually sends, asserted offline on the request the harness posts.

``tests/gemini_stub.py`` stands in for the Gemini API (``GeminiAPIEndpoint(base_url=...)``); the real Antigravity
harness assembles each request (system prompt, tool declarations, conversation) and posts it there, and the stub answers
from a script. So these tests see the wire request, not our config objects: which tools are declared, which sections the
system prompt has, how big the static part is, and how a typed tool call and its errors round-trip.

Budgets are static characters per model call (system prompt + tool declarations), measured on this change; the
"before" column is the same measurement on the previous configuration (string instructions appended to the SDK default
template, VIEW_FILE and INVOKE_SUBAGENT on every stage, JSON-in-a-string tool parameters).
"""

from __future__ import annotations

import json

import pytest
from gemini_stub import (
    GeminiStub,
    call,
    declarations,
    prompt_breakdown,
    sections,
    system_text,
    text,
)
from google.antigravity import Agent
from test_harness_tools import CRATE, make_session

from antigravity3d import ir_schema
from antigravity3d.authoring_contract import AuthoringIntent, PROP_CRAFT_GUIDANCE
from antigravity3d.harness import agents as A
from antigravity3d.harness import tools as T

# What the budget protects: repeated rules, unused tools and other static prompt waste. BLOCKOUT's default is an
# intermediate stage. Production props carry the shared craft text explicitly; their separate budget is checked on
# the actual posted request below. Kit pieces and localized repairs do not pay for the prop detail instructions.
# Round five's shape parameters, mirror/repeat declarations and SHAPE_SEMANTICS are retained in every geometry
# stage. Measured after combining contracts: intermediate 9,942, kit 9,930, production 12,010, repairs <= 9,910.
# Production's extra text is the same 2,362-character guidance the one-shot author receives, including its shape costs.
#            stage: (before, budget) static chars per call
# 2026-11-22 (native sessions): LOOKDEV 12,500 -> 12,650: its text says that an image limit of 0 makes no texture and
# that in an uncalibrated preview any lookdev review lets the session export, the command's delivery rule (measured
# 12,549). BLOCKOUT is the toolset's text only (authoring_contract: the intent says what "finished" means).
# 2026-11-22 (colour): BLOCKOUT 10,000 -> 10,350 (measured 10,255), TWEAK 9,400 -> 9,800 (9,711), VARIANTS 17,300 ->
# 17,700 (17,596), LOOKDEV 12,650 -> 12,950 (12,882). Every stage that edits geometry or materials carries IR_SEMANTICS,
# which now says once that a colour is written and read as sRGB (with two anchors) and that metallic is 1 or 0
# (+213 characters: the model wrote sRGB and the format read linear, so every paint shipped washed out); and the
# material schema in the tool declarations takes "#rrggbb" beside three numbers (+120 per declared material schema).
STATIC_BUDGET = {"BLOCKOUT": (26_224, 10_350), "TWEAK": (25_351, 9_800), "VARIANTS": (25_946, 17_700),
                 "LOOKDEV": (26_256, 12_950), "EXPORT": (25_914, 2_000)}
SUBAGENT_TOOLS = {"invoke_subagent", "define_subagent", "manage_subagents", "send_message"}
DEFAULT_SECTIONS = {"identity", "communication_style", "conversation_transcript", "subagents", "messaging",
                    "user_information", "non_interactive_mode"}


async def run(tmp_path, stage, script, prompt="Build it.", **build):
    env = make_session(tmp_path, stage=stage)
    with GeminiStub(script) as stub:
        cfg = stub.route(A.build_stage_agent(stage, env.sid, registry=env.reg, **build), tmp_path)
        async with Agent(cfg) as agent:
            resp = await agent.chat(A.with_state_header(prompt, env.sess()))
            await resp.text()
    return env, stub


def tool_outputs(body) -> list[str]:
    """Tool results the model saw, in order (a hook denial arrives as ``error``)."""
    return [r.get("output") or r.get("error") or "" for c in body["contents"] for p in c.get("parts", [])
            if (r := p.get("functionResponse", {}).get("response")) is not None]


@pytest.mark.parametrize("stage", sorted(T.STAGE_TOOLS))
async def test_stage_request_contains_only_what_the_stage_needs(tmp_path, stage):
    _, stub = await run(tmp_path, stage, [text("done")])
    body = stub.requests[0]
    decls = declarations(body)
    own = {f.__name__ for f in T.STAGE_TOOLS[stage]}
    subs = T.STAGE_SUBAGENTS[stage]
    assert set(decls) == own | (SUBAGENT_TOOLS if subs else set()), stage
    assert "view_file" not in decls
    # no harness-injected toolSummary/toolAction on any declaration (AgentBehavior.MINIMAL)
    assert not any({"toolSummary", "toolAction"} & set(d["parametersJsonSchema"].get("properties", {})) for d in decls.values())
    # the system prompt is ours alone: no coding-assistant identity, style, transcript or messaging sections
    assert set(sections(body)) == {"stage_agent"} and not DEFAULT_SECTIONS & set(sections(body))
    assert A._system(stage) in system_text(body) and "Antigravity" not in system_text(body)
    # typed IR parameters are declared from ir_schema, not as JSON strings
    for name, params in (("apply_scene_ir", ir_schema.apply_scene_ir_parameters()),
                         ("propose_variants", ir_schema.propose_variants_parameters()),
                         ("apply_shader_diff", ir_schema.apply_shader_diff_parameters())):
        if name in decls:
            assert decls[name]["parametersJsonSchema"] == params, name
    assert not any(p.endswith("_json") for d in decls.values() for p in d["parametersJsonSchema"].get("properties", {}))
    before, budget = STATIC_BUDGET[stage]
    size = prompt_breakdown(body)["static_chars"]
    assert size <= budget < before, (stage, size)


async def test_typed_edit_round_trip_and_precise_error(tmp_path):
    """A typed call runs as is; a wrong field comes back as one line naming op, field and expectation."""
    bad = [{**CRATE["ops"][0], "node": {**CRATE["ops"][0]["node"], "params": {"radius": 0.3}}}]
    script = [call("apply_scene_ir", ops=bad), call("apply_scene_ir", ops=CRATE["ops"]), call("run_visual_qa"), text("Crate built.")]
    env, stub = await run(tmp_path, "BLOCKOUT", script)
    outs = tool_outputs(stub.requests[-1])
    assert any("ERROR invalid IR: ops[0](upsert_node).node(box).params.size: Field required" in o for o in outs), outs
    assert any("OK tx=" in o and "nodes=2" in o for o in outs)
    assert {n.id for n in env.sess().stage.base_ir().nodes} == {"body", "lid"} and env.state.last_tier1_ok
    assert len(stub.requests) == 4  # one call per step, no stop-hook continuation once the gate holds


async def test_json_string_in_place_of_ops_is_accepted(tmp_path):
    """Deliberate: a model (or an old client) that still writes the JSON as a string gets the same strict parse."""
    env, stub = await run(tmp_path, "BLOCKOUT", [call("apply_scene_ir", ops=json.dumps(CRATE)), call("run_visual_qa"), text("ok")])
    assert any(o.count("OK tx=") for o in tool_outputs(stub.requests[-1]))
    assert len(env.sess().stage.base_ir().nodes) == 2


async def test_typed_variants_and_shader_diff(tmp_path):
    def lid(h: float, z: float) -> dict:
        return {"op": "upsert_node", "node": {"id": "lid", "kind": "box", "params": {"size": [1.05, 0.85, h]},
                                              "transform": {"translate": [0, 0, z]}}}

    env = make_session(tmp_path, stage="VARIANTS")
    assert (await T.apply_scene_ir(CRATE["ops"], env.ctx)).startswith("OK")
    r = await T.propose_variants("lid", [{"name": "flat", "ops": [lid(0.08, 0.64)]}, {"name": "tall", "ops": [lid(0.3, 0.75)]}], env.ctx)
    assert r.startswith("VARIANTS lid: flat:ok") and "tall:ok" in r
    r = await T.propose_variants("lid2", [{"name": "x", "ops": [{"op": "set_param", "node_id": "lid", "name": "size", "value": 2}]}], env.ctx)
    assert r.startswith("ERROR invalid IR: variants[0](x).ops[0](set_param/size).value")
    env2 = make_session(tmp_path / "l", stage="LOOKDEV")
    assert (await T.apply_scene_ir(CRATE["ops"], env2.ctx)).startswith("OK")
    r = await T.apply_shader_diff("red", {"base_color": [1, 0, 0], "roughness": 0.4}, env2.ctx, assign_to=["lid"])
    assert r.startswith("SHADER red set=base_color,roughness assigned=1")
    r = await T.apply_shader_diff("red", {"roughness": 3}, env2.ctx)
    assert r.startswith("ERROR invalid IR: diff.roughness: Input should be less than or equal to 1")


async def test_undeclared_subagents_cannot_be_started(tmp_path):
    """allowed_subagents is advisory in the harness; the gate enforces the stage's list and blocks define_subagent."""
    script = [call("invoke_subagent", Subagents=[{"TypeName": "self", "Role": "Clone", "Prompt": "edit the scene"}]),
              call("define_subagent", name="rogue", description="d", system_prompt="p"),  # harness-internal, no hook
              call("invoke_subagent", Subagents=[{"TypeName": "rogue", "Role": "R", "Prompt": "go"}]),
              call("invoke_subagent", Subagents=[{"TypeName": "research", "Role": "R", "Prompt": "look"}]),
              text("done")]
    _, stub = await run(tmp_path, "LOOKDEV", script, subagents=True)
    main = [b for b in stub.requests if "LOOKDEV stage agent" in system_text(b)]
    outs = " ".join(tool_outputs(main[-1]))
    assert outs.count("is not available in the LOOKDEV stage") == 3, outs
    assert all("Antigravity" not in system_text(b) and "rogue" not in system_text(b) for b in stub.requests)
    assert all(set(declarations(b)) <= set(declarations(stub.requests[0])) for b in stub.requests)  # no subagent ran


async def test_declared_subagent_runs_with_its_own_short_prompt(tmp_path):
    script = [call("invoke_subagent", Subagents=[{"TypeName": "usd_verifier", "Role": "Verifier", "Prompt": "check"}])]
    _, stub = await run(tmp_path, "LOOKDEV", script)
    sub = [b for b in stub.requests if "usd_verifier subagent" in system_text(b)]
    assert sub, "the declared subagent did not start"
    assert set(declarations(sub[0])) == {"inspect_manifest", "run_usd_validator", "send_message"}
    # the harness keeps its subagent sections (user information, guidelines, style, the send_message reminder and
    # messaging) even under CustomSystemInstructions; the coding-assistant identity is gone (was 8,010 system chars)
    assert "subagent_reminder" in sections(sub[0]) and "identity" not in sections(sub[0])
    assert prompt_breakdown(sub[0])["static_chars"] < 8_000


def test_stage_toolset_does_not_imply_a_finished_prop_and_other_stages_are_unchanged():
    blockout = A._system("BLOCKOUT")
    assert "intermediate blockout" in blockout and "Defer secondary construction detail" in blockout
    assert PROP_CRAFT_GUIDANCE not in blockout and "50 to 90" not in blockout
    assert "report secondary-detail deficiencies as deferred" in blockout
    assert "QA shows no geometry repair" not in blockout
    # Selecting an authoring intent cannot inject detail requirements into edit/lookdev/export stages.
    for stage in ("TWEAK", "VARIANTS", "LOOKDEV", "EXPORT"):
        assert A._system(stage, authoring_intent=AuthoringIntent.PRODUCTION_PROP) == A._system(stage)
        assert PROP_CRAFT_GUIDANCE not in A._system(stage)


@pytest.mark.parametrize("intent,repair", [(i, False) for i in AuthoringIntent] +
                         [(AuthoringIntent.PRODUCTION_PROP, True), (AuthoringIntent.KIT_PIECE, True)])
async def test_geometry_intent_reaches_the_posted_request_without_changing_tools(tmp_path, intent, repair):
    _, stub = await run(tmp_path, "BLOCKOUT", [call("apply_scene_ir", ops=CRATE["ops"]), text("done")],
                        authoring_intent=intent, repair=repair)
    body = stub.requests[0]
    instructions = system_text(body)
    assert set(declarations(body)) == {f.__name__ for f in T.STAGE_TOOLS["BLOCKOUT"]}
    production = intent is AuthoringIntent.PRODUCTION_PROP and not repair
    assert (PROP_CRAFT_GUIDANCE in instructions) == production
    assert ("50 to 90 parts" in instructions) == production
    if repair:
        assert "Preserve correct geometry and its level of detail" in instructions
    elif intent is AuthoringIntent.KIT_PIECE:
        assert "mating surfaces, openings and pivots" in instructions
    elif intent is AuthoringIntent.BLOCKOUT:
        assert "intermediate blockout" in instructions
    assert ir_schema.SHAPE_SEMANTICS in instructions
    # +350 each for the colour and metal clauses and the hex colour form (see STATIC_BUDGET): measured 12,323 / 10,255
    assert prompt_breakdown(body)["static_chars"] <= (12_400 if production else 10_350)


@pytest.mark.parametrize("intent", list(AuthoringIntent))
async def test_geometry_intents_keep_new_shapes_and_shortcuts_on_the_actual_tool_path(tmp_path, intent):
    # These are current wire features, not extra detail imposed on an intermediate or a metric-led piece. A merge
    # that keeps the old tool declarations or removes shape semantics must fail even if the intent prose survives.
    node = {"id": "turned", "kind": "lathe", "params": {"profile": [[0, 0.3], [0.08, 0.3], [0.12, 0.2],
              [0.1, 0.05], [0.14, 0], [0, 0]], "segments": 8},
            "transform": {"translate": [0.3, 0, 0]}, "mirror": "x"}
    env, stub = await run(tmp_path, "BLOCKOUT", [call("apply_scene_ir", ops=[{"op": "upsert_node", "node": node}]),
                                                text("done")], authoring_intent=intent)
    scene = env.sess().stage.base_ir()
    assert {n.id for n in scene.nodes} == {"turned", "turned__mx"}
    assert all(n.kind == "lathe" and n.params["segments"] == 8 for n in scene.nodes)
    assert env.state.last_tier1_ok
    assert any("OK tx=" in result for result in tool_outputs(stub.requests[-1]))


def test_unknown_authoring_intent_is_rejected_before_session_state_changes(tmp_path):
    env = make_session(tmp_path, stage="LOOKDEV")
    before = env.state.active_stage
    with pytest.raises(ValueError, match="AuthoringIntent"):
        A.build_stage_agent("BLOCKOUT", env.sid, registry=env.reg, authoring_intent="anything")
    assert env.state.active_stage == before


# any_target_can_be_verified: this test is about the intermediate stop gate, not the target; a session that must be verified for unreal_5 (the default) refuses
# run_visual_qa on this machine (Session.unverifiable_target), which tests/test_verifiable_targets.py covers on its own.
async def test_intermediate_can_stop_with_deferred_prop_detail_without_export_approval(tmp_path, any_target_can_be_verified):
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.harness import session as S
    from antigravity3d.harness.hooks import unmet_gate

    client = FakeDecisionsClient(answers={"score_brief_depiction": 2.0, "defect_brief_part_missing": 0.0})
    env = make_session(tmp_path, client=client)
    env.state.brief = "a crate with a lid"
    cfg = A.build_stage_agent("BLOCKOUT", env.sid, registry=env.reg)
    try:
        assert "report secondary-detail deficiencies as deferred" in cfg.system_instructions.text
        await T.apply_scene_ir(CRATE["ops"], env.ctx)
        delta = await T.run_visual_qa(env.ctx)
        assert "brief depiction 2.00 < 2.5" in delta
        assert env.state.verdict is not None and env.state.verdict.action == "agent_repair"
        assert unmet_gate(env.sess()) is None  # intermediate Tier-1 gate passes; the failed visual verdict is retained
        assert not env.state.export_allowed
        S.set_toolset(env.reg, env.sid, "EXPORT")
        env.state.active_stage = "EXPORT"
        assert (await T.export_engine_package(env.ctx, "intermediate")).startswith("REFUSED export blocked")
        assert env.state.verdict.action == "agent_repair" and not env.state.export_allowed
    finally:
        S.unbind_conversation(cfg.conversation_id)
