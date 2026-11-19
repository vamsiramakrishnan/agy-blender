"""Session wiring shared by harness tools and hooks.

Tools receive a ``ToolContext`` and hooks a ``HookContext``; the SDK keeps their state stores
apart, so both resolve the live session through a :class:`SceneSessionRegistry`. Tools map
``ctx.conversation_id`` -> session through :func:`bind_conversation` (set when the stage agent is
built, with the conversation id pinned in its config) and fall back to a session id stored in the
ToolContext state under :data:`CTX_SESSION_KEY`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from antigravity3d.decisions.compose import Thresholds
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.genmedia.nano_banana import ImageForge
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import AssetSessionState, EngineBudget, SceneSessionRegistry

DEFAULT_REGISTRY = SceneSessionRegistry()
CTX_SESSION_KEY = "agy3d.session_id"
CTX_REGISTRY_KEY = "agy3d.registry"

# resource names attached per session
R_STAGE, R_PLANE, R_FORGE, R_POOL = "stage", "plane", "forge", "pool"
R_THRESHOLDS, R_WORKDIR, R_ARTIFACTS, R_EXPORTS = "thresholds", "workdir", "artifacts_dir", "export_dir"
R_PREVIEWS, R_GEOM_VERSION, R_TOOLSET, R_LAST_EXPORT = "previews", "geom_version", "toolset", "last_export"

_bindings: dict[str, tuple[SceneSessionRegistry, str]] = {}
_bind_guard = threading.Lock()


@dataclass(frozen=True)
class Preview:
    path: Path
    uri: str
    geom_version: int
    index: int


@dataclass
class Session:
    """Typed view over one registered session and its live resources."""

    registry: SceneSessionRegistry
    state: AssetSessionState
    stage: StageManager
    plane: DecisionControlPlane | None
    forge: ImageForge | None
    pool: Any
    thresholds: Thresholds
    workdir: Path
    artifacts_dir: Path
    export_dir: Path

    @property
    def sid(self) -> str:
        return self.state.session_id

    def _res(self, name: str, default: Any = None) -> Any:
        try:
            return self.registry.resource(self.sid, name)
        except KeyError:
            return default

    def _set(self, name: str, value: Any) -> None:
        self.registry.attach(self.sid, name, value)

    @property
    def geom_version(self) -> int:
        return int(self._res(R_GEOM_VERSION, 0))

    @property
    def previews(self) -> list[Preview]:
        return self._res(R_PREVIEWS, [])

    @property
    def toolset(self) -> str:
        return str(self._res(R_TOOLSET, self.state.active_stage))

    @property
    def last_export(self) -> Path | None:
        return self._res(R_LAST_EXPORT)

    def set_last_export(self, path: Path | None) -> None:
        self._set(R_LAST_EXPORT, path)

    def add_preview(self, path: Path, uri: str, geom_version: int | None = None) -> Preview:
        version = self.geom_version if geom_version is None else geom_version
        p = Preview(path, uri, version, len(self.previews) + 1)
        self._set(R_PREVIEWS, [*self.previews, p])
        return p

    def latest_preview(self) -> Preview | None:
        """The newest contact sheet, only if it still matches the current geometry."""
        prev = self.previews
        return prev[-1] if prev and prev[-1].geom_version == self.geom_version else None

    def sync_tx(self) -> None:
        """Mirrors the stage manager's transaction stack into the serialisable state."""
        self.state.tx_stack = list(self.stage.tx_stack)

    def visuals_changed(self) -> None:
        """Any geometry/material change: invalidate the visual score and stale previews."""
        self._set(R_GEOM_VERSION, self.geom_version + 1)
        self.state.last_tier15_score = 0.0
        self.sync_tx()


def open_session(
    registry: SceneSessionRegistry | None,
    session_id: str,
    workdir: str | Path,
    asset_name: str,
    budget: EngineBudget,
    *,
    plane: DecisionControlPlane | None,
    forge: ImageForge | None,
    pool: Any = None,
    thresholds: Thresholds | None = None,
) -> AssetSessionState:
    """Creates the on-disk stage under ``workdir``, registers the state and attaches resources."""
    registry = registry if registry is not None else DEFAULT_REGISTRY
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    artifacts, exports = workdir / "artifacts", workdir / "export"
    artifacts.mkdir(exist_ok=True)
    exports.mkdir(exist_ok=True)
    usd_path = workdir / f"{asset_name}.usda"
    stage = StageManager.create(usd_path, asset_name)
    state = registry.create(AssetSessionState(session_id=session_id, usd_stage_path=usd_path, budget=budget))
    for name, res in (
        (R_STAGE, stage), (R_PLANE, plane), (R_FORGE, forge), (R_POOL, pool),
        (R_THRESHOLDS, thresholds or Thresholds()), (R_WORKDIR, workdir), (R_ARTIFACTS, artifacts),
        (R_EXPORTS, exports), (R_PREVIEWS, []), (R_GEOM_VERSION, 0), (R_LAST_EXPORT, None),
    ):
        registry.attach(session_id, name, res)
    state.tx_stack = list(stage.tx_stack)
    return state


def session(registry: SceneSessionRegistry, session_id: str) -> Session:
    r = lambda n: registry.resource(session_id, n)  # noqa: E731
    return Session(
        registry, registry.get(session_id), r(R_STAGE), r(R_PLANE), r(R_FORGE), r(R_POOL), r(R_THRESHOLDS),
        r(R_WORKDIR), r(R_ARTIFACTS), r(R_EXPORTS),
    )


def set_toolset(registry: SceneSessionRegistry, session_id: str, toolset: str) -> None:
    """Records which stage tool-set the running agent was built with (the gate scopes to it)."""
    registry.attach(session_id, R_TOOLSET, toolset)


def bind_conversation(conversation_id: str, session_id: str, registry: SceneSessionRegistry | None = None) -> None:
    with _bind_guard:
        _bindings[conversation_id] = (registry if registry is not None else DEFAULT_REGISTRY, session_id)


def unbind_conversation(conversation_id: str) -> None:
    with _bind_guard:
        _bindings.pop(conversation_id, None)


def resolve(ctx: Any) -> Session:
    """ToolContext -> Session. Raises LookupError with a one-line reason when unbound."""
    conv = ""
    try:
        conv = ctx.conversation_id or ""
    except Exception:  # noqa: BLE001 - a ToolContext without a live conversation
        conv = ""
    with _bind_guard:
        bound = _bindings.get(conv) if conv else None
    if bound is None:
        sid = ctx.get_state(CTX_SESSION_KEY)
        if not sid:
            raise LookupError(f"no asset session bound to conversation {conv or '<none>'}")
        bound = (ctx.get_state(CTX_REGISTRY_KEY) or DEFAULT_REGISTRY, sid)
    return session(*bound)
