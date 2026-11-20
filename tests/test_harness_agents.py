"""Stage agent factories (offline) and one live BLOCKOUT run (needs GEMINI_API_KEY)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time

import pytest
from google.antigravity import types
from google.antigravity.connections.local import local_connection as lc
from google.antigravity.tools.tool_runner import ToolRunner, ToolWithSchema
from google.antigravity.triggers import TriggerContext

from antigravity3d.harness import agents as A
from antigravity3d.harness import hooks as H
from antigravity3d.harness import session as S
from antigravity3d.harness import tools as T
from antigravity3d.models import tier
from test_harness_tools import make_session

STAGES = sorted(T.STAGE_TOOLS)


@pytest.mark.parametrize("stage", STAGES)
def test_stage_config_builds(tmp_path, stage):
    env = make_session(tmp_path)
    cfg = A.build_stage_agent(stage, env.sid, registry=env.reg)
    names = [f.__name__ for f in cfg.tools]
    assert 1 <= len(names) <= 5 and names == [f.__name__ for f in T.STAGE_TOOLS[stage]]
    enabled = set(cfg.capabilities.enabled_tools)
    subs = T.STAGE_SUBAGENTS[stage]
    assert enabled == {types.BuiltinTools.FINISH} | ({types.BuiltinTools.INVOKE_SUBAGENT} if subs else set())
    assert cfg.capabilities.enable_subagents is bool(subs) and [c.name for c in cfg.subagents] == list(subs)
    assert cfg.capabilities.allowed_subagents == (list(subs) if subs else None)
    assert cfg.capabilities.agent_behavior is types.AgentBehavior.MINIMAL
    assert isinstance(cfg.system_instructions, types.CustomSystemInstructions)
    assert cfg.capabilities.tool_output_truncation_config.max_tokens == A.TOOL_OUTPUT_MAX_TOKENS
    assert cfg.compaction_config.token_threshold == A.COMPACTION_TOKENS
    assert env.state.active_stage == T.TOOLSET_STAGE[stage] and env.sess().toolset == stage
    assert {type(h) for h in cfg.hooks} >= {H.InvariantAndSafetyGate, H.TelemetryHook, H.TracebackCompressorHook,
                                             H.VisionEvictionHook, H.QAStopHook, H.StateHeaderHook}
    assert cfg.workspaces == [str(env.sess().workdir)]
    # tools resolve this session from the pinned conversation id
    assert S.resolve(type("C", (), {"conversation_id": cfg.conversation_id})()).sid == env.sid
    assert "[STATE" in cfg.system_instructions.text and A._STAGE_TEXT[stage] in cfg.system_instructions.text


@pytest.mark.parametrize("stage", STAGES)
def test_model_and_thinking_follow_models_tier(tmp_path, stage):
    env = make_session(tmp_path)
    cfg = A.build_stage_agent(stage, env.sid, registry=env.reg)
    role = A.DEFAULT_ROLE[stage]
    text = [m for m in cfg.models if types.ModelType.TEXT in m.types]
    assert len(text) == 1
    t = tier(role)
    assert text[0].name == t.model and text[0].endpoint.options.thinking_level == t.thinking
    [proto] = [p for p in lc.build_models_proto(cfg.models) if p.name == t.model]
    assert proto.gemini_api_endpoint.options.thinking_level == t.thinking.value
    assert {s.name: s.model for s in cfg.subagents} == {n: tier("worker").model for n in T.STAGE_SUBAGENTS[stage]}


def test_role_mapping_and_override(tmp_path):
    env = make_session(tmp_path)
    assert A.DEFAULT_ROLE == {"BLOCKOUT": "builder", "TWEAK": "bulk", "VARIANTS": "builder", "LOOKDEV": "builder",
                              "EXPORT": "bulk", "PLAN": "director"}
    cfg = A.build_stage_agent("BLOCKOUT", env.sid, registry=env.reg, role="worker")
    assert cfg.models[0].name == tier("worker").model and cfg.models[0].endpoint.options.thinking_level.value == "low"
    with pytest.raises(ValueError):
        A.build_stage_agent("RIGGING", env.sid, registry=env.reg)


def test_subagents_are_read_only():
    subs = A._subagents()
    assert {s.name for s in subs} == set(T.SUBAGENT_TOOLS) >= {n for ns in T.STAGE_SUBAGENTS.values() for n in ns}
    for sub in subs:
        assert set(sub.capabilities.enabled_tools) == {types.BuiltinTools.FINISH}
        assert sub.capabilities.agent_behavior is types.AgentBehavior.MINIMAL
        assert {f.__name__ for f in sub.tools} <= T.READ_ONLY_TOOL_NAMES
        assert isinstance(sub.system_instructions, types.CustomSystemInstructions) and "never edit" in sub.system_instructions.text


def test_stage_prompt_states_rules_not_types():
    """Types live in the tool declarations; the prose keeps only what a schema cannot say, and only where IR is edited."""
    from antigravity3d import ir_schema

    for stage in STAGES:
        text = A._system(stage)
        assert "scene: {" not in text and "kind -> params" not in text and "Types below" not in text
        assert (ir_schema.IR_SEMANTICS in text) is (stage != "EXPORT")  # ids, units, material cross-field rules
        assert (ir_schema.OP_SEMANTICS in text) is (stage in {"BLOCKOUT", "TWEAK", "VARIANTS"})  # anchor ops
        assert ("invoke_subagent" in text) is bool(T.STAGE_SUBAGENTS[stage])
        assert all(n in text for n in T.STAGE_SUBAGENTS[stage])


def test_subagents_flag_disables_them_everywhere(tmp_path):
    env = make_session(tmp_path)
    for stage in STAGES:
        cfg = A.build_stage_agent(stage, env.sid, registry=env.reg, subagents=False)
        assert cfg.subagents == [] and cfg.capabilities.enabled_tools == [types.BuiltinTools.FINISH]
        assert "invoke_subagent" not in cfg.system_instructions.text


@pytest.mark.parametrize("stage", STAGES)
def test_tool_schemas_hide_ctx(tmp_path, stage):
    env = make_session(tmp_path)
    cfg = A.build_stage_agent(stage, env.sid, registry=env.reg)
    runner = ToolRunner(cfg._get_all_custom_tools())
    assert set(runner.tool_names) >= {f.__name__ for f in cfg.tools}
    for name in runner.tool_names:
        pub = runner.get_public_callable(name)
        if not isinstance(pub, ToolWithSchema):  # an explicit schema is sent as-is; checked below
            assert "ctx" not in inspect.signature(pub).parameters
        proto = lc.callable_to_tool_proto(runner.tools[name], tool_runner=runner)
        schema = json.loads(proto.parameters_json_schema)
        assert "ctx" not in schema.get("properties", {}) and "ToolContext" not in proto.parameters_json_schema
        assert proto.description and len(proto.description) < 300


async def test_agent_enters_safety_check_without_policies(tmp_path):
    """Agent.__aenter__ refuses write tools without a policy or decide hook; ours has the gate."""
    env = make_session(tmp_path)
    cfg = A.build_stage_agent("BLOCKOUT", env.sid, registry=env.reg)
    from google.antigravity.hooks.hook_runner import HookRunner

    assert HookRunner(hooks=cfg.hooks).pre_tool_call_decide_hooks


def test_director_config():
    cfg = A.build_director_agent()
    schema = json.loads(cfg.response_schema)
    assert set(schema["properties"]) == {"stages", "parts", "materials", "risks"}
    assert cfg.models[0].name == tier("director").model and cfg.models[0].endpoint.options.thinking_level.value == "high"
    assert cfg.tools == []
    A.AssetPlan.model_validate({"stages": ["BLOCKOUT"], "parts": [{"name": "body", "primitive": "box", "approx_size_m": [1, 1, 1]}],
                                "materials": [], "risks": []})


class _Conn:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_trigger_notification(self, content: str) -> None:
        self.sent.append(content)


async def test_bake_completion_trigger(tmp_path):
    env = make_session(tmp_path)
    trig = A.BakeCompletionTrigger(env.reg, env.sid)
    conn = _Conn()
    task = asyncio.create_task(trig(TriggerContext(conn)))
    trig.notify(A.CompletionEvent("bake", "normal+ao maps for body at 2048px"))
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(trig.notify, A.CompletionEvent("render", "cycles 256spp", ok=False), loop)
    for _ in range(50):
        if len(conn.sent) == 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.sent[0].startswith("[STATE ") and "[EVENT] bake finished: normal+ao" in conn.sent[0]
    assert "[EVENT] render FAILED" in conn.sent[1]
    hook = H.StateHeaderHook(env.reg, env.sid)
    from google.antigravity.hooks.hooks import SessionContext, TurnContext

    assert (await hook.run(TurnContext(SessionContext()), conn.sent[0])).allow


# --------------------------------------------------------------------------- live


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY")
async def test_live_blockout_crate(tmp_path):
    env = make_session(tmp_path, stage="BLOCKOUT")
    t0 = time.perf_counter()
    run = await A.run_stage(
        "BLOCKOUT", env.sid,
        "Build a simple wooden crate with a lid from exactly two boxes: a body 0.8 x 0.6 x 0.5 m standing on the ground, "
        "and a lid 0.84 x 0.64 x 0.06 m resting exactly on top of the body. Apply it in one apply_scene_ir call, then run_visual_qa.",
        registry=env.reg, role="worker", timeout_s=300)
    wall = time.perf_counter() - t0
    scene = env.sess().stage.base_ir()
    u = run.usage
    print(f"\nLIVE wall={wall:.1f}s tokens(prompt={u and u.prompt_token_count}, out={u and u.candidates_token_count}, "
          f"thoughts={u and u.thoughts_token_count}, total={u and u.total_token_count}) nodes={[n.id for n in scene.nodes]} "
          f"tier1={env.state.last_tier1_ok} tier15={env.state.last_tier15_score:.2f} stop_continuations={run.stop_continuations}\n"
          f"telemetry={json.dumps(run.telemetry)}\nfinal_text={run.text[:400]!r}\naudit={env.state.audit_deltas}")
    assert len(scene.nodes) >= 2
    assert env.state.last_tier1_ok
    assert not run.image_violations
    assert run.telemetry["calls"] >= 1
