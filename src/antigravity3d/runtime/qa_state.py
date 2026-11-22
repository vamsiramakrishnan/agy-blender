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

from antigravity3d.state import EXPORT_MIN_TIER15_SCORE, VisualVerdict

SCHEMA = "agy3d.qa_state/2"
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
    revision: str = ""
    target: str = ""
    verdict: VisualVerdict | None = None
    validator_ok: bool | None = None
    ts: float = 0.0
    schema: str = SCHEMA
    preview: bool = False  # an unverified preview session; absent in older records, which are therefore never previews
    # What only the user can decide before this session can be reviewed or delivered ("" when nothing): a session that
    # is not a preview on a key without the calibrated judge. The hooks then let the agent stop and ask, instead of
    # telling it to iterate (Session.waits_for_user). Absent in older records: nothing.
    waits_for_user: str = ""

    @property
    def export_allowed(self) -> bool:
        reviewed = self.preview is True and self.verdict is not None and self.verdict.delivers_on_review
        floor = 0.0 if reviewed else EXPORT_MIN_TIER15_SCORE  # an uncalibrated preview's score is a finding (VisualVerdict)
        base = (self.validator_ok is not False and self.tier1_ok is True and floor <= self.tier15_score <= 4.0
                and self.verdict is not None)
        if self.preview is True:
            return base and self.verdict.qualifies_preview(self.revision, self.target)  # type: ignore[union-attr]
        return base and self.verdict.qualifies(self.revision, self.target)  # type: ignore[union-attr]


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
    revision: str = "",
    target: str = "",
    verdict: VisualVerdict | None = None,
    validator_ok: bool | None = None,
    workspace: Path | str | None = None,
    preview: bool = False,
    waits_for_user: str = "",
) -> QAState:
    """Records a QA verdict. Call from the QA tools after every Tier-1 / Tier-1.5 run."""
    st = QAState(bool(tier1_ok), float(tier15_score), session_id, conversation_id, asset, revision, target, verdict, validator_ok,
                 time.time(), preview=bool(preview), waits_for_user=str(waits_for_user or ""))
    qa_dir = state_root(workspace) / "qa"
    payload = asdict(st)
    for key in dict.fromkeys(k for k in (session_id, conversation_id) if k):
        _atomic_write_json(qa_dir / f"{safe_key(key)}.json", payload)
    _atomic_write_json(qa_dir / "latest.json", payload)
    return st


def read_qa_state(key: str | None = None, workspace: Path | str | None = None) -> QAState | None:
    """Read the exact session/conversation key. Only an unkeyed diagnostic read uses latest."""
    name = safe_key(key) if key else "latest"
    try:
        data = json.loads((state_root(workspace) / "qa" / f"{name}.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return None
        if key and key not in (data.get("session_id"), data.get("conversation_id")):
            return None
        if data.get("verdict") is not None:
            data["verdict"] = VisualVerdict(**data["verdict"])
        return QAState(**data)
    except (OSError, ValueError, TypeError, KeyError):
        return None
