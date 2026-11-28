"""MCP stdio server exposing the harness tools to native Antigravity conversations.

    python -m antigravity3d.runtime.mcp_server

Conversations started by the Antigravity app/CLI, by ``agentapi new-conversation`` (depot watcher,
scheduled automations) and by ``run_workflow`` agents do not receive Python-SDK custom tools; they
only see tools served over MCP (plugin ``mcp_config.json``). This server serves the same functions
from :mod:`antigravity3d.harness.tools` under their existing names, docstrings and parameter
schemas (generated from the signatures, minus ``ctx``, plus an optional ``session_id``; the IR-typed tools
advertise their explicit IR input schema, the one the SDK path declares), together
with four session-management tools (``open_asset_session``, ``set_stage``, ``session_status``,
``list_sessions``) because nothing else opens a session for a native conversation.

Enforcement parity. On the SDK path ``InvariantAndSafetyGate`` runs as a pre-tool hook. There is no
SDK hook runner here, so every dispatch first calls the *same* gate object
(``InvariantAndSafetyGate.decide``, factored out of the hook in ``harness/hooks.py``): stage
tool-set scoping, the QA export gate, the raw-script linter gate and the image policy gate. A denial
comes back as one ``REFUSED ...`` line and the tool never runs.

Each stdio connection owns a StudioHost. Discovery returns the four management tools plus the active
asset's stage tools. Opening/selecting assets and switching stages triggers an MCP list-changed
event when the visible schema set changes. Dispatch independently checks the requested session.
Read-only subagents retain their explicitly granted, named probes.

Inspector MCP App. The 3D review inspector is served as an MCP App (``ui://`` resource, the
model-visible ``open_inspector`` / ``review_notes`` and four app-only ``inspector_*`` tools); all of it
lives in :mod:`antigravity3d.runtime.inspector_server` and is wired in by the "inspector MCP App"
section below. Those tools are session-level like the management tools: listed whatever the stage.

Backends. ``GEMINI_API_KEY`` set: ``make_control_plane`` (Decisions API, generative fallback) and a
real ``ImageForge``. ``AGY3D_OFFLINE=1`` (or no key): the fakes. A Blender pool is attached through
``PoolClient`` when ``$AGY3D_POOL_SOCKET`` names an existing socket.

stdout carries protocol frames only; all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import inspect
import itertools
import logging
import os
import re
import sys
import typing
import uuid
from pathlib import Path
from typing import Annotated, Any, Callable
from urllib.parse import unquote, urlparse

import anyio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.stdio import stdio_server
from mcp_types import JSONRPCError, JSONRPCRequest, JSONRPCResponse, Tool
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from google.antigravity.tools.tool_runner import ToolWithSchema
from pydantic import Field

from antigravity3d import ir_schema
from antigravity3d.art_direction import load as art_direction_load, style_ids
from antigravity3d.harness import keeper
from antigravity3d.harness import tools as T
from antigravity3d.harness.hooks import InvariantAndSafetyGate, state_header
from antigravity3d.harness.session import (
    CTX_REGISTRY_KEY, CTX_SESSION_KEY, bind_conversation, open_session, session, set_toolset, switch_to_preview,
)
from antigravity3d.harness.traceback_compressor import compress_exception
from antigravity3d.runtime import inspector_server as inspector_app
from antigravity3d.state import EngineBudget, SceneSessionRegistry, texture_px_choice
from antigravity3d.tactics.stream_call import CURRENT_SINK

log = logging.getLogger("antigravity3d.mcp")

SERVER_NAME = "antigravity-3d"
ENGINES = ("unreal_5", "unity_urp", "godot_4", "roblox_r15", "web_three")
MANAGEMENT_TOOLS = ("open_asset_session", "open_existing_asset", "start_preview", "set_stage", "session_status", "list_sessions",
                    "install_into_project", "check_site")
SESSION_ID_DOC = "Asset session to act on; omit to use the active (most recently opened or selected) one."
INSTRUCTIONS = (
    "antigravity-3d asset studio. To change an asset that already exists (a package or a prop folder in the workspace), "
    "call open_existing_asset and edit it; never author it again. For a new asset call open_asset_session first. "
    + T.SHORT_PATH + " The stage tools (set_stage switches; tools outside the active stage are refused) are there for finer "
    "control. Edit only through apply_scene_ir; outside a preview export is "
    "refused until Tier-1 passes and Decisions approves current EEVEE/Cycles lookdev at >= 3.4. Without the native Decisions "
    "API only an UNVERIFIED preview can be exported: open_asset_session's reply says whether this session already is one "
    "(limits given at open); only if the reply asks for it, ask the user for a token ceiling and an image limit and call "
    "start_preview. "
    "Use the returned session_id for native export. Replies are one line." + ir_schema.colour_note())
# The tools whose input carries a colour: their descriptions say what a colour means (ir_schema.colour_note).
COLOUR_TOOLS = ("apply_scene_ir", "apply_shader_diff")

# Script mode (plugin_install.install(authoring="script") puts AGY3D_AUTHORING=script in the server's env): an asset is
# one Python script (antigravity3d.build; the skill parametric-kitbash is its reference), run_build runs, checks and shows
# it, deliver_package delivers it. Without the variable nothing here changes and run_build is not served. Today's
# session (the default) is the baseline the script path is measured against (S/r6/CONTRACT.md section 5).
AUTHORING_ENV = "AGY3D_AUTHORING"
SCRIPT_INSTRUCTIONS = (
    "antigravity-3d asset studio. An asset is one Python script (`from agy3d import Asset`; the skill parametric-kitbash is "
    "its reference). run_build(path) runs the script, checks the design and shows it: findings that name each part and its "
    "fix, and five colour views; edit the script and run it again. deliver_package(dest) reviews, builds levels of detail "
    "and collision shapes, validates and exports; its answer starts with \"DELIVERED\" (finished: reply to the user with "
    "the package path and the findings, and do not inspect it again), \"NOT DELIVERED: a person must decide\" (reply to the "
    "user with the reason and stop), \"NOT DELIVERED\" (fix what it names in the script, run_build, then deliver_package "
    "again) or \"STILL WORKING\" (nothing failed: the work goes on; call the same tool again at once with the same "
    "arguments for its result; run_build answers so too when a build takes longer). To change an asset that already exists, edit its script (the workspace's <asset_name>.py, or source/build.py "
    "in its package) and run it again; never write it anew. Without the native Decisions API only an UNVERIFIED preview "
    "can be delivered: run_build's reply says whether this session already is one; only if the reply asks for it, ask the "
    "user for a token ceiling and an image limit and call start_preview. Replies are short." + ir_schema.colour_note())
# What script mode lists: run_build instead of open_asset_session and the stage tools; deliver_package is the one
# harness tool (the inspector MCP App's tools are added as in the default mode).
SCRIPT_MANAGEMENT_TOOLS = ("run_build", "open_existing_asset", "start_preview", "session_status", "list_sessions",
                           "install_into_project", "check_site")
SCRIPT_HARNESS_TOOLS = ("deliver_package",)
BUILD_TIMEOUT_S = 180.0
NEXT_STEP = "NEXT: edit the script and run_build again to fix or improve; when it reads as the request, deliver_package(dest=...)."


def script_mode() -> bool:
    return os.environ.get(AUTHORING_ENV, "").strip().lower() == "script"


class McpToolContext:
    """Minimal stand-in for the SDK ``ToolContext``.

    ``harness.session.resolve`` reads ``ctx.conversation_id`` and falls back to
    ``ctx.get_state(CTX_SESSION_KEY / CTX_REGISTRY_KEY)``; that fallback is the contract satisfied here
    (an MCP call carries no conversation id, so nothing is bound through ``bind_conversation``).
    """

    conversation_id = ""

    def __init__(self, registry: SceneSessionRegistry, session_id: str) -> None:
        self._state: dict[str, Any] = {CTX_SESSION_KEY: session_id, CTX_REGISTRY_KEY: registry}

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value


def _offline() -> bool:
    return os.environ.get("AGY3D_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"} or not os.environ.get("GEMINI_API_KEY")


class Backends:
    """Decision plane, image forge and Blender pool, created once on first session."""

    def __init__(self) -> None:
        self.plane: Any = None
        self.forge: Any = None
        self.pool: Any = None
        self.kind = "uninitialised"
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        async with self._lock:
            if self.kind != "uninitialised":
                return
            if _offline():
                from antigravity3d.decisions.engine import DecisionControlPlane
                from antigravity3d.decisions.fakes import FakeDecisionsClient
                from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge

                self.plane = DecisionControlPlane(FakeDecisionsClient())
                self.forge = ImageForge(FakeImageBackend())
                self.kind = "offline"
            else:
                from google import genai

                from antigravity3d.decisions import make_control_plane
                from antigravity3d.genmedia.nano_banana import ImageForge

                client = genai.Client()
                self.plane = await make_control_plane(client)
                self.forge = ImageForge(client=client)
                self.kind = f"gemini/{self.plane.backend_kind}"
            sock = os.environ.get("AGY3D_POOL_SOCKET")
            if sock and os.path.exists(sock):
                try:
                    from antigravity3d.runtime.worker_daemon import PoolClient

                    self.pool = await PoolClient(sock).connect()
                except Exception as e:  # noqa: BLE001 - the pool is optional
                    log.warning("Blender pool at %s unavailable: %s", sock, e)

    async def close(self) -> None:
        if self.pool is not None:
            try:
                await self.pool.close()
            except Exception:  # noqa: BLE001
                pass
            self.pool = None


def _slug(text: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", text).strip("_") or fallback


class StudioHost:
    """Holds sessions for one server process and dispatches tool calls through the shared gate."""

    def __init__(self, *, script: bool | None = None) -> None:
        self.script = script_mode() if script is None else script
        # sessions run_build opened: session id -> (asset, engine, art direction), the key a later run_build reuses it by
        self.built: dict[str, tuple[str, str, str]] = {}
        self.last_timings: dict[str, float] = {}  # the last run_build's phases in seconds (measurement; logged too)
        self.registry = SceneSessionRegistry()
        self.backends = Backends()
        self.gates: dict[str, InvariantAndSafetyGate] = {}
        self.active: str | None = None
        # Requests are dispatched concurrently by the MCP runner. One FIFO lock, taken as the very first
        # await of every tool call, makes calls run in arrival order, so a client that pipelines
        # open_asset_session + apply_scene_ir behaves deterministically. Calls are therefore serial per
        # server process (the tools are already serialised per session by the registry lock); the price is
        # that a long QA call delays other sessions' calls.
        self._order = asyncio.Lock()
        # Calls with no session of their own yet (run_build, install_into_project) are kept here, as a session keeps its
        # tools' calls (harness/keeper.py): they answer inside the host's 3 minutes and their work goes on.
        self.kept = keeper.Keeper()

    async def run_ordered(self, fn: Any, *args: Any) -> str:
        async with self._order:
            try:
                return await fn(*args)
            except (ValueError, LookupError, RuntimeError, OSError) as e:
                return f"ERROR {compress_exception(e)}"
            except Exception as e:  # noqa: BLE001
                log.exception("management tool failed")
                return f"ERROR {compress_exception(e)}"

    def visible_tools(self) -> set[str]:
        if self.script:  # no stages to scope by: the same list before and after a session opens
            return set(SCRIPT_MANAGEMENT_TOOLS) | set(inspector_app.TOOL_NAMES) | set(SCRIPT_HARNESS_TOOLS)
        names = set(MANAGEMENT_TOOLS) | set(inspector_app.TOOL_NAMES) | {fn.__name__ for fn in T.SHORT_PATH_TOOLS}
        if self.active is not None:
            stage = session(self.registry, self.active).toolset
            names.update(fn.__name__ for fn in (*T.STAGE_TOOLS[stage], *T.SESSION_TOOLS[stage]))
        return names

    async def change_catalog(self, ctx: Context, fn: Any, *args: Any) -> str:
        async def change() -> str:
            previous = self.visible_tools()
            result = await fn(*args)
            if self.visible_tools() != previous:
                if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
                    await ctx.notify_tools_changed()
                else:
                    await ctx.session.send_tool_list_changed()
            return result
        return await self.run_ordered(change)

    # ---- session selection
    def pick(self, session_id: str = "") -> str:
        sid = session_id or self.active
        if not sid:
            raise LookupError("no asset session: call run_build first" if self.script else "no asset session: call open_asset_session first")
        if sid not in self.registry:
            raise LookupError(f"unknown session {sid!r}; list_sessions shows the open ones")
        return sid

    def _line(self, sid: str) -> str:
        s = session(self.registry, sid)
        line = (f"SESSION {sid} asset={s.stage.asset_prim.GetName()} engine={s.state.budget.target_engine} "
                f"stage={s.toolset} tris<={s.state.budget.max_tris}"
                + (f" textures<={s.state.budget.max_texture_px}px" if s.state.budget.max_texture_px else "")
                + (f" art={s.state.art_direction}" if s.state.art_direction else "") + f" workdir={s.workdir}")
        return line + self._judge_note(sid)

    def _judge_note(self, sid: str) -> str:
        """" | PREVIEW ..." or " | WARNING ..." (the judge this session has and what it means), or ""."""
        s = session(self.registry, sid)
        if s.state.preview:
            note = T.surfaces_note(s, opening=True)
            return (f" | PREVIEW (UNVERIFIED: judged by {s.plane.judge_label if s.plane else 'none'}; never verified)"
                    f" limits: {s.governor.summary()}") + (f" | {note}" if note else "")
        if s.plane is not None and not s.plane.calibrated:  # fail early: say now what will be refused, not at export
            return (" | WARNING: the native Decisions API is not available on this key, so run_visual_qa and "
                    f"export_engine_package will be refused. Remedy: {T.PREVIEW_SWITCH}")
        if why := s.unverifiable_target():  # the same, for a target no key can verify here
            return (f" | WARNING: {why}. run_visual_qa, forge_pbr_material and run_paintover_loop are refused and "
                    f"export_engine_package cannot deliver it. Remedy: {T.PREVIEW_SWITCH}")
        return ""

    # ---- management tools
    async def open_asset_session(self, asset_name: str, target_engine: str, max_tris: int = 15_000,
                                 workdir: str = "", workspace: str = "", conversation_id: str = "", art_direction: str = "",
                                 keep_contact_faces: bool = False, preview: bool = False, token_ceiling: int | None = None,
                                 max_images: int | None = None, brief: str = "", max_texture_px: int | None = None) -> str:
        sid = await self._open(asset_name, target_engine, max_tris, workdir, workspace, conversation_id, art_direction,
                               keep_contact_faces, preview, token_ceiling, max_images, brief, max_texture_px)
        return self._line(sid) + " | " + T.SHORT_PATH

    async def _open(self, asset_name: str, target_engine: str, max_tris: int, workdir: str, workspace: str,
                    conversation_id: str, art_direction: str, keep_contact_faces: bool, preview: bool,
                    token_ceiling: int | None, max_images: int | None, brief: str, max_texture_px: int | None = None) -> str:
        """Open a new asset session, bound to the conversation, stage BLOCKOUT, active; its id. The checks and preview
        rules of open_asset_session (which run_build opens its sessions through as well)."""
        if target_engine not in ENGINES:
            raise ValueError(f"target_engine must be one of {', '.join(ENGINES)}")
        if int(max_tris) < 1:
            raise ValueError("max_tris must be >= 1")
        texture_px = texture_px_choice(max_texture_px)
        art_direction_load(art_direction)  # an unknown id fails here, listing the valid ones, before anything is created
        asset = _slug(asset_name, "Asset")
        if delay := float(os.environ.get("AGY3D_TEST_OPEN_DELAY_S", "0") or 0):  # test hook: widens the race window
            await asyncio.sleep(delay)
        await self.backends.ensure()
        sid = f"{asset}-{uuid.uuid4().hex[:6]}"
        root = Path(workspace or os.getcwd())
        wd = Path(workdir) if workdir else root / "agy3d_assets" / sid
        open_session(
            self.registry, sid, wd, asset,
            EngineBudget(target_engine, max_tris=int(max_tris), keep_contact_faces=bool(keep_contact_faces),  # type: ignore[arg-type]
                         max_texture_px=texture_px),
            plane=self.backends.plane, forge=self.backends.forge, pool=self.backends.pool,
            state_workspace=root, art_direction=art_direction or None, preview=bool(preview),
            token_ceiling=token_ceiling, max_images=max_images, brief=brief or None)
        if conversation_id:
            bind_conversation(conversation_id, sid, self.registry)
        self._set_stage(sid, "BLOCKOUT")
        self.gates[sid] = InvariantAndSafetyGate(self.registry, sid)
        self.active = sid
        return sid

    async def open_existing_asset(self, path: str, workspace: str = "", conversation_id: str = "", preview: bool = False,
                                  token_ceiling: int | None = None, max_images: int | None = None) -> str:
        """A session whose stage is an asset this project already shipped (``dags.edit_asset.load_shipped``: the design
        embedded in its package, each material on its source maps), stage TWEAK: an edit changes what it names and the
        rest stays as it shipped."""
        from antigravity3d.dags.edit_asset import EditError, load_shipped, source_materials

        root = Path(workspace or os.getcwd())
        target = Path(path) if Path(path).is_absolute() else root / path
        try:
            shipped = load_shipped(target)
        except EditError as e:
            raise ValueError(str(e)) from e
        script = _shipped_script(shipped.package, shipped.manifest)
        if script is None and self.script:
            # script mode serves no tool that edits a design in place (found by review: this answered with stage tools)
            return (f"NOT A SCRIPT ASSET: {shipped.package} was not built by a script (it has no source/build.py), so script "
                    "mode cannot change it: tell the user, who can change it with the plugin installed in its default mode, "
                    "or have it made anew as a script (write one and run_build it).")
        if script is not None and self.script:
            # an asset a script built is changed by editing that script: no session to open here (run_build opens one)
            same = root / f"{shipped.asset}.py"
            alias = (f" (the workspace's {same.name} is the same script)" if same.is_file()
                     and same.read_bytes() == script.read_bytes() else "")
            return (f"SCRIPT {script} built this asset{alias}. To change it, edit only the lines of the parts the request "
                    "names in that script, keep every other line, then run_build(path=...) and deliver_package(dest=...) "
                    "to the same folder.")
        await self.backends.ensure()
        sid = f"{shipped.asset}-{uuid.uuid4().hex[:6]}"
        wd = root / "agy3d_assets" / sid
        scene, origin = source_materials(shipped, wd / "inputs")
        open_session(self.registry, sid, wd, shipped.asset, shipped.budget, plane=self.backends.plane, forge=self.backends.forge,
                     pool=self.backends.pool, state_workspace=root, art_direction=shipped.art_direction, preview=bool(preview),
                     token_ceiling=token_ceiling, max_images=max_images, brief=shipped.brief)
        s = session(self.registry, sid)
        self.registry.attach(sid, T.EDIT_OF, str(shipped.package))  # an edit: separation moves only what the edit changes
        async with self.registry.locked(sid):
            s.stage.apply_ir(scene)
            s.state.tx_stack = list(s.stage.tx_stack)
            s.visuals_changed(tier1_ok=None)
        if conversation_id:
            bind_conversation(conversation_id, sid, self.registry)
        self._set_stage(sid, "TWEAK")
        self.gates[sid] = InvariantAndSafetyGate(self.registry, sid)
        self.active = sid
        return (self._line(sid) + f" | opened {shipped.package} as it shipped ({len(scene.nodes)} parts; materials from {origin}). "
                "Stage TWEAK: change only what the user asks with apply_scene_ir (inspect_manifest lists the part ids); every "
                "other part stays as it is. Then render_preview to check the change, and deliver_package."
                + (f" | built by the script {script}: editing it and running run_build rebuilds the design" if script else ""))

    # ---- run_build (script mode)
    async def run_build(self, path: str, workspace: str = "", conversation_id: str = "", preview: bool = False,
                        token_ceiling: int | None = None, max_images: int | None = None, session_id: str = "",
                        timeout_s: float = BUILD_TIMEOUT_S) -> str:
        """``_run_build`` as a kept call (harness/keeper.py): it answers within the host's time and a call with the same
        arguments for the same script text joins the build that is running. A changed script is another call: it waits
        for the earlier build, then builds anew."""
        args = {"path": path, "preview": preview, "token_ceiling": token_ceiling, "max_images": max_images,
                "session_id": session_id}
        shown = {k: v for k, v in args.items() if v not in ("", None, False)}
        try:
            text = _script_path(Path(workspace or os.getcwd()).resolve(), path).read_bytes()
        except (ValueError, OSError):
            text = b""  # the build answers why
        key = {**shown, "workspace": workspace, "script_sha256": hashlib.sha256(text).hexdigest()}
        return await self.kept.call("run_build", key, lambda: self._run_build(
            path, workspace, conversation_id, preview, token_ceiling, max_images, session_id, timeout_s), shown=shown)

    async def _run_build(self, path: str, workspace: str = "", conversation_id: str = "", preview: bool = False,
                         token_ceiling: int | None = None, max_images: int | None = None, session_id: str = "",
                         timeout_s: float = BUILD_TIMEOUT_S) -> str:
        """Run an asset script (``antigravity3d.build.runner``), open or reuse its session, apply its design as one checked
        transaction and answer with the findings and the colour views (S/r6/CONTRACT.md section 4). The first word of
        the reply decides the agent's next step: BUILT, UNCHANGED or NOT BUILT; a failure leaves every session as it was.
        The first reply after this call opened a session carries what the session's judge means for delivery, whatever
        its first word (``_notice``)."""
        import shutil
        import time

        from antigravity3d.build import handoff, runner

        timings: dict[str, float] = {}
        self.last_timings = timings
        root = Path(workspace or os.getcwd()).resolve()
        try:
            script = _script_path(root, path)
        except ValueError as e:
            return f"NOT BUILT: {e}. Pass the script's path inside the workspace (relative to it, e.g. crate.py)."
        rel = script.relative_to(root).as_posix()
        cand = session_id or self.active
        reuse_key = self.built.get(cand) if cand in self.gates else None
        if reuse_key is not None:  # the session this build may replace the design of: its calls wait for the build
            T._keeper(session(self.registry, cand)).claim(keeper.current())
        kept = (f"session {cand} is unchanged" if reuse_key else "no session was opened")
        run_dir = root / "agy3d_assets" / f".run-{uuid.uuid4().hex[:12]}"
        t0 = time.perf_counter()
        res = await asyncio.to_thread(runner.run_script, script, run_dir / "out", timeout_s=timeout_s)
        timings["script"] = time.perf_counter() - t0
        sid: str | None = None
        try:
            if not res.ok:
                return _not_built(res, kept, rel)
            text = res.source  # the text that was checked and run: the one recorded and shipped
            try:
                first = handoff.read(res.out)  # read here for the asset's name and budget; the session reads its copy
            except (ValueError, OSError) as e:
                return f"NOT BUILT: the design {rel} saved cannot be read: {e}. Fix the script and run_build again; {kept}."
            # Refused before anything is applied when the design's own count is over the budget: measured by review
            # (S/r6/scratch_review_door/seq/b11.py) 31,756 triangles against 4,000 took 25.9 s and 1.0 GB in the apply
            # and Tier-1 before the same refusal, and 240 k triangles were killed for memory.
            if (over := _over_budget(first, rel)) is not None:
                return f"{over}; {kept}."
            digest = _folder_digest(res.out)
            asset = _slug(first.name, "Asset")
            key = (asset, first.engine, first.art_direction or "")
            opened, why_new = False, ""
            if reuse_key is not None and reuse_key == key:
                sid = cand
            elif reuse_key is not None:
                was = dict(zip(("asset", "engine", "art direction"), reuse_key))
                now = dict(zip(("asset", "engine", "art direction"), key))
                why_new = ", ".join(f"{k} changed from {was[k] or 'none'} to {now[k] or 'none'}" for k in was if was[k] != now[k])
            if sid is None:
                try:
                    sid = await self._open(first.name, first.engine, first.max_tris, "", str(root), conversation_id,
                                           first.art_direction, False, preview, token_ceiling, max_images, first.brief,
                                           first.max_texture_px)
                except (ValueError, LookupError) as e:
                    return f"NOT BUILT: the session could not be opened: {compress_exception(e)}. Fix the Asset(...) line and run_build again; {kept}."
                self.built[sid] = key
                T._keeper(session(self.registry, sid)).claim(keeper.current())  # as above, for the session it opened
                # a script session from its opening on: every fix instruction names the script, even before a design
                self.registry.attach(sid, T.R_SCRIPT_SESSION, True)
                self.registry.attach(sid, R_NOTICE, True)
                opened = True
            s = session(self.registry, sid)
            kept = f"session {sid} is unchanged" if not opened else f"session {sid} was opened and holds no design yet"
            new_session = (f"NEW SESSION {sid}: the script's {why_new}, so this is a new session; the earlier session {cand} "
                           "keeps its design." if why_new else "")
            if why_new:
                new_session += (" This session is a preview (the limits this call gave)." if s.state.preview else
                                " This session is NOT a preview" + (" (the earlier one was)" if session(self.registry, cand).state.preview else "")
                                + ": if deliver_package needs one, ask the user for their two limits and call start_preview.")
            folder = s.workdir / "builds" / digest[:16]
            if not folder.is_dir():  # the session never depends on a temporary folder: it reads its own copy
                shutil.copytree(res.out, folder)
            try:
                built = handoff.read(folder)
            except ValueError as e:
                return self._notice(sid, f"NOT BUILT: the design {rel} saved cannot be read: {e}. Fix the script and run_build again; {kept}.")
            record = {"path": rel, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text, "digest": digest,
                      "parts": sorted(p.name for p in (folder / "parts").glob("*") if p.is_file()), "key": key}
            last = s._res(T.R_SCRIPT)
            if not opened and last and last["digest"] == digest and last["geom_version"] == s.geom_version:
                s._set(T.R_SCRIPT, {**record, "geom_version": last["geom_version"]})  # a comment-only edit ships too
                prev = s.latest_preview()
                return self._notice(sid, f"UNCHANGED: {rel} builds exactly the design session {sid} already holds; nothing was "
                                    "applied or rendered" + (f" (the views of #{prev.index} stand)" if prev is not None else "")
                                    + ". Edit the script to change the design, or deliver_package(dest=...) when it reads as the request.")
            budget_note: list[str] = []
            if stopped := await T.stop_kept_work(s):  # a delivery of the earlier design is no longer wanted
                budget_note.append(T.stopped_line(stopped))
            before = None if opened else self._budget_update(s, built, budget_note)
            t1 = time.perf_counter()
            try:
                tid, rep, _n, nodes, note = await T.apply_design(s, {"nodes": built.nodes, "materials": built.materials}, before,
                                                                attach=True, handoff=True)
            except ir_schema.IRArgumentError as e:
                return self._notice(sid, f"NOT BUILT: the design is malformed: {str(e).removeprefix('invalid IR: ')[:400]}. Fix the script and run_build again; {kept}.")
            except (ValueError, LookupError, RuntimeError, OSError) as e:
                return self._notice(sid, f"NOT BUILT: the design could not be applied: {compress_exception(e)}. Fix the script and run_build again; {kept}.")
            timings["apply_check"] = time.perf_counter() - t1
            if tid is None:
                faults = "; ".join(dict.fromkeys(T.script_critical(i, rep.stats) for i in rep.issues if i.severity == "critical"))
                return self._notice(sid, f"NOT BUILT: {rel} makes a design the engine would reject: {faults}. Change those parts in "
                                    f"the script and run_build again; {kept}" + (" (its earlier design stays)" if not opened else "") + ".")
            s._set(T.R_SCRIPT, {**record, "geom_version": s.geom_version})
            t2 = time.perf_counter()
            try:
                views = await T._render_views(s, colour=True)
            except ValueError as e:  # an empty design: nothing to show
                views = None
                budget_note.append(f"no views: {e}")
            timings["render"] = time.perf_counter() - t2
            log.info("run_build %s: %s", rel, ", ".join(f"{k} {v:.2f} s" for k, v in timings.items()))
            mats = len(s.stage.base_ir().materials)
            head = (f"BUILT {rel} -> session {sid} ({s.state.budget.target_engine}, tris {rep.stats['tris']} of "
                    f"{s.state.budget.max_tris}, {nodes} parts, {mats} materials, draws={rep.stats['draw_calls']})")
            lines = [head, *T._detached(rep)]
            warns = [w for i in rep.issues if i.code not in T.DETACHED and (w := T.script_warning(i)) is not None]
            lines += warns[:12] + ([f"warning: {len(warns) - 12} more of the same kinds"] if len(warns) > 12 else [])
            lines += [w for x in note if (w := T.script_note(x))]
            lines += [f"builder: {x}" for x in built.notes]
            lines += budget_note
            printed = "\n".join(x for x in res.stdout_tail.splitlines() if not x.startswith("agy3d: wrote"))
            if printed.strip():
                lines.append("the script printed: " + printed.strip()[-400:])
            if new_session:
                lines.append(new_session)
            if (notice := self._take_notice(sid)):
                lines.append(notice)
            if views is None:
                return "\n".join([*lines, NEXT_STEP])
            shown = T._views_reply(views, "VIEWS", colour=True)
            return T.ToolReply("\n".join([*lines, str(shown), NEXT_STEP]), shown.images)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def _script_judge_note(self, sid: str) -> str:
        """What the session's judge means for delivery, in a script session's words (no stage, no run_visual_qa or
        export_engine_package: script mode serves neither)."""
        s = session(self.registry, sid)
        if s.state.preview:
            return self._judge_note(sid).removeprefix(" | ")
        if s.plane is not None and not s.plane.calibrated:
            return ("WARNING: this key has no calibrated judge, so deliver_package will be refused until the user allows an "
                    "unverified preview: ask the user whether they want one and for their two limits (a token ceiling and an "
                    "image limit), then call start_preview with them; the design is kept.")
        if why := s.unverifiable_target():
            return (f"WARNING: {why}. deliver_package will be refused until the user allows an unverified preview: ask the user "
                    "whether they want one and for their two limits (a token ceiling and an image limit), then call "
                    "start_preview with them; the design is kept. Or change engine= in the script to a target that can be "
                    "verified here.")
        return ""

    def _take_notice(self, sid: str) -> str:
        """The judge notice once: the first reply after run_build opened the session carries it, whatever its first word
        (found by review: an opening build refused for its budget opened the session silently, and the agent first
        heard of the missing judge from deliver_package)."""
        if not (_has(self.registry, sid, R_NOTICE) and self.registry.resource(sid, R_NOTICE)):
            return ""
        self.registry.attach(sid, R_NOTICE, False)
        return self._script_judge_note(sid)

    def _notice(self, sid: str, reply: str) -> str:
        notice = self._take_notice(sid)
        return reply + (f"\n{notice}" if notice else "")

    def _budget_update(self, s: Any, built: Any, note: list[str]) -> Any:
        """A reused session takes the script's max_tris and brief in place (plain session state: Tier-1 and the export
        read the budget, the review the brief; both are set before the design is checked, and put back if it is not
        applied). Returns the ``before`` callable for ``tools.apply_design`` or None when nothing changes."""
        import dataclasses

        from antigravity3d.art_direction import load as load_art

        old_budget, old_brief = s.state.budget, s.state.brief
        art = load_art(s.state.art_direction)
        budget = dataclasses.replace(old_budget, max_tris=int(built.max_tris), max_texture_px=built.max_texture_px)
        if art is not None:
            budget = art.budget(budget, "prop")
        brief = built.brief if built.brief and built.brief.strip() else None
        if budget == old_budget and brief == old_brief:
            return None
        if budget.max_tris != old_budget.max_tris:
            note.append(f"budget now {budget.max_tris:,} triangles (was {old_budget.max_tris:,})")
        if budget.max_texture_px != old_budget.max_texture_px:
            note.append(f"texture atlas now at most {budget.max_texture_px or 'the engine default'} px "
                        f"(was {old_budget.max_texture_px or 'the engine default'})")
        if brief != old_brief:
            note.append("brief updated: the review checks the asset against the new words")

        def before() -> Any:
            s.state.budget, s.state.brief = budget, brief

            def undo() -> None:
                s.state.budget, s.state.brief = old_budget, old_brief
            return undo
        return before

    async def start_preview(self, token_ceiling: int, max_images: int, session_id: str = "") -> str:
        sid = self.pick(session_id)
        await T.stop_kept_work(session(self.registry, sid))  # earlier verdicts are discarded: work built on them is stale
        async with self.registry.locked(sid):
            switch_to_preview(self.registry, sid, token_ceiling, max_images)
        if self.script:
            note = self._judge_note(sid).removeprefix(" | ").rstrip(".")
            return (f"SESSION {sid} is now an UNVERIFIED preview: {note}. The design is kept: deliver_package(dest=...) when "
                    "it reads as the request, or edit the script and run_build first.")
        return self._line(sid) + " | earlier verdicts discarded: re-run visual QA"

    def _set_stage(self, sid: str, stage: str) -> None:
        s = session(self.registry, sid)
        s.state.active_stage = T.TOOLSET_STAGE[stage]  # type: ignore[assignment]
        set_toolset(self.registry, sid, stage)

    async def set_stage(self, stage: str, session_id: str = "") -> str:
        stage = stage.upper()
        if stage not in T.STAGE_TOOLS:
            raise ValueError(f"unknown stage {stage!r}; expected one of {', '.join(T.STAGE_TOOLS)}")
        sid = self.pick(session_id)
        self._set_stage(sid, stage)
        self.active = sid
        names = ", ".join(f.__name__ for f in T.STAGE_TOOLS[stage])
        note = T.surfaces_note(session(self.registry, sid)) if stage == "LOOKDEV" else ""
        return f"STAGE {stage} callable: {names}" + (f" | {note}" if note else "")

    async def session_status(self, session_id: str = "") -> str:
        s = session(self.registry, self.pick(session_id))
        running = [x for x in (T._keeper(s).status(), self.kept.status()) if x]  # a call still at work, and its step
        return "\n".join([state_header(s), *running])

    async def list_sessions(self) -> str:
        sids = list(self.gates)
        if not sids:
            return "no sessions: call run_build" if self.script else "no sessions: call open_asset_session"
        return "\n".join(("* " if sid == self.active else "  ") + self._line(sid) for sid in sids)

    def delivering(self, project: str, package_dir: str) -> Any:
        """The kept deliver_package or export_engine_package still writing the package ``package_dir`` names (in its
        session's export folder, or in the folder deliver_package(dest=...) places it in); None otherwise."""
        target = Path(package_dir) if Path(package_dir).is_absolute() else Path(project) / package_dir
        target = target.resolve()
        for sid in list(self.gates):
            s = session(self.registry, sid)
            k = T._keeper(s).running()
            if k is None or k.name not in {"deliver_package", "export_engine_package"}:
                continue
            try:
                dest = json.loads(k.key).get("dest") or ""
            except (ValueError, AttributeError):
                dest = ""
            places = [s.export_dir.resolve()] + ([(T._workspace(s) / dest).resolve()] if dest else [])
            if any(target == p or target.is_relative_to(p) for p in places):
                return k
        return None

    # ---- harness tools
    async def call_tool(self, name: str, fn: T.ToolFn, session_id: str, kwargs: dict[str, Any]) -> str:
        async with self._order:
            return await self._call_tool(name, fn, session_id, kwargs)

    async def _call_tool(self, name: str, fn: T.ToolFn, session_id: str, kwargs: dict[str, Any]) -> str:
        try:
            sid = self.pick(session_id)
            verdict = await self.gates[sid].decide(name, kwargs)
            if not verdict.allow:
                return f"REFUSED {verdict.message}"
            names = keeper.NAMES_SESSION.set(True)  # every tool here takes session_id: a call to repeat names it
            try:
                return await fn(ctx=McpToolContext(self.registry, sid), **kwargs)  # type: ignore[call-arg]
            finally:
                keeper.NAMES_SESSION.reset(names)
        except (LookupError, ValueError, RuntimeError) as e:  # expected: no/unknown session, bad input
            return f"ERROR {compress_exception(e)}"
        except Exception as e:  # noqa: BLE001 - one compressed line, never a traceback
            log.exception("tool %s failed", name)
            return f"ERROR {compress_exception(e)}"


def _script_path(root: Path, path: str) -> Path:
    """The script ``path`` names, inside the workspace ``root`` (relative to it, or absolute under it), checked: an
    existing .py file, no symbolic link on the way. Raises ValueError with one sentence."""
    from antigravity3d import safe_paths

    raw = (path or "").strip()
    if not raw:
        raise ValueError("no script path was given")
    p = Path(raw)
    if p.is_absolute():
        try:
            raw = p.resolve().relative_to(root).as_posix()
        except ValueError:
            raise ValueError(f"the script {path} is outside the workspace {root}") from None
    try:
        safe_paths.safe_name(raw, "script")
        target = safe_paths.inside(root, raw, "script", write=False)
    except safe_paths.UnsafePath as e:
        raise ValueError(str(e)) from None
    if not target.is_file():
        raise ValueError(f"there is no script {raw} in the workspace")
    if target.suffix != ".py":
        raise ValueError(f"{raw} is not a Python script (.py)")
    return target


def _folder_digest(folder: Path) -> str:
    """sha256 of a hand-off folder: every file's relative path and bytes, in sorted order."""
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(f.relative_to(folder).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


R_NOTICE = "judge_notice_pending"  # set when run_build opens a session; cleared by the first reply that carries the notice


def _has(registry: Any, sid: str, name: str) -> bool:
    try:
        registry.resource(sid, name)
    except KeyError:
        return False
    return True


def _ply_faces(path: Path) -> int:
    """The face count a binary PLY's header declares (the builder's free-form parts); 0 when it cannot be read."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096).split(b"end_header", 1)[0].decode("ascii", "replace")
    except OSError:
        return 0
    m = re.search(r"element face (\d+)", head)
    return int(m.group(1)) if m else 0


def _over_budget(build: Any, rel: str) -> str | None:
    """The NOT BUILT reply (without its last clause) when the design's exact count (design.json stats.triangles) is over
    the budget the script asks for (lowered by its art direction like a session's), naming the five heaviest parts; else
    None. A part's count: the kernel's ``triangle_count`` for a ready shape, its file's face count for a free-form one."""
    from antigravity3d.art_direction import load as load_art
    from antigravity3d.kernel.compile_ir import mesh_ref_file, triangle_count
    from antigravity3d.state import EngineBudget

    tris = (build.stats or {}).get("triangles")
    if not isinstance(tris, int):
        return None
    limit = int(build.max_tris)
    art = load_art(build.art_direction or None)
    if art is not None:
        limit = art.budget(EngineBudget(build.engine, max_tris=limit), "prop").max_tris  # type: ignore[arg-type]
    if tris <= limit:
        return None
    try:
        scene = build.scene()
        cost = {}
        for n in scene.nodes:
            if n.kind == "mesh_ref":
                cost[n.id] = _ply_faces(mesh_ref_file(str(n.params.get("path")), None))  # absolute from the hand-off (build.handoff)
            elif n.kind != "group":
                cost[n.id] = triangle_count(n)
        heavy = ", ".join(f"{k} {v:,}" for k, v in sorted(cost.items(), key=lambda kv: (-kv[1], kv[0]))[:5])
    except (ValueError, KeyError, TypeError):
        heavy = ""
    return (f"NOT BUILT: {rel} makes {tris:,} triangles and the budget is {limit:,}"
            + (f". The heaviest parts: {heavy}" if heavy else "")
            + ". Lower sides and segments on those parts (and smooth=False on a tube) or, if the request allows more, raise "
              "max_tris in the script's Asset line; nothing was applied")


def _not_built(res: Any, kept: str, rel: str = "the script") -> str:
    """The reply for a script that did not produce a design (syntax error, guard refusal, error, time-out, no a.save())."""
    if res.syntax:
        return f"NOT BUILT: {rel} has a syntax error at {res.error}. Fix that line and run_build again; {kept}."
    if res.refused:
        return f"NOT BUILT: the script was refused before it ran: {res.error}. Change that line and run_build again; {kept}."
    if res.timed_out:
        return (f"NOT BUILT: {res.error}. Make it finish sooner (fewer segments or sides, no endless loop) and run_build "
                f"again; {kept}.")
    if res.no_save:
        return f"NOT BUILT: {res.error}. Add a.save() as the last line and run_build again; {kept}."
    printed = f"\nthe script printed: {res.stdout_tail[-300:]}" if res.stdout_tail.strip() else ""
    return f"NOT BUILT: the script stopped with an error:\n{res.error}{printed}\nFix that line and run_build again; {kept}."


def _shipped_script(package: Path, manifest: dict[str, Any]) -> Path | None:
    """The package's ``source/build.py`` when its provenance says a script built the design (``run_build``); else None."""
    design = ((manifest.get("provenance") or {}).get("sources") or {}).get("design") or {}
    path = Path(package) / T.SCRIPT_FILE
    return path if design.get("kind") == "script" and path.is_file() and not path.is_symlink() else None


# --------------------------------------------------------------------------- schema generation


def _wrapper(name: str, fn: T.ToolFn, host: StudioHost) -> Any:
    """An async function whose signature is ``fn``'s minus ``ctx`` plus an optional ``session_id``."""
    raw = inspect.unwrap(fn)
    hints = typing.get_type_hints(raw)
    params = [p.replace(annotation=hints.get(p.name, p.annotation))
              for p in inspect.signature(raw).parameters.values() if p.name != "ctx"]
    params.append(inspect.Parameter("session_id", inspect.Parameter.KEYWORD_ONLY, default="",
                                    annotation=Annotated[str, Field(description=SESSION_ID_DOC)]))

    # The MCP request's context, injected by the SDK (found by its annotation, left out of the input schema): the tool's
    # model calls report to its client as notifications/progress while it runs (_progress_sink).
    params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context))

    async def tool(**kwargs: Any) -> Any:
        ctx = kwargs.pop("ctx", None)
        sid = kwargs.pop("session_id", "") or ""
        token = CURRENT_SINK.set(_progress_sink(ctx)) if ctx is not None else None
        try:
            return _with_images(await host.call_tool(name, fn, sid, kwargs))
        finally:
            if token is not None:
                CURRENT_SINK.reset(token)

    tool.__name__ = tool.__qualname__ = name
    tool.__doc__ = inspect.getdoc(raw) + (ir_schema.colour_note() if name in COLOUR_TOOLS else "")
    tool.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    tool.__annotations__ = {**{p.name: p.annotation for p in params}, "return": str}
    return tool


def _progress_sink(ctx: Any) -> Callable[[Any], None]:
    """A progress sink (``tactics.stream_call.ModelCallProgress``) that sends each event to the MCP client as
    ``notifications/progress``: ``progress`` counts events (it only grows, as the protocol asks), ``message`` is the event's
    line. Sending is scheduled, never awaited, so a slow or gone client never slows the call; a failed send is dropped.
    A client that sent no progress token receives nothing (the SDK sends nothing then)."""
    loop = asyncio.get_running_loop()
    count = itertools.count(1)
    pending: set[asyncio.Task[Any]] = set()

    def sink(e: Any) -> None:
        task = loop.create_task(ctx.report_progress(next(count), None, e.line()))
        pending.add(task)
        task.add_done_callback(lambda t: (pending.discard(t), t.cancelled() or t.exception()))

    return sink


def _with_images(reply: Any) -> Any:
    """A reply that shows images (``tools.ToolReply``: the renders of the design) as the text and one image content per
    file (PNG or WebP), so the client can put them in front of the model; any other reply as it is."""
    images = getattr(reply, "images", ())
    if not images:
        return reply
    import base64

    from mcp_types import ImageContent, TextContent

    return [TextContent(type="text", text=str(reply)),
            *(ImageContent(type="image", data=base64.b64encode(Path(path).read_bytes()).decode(),
                           mime_type="image/webp" if Path(path).suffix == ".webp" else "image/png") for _, path in images)]


def _declared_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    """The tool's IR input schema (``tools._typed``) plus the optional ``session_id`` every harness tool takes here."""
    props = {**input_schema.get("properties", {}), "session_id": {"type": "string", "default": "", "description": SESSION_ID_DOC}}
    return {**input_schema, "properties": props}


PREVIEW_DOC = ("Open an UNVERIFIED preview: judged by whatever judge the key has (the generative fallback when the "
               "native Decisions API is unavailable), exported labelled verified=false. Needs token_ceiling and max_images.")
TOKEN_CEILING_DOC = "The USER's token ceiling for this preview (input + output tokens, no price applied). Ask the user; never choose it."
MAX_IMAGES_DOC = "The USER's limit on generated images for this preview. Ask the user; never choose it."
BRIEF_DOC = ("The user's request for this asset, in their words (for example 'a rugged supply crate with corner guards, "
             "latches and two side handles'). Visual QA shows it to the judge and asks whether every part it names is there "
             "and how fully the asset depicts it. Empty: QA does not ask.")
MAX_TEXTURE_PX_DOC = ("Largest side of the texture atlas in pixels: 256, 512, 1024, 2048 or 4096; empty for the size the "
                      "parts need (at most 4096; at most 2048 for web_three, where 4096 has to be asked for). The answer "
                      "after delivery says up to how many pixels on screen the textures stay sharp; each step down halves "
                      "that and quarters the texture memory. Choose it from the largest size the model is shown at (for a "
                      "web page, a phone's screen too).")

ART_DIRECTION_DOC = ("Named art direction (skills/art-direction): " + " | ".join(style_ids())
                     + "; empty for none. Lowers the budget where the look needs it and styles maps, shading and prompts.")


def _management(host: StudioHost) -> dict[str, tuple[Any, str]]:
    async def open_asset_session(
        asset_name: str,
        target_engine: Annotated[str, Field(description="unreal_5 | unity_urp | godot_4 | roblox_r15 | web_three")],
        ctx: Context,
        conversation_id: Annotated[str, Field(description="Native conversation ID from the SessionStart header")] = "",
        max_tris: int = 15_000,
        workdir: Annotated[str, Field(description="Session folder; default <workspace>/agy3d_assets/<session id>")] = "",
        art_direction: Annotated[str, Field(description=ART_DIRECTION_DOC)] = "",
        keep_contact_faces: Annotated[bool, Field(description="Ship the faces parts press together too (closed parts; for parts that will be moved apart in the engine)")] = False,
        preview: Annotated[bool, Field(description=PREVIEW_DOC)] = False,
        token_ceiling: Annotated[int | None, Field(description=TOKEN_CEILING_DOC)] = None,
        max_images: Annotated[int | None, Field(description=MAX_IMAGES_DOC)] = None,
        brief: Annotated[str, Field(description=BRIEF_DOC)] = "",
        max_texture_px: Annotated[int | None, Field(description=MAX_TEXTURE_PX_DOC)] = None,
    ) -> str:
        async def go() -> str:
            return await host.open_asset_session(asset_name, target_engine, max_tris, workdir, await _workspace_root(ctx), conversation_id,
                                                 art_direction=art_direction, keep_contact_faces=keep_contact_faces,
                                                 preview=preview, token_ceiling=token_ceiling, max_images=max_images, brief=brief,
                                                 max_texture_px=max_texture_px)

        return await host.change_catalog(ctx, go)

    async def open_existing_asset(
        path: Annotated[str, Field(description="The asset to change: a prop output folder (holds report.json) or a package "
                                               "folder (holds manifest.json), absolute or relative to the workspace")],
        ctx: Context,
        conversation_id: Annotated[str, Field(description="Native conversation ID from the SessionStart header")] = "",
        preview: Annotated[bool, Field(description=PREVIEW_DOC)] = False,
        token_ceiling: Annotated[int | None, Field(description=TOKEN_CEILING_DOC)] = None,
        max_images: Annotated[int | None, Field(description=MAX_IMAGES_DOC)] = None,
    ) -> str:
        async def go() -> str:
            return await host.open_existing_asset(path, await _workspace_root(ctx), conversation_id, preview=preview,
                                                  token_ceiling=token_ceiling, max_images=max_images)

        return await host.change_catalog(ctx, go)

    async def start_preview(
        token_ceiling: Annotated[int, Field(description=TOKEN_CEILING_DOC)],
        max_images: Annotated[int, Field(description=MAX_IMAGES_DOC)],
        session_id: Annotated[str, Field(description=SESSION_ID_DOC)] = "",
    ) -> str:
        return await host.run_ordered(host.start_preview, token_ceiling, max_images, session_id)

    async def set_stage(stage: str, ctx: Context, session_id: Annotated[str, Field(description=SESSION_ID_DOC)] = "") -> str:
        return await host.change_catalog(ctx, host.set_stage, stage, session_id)

    async def session_status(session_id: Annotated[str, Field(description=SESSION_ID_DOC)] = "") -> str:
        return await host.run_ordered(host.session_status, session_id)

    async def list_sessions() -> str:
        return await host.run_ordered(host.list_sessions)

    async def run_build(
        path: Annotated[str, Field(description="The asset's script, relative to the workspace (e.g. wheelbarrow.py)")],
        ctx: Context,
        conversation_id: Annotated[str, Field(description="Native conversation ID from the SessionStart header")] = "",
        preview: Annotated[bool, Field(description=PREVIEW_DOC + " Used only when this call opens the session.")] = False,
        token_ceiling: Annotated[int | None, Field(description=TOKEN_CEILING_DOC)] = None,
        max_images: Annotated[int | None, Field(description=MAX_IMAGES_DOC)] = None,
        session_id: Annotated[str, Field(description=SESSION_ID_DOC)] = "",
    ) -> Any:
        async def go() -> Any:
            return await host.run_build(path, await _workspace_root(ctx), conversation_id, preview=preview,
                                        token_ceiling=token_ceiling, max_images=max_images, session_id=session_id)

        return _with_images(await host.change_catalog(ctx, go))

    async def install_into_project(
        package_dir: Annotated[str, Field(description="Package directory holding manifest.json (the folder export_engine_package wrote)")],
        ctx: Context,
        project_dir: Annotated[str, Field(description="Game project root; default: this workspace")] = "",
        dest: Annotated[str, Field(description="Directory inside the project; default agy3d/<asset> (three.js: src/agy3d/<asset>, "
                                                "where the loader and the install record go)")] = "",
        assets: Annotated[str, Field(description="three.js only: folder inside the project's served folder (public/ or static/) for "
                                                 "the asset's files; default <served>/agy3d/<asset>")] = "",
        dry_run: Annotated[bool, Field(description="True (default): only report what would be written. A project the user already "
                                                   "had: show them that plan and call again with false once they agree. A project "
                                                   "you are building in this session because the user asked for it: pass false")] = True,
        force: Annotated[bool, Field(description="Replace this tool's own files the developer edited since the last install")] = False,
        compress: Annotated[str, Field(description="three.js only, when the project has the compression packages: auto (default: "
                                                   "compress only when the files do not grow), always (for GPU memory), never")] = "auto",
    ) -> str:
        from antigravity3d.deliver import install_package

        project = project_dir or await _workspace_root(ctx)

        async def install() -> str:
            r = await asyncio.to_thread(install_package, package_dir, project, dest or None, dry_run, force, True,
                                        assets=assets or None, compress=compress)
            return _install_text(r)

        shown = {k: v for k, v in (("package_dir", package_dir), ("project_dir", project_dir), ("dest", dest),
                                   ("assets", assets), ("dry_run", dry_run), ("force", force), ("compress", compress))
                 if v != _INSTALL_DEFAULTS.get(k)}
        async def go() -> str:  # kept (harness/keeper.py): a browser or Godot check can pass the host's 3 minutes
            # A delivery still writing this package is waited for first (the install would read a package half
            # written, or the one it replaces); the key is read after it: a package delivered again in between is
            # another call, which installs that package.
            return await host.kept.call(
                "install_into_project", lambda: {**shown, "project": project, "manifest": _manifest_digest(project, package_dir)},
                install, shown=shown, wait_for=host.delivering(project, package_dir))

        return await host.run_ordered(go)

    async def check_site(
        target: Annotated[str, Field(description="The built site: its folder (holds index.html; for Vite, dist/ after the build), "
                                                 "absolute or relative to the workspace, or a localhost URL of a running server")],
        ctx: Context,
        sources: Annotated[str, Field(description="Folder that data-src paths are relative to (the project the site is about); "
                                                  "default: the site folder")] = "",
        steps: Annotated[int, Field(description="Scroll positions photographed, evenly from top to bottom (2 to 25)")] = 9,
        sizes: Annotated[str, Field(description="desktop (1440 x 900), phone (390 x 844), or both, comma-separated")] = "desktop,phone",
        other_hosts: Annotated[str, Field(description="block (default): requests to other hosts are listed, not made; "
                                                      "try: make them")] = "block",
    ) -> str:
        from antigravity3d.deliver.site_check import acheck_site, out_dir_for

        root = Path(await _workspace_root(ctx))
        where = target if target.startswith(("http://", "https://")) else str((root / target).resolve())
        src = str((root / sources).resolve()) if sources else None
        try:
            rep = await acheck_site(where, sources=src, steps=steps, sizes=sizes, other_hosts=other_hosts,
                                    out_dir=out_dir_for(root, where))
        except ValueError as e:
            return f"NOT CHECKED: {e}"
        return rep.text()

    open_asset_session.__doc__ = (
        "Open an asset session (new USD stage) and make it active, stage BLOCKOUT. Call this first for a NEW asset; returns the "
        "session line.")
    open_existing_asset.__doc__ = (
        "Reopen an asset this studio already made (its prop output folder or its package folder) to CHANGE it: the stage is "
        "the design as it shipped, each material on its source maps, stage TWEAK. Use this, never open_asset_session, when "
        "the user asks to change an existing asset ('make the handles 30 % larger'): edit only the parts named and export.")
    set_stage.__doc__ = ("Switch the active stage tool-set: BLOCKOUT, TWEAK, VARIANTS, LOOKDEV or EXPORT. "
                         "Only that stage's tools are callable.")
    session_status.__doc__ = "Current stage, budget, QA verdict (tier1/tier15/export allowed) and newest audit lines."
    list_sessions.__doc__ = "Open asset sessions; the active one is marked with *."
    install_into_project.__doc__ = (
        "Install a finished package into the game project this workspace is (Godot 4: a scene with levels, collision and "
        "materials wired, then imported and checked in the project's engine; three.js / React Three Fiber: the files where "
        "the dev server serves them, a loader module (and a component) to import, compressed for the web when the project "
        "has the tools, then checked in a browser with the project's own three.js; Unity/Unreal/Roblox: says what to import). "
        "Two cases. A project you are building in this session because the user asked for it (a site, a prototype): install "
        "directly with dry_run=false; this is how a package gets into it, never by copying its files by hand. A project the "
        "user already had: call with dry_run=true first, show the user the plan, and install once they agree. Without the "
        "compression packages in the project the install still writes the web GLB (WebP maps where the package has them) and "
        "the loader with every level of detail wired (it resolves with the nearest level and fetches each farther one "
        "when the camera first comes near it; levels: \"all\" fetches every level first); compression is added where "
        "the packages are.")
    run_build.__doc__ = (
        "Run the asset's Python script (from agy3d import Asset), check the design it saves and show it: the first call "
        "opens the asset's session from the script's Asset(...) line, a later one replaces the design in one checked step. "
        "The answer starts with BUILT (findings naming each part and its fix, five colour views), UNCHANGED (the script "
        "builds the design the session already holds) or NOT BUILT (the cause and the line to fix; nothing changed).")
    start_preview.__doc__ = ("Switch the session to an UNVERIFIED preview in place (scene kept, earlier verdicts discarded), "
                             "limited to the user's token ceiling and image limit. Ask the user for both; never choose them.")
    if getattr(host, "script", False):  # no stage vocabulary: script mode has no stages to name
        open_existing_asset.__doc__ = (
            "Find the script of an asset this studio already made (its package folder): to CHANGE it ('make the handles 30 % "
            "larger') edit only the lines of the parts named in that script and run_build it. An asset not built by a "
            "script cannot be changed in script mode; the answer says so.")
        session_status.__doc__ = "The asset session's budget, whether its design passed the checks, and whether it can be delivered."
    check_site.__doc__ = (
        "Look at a built website the way a visitor does, and measure it. Give the site's folder (holds index.html) or a "
        "localhost URL. It serves the folder under a sub-path, opens it at desktop and phone size, scrolls through it in "
        "even steps and photographs each one, then answers in plain text: what is broken (errors, failed requests, facts "
        "that disagree with their source), what a visitor would notice (steps where the 3D canvas is empty or nothing "
        "changed, subjects the page lists in window.__site.info().onScreen cut by the frame's edge or hidden under its HTML, "
        "sideways scroll on a phone, download weight, other hosts, reduce motion and no-WebGL handling), what the "
        "scene costs (draw calls, triangles, texture memory) and where the contact sheets are: open them with your file "
        "viewer. Mark every number and code specimen the page shows with its source, data-src=\"file#/json/pointer\", "
        "\"file#L10-L14\" or \"file\" (relative to sources), and it is checked against that file. A story (a page that "
        "sets window.__story: a deck of slides over a 3D stage, or a film) is stepped beat by beat instead of scrolled: "
        "each beat photographed at both sizes, a tile per beat on the sheets, the answer headed STORY CHECKED (empty "
        "beats, beats whose picture does not change (a slide may add words alone), words that are not shown or are "
        "hard to read over the picture, text that does not fit, lines missing from the HTML, scenes that stand still, "
        "the arrow key and the hash), one still per scene. Writes only its pictures, under agy3d_site_check/. Answers "
        "within 150 s; use it after every change you want to see.")
    tools = (run_build, open_existing_asset, start_preview, session_status, list_sessions, install_into_project, check_site) \
        if getattr(host, "script", False) \
        else (open_asset_session, open_existing_asset, start_preview, set_stage, session_status, list_sessions, install_into_project,
              check_site)
    return {f.__name__: (f, f.__doc__ or "") for f in tools}


_INSTALL_DEFAULTS = {"project_dir": "", "dest": "", "assets": "", "dry_run": True, "force": False, "compress": "auto"}


def _manifest_digest(project: str, package_dir: str) -> str:
    """sha256 of the package's manifest.json ('' when it cannot be read): a package delivered again is a new one."""
    p = Path(package_dir) if Path(package_dir).is_absolute() else Path(project) / package_dir
    try:
        return hashlib.sha256((p / "manifest.json").read_bytes()).hexdigest()
    except OSError:
        return ""


def _install_text(r: Any) -> str:
    """The install's answer, short enough for the host to show it: the outcome, what to import, the checks as one count
    with a line for each that did not pass, warnings and notes cut to a sentence or two, and where the whole record is.

    Run 2 of the website task (2026-11-24): the answer carried every check's detail and then the whole result as JSON,
    10,292 characters for one prop. Antigravity saved it to a file and gave the agent a pointer to go and read. A dry
    run still lists the files, because that list is the plan a person approves."""
    from collections import Counter

    word = r.status.replace("_", " ")
    msg = r.message[len(word) + 1:] if r.message.lower().startswith(word + " ") else r.message  # not "INSTALLED installed ..."
    lines = [f"{r.status.upper()} {msg}"]
    asset = (r.package or {}).get("asset")
    if r.engine == "threejs" and r.dest and asset and r.status in ("installed", "updated", "unchanged"):
        from antigravity3d.deliver.web import pascal

        lines.append(f"use it: {r.dest}/index.js exports load{pascal(asset)}({{ renderer }}); it returns a Group with every "
                     "level of detail wired")
    changed = [f for f in r.files if f.get("action") != "keep"]
    if r.status == "dry_run":
        lines += [f"would {f['action']} {f['path']}" + (f" ({f['bytes']:,} bytes)" if f.get("bytes") else "") for f in changed[:40]]
        if len(changed) > 40:
            lines.append(f"... and {len(changed) - 40} more file(s)")
    elif changed:
        lines.append(f"files: {len(changed)} written or removed, {sum(f.get('bytes') or 0 for f in changed):,} bytes")
    if r.checks:
        n = Counter(c.status for c in r.checks)
        lines.append(f"checks: {n['passed']} passed, {n['failed']} failed, {n['skipped']} skipped")
        lines += [f"check {c.name}: {c.status} {c.detail[:400]}" for c in r.checks if c.status != "passed"]
    lines += [f"warning: {w[:300]}" for w in r.warnings]
    lines += [f"note: {x[:300]}" for x in r.notes]
    if r.report_path:
        lines.append(f"every check in full: {r.report_path}")
    return "\n".join(lines)


async def _workspace_root(ctx: Any) -> str:
    """The client's first root (``roots/list``) when it offers one: that is the workspace the plugin's
    hooks derive ``<workspace>/.agy3d`` from. Falls back to the process cwd. Best effort, 3 s cap."""
    try:
        caps = ctx.client_capabilities if ctx is not None else None
        if caps is not None and caps.roots is not None:
            res = await asyncio.wait_for(ctx.session.list_roots(), 3)
            for root in res.roots:
                u = urlparse(str(root.uri))
                if u.scheme == "file" and u.path:
                    return unquote(u.path)
    except Exception as e:  # noqa: BLE001
        log.info("roots/list unavailable (%s); using cwd", e)
    return os.getcwd()


class StudioServer(MCPServer):
    """One client per stdio process; keep discovery bound to its active session."""
    def __init__(self, host: StudioHost):
        self.host = host
        super().__init__(SERVER_NAME, instructions=SCRIPT_INSTRUCTIONS if host.script else INSTRUCTIONS)

    async def list_tools(self) -> list[Tool]:
        async with self.host._order:
            names = self.host.visible_tools()
            return [tool for tool in await super().list_tools() if tool.name in names]


def build_server(host: StudioHost | None = None) -> MCPServer:
    host = host or StudioHost()
    server = StudioServer(host)
    for name, (fn, doc) in _management(host).items():
        server.add_tool(fn, name=name, description=doc, structured_output=False)
    for name, fn in T.ALL_TOOLS.items():
        if host.script and name not in SCRIPT_HARNESS_TOOLS:  # script mode serves no stage tool at all
            continue
        w = _wrapper(name, fn, host)
        server.add_tool(w, name=name, description=w.__doc__, structured_output=False)
        if isinstance(fn, ToolWithSchema):  # IR-typed parameters: advertise the same declaration the SDK path sends
            tool = server._tool_manager.get_tool(name)
            tool.parameters = _declared_schema(fn.input_schema)
    register_inspector_app(server, host)
    server._agy3d_host = host  # type: ignore[attr-defined]
    return server


# --------------------------------------------------------------------------- inspector MCP App
# Self-contained: the resource, tools, notes store and payload builder are in runtime/inspector_server.py.


def register_inspector_app(server: MCPServer, host: StudioHost) -> None:
    """Serves the 3D review inspector as an MCP App on this server (see :mod:`.inspector_server`)."""
    host.inspector = inspector_app.register(server, host)  # type: ignore[attr-defined]


class _DrainingStreams:
    """Wraps the transport streams so EOF on stdin does not cancel in-flight requests.

    The MCP runner stops serving when the read side closes, which would drop the replies of requests
    that arrived just before EOF (a client that pipelines its last calls and closes stdin). This tracks
    request ids from the read side, clears them when the response is written, and holds the read
    iterator open at EOF until pending finite requests have been answered. Subscription streams end at EOF.
    """

    def __init__(self, timeout_s: float = 600.0) -> None:
        self.pending: set[Any] = set()
        self.timeout_s = timeout_s

    def read(self, inner: Any) -> Any:
        outer = self

        class Read:
            def __aiter__(self) -> Any:
                return self._gen()

            async def _gen(self) -> Any:
                async for item in inner:
                    msg = getattr(item, "message", None)
                    if isinstance(msg, JSONRPCRequest) and msg.method != "subscriptions/listen":
                        outer.pending.add(msg.id)
                    yield item
                await outer.drain()

            async def receive(self) -> Any:
                try:
                    item = await inner.receive()
                except anyio.EndOfStream:
                    await outer.drain()
                    raise
                msg = getattr(item, "message", None)
                if isinstance(msg, JSONRPCRequest) and msg.method != "subscriptions/listen":
                    outer.pending.add(msg.id)
                return item

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            async def __aenter__(self) -> "Read":
                await inner.__aenter__()
                return self

            async def __aexit__(self, *exc: Any) -> Any:
                return await inner.__aexit__(*exc)

        return Read()

    def write(self, inner: Any) -> Any:
        outer = self

        class Write:
            async def send(self, item: Any) -> None:
                await inner.send(item)
                msg = getattr(item, "message", None)
                if isinstance(msg, (JSONRPCResponse, JSONRPCError)):
                    outer.pending.discard(msg.id)

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            async def __aenter__(self) -> "Write":
                await inner.__aenter__()
                return self

            async def __aexit__(self, *exc: Any) -> Any:
                return await inner.__aexit__(*exc)

        return Write()

    async def drain(self) -> None:
        with anyio.move_on_after(self.timeout_s):
            while self.pending:
                await anyio.sleep(0.02)
        if self.pending:
            log.warning("exiting with %d unanswered request(s)", len(self.pending))


async def serve(server: MCPServer) -> None:
    """``MCPServer.run_stdio_async`` plus the EOF drain."""
    drain = _DrainingStreams()
    async with stdio_server() as (read, write):
        low = server._lowlevel_server
        await low.run(drain.read(read), drain.write(write), low.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True)))


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = build_server()
    host: StudioHost = server._agy3d_host
    try:
        anyio.run(serve, server)
    finally:
        try:
            asyncio.run(host.backends.close())
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
