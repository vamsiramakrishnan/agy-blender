"""The MCP stdio server, driven as a real subprocess through the official ``mcp`` client."""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from antigravity3d.harness import tools as T
from antigravity3d.plugin_install import bundle_dir
from test_harness_tools import CRATE

MANAGEMENT = {"open_asset_session", "set_stage", "session_status", "list_sessions"}


def server_env(state: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY3D_POOL_SOCKET"}}
    return {**env, "AGY3D_OFFLINE": "1", "AGY3D_STATE_DIR": str(state)}


@asynccontextmanager
async def client(tmp_path: Path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "antigravity3d.runtime.mcp_server"],
                                   env=server_env(tmp_path / "state"), cwd=str(tmp_path))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def call(s: ClientSession, name: str, **args) -> str:
    res = await s.call_tool(name, args)
    text = "".join(c.text for c in res.content if c.type == "text")
    assert text.count("\n") < 10, text
    return text


async def test_tools_list_has_harness_tools_with_clean_schemas(tmp_path):
    async with client(tmp_path) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert set(tools) == set(T.ALL_TOOLS) | MANAGEMENT
    for name, fn in T.ALL_TOOLS.items():
        t = tools[name]
        props = t.input_schema["properties"]
        assert "ctx" not in props and "session_id" in props, name
        assert t.description == inspect.getdoc(inspect.unwrap(fn)), name
        wanted = [p.name for p in inspect.signature(fn).parameters.values() if p.name != "ctx"]
        assert [p for p in props if p != "session_id"] == wanted, name
        required = [p.name for p in inspect.signature(fn).parameters.values() if p.name != "ctx" and p.default is p.empty]
        assert t.input_schema.get("required", []) == required, name
    assert tools["apply_scene_ir"].input_schema["required"] == ["ops_json"]
    assert tools["render_preview"].input_schema["properties"]["wireframe"]["default"] is True
    assert tools["forge_pbr_material"].input_schema["properties"]["candidates"]["type"] == "integer"
    assert set(tools["open_asset_session"].input_schema["required"]) == {"asset_name", "target_engine"}
    assert "max_tris" in tools["open_asset_session"].input_schema["properties"]
    assert "workdir" in tools["open_asset_session"].input_schema["properties"]
    assert "ctx" not in tools["open_asset_session"].input_schema["properties"]


def test_hook_script_tool_list_matches_the_server():
    sys.path.insert(0, str(bundle_dir() / "hooks" / "scripts"))
    try:
        import _common
    finally:
        sys.path.pop(0)
    assert set(_common.STUDIO_TOOLS) == set(T.ALL_TOOLS) | MANAGEMENT


async def test_session_stage_scoping_export_gate_and_linter(tmp_path):
    async with client(tmp_path) as s:
        assert (await call(s, "apply_scene_ir", ops_json="{}")).startswith("ERROR no asset session")
        line = await call(s, "open_asset_session", asset_name="Crate", target_engine="unreal_5", max_tris=9000)
        assert line.startswith("SESSION Crate-") and "stage=BLOCKOUT" in line and "tris<=9000" in line
        assert (await call(s, "list_sessions")).startswith("* SESSION Crate-")

        r = await call(s, "apply_scene_ir", ops_json=json.dumps(CRATE))
        assert r.startswith("OK tx=") and "nodes=2" in r and "tris=24" in r
        assert (await call(s, "inspect_manifest")).count("\n") >= 1

        # only the active stage's tools are callable
        r = await call(s, "forge_pbr_material", material_id="wood", prompt="oak")
        assert r.startswith("REFUSED") and "BLOCKOUT" in r and "\n" not in r
        assert (await call(s, "set_stage", stage="NOPE")).startswith("ERROR")

        # export before QA is refused (stage EXPORT lets the call reach the QA gate)
        await call(s, "set_stage", stage="EXPORT")
        r = await call(s, "export_engine_package", out_name="crate_pkg")
        assert r.startswith("REFUSED export blocked") and "tier15" in r
        assert (await call(s, "apply_scene_ir", ops_json="{}")).startswith("REFUSED")

        # unsafe raw scripts never reach the pool: the linter gate refuses them
        r = await call(s, "run_bpy_script", source="import os\nos.remove('x')\n")
        assert r.startswith("REFUSED script rejected by linter")
        r = await call(s, "run_bpy_script", source="import bpy\nresult = 1\n")
        assert r.startswith("UNAVAILABLE")  # clean script passes the gate; there is no pool in this run

        # QA in BLOCKOUT, then the EXPORT stage steps
        await call(s, "set_stage", stage="BLOCKOUT")
        assert (await call(s, "render_preview")).startswith("PREVIEW #1")
        assert (await call(s, "run_visual_qa")).startswith("QA[blockout] approve")
        status = await call(s, "session_status")
        assert "tier1=ok" in status and "export=allowed" in status
        await call(s, "set_stage", stage="EXPORT")
        assert (await call(s, "generate_lods_and_colliders")).startswith("LODS 2 nodes")
        assert (await call(s, "run_usd_validator")).startswith("USD ")
        r = await call(s, "export_engine_package", out_name="crate_pkg")
        assert r.startswith("EXPORTED crate_pkg"), r

        # the QA record is on disk for the plugin hooks
        qa = json.loads((tmp_path / "state" / "qa" / "latest.json").read_text())
        assert qa["tier1_ok"] and qa["tier15_score"] >= 3.4 and qa["session_id"].startswith("Crate-")


async def test_explicit_session_id_selects_between_sessions(tmp_path):
    async with client(tmp_path) as s:
        a = (await call(s, "open_asset_session", asset_name="A", target_engine="godot_4")).split()[1]
        b = (await call(s, "open_asset_session", asset_name="B", target_engine="unity_urp")).split()[1]
        assert a != b
        r = await call(s, "apply_scene_ir", ops_json=json.dumps(CRATE), session_id=a)
        assert r.startswith("OK")
        assert "nodes=0" in (await call(s, "session_status"))  # the active session is B (last opened)
        assert "nodes=2" in (await call(s, "session_status", session_id=a))
        assert (await call(s, "session_status", session_id="nope")).startswith("ERROR unknown session")
        assert (await call(s, "open_asset_session", asset_name="C", target_engine="quake")).startswith("ERROR")


def _frame(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def test_stdout_is_protocol_only_and_exit_is_clean(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "antigravity3d.runtime.mcp_server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=server_env(tmp_path / "state"),
                            cwd=tmp_path, start_new_session=True)
    pgid = os.getpgid(proc.pid)
    try:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        msgs = [init, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "open_asset_session", "arguments": {"asset_name": "Crate", "target_engine": "unreal_5"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "apply_scene_ir", "arguments": {"ops_json": json.dumps(CRATE)}}}]
        out, err = proc.communicate(b"".join(map(_frame, msgs)), timeout=120)  # closes stdin after the last frame
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err.decode()[-800:]
    lines = out.decode().splitlines()
    assert lines, "no frames on stdout"
    frames = [json.loads(ln) for ln in lines]  # every stdout line must be a JSON-RPC frame
    assert all(f.get("jsonrpc") == "2.0" for f in frames)
    assert {f["id"] for f in frames if "id" in f} == {1, 2, 3, 4}
    last = next(f for f in frames if f.get("id") == 4)
    assert last["result"]["content"][0]["text"].startswith("OK tx=")
    # nothing left running from this server's process group
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(pgid, signal.SIGKILL)
        pytest.fail("child processes outlived the server")
