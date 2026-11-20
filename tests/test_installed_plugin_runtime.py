"""Launch the copied plugin exactly as its registered commands direct."""
import json
import shlex
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from antigravity3d.plugin_install import install, validate
from test_mcp_server import call, server_env


async def test_installed_plugin_launchers(tmp_path):
    plugin = install(tmp_path / "installed")
    env = server_env(tmp_path / "state")
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    assert validate(plugin) == []
    studio = json.loads((plugin / "mcp_config.json").read_text())["mcpServers"]["studio"]
    assert Path(studio["command"]) == Path(sys.executable).absolute()
    args = StdioServerParameters(command=studio["command"], args=studio["args"], env=env, cwd=tmp_path)
    async with stdio_client(args) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        assert "open_asset_session" in {tool.name for tool in (await session.list_tools()).tools}
        app = [r for r in (await session.list_resources()).resources if str(r.uri) == "ui://antigravity-3d/inspector.html"]
        assert len(app) == 1 and app[0].mime_type == "text/html;profile=mcp-app"  # the inspector MCP App
        await call(session, "open_asset_session", asset_name="Probe", target_engine="godot_4")
        for stage in ["BLOCKOUT", "TWEAK", "VARIANTS", "LOOKDEV", "EXPORT"]:
            assert (await call(session, "set_stage", stage=stage)).startswith(f"STAGE {stage} callable:")
        assert (await call(session, "export_engine_package", out_name="ungated")).startswith("REFUSED")

    hooks = json.loads((plugin / "hooks.json").read_text())
    command = hooks["agy3d-qa-export-gate"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(command)[0] == sys.executable
    for name in ["blender_pool", "depot_watcher"]:
        cfg = json.loads((plugin / f"sidecars/{name}/sidecar.json").read_text())
        assert cfg["command"] == sys.executable
        subprocess.run([cfg["command"], *cfg["args"], "--help"], cwd=tmp_path,
                       env=env, check=True, capture_output=True, timeout=20)
    weekly = json.loads((plugin / "sidecars/weekly_distill/sidecar.json").read_text())
    command = shlex.join([sys.executable, "-m", "antigravity3d.runtime.distill_job"])
    assert f"command({command})" in weekly["agent_permissions"]["access_grants"]
    assert command in weekly["args"][-1]


async def test_installed_profiles_enter_native_sdk(tmp_path, monkeypatch):
    from google.antigravity import Agent, types
    from google.antigravity.models import GeminiAPIEndpoint

    from antigravity3d.harness.agents import build_stage_agent
    from antigravity3d.harness.session import open_session
    from antigravity3d.harness.tools import STAGE_TOOLS
    from antigravity3d.state import EngineBudget, SceneSessionRegistry

    monkeypatch.setenv("AGY3D_STATE_DIR", str(tmp_path / "state"))
    registry = SceneSessionRegistry()
    for stage, tools in STAGE_TOOLS.items():
        workdir = tmp_path / stage.lower()
        plugin = install(workdir / ".agents")
        studio = json.loads((plugin / "mcp_config.json").read_text())["mcpServers"]["studio"]
        server = types.McpStdioServer(name="antigravity-3d_studio", **studio)
        open_session(registry, stage, workdir, stage, EngineBudget("godot_4"), plane=None, forge=None)
        config = build_stage_agent(stage, stage, registry=registry)
        # Test startup only. A loopback fixture keeps credentials and model inference out of this lane.
        endpoint = GeminiAPIEndpoint(base_url="http://127.0.0.1:1", api_key="local-fixture-only")
        config = config.model_copy(update={
            "model": None, "models": [m.model_copy(update={"endpoint": endpoint}) for m in config.models],
            "app_data_dir": str(workdir / "app-data"), "save_dir": str(workdir / "sessions"),
            "mcp_servers": [server],
        })
        async with Agent(config):
            assert list(config.tools) == tools
