"""On-disk QA record shared by the QA tools and the plugin's lifecycle hooks.

Plugin hooks run as separate short-lived processes (``hooks.json`` command
handlers), so they cannot see :class:`antigravity3d.state.SceneSessionRegistry`.
The QA tools (``run_visual_qa`` / ``run_usd_validator``) therefore persist the
gate-relevant facts here, and the hooks read them back with plain ``json``.

Layout (``root`` = ``$AGY3D_STATE_DIR`` or ``<workspace>/.agy3d``)::

    <root>/qa/latest.json            last QA verdict written by any session
    <root>/qa/<key>.json             per session / per conversation verdict
    <root>/hooks/dirty/<conv>.json   written by the post-tool hook after a mutating tool
    <root>/telemetry/tool_calls.jsonl  post-tool telemetry (failure-to-skill input)

The hook scripts in ``plugin/antigravity-3d/hooks/scripts/_common.py`` re-implement
the *reader* side with the stdlib only; ``tests/test_plugin_bundle.py`` checks the
two stay compatible.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from antigravity3d.state import EXPORT_MIN_TIER15_SCORE

SCHEMA = "agy3d.qa_state/1"
_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


def state_root(workspace: Path | str | None = None) -> Path:
    env = os.environ.get("AGY3D_STATE_DIR")
    if env:
        return Path(env)
    return Path(workspace or os.getcwd()) / ".agy3d"


def safe_key(key: str) -> str:
    cleaned = _SAFE_KEY.sub("_", key)[:128]
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unusable state key {key!r}")
    return cleaned


@dataclass(frozen=True)
class QAState:
    tier1_ok: bool
    tier15_score: float
    session_id: str = ""
    conversation_id: str = ""
    asset: str = ""
    ts: float = 0.0
    schema: str = SCHEMA

    @property
    def export_allowed(self) -> bool:
        return bool(self.tier1_ok) and float(self.tier15_score) >= EXPORT_MIN_TIER15_SCORE


def _atomic_write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_qa_state(
    *,
    tier1_ok: bool,
    tier15_score: float,
    session_id: str = "",
    conversation_id: str = "",
    asset: str = "",
    workspace: Path | str | None = None,
) -> QAState:
    """Records a QA verdict. Call from the QA tools after every Tier-1 / Tier-1.5 run."""
    st = QAState(bool(tier1_ok), float(tier15_score), session_id, conversation_id, asset, time.time())
    qa_dir = state_root(workspace) / "qa"
    payload = asdict(st)
    for key in {k for k in (session_id, conversation_id) if k}:
        _atomic_write_json(qa_dir / f"{safe_key(key)}.json", payload)
    _atomic_write_json(qa_dir / "latest.json", payload)
    return st


def read_qa_state(key: str | None = None, workspace: Path | str | None = None) -> QAState | None:
    """Per-key record if present, else ``latest.json``; None when nothing was recorded."""
    qa_dir = state_root(workspace) / "qa"
    candidates = ([qa_dir / f"{safe_key(key)}.json"] if key else []) + [qa_dir / "latest.json"]
    for p in candidates:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("schema") == SCHEMA:
            return QAState(
                bool(d.get("tier1_ok")), float(d.get("tier15_score", 0.0)), str(d.get("session_id", "")),
                str(d.get("conversation_id", "")), str(d.get("asset", "")), float(d.get("ts", 0.0)),
            )
    return None
