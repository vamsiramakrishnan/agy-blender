"""The MCP stdio server, driven as a real subprocess through the official ``mcp`` client."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.subscriptions import listen

from antigravity3d.harness import tools as T
from antigravity3d.plugin_install import bundle_dir
from test_harness_tools import CRATE, box

# Session-level tools, listed whatever the stage: session management plus the inspector MCP App's model tools.
MANAGEMENT = {"open_asset_session", "set_stage", "session_status", "list_sessions", "open_inspector", "review_notes"}
# The inspector MCP App's app-only tools: listed (the host needs their _meta) but hidden from the model.
APP_ONLY = {"inspector_payload", "inspector_qa_state", "inspector_list_notes", "inspector_save_note"}
ALWAYS = MANAGEMENT | APP_ONLY


def server_env(state: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY3D_POOL_SOCKET"}}
    return {**env, "AGY3D_OFFLINE": "1", "AGY3D_STATE_DIR": str(state)}


@asynccontextmanager
async def client(tmp_path: Path, *, modern: bool = False, env: dict[str, str] | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(command=sys.executable, args=["-m", "antigravity3d.runtime.mcp_server"],
                                   env={**server_env(tmp_path / "state"), **(env or {})}, cwd=str(tmp_path))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            if modern:
                await s.discover()
            else:
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
        assert set(tools) == ALWAYS
        await call(s, "open_asset_session", asset_name="Catalog", target_engine="godot_4")
        for stage, entries in T.STAGE_TOOLS.items():
            await call(s, "set_stage", stage=stage)
            catalog = {t.name: t for t in (await s.list_tools()).tools}
            assert set(catalog) == {fn.__name__ for fn in entries} | ALWAYS
            tools.update(catalog)
    for name, fn in T.ALL_TOOLS.items():
        if name not in tools:
            assert name in T.READ_ONLY_TOOL_NAMES
            continue
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
    assert set(_common.STUDIO_TOOLS) == set(T.ALL_TOOLS) | ALWAYS


async def test_session_stage_scoping_export_gate_and_linter(tmp_path):
    async with client(tmp_path) as s:
        r = await call(s, "apply_scene_ir", ops_json="{}")
        assert r.startswith("ERROR") and "no asset session" in r
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
        assert "tier1=ok" in status and "export=blocked" in status
        await call(s, "set_stage", stage="EXPORT")
        assert (await call(s, "generate_lods_and_colliders")).startswith("LODS 2 nodes")
        assert (await call(s, "run_usd_validator")).startswith("USD ")
        r = await call(s, "export_engine_package", out_name="crate_pkg")
        assert r.startswith("REFUSED") and "approval" in r, r

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
        r = await call(s, "session_status", session_id="nope")
        assert r.startswith("ERROR") and "unknown session 'nope'" in r
        assert (await call(s, "open_asset_session", asset_name="C", target_engine="quake")).startswith("ERROR")


def _frame(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


@pytest.mark.parametrize("open_delay_s", ["0", "0.6"])
def test_pipelined_calls_are_ordered_drained_and_stdout_is_protocol_only(tmp_path, open_delay_s):
    """All frames are written at once and stdin is closed straight away: the worst case for a slow runner.

    ``AGY3D_TEST_OPEN_DELAY_S`` (a test hook in the server) widens the window in which apply_scene_ir could
    overtake open_asset_session, so the result does not depend on machine speed.
    """
    proc = subprocess.Popen([sys.executable, "-m", "antigravity3d.runtime.mcp_server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path, start_new_session=True,
                            env={**server_env(tmp_path / "state"), "AGY3D_TEST_OPEN_DELAY_S": open_delay_s})
    pgid = os.getpgid(proc.pid)

    def call_msg(i: int, name: str, **arguments) -> dict:
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            call_msg(3, "open_asset_session", asset_name="Crate", target_engine="unreal_5"),
            call_msg(4, "apply_scene_ir", ops_json=json.dumps(CRATE)),
            call_msg(5, "session_status")]
    try:
        out, err = proc.communicate(b"".join(map(_frame, msgs)), timeout=180)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err.decode()[-800:]
    frames = [json.loads(ln) for ln in out.decode().splitlines()]  # every stdout line must be a JSON-RPC frame
    assert all(f.get("jsonrpc") == "2.0" for f in frames)
    by_id = {f["id"]: f for f in frames if "id" in f}
    assert set(by_id) == {1, 2, 3, 4, 5}, (sorted(by_id), err.decode()[-800:])
    text = {i: by_id[i]["result"]["content"][0]["text"] for i in (3, 4, 5)}
    assert text[3].startswith("SESSION Crate-")
    assert text[4].startswith("OK tx="), text[4]
    assert "nodes=2" in text[5]
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == ALWAYS
    assert by_id[1]["result"]["capabilities"]["tools"]["listChanged"]
    assert any(f.get("method") == "notifications/tools/list_changed" for f in frames)
    # nothing left running from this server's process group
    end = time.time() + 5
    while time.time() < end:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(pgid, signal.SIGKILL)
        pytest.fail("child processes outlived the server")


async def test_two_stdio_clients_keep_independent_stage_catalogs(tmp_path):
    async with client(tmp_path / "first") as first, client(tmp_path / "second") as second:
        await asyncio.gather(
            call(first, "open_asset_session", asset_name="First", target_engine="godot_4"),
            call(second, "open_asset_session", asset_name="Second", target_engine="unity_urp"),
        )
        await call(first, "set_stage", stage="LOOKDEV")
        await call(second, "set_stage", stage="EXPORT")
        left, right = await asyncio.gather(first.list_tools(), second.list_tools())
        assert {t.name for t in left.tools} == ALWAYS | {f.__name__ for f in T.STAGE_TOOLS["LOOKDEV"]}
        assert {t.name for t in right.tools} == ALWAYS | {f.__name__ for f in T.STAGE_TOOLS["EXPORT"]}
        assert (await call(first, "export_engine_package", out_name="wrong_stage")).startswith("REFUSED")
        # A subagent with an explicit read-only grant can still call its named probe.
        assert "nodes=0" in await call(first, "inspect_manifest")

async def test_modern_client_receives_catalog_invalidation(tmp_path):
    async with client(tmp_path, modern=True) as session:
        async with listen(session, tools_list_changed=True) as changes:
            await call(session, "open_asset_session", asset_name="Notify", target_engine="godot_4")
            await asyncio.wait_for(anext(changes), timeout=5)
            assert "apply_scene_ir" in {t.name for t in (await session.list_tools()).tools}
            await call(session, "set_stage", stage="LOOKDEV")
            await asyncio.wait_for(anext(changes), timeout=5)
            assert "apply_scene_ir" not in {t.name for t in (await session.list_tools()).tools}


# --------------------------------------------------------------------------- inspector MCP App
# Expected values are the ones in the Antigravity docs ("# Developing MCP Apps"): resource MIME type,
# tool _meta keys, visibility and entrypoints.

APP_URI = "ui://antigravity-3d/inspector.html"
APP_MIME = "text/html;profile=mcp-app"


async def raw(s: ClientSession, name: str, **args):
    res = await s.call_tool(name, args)
    assert not res.is_error, res.content
    return res


async def fetch_part(s: ClientSession, sid: str, revision: str, part: str = "scene") -> str:
    first = (await raw(s, "inspector_payload", session_id=sid, revision=revision, part=part, chunk=0)).structured_content
    data = [first["data"]]
    for i in range(1, first["chunks"]):
        nxt = (await raw(s, "inspector_payload", session_id=sid, revision=revision, part=part, chunk=i)).structured_content
        assert nxt["chunks"] == first["chunks"] and nxt["revision"] == revision
        data.append(nxt["data"])
    return "".join(data)


async def test_inspector_app_resource_and_tool_metadata(tmp_path):
    from antigravity3d.runtime.inspector_server import ui_meta_problems

    async with client(tmp_path) as s:
        init_caps = s.get_server_capabilities()
        assert init_caps is not None and init_caps.resources is not None  # the host requires the resources capability
        resources = (await s.list_resources()).resources
        app = [r for r in resources if str(r.uri) == APP_URI]
        assert len(app) == 1 and app[0].mime_type == APP_MIME
        assert app[0].meta == {"ui": {"prefersBorder": True}}
        contents = (await s.read_resource(APP_URI)).contents
        assert len(contents) == 1 and contents[0].mime_type == APP_MIME and contents[0].meta == {"ui": {"prefersBorder": True}}
        html = contents[0].text
        assert 0 < len(html.encode()) < 10 * 1024 * 1024  # documented 10 MiB limit
        assert html.lstrip().lower().startswith("<!doctype html>")
        # self-contained: no external scripts, styles, frames or base (the default CSP is default-src 'none')
        assert not re.search(r"<(script|link|iframe|base)\b[^>]*\b(src|href)\s*=", html, re.I)

        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert tools["open_inspector"].meta == {
            "ui": {"resourceUri": APP_URI},
            "com.google.antigravity/ui": {"entrypoints": [{"type": "aux_pane", "title": "3D Inspector"},
                                                          {"type": "global", "title": "3D Inspector"}]},
        }
        assert tools["open_inspector"].input_schema.get("required", []) == []  # entrypoint launches call it with {}
        assert not (tools["review_notes"].meta or {}).get("ui")  # visible to the model
        assert tools["review_notes"].input_schema.get("required", []) == []
        for name in APP_ONLY:
            assert tools[name].meta == {"ui": {"visibility": ["app"]}}, name
        assert ui_meta_problems([(t.name, t.meta) for t in tools.values()],
                                [(str(r.uri), r.mime_type) for r in resources]) == []
        # entrypoint launch before any session: a sensible empty view, not an error
        res = await raw(s, "open_inspector")
        assert res.structured_content["session_id"] is None and "open_asset_session" in res.content[0].text


def test_ui_meta_checker_reports_what_the_host_would_drop():
    from antigravity3d.runtime.inspector_server import ui_meta_problems

    errs = "\n".join(ui_meta_problems(
        [("a", {"ui": {"resourceUri": "https://x/app.html"}}), ("b", {"ui": {"visibility": ["model", "user"]}}),
         ("c", {"com.google.antigravity/ui": {"entrypoints": [{"type": "aux_pane"}]}}),
         ("d", {"ui": {"resourceUri": APP_URI}, "com.google.antigravity/ui": {"entrypoints": [{"type": "panel"}]}}),
         ("e", {"ui": {"resourceUri": "ui://other/app.html"}})],
        [(APP_URI, APP_MIME), ("ui://x/y.html", "text/html")]))
    for needle in ("tool a: ui.resourceUri must start with ui://", "tool b: ui.visibility", "tool c: entrypoints are only kept",
                   "tool d: unknown entrypoint type 'panel'", "tool e: ui.resourceUri ui://other/app.html is not a resource",
                   "resource ui://x/y.html: MIME"):
        assert needle in errs, needle


async def test_inspector_payload_notes_and_qa_round_trip(tmp_path):
    work = tmp_path / "crate_session"
    async with client(tmp_path, env={"AGY3D_TEST_INSPECTOR_CHUNK_CHARS": "700"}) as s:
        sid = (await call(s, "open_asset_session", asset_name="Crate", target_engine="godot_4", workdir=str(work))).split()[1]
        assert (await call(s, "apply_scene_ir", ops_json=json.dumps(CRATE))).startswith("OK")

        res = await raw(s, "open_inspector")
        summary = res.structured_content
        assert summary["session_id"] == sid and summary["asset"] == "Crate" and summary["view"] == {}
        assert summary["counts"]["nodes"] == 2 and summary["counts"]["tris"] == 24 and summary["counts"]["scene_chunks"] > 1
        assert len(json.dumps(summary)) < 4096  # small: large data comes from the app-only tool
        text = res.content[0].text
        assert text.startswith(f"INSPECTOR {sid} asset=Crate rev={summary['revision']} nodes=2 tris=24") and "\n" not in text

        rev = summary["revision"]
        payload = json.loads(await fetch_part(s, sid, rev))
        assert payload["revision"] == rev and payload["session_id"] == sid
        assert [m["id"] for m in payload["meshes"]] == ["body", "lid"] and payload["lods"] == [] and payload["colliders"] == []
        assert payload["budget"]["target_engine"] == "godot_4"
        bad = await s.call_tool("inspector_payload", {"session_id": sid, "revision": "0" * 16, "part": "scene", "chunk": 0})
        assert bad.is_error and bad.structured_content["current_revision"] == rev

        # the agent drives the open view
        res = await raw(s, "open_inspector", node="lid", channel="clay", view="top", wireframe=True)
        assert res.structured_content["view"] == {"node": "lid", "channel": "clay", "preset": "top", "wireframe": True}
        assert (await raw(s, "open_inspector", node="*")).structured_content["view"] == {"node": None}
        assert (await call(s, "open_inspector", node="nope")).startswith("ERROR") and "lid" in await call(s, "open_inspector", node="nope")
        assert (await call(s, "open_inspector", channel="xray")).startswith("ERROR")
        assert (await call(s, "open_inspector", lod=3)).startswith("ERROR")

        # notes: saved against node + revision, listed for the app and readable by the model
        saved = (await raw(s, "inspector_save_note", session_id=sid, node_id="lid", text="lid too thin\nmake it 2x", revision=rev)).structured_content
        assert saved["note"]["node_id"] == "lid" and saved["note"]["revision"] == rev and saved["counts"] == {"current": 1, "stale": 0}
        await raw(s, "inspector_save_note", session_id=sid, text="overall too clean", revision=rev)
        await raw(s, "inspector_save_note", session_id=sid, node_id="lid", text="lid too thin, make it 2x", revision=rev)  # edit in place
        listed = (await raw(s, "inspector_list_notes", session_id=sid)).structured_content
        assert sorted((n["node_id"] or "", n["text"], n["stale"]) for n in listed["notes"]) == [
            ("", "overall too clean", False), ("lid", "lid too thin, make it 2x", False)]
        notes_text = await call(s, "review_notes")
        assert notes_text.startswith(f"NOTES {sid} asset=Crate rev={rev}: 2 current, 0 stale")
        assert "[current] node lid" in notes_text and "lid too thin, make it 2x" in notes_text and "whole asset" in notes_text
        assert (await call(s, "inspector_save_note", session_id=sid, node_id="ghost", text="x", revision=rev)).startswith("ERROR")
        assert json.loads((work / "review_notes.json").read_text())["schema"] == "antigravity3d.review_notes/2"

        qa = (await raw(s, "inspector_qa_state", session_id=sid)).structured_content
        assert qa["revision"] == rev and qa["qa"]["tier1_ok"] is True and qa["qa"]["export_allowed"] is False
        assert qa["qa"]["threshold"] == 3.4 and qa["qa"]["stage"] == "BLOCKOUT"

        # a geometry edit: new revision, earlier notes kept and marked stale (never dropped)
        edit = {"ops": [box("lid", (1.05, 0.85, 0.16), 0.68)]}
        assert (await call(s, "apply_scene_ir", ops_json=json.dumps(edit))).startswith("OK")
        new_rev = (await raw(s, "open_inspector")).structured_content["revision"]
        assert new_rev != rev
        listed = (await raw(s, "inspector_list_notes", session_id=sid)).structured_content
        assert len(listed["notes"]) == 2 and all(n["stale"] for n in listed["notes"]) and listed["revision"] == new_rev
        notes_text = await call(s, "review_notes")
        assert ": 0 current, 2 stale" in notes_text and f"[stale, rev {rev}] node lid" in notes_text
        assert (await call(s, "review_notes", include_stale=False)).endswith("(no review notes yet: the reviewer writes them in the inspector, open_inspector)")
        # emptying a note deletes it
        await raw(s, "inspector_save_note", session_id=sid, text="", revision=rev)
        assert len((await raw(s, "inspector_list_notes", session_id=sid)).structured_content["notes"]) == 1

        # the EXPORT stage's derived data (UVs, LOD chain, hulls) shows up in the payload
        await call(s, "set_stage", stage="EXPORT")
        assert (await call(s, "generate_lods_and_colliders")).startswith("LODS")
        summary = (await raw(s, "open_inspector", lod=1, colliders=True)).structured_content
        assert summary["counts"]["lods"] >= 1 and summary["counts"]["colliders"] >= 1 and summary["view"] == {"lod": 1, "colliders": True}
        payload = json.loads(await fetch_part(s, sid, summary["revision"]))
        assert all(m["uv"] for m in payload["meshes"]) and payload["lods"] and payload["colliders"]


async def test_review_notes_survive_a_server_restart(tmp_path):
    work = tmp_path / "persist"
    async with client(tmp_path / "one") as s:
        await call(s, "open_asset_session", asset_name="Crate", target_engine="unity_urp", workdir=str(work))
        await call(s, "apply_scene_ir", ops_json=json.dumps(CRATE))
        summary = (await raw(s, "open_inspector")).structured_content
        await raw(s, "inspector_save_note", session_id=summary["session_id"], node_id="body", text="chamfer the edges",
                  revision=summary["revision"])
    async with client(tmp_path / "two") as s:
        sid = (await call(s, "open_asset_session", asset_name="Crate", target_engine="unity_urp", workdir=str(work))).split()[1]
        assert sid != summary["session_id"]
        await call(s, "apply_scene_ir", ops_json=json.dumps(CRATE))
        again = (await raw(s, "open_inspector")).structured_content
        assert again["revision"] == summary["revision"]  # same content, same revision
        assert again["notes"] == {"current": 1, "stale": 0}
        notes = (await raw(s, "inspector_list_notes", session_id=sid)).structured_content["notes"]
        assert [(n["node_id"], n["text"], n["stale"]) for n in notes] == [("body", "chamfer the edges", False)]
        assert "chamfer the edges" in await call(s, "review_notes")
