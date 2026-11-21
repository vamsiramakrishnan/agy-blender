"""Lifecycle hooks for stage agents.

Every hook takes ``(registry, session_id)``: hook contexts cannot see tool state, so the session
is resolved through the shared :class:`SceneSessionRegistry`.

What the SDK (google-antigravity 0.1.24) actually lets a hook do, verified in
``hooks/hook_runner.py`` and ``connections/local/hook_router.py``:

* ``PreTurnHook`` is a *decide* hook: ALLOW or DENY(reason). It cannot rewrite the prompt.
* ``PreToolCallDecideHook`` can deny or shallow-merge ``modified_args``; it runs for builtin and
  custom tools alike.
* ``PostToolCallHook`` / ``OnCompactionHook`` / session hooks are inspect-only (return ignored).
* ``OnToolErrorHook`` may replace the error text sent to the model.
* ``StopHook`` may return CONTINUE; its reason is injected as a system message.
* No hook can edit or drop steps already in the conversation history (it lives in the Go
  harness); only compaction removes history.
* In the local harness the post-tool hook gets a *fresh* ``OperationContext``, not the one the
  pre-tool hook saw (only ``HookRunner.dispatch_*`` called directly shares it), so pre/post pairing
  is keyed by tool-call id.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.antigravity import types
from google.antigravity.hooks.hooks import (
    HookContext, OnToolErrorHook, PostToolCallHook, PreToolCallDecideHook, PreTurnHook, StopHook,
)
from google.antigravity.types import BuiltinTools, HookResult

from antigravity3d.adapters.blender_5_3.linter import lint_script
from antigravity3d.harness.session import Session, session
from antigravity3d.harness.tools import (
    ALL_TOOLS, READ_ONLY_TOOL_NAMES, STAGE_SUBAGENTS, STAGE_TOOLS, _local, export_block_reason, image_refs,
)
from antigravity3d.harness.traceback_compressor import compress_exception
from antigravity3d.state import SceneSessionRegistry

log = logging.getLogger(__name__)

HEADER_PREFIX = "[STATE "
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".exr", ".hdr", ".ktx2"})
_PATH_KEYS = ("path", "file_path", "TargetFile", "AbsolutePath", "output_path", "directory_path")
_FILE_WRITE_TOOLS = frozenset({BuiltinTools.CREATE_FILE.value, BuiltinTools.EDIT_FILE.value})
_SOFT_FAIL = ("ERROR", "REFUSED", "REJECTED", "ROLLED BACK", "BLOCKED", "UNAVAILABLE")
_IMAGE_SIGS = ("iVBORw0KGgo", "/9j/", "R0lGOD", "UklGR", "data:image/")


def _name(x: types.ToolCall | types.ToolResult) -> str:
    return x.name.value if isinstance(x.name, BuiltinTools) else str(x.name)


class _Shared:
    """Agent() deep-copies its config before restoring hook identity; hooks reach live, un-copyable
    session objects (USD stage, locks), so a copy must be the same instance."""

    def __deepcopy__(self, memo: dict) -> "_Shared":
        return self

    def __copy__(self) -> "_Shared":
        return self


def _deny(msg: str) -> HookResult:
    return HookResult(allow=False, message=msg.replace("\n", " ")[:240])


# --------------------------------------------------------------------------- state header


BINDINGS_CAP = 1500  # characters of the LOOKDEV node listing (the manifest tool's own cap)


def _bindings_line(scene: Any) -> str:
    """``bound: wood=body,lid; iron=strap_0; (none)=handle``: the nodes that can carry a material, by material.

    The LOOKDEV stage assigns materials to nodes by id and has no manifest tool in its five-tool set, so its
    state header carries the ids. Longer listings are cut at BINDINGS_CAP with the number of nodes left out.
    """
    groups: dict[str, list[str]] = {}
    for n in scene.nodes:
        if n.kind != "group":
            groups.setdefault(n.material_id or "(none)", []).append(n.id)
    if not groups:
        return ""
    total = sum(len(v) for v in groups.values())
    out, shown = "", 0
    for mid, ids in groups.items():
        for i, nid in enumerate(ids):
            piece = (f"{'; ' if out else ''}{mid}={nid}" if i == 0 else f",{nid}")
            if len(out) + len(piece) > BINDINGS_CAP:
                return f"\nbound: {out} ... +{total - shown} more node(s)"
            out += piece
            shown += 1
    return f"\nbound: {out}"


def state_header(s: Session) -> str:
    """Compact current-state block: one status line, for LOOKDEV the nodes by material, and the <=8 newest
    audit deltas."""
    st, b = s.state, s.state.budget
    scene = s.stage.base_ir()
    mats = ",".join(m.id for m in scene.materials) or "-"
    head = (f"{HEADER_PREFIX}stage={st.active_stage} tools={s.toolset} engine={b.target_engine} "
            f"budget=tris<={b.max_tris},draws<={b.max_draw_calls},hulls<={b.max_collision_hulls} | "
            f"nodes={len(scene.nodes)} mats={mats} open_tx={len(st.tx_stack)} | "
            f"tier1={'ok' if st.last_tier1_ok else 'no'} tier15={st.last_tier15_score:.2f} "
            f"export={'allowed' if st.export_allowed else 'blocked'}"
            + (f" art={st.art_direction}" if st.art_direction else "")
            + (f" PREVIEW(UNVERIFIED; judge={s.plane.judge_label if s.plane else 'none'}; {s.governor.summary()})" if st.preview else "") + "]")
    if s.toolset == "LOOKDEV":
        head += _bindings_line(scene)
    return head + ("\naudit:\n" + "\n".join(st.audit_deltas) if st.audit_deltas else "")


def with_state_header(prompt: types.Content, s: Session) -> types.Content:
    header = state_header(s)
    if isinstance(prompt, str):
        return f"{header}\n\n{prompt}"
    return [header, *prompt] if isinstance(prompt, Sequence) else [header, prompt]


def _first_text(prompt: types.Content) -> str:
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, Sequence):
        return next((p for p in prompt if isinstance(p, str)), "")
    return ""


class StateHeaderHook(_Shared, PreTurnHook):
    """Keeps current session state in front of the model on every turn.

    Mechanism: the SDK offers no prompt-rewriting hook (PreTurn is allow/deny only, session-start
    is inspect-only), so the header is *prepended by the host* (:func:`with_state_header`, used by
    ``run_stage`` and the bake trigger) and this PreTurn hook *enforces* it: with ``require=True``
    a turn whose prompt does not start with a ``[STATE ...]`` header is denied, so no turn runs
    on stale state. ``QAStopHook`` re-states the gate status in its continuation reason.
    """

    def __init__(self, registry: SceneSessionRegistry, session_id: str, *, require: bool = True) -> None:
        self.registry, self.session_id, self.require = registry, session_id, require

    async def run(self, context: HookContext, data: types.Content) -> HookResult:
        if _first_text(data).lstrip().startswith(HEADER_PREFIX):
            context.set_state("agy3d.header_seen", True)
            return HookResult(allow=True)
        if not self.require:
            return HookResult(allow=True)
        return _deny("turn rejected: prompt lacks the [STATE ...] header; send it via run_stage()/with_state_header()")


# --------------------------------------------------------------------------- gate


class InvariantAndSafetyGate(_Shared, PreToolCallDecideHook):
    """Primary pre-tool gate: stage scoping, export invariant, script linter, image policy, builtin safety.

    Builtins: ``run_command`` is always denied; ``create_file``/``edit_file`` are denied outside the
    session workdir. Custom tools outside the active stage tool-set (plus the read-only subagent
    tools) are denied. ``export_engine_package`` is denied until ``state.export_allowed``.
    ``run_bpy_script`` is denied when the AST linter rejects the source. Material maps passed to
    ``apply_shader_diff``/``apply_scene_ir`` that already exist are policy-gated before commit.
    """

    def __init__(self, registry: SceneSessionRegistry, session_id: str) -> None:
        self.registry, self.session_id = registry, session_id
        self._verdicts: dict[str, Any] = {}
        self.denials: list[tuple[str, str]] = []

    async def run(self, context: HookContext, data: types.ToolCall) -> HookResult:
        res = await self._decide(data)
        if not res.allow:
            self.denials.append((_name(data), res.message))
        return res

    async def _decide(self, call: types.ToolCall) -> HookResult:
        return await self.decide(_name(call), call.args, call.canonical_path)

    async def decide(self, name: str, args: dict[str, Any], canonical_path: str | None = None) -> HookResult:
        """The decision logic, independent of the SDK ``ToolCall`` type.

        Shared by the SDK hook path (``run``) and the MCP server, which has no hook runner and calls
        this directly before dispatching a tool, so both paths enforce exactly the same rules.
        """
        s = session(self.registry, self.session_id)
        if name == BuiltinTools.RUN_COMMAND.value:
            return _deny("run_command is disabled for asset agents; use the scene tools")
        if name in _FILE_WRITE_TOOLS:
            target = canonical_path or next((args[k] for k in _PATH_KEYS if isinstance(args.get(k), str)), "")
            if not target or not _inside(Path(target), s.workdir):
                return _deny(f"{name} outside the session workdir is not allowed ({target or 'no path'})")
            return HookResult(allow=True)
        if name == BuiltinTools.INVOKE_SUBAGENT.value:
            # CapabilitiesConfig.allowed_subagents is not enforced by the harness at call time (the built-in `self`
            # and `research` agents and newly defined ones all start), so the stage's list is enforced here.
            # define_subagent never reaches pre-tool hooks (harness-internal), but whatever it defines can only be
            # started through invoke_subagent, which this rule restricts to the stage's declared subagents.
            allowed = STAGE_SUBAGENTS.get(s.toolset, ())
            names = _subagent_names(args)
            if not names or any(n not in allowed for n in names):
                offer = ", ".join(allowed) or "none"
                return _deny(f"subagent {', '.join(n for n in names if n not in allowed) or '?'} is not available in the "
                             f"{s.toolset} stage (available: {offer})")
            return HookResult(allow=True)
        if name not in ALL_TOOLS:
            return HookResult(allow=True)  # other builtins: scoped by enabled_tools / VisionEvictionHook
        allowed = {f.__name__ for f in STAGE_TOOLS.get(s.toolset, [])} | READ_ONLY_TOOL_NAMES
        if name not in allowed:
            return _deny(f"{name} is not available in the {s.toolset} stage")
        if name == "export_engine_package" and (why := export_block_reason(s.state)):
            return _deny(why)
        if name == "run_bpy_script":
            errs = [i for i in lint_script(str(args.get("source", ""))) if i.severity == "error"]
            if errs:
                return _deny("script rejected by linter: " + "; ".join(str(i) for i in errs[:2]))
        for ref in _image_refs(name, args):
            if (why := await self._policy(s, ref)) is not None:
                return _deny(why)
        return HookResult(allow=True)

    async def _policy(self, s: Session, ref: str) -> str | None:
        if s.plane is None:
            return "no decision plane available to screen the material image"
        path = _local(ref)
        if path is not None:
            data = await asyncio.to_thread(path.read_bytes)
            key = hashlib.sha256(data).hexdigest()
        else:
            data = None
            key = ref
        if key not in self._verdicts:
            if data is not None and s.forge is not None:
                uri = await s.forge.upload(data)
            elif data is None and "://" in ref and not ref.startswith("file://"):
                uri = ref
            else:
                return f"could not screen material image {Path(ref).name}"
            self._verdicts[key] = await s.plane.policy_gate(uri, governor=s.governor)
        v = self._verdicts[key]
        return None if v.allowed else f"policy gate blocked {Path(ref).name}: {', '.join(v.reasons)}"


def _inside(p: Path, root: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _image_refs(name: str, args: dict[str, Any]) -> list[str]:
    """Texture refs a call would bind; parsed from the typed arguments (or a legacy JSON string) by ``tools.image_refs``."""
    return image_refs(name, args)


def _subagent_names(args: dict[str, Any]) -> list[str]:
    entries = args.get("Subagents")
    entries = entries if isinstance(entries, list) else [args]
    return [str(e.get("TypeName") or e.get("type_name") or "") for e in entries if isinstance(e, dict)]


# --------------------------------------------------------------------------- telemetry


class TelemetryHook(_Shared, PreToolCallDecideHook, PostToolCallHook):
    """Per-tool counts, hard errors, soft failures and wall durations; JSONL in the session dir.

    Registered as both a pre-tool decide hook (always allows; stamps t0) and a post-tool hook.
    Pairing uses the shared ``OperationContext`` when the runner provides it and otherwise the
    tool-call id (FIFO per tool name when ids are absent), since the local harness hands the
    post-tool hook a fresh context. Place it after the gate: denied calls are not timed.
    """

    _T0 = "agy3d.telemetry.t0"

    def __init__(self, registry: SceneSessionRegistry, session_id: str, path: str | Path | None = None) -> None:
        self.registry, self.session_id = registry, session_id
        self.path = Path(path) if path else session(registry, session_id).workdir / "telemetry.jsonl"
        self._pending: dict[str, list[float]] = defaultdict(list)
        self.calls: dict[str, int] = defaultdict(int)
        self.errors: dict[str, int] = defaultdict(int)
        self.soft_failures: dict[str, int] = defaultdict(int)
        self.durations_ms: dict[str, list[float]] = defaultdict(list)

    @staticmethod
    def _key(x: types.ToolCall | types.ToolResult) -> str:
        return x.id or f"name:{_name(x)}"

    async def run(self, context: HookContext, data: Any) -> Any:
        if isinstance(data, types.ToolCall):
            t0 = time.perf_counter()
            context.set_state(self._T0, t0)
            self._pending[self._key(data)].append(t0)
            return HookResult(allow=True)
        if isinstance(data, types.ToolResult):
            self._record(context, data)
        return None

    def _record(self, context: HookContext, r: types.ToolResult) -> None:
        name, key = _name(r), self._key(r)
        fifo = self._pending[key].pop(0) if self._pending.get(key) else None
        if not self._pending.get(key):
            self._pending.pop(key, None)
        t0 = context.get_state(self._T0) or fifo
        ms = round((time.perf_counter() - t0) * 1000, 2) if t0 else None
        err = bool(r.error)
        soft = isinstance(r.result, str) and r.result.startswith(_SOFT_FAIL)
        self.calls[name] += 1
        self.errors[name] += err
        self.soft_failures[name] += soft
        if ms is not None:
            self.durations_ms[name].append(ms)
        rec = {"ts": time.time(), "session": self.session_id, "tool": name, "ms": ms, "error": err, "soft_fail": soft,
               "result_chars": len(r.result) if isinstance(r.result, str) else None}
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def summary(self) -> dict[str, Any]:
        def p50(xs: list[float]) -> float | None:
            return sorted(xs)[len(xs) // 2] if xs else None

        by_tool = {n: {"calls": c, "errors": self.errors[n], "soft_failures": self.soft_failures[n],
                       "total_ms": round(sum(self.durations_ms[n]), 2), "p50_ms": p50(self.durations_ms[n])}
                   for n, c in sorted(self.calls.items())}
        return {"calls": sum(self.calls.values()), "errors": sum(self.errors.values()),
                "soft_failures": sum(self.soft_failures.values()), "by_tool": by_tool}


# --------------------------------------------------------------------------- errors


_EXC_LINE = re.compile(r"^\s*(?:[\w.]+\.)?(\w+(?:Error|Exception|Exit|Interrupt|Warning))\s*:\s*(.*)$")


def _unwrap_tool_error(exc: Exception) -> Exception:
    """The local harness hands the hook a ToolExecutionError whose message is the formatted traceback;
    rebuild the root exception (same class *name*, last message) so the compressor's hints apply."""
    if not isinstance(exc, types.ToolExecutionError):
        return exc
    for line in reversed(str(exc).splitlines()):
        if m := _EXC_LINE.match(line):
            return type(m.group(1), (Exception,), {})(m.group(2))
    return exc


class TracebackCompressorHook(_Shared, OnToolErrorHook):
    """Replaces the stack trace sent to the model with one compressed line."""

    async def run(self, context: HookContext, data: Exception) -> str:
        return compress_exception(_unwrap_tool_error(data))


# --------------------------------------------------------------------------- vision


def _looks_like_image(v: Any, depth: int = 0) -> bool:
    if isinstance(v, (bytes, bytearray, memoryview, types.Image)):
        return True
    if isinstance(v, str):
        head = v.lstrip()[:64]
        return any(sig in head for sig in _IMAGE_SIGS) or (len(v) > 4096 and _is_b64(v))
    if depth < 3 and isinstance(v, dict):
        return any(_looks_like_image(x, depth + 1) for x in v.values())
    if depth < 3 and isinstance(v, (list, tuple)):
        return any(_looks_like_image(x, depth + 1) for x in v)
    return False


def _is_b64(v: str) -> bool:
    try:
        base64.b64decode(v[:4096], validate=True)
        return True
    except ValueError:
        return False


class VisionEvictionHook(_Shared, PreTurnHook, PreToolCallDecideHook, PostToolCallHook):
    """Keeps image bytes out of the conversation history.

    Guarantee provided (structural, not after-the-fact):
      * Harness tools never return images, only URI/handle lines; ``PostToolCallHook`` checks every
        tool result and records a violation (``self.violations`` + error log) if one ever looks
        like image data (bytes, ``types.Image``, base64 PNG/JPEG/GIF/WebP, ``data:image``).
      * ``view_file`` on an image path and ``generate_image`` are denied before they run, so no
        builtin can pull pixels into context.
      * A user turn carrying ``types.Image`` parts is uploaded through the session's ImageForge
        (URIs recorded on the session as ``user_image_uris``) and then DENIED: the SDK's PreTurn
        hook cannot strip parts, only refuse the turn. ``sanitize_prompt`` performs the
        upload-and-replace host-side before sending, and ``run_stage`` always uses it.
      * Stage agents set ``ToolOutputTruncationConfig`` as a size backstop.

    Not provided: the SDK exposes no way for a hook to edit or drop steps already in history, so
    an image that entered by a path these checks do not cover (e.g. an MCP server, a subagent
    builtin outside the harness hook path) cannot be evicted retroactively; it only leaves
    context through compaction. The post-tool check detects, it cannot redact (inspect-only).
    """

    def __init__(self, registry: SceneSessionRegistry, session_id: str) -> None:
        self.registry, self.session_id = registry, session_id
        self.violations: list[str] = []

    async def run(self, context: HookContext, data: Any) -> Any:
        if isinstance(data, types.ToolCall):
            name = _name(data)
            if name == BuiltinTools.GENERATE_IMAGE.value:
                return _deny("generate_image is disabled: images never enter context; use forge_pbr_material")
            if name == BuiltinTools.VIEW_FILE.value:
                target = data.canonical_path or next((data.args[k] for k in _PATH_KEYS if isinstance(data.args.get(k), str)), "")
                if Path(target).suffix.lower() in IMAGE_EXTS:
                    return _deny("images are never loaded into context; use render handles and run_visual_qa")
            return HookResult(allow=True)
        if isinstance(data, types.ToolResult):
            if _looks_like_image(data.result):
                self.violations.append(_name(data))
                log.error("tool %s returned image-like data into context", _name(data))
            return None
        _, uris = await sanitize_prompt(data, self._session())
        if uris:
            return _deny(f"image input not admitted into history; uploaded as {', '.join(uris)}; resend as text referencing these URIs")
        return HookResult(allow=True)

    def _session(self) -> Session:
        return session(self.registry, self.session_id)


async def sanitize_prompt(prompt: types.Content, s: Session) -> tuple[types.Content, list[str]]:
    """Uploads image parts and replaces each with a ``[image uploaded: <uri>]`` text line."""
    parts = list(prompt) if isinstance(prompt, Sequence) and not isinstance(prompt, str) else [prompt]
    out: list[Any] = []
    uris: list[str] = []
    for p in parts:
        if isinstance(p, types.Image):
            if s.forge is not None:
                uri = await s.forge.upload(p.data, mime_type=p.mime_type)
            else:
                path = s.artifacts_dir / f"user_image_{len(list(s.artifacts_dir.glob('user_image_*'))) + 1:03d}"
                path.write_bytes(p.data)
                uri = path.as_uri()
            uris.append(uri)
            out.append(f"[image uploaded: {uri}]")
        else:
            out.append(p)
    if uris:
        s._set("user_image_uris", [*(s._res("user_image_uris") or []), *uris])
    single = isinstance(prompt, str) or not isinstance(prompt, Sequence)
    return (out[0] if single else out), uris


# --------------------------------------------------------------------------- stop


def unmet_gate(s: Session) -> str | None:
    """One line naming the active stage's unmet exit gate, or None when it is met."""
    st = s.state
    stage = {"TWEAK": "BLOCKOUT", "VARIANTS": "BLOCKOUT"}.get(s.toolset, s.toolset)
    if stage == "BLOCKOUT":
        if not s.stage.base_ir().nodes:
            return "scene is empty: build the blockout with apply_scene_ir"
        if not st.last_tier1_ok:
            return "Tier-1 QA has not passed on the current scene: fix and re-apply"
    elif stage == "LOOKDEV":
        if not st.export_allowed:
            return f"visual QA not passing: {export_block_reason(st)}"
    elif stage == "EXPORT":
        if s.last_export is None and st.export_allowed:
            return "export requested but not written: call export_engine_package"
    return None


class QAStopHook(_Shared, StopHook):
    """Refuses to let a turn end while the stage gate is unmet, at most ``max_continuations`` times.

    Bounded by ``StopArgs.continuation_count`` (0-based count of continuations already granted
    in this turn), so it cannot loop. Budget/error stops (non-UNSPECIFIED stop_reason or an
    error message) are never overridden. EXPORT with QA still blocking is allowed to stop: the
    EXPORT tool-set cannot repair QA, so continuing would only burn tokens.
    """

    def __init__(self, registry: SceneSessionRegistry, session_id: str, *, max_continuations: int = 2) -> None:
        self.registry, self.session_id, self.max_continuations = registry, session_id, max_continuations
        self.continued = 0

    async def run(self, context: HookContext, data: types.StopArgs) -> types.StopHookResult:
        if data.continuation_count >= self.max_continuations or data.error_message or (
                data.stop_reason != types.StopReason.UNSPECIFIED):
            return types.StopHookResult()
        reason = unmet_gate(session(self.registry, self.session_id))
        if reason is None:
            return types.StopHookResult()
        self.continued += 1
        return types.StopHookResult(
            decision=types.StopDecision.CONTINUE,
            reason=f"Stage gate unmet ({data.continuation_count + 1}/{self.max_continuations}): {reason}")


def stage_hooks(registry: SceneSessionRegistry, session_id: str, *, require_header: bool = True) -> list[Any]:
    """The standard hook stack, in dispatch order (the gate runs before telemetry)."""
    return [
        InvariantAndSafetyGate(registry, session_id),
        VisionEvictionHook(registry, session_id),
        TelemetryHook(registry, session_id),
        TracebackCompressorHook(),
        QAStopHook(registry, session_id),
        StateHeaderHook(registry, session_id, require=require_header),
    ]
