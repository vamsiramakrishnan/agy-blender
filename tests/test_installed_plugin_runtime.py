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
        await call(session, "open_asset_session", asset_name="Probe", target_engine="godot_4")
        for stage in ["BLOCKOUT", "TWEAK", "VARIANTS", "LOOKDEV", "EXPORT"]:
            assert (await call(session, "set_stage", stage=stage)).startswith(f"STAGE {stage} callable:")
        assert (await call(session, "export_engine_package", out_name="ungated")).startswith("REFUSED")

    hooks = json.loads((plugin / "hooks.json").read_text())
    command = hooks["agy3d-qa-export-gate"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(command)[0] == sys.executable
    for name in ["blender_pool", "depot_watcher", "inspector"]:
        cfg = json.loads((plugin / f"sidecars/{name}/sidecar.json").read_text())
        assert cfg["command"] == sys.executable
        subprocess.run([cfg["command"], *cfg["args"], "--help"], cwd=tmp_path,
                       env=env, check=True, capture_output=True, timeout=20)
    weekly = json.loads((plugin / "sidecars/weekly_distill/sidecar.json").read_text())
    command = shlex.join([sys.executable, "-m", "antigravity3d.runtime.distill_job"])
    assert f"command({command})" in weekly["agent_permissions"]["access_grants"]
    assert command in weekly["args"][-1]
