"""Failure-to-skill log and clustering.

Every expensive-model fix for a defect the cheap checks flagged is appended to
a JSON Lines log. Recurring fixes are clustered by their signature so they can
be turned into deterministic guardrails.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TOLERANCE = 1e-9


@dataclass(frozen=True)
class FixRecord:
    asset_id: str
    defect_vector: dict[str, float]  # defect id -> probability before the fix
    ops: list[str]  # names of the IR ops in the fix
    params_changed: list[str]  # e.g. ["lid.size", "hinge.translate"]
    score_before: float
    score_after: float
    note: str = ""
    rule: str = ""  # set only when the observed IR delta equals a built-in candidate
    scene_hash: str = ""
    origin: str = "unknown"


@dataclass(frozen=True)
class FixCluster:
    defects: tuple[str, ...]
    ops: tuple[str, ...]
    count: int
    mean_gain: float
    common_params: tuple[str, ...]
    example_asset_ids: tuple[str, ...]


_REQUIRED = ("asset_id", "defect_vector", "ops", "params_changed", "score_before", "score_after")


def _record_from_dict(obj: Any) -> FixRecord:
    if not isinstance(obj, dict):
        raise ValueError("record is not a JSON object")
    missing = [k for k in _REQUIRED if k not in obj]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    asset_id = obj["asset_id"]
    defect_vector = obj["defect_vector"]
    ops = obj["ops"]
    params = obj["params_changed"]
    note = obj.get("note", "")
    if not isinstance(asset_id, str):
        raise ValueError("asset_id must be a string")
    if not isinstance(defect_vector, dict) or not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in defect_vector.values()
    ):
        raise ValueError("defect_vector must map names to numbers")
    if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
        raise ValueError("ops must be a list of strings")
    if not isinstance(params, list) or not all(isinstance(p, str) for p in params):
        raise ValueError("params_changed must be a list of strings")
    for key in ("score_before", "score_after"):
        value = obj[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{key} must be a number")
    if not isinstance(note, str):
        raise ValueError("note must be a string")
    return FixRecord(
        asset_id=asset_id,
        defect_vector={str(k): float(v) for k, v in defect_vector.items()},
        ops=list(ops),
        params_changed=list(params),
        score_before=float(obj["score_before"]),
        score_after=float(obj["score_after"]),
        note=note,
        rule=str(obj.get("rule", "")),
        scene_hash=str(obj.get("scene_hash", "")),
        origin=str(obj.get("origin", "unknown")),
    )


class FixLog:
    """Append-only JSON Lines log of fix records."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.skipped = 0
        self._index_path: Path | None = None

    @classmethod
    def for_prop(cls, output: Path) -> FixLog:
        """Keep the per-run log and index it even when --out is outside the workspace."""
        from antigravity3d.runtime.qa_state import state_root

        path = (Path(output) / "fixlog.jsonl").resolve()
        key = hashlib.sha256(str(path).encode()).hexdigest()
        log = cls(path)
        log._index_path = state_root() / "fixlogs" / f"{key}.json"
        return log

    def append(self, record: FixRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = dataclasses.asdict(record)
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(payload, sort_keys=True, allow_nan=False)
        if self._index_path is not None:
            from antigravity3d.runtime.qa_state import _atomic_write_json

            _atomic_write_json(self._index_path, {"path": str(self.path)})
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read(self, *, since: float | None = None) -> list[FixRecord]:
        """Read all valid records. Blank and malformed lines are skipped and counted in ``skipped``."""
        self.skipped = 0
        if not self.path.exists():
            return []
        records: list[FixRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    record = _record_from_dict(raw)
                    if since is not None:
                        ts = datetime.fromisoformat(str(raw.get("ts", "")))
                        if ts.tzinfo is None:
                            raise ValueError("fix timestamp lacks timezone")
                        if ts.timestamp() < since:
                            continue
                    records.append(record)
                except (ValueError, TypeError):
                    self.skipped += 1
        return records


def signature(record: FixRecord, threshold: float = 0.5) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Clustering key: (defects at or above ``threshold``, unique op names)."""
    defects = tuple(sorted(k for k, v in record.defect_vector.items() if v >= threshold))
    ops = tuple(sorted(set(record.ops)))
    return defects, ops


def _generic_param(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _common_params(group: list[FixRecord]) -> tuple[str, ...]:
    counts: dict[str, int] = defaultdict(int)
    for record in group:
        for name in {_generic_param(p) for p in record.params_changed}:
            counts[name] += 1
    half = len(group) / 2
    return tuple(sorted(name for name, n in counts.items() if n >= half))


def cluster(
    records: list[FixRecord],
    *,
    min_count: int = 3,
    min_gain: float = 0.3,
    threshold: float = 0.5,
) -> list[FixCluster]:
    """Group records by signature and keep recurring, useful fixes.

    Sorted by ``count * mean_gain`` descending, ties broken by defects then ops.
    """
    groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[FixRecord]] = defaultdict(list)
    for record in records:
        groups[signature(record, threshold)].append(record)

    clusters: list[FixCluster] = []
    for (defects, ops), group in groups.items():
        if len(group) < min_count:
            continue
        mean_gain = sum(r.score_after - r.score_before for r in group) / len(group)
        if mean_gain < min_gain - _TOLERANCE:
            continue
        clusters.append(
            FixCluster(
                defects=defects,
                ops=ops,
                count=len(group),
                mean_gain=mean_gain,
                common_params=_common_params(group),
                example_asset_ids=tuple(r.asset_id for r in group[:3]),
            )
        )

    clusters.sort(key=lambda c: (-(c.count * c.mean_gain), c.defects, c.ops))
    return clusters


def observed_fixes(clusters: list[FixCluster]) -> str:
    """Describe historical co-occurrence. Promotion requires independent replay."""
    heading = "## Observed repair clusters"
    if not clusters:
        return f"{heading}\n\n_No recurring fixes yet._\n"
    bullets = []
    for c in clusters:
        params = ", ".join(c.common_params) if c.common_params else "various parameters"
        bullets.append(
            f"- When **{', '.join(c.defects)}** is flagged, the fix that worked {c.count} times "
            f"(mean gain +{c.mean_gain:.2f}) was: {' + '.join(c.ops)} on {params}. "
            f"Examples: {', '.join(c.example_asset_ids)}."
        )
    return f"{heading}\n\n" + "\n".join(bullets) + "\n"


# Keep the legacy import working, including for clients reading older FixLog records.
propose_rules = observed_fixes
