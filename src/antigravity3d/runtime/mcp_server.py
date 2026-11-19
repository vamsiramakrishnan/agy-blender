"""MCP stdio server exposing the harness tools to native Antigravity conversations.

    python -m antigravity3d.runtime.mcp_server

Conversations started by the Antigravity app/CLI, by ``agentapi new-conversation`` (depot watcher,
scheduled automations) and by ``run_workflow`` agents do not receive Python-SDK custom tools; they
only see tools served over MCP (plugin ``mcp_config.json``). This server serves the same functions
from :mod:`antigravity3d.harness.tools` under their existing names, docstrings and parameter
schemas (generated from the signatures, minus ``ctx``, plus an optional ``session_id``), together
with four session-management tools (``open_asset_session``, ``set_stage``, ``session_status``,
``list_sessions``) because nothing else opens a session for a native conversation.

Enforcement parity. On the SDK path ``InvariantAndSafetyGate`` runs as a pre-tool hook. There is no
SDK hook runner here, so every dispatch first calls the *same* gate object
(``InvariantAndSafetyGate.decide``, factored out of the hook in ``harness/hooks.py``): stage
tool-set scoping, the QA export gate, the raw-script linter gate and the image policy gate. A denial
comes back as one ``REFUSED ...`` line and the tool never runs.

Tool listing. ``tools/list`` always lists every harness tool. Narrowing the list to the active stage
would need ``notifications/tools/list_changed`` and a client that re-lists on it; the Antigravity MCP
docs (``# MCP Servers``) do not say its client does, so scoping is enforced at call time instead.

Backends. ``GEMINI_API_KEY`` set: ``make_control_plane`` (Decisions API, generative fallback) and a
real ``ImageForge``. ``AGY3D_OFFLINE=1`` (or no key): the fakes. A Blender pool is attached through
``PoolClient`` when ``$AGY3D_POOL_SOCKET`` names an existing socket.

stdout carries protocol frames only; all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import sys
import typing
import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from antigravity3d.harness import tools as T
from antigravity3d.harness.hooks import InvariantAndSafetyGate, state_header
from antigravity3d.harness.session import (
    CTX_REGISTRY_KEY, CTX_SESSION_KEY, open_session, session, set_toolset,
)
from antigravity3d.harness.traceback_compressor import compress_exception
from antigravity3d.state import EngineBudget, SceneSessionRegistry

log = logging.getLogger("antigravity3d.mcp")

SERVER_NAME = "antigravity-3d"
ENGINES = ("unreal_5", "unity_urp", "godot_4", "roblox_r15")
MANAGEMENT_TOOLS = ("open_asset_session", "set_stage", "session_status", "list_sessions")
SESSION_ID_DOC = "Asset session to act on; omit to use the active (most recently opened or selected) one."
INSTRUCTIONS = (
    "antigravity-3d asset studio. Call open_asset_session first, then work stage by stage: tools outside the "
    "active stage are refused (set_stage switches). Edit only through apply_scene_ir; export_engine_package is "
    "refused until Tier-1 QA passed and run_visual_qa scored >= 3.4 on the current geometry. Replies are one line.")


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
        return (f"SESSION {sid} asset={s.stage.asset_prim.GetName()} engine={s.state.budget.target_engine} "
                f"stage={s.toolset} tris<={s.state.budget.max_tris} workdir={s.workdir}")

    # ---- management tools
    async def open_asset_session(self, asset_name: str, target_engine: str, max_tris: int = 15_000,
                                 workdir: str = "", workspace: str = "") -> str:
        if target_engine not in ENGINES:
            raise ValueError(f"target_engine must be one of {', '.join(ENGINES)}")
        if int(max_tris) < 1:
            raise ValueError("max_tris must be >= 1")
        asset = _slug(asset_name, "Asset")
        await self.backends.ensure()
        sid = f"{asset}-{uuid.uuid4().hex[:6]}"
        root = Path(workspace or os.getcwd())
        wd = Path(workdir) if workdir else root / "agy3d_assets" / sid
        open_session(
            self.registry, sid, wd, asset, EngineBudget(target_engine, max_tris=int(max_tris)),  # type: ignore[arg-type]
            plane=self.backends.plane, forge=self.backends.forge, pool=self.backends.pool,
            state_workspace=root)
        self._set_stage(sid, "BLOCKOUT")
        self.gates[sid] = InvariantAndSafetyGate(self.registry, sid)
        self.active = sid
        return self._line(sid)

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
        return f"STAGE {stage} callable: {names}"

    async def session_status(self, session_id: str = "") -> str:
        return state_header(session(self.registry, self.pick(session_id)))

    async def list_sessions(self) -> str:
        sids = list(self.gates)
        if not sids:
            return "no sessions: call open_asset_session"
        return "\n".join(("* " if sid == self.active else "  ") + self._line(sid) for sid in sids)

    # ---- harness tools
    async def call_tool(self, name: str, fn: T.ToolFn, session_id: str, kwargs: dict[str, Any]) -> str:
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


async def _managed(host: StudioHost, fn: Any, *args: Any, **kwargs: Any) -> str:
    try:
        return await fn(*args, **kwargs)
    except (ValueError, LookupError, RuntimeError, OSError) as e:
        return f"ERROR {compress_exception(e)}"
    except Exception as e:  # noqa: BLE001
        log.exception("management tool failed")
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

    async def tool(**kwargs: Any) -> str:
        sid = kwargs.pop("session_id", "") or ""
        return await host.call_tool(name, fn, sid, kwargs)

    tool.__name__ = tool.__qualname__ = name
    tool.__doc__ = inspect.getdoc(raw)
    tool.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    return tool


def _management(host: StudioHost) -> dict[str, tuple[Any, str]]:
    async def open_asset_session(
        asset_name: str,
        target_engine: Annotated[str, Field(description="unreal_5 | unity_urp | godot_4 | roblox_r15")],
        ctx: Context,
        max_tris: int = 15_000,
        workdir: Annotated[str, Field(description="Session folder; default <workspace>/agy3d_assets/<session id>")] = "",
    ) -> str:
        workspace = await _workspace_root(ctx)
        return await _managed(host, host.open_asset_session, asset_name, target_engine, max_tris, workdir, workspace)

    async def set_stage(stage: str, session_id: Annotated[str, Field(description=SESSION_ID_DOC)] = "") -> str:
        return await _managed(host, host.set_stage, stage, session_id)

    async def session_status(session_id: Annotated[str, Field(description=SESSION_ID_DOC)] = "") -> str:
        return await _managed(host, host.session_status, session_id)

    async def list_sessions() -> str:
        return await _managed(host, host.list_sessions)

    open_asset_session.__doc__ = (
        "Open an asset session (new USD stage) and make it active, stage BLOCKOUT. Call this first; returns the session line.")
    set_stage.__doc__ = ("Switch the active stage tool-set: BLOCKOUT, TWEAK, VARIANTS, LOOKDEV or EXPORT. "
                         "Only that stage's tools are callable.")
    session_status.__doc__ = "Current stage, budget, QA verdict (tier1/tier15/export allowed) and newest audit lines."
    list_sessions.__doc__ = "Open asset sessions; the active one is marked with *."
    return {f.__name__: (f, f.__doc__ or "") for f in (open_asset_session, set_stage, session_status, list_sessions)}


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


def build_server(host: StudioHost | None = None) -> MCPServer:
    host = host or StudioHost()
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
    for name, (fn, doc) in _management(host).items():
        server.add_tool(fn, name=name, description=doc, structured_output=False)
    for name, fn in T.ALL_TOOLS.items():
        w = _wrapper(name, fn, host)
        server.add_tool(w, name=name, description=w.__doc__, structured_output=False)
    server._agy3d_host = host  # type: ignore[attr-defined]
    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = build_server()
    host: StudioHost = server._agy3d_host
    try:
        server.run("stdio")
    finally:
        try:
            asyncio.run(host.backends.close())
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
