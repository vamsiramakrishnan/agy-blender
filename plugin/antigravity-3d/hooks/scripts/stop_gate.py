#!/usr/bin/env python3
"""Stop hook for antigravity-3d (wired in ../../hooks.json).

Refuses a normal finish (``terminationReason == "NO_TOOL_CALL"``) while this
conversation has changed an asset (the PostToolUse hook stamped it dirty) and
the QA gate is unmet, returning ``{"decision": "continue", "reason": <one line>}``
(docs: "continue" blocks the stop; the reason is injected as a system message).

Loop guard: if a Stop hook already kept the agent going this turn
(``continuationActive``), it lets the agent stop. Conversations that never
touched an asset, errors, cancellations and budget limits are never blocked.
Set ``AGY3D_STOP_GATE=off`` to disable. Fails open (lets the agent stop).
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    allow = {"decision": "stop"}
    if os.environ.get("AGY3D_STOP_GATE", "").lower() in {"off", "0", "false"}:
        return allow
    if payload.get("continuationActive") or payload.get("terminationReason", "NO_TOOL_CALL") != "NO_TOOL_CALL":
        return allow
    root, conv = c.state_root(payload), c.conv_id(payload)
    if not conv or c.read_dirty(root, conv) is None:
        return allow  # no asset work in this conversation
    ok, reason = c.export_gate(root, conv)
    if ok:
        return allow
    return {"decision": "continue", "reason": f"antigravity-3d QA gate unmet: {reason}."}


def main() -> None:
    payload = c.read_payload()
    try:
        out = decide(payload)
    except Exception as e:  # noqa: BLE001
        c.warn(payload, f"stop gate failed open: {e!r}")
        out = {"decision": "stop"}
    c.emit(out)


if __name__ == "__main__":
    main()
