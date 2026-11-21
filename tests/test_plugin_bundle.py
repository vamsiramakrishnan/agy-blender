"""The Antigravity plugin bundle: validation, install, and hook-script contracts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from antigravity3d import models
from antigravity3d.plugin_install import (
    PLUGIN_NAME,
    bundle_dir,
    install,
    load_sidecar_schema,
    register,
    schema_errors,
    validate,
)
from antigravity3d.runtime import qa_state
from test_harness_tools import CRATE, box, make_session, record_approval

BUNDLE = bundle_dir()
HOOKS = json.loads((BUNDLE / "hooks.json").read_text())


def _hook_command(event: str, tool: str | None = None) -> str:
    for spec in HOOKS.values():
        for item in spec.get(event, []):
            if "hooks" in item:
                if tool is None or _matches(item["matcher"], tool):
                    return item["hooks"][0]["command"]
            else:
                return item["command"]
    raise KeyError((event, tool))


def _matches(matcher: str, tool: str) -> bool:
    return matcher in ("*", "") or re.fullmatch(matcher, tool) is not None


def run_hook(event: str, payload: dict, state: Path, tool: str | None = None) -> dict:
    """Runs the hooks.json command exactly like the harness: `sh -c` from the plugin dir, JSON on stdin."""
    cmd = _hook_command(event, tool)
    env = {**os.environ, "AGY3D_STATE_DIR": str(state), "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
    proc = subprocess.run(["sh", "-c", cmd], input=json.dumps({"hookEventName": event, **payload}), text=True,
                          capture_output=True, cwd=BUNDLE, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def pre(state: Path, name: str, args: dict | None = None, conv: str = "conv-1", tid: str = "t1") -> dict:
    return run_hook("PreToolUse", {"conversationId": conv, "toolCall": {"id": tid, "name": name, "args": args or {}}},
                    state, tool=name)


def post(state: Path, name: str, conv: str = "conv-1", tid: str = "t1", error: str | None = None) -> dict:
    payload = {"conversationId": conv, "stepIdx": 7, "toolCall": {"id": tid, "name": name, "args": {}}}
    if error:
        payload["error"] = error
    return run_hook("PostToolUse", payload, state, tool=name)


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "state"
    monkeypatch.setenv("AGY3D_STATE_DIR", str(d))
    return d


# --------------------------------------------------------------------------- bundle


def test_bundle_validates_cleanly():
    assert validate(BUNDLE) == []


def test_bundle_layout_follows_plugin_docs():
    for rel in ("plugin.json", "hooks.json", "rules/AGENTS.md", "agents/usd_verifier.md", "agents/lookdev_critic.md",
                "workflows/batch_sanitize.py"):
        assert (BUNDLE / rel).is_file(), rel
    assert not (BUNDLE / "hooks" / "hooks.json").exists()
    skills = sorted(p.name for p in (BUNDLE / "skills").iterdir())
    assert skills == ["art-direction", "concept-to-blueprint", "engine-truth-verify", "game-ready-sanitizer", "parametric-kitbash",
                      "pbr-material-forge"]
    sidecars = sorted(p.name for p in (BUNDLE / "sidecars").iterdir())
    # no inspector sidecar: the studio MCP server serves the inspector as an MCP App
    assert sidecars == ["blender_pool", "depot_watcher", "nightly_variant_factory", "weekly_distill"]


def test_skills_name_their_tools():
    tools = {"apply_scene_ir", "inspect_manifest", "render_preview", "run_visual_qa", "forge_pbr_material",
             "apply_shader_diff", "run_paintover_loop", "generate_lods_and_colliders", "run_usd_validator",
             "export_engine_package"}
    used: set[str] = set()
    for md in (BUNDLE / "skills").glob("*/SKILL.md"):
        body = md.read_text()
        named = {t for t in tools if t in body}
        assert len(named) >= 2, md
        used |= named
    assert used == tools


def test_agents_pinned_to_worker_tier_and_read_only():
    w = models.tier("worker")
    expected_model = f"{w.model}-{w.thinking.value}"
    assert expected_model == "gemini-4-hydrogen-low"
    for name in ("usd_verifier", "lookdev_critic"):
        text = (BUNDLE / "agents" / f"{name}.md").read_text()
        fm = yaml.safe_load(text.split("\n---", 1)[0][3:])
        assert fm["name"] == name and fm["subagent"] is True and fm["mainAgent"] is False
        assert fm["model"] == expected_model
        assert not {"write_file", "edit_file", "run_command", "export_engine_package", "apply_scene_ir",
                    "apply_shader_diff", "run_visual_qa", "render_preview"} & set(fm["tools"])


def test_sidecars_use_documented_forms():
    pool = json.loads((BUNDLE / "sidecars/blender_pool/sidecar.json").read_text())
    assert pool["restart_policy"] == "always"
    assert " ".join([pool["command"], *pool["args"]]) == \
        "uv run python -m antigravity3d.runtime.worker_daemon --workers 4"
    assert set(pool["env"]) == {"BLENDER_BIN", "AGY3D_POOL_SOCKET"}
    assert not any(json.loads(p.read_text()).get("has_web_ui") for p in BUNDLE.glob("sidecars/*/sidecar.json"))
    for sched in ("nightly_variant_factory", "weekly_distill"):
        cfg = json.loads((BUNDLE / f"sidecars/{sched}/sidecar.json").read_text())
        assert cfg["builtin"] == "schedule" and "command" not in cfg
        assert cfg["args"][0].startswith("CRON_TZ=")
        assert cfg["args"][1:3] == ["agentapi", "new-conversation"] and cfg["args"][-2] == "--"
        assert cfg["agent_permissions"]["allows_user_interactions"] is False
        assert not any("*" in g for g in cfg["agent_permissions"].get("access_grants", []))


def test_validate_reports_broken_bundles(tmp_path):
    bad = tmp_path / PLUGIN_NAME
    shutil.copytree(BUNDLE, bad)
    sc = bad / "sidecars/blender_pool/sidecar.json"
    cfg = json.loads(sc.read_text())
    cfg.update(builtin="schedule", working_dir="/x", restart_policy="sometimes")
    sc.write_text(json.dumps(cfg))
    (bad / "skills/parametric-kitbash/SKILL.md").write_text("---\nname: wrong\ndescription: x\n---\nbody\n")
    hooks = json.loads((bad / "hooks.json").read_text())
    hooks["agy3d-stop-gate"]["Stop"][0]["command"] = "python3 ./hooks/scripts/nope.py"
    hooks["agy3d-stop-gate"]["BeforeTool"] = []
    (bad / "hooks.json").write_text(json.dumps(hooks))
    (bad / "hooks" / "hooks.json").write_text("{}")
    (bad / "workflows/bad.py").write_text("print('hi')\n")
    (bad / "agents/x.md").write_text("no frontmatter")
    (bad / "broken.json").write_text("{nope")
    errs = "\n".join(validate(bad))
    for needle in ("unknown property 'working_dir'", "'sometimes' not in", "exactly one of oneOf",
                   "frontmatter name must equal", "missing file ./hooks/scripts/nope.py", "BeforeTool: unknown event",
                   "hooks/hooks.json", "workflows/bad.py", "agents/x.md", "broken.json: invalid JSON"):
        assert needle in errs, needle


def test_mcp_config_follows_documented_format():
    cfg = json.loads((BUNDLE / "mcp_config.json").read_text())
    assert list(cfg) == ["mcpServers"] and list(cfg["mcpServers"]) == ["studio"]  # the harness adds the <plugin>_ prefix
    srv = cfg["mcpServers"]["studio"]
    assert srv["command"] == "uv" and srv["args"][-2:] == ["-m", "antigravity3d.runtime.mcp_server"]
    assert "${PLUGIN_ROOT}" in srv["args"] and set(srv) <= {"command", "args", "env"}
    for name in ("nightly_variant_factory", "depot_watcher"):
        grants = json.loads((BUNDLE / f"sidecars/{name}/sidecar.json").read_text())["agent_permissions"]["access_grants"]
        assert "mcp(antigravity-3d_studio/apply_scene_ir)" in grants and not any(g in ("mcp(*)",) for g in grants)


def test_validate_checks_mcp_config(tmp_path):
    bad = tmp_path / PLUGIN_NAME
    shutil.copytree(BUNDLE, bad)
    (bad / "mcp_config.json").write_text(json.dumps({"mcpServers": {
        "antigravity-3d-studio": {"command": "python3", "args": ["-m", "no_such_pkg.server"], "env": {"A": 1}, "cwd": "/x"},
        "both": {"command": "x", "url": "https://x"},
        "remote": {"url": "ftp://x"}, "a/b": {"command": "x"}}}))
    sc = bad / "sidecars/depot_watcher/sidecar.json"
    cfg = json.loads(sc.read_text())
    cfg["agent_permissions"]["access_grants"] += ["mcp(other_server/apply_scene_ir)"]
    sc.write_text(json.dumps(cfg))
    errs = "\n".join(validate(bad))
    for needle in ("do not repeat the plugin name", "no_such_pkg.server is not importable", "env must map strings",
                   "unknown field 'cwd'", "exactly one transport", "url must be an http(s) URL", "must not contain whitespace",
                   "mcp(other_server/apply_scene_ir)"):
        assert needle in errs, needle
    (bad / "mcp_config.json").write_text(json.dumps({"servers": {}}))
    assert "needs a non-empty 'mcpServers'" in "\n".join(validate(bad))


def test_vendored_schema_rejects_guessed_fields():
    schema = load_sidecar_schema()
    assert "SDK" in schema["$comment"] or "0.1.24" in schema["$comment"]
    ok = {"command": "python3", "args": ["x.py"], "restart_policy": "on-failure"}
    assert schema_errors(ok, schema) == []
    assert schema_errors({**ok, "working_dir": "/tmp"}, schema)
    assert schema_errors({"args": []}, schema)  # neither command nor builtin


def test_install_copy_symlink_and_personalise(tmp_path):
    root = tmp_path / ".agents"
    dest = install(root)
    assert dest == root / "plugins" / PLUGIN_NAME and (dest / "plugin.json").is_file() and not dest.is_symlink()
    assert validate(dest) == []
    assert not list(dest.rglob("__pycache__"))

    ws, depot = tmp_path / "ws", tmp_path / "depot"
    dest = install(root, workspace=ws, depot=depot)  # replaces the previous copy
    dw = json.loads((dest / "sidecars/depot_watcher/sidecar.json").read_text())
    assert dw["agent_permissions"]["workspace_uris"] == [ws.resolve().as_uri()]
    assert dw["env"]["AGY3D_DEPOT_DIR"] == str(depot.resolve())
    assert f"read_file({depot.resolve()})" in dw["agent_permissions"]["access_grants"]
    nightly = json.loads((dest / "sidecars/nightly_variant_factory/sidecar.json").read_text())
    assert nightly["agent_permissions"]["workspace_uris"] == [ws.resolve().as_uri()]
    assert validate(dest) == []

    dest = install(root, symlink=True)
    assert dest.is_symlink() and dest.resolve() == BUNDLE.resolve()
    assert validate(dest) == []

    foreign = tmp_path / "other" / "plugins" / PLUGIN_NAME
    foreign.mkdir(parents=True)
    (foreign / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):
        install(tmp_path / "other")


def test_register_writes_plugins_json(tmp_path):
    p = register(tmp_path)
    cfg = json.loads(p.read_text())
    assert cfg["entries"] == [{"path": str(BUNDLE.parent.resolve()), "include_only": [PLUGIN_NAME]}]
    register(tmp_path)
    assert len(json.loads(p.read_text())["entries"]) == 1


def test_workflow_script_passes_sdk_validator():
    from google.antigravity import workflows

    workflows.validate_workflow_source((BUNDLE / "workflows/batch_sanitize.py").read_text())


# --------------------------------------------------------------------------- hook matchers


def test_gate_matcher_targets_only_gated_tools():
    matcher = HOOKS["agy3d-qa-export-gate"]["PreToolUse"][0]["matcher"]
    for name in ("export_engine_package", "mcp_antigravity-3d_export_engine_package", "run_script", "run_bpy_script"):
        assert _matches(matcher, name), name
    for name in ("run_command", "view_file", "apply_scene_ir", "inspect_manifest"):
        assert not _matches(matcher, name), name


# --------------------------------------------------------------------------- hook contracts


def write_passing_receipt(**kwargs):
    from antigravity3d.state import VisualVerdict

    revision = "fixture-revision"
    return qa_state.write_qa_state(revision=revision, target="unreal_5", session_id="crate-session",
                                   verdict=VisualVerdict(revision, "unreal_5", "lookdev", "eevee", "decisions", "approve"),
                                   **kwargs)


def test_export_blocked_without_qa_state(state):
    out = pre(state, "export_engine_package")
    assert out["decision"] == "deny" and "no QA verdict" in out["reason"]


def test_export_allowed_with_passing_qa_then_blocked_after_edit(state):
    write_passing_receipt(tier1_ok=True, tier15_score=3.8, conversation_id="conv-1", asset="crate")
    out = pre(state, "export_engine_package", tid="e1")
    assert out == {"decision": "allow", "reason": out["reason"]} and "3.80" in out["reason"]

    assert post(state, "apply_scene_ir", tid="m1") == {}  # mutation after QA makes it stale
    out = pre(state, "export_engine_package", tid="e2")
    assert out["decision"] == "deny" and "apply_scene_ir" in out["reason"]

    write_passing_receipt(tier1_ok=True, tier15_score=3.5, conversation_id="conv-1")
    assert pre(state, "export_engine_package", tid="e3")["decision"] == "allow"


@pytest.mark.parametrize(("tier1", "score", "needle"), [(False, 4.5, "Tier-1"), (True, 3.39, "3.39 < 3.4")])
def test_export_blocked_when_gate_unmet(state, tier1, score, needle):
    qa_state.write_qa_state(tier1_ok=tier1, tier15_score=score, conversation_id="conv-1")
    out = pre(state, "export_engine_package")
    assert out["decision"] == "deny" and needle in out["reason"]


def test_export_gate_fails_closed_on_corrupt_state(state):
    (state / "qa").mkdir(parents=True)
    (state / "qa" / "latest.json").write_text("{corrupt")
    assert pre(state, "export_engine_package")["decision"] == "deny"


def test_mcp_steps_are_routed_to_the_gate_and_identified_by_tool_name(state):
    # MCP calls arrive under the generic step name `mcp_tool` (docs: lower-cased step type); the matcher
    # routes it here and the script identifies the tool from the arguments.
    matcher = HOOKS["agy3d-qa-export-gate"]["PreToolUse"][0]["matcher"]
    assert _matches(matcher, "mcp_tool")
    for key in ("ToolName", "toolName", "tool_name"):
        payload = {"conversationId": "conv-1", "toolCall": {"id": "t1", "name": "mcp_tool",
                   "args": {"ServerName": "antigravity-3d_studio", key: "export_engine_package"}}}
        assert run_hook("PreToolUse", payload, state, tool="mcp_tool")["decision"] == "deny"
    # a namespaced step name is recognised too
    out = run_hook("PreToolUse", {"conversationId": "conv-1", "toolCall": {"id": "t2", "name": "mcp_antigravity-3d_studio_export_engine_package",
                   "args": {}}}, state, tool="export_engine_package")
    assert out["decision"] == "deny"
    # raw scripts over MCP are linted, with the arguments in the JSON-string form too
    bad = {"conversationId": "conv-1", "toolCall": {"id": "t3", "name": "mcp_tool", "args": {
        "ServerName": "antigravity-3d_studio", "ToolName": "run_bpy_script", "ArgumentsJson": json.dumps({"source": "import bgl\n"})}}}
    assert run_hook("PreToolUse", bad, state, tool="mcp_tool")["decision"] == "deny"
    # this plugin's other tools are gated server-side and pass; foreign MCP servers keep the normal permission flow
    own = {"conversationId": "conv-1", "toolCall": {"id": "t4", "name": "mcp_tool", "args": {
        "ServerName": "antigravity-3d_studio", "ToolName": "apply_scene_ir"}}}
    assert run_hook("PreToolUse", own, state, tool="mcp_tool")["decision"] == "allow"
    foreign = {"conversationId": "conv-1", "toolCall": {"id": "t5", "name": "mcp_tool", "args": {
        "ServerName": "github", "ToolName": "create_issue"}}}
    assert run_hook("PreToolUse", foreign, state, tool="mcp_tool")["decision"] == "ask"


async def test_python_session_is_the_single_writer_of_the_hook_qa_state(tmp_path, state):
    """apply_scene_ir + render_preview + run_visual_qa flips the plugin's export gate to allow; a geometry edit flips it back."""
    from antigravity3d.harness import tools as T

    env = make_session(tmp_path)
    conv = env.ctx.conversation_id
    assert env.state.session_id == "s1"
    exp = lambda: pre(state, "export_engine_package", conv=conv, tid="x")  # noqa: E731
    assert exp()["decision"] == "deny"  # a fresh session writes a blank verdict

    assert (await T.apply_scene_ir(json.dumps(CRATE), env.ctx)).startswith("OK")
    assert exp()["decision"] == "deny" and "3.4" in exp()["reason"]  # Tier-1 ok, no visual score yet
    assert (await T.render_preview(env.ctx)).startswith("PREVIEW")
    assert (await T.run_visual_qa(env.ctx)).startswith("QA[blockout] approve")
    assert exp()["decision"] == "deny"
    record_approval(env)
    out = exp()
    assert out["decision"] == "allow" and "Tier-1 ok" in out["reason"], out
    qa = json.loads((state / "qa" / f"{conv}.json").read_text())
    assert qa["session_id"] == "s1" and qa["conversation_id"] == conv and qa["asset"] == "Crate" and qa["tier1_ok"]
    assert exp()["decision"] == "allow"
    assert pre(state, "export_engine_package", conv="some-other-conversation", tid="y")["decision"] == "deny"

    # geometry edit -> score reset to 0 -> deny again, with no post-tool hook involved
    assert (await T.apply_scene_ir(json.dumps({"ops": [box("lid", (1.05, 0.85, 0.1), 0.65)]}), env.ctx)).startswith("OK")
    out = exp()
    assert out["decision"] == "deny" and "0.00" in out["reason"]
    await T.run_visual_qa(env.ctx)
    assert exp()["decision"] == "deny"
    record_approval(env)
    assert exp()["decision"] == "allow"
    await T.undo_last_edit(env.ctx)
    assert exp()["decision"] == "deny"

    # a rolled-back (Tier-1 failing) edit leaves the verdict alone; a failed write never fails the tool
    await T.run_visual_qa(env.ctx)
    assert exp()["decision"] == "deny"
    record_approval(env)
    assert exp()["decision"] == "allow"


async def test_failed_qa_write_never_fails_the_tool_and_logs_once(tmp_path, state, monkeypatch, caplog):
    import antigravity3d.harness.session as S
    from antigravity3d.harness import tools as T

    env = make_session(tmp_path)

    def boom(**_kw):
        raise OSError("disk full")

    monkeypatch.setattr(S.qa_state, "write_qa_state", boom)
    monkeypatch.setattr(S, "_qa_write_warned", False)
    with caplog.at_level("WARNING", logger=S.log.name):
        assert (await T.apply_scene_ir(json.dumps(CRATE), env.ctx)).startswith("OK")
        assert (await T.apply_scene_ir(json.dumps({"ops": [box("knob", (0.1, 0.1, 0.1), 0.7)]}), env.ctx)).startswith("OK")
        assert (await T.run_visual_qa(env.ctx)).startswith("QA[")
    assert not env.state.export_allowed  # offline blockout cannot authorize publication
    assert len([r for r in caplog.records if "could not persist QA state" in r.message]) == 1


def test_qa_state_does_not_leak_between_sessions(tmp_path, state):
    import asyncio

    from antigravity3d.harness import tools as T

    a = make_session(tmp_path, sid="a")

    async def work():
        await T.apply_scene_ir(json.dumps(CRATE), a.ctx)
        await T.run_visual_qa(a.ctx)
        record_approval(a)

    asyncio.run(work())
    assert pre(state, "export_engine_package", conv=a.ctx.conversation_id)["decision"] == "allow"
    b = make_session(tmp_path, sid="b")  # opening a new session blanks latest.json
    assert pre(state, "export_engine_package", conv="unknown")["decision"] == "deny"
    assert pre(state, "export_engine_package", conv=b.ctx.conversation_id)["decision"] == "deny"


def test_unsafe_script_blocked_safe_script_allowed(state):
    bad = "import os\nimport bpy\nbpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'\nos.remove('x')\n"
    out = pre(state, "run_script", {"source": bad})
    assert out["decision"] == "deny" and "AG001" in out["reason"]
    out = pre(state, "run_bpy_script", {"Arguments": json.dumps({"script": "import bgl\n"})})
    assert out["decision"] == "deny" and "AG010" in out["reason"]
    good = "import bpy\nbpy.context.scene.render.engine = 'BLENDER_EEVEE'\nresult = len(bpy.data.objects)\n"
    assert pre(state, "run_script", {"source": good})["decision"] == "allow"
    assert pre(state, "run_script", {})["decision"] == "deny"  # nothing to lint


def test_telemetry_appends_jsonl_with_duration(state):
    pre(state, "run_script", {"source": "result = 1\n"}, tid="tool-42")
    assert post(state, "run_script", tid="tool-42") == {}
    assert post(state, "render_preview", tid="tool-43", error="exit status 1") == {}
    lines = [json.loads(x) for x in (state / "telemetry" / "tool_calls.jsonl").read_text().splitlines()]
    assert [(r["tool"], r["ok"], r["session"]) for r in lines] == [
        ("run_script", True, "conv-1"), ("render_preview", False, "conv-1")]
    assert lines[0]["duration_ms"] is not None and lines[0]["duration_ms"] >= 0
    assert lines[1]["duration_ms"] is None and lines[1]["since_prev_ms"] is not None
    assert lines[1]["error"] == "exit status 1"
    assert not list((state / "hooks" / "inflight").glob("*"))


def test_session_start_injects_budget_context(state):
    state.mkdir(parents=True)
    (state / "budget.json").write_text(json.dumps({"target_engine": "roblox_r15", "max_tris": 9000, "stage": "LOOKDEV"}))
    qa_state.write_qa_state(tier1_ok=True, tier15_score=3.1, asset="lamp", conversation_id="c9")
    out = run_hook("SessionStart", {"conversationId": "c9"}, state)
    msg = out["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "roblox_r15" in msg and "9000" in msg and "LOOKDEV" in msg and "3.10" in msg
    assert len(msg) < 800


def test_stop_hook_refuses_until_gate_passes(state):
    stop = {"conversationId": "conv-1", "terminationReason": "NO_TOOL_CALL", "continuationActive": False}
    assert run_hook("Stop", stop, state) == {"decision": "stop"}  # no asset work yet
    post(state, "apply_scene_ir", tid="m1")
    out = run_hook("Stop", stop, state)
    assert out["decision"] == "continue" and "\n" not in out["reason"]
    assert run_hook("Stop", {**stop, "continuationActive": True}, state)["decision"] == "stop"
    assert run_hook("Stop", {**stop, "terminationReason": "USER_CANCELED"}, state)["decision"] == "stop"
    write_passing_receipt(tier1_ok=True, tier15_score=3.6, conversation_id="conv-1")
    assert run_hook("Stop", stop, state)["decision"] == "stop"


def test_hooks_fail_open_on_garbage_input(state):
    env = {**os.environ, "AGY3D_STATE_DIR": str(state)}
    for script, expect in (("post_tool_telemetry.py", {}), ("stop_gate.py", {"decision": "stop"})):
        proc = subprocess.run([sys.executable, f"./hooks/scripts/{script}"], input="not json", text=True,
                              capture_output=True, cwd=BUNDLE, env=env, timeout=30)
        assert proc.returncode == 0 and json.loads(proc.stdout) == expect


def test_native_export_requires_exact_conversation_and_explicit_session(state):
    write_passing_receipt(tier1_ok=True, tier15_score=3.9, conversation_id="conv-1")
    def gate(conv, sid):
        return run_hook("PreToolUse", {"conversationId": conv, "toolCall": {"id": "check", "name": "mcp_tool",
                        "args": {"ServerName": "antigravity-3d_studio", "ToolName": "export_engine_package",
                                 "Arguments": {"session_id": sid}}}}, state, tool="mcp_tool")["decision"]
    assert gate("conv-1", "crate-session") == "allow"
    assert gate("conv-2", "crate-session") == gate("conv-1", "") == gate("conv-1", "other") == "deny"
