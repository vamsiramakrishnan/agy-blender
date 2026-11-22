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
from antigravity3d.harness import agents as A
from antigravity3d.harness import tools as T

# What the budget protects: prompt waste, i.e. text the stage does not need on every model call (repeated rules, unused
# tools, JSON-in-a-string parameters). BLOCKOUT moved from 7,000 (measured 6,914) to 8,200 (measured 8,095) deliberately:
# the added 1,181 characters are the craft rules (harness.agents.CRAFT_RULES), which are what the model needs to model a
# production prop instead of a blockout (measured with the one-shot request: 15-21 parts and 272-356 triangles without
# them, 63-64 parts and 936-976 triangles with them). That is guidance, not waste, so it is inside the limit; the limit
# still stops anything else from growing (margin ~100 characters, as before).
# Round five moved BLOCKOUT 8,200 -> 11,400 (measured 11,364), TWEAK 6,600 -> 9,400 (9,334) and VARIANTS 15,500 -> 17,300
# (17,263), each for the same reasons, measured on this change: (1) the tool declarations carry the new shape parameters
# and kinds and the mirror / repeat shortcuts: apply_scene_ir's parameters 3,936 -> 5,352 characters, with the
# parameters' descriptions left out (they are in SHAPE_SEMANTICS; with them it was 5,856 before the shortcuts); (2) the
# system prompt of every geometry stage carries ir_schema.SHAPE_SEMANTICS (1,342: what each parameter and shortcut
# means and the kernel's limits, which no schema states); (3) BLOCKOUT's CRAFT_RULES grew 1,103 -> 1,613 (when to bevel,
# inset, lathe, extrude, mirror and repeat, and what each costs in triangles). All of it is what the stage needs to use
# the new shapes; the margin is ~100 characters again. LOOKDEV and EXPORT carry none of it and are unchanged.
#            stage: (before, budget) static chars per call
# 2026-11-22 (native sessions): BLOCKOUT 11,400 -> 11,900 and LOOKDEV 12,500 -> 12,650. BLOCKOUT's text now asks for
# the loop the command's author runs (render_preview shows the design's five badged views: look, compare with the
# request, patch, repeat; half the budget and plain boxes is not done; run_visual_qa names parts and views), measured
# 11,819; both stages say that an uncalibrated preview's scores are findings (at most two improvement rounds; any lookdev
# review lets it export, the command's delivery rule) and LOOKDEV that an image limit of 0 makes no texture (12,549).
STATIC_BUDGET = {"BLOCKOUT": (26_224, 11_900), "TWEAK": (25_351, 9_400), "VARIANTS": (25_946, 17_300),
                 "LOOKDEV": (26_256, 12_650), "EXPORT": (25_914, 2_000)}
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


def test_blockout_stage_text_carries_the_craft_rules_and_the_other_geometry_stages_do_not():
    assert A.CRAFT_RULES in A._system("BLOCKOUT") and "50 to 90 parts" in A._STAGE_TEXT["BLOCKOUT"]
    assert "exactly on the surface" in A.CRAFT_RULES and "no overlap" in A.CRAFT_RULES  # contact, not the 1-2 mm overlap
    assert "overlap them" not in A.CRAFT_RULES and "sink" not in A.CRAFT_RULES
    # TWEAK edits parameters and VARIANTS adds patch alternatives on an already built base: neither is asked for more parts
    for stage in ("TWEAK", "VARIANTS", "LOOKDEV", "EXPORT"):
        assert "Craft rules" not in A._system(stage), stage
    assert len(A._system("BLOCKOUT")) < 11_900 - 6_000  # the rules are text, the rest of the static size is the tool declarations
