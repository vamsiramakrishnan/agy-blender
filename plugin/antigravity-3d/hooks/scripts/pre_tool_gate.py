#!/usr/bin/env python3
"""PreToolUse gate for antigravity-3d (wired in ../../hooks.json).

- ``export_engine_package``: **fails closed**. Denied unless the recorded QA
  verdict has Tier-1 ok, Tier-1.5 >= 3.4, and no mutating tool ran after it.
  Any error while evaluating the gate is a deny.
- The same two rules apply when the tools arrive over MCP: the step name is the generic ``mcp_tool`` and
  the tool is identified from ``args.ToolName`` (or a namespaced name ``..._<tool>``).
- raw Blender scripts (``run_script`` / ``run_bpy_script`` / ``run_blender_script``):
  linted with ``antigravity3d.adapters.blender_5_3.linter.assert_safe``;
  failing scripts are denied with the first issues. If the linter cannot be
  imported the call is not auto-approved: decision ``ask`` (the user decides),
  with a logged warning -- the Blender daemon re-lints every script anyway.

Output (docs): ``{"decision": "allow"|"deny"|"ask", "reason": "..."}``. The
harness blocks on an unrecognised decision, so every path prints one.
Also records the call's start time so the PostToolUse logger can report an
exact duration for gated calls.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True  # keep the plugin folder clean
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402

LINTER_REL = Path("src/antigravity3d/adapters/blender_5_3/linter.py")


def _load_linter(payload: dict[str, Any]) -> Any | None:
    try:
        from antigravity3d.adapters.blender_5_3 import linter  # type: ignore[import-not-found]

        return linter
    except Exception:  # noqa: BLE001
        pass
    cands = [os.environ.get("AGY3D_LINTER_PATH")]
    cands += [str(Path(w) / LINTER_REL) for w in payload.get("workspacePaths") or [] if isinstance(w, str)]
    cands.append(str(Path(__file__).resolve().parents[4] / LINTER_REL))  # plugin symlinked from the repo
    for p in cands:
        if p and os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("_agy3d_linter", p)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod  # dataclasses needs the module registered
                spec.loader.exec_module(mod)
                return mod
    return None


def _record_start(payload: dict[str, Any], tool_call: dict[str, Any]) -> None:
    tid = tool_call.get("id")
    if not tid:
        return
    try:
        p = c.state_root(payload) / "hooks" / "inflight" / f"{c.safe_key(str(tid))}.json"
        c.write_json(p, {"t": time.time()})
    except Exception as e:  # noqa: BLE001
        c.warn(payload, f"could not record start time: {e}")


def preview_decision(args: dict[str, Any]) -> dict[str, Any]:
    """``ask`` (the strongest decision this platform offers short of deny): the user sees the two limits the model
    passed and approves or refuses the unverified preview. Missing or malformed limits are denied outright."""
    def whole(v: Any) -> int | None:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if str(n) == str(v).strip() or n == v else None

    tokens, images = whole(args.get("token_ceiling")), whole(args.get("max_images"))
    if tokens is None or tokens <= 0 or images is None or images < 0:
        return {"decision": "deny", "reason": "an unverified preview needs the USER's token_ceiling and max_images: ask the "
                                              "user for both numbers, never choose them"}
    return {"decision": "ask", "reason": (f"Open an UNVERIFIED antigravity-3d preview limited to {tokens:,} tokens (no price "
                                          f"applied) and {images} generated image(s)? It is judged by whatever judge this key "
                                          "has (the generative fallback, uncalibrated, when the native Decisions API is "
                                          "unavailable) and its package is labelled verified=false. Approve only if these "
                                          "are your limits.")}


def install_decision(args: dict[str, Any]) -> dict[str, Any]:
    """``allow`` for a dry run (it writes nothing); ``ask`` for an install, naming the project and the directory."""
    if args.get("dry_run", True) in (True, "true", "True", 1, None):
        return {"decision": "allow", "reason": "dry run: reports what would be written, writes nothing"}
    project = args.get("project_dir") or "this workspace"
    dest = args.get("dest") or "agy3d/<asset>"
    force = " It REPLACES files of an earlier install that were edited since (force)." if args.get("force") in (True, "true", "True", 1) else ""
    return {"decision": "ask", "reason": (f"Install the antigravity-3d package {args.get('package_dir', '?')} into {project}, "
                                          f"under {dest}? Nothing outside that directory is written, a file this tool did not "
                                          f"write is never replaced, and agy3d uninstall removes exactly what it wrote.{force}")}


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    names = c.tool_names(tool_call)
    _record_start(payload, tool_call)

    if c.is_tool(names, ("open_asset_session",)) and c.is_studio_mcp_call(tool_call, names):
        if not c.conv_id(payload) or c.tool_args(tool_call).get("conversation_id") != c.conv_id(payload):
            return {"decision": "deny", "reason": "pass this conversation's ID from the session header to open_asset_session"}

    # run_build opens the session on its first call of a conversation (script mode): the conversation ID is asked for
    # as for open_asset_session. Once a session is bound to this conversation (its QA record) a call may leave it out,
    # since only an opening call uses it; one that passes it must pass this conversation's.
    if c.is_tool(names, ("run_build",)) and c.is_studio_mcp_call(tool_call, names):
        given = c.tool_args(tool_call).get("conversation_id")
        bound = bool(c.conv_id(payload)) and c.read_qa(c.state_root(payload), c.conv_id(payload)) is not None
        if not c.conv_id(payload) or (given or not bound) and given != c.conv_id(payload):
            return {"decision": "deny", "reason": "pass this conversation's ID from the session header to run_build"}

    # The limits of an unverified preview are the user's, not the model's: the user sees both numbers and decides.
    preview_call = c.is_tool(names, ("start_preview",)) or (
        c.is_tool(names, ("open_asset_session", "run_build")) and c.tool_args(tool_call).get("preview") in (True, "true", "True", 1))
    if preview_call and c.is_studio_mcp_call(tool_call, names):
        return preview_decision(c.tool_args(tool_call))

    # Writing into the developer's game project is theirs to approve; a dry run writes nothing and is allowed.
    if c.is_tool(names, ("install_into_project",)) and c.is_studio_mcp_call(tool_call, names):
        return install_decision(c.tool_args(tool_call))

    if c.is_tool(names, c.EXPORT_TOOLS):
        try:
            sid = c.tool_args(tool_call).get("session_id", "")
            if c.is_studio_mcp_call(tool_call, names) and not sid:
                return {"decision": "deny", "reason": "pass the session_id from open_asset_session for native export"}
            ok, reason = c.export_gate(c.state_root(payload), c.conv_id(payload), sid)
        except Exception as e:  # noqa: BLE001 - fail closed
            return {"decision": "deny", "reason": f"export gate could not read QA state ({e}); failing closed"}
        return {"decision": "allow" if ok else "deny", "reason": reason}

    if c.is_tool(names, c.SCRIPT_TOOLS):
        args = c.tool_args(tool_call)
        source = next((args[k] for k in c.SCRIPT_ARG_KEYS if isinstance(args.get(k), str)), None)
        if source is None:
            return {"decision": "deny", "reason": "raw script call has no source argument to lint"}
        try:
            linter = _load_linter(payload)
        except Exception as e:  # noqa: BLE001
            linter = None
            c.warn(payload, f"linter import failed: {e}")
        if linter is None:
            c.warn(payload, "antigravity3d linter not importable; asking the user instead of auto-approving")
            return {"decision": "ask", "reason": "antigravity-3d linter unavailable; review this raw bpy script"}
        try:
            linter.assert_safe(source)
        except linter.UnsafeScriptError as e:
            issues = "; ".join(str(i) for i in e.issues[:3])
            more = f" (+{len(e.issues) - 3} more)" if len(e.issues) > 3 else ""
            return {"decision": "deny", "reason": f"raw bpy script rejected by the 5.3 linter: {issues}{more}. "
                                                  "Prefer apply_scene_ir; the escape hatch must lint clean."}
        return {"decision": "allow", "reason": "raw bpy script passed the Blender 5.3 linter"}

    # MCP steps reach this hook under the generic step name ``mcp_tool`` (the matcher includes it). For
    # this plugin's own server the other tools carry no gate rule here, and the server already applies the
    # stage/QA/linter gates before dispatch, so they are allowed; any other MCP server's tools go to the
    # normal permission flow.
    if c.is_studio_mcp_call(tool_call, names):
        return {"decision": "allow", "reason": "antigravity-3d MCP tool; gated server-side"}

    # Anything else is left to the normal permission flow rather than auto-approved.
    return {"decision": "ask", "reason": "not an antigravity-3d gated tool"}


def main() -> None:
    payload = c.read_payload()
    try:
        out = decide(payload)
    except Exception as e:  # noqa: BLE001
        names = c.tool_names(payload.get("toolCall") or {}) if isinstance(payload.get("toolCall"), dict) else []
        c.warn(payload, f"gate crashed: {e!r}")
        if c.is_tool(names, c.EXPORT_TOOLS):
            out = {"decision": "deny", "reason": f"export gate error ({e}); failing closed"}
        else:
            out = {"decision": "ask", "reason": f"antigravity-3d gate error ({e}); please review"}
    c.emit(out)


if __name__ == "__main__":
    main()
