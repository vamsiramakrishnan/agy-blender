"""Shared stdlib-only helpers for the antigravity-3d hook scripts.

Contract (harness docs, "Lifecycle Hooks (hooks.json)"): each hook gets one JSON
object on stdin (camelCase keys: conversationId, workspacePaths, cwd,
hookEventName, toolCall{id,name,args}, error, ...) and must print one JSON
object on stdout. Hooks run synchronously in the agent loop, so everything
here is cheap: no third-party imports, no network, small files only.

State lives under ``$AGY3D_STATE_DIR`` or ``<first workspace>/.agy3d``; the
format matches ``antigravity3d.runtime.qa_state`` (schema agy3d.qa_state/1).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

QA_SCHEMA = "agy3d.qa_state/1"
EXPORT_MIN_TIER15_SCORE = 3.4

EXPORT_TOOLS = ("export_engine_package",)
SCRIPT_TOOLS = ("run_script", "run_bpy_script", "run_blender_script")
QA_TOOLS = ("run_visual_qa", "run_usd_validator")
MUTATING_TOOLS = (
    "apply_scene_ir", "apply_shader_diff", "forge_pbr_material", "run_paintover_loop",
    "generate_lods_and_colliders",
) + SCRIPT_TOOLS
SCRIPT_ARG_KEYS = ("source", "script", "code", "Source", "Script", "Code")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def read_payload() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def state_root(payload: dict[str, Any]) -> Path:
    env = os.environ.get("AGY3D_STATE_DIR")
    if env:
        return Path(env)
    ws = payload.get("workspacePaths") or []
    base = ws[0] if ws and isinstance(ws[0], str) else payload.get("cwd") or os.getcwd()
    return Path(base) / ".agy3d"


def safe_key(key: str) -> str:
    k = _SAFE.sub("_", key or "")[:128]
    return k if k and k not in {".", ".."} else "_"


def conv_id(payload: dict[str, Any]) -> str:
    return str(payload.get("conversationId") or "")


def warn(payload: dict[str, Any], msg: str) -> None:
    """Logged warning (stderr + <state>/hooks/warnings.log); never raises."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{payload.get('hookEventName', '?')}] {msg}"
    try:
        sys.stderr.write(line + "\n")
        p = state_root(payload) / "hooks" / "warnings.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- tool identity


def tool_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    args = tool_call.get("args")
    args = dict(args) if isinstance(args, dict) else {}
    inner = args.get("Arguments") or args.get("arguments")  # MCP-style wrapper
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            inner = None
    if isinstance(inner, dict):
        args = {**args, **inner}
    return args


def tool_names(tool_call: dict[str, Any]) -> list[str]:
    names = [tool_call.get("name")]
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    names += [args.get("ToolName"), args.get("toolName")]
    return [n.lower() for n in names if isinstance(n, str) and n]


def is_tool(names: list[str], wanted: tuple[str, ...]) -> str | None:
    """Returns the matched canonical name. Accepts namespaced forms (``mcp_x_<name>``, ``x/<name>``)."""
    for n in names:
        for w in wanted:
            if n == w or n.endswith(("_" + w, "/" + w, "." + w, ":" + w)):
                return w
    return None


# ---------------------------------------------------------------- QA gate


def read_qa(root: Path, conversation: str) -> dict[str, Any] | None:
    qa_dir = root / "qa"
    for p in ([qa_dir / f"{safe_key(conversation)}.json"] if conversation else []) + [qa_dir / "latest.json"]:
        d = read_json(p)
        if isinstance(d, dict) and d.get("schema") == QA_SCHEMA:
            return d
    return None


def read_dirty(root: Path, conversation: str) -> dict[str, Any] | None:
    d = read_json(root / "hooks" / "dirty" / f"{safe_key(conversation)}.json")
    return d if isinstance(d, dict) else None


def export_gate(root: Path, conversation: str) -> tuple[bool, str]:
    """(ok, one-line reason). Raises on unreadable-but-present state; callers decide fail mode."""
    qa = read_qa(root, conversation)
    if qa is None:
        return False, ("no QA verdict recorded: run run_usd_validator and run_visual_qa "
                       "(usd_verifier subagent) before export_engine_package")
    score = float(qa.get("tier15_score", 0.0))
    if not qa.get("tier1_ok"):
        return False, "Tier-1 QA has not passed: fix the run_usd_validator / Tier-1 findings, then re-run QA"
    if score < EXPORT_MIN_TIER15_SCORE:
        return False, f"Tier-1.5 score {score:.2f} < {EXPORT_MIN_TIER15_SCORE}: iterate lookdev/blockout and re-run QA"
    dirty = read_dirty(root, conversation)
    if dirty and float(dirty.get("last_mutation_ts", 0)) > float(qa.get("ts", 0)):
        return False, (f"scene changed by {dirty.get('tool', 'a mutating tool')} after the last QA run "
                       f"(score {score:.2f}); re-run QA before export")
    return True, f"QA gate passed (Tier-1 ok, Tier-1.5 {score:.2f} >= {EXPORT_MIN_TIER15_SCORE})"
