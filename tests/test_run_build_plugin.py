"""Script mode of the plugin bundle (S/r6/CONTRACT.md section 5): the installer option, the installed bundle's texts and
hooks, and the colour views (a red and a blue part render red and blue; clay renders are unchanged)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from antigravity3d import plugin_install as P
from antigravity3d.kernel.raster import CLAY, render_buffers
from antigravity3d.kernel.types import MeshData
from antigravity3d.runtime import qa_state
from antigravity3d.tactics.judging import render_design

SCRIPT_TEXTS = Path(P.__file__).with_name("build") / "plugin_script_mode"


def test_script_install_swaps_the_texts_sets_the_env_drops_three_skills_and_validates(tmp_path):
    dest = P.install(tmp_path / "root", authoring="script")
    cfg = json.loads((dest / "mcp_config.json").read_text())["mcpServers"]["studio"]
    assert cfg["env"]["AGY3D_AUTHORING"] == "script" and cfg["args"] == ["-m", "antigravity3d.runtime.mcp_server"]
    assert (dest / "rules" / "AGENTS.md").read_text() == (SCRIPT_TEXTS / "AGENTS.md").read_text()
    assert (dest / "skills" / "parametric-kitbash" / "SKILL.md").read_text() == (SCRIPT_TEXTS / "SKILL.md").read_text()
    assert (dest / "authoring").read_text() == "script\n"
    assert sorted(p.name for p in (dest / "skills").iterdir()) == ["art-direction", "install-into-project", "parametric-kitbash"]
    assert P.validate(dest) == []
    # the default bundle in the repo is untouched by a script install
    assert "run_build" not in (P.bundle_dir() / "rules" / "AGENTS.md").read_text()


def test_the_default_install_is_todays_bundle(tmp_path):
    dest = P.install(tmp_path / "root")
    cfg = json.loads((dest / "mcp_config.json").read_text())["mcpServers"]["studio"]
    assert "AGY3D_AUTHORING" not in cfg.get("env", {}) and not (dest / "authoring").exists()
    assert (dest / "rules" / "AGENTS.md").read_text() == (P.bundle_dir() / "rules" / "AGENTS.md").read_text()
    assert sorted(p.name for p in (dest / "skills").iterdir()) == sorted(p.name for p in (P.bundle_dir() / "skills").iterdir())


def test_script_mode_needs_a_copy_install_and_the_cli_takes_the_option(tmp_path):
    with pytest.raises(ValueError, match="copy install"):
        P.install(tmp_path / "a", symlink=True, authoring="script")
    with pytest.raises(ValueError, match="authoring must be one of"):
        P.install(tmp_path / "a", authoring="json")
    assert P.main(["install", "--authoring", "script", "--target", str(tmp_path / "cli")]) == 0
    assert (tmp_path / "cli" / "plugins" / P.PLUGIN_NAME / "authoring").read_text() == "script\n"


@pytest.fixture(scope="module")
def script_bundle(tmp_path_factory) -> Path:
    return P.install(tmp_path_factory.mktemp("sb") / "root", authoring="script")


def hook(bundle: Path, script: str, payload: dict, state: Path) -> dict:
    env = {**os.environ, "AGY3D_STATE_DIR": str(state)}
    proc = subprocess.run([sys.executable, f"./hooks/scripts/{script}"], input=json.dumps(payload), text=True,
                          capture_output=True, cwd=bundle, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def mcp(name: str, args: dict, conv: str = "conv-1") -> dict:
    return {"hookEventName": "PreToolUse", "conversationId": conv,
            "toolCall": {"id": "t1", "name": "mcp_tool", "args": {"ServerName": "antigravity-3d_studio", "ToolName": name,
                                                                  "Arguments": json.dumps(args)}}}


def test_session_start_in_script_mode_says_script_and_run_build(script_bundle, tmp_path):
    msg = hook(script_bundle, "session_start.py", {"hookEventName": "SessionStart", "conversationId": "conv-1"},
               tmp_path)["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Pass conversation_id=conv-1 to run_build." in msg and "one Python script" in msg
    assert "Scene IR" not in msg and "open_asset_session" not in msg
    default = hook(P.bundle_dir(), "session_start.py", {"hookEventName": "SessionStart", "conversationId": "conv-1"},
                   tmp_path)["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Edit via Scene IR tools" in default and "run_build" not in default


def test_the_gate_asks_the_user_for_a_preview_run_build_and_wants_the_conversation_id(script_bundle, tmp_path, monkeypatch):
    out = hook(script_bundle, "pre_tool_gate.py", mcp("run_build", {"path": "c.py", "conversation_id": "conv-1", "preview": True,
                                                                    "token_ceiling": 200000, "max_images": 0}), tmp_path)
    assert out["decision"] == "ask" and "200,000 tokens" in out["reason"] and "0 generated image(s)" in out["reason"]
    out = hook(script_bundle, "pre_tool_gate.py", mcp("run_build", {"path": "c.py", "conversation_id": "conv-1", "preview": True}), tmp_path)
    assert out["decision"] == "deny" and "token_ceiling and max_images" in out["reason"]
    out = hook(script_bundle, "pre_tool_gate.py", mcp("run_build", {"path": "c.py"}), tmp_path)
    assert out["decision"] == "deny" and "conversation's ID" in out["reason"] and "run_build" in out["reason"]
    out = hook(script_bundle, "pre_tool_gate.py", mcp("run_build", {"path": "c.py", "conversation_id": "other"}), tmp_path)
    assert out["decision"] == "deny"
    assert hook(script_bundle, "pre_tool_gate.py", mcp("run_build", {"path": "c.py", "conversation_id": "conv-1"}),
                tmp_path)["decision"] == "allow"
    # once a session is bound to this conversation, a later run_build may leave the ID out (only an opening call uses it)
    monkeypatch.setenv("AGY3D_STATE_DIR", str(tmp_path))
    qa_state.write_qa_state(tier1_ok=True, tier15_score=0.0, session_id="crate-1", conversation_id="conv-1", asset="crate",
                            revision="r1", target="godot_4", verdict=None, validator_ok=None, workspace=tmp_path,
                            preview=False, waits_for_user="")
    assert hook(script_bundle, "pre_tool_gate.py", mcp("run_build", {"path": "c.py"}), tmp_path)["decision"] == "allow"


def box(nid: str, x: float) -> MeshData:
    import trimesh

    m = trimesh.creation.box(extents=(0.8, 0.8, 0.8))
    return MeshData(nid, f"/A/{nid}", np.asarray(m.vertices) + (x, 0, 0.4), np.asarray(m.faces, np.int32), material_id=nid)


def test_colour_views_shade_each_part_in_its_colour_and_clay_is_unchanged():
    meshes = [box("red", -0.6), box("blue", 0.6)]
    buf = render_buffers(meshes, "front", 128, colours={"red": (200, 30, 30), "blue": (30, 40, 210)})
    img = buf.color.reshape(-1, 3).astype(int)
    prim = buf.prim_id.reshape(-1)
    red, blue = img[prim == 0], img[prim == 1]
    assert len(red) > 500 and len(blue) > 500
    assert (red[:, 0] > red[:, 2] + 80).mean() > 0.9 and (blue[:, 2] > blue[:, 0] + 80).mean() > 0.9
    clay = render_buffers(meshes, "front", 128)
    assert np.array_equal(clay.color, render_buffers(meshes, "front", 128, colours=None).color)
    hit = clay.prim_id.reshape(-1) >= 0
    px = clay.color.reshape(-1, 3)[hit].astype(float)
    # clay: one hue, CLAY scaled by the light
    assert np.allclose(px / np.maximum(px.sum(1, keepdims=True), 1), CLAY / CLAY.sum(), atol=0.02)
    # the judged design renders, with shipped normals and edges, are clay unless colours are asked for
    a = render_design(meshes, views=("front",), size=96)
    b = render_design(meshes, views=("front",), size=96, colours=None)
    assert a.plain == b.plain and a.marked == b.marked
    c = render_design(meshes, views=("front",), size=96, colours={"red": (200, 30, 30), "blue": (30, 40, 210)})
    assert c.plain != a.plain


def test_the_build_package_is_source_not_an_ignored_build_output():
    """``.gitignore`` ignores ``build/`` (build outputs); ``src/antigravity3d/build`` is a package of the project, so a
    file of it that git ignores would be left out of a commit and of the wheel (hatch honours .gitignore)."""
    import shutil

    repo = Path(__file__).resolve().parents[1]
    if shutil.which("git") is None or not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    for rel in ("src/antigravity3d/build/runner.py", "src/antigravity3d/build/guard.py",
                "src/antigravity3d/build/plugin_script_mode/SKILL.md", "src/antigravity3d/build/plugin_script_mode/AGENTS.md"):
        assert subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo).returncode != 0, rel


def test_a_script_install_leaves_out_the_sub_agents_and_ships_the_script_budget_rule(tmp_path):
    dest = P.install(tmp_path / "root", authoring="script")
    assert not (dest / "agents").exists()
    rule = (dest / "rules" / "engine-budgets.md").read_text()
    assert rule == (SCRIPT_TEXTS / "engine-budgets.md").read_text()
    for word in ("export_engine_package", "open_asset_session", "engine-truth-verify", "generate_lods_and_colliders"):
        assert word not in rule, word
    assert "Asset(engine=..." in rule and "deliver_package" in rule
    assert rule.startswith("---\ntrigger: glob\n")  # the same frontmatter: validate() reads it
    assert P.validate(dest) == []
    # the default bundle keeps both
    assert (P.bundle_dir() / "agents" / "usd_verifier.md").is_file()
    assert "export_engine_package" in (P.bundle_dir() / "rules" / "engine-budgets.md").read_text()
