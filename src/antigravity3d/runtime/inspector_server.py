"""Server side of the 3D review inspector as an MCP App, served by the studio MCP server.

The inspector used to be a static HTML page shown through a dedicated HTTP sidecar, with review
notes in the browser's ``localStorage``. Antigravity renders MCP Apps natively (harness docs,
"# Developing MCP Apps"), so the studio MCP server (:mod:`antigravity3d.runtime.mcp_server`)
serves it instead:

- resource :data:`APP_URI` (MIME ``text/html;profile=mcp-app``): the app, one self-contained
  document (``antigravity3d/artifacts/web/inspector_app.html``; three.js and the MCP Apps SDK are
  bundled in, so the default ``default-src 'none'`` CSP needs no extra domains);
- model-visible ``open_inspector``: opens the inspector inline, from the side pane ``+`` menu
  (``aux_pane``) or full page (``global``). Every argument is optional (entrypoint launches call it
  with ``{}``); calling it again updates the open view, so the agent can point the reviewer at a
  node, LOD, channel or camera preset. Short text for the model, a small summary as
  ``structuredContent`` for the app;
- model-visible ``review_notes``: the reviewer's notes, current and stale, as text for the agent;
- app-only (``visibility: ["app"]``) tools for everything large or private: ``inspector_payload``
  (geometry/material/texture data in chunks: the stored ``CallToolResult`` of a model tool call is
  capped at 1 MiB, and the docs state no limit for app calls, so every chunk stays well below it),
  ``inspector_qa_state``, ``inspector_list_notes`` and ``inspector_save_note``.

Review notes live with the asset session, in ``<session workdir>/review_notes.json``, so they
survive the frame (removed without teardown), the conversation and a server restart. Each note is
tied to the node and to the *revision* it was written against: a content hash of what the
inspector showed (:func:`antigravity3d.artifacts.inspector.inspector_payload`). After any visible
edit the old notes are kept and reported as stale, never dropped.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from mcp_types import CallToolResult, TextContent
from pydantic import Field

if TYPE_CHECKING:
    from antigravity3d.harness.session import Session

APP_URI = "ui://antigravity-3d/inspector.html"
APP_MIME = "text/html;profile=mcp-app"
APP_NAME = "3D Inspector"
RESOURCE_META: dict[str, Any] = {"ui": {"prefersBorder": True}}
OPEN_TOOL = "open_inspector"
NOTES_TOOL = "review_notes"
APP_TOOLS = ("inspector_payload", "inspector_qa_state", "inspector_list_notes", "inspector_save_note")
MODEL_TOOLS = (OPEN_TOOL, NOTES_TOOL)
TOOL_NAMES = MODEL_TOOLS + APP_TOOLS
# Tool _meta exactly as in the docs' examples: the opening tool points at the resource and declares
# the side-pane and global entrypoints; app-only tools carry only the visibility.
OPEN_META: dict[str, Any] = {
    "ui": {"resourceUri": APP_URI},
    "com.google.antigravity/ui": {"entrypoints": [{"type": "aux_pane", "title": APP_NAME},
                                                  {"type": "global", "title": APP_NAME}]},
}
APP_ONLY_META: dict[str, Any] = {"ui": {"visibility": ["app"]}}
# Characters of payload text per inspector_payload result. AGY3D_TEST_INSPECTOR_CHUNK_CHARS is a test hook
# that forces small chunks so the multi-chunk path is exercised with small assets.
CHUNK_CHARS = int(os.environ.get("AGY3D_TEST_INSPECTOR_CHUNK_CHARS") or 512 * 1024)
MAX_NOTE_CHARS = 20_000
NOTES_FILE = "review_notes.json"
NOTES_SCHEMA = "antigravity3d.review_notes/2"
VIEW_SCHEMA = "antigravity3d.inspector_view/1"
CHANNELS = ("lit", "clay", "albedo", "orm", "occ", "rough", "metal", "normal", "uv", "prim")
PRESETS = ("front", "right", "top", "persp")
SESSION_DOC = "Asset session; omit for the active one."
SessionArg = Annotated[str, Field(description=SESSION_DOC)]


# --------------------------------------------------------------------------- notes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewNotes:
    """Review notes of one asset session, one JSON file in the session folder (atomic writes).

    A note is ``{id, node_id (None = whole asset), text, revision, created_at, updated_at}``. There
    is at most one note per (node, revision): the reviewer edits it in place, an empty text deletes
    it. Notes written against another revision are kept; listings flag them ``stale``.
    """

    def __init__(self, workdir: Path) -> None:
        self.path = Path(workdir) / NOTES_FILE

    def load(self) -> list[dict[str, Any]]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(doc, dict) or doc.get("schema") != NOTES_SCHEMA:
            raise ValueError(f"{self.path}: not a review notes file (expected schema {NOTES_SCHEMA!r})")
        return [n for n in doc.get("notes", []) if isinstance(n, dict) and str(n.get("text", "")).strip()]

    def _write(self, notes: list[dict[str, Any]], asset: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"schema": NOTES_SCHEMA, "asset_name": asset, "notes": notes}
        fd, tmp = tempfile.mkstemp(prefix=".review_notes.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save(self, node_id: str | None, text: str, revision: str, asset: str) -> dict[str, Any] | None:
        notes = self.load()
        hit = next((n for n in notes if n.get("node_id") == node_id and n.get("revision") == revision), None)
        if not text.strip():
            if hit is not None:
                notes.remove(hit)
                self._write(notes, asset)
            return None
        now = _now()
        if hit is None:
            hit = {"id": uuid.uuid4().hex[:12], "node_id": node_id, "text": text, "revision": revision,
                   "created_at": now, "updated_at": now}
            notes.append(hit)
        else:
            hit.update(text=text, updated_at=now)
        self._write(notes, asset)
        return hit

    @staticmethod
    def annotate(notes: list[dict[str, Any]], revision: str, nodes: set[str]) -> list[dict[str, Any]]:
        """Adds ``stale`` (written against another revision) and ``node_missing`` (node gone now)."""
        return [{**n, "stale": n.get("revision") != revision,
                 "node_missing": n.get("node_id") is not None and n.get("node_id") not in nodes} for n in notes]


# --------------------------------------------------------------------------- data for one session


@dataclass
class _View:
    key: tuple
    payload: dict[str, Any]
    scene: str  # payload as JSON text (chunked by inspector_payload)
    parts: dict[str, str]  # "glb": the review GLB, base64

    @property
    def revision(self) -> str:
        return str(self.payload["revision"])

    @property
    def nodes(self) -> set[str]:
        return {m["id"] for m in self.payload["nodes"]}

    def text(self, part: str) -> str:
        if part == "scene":
            return self.scene
        if part in self.parts:
            return self.parts[part]
        raise LookupError(f"unknown part {part!r}")


def _build_view(s: Session, key: tuple) -> _View:
    """The inspector payload for the session's current composed stage (caller holds the session lock).

    LOD0 is the stage geometry. When the EXPORT stage has derived the export bundle for this geometry
    (``generate_lods_and_colliders``), its seam-split UV meshes, per-node LOD chains and convex hulls are shown
    instead, exactly what the package GLBs are written from. Materials: :func:`.review_glb.session_materials`.
    Everything is one review GLB (part ``glb``, base64) plus its metadata (part ``scene``).
    """
    import base64

    from antigravity3d.artifacts.inspector import inspector_payload
    from antigravity3d.artifacts.review_glb import session_materials
    from antigravity3d.kernel.types import MeshData

    meshes = s.stage.extract_meshes()
    cached = s._res("bundle")
    bundle = cached[1] if cached and cached[0] == (s.geom_version, s.state.budget) else None
    materials = session_materials(s.stage.stage, meshes)
    uvs: dict[str, Any] = {}
    lods: Any = None
    colliders = None
    if bundle is not None:
        by_id = {u.node_id: u for u in bundle.uvs}
        meshes = [MeshData(m.node_id, m.prim_path, by_id[m.node_id].points, by_id[m.node_id].triangles,
                           m.material_id, m.collider) if m.node_id in by_id else m for m in meshes]
        uvs = {u.node_id: u.uvs for u in bundle.uvs}
        if bundle.lods and set(bundle.lods) == {m.node_id for m in meshes}:
            lods = bundle.lods  # {node_id: [LOD0, LOD1, ...]}, as the package's *_LODn.glb are written
        colliders = [(h.name, h.points, h.triangles) for h in bundle.colliders]
    payload, glb = inspector_payload(meshes, asset_name=s.stage.asset_prim.GetName(), budget=s.state.budget,
                                     materials=materials, lods=lods, colliders=colliders, uvs=uvs)
    payload["session_id"] = s.sid
    scene = json.dumps(payload, separators=(",", ":"))
    return _View(key, payload, scene, {"glb": base64.b64encode(glb).decode("ascii")})


def qa_state(s: Session) -> dict[str, Any]:
    """The session's QA gate fields in the shape the inspector's QA panel renders."""
    from antigravity3d.harness.tools import export_block_reason
    from antigravity3d.state import EXPORT_MIN_TIER15_SCORE

    st = s.state
    verdict = st.verdict
    scored = verdict is not None or st.last_tier15_score > 0
    return {
        "tier1_ok": bool(st.last_tier1_ok),
        "tier1_line": "" if st.last_tier1_ok else "Tier-1 has not passed for the current scene",
        "tier15_score": float(st.last_tier15_score) if scored else None,
        "threshold": EXPORT_MIN_TIER15_SCORE,
        "export_allowed": bool(st.export_allowed),
        "export_block_reason": export_block_reason(st) if not st.export_allowed else None,
        "validator_ok": st.validator_ok,
        "verdict": None if verdict is None else {"stage": verdict.stage, "renderer": verdict.renderer,
                                                 "backend": verdict.backend, "action": verdict.action},
        "audit_deltas": list(st.audit_deltas),
        "defects": {},
        "stage": s.toolset,
    }


def _result(text: str, structured: dict[str, Any] | None = None, *, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=structured, is_error=error)


def _one_line(text: str) -> str:
    return " / ".join(part.strip() for part in text.strip().splitlines() if part.strip())


# --------------------------------------------------------------------------- the service


class InspectorService:
    """The inspector's tools, bound to one :class:`~antigravity3d.runtime.mcp_server.StudioHost`.

    Every call runs through ``host.run_ordered`` (the server's FIFO lock), like the other session
    management tools; payload building additionally holds the session's registry lock.
    """

    def __init__(self, host: Any) -> None:
        self.host = host
        self._views: dict[str, _View] = {}

    # ---- helpers
    def _session(self, session_id: str) -> Session:
        from antigravity3d.harness.session import session

        return session(self.host.registry, self.host.pick(session_id))

    async def _view(self, s: Session) -> _View:
        async with s.registry.locked(s.sid):
            cached = s._res("bundle")
            key = (s.state.revision, s.geom_version, id(cached[1]) if cached else None, s.state.budget)
            view = self._views.get(s.sid)
            if view is None or view.key != key:
                view = self._views[s.sid] = _build_view(s, key)
            return view

    def _notes(self, s: Session, view: _View) -> list[dict[str, Any]]:
        return ReviewNotes.annotate(ReviewNotes(s.workdir).load(), view.revision, view.nodes)

    @staticmethod
    def _counts(notes: list[dict[str, Any]]) -> dict[str, int]:
        stale = sum(1 for n in notes if n["stale"])
        return {"current": len(notes) - stale, "stale": stale}

    # ---- model-visible
    async def open_inspector(self, session_id: str = "", node: str = "", lod: int | None = None, channel: str = "",
                             view: str = "", wireframe: bool | None = None,
                             colliders: bool | None = None) -> CallToolResult:
        if not session_id and self.host.active is None:
            msg = "No asset session is open yet: the agent opens one with open_asset_session."
            return _result("INSPECTOR no asset session: call open_asset_session first, then open_inspector",
                           {"schema": VIEW_SCHEMA, "session_id": None, "message": msg})
        s = self._session(session_id)
        v = await self._view(s)
        p = v.payload
        directives: dict[str, Any] = {}
        if node:
            if node == "*":
                directives["node"] = None
            elif node not in v.nodes:
                raise ValueError(f"unknown node {node!r}; nodes: {', '.join(sorted(v.nodes)) or '(none)'}; '*' = whole asset")
            else:
                directives["node"] = node
        if lod is not None:
            if not 0 <= int(lod) < len(p["levels"]):
                raise ValueError(f"lod must be 0..{len(p['levels']) - 1} (LOD1+ exist after generate_lods_and_colliders)")
            directives["lod"] = int(lod)
        if channel:
            if channel not in CHANNELS:
                raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
            directives["channel"] = channel
        if view:
            if view not in PRESETS:
                raise ValueError(f"view must be one of {', '.join(PRESETS)}")
            directives["preset"] = view
        if wireframe is not None:
            directives["wireframe"] = bool(wireframe)
        if colliders is not None:
            directives["colliders"] = bool(colliders)
        notes = self._counts(self._notes(s, v))
        qa = qa_state(s)
        derived = any(m["uv"] for m in p["nodes"])  # the export bundle (UVs, LODs, hulls) exists for this geometry
        textured = sorted({n["mat"] for n in p["nodes"] if n["maps"]})
        tex_note = (f"{len(textured)} textured materials" if textured else "none") if derived else \
            "not yet (generate_lods_and_colliders derives UVs, LODs and hulls)"
        summary = {
            "schema": VIEW_SCHEMA, "session_id": s.sid, "asset": p["asset_name"], "engine": p["budget"]["target_engine"],
            "stage": s.toolset, "revision": v.revision, "budget": p["budget"],
            "counts": {"nodes": len(p["nodes"]), "tris": p["summary"]["tris"], "lods": len(p["levels"]) - 1,
                       "colliders": len(p["colliders"]), "textured_materials": len(textured), "glb_bytes": p["glb"]["bytes"],
                       "scene_chunks": max(1, -(-len(v.scene) // CHUNK_CHARS))},
            "notes": notes,
            "qa": {k: qa[k] for k in ("tier1_ok", "tier15_score", "export_allowed")},
            "view": directives,
        }
        shown = " ".join(f"{k}={'*' if val is None else val}" for k, val in directives.items())
        text = (f"INSPECTOR {s.sid} asset={p['asset_name']} rev={v.revision} nodes={len(p['nodes'])} "
                f"tris={p['summary']['tris']} lods={len(p['levels']) - 1} colliders={len(p['colliders'])} "
                f"textures={tex_note} "
                f"notes={notes['current']} current, {notes['stale']} stale" + (f" | view {shown}" if shown else ""))
        return _result(text, summary)

    async def review_notes(self, session_id: str = "", include_stale: bool = True) -> str:
        s = self._session(session_id)
        v = await self._view(s)
        notes = self._notes(s, v)
        c = self._counts(notes)
        head = f"NOTES {s.sid} asset={v.payload['asset_name']} rev={v.revision}: {c['current']} current, {c['stale']} stale"
        shown = [n for n in notes if include_stale or not n["stale"]]
        if not shown:
            return head + " (no review notes yet: the reviewer writes them in the inspector, open_inspector)"
        shown.sort(key=lambda n: (n["stale"], str(n.get("updated_at", ""))))
        lines = [head]
        for n in shown:
            tag = f"stale, rev {n.get('revision')}" if n["stale"] else "current"
            if n["node_missing"]:
                tag += ", node no longer exists"
            target = "whole asset" if n.get("node_id") is None else f"node {n['node_id']}"
            lines.append(f"- [{tag}] {target} ({n.get('updated_at', '')}): {_one_line(str(n['text']))}")
        return "\n".join(lines)

    # ---- app-only
    async def payload(self, session_id: str = "", revision: str = "", part: str = "scene", chunk: int = 0) -> CallToolResult:
        s = self._session(session_id)
        v = await self._view(s)
        if revision and revision != v.revision:
            return _result(f"ERROR revision changed: {revision} is no longer current (now {v.revision}); reload",
                           {"current_revision": v.revision}, error=True)
        text = v.text(part)
        chunks = max(1, -(-len(text) // CHUNK_CHARS))
        if not 0 <= int(chunk) < chunks:
            raise ValueError(f"chunk must be 0..{chunks - 1}")
        data = text[int(chunk) * CHUNK_CHARS:(int(chunk) + 1) * CHUNK_CHARS]
        return _result(f"PAYLOAD {part} {int(chunk) + 1}/{chunks} rev={v.revision}",
                       {"session_id": s.sid, "revision": v.revision, "part": part, "chunk": int(chunk),
                        "chunks": chunks, "data": data})

    async def qa(self, session_id: str = "") -> CallToolResult:
        s = self._session(session_id)
        v = await self._view(s)
        qa = qa_state(s)
        return _result(f"QA tier1={'ok' if qa['tier1_ok'] else 'fail'} tier15={qa['tier15_score']} "
                       f"export={'allowed' if qa['export_allowed'] else 'blocked'} rev={v.revision}",
                       {"session_id": s.sid, "revision": v.revision, "qa": qa})

    async def list_notes(self, session_id: str = "") -> CallToolResult:
        s = self._session(session_id)
        v = await self._view(s)
        notes = self._notes(s, v)
        c = self._counts(notes)
        return _result(f"NOTES {c['current']} current, {c['stale']} stale",
                       {"session_id": s.sid, "revision": v.revision, "notes": notes, "counts": c})

    async def save_note(self, text: str, session_id: str = "", node_id: str = "", revision: str = "") -> CallToolResult:
        s = self._session(session_id)
        v = await self._view(s)
        if len(text) > MAX_NOTE_CHARS:
            raise ValueError(f"note too long ({len(text)} > {MAX_NOTE_CHARS} characters)")
        rev = revision or v.revision
        nid = node_id or None
        if rev == v.revision and nid is not None and nid not in v.nodes:
            raise ValueError(f"unknown node {nid!r}")
        note = ReviewNotes(s.workdir).save(nid, text, rev, v.payload["asset_name"])
        notes = self._notes(s, v)
        c = self._counts(notes)
        what = "deleted" if note is None else ("saved (stale: the view is behind the scene)" if rev != v.revision else "saved")
        return _result(f"NOTE {what} {nid or 'whole asset'}",
                       {"session_id": s.sid, "revision": v.revision, "note": note, "notes": notes, "counts": c})


# --------------------------------------------------------------------------- MCP registration


def register(server: Any, host: Any) -> InspectorService:
    """Adds the inspector's resource and tools to the studio ``MCPServer`` (see the module docstring)."""
    from antigravity3d.artifacts.inspector import app_html

    svc = InspectorService(host)

    async def open_inspector(
        session_id: SessionArg = "",
        node: Annotated[str, Field(description="Node id to select and highlight; '*' selects the whole asset.")] = "",
        lod: Annotated[int | None, Field(description="LOD level to show (0 = full detail).")] = None,
        channel: Annotated[str, Field(description="lit | clay | albedo | orm | occ | rough | metal | normal | uv | prim")] = "",
        view: Annotated[str, Field(description="Camera preset: front | right | top | persp")] = "",
        wireframe: bool | None = None,
        colliders: Annotated[bool | None, Field(description="Show the collision hull overlay.")] = None,
    ) -> CallToolResult:
        return await host.run_ordered(svc.open_inspector, session_id, node, lod, channel, view, wireframe, colliders)

    async def review_notes(session_id: SessionArg = "", include_stale: bool = True) -> str:
        return await host.run_ordered(svc.review_notes, session_id, include_stale)

    async def inspector_payload(session_id: str = "", revision: str = "", part: str = "scene", chunk: int = 0) -> CallToolResult:
        return await host.run_ordered(svc.payload, session_id, revision, part, chunk)

    async def inspector_qa_state(session_id: str = "") -> CallToolResult:
        return await host.run_ordered(svc.qa, session_id)

    async def inspector_list_notes(session_id: str = "") -> CallToolResult:
        return await host.run_ordered(svc.list_notes, session_id)

    async def inspector_save_note(text: str, session_id: str = "", node_id: str = "", revision: str = "") -> CallToolResult:
        return await host.run_ordered(svc.save_note, text, session_id, node_id, revision)

    server.add_tool(open_inspector, name=OPEN_TOOL, title=APP_NAME, meta=OPEN_META, structured_output=False, description=(
        "Show the asset in the reviewer's 3D inspector (inline; also in the side pane). Calling it again updates the "
        "open view: select a node, switch LOD, channel or camera. The reviewer's notes come back via review_notes."))
    server.add_tool(review_notes, name=NOTES_TOOL, structured_output=False, description=(
        "Review notes the reviewer wrote in the 3D inspector, per node; notes written against an older revision are "
        "marked stale."))
    for fn, doc in ((inspector_payload, "Inspector app: one chunk of the review metadata ('scene') or review GLB ('glb') of a revision."),
                    (inspector_qa_state, "Inspector app: QA gate state of the session."),
                    (inspector_list_notes, "Inspector app: all review notes, with stale flags."),
                    (inspector_save_note, "Inspector app: save (empty text deletes) the note for a node and revision.")):
        server.add_tool(fn, name=fn.__name__, description=doc, meta=APP_ONLY_META, structured_output=False)

    def inspector_app() -> str:
        return app_html()

    server.resource(APP_URI, name="antigravity-3d inspector", title=APP_NAME, mime_type=APP_MIME, meta=RESOURCE_META,
                    description="3D review inspector (MCP App): geometry, LODs, colliders, PBR channels, budget, QA, notes")(inspector_app)
    return svc


def ui_meta_problems(tools: list[tuple[str, dict[str, Any] | None]], resources: list[tuple[str, str | None]]) -> list[str]:
    """Checks tool and resource ``_meta`` against the rules the host enforces (MCP Apps docs, "Rules the
    host enforces"): ``resourceUri`` starts with ``ui://`` and names a resource of this server with MIME
    ``text/html;profile=mcp-app``; ``visibility`` only holds ``model``/``app``; entrypoints only on tools
    with a ``resourceUri`` and only of the known types."""
    errs: list[str] = []
    ui_resources = {uri: mime for uri, mime in resources if uri.startswith("ui://")}
    for uri, mime in ui_resources.items():
        if mime != APP_MIME:
            errs.append(f"resource {uri}: MIME must be {APP_MIME!r}, got {mime!r}")
    for name, meta in tools:
        ui = (meta or {}).get("ui") or {}
        uri = ui.get("resourceUri")
        if uri is not None:
            if not str(uri).startswith("ui://"):
                errs.append(f"tool {name}: ui.resourceUri must start with ui:// (the host drops it otherwise)")
            elif uri not in ui_resources:
                errs.append(f"tool {name}: ui.resourceUri {uri} is not a resource of this server")
        vis = ui.get("visibility")
        if vis is not None and (not isinstance(vis, list) or set(vis) - {"model", "app"}):
            errs.append(f"tool {name}: ui.visibility must be a list of 'model' and/or 'app'")
        eps = ((meta or {}).get("com.google.antigravity/ui") or {}).get("entrypoints")
        if eps:
            if uri is None:
                errs.append(f"tool {name}: entrypoints are only kept on tools that declare ui.resourceUri")
            for ep in eps:
                if ep.get("type") not in {"aux_pane", "global"}:
                    errs.append(f"tool {name}: unknown entrypoint type {ep.get('type')!r} (dropped by the host)")
    return errs
