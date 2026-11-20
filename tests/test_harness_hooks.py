"""Harness hooks driven through the SDK's own HookRunner dispatch (not just ``.run``)."""

from __future__ import annotations

import json

import pytest
from google.antigravity import types
from google.antigravity.hooks.hook_runner import HookRunner
from google.antigravity.tools.tool_runner import ToolRunner

from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.harness import hooks as H
from antigravity3d.harness import session as S
from antigravity3d.harness import tools as T
from test_harness_tools import CRATE, make_session, record_approval, simulate_engine_import


def runner_for(env) -> tuple[HookRunner, dict[type, object]]:
    hs = H.stage_hooks(env.reg, env.sid)
    return HookRunner(hooks=hs), {type(h): h for h in hs}


async def turn(runner: HookRunner, env):
    res, tctx = await runner.dispatch_pre_turn(H.with_state_header("go", env.sess()))
    assert res.allow
    return tctx


async def pre(runner: HookRunner, tctx, name, **args):
    res, _call, op = await runner.dispatch_pre_tool_call(tctx, types.ToolCall(name=name, args=args, id=f"call-{name}"))
    return res, op


def test_hooks_register_into_expected_slots(tmp_path):
    env = make_session(tmp_path)
    runner, by = runner_for(env)
    assert [type(h) for h in runner.pre_tool_call_decide_hooks] == [H.InvariantAndSafetyGate, H.VisionEvictionHook, H.TelemetryHook]
    assert [type(h) for h in runner.post_tool_call_hooks] == [H.VisionEvictionHook, H.TelemetryHook]
    assert [type(h) for h in runner.pre_turn_hooks] == [H.VisionEvictionHook, H.StateHeaderHook]
    assert [type(h) for h in runner.on_tool_error_hooks] == [H.TracebackCompressorHook]
    assert [type(h) for h in runner.stop_hooks] == [H.QAStopHook]


async def test_export_gate(tmp_path):
    env = make_session(tmp_path, stage="EXPORT")
    runner, _ = runner_for(env)
    tctx = await turn(runner, env)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    res, _ = await pre(runner, tctx, "export_engine_package", out_name="pkg")
    assert not res.allow and "tier15>=3.4" in res.message and "\n" not in res.message
    await T.run_visual_qa(env.ctx)
    assert not (await pre(runner, tctx, "export_engine_package", out_name="pkg"))[0].allow
    record_approval(env)
    res, _ = await pre(runner, tctx, "export_engine_package", out_name="pkg")
    assert res.allow


async def test_linter_gate(tmp_path):
    env = make_session(tmp_path, stage="EXPORT")
    runner, by = runner_for(env)
    tctx = await turn(runner, env)
    res, _ = await pre(runner, tctx, "run_bpy_script", source="import subprocess\nsubprocess.run(['rm', '-rf', '/'])")
    assert not res.allow and res.message.startswith("script rejected by linter") and "AG001" in res.message
    assert "\n" not in res.message
    res, _ = await pre(runner, tctx, "run_bpy_script", source="import bpy\nbpy.ops.mesh.primitive_cube_add()")
    assert res.allow
    assert by[H.InvariantAndSafetyGate].denials[0][0] == "run_bpy_script"


async def test_out_of_stage_tool_denied(tmp_path):
    env = make_session(tmp_path, stage="BLOCKOUT")
    runner, _ = runner_for(env)
    tctx = await turn(runner, env)
    res, _ = await pre(runner, tctx, "forge_pbr_material", material_id="m", prompt="oak")
    assert not res.allow and "not available in the BLOCKOUT stage" in res.message
    for ok in ("apply_scene_ir", "inspect_manifest", "run_usd_validator", "qa_probe"):  # stage + read-only subagent tools
        assert (await pre(runner, tctx, ok, ops="{}"))[0].allow, ok


async def test_builtin_safety(tmp_path):
    env = make_session(tmp_path)
    runner, _ = runner_for(env)
    tctx = await turn(runner, env)
    wd = env.sess().workdir
    assert not (await pre(runner, tctx, types.BuiltinTools.RUN_COMMAND, CommandLine="ls"))[0].allow
    assert (await pre(runner, tctx, "create_file", TargetFile=str(wd / "notes.txt")))[0].allow
    res, _ = await pre(runner, tctx, "create_file", TargetFile="/etc/passwd")
    assert not res.allow and "outside the session workdir" in res.message
    assert not (await pre(runner, tctx, "edit_file", TargetFile=str(wd / ".." / "escape.txt")))[0].allow
    res, _ = await pre(runner, tctx, "view_file", AbsolutePath=str(wd / "artifacts" / "preview_001.png"))
    assert not res.allow and "never loaded into context" in res.message
    assert (await pre(runner, tctx, "view_file", AbsolutePath=str(wd / "Crate.usda")))[0].allow


async def test_policy_gate_on_existing_image(tmp_path):
    env = make_session(tmp_path, client=FakeDecisionsClient({"policy_trademark_logo": 0.93}), stage="LOOKDEV")
    runner, _ = runner_for(env)
    tctx = await turn(runner, env)
    img = env.sess().artifacts_dir / "logo.png"
    from PIL import Image

    Image.new("RGB", (8, 8)).save(img)
    res, _ = await pre(runner, tctx, "apply_shader_diff", material_id="m", diff=json.dumps({"albedo_uri": str(img)}))
    assert not res.allow and "policy gate blocked logo.png" in res.message and "policy_trademark_logo" in res.message
    calls = len(env.client.calls)
    res, _ = await pre(runner, tctx, "apply_shader_diff", material_id="m", diff=json.dumps({"roughness": 0.4}))
    assert res.allow and len(env.client.calls) == calls  # nothing to screen: no extra Decisions call
    res, _ = await pre(runner, tctx, "apply_shader_diff", material_id="m", diff=json.dumps({"albedo_uri": "/nope/missing.png"}))
    assert not res.allow and "could not screen" in res.message


async def test_traceback_compression(tmp_path):
    env = make_session(tmp_path)
    runner, _ = runner_for(env)
    tctx = await turn(runner, env)
    _, op = await pre(runner, tctx, "apply_scene_ir", ops="{}")
    try:
        raise KeyError("set_transform: unknown node lid_hinge")
    except KeyError as e:
        res, msg = await runner.dispatch_on_tool_error(op, e)
    assert res.allow and isinstance(msg, str) and "\n" not in msg and len(msg) <= 240
    assert msg.startswith("KeyError") and "inspect_manifest" in msg
    err = types.ToolExecutionError("Traceback (most recent call last):\n  File \"x.py\", line 3\nValueError: box size must be > 0",
                                   tool_name="apply_scene_ir")
    _, msg = await runner.dispatch_on_tool_error(op, err)
    assert "\n" not in msg and "box size" in msg


async def test_stop_hook_continuation_is_bounded(tmp_path):
    env = make_session(tmp_path)
    runner, by = runner_for(env)
    tctx = await turn(runner, env)
    res = await runner.dispatch_stop(tctx, types.StopArgs(continuation_count=0))
    assert res.decision == types.StopDecision.CONTINUE and "scene is empty" in res.reason and "\n" not in res.reason
    assert (await runner.dispatch_stop(tctx, types.StopArgs(continuation_count=1))).decision == types.StopDecision.CONTINUE
    assert (await runner.dispatch_stop(tctx, types.StopArgs(continuation_count=2))).decision == types.StopDecision.ALLOW_STOP
    assert (await runner.dispatch_stop(tctx, types.StopArgs(error_message="boom"))).decision == types.StopDecision.ALLOW_STOP
    budget = types.StopArgs(stop_reason=types.StopReason.MAX_TOOL_CALLS_EXCEEDED)
    assert (await runner.dispatch_stop(tctx, budget)).decision == types.StopDecision.ALLOW_STOP
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    assert (await runner.dispatch_stop(tctx, types.StopArgs())).decision == types.StopDecision.ALLOW_STOP
    assert by[H.QAStopHook].continued == 2


async def test_stop_hook_lookdev_and_export_gates(tmp_path, monkeypatch):
    simulate_engine_import(monkeypatch)
    env = make_session(tmp_path, stage="LOOKDEV")
    runner, _ = runner_for(env)
    tctx = await turn(runner, env)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    res = await runner.dispatch_stop(tctx, types.StopArgs())
    assert res.decision == types.StopDecision.CONTINUE and "visual QA not passing" in res.reason
    await T.run_visual_qa(env.ctx)
    assert (await runner.dispatch_stop(tctx, types.StopArgs())).decision == types.StopDecision.CONTINUE
    record_approval(env)
    assert (await runner.dispatch_stop(tctx, types.StopArgs())).decision == types.StopDecision.ALLOW_STOP
    env2 = make_session(tmp_path, sid="s2", stage="EXPORT")
    r2, _ = runner_for(env2)
    t2 = await turn(r2, env2)
    assert (await r2.dispatch_stop(t2, types.StopArgs())).decision == types.StopDecision.ALLOW_STOP  # blocked export: no loop
    await T.apply_scene_ir(json.dumps(CRATE), env2.ctx)
    await T.run_visual_qa(env2.ctx)
    record_approval(env2)
    res = await r2.dispatch_stop(t2, types.StopArgs())
    assert res.decision == types.StopDecision.CONTINUE and "export_engine_package" in res.reason
    await T.export_engine_package(env2.ctx, "pkg")
    assert (await r2.dispatch_stop(t2, types.StopArgs())).decision == types.StopDecision.ALLOW_STOP


async def test_telemetry_pairs_and_writes_jsonl(tmp_path):
    env = make_session(tmp_path)
    runner, by = runner_for(env)
    tel: H.TelemetryHook = by[H.TelemetryHook]
    tctx = await turn(runner, env)
    tools = ToolRunner(T.STAGE_TOOLS["BLOCKOUT"])
    tools.set_context(env.ctx)
    for name, args in (("apply_scene_ir", {"ops": json.dumps(CRATE)}), ("inspect_manifest", {}),
                       ("apply_scene_ir", {"ops": "not json"})):
        call = types.ToolCall(name=name, args=args, id=f"id-{name}-{len(args)}")
        res, call, op = await runner.dispatch_pre_tool_call(tctx, call)
        assert res.allow
        [result] = await tools.process_tool_calls([call])
        await runner.dispatch_post_tool_call(op, result)
    # harness path: post-tool arrives with a fresh OperationContext, paired by call id
    _, _, _ = await runner.dispatch_pre_tool_call(tctx, types.ToolCall(name="undo_last_edit", id="live-1"))
    fresh = type(op)(tctx)
    await runner.dispatch_post_tool_call(fresh, types.ToolResult(name="undo_last_edit", id="live-1", error="boom"))
    summ = tel.summary()
    assert summ["calls"] == 4 and summ["errors"] == 1 and summ["soft_failures"] == 1
    assert summ["by_tool"]["apply_scene_ir"]["calls"] == 2 and summ["by_tool"]["apply_scene_ir"]["soft_failures"] == 1
    lines = [json.loads(x) for x in tel.path.read_text().splitlines()]
    assert len(lines) == 4 and all(rec["ms"] is not None and rec["ms"] >= 0 for rec in lines)
    assert lines[-1] == {**lines[-1], "tool": "undo_last_edit", "error": True}
    assert tel.path.parent == env.sess().workdir and not tel._pending


async def test_state_header_required_on_turns(tmp_path):
    env = make_session(tmp_path)
    runner, _ = runner_for(env)
    res, _ = await runner.dispatch_pre_turn("build a crate")
    assert not res.allow and "[STATE" in res.message
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    await T.run_visual_qa(env.ctx)
    hdr = H.state_header(env.sess())
    assert hdr.startswith("[STATE stage=BLOCKOUT tools=BLOCKOUT engine=unreal_5") and "nodes=2" in hdr
    assert "tier1=ok tier15=3.60 export=blocked" in hdr and "QA[blockout] approve" in hdr
    res, tctx = await runner.dispatch_pre_turn(H.with_state_header("continue", env.sess()))
    assert res.allow and tctx.get_state("agy3d.header_seen")


async def test_vision_eviction(tmp_path):
    env = make_session(tmp_path)
    runner, by = runner_for(env)
    png = (env.sess().artifacts_dir / "ref.png")
    from PIL import Image

    Image.new("RGB", (4, 4), (255, 0, 0)).save(png)
    img = types.Image.from_file(png)
    res, _ = await runner.dispatch_pre_turn([H.state_header(env.sess()), "match this", img])
    assert not res.allow and "uploaded as fake://files/" in res.message
    assert env.sess()._res("user_image_uris")
    clean, uris = await H.sanitize_prompt(["match this", img], env.sess())
    assert clean[1].startswith("[image uploaded: fake://") and len(uris) == 1
    assert not any(isinstance(p, types.Image) for p in clean)
    tctx = await turn(runner, env)
    _, op = await pre(runner, tctx, "render_preview")
    import base64

    leaked = types.ToolResult(name="render_preview", id="call-render_preview", result=base64.b64encode(png.read_bytes()).decode())
    await runner.dispatch_post_tool_call(op, leaked)
    assert by[H.VisionEvictionHook].violations == ["render_preview"]
    assert not (await pre(runner, tctx, types.BuiltinTools.GENERATE_IMAGE, Prompt="crate"))[0].allow


@pytest.mark.parametrize("value,expected", [("OK tx=1 ops=2", False), (b"\x89PNG", True), ("data:image/png;base64,AAAA", True),
                                            ({"x": ["iVBORw0KGgoAAAANS"]}, True)])
def test_image_detector(value, expected):
    assert H._looks_like_image(value) is expected


async def test_gate_decide_is_the_shared_decision_path(tmp_path):
    """The MCP server calls ``decide(name, args)``; the SDK hook goes through the same function."""
    env = make_session(tmp_path, stage="BLOCKOUT")
    gate = H.InvariantAndSafetyGate(env.reg, env.sid)
    for name, args in (("export_engine_package", {"out_name": "p"}), ("forge_pbr_material", {"material_id": "m", "prompt": "x"}),
                       ("run_bpy_script", {"source": "import os\nos.remove('x')\n"}), ("apply_scene_ir", {"ops": "{}"})):
        direct = await gate.decide(name, args)
        via_hook = await gate._decide(types.ToolCall(name=name, args=args, id="c"))
        assert (direct.allow, direct.message) == (via_hook.allow, via_hook.message), name
    assert not (await gate.decide("forge_pbr_material", {})).allow
    assert "BLOCKOUT" in (await gate.decide("forge_pbr_material", {})).message
    assert (await gate.decide("apply_scene_ir", {"ops": "{}"})).allow


async def test_twenty_tool_turns_admit_no_inline_image_history(tmp_path):
    """Replay the host's exact pre-turn and tool pipelines with a new attached PNG each turn."""
    import io
    from PIL import Image

    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps(CRATE), env.ctx)
    runner, hooks = runner_for(env)
    tools = ToolRunner(T.STAGE_TOOLS["BLOCKOUT"])
    tools.set_context(env.ctx)
    history = []
    for i in range(20):
        png = io.BytesIO()
        Image.new("RGB", (8, 8), (i, 35, 50)).save(png, "PNG")
        image = types.Image(data=png.getvalue(), mime_type="image/png")
        clean, uris = await H.sanitize_prompt([f"Review frame {i}", image], env.sess())
        prompt = H.with_state_header(clean, env.sess())
        decision, turn_ctx = await runner.dispatch_pre_turn(prompt)
        assert decision.allow and len(uris) == 1
        name = "run_visual_qa" if i % 2 else "render_preview"
        call = types.ToolCall(name=name, args={}, id=f"turn-{i}")
        decision, call, op = await runner.dispatch_pre_tool_call(turn_ctx, call)
        assert decision.allow
        [result] = await tools.process_tool_calls([call])
        await runner.dispatch_post_tool_call(op, result)
        history.extend([*prompt, result.result])
        assert all(isinstance(part, str) and not H._looks_like_image(part) for part in history)
    assert len(env.backend.uploads) >= 20
    assert hooks[H.VisionEvictionHook].violations == []


async def test_gate_screens_typed_ir_arguments(tmp_path):
    """The gate decides on the same facts whether the IR arrives typed or as a legacy JSON string."""
    env = make_session(tmp_path, client=FakeDecisionsClient({"policy_trademark_logo": 0.93}), stage="LOOKDEV")
    gate = H.InvariantAndSafetyGate(env.reg, env.sid)
    img = env.sess().artifacts_dir / "logo.png"
    from PIL import Image

    Image.new("RGB", (8, 8)).save(img)
    for diff in ({"albedo_uri": str(img)}, json.dumps({"albedo_uri": str(img)})):
        res = await gate.decide("apply_shader_diff", {"material_id": "m", "diff": diff})
        assert not res.allow and "policy gate blocked logo.png" in res.message
    assert (await gate.decide("apply_shader_diff", {"material_id": "m", "diff": {"roughness": 0.4}})).allow
    S.set_toolset(env.reg, env.sid, "BLOCKOUT")
    op = {"op": "upsert_material", "material": {"id": "m", "orm_uri": str(img)}}
    for ops in ([op], json.dumps({"ops": [op]}), json.dumps({"nodes": [], "materials": [op["material"]]})):
        res = await gate.decide("apply_scene_ir", {"ops": ops})
        assert not res.allow and "logo.png" in res.message, ops


async def test_gate_enforces_stage_subagents(tmp_path):
    """allowed_subagents is not enforced by the harness at call time; the gate allows only the stage's own."""
    env = make_session(tmp_path, stage="LOOKDEV")
    gate = H.InvariantAndSafetyGate(env.reg, env.sid)

    def inv(*names):
        return {"Subagents": [{"TypeName": n, "Role": "r", "Prompt": "p"} for n in names]}

    assert (await gate.decide("invoke_subagent", inv("lookdev_critic", "usd_verifier"))).allow
    for bad in (inv("self"), inv("research"), inv("rogue"), inv("lookdev_critic", "self"), {"Subagents": []}):
        res = await gate.decide("invoke_subagent", bad)
        assert not res.allow and "not available in the LOOKDEV stage" in res.message, bad
    S.set_toolset(env.reg, env.sid, "BLOCKOUT")
    res = await gate.decide("invoke_subagent", inv("usd_verifier"))
    assert not res.allow and "available: none" in res.message


async def test_lookdev_header_lists_the_nodes_by_material(tmp_path):
    """LOOKDEV assigns materials to nodes by id and has no manifest tool: its header carries the ids."""
    env = make_session(tmp_path)
    await T.apply_scene_ir(json.dumps({"ops": [
        {"op": "upsert_material", "material": {"id": "wood", "base_color": [0.5, 0.3, 0.2]}},
        *CRATE["ops"],
        {"op": "assign_material", "node_id": "body", "material_id": "wood"},
    ]}), env.ctx)
    assert "bound:" not in H.state_header(env.sess())  # geometry stages have inspect_manifest instead
    S.set_toolset(env.reg, env.sid, "LOOKDEV")
    hdr = H.state_header(env.sess())
    line = next(x for x in hdr.splitlines() if x.startswith("bound: "))
    assert line == "bound: wood=body; (none)=lid"
    assert hdr.splitlines()[0].startswith("[STATE ") and hdr.splitlines()[0].endswith("]")  # the status line is intact

    class Node:
        def __init__(self, i):
            self.id, self.kind, self.material_id = f"plank_{i:04d}", "box", "wood"

    class Scene:
        nodes = [Node(i) for i in range(400)]

    long = H._bindings_line(Scene)
    assert len(long) <= H.BINDINGS_CAP + 40 and long.endswith("more node(s)")
    shown = long.count("plank_")
    assert 0 < shown < 400 and f"+{400 - shown} more node(s)" in long
