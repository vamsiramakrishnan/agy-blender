#!/usr/bin/env python3
"""PostToolUse telemetry logger for antigravity-3d (wired in ../../hooks.json).

Appends one JSON line per tool call to ``<state>/telemetry/tool_calls.jsonl``:
``{ts, session, tool, ok, error, duration_ms, since_prev_ms, step}``. This is
the input of the weekly failure-to-skill distillation
(``python -m antigravity3d.runtime.distill_job``).

Timing: the harness PostToolUse payload carries no timestamps (docs list only
toolCall, stepIdx, error). ``duration_ms`` is exact for calls that went through
the PreToolUse gate (which records a start time); otherwise it is null and
``since_prev_ms`` (time since the previous logged call in this conversation) is
an upper bound.

After a successful *mutating* tool it also stamps
``<state>/hooks/dirty/<conversation>.json`` so the export gate and the Stop
hook know the last QA verdict is stale.

Output: ``{}`` as documented. Fails open: errors are logged as warnings.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402


def log_call(payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    root = c.state_root(payload)
    conv = c.conv_id(payload)
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    names = c.tool_names(tool_call)
    tool = names[-1] if len(names) > 1 else (names[0] if names else "?")  # prefer MCP ToolName
    error = payload.get("error")
    ok = not error

    duration_ms = None
    tid = tool_call.get("id")
    if tid:
        inflight = root / "hooks" / "inflight" / f"{c.safe_key(str(tid))}.json"
        rec = c.read_json(inflight)
        if isinstance(rec, dict) and "t" in rec:
            duration_ms = round((now - float(rec["t"])) * 1000, 1)
        try:
            inflight.unlink()
        except OSError:
            pass

    last_path = root / "hooks" / "last_call" / f"{c.safe_key(conv)}.json"
    last = c.read_json(last_path)
    since_prev_ms = round((now - float(last["t"])) * 1000, 1) if isinstance(last, dict) and "t" in last else None
    c.write_json(last_path, {"t": now})

    rec_out = {"ts": round(now, 3), "session": conv, "tool": tool, "ok": ok,
               "error": (str(error)[:500] if error else None), "duration_ms": duration_ms,
               "since_prev_ms": since_prev_ms, "step": payload.get("stepIdx")}
    tele = root / "telemetry" / "tool_calls.jsonl"
    tele.parent.mkdir(parents=True, exist_ok=True)
    with tele.open("a", encoding="utf-8") as fh:  # one short write per line: atomic enough for O_APPEND
        fh.write(json.dumps(rec_out) + "\n")

    mutating = c.is_tool(names, c.MUTATING_TOOLS)
    if ok and mutating and conv:
        c.write_json(root / "hooks" / "dirty" / f"{c.safe_key(conv)}.json",
                     {"last_mutation_ts": now, "tool": mutating})
    return {}


def main() -> None:
    payload = c.read_payload()
    try:
        out = log_call(payload)
    except Exception as e:  # noqa: BLE001 - telemetry must never block the agent
        c.warn(payload, f"telemetry failed: {e!r}")
        out = {}
    c.emit(out)


if __name__ == "__main__":
    main()
