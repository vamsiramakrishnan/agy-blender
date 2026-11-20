"""Session state shared between tools (ToolContext) and hooks (HookContext).

google-antigravity gives tools and hooks separate state stores, so both sides
look the session up in a process-wide :class:`SceneSessionRegistry` instead.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

TargetEngine = Literal["unreal_5", "unity_urp", "godot_4", "roblox_r15"]
PipelineStage = Literal["ROUTE", "BLOCKOUT", "LOOKDEV", "EXPORT", "DONE"]

MAX_AUDIT_DELTAS = 8
EXPORT_MIN_TIER15_SCORE = 3.4


@dataclass(frozen=True)
class EngineBudget:
    target_engine: TargetEngine
    max_tris: int = 15_000
    max_draw_calls: int = 4
    texel_density_px_per_m: float = 1024.0
    max_uv_stretch: float = 0.05
    max_collision_hulls: int = 8
    require_manifold: bool = True


@dataclass(frozen=True)
class VisualVerdict:
    revision: str
    target: str
    stage: str
    renderer: str
    backend: str
    action: str
    texture_hash: str = ""

    def qualifies(self, revision: str, target: str) -> bool:
        return (bool(revision and target) and self.revision == revision and self.target == target
                and self.stage in {"lookdev", "final"} and self.renderer in {"eevee", "cycles"}
                and self.backend == "decisions" and self.action == "approve")


@dataclass
class AssetSessionState:
    session_id: str
    usd_stage_path: Path
    budget: EngineBudget
    active_stage: PipelineStage = "ROUTE"
    tx_stack: list[str] = field(default_factory=list)  # anonymous USD layer ids
    style_anchor_uri: str | None = None  # Files API URI for the studio art bible
    ortho_uris: list[str] = field(default_factory=list)
    paintover_uri: str | None = None
    last_tier1_ok: bool = False
    last_tier15_score: float = 0.0
    revision: str = field(default_factory=lambda: uuid.uuid4().hex)
    verdict: VisualVerdict | None = None
    validator_ok: bool | None = None
    audit_deltas: list[str] = field(default_factory=list)

    def add_audit_delta(self, line: str) -> None:
        """Appends one audit line, keeping only the newest MAX_AUDIT_DELTAS."""
        self.audit_deltas.append(line)
        del self.audit_deltas[:-MAX_AUDIT_DELTAS]

    @property
    def export_allowed(self) -> bool:
        return (self.validator_ok is not False and self.last_tier1_ok and EXPORT_MIN_TIER15_SCORE <= self.last_tier15_score <= 4.0
                and self.verdict is not None and self.verdict.qualifies(self.revision, self.budget.target_engine))


class SceneSessionRegistry:
    """Process-wide session table keyed by session id.

    Mutations of a session go through ``async with registry.locked(sid)`` when
    they span an await; plain reads and single-field writes need no lock.
    Non-serialisable live objects (the open USD stage manager, Blender pool
    handles) are attached per session with ``attach`` / ``resource``.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AssetSessionState] = {}
        self._resources: dict[str, dict[str, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = threading.RLock()

    def create(self, state: AssetSessionState) -> AssetSessionState:
        with self._guard:
            if state.session_id in self._sessions:
                raise KeyError(f"session already registered: {state.session_id}")
            self._sessions[state.session_id] = state
            self._resources[state.session_id] = {}
            return state

    def get(self, session_id: str) -> AssetSessionState:
        with self._guard:
            try:
                return self._sessions[session_id]
            except KeyError:
                raise KeyError(f"unknown session: {session_id}") from None

    def __contains__(self, session_id: str) -> bool:
        with self._guard:
            return session_id in self._sessions

    def attach(self, session_id: str, name: str, resource: object) -> None:
        with self._guard:
            self.get(session_id)
            self._resources[session_id][name] = resource

    def resource(self, session_id: str, name: str) -> object:
        with self._guard:
            self.get(session_id)
            try:
                return self._resources[session_id][name]
            except KeyError:
                raise KeyError(f"session {session_id} has no resource {name!r}") from None

    def locked(self, session_id: str) -> asyncio.Lock:
        with self._guard:
            self.get(session_id)
            return self._locks.setdefault(session_id, asyncio.Lock())

    def drop(self, session_id: str) -> None:
        with self._guard:
            self._sessions.pop(session_id, None)
            self._resources.pop(session_id, None)
            self._locks.pop(session_id, None)
