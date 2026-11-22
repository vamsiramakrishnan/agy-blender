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

from antigravity3d.art_direction import load as art_direction_load, style_ids
from antigravity3d.harness import tools as T
from antigravity3d.harness.hooks import InvariantAndSafetyGate, state_header
from antigravity3d.harness.session import (
    CTX_REGISTRY_KEY, CTX_SESSION_KEY, bind_conversation, open_session, session, set_toolset, switch_to_preview,
)
from antigravity3d.harness.traceback_compressor import compress_exception
from antigravity3d.runtime import inspector_server as inspector_app
from antigravity3d.state import EngineBudget, SceneSessionRegistry
from antigravity3d.tactics.stream_call import CURRENT_SINK

log = logging.getLogger("antigravity3d.mcp")

SERVER_NAME = "antigravity-3d"
ENGINES = ("unreal_5", "unity_urp", "godot_4", "roblox_r15")
MANAGEMENT_TOOLS = ("open_asset_session", "open_existing_asset", "start_preview", "set_stage", "session_status", "list_sessions",
                    "install_into_project")
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
    "Use the returned session_id for native export. Replies are one line.")


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

    def __init__(self) -> None:
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
            raise LookupError("no asset session: call open_asset_session first")
        if sid not in self.registry:
            raise LookupError(f"unknown session {sid!r}; list_sessions shows the open ones")
        return sid

    def _line(self, sid: str) -> str:
        s = session(self.registry, sid)
        line = (f"SESSION {sid} asset={s.stage.asset_prim.GetName()} engine={s.state.budget.target_engine} "
                f"stage={s.toolset} tris<={s.state.budget.max_tris}"
                + (f" art={s.state.art_direction}" if s.state.art_direction else "") + f" workdir={s.workdir}")
        if s.state.preview:
            note = T.surfaces_note(s, opening=True)
            return line + (f" | PREVIEW (UNVERIFIED: judged by {s.plane.judge_label if s.plane else 'none'}; never verified)"
                           f" limits: {s.governor.summary()}") + (f" | {note}" if note else "")
        if s.plane is not None and not s.plane.calibrated:  # fail early: say now what will be refused, not at export
            return line + (" | WARNING: the native Decisions API is not available on this key, so run_visual_qa and "
                           f"export_engine_package will be refused. Remedy: {T.PREVIEW_SWITCH}")
        return line

    # ---- management tools
    async def open_asset_session(self, asset_name: str, target_engine: str, max_tris: int = 15_000,
                                 workdir: str = "", workspace: str = "", conversation_id: str = "", art_direction: str = "",
                                 keep_contact_faces: bool = False, preview: bool = False, token_ceiling: int | None = None,
                                 max_images: int | None = None, brief: str = "") -> str:
        if target_engine not in ENGINES:
            raise ValueError(f"target_engine must be one of {', '.join(ENGINES)}")
        if int(max_tris) < 1:
            raise ValueError("max_tris must be >= 1")
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
            EngineBudget(target_engine, max_tris=int(max_tris), keep_contact_faces=bool(keep_contact_faces)),  # type: ignore[arg-type]
            plane=self.backends.plane, forge=self.backends.forge, pool=self.backends.pool,
            state_workspace=root, art_direction=art_direction or None, preview=bool(preview),
            token_ceiling=token_ceiling, max_images=max_images, brief=brief or None)
        if conversation_id:
            bind_conversation(conversation_id, sid, self.registry)
        self._set_stage(sid, "BLOCKOUT")
        self.gates[sid] = InvariantAndSafetyGate(self.registry, sid)
        self.active = sid
        return self._line(sid) + " | " + T.SHORT_PATH

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
                "other part stays as it is. Then render_preview to check the change, and deliver_package.")

    async def start_preview(self, token_ceiling: int, max_images: int, session_id: str = "") -> str:
        sid = self.pick(session_id)
        async with self.registry.locked(sid):
            switch_to_preview(self.registry, sid, token_ceiling, max_images)
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
        return state_header(session(self.registry, self.pick(session_id)))

    async def list_sessions(self) -> str:
        sids = list(self.gates)
        if not sids:
            return "no sessions: call open_asset_session"
        return "\n".join(("* " if sid == self.active else "  ") + self._line(sid) for sid in sids)

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
            return await fn(ctx=McpToolContext(self.registry, sid), **kwargs)  # type: ignore[call-arg]
        except (LookupError, ValueError, RuntimeError) as e:  # expected: no/unknown session, bad input
            return f"ERROR {compress_exception(e)}"
        except Exception as e:  # noqa: BLE001 - one compressed line, never a traceback
            log.exception("tool %s failed", name)
            return f"ERROR {compress_exception(e)}"


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
    tool.__doc__ = inspect.getdoc(raw)
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
ART_DIRECTION_DOC = ("Named art direction (skills/art-direction): " + " | ".join(style_ids())
                     + "; empty for none. Lowers the budget where the look needs it and styles maps, shading and prompts.")


def _management(host: StudioHost) -> dict[str, tuple[Any, str]]:
    async def open_asset_session(
        asset_name: str,
        target_engine: Annotated[str, Field(description="unreal_5 | unity_urp | godot_4 | roblox_r15")],
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
    ) -> str:
        async def go() -> str:
            return await host.open_asset_session(asset_name, target_engine, max_tris, workdir, await _workspace_root(ctx), conversation_id,
                                                 art_direction=art_direction, keep_contact_faces=keep_contact_faces,
                                                 preview=preview, token_ceiling=token_ceiling, max_images=max_images, brief=brief)

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

    async def install_into_project(
        package_dir: Annotated[str, Field(description="Package directory holding manifest.json (the folder export_engine_package wrote)")],
        ctx: Context,
        project_dir: Annotated[str, Field(description="Game project root; default: this workspace")] = "",
        dest: Annotated[str, Field(description="Directory inside the project; default agy3d/<asset>")] = "",
        dry_run: Annotated[bool, Field(description="True (default): only report what would be written. Show that plan to the user "
                                                   "and call again with false once they agree")] = True,
        force: Annotated[bool, Field(description="Replace this tool's own files the developer edited since the last install")] = False,
    ) -> str:
        from antigravity3d.deliver import install_package

        project = project_dir or await _workspace_root(ctx)

        async def go() -> str:
            r = await asyncio.to_thread(install_package, package_dir, project, dest or None, dry_run, force, not dry_run)
            return _install_text(r)

        return await host.run_ordered(go)

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
        "materials wired, then imported and checked in the project's engine; Unity/Unreal/Roblox: says what to import). "
        "Run with dry_run first and show the user the plan; writing into their project needs their approval.")
    start_preview.__doc__ = ("Switch the session to an UNVERIFIED preview in place (scene kept, earlier verdicts discarded), "
                             "limited to the user's token ceiling and image limit. Ask the user for both; never choose them.")
    return {f.__name__: (f, f.__doc__ or "") for f in (open_asset_session, open_existing_asset, start_preview, set_stage, session_status,
                                                       list_sessions, install_into_project)}


def _install_text(r: Any) -> str:
    """One status line, the checks, then the machine-readable result."""
    import json

    head = f"{r.status.upper()} {r.message}"
    lines = [head, *(f"check {c.name}: {c.status} {c.detail}" for c in r.checks), *(f"warning: {w}" for w in r.warnings)]
    body = r.to_dict()
    body["files"] = [f for f in body["files"] if f["action"] != "keep"][:60]
    return "\n".join(lines) + "\n" + json.dumps(body)


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
        super().__init__(SERVER_NAME, instructions=INSTRUCTIONS)

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
