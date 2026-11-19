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
    assert skills == ["concept-to-blueprint", "engine-truth-verify", "game-ready-sanitizer", "parametric-kitbash",
                      "pbr-material-forge"]
    sidecars = sorted(p.name for p in (BUNDLE / "sidecars").iterdir())
    assert sidecars == ["blender_pool", "depot_watcher", "inspector", "nightly_variant_factory", "weekly_distill"]


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
                    "apply_shader_diff"} & set(fm["tools"])


def test_sidecars_use_documented_forms():
    pool = json.loads((BUNDLE / "sidecars/blender_pool/sidecar.json").read_text())
    assert pool["restart_policy"] == "always"
    assert " ".join([pool["command"], *pool["args"]]) == \
        "uv run python -m antigravity3d.runtime.worker_daemon --workers 4"
    assert set(pool["env"]) == {"BLENDER_BIN", "AGY3D_POOL_SOCKET"}
    ui = json.loads((BUNDLE / "sidecars/inspector/sidecar.json").read_text())
    assert ui["has_web_ui"] is True
    assert ui["ui_config"]["views"][0]["entrypoint"] == "SIDECAR_UI_ENTRYPOINT_AUX_PANE"
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


def test_export_blocked_without_qa_state(state):
    out = pre(state, "export_engine_package")
    assert out["decision"] == "deny" and "no QA verdict" in out["reason"]


def test_export_allowed_with_passing_qa_then_blocked_after_edit(state):
    qa_state.write_qa_state(tier1_ok=True, tier15_score=3.8, conversation_id="conv-1", asset="crate")
    out = pre(state, "export_engine_package", tid="e1")
    assert out == {"decision": "allow", "reason": out["reason"]} and "3.80" in out["reason"]

    assert post(state, "apply_scene_ir", tid="m1") == {}  # mutation after QA makes it stale
    out = pre(state, "export_engine_package", tid="e2")
    assert out["decision"] == "deny" and "apply_scene_ir" in out["reason"]

    qa_state.write_qa_state(tier1_ok=True, tier15_score=3.5, conversation_id="conv-1")
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


def test_namespaced_mcp_export_is_gated_by_the_script(state):
    # If the tools are ever served over MCP the step name may be generic; the script still
    # recognises the export by ToolName (the hooks.json matcher would need `mcp_tool` added).
    payload = {"conversationId": "conv-1", "toolCall": {"id": "t1", "name": "mcp_tool",
               "args": {"ServerName": "antigravity-3d", "ToolName": "export_engine_package"}}}
    out = run_hook("PreToolUse", payload, state, tool="export_engine_package")
    assert out["decision"] == "deny"


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
    qa_state.write_qa_state(tier1_ok=True, tier15_score=3.1, asset="lamp")
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
    qa_state.write_qa_state(tier1_ok=True, tier15_score=3.6, conversation_id="conv-1")
    assert run_hook("Stop", stop, state)["decision"] == "stop"


def test_hooks_fail_open_on_garbage_input(state):
    env = {**os.environ, "AGY3D_STATE_DIR": str(state)}
    for script, expect in (("post_tool_telemetry.py", {}), ("stop_gate.py", {"decision": "stop"})):
        proc = subprocess.run([sys.executable, f"./hooks/scripts/{script}"], input="not json", text=True,
                              capture_output=True, cwd=BUNDLE, env=env, timeout=30)
        assert proc.returncode == 0 and json.loads(proc.stdout) == expect
