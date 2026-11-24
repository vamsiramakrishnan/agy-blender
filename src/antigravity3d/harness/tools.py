"""Model-facing harness tools.

Docstrings are prompt text (sent every turn): keep them terse. Every tool returns a short string
(one to three lines); images are written to the session artifacts dir and referenced by URI. One exception: the renders of
the design (``render_preview``) are a ``ToolReply``, a string that also names its PNGs, so that a runtime that can put
an image in front of the agent (the MCP server: image content in the tool result) does.
Expected failures (bad IR, unknown ids, refused stage edits) come back as one ``ERROR ...`` line;
anything unexpected propagates to ``TracebackCompressorHook``.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
from google.antigravity.tools.tool_context import ToolContext
from google.antigravity.tools.tool_runner import ToolWithSchema
from PIL import Image

from antigravity3d import ir, ir_schema, ir_shortcuts
from antigravity3d.adapters.blender_5_3.linter import UnsafeScriptError, assert_safe
from antigravity3d.adapters.engines.validators import (
    MIN_SILHOUETTE_IOU,
    blender_frame_png,
    silhouette_iou,
)
from antigravity3d.decisions.models import DecisionBudgetError
from antigravity3d.decisions.questions import MARKED_DEFECT_PHRASES, VIEW_WORDS
from antigravity3d.decisions.compose import (
    Action,
    brief_reasons,
    decide_action,
    engine_parity_reasons,
    rank,
    tier15_score,
)
from antigravity3d.genmedia.pbr_deriver import delight, roughness_breakup
from antigravity3d.harness import keeper
from antigravity3d.harness.session import Preview, Session, resolve, set_toolset
from antigravity3d.harness.traceback_compressor import compress_exception
from antigravity3d.genmedia.material_prompt import texture_prompt
from antigravity3d.ir import IRPatch, MaterialIR, SetTransform, UpsertMaterial
from antigravity3d.kernel import provenance
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import (
    ExportBundle,
    Tier1Report,
    describe_collider_sources,
    run_export_pipeline,
    shipped,
    shipped_masks,
    tier1_qa,
)
from antigravity3d.kernel.raster import contact_sheet
from antigravity3d.kernel.separate import separate_coplanar
from antigravity3d.kernel.validator import check_engine_compliance, engine_texture_px, validate_usd
from antigravity3d.state import (
    EXPORT_MIN_TIER15_SCORE,
    AssetSessionState,
    VisualVerdict,
)
from antigravity3d.art_direction import texture_brief
from antigravity3d.kernel.compile_ir import CREASE_DEG
from antigravity3d.tactics import judging
from antigravity3d.tactics.cached import cached_derive_pbr, image_identity

PREVIEW_PX = 256  # per view; the sheet is 2x2 (render_lookdev's preview tier)
# The local Antigravity harness (google-antigravity 0.1.24) puts an image of an MCP tool result in front of the model
# only up to a size: measured offline on 2026-11-22 against the stand-in model (r5/scratch_native/native_trace.py), a
# 10,218-byte PNG and a 10,220-byte WebP were sent inline (WebP and JPEG keep their own type), a 10,928-byte PNG and an
# 11,652-byte WebP were offloaded to a file, the model told only its path; view_file on an image sends it whole. Each
# view the agent is shown is the smallest of these that fits, all at 512 px (``_agent_image``): a 32-colour palette PNG,
# then lossless WebP of the 32-colour image, then of a 16-colour one (by eye the same as full colour for clay with
# outlines and badges; 8 colours bands the shading of round parts). Measured on crates, hydrants and chairs of 60-90
# parts: the three orthographic views fit (2.5-10 KB), the two perspectives of the most detailed designs do not
# (10.9-13 KB even at 8 colours); lossy WebP and JPEG are larger than these (badge text and outlines). The reply names
# every view's file, so the agent opens with view_file a view that did not fit (written in full colour: view_file sends
# any size).
INLINE_IMAGE_BYTES = 10_000
TEXTURE_PX = 2048
MAX_CANDIDATES = 6
MANIFEST_CAP = 1500
TOOLSET_STAGE = {"BLOCKOUT": "BLOCKOUT", "TWEAK": "BLOCKOUT", "VARIANTS": "BLOCKOUT", "LOOKDEV": "LOOKDEV", "EXPORT": "EXPORT"}
QA_STAGE = {"ROUTE": "blockout", "BLOCKOUT": "blockout", "LOOKDEV": "lookdev", "EXPORT": "final", "DONE": "final"}
MAP_KEYS = ("albedo_uri", "orm_uri", "normal_uri")
# A preview with no image allowance (start_preview(max_images=0)): the tools that generate an image say so in one line and
# what to do instead, rather than failing on the image limit. The package writer makes the surfaces from the geometry
# when it writes the package (materials keep their ids and base colours).
NO_IMAGES = ("NO IMAGES: this preview's image limit is {limit} and {used} used, so no image can be generated. Surfaces are "
             "generated from the geometry when the package is written: keep each material's id and base colour, skip "
             "forge_pbr_material and run_paintover_loop, and continue (render_lookdev, run_visual_qa, set_stage EXPORT).")


# How a session gets from a request to a package in the fewest model calls (said by open_asset_session and the
# server's instructions). Live on 2026-11-22 (r5/live_native2, arm N, crate): a model call costs what the whole
# conversation costs (about 55,000 tokens on average, over 100,000 by the end), and walking BLOCKOUT -> LOOKDEV -> EXPORT
# with a schema read, a render and a review at each step took 25 calls and ran out of a 1.5 M share before EXPORT; the
# stock plain-MCP agent delivered its crate in 19 calls and 1.10 M.
# The size of a call (r5/live_native3): an agent told to build "in as few calls as it takes" sent the whole crate in
# one reply, which was cut off at the 16,384-token output limit (13,103 output + 3,281 thinking, MAX_TOKENS, no tool
# call: nothing reaches a tool of ours), and it went on to script Blender in files instead. Measured on the two earlier
# live runs: 46-133 output tokens per operation (mean about 100) and up to 5,700 thinking tokens per call, so 25
# operations (about 2,500 tokens) leave room for any thinking seen.
# "STILL WORKING" (harness/keeper.py): run 1 of the website task (2026-11-24) had two deliver_package calls cut off by
# the host at 3 minutes; the agent then polled folders for four minutes and fell back to stage tools. The answer that
# replaces the cut-off says in its first words that nothing failed, since a "NOT DELIVERED" sends an agent to repair.
SHORT_PATH = "THE SHORT PATH (each model call costs the whole conversation so far, so put calls that can go together in one turn and do not explore folders): build the design with apply_scene_ir, not by scripting Blender, because only a design in the session's stage can be edited, checked and packaged here (Blender on the command line is for inspecting files). First call: the materials and the main body; then two or three calls of at most 25 operations each, writing anything that occurs more than once once, with mirror or repeat. A reply is cut off at 16,384 output tokens, thinking included, and an operation costs about 100: a call that was cut off is split and sent again. Then render_preview and look at the views; improve once or twice; deliver_package, which reviews, builds LODs and colliders, validates and exports (when the request names a folder for the result, pass it as dest; do not copy files by hand afterwards). Its answer starts with one of four: \"DELIVERED\" (the run is finished: reply to the user with the package path and the findings, and do not inspect it again); \"NOT DELIVERED: a person must decide\" (reply to the user with the reason and stop); \"NOT DELIVERED\" otherwise (fix what it names with apply_scene_ir and call deliver_package again); \"STILL WORKING\" (nothing failed and nothing needs fixing: the work goes on by itself; call deliver_package again at once with the same arguments and it answers with the result, doing no step twice; any tool that needs more than about two and a half minutes answers the same way). Nothing else is required."


# What happens to surfaces, said by the tools at the moment it matters (opening a session without images, entering
# LOOKDEV). Live on 2026-11-22 (r5/live_native, arm N, crate): told only in the stage text, the agent wrote a PIL script
# that painted albedo, normal and ORM maps for its three materials and bound them, 330 s of a 694 s session, and never
# called the image tool whose answer said surfaces are generated.
SURFACES = ("SURFACES: every material without map files gets a surface generated from the geometry when the package is "
            "written (kernel.smart_source): its class from the material's surface words, wear, dirt and markings from those "
            "words and the asset's name, all recorded in the manifest. Painting texture maps by script is neither needed nor "
            "wanted. In LOOKDEV check each material's base colour and surface words (inspect_manifest lists them; "
            "apply_shader_diff changes surface, base_color, metallic, roughness) if you want to; deliver_package renders "
            "the generated surfaces, reviews them and exports.")


# The same fact for a session a build script made (run_build): it has no LOOKDEV stage, inspect_manifest or
# apply_shader_diff to point at; a material is changed in the script.
SURFACES_SCRIPT = ("SURFACES: every material's maps (base colour, normal, occlusion-roughness-metallic) are generated from "
                   "the geometry and the material's surface words when the package is written, and recorded in the manifest. "
                   "Do not paint or generate texture files yourself: to change a surface, change its colour or surface words "
                   "in the script. deliver_package renders the surfaces, reviews them and exports.")


def surfaces_note(s: Session, *, opening: bool = False) -> str:
    """The SURFACES line for this session ('' when generated surfaces are off). ``opening``: only when the session can
    generate no image (said at open; LOOKDEV says it always). A session built by a script gets the script's wording."""
    if not s.state.budget.generated_surfaces:
        return ""
    text = SURFACES_SCRIPT if script_session(s) else SURFACES
    if opening:
        limit = getattr(s.governor, "max_images", None)
        if limit is None or s.governor.images_used < limit:
            return ""
        return f"NO IMAGES in this session (image limit {limit}). " + text
    return text


def _no_images(s: Session) -> str | None:
    """The NO_IMAGES line when the session's ledger has no image left, else None."""
    gov = s.governor
    limit = getattr(gov, "max_images", None)
    if limit is None:
        return None
    used = gov.images_used
    return NO_IMAGES.format(limit=limit, used=used) if used >= limit else None


# The remedy a session without the native Decisions API names (open result, run_visual_qa, export refusal).
PREVIEW_SWITCH = ("ask the user whether they want an unverified preview and for their two limits (a token ceiling and an "
                  "image limit), then call start_preview with them; the scene is kept")

ToolFn = Callable[..., Awaitable[str]]


class ToolReply(str):
    """A tool's text reply, with the images it shows: ``images`` is ((label, png path), ...). It is a ``str``, so every
    caller that reads text reads what it always read; a runtime that can show images attaches the PNGs after the text
    (``runtime/mcp_server.py``)."""

    images: tuple[tuple[str, Path], ...]

    def __new__(cls, text: str, images: tuple[tuple[str, Path], ...] = ()) -> "ToolReply":
        reply = super().__new__(cls, text)
        reply.images = tuple(images)
        return reply


# The tools that change the design or its materials: called while a kept call of the session runs (harness/keeper.py),
# they stop it first, because its work was for the design before the change. READ_TOOLS only read and start at once
# beside a running call; every other tool waits for it within its own time.
EDIT_TOOLS = frozenset({"apply_scene_ir", "undo_last_edit", "apply_shader_diff", "forge_pbr_material", "run_paintover_loop",
                        "propose_variants", "select_variant", "promote_variant"})
# Of the edits, these are one quick transaction on the design as they find it: two of them run one after the other
# (tests/test_harness_tools.py::test_session_lock_serialises_concurrent_edits); a longer edit (an image generated for
# the current surfaces) is stopped by a later one, since its result would be stale.
TRANSACTION_TOOLS = frozenset({"apply_scene_ir", "undo_last_edit", "apply_shader_diff", "propose_variants",
                               "select_variant", "promote_variant"})
READ_TOOLS = frozenset({"inspect_manifest", "query_region", "qa_probe"})
R_KEEPER = "kept_calls"


def design_version(s: Session) -> tuple[Any, ...]:
    """What an answer about the design holds for: its geometry, budget, material files and revision (the key the
    review's verdict and the checked package are held by)."""
    return (s.geom_version, s.state.budget, s.texture_digest(), s.state.revision)


def _keeper(s: Session) -> keeper.Keeper:
    k = s._res(R_KEEPER)
    if k is None:
        k = keeper.Keeper(version=lambda: design_version(s))
        s._set(R_KEEPER, k)
    return k


async def stop_kept_work(s: Session) -> list[str]:
    """Stop the session's kept calls and drop their answers (the design is about to change outside the tools here: a
    script's design applied by run_build, a switch to a preview); the names of the calls that were still running."""
    k = s._res(R_KEEPER)
    return await k.stop() if k is not None else []


def stopped_line(names: list[str]) -> str:
    return (f"STOPPED the {' and '.join(dict.fromkeys(names))} that was still running: it was working on the design as it "
            "was before this change, so its work was removed; call it again when you want it for this design.")


def _guard(fn: ToolFn) -> ToolFn:
    """Expected failures as one ``ERROR`` line, and every call kept by its session (``harness/keeper.py``): it answers
    within ``keeper.ANSWER_S`` and its work goes on, for the next call with the same arguments to collect."""
    sig = inspect.signature(fn)
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        async def run() -> str:
            try:
                return await fn(*args, **kwargs)
            except ir_schema.IRArgumentError as e:  # already one actionable line: where, what, what was expected
                return f"ERROR {str(e)[:400]}"
            except (ValueError, LookupError, RuntimeError) as e:  # bad input / unknown id / refused stage edit
                return f"ERROR {compress_exception(e)}"

        if keeper.inside():
            return await run()
        try:
            bound = sig.bind(*args, **kwargs)
            s = resolve(bound.arguments["ctx"])
        except (TypeError, KeyError, LookupError):
            return await run()  # no session (the tool answers so) or a call the signature refuses: nothing to keep
        bound.apply_defaults()  # an argument left out and the same one given as its default are the same call
        given = {k: v for k, v in bound.arguments.items() if k != "ctx" and v != sig.parameters[k].default}
        shown = {**given, "session_id": s.sid} if keeper.NAMES_SESSION.get() else None
        return await _keeper(s).call(name, given, run, shown=shown, edit=name in EDIT_TOOLS, read=name in READ_TOOLS,
                                     transaction=name in TRANSACTION_TOOLS,
                                     stopped_note=stopped_line)

    return wrapper


def _typed(parameters: Callable[[], dict[str, Any]]) -> Callable[[ToolFn], ToolFn]:
    """Declare the tool with an explicit input schema (the SDK's ``ToolWithSchema``) generated from the IR schema.

    The SDK sends ``input_schema`` as the function declaration instead of deriving one from the signature, and the MCP
    server advertises the same dict (``runtime/mcp_server.py``), so both paths have one definition. Arguments are not
    validated by either runtime against it; the tool parses them strictly (``ir_schema.parse_ops``) and answers one
    ``ERROR invalid IR: ...`` line naming the op, the field and what was expected.
    """
    def wrap(fn: ToolFn) -> ToolFn:
        return ToolWithSchema(fn, parameters())  # type: ignore[return-value]

    return wrap


def _decoded(value: Any, what: str) -> Any:
    """Arguments arrive typed; a JSON *string* in their place (a model or an older client still writing the old
    ``*_json`` form) is accepted deliberately: its meaning is unambiguous and refusing it would only cost a round trip.
    It goes through the same strict parse."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise ir_schema.IRArgumentError(f"invalid IR: {what} is a string that is not JSON ({e.msg} at char {e.pos})") from None


def export_block_reason(state: AssetSessionState, plane: Any = None) -> str | None:
    """None when export is allowed, else one line naming what is missing. ``plane``: the session's judge; in a preview a
    hard policy flag it raised blocks export (content risk is never a finding)."""
    flags = list(getattr(plane, "hard_policy_flags", None) or []) if state.preview and getattr(plane, "preview", False) else []
    if state.export_allowed and not flags:
        return None
    if flags:
        return f"PREVIEW export blocked: the judge raised hard policy flag(s) {', '.join(dict.fromkeys(flags))}; a person must clear them"
    missing = []
    if state.validator_ok is False:
        missing.append("passing USD/target checks (run run_usd_validator)")
    if not state.last_tier1_ok:
        missing.append("a passing Tier-1 QA (last edit failed or none yet)")
    findings = state.preview and state.verdict is not None and state.verdict.delivers_on_review
    if not findings and not EXPORT_MIN_TIER15_SCORE <= state.last_tier15_score <= 4.0:
        missing.append(f"tier15>={EXPORT_MIN_TIER15_SCORE} (have {state.last_tier15_score:.2f}; run run_visual_qa after the last edit)")
    if state.preview:
        if findings and state.verdict.action == "escalate_human":  # type: ignore[union-attr]
            missing.append("a person's decision (the review escalated this design to a person: ask the user)")
        elif not state.verdict or not state.verdict.qualifies_preview(state.revision, state.budget.target_engine):
            missing.append("a lookdev/final visual QA review of the current revision (run_visual_qa in LOOKDEV)" if
                           state.verdict is None or state.verdict.delivers_on_review else
                           "an approving lookdev/final visual QA verdict on the current revision")
        return "PREVIEW export blocked: needs " + " and ".join(missing)
    if not state.verdict or not state.verdict.qualifies(state.revision, state.budget.target_engine):
        missing.append("current lookdev/final approval from Decisions on an EEVEE/Cycles render")
        if state.verdict is not None and state.verdict.backend != "decisions":
            missing.append(f"(the judge is not the native Decisions API: {PREVIEW_SWITCH})")
    return "export blocked: needs " + " and ".join(missing)


# --------------------------------------------------------------------------- shared helpers


def _unverifiable_reply(s: Session) -> str | None:
    """The reply of a tool that would call the judge or the image service in a session that can never be delivered
    (``Session.unverifiable_target``): nothing is asked, and the reply says what to do. None for any other session."""
    if not s.unverifiable_target() or (s.plane is not None and not s.plane.calibrated):
        return None  # (a key without the calibrated judge is refused by the plane itself, in its own words, before any call)
    from antigravity3d.adapters.engines.validators import verifiable_targets

    ok = verifiable_targets()
    return (f"REFUSED nothing was asked of the judge or the image service: a session for {s.state.budget.target_engine} cannot be "
            f"verified on this machine, so it can never be delivered. {PREVIEW_SWITCH}"
            + (f"; or open a session for {' or '.join(ok)}, which can be verified here" if ok else ""))


async def _tier1(s: Session) -> Tier1Report:
    # Copy mesh arrays under the session lock. A cancelled QA worker cannot touch the live USD stage.
    return await asyncio.to_thread(tier1_qa, s.stage.extract_meshes(), s.state.budget)


async def _commit(s: Session, apply: Callable[[], str]) -> tuple[str | None, Tier1Report]:
    """Runs ``apply`` (opens one tx), Tier-1 checks it, rolls back on critical issues.

    Caller holds the session lock. Returns (tx id or None if rolled back, report).
    """
    cached = s._res("bundle")
    restore_uvs = cached is not None and cached[0] == (s.geom_version, s.state.budget)
    tid = None
    try:
        tid = apply()
        report = await _tier1(s)
        if report.ok:
            s.visuals_changed(tier1_ok=True)
            return tid, report
    except BaseException:
        if tid is not None:
            s.stage.rollback(tid, discard=True)
        if restore_uvs:
            s.stage.author_uvs(cached[1].uvs)
        s.sync_tx()
        raise
    s.stage.rollback(tid, discard=True)
    if restore_uvs:
        s.stage.author_uvs(cached[1].uvs)
    s.sync_tx()
    return None, report


# Session resource naming the package an edit session was opened from (runtime/mcp_server.open_existing_asset).
EDIT_OF = "edit_of"


def _separated(s: Session, apply: Callable[[], str], extend: Callable[[list], Callable[[], str]], note: list[str]) -> Callable[[], str]:
    """``apply`` followed, in the same transaction, by the deterministic separation of coplanar faces pass A runs
    (kernel/separate.py): when the edit leaves z_fighting findings, the edit is re-applied together with the face moves
    as one patch, so one edit stays one transaction (``undo_last_edit`` undoes both). The moves are kept only if they
    raise no critical issue and make no part float; what happened is appended to ``note``. Caller holds the lock.

    In a session that edits an asset that already shipped (``open_existing_asset``: resource ``EDIT_OF``), only moves of
    parts this edit changed are made: a flush pair that only moving another part would fix is left and named in
    ``note``, because a part the developer did not ask about must not change (as ``agy3d edit`` does)."""
    def run() -> str:
        prior = s.stage.base_ir() if s._res(EDIT_OF) is not None else None
        tid = apply()
        budget = s.state.budget
        meshes = s.stage.extract_meshes()
        before = tier1_qa(meshes, budget)
        if not any(i.code == "z_fighting" for i in before.issues):
            return tid
        r = separate_coplanar(s.stage.base_ir(), meshes, hidden=shipped_masks(meshes, budget))
        if prior is not None and r.ops:
            from antigravity3d.dags.edit_asset import changed_nodes

            changed = changed_nodes(prior, s.stage.base_ir())
            left = sorted({op.node_id for op in r.ops if op.node_id not in changed})
            r.ops = [op for op in r.ops if op.node_id in changed]
            if left:
                note.append("separate: flush faces left as they are, because fixing them would move parts this edit does not "
                            "change: " + ", ".join(left[:8]))
        if not r.ops:
            note.append(f"separate: {r.summary()}")
            return tid
        s.stage.rollback(tid, discard=True)
        tid = extend(r.ops)()
        after = tier1_qa(s.stage.extract_meshes(), budget)
        floats = lambda t: {(i.code, i.node_id) for i in t.issues if i.code in ("floating_part", "floating_group")}  # noqa: E731
        if sum(i.severity == "critical" for i in after.issues) > sum(i.severity == "critical" for i in before.issues) \
                or floats(after) - floats(before):
            s.stage.rollback(tid, discard=True)
            note.append("separate: not applied (a critical issue or a floating part)")
            return apply()
        note.append(f"separate: {r.summary()}")
        return tid
    return run


def _attached(s: Session, run: Callable[[], str], note: list[str]) -> Callable[[], str]:
    """``run`` (a full scene applied, coplanar faces separated: ``_separated``) followed, in the same transaction, by the
    command path's attach step (``tactics.pyramid.attach_plan``, ``kernel/attach.py``): a part that touches nothing by
    the engine check's rule (one of its own vertices within 2 mm of another part's shipped surface) is moved onto, or a
    millimetre into, its nearest part within reach. Found driving the script API (S/r6/probe/attach_probe.py): a lathe
    stacked exactly flush with ``bottom=(0, 0, collar.top)``, the idiom the reference page teaches, came back
    floating_part, which delivery refuses. The moves are kept only if they raise no Tier-1 critical issue; each is one
    line of ``note``. For run_build's full scenes only (it re-applies the scene with the moves): apply_scene_ir does not
    call it. Caller holds the lock."""
    def go() -> str:
        from antigravity3d.tactics.pyramid import attach_plan

        tid = run()
        budget = s.state.budget
        try:
            plan = attach_plan(s.stage.extract_meshes(), s.stage.base_ir(), budget)
        except (ValueError, KeyError) as e:
            note.append(f"attach: not possible ({e})")
            return tid
        if plan is None or not plan[1]:
            return tid
        r, ops, before = plan
        current = s.stage.base_ir()
        s.stage.rollback(tid, discard=True)
        tid = s.stage.apply_ir(ir.apply_patch(current, IRPatch(ops=ops)))
        crit = lambda t: sum(i.severity == "critical" for i in t.issues)  # noqa: E731
        if crit(tier1_qa(s.stage.extract_meshes(), budget)) > crit(before):
            s.stage.rollback(tid, discard=True)
            note.append("attach: not applied (it would raise a Tier-1 critical issue)")
            return s.stage.apply_ir(current)
        note.extend([_joined(a) for a in r.attached])
        return tid
    return go


def _joined(line: str) -> str:
    """One attach move in the author's words, short (the line stays in the conversation): "gallery moved 1.0 mm onto
    collar (+0.0, +0.0, -1.0 mm)" -> "joined: gallery set 1.0 mm into collar (it lay flush)"."""
    m = re.match(r"(\S+) moved ([\d.]+) mm onto (\S+)", line)
    return f"joined: {m.group(1)} set {m.group(2)} mm into {m.group(3)} (it lay flush)" if m else f"joined: {line}"


def _stats(r: Tier1Report) -> str:
    return f"tris={r.stats['tris']} draws={r.stats['draw_calls']}"


def _warn(r: Tier1Report) -> str:
    """The detached parts, each on its own segment with its gap, then the other warnings (cut at 160 characters).
    Live on 2026-11-22 (r5/live_native7) the kernel found the crate's hasp staple 4 mm from everything from the fourth
    edit on, but its token came after the interpenetration ones and was cut from the reply: the agent first heard of it
    from the Blender check, at the end of a 77 s delivery."""
    detached = "".join(f" | {line}" for line in _detached(r))
    line = Tier1Report(r.ok, [i for i in r.issues if i.code not in DETACHED], r.stats).one_line(160)
    return detached + (f" |{line.split('|', 1)[1]}" if "|" in line else "")


# The kernel's rule for a part that is not joined to the rest of the asset (kernel/fast_ops tier1_qa, the rule pass A's
# repair uses): a part, or a group of parts touching only each other, more than TOUCH_TOL (2 mm) from every other part
# and not resting on the ground. The Blender check of the package (upstream qa_check, same 2 mm tolerance) refuses it.
DETACHED = ("floating_part", "floating_group")


def _detached(r: Tier1Report) -> list[str]:
    """One sentence per detached part or group, with its id and gap, worded as what it will do to the delivery."""
    return [f"detached: {i.node_id} ({i.detail}): deliver_package will refuse delivery until it touches the asset"
            for i in r.issues if i.code in DETACHED]


def _material(s: Session, mid: str) -> MaterialIR | None:
    return next((m for m in s.stage.base_ir().materials if m.id == mid), None)


async def _render(s: Session, wireframe: bool = True) -> Preview:
    async with s.registry.locked(s.sid):
        meshes = s.stage.extract_meshes()
        version = s.geom_version
    if not meshes:
        raise ValueError("nothing to render: the scene has no meshes")
    sheet = await asyncio.to_thread(contact_sheet, meshes, size=PREVIEW_PX, wireframe_overlay=wireframe)
    path = s.artifacts_dir / f"preview_{uuid.uuid4().hex}.png"
    try:
        sheet.save(path)
        uri = await s.forge.upload(path) if s.forge is not None else path.as_uri()
        async with s.registry.locked(s.sid):
            return s.add_preview(path, uri, version)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def display_colours(s: Session, meshes: list[Any]) -> dict[str, tuple[float, float, float]]:
    """node id -> its material's base colour as the author wrote it (sRGB 0-255; the scene stores linear, converted back
    with ``ir_schema.written_colour``). Parts without a material are left out (clay)."""
    by_id = {m.id: m for m in s.stage.base_ir().materials}
    out = {}
    for m in meshes:
        mat = by_id.get(m.material_id or "")
        if mat is not None:
            out[m.node_id] = tuple(min(255.0, max(0.0, 255.0 * c)) for c in ir_schema.written_colour(mat.base_color))
    return out


async def _render_views(s: Session, *, colour: bool = False) -> Preview:
    """The design's separate views (``tactics.judging``: five 512 px renders shaded as shipped with their feature edges),
    plain for the judge and badged for the agent, uploaded and recorded as a preview of kind "views". ``colour``: each
    part shaded in its material's base colour instead of clay (``run_build``'s views); render_preview stays clay."""
    async with s.registry.locked(s.sid):
        meshes = s.stage.extract_meshes()
        version = s.geom_version
        colours = display_colours(s, meshes) if colour else None
    if not meshes:
        raise ValueError("nothing to render: the scene has no meshes")
    art = s.art
    stem = f"views_{uuid.uuid4().hex[:12]}"
    written: list[Path] = []

    async def upload(data: bytes) -> str:
        if s.forge is not None:
            return await s.forge.upload(data)
        path = s.artifacts_dir / f"{stem}_{len(written):02d}.png"
        path.write_bytes(data)
        written.append(path)
        return path.as_uri()

    try:
        crease = art.crease_deg() if art is not None else CREASE_DEG
        design = await (judging.upload_design(meshes, upload, crease_deg=crease) if colours is None else
                        judging.upload_design(meshes, upload, crease_deg=crease, colours=colours))
        paths = []
        for name, png in design.renders.marked:  # the badged views on disk, as the agent is shown them (see INLINE_IMAGE_BYTES)
            data, ext = await asyncio.to_thread(_agent_image, png)
            path = s.artifacts_dir / f"{stem}_{name}.{ext}"
            path.write_bytes(data)
            paths.append(path)
            written.append(path)
        async with s.registry.locked(s.sid):
            return s.add_preview(paths[0], design.badged[0][1], version, kind="views", design=design, view_paths=tuple(paths))
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def _quantized(rgb: Image.Image, colours: int) -> Image.Image:
    """``rgb`` mapped, without dithering, to a palette of ``colours``: the colours the badges, their leader lines and the
    view label are drawn in, kept exactly, and the rest chosen for the clay. Found live on 2026-11-22: a palette chosen
    from the image alone merged the few yellow leader-line pixels into grey, and the model described the leaders as white."""
    from antigravity3d.kernel import som
    from antigravity3d.kernel.raster import BG

    reserved = [tuple(int(x) for x in BG), som.BADGE_FILL, som.BADGE_TEXT, som.BADGE_OUTLINE, som.LEADER, (0, 0, 0),
                (255, 255, 255), (70, 74, 84)]
    reserved = list(dict.fromkeys(reserved))
    rest = rgb.quantize(colors=colours - len(reserved), method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    flat = [c for rgb_ in reserved for c in rgb_] + (rest.getpalette() or [])[:3 * (colours - len(reserved))]
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat + [0] * (768 - len(flat)))
    return rgb.quantize(palette=pal, dither=Image.Dither.NONE)


def _agent_image(png: bytes) -> tuple[bytes, str]:
    """(bytes, extension) of one view as the agent is shown it: the first of 32-colour palette PNG, lossless WebP of 32
    colours, lossless WebP of 16 colours that fits ``INLINE_IMAGE_BYTES``, else the full-colour PNG (see there). 512 px
    always."""
    import io

    rgb = Image.open(io.BytesIO(png)).convert("RGB")
    out: list[tuple[bytes, str]] = []
    for colours, fmt in ((32, "PNG"), (32, "WEBP"), (16, "WEBP")):
        im = _quantized(rgb, colours)
        buf = io.BytesIO()
        if fmt == "PNG":
            im.save(buf, "PNG", optimize=True)
        else:
            im.convert("RGB").save(buf, "WEBP", lossless=True, method=6)
        out.append((buf.getvalue(), fmt.lower()))
        if len(out[-1][0]) <= INLINE_IMAGE_BYTES:
            return out[-1]
    return png, "png"  # none fits: the agent opens it with view_file, which sends any size, so it gets every colour


def _views_reply(p: Preview, head: str, *, colour: bool = False) -> ToolReply:
    """The reply that shows the agent its design: one line per image (its file; whether the client shows it inline),
    the badge legend, the images attached. ``colour``: the views were shaded in the materials' base colours."""
    names = [name for name, _ in p.design.badged]
    shading = ("each part in its material's base colour, shaded as the package ships with part outlines drawn" if colour
               else "shaded as the package ships with part outlines drawn")
    lines = [f"{head} #{p.index}: {len(names)} renders of the current design, one per view, {judging.JUDGE_VIEW_PX} px, "
             f"{shading}; each part carries a numbered badge [NN], the same "
             "number in every view. LOOK at every view before your next edit: compare it with the request, find what is "
             "missing, plain or wrong, and name parts by their badge."]
    if colour:  # run_build's reply, which stays in the conversation for every later call: see _compact_views
        return ToolReply("\n".join(lines + _compact_views(p, names)), tuple(zip(names, p.view_paths)))
    for name, path in zip(names, p.view_paths):
        big = path.stat().st_size > INLINE_IMAGE_BYTES
        lines.append(f"  {name} ({VIEW_WORDS.get(name, name)}): {path}" + (
            "  <- too large to show inline: open it with view_file" if big else "  (attached)"))
    lines += ["Legend (badge, node id, views it is seen in):", p.design.renders.legend_text]
    return ToolReply("\n".join(lines), tuple(zip(names, p.view_paths)))


def _compact_views(p: Preview, names: list[str]) -> list[str]:
    """run_build's view lines: the attached views on one line, without their paths (about 170 characters a view); a view
    too large to attach on its own line with its path; the legend as badge and id, several to a line, without the views
    each part is seen in (about 2,000 characters at 40 parts, found by review). render_preview keeps its lines."""
    shown = [n for n, path in zip(names, p.view_paths) if path.stat().st_size <= INLINE_IMAGE_BYTES]
    lines = [f"attached, in this order: {', '.join(f'{n} ({VIEW_WORDS.get(n, n)})' for n in shown)}"] if shown else []
    lines += [f"  {n} ({VIEW_WORDS.get(n, n)}): {path}  <- too large to show inline: open it with view_file"
              for n, path in zip(names, p.view_paths) if n not in shown]
    legend = p.design.legend
    seen = {legend[b] for nums in (p.design.renders.visible or {}).values() for b in nums if b in legend}
    entries = [f"[{num:02d}] {nid}" for num, nid in sorted(legend.items())]
    lines.append("Legend (badge, part):")
    row = ""
    for e in entries:
        if row and len(row) + 2 + len(e) > 110:
            lines.append(row)
            row = ""
        row = f"{row}  {e}" if row else e
    if row:
        lines.append(row)
    if unseen := sorted(set(legend.values()) - seen):
        lines.append("not seen in any view (too small or hidden): " + ", ".join(unseen))
    return lines


async def _visual_qa(s: Session, *, read_only: bool = False):
    """The stage's visual QA. At BLOCKOUT (the design's shape) it is the command's own review: the design judged alone on
    its five views as shipped (``tactics.judging``), with the brief's questions; later stages judge the textured render."""
    if s.plane is None:
        raise RuntimeError("no decision plane attached to this session")
    qa_stage = QA_STAGE.get(s.state.active_stage, "blockout")
    prev = s.latest_preview()
    ortho = s.state.ortho_uris[0] if s.state.ortho_uris else None
    if qa_stage == "blockout":
        if prev is None or prev.kind != "views":
            if read_only:
                raise ValueError("no current renders; the parent agent must call render_preview before review")
            prev = await _render_views(s)
        judged = await judging.judge_uploaded(s.plane, prev.design, brief=s.state.brief or None, governor=s.governor,
                                              stage="blockout", ortho_uri=ortho, style_uri=s.state.style_anchor_uri)
        res = judged.result
    else:
        if prev is None or prev.kind == "views":  # the textured render is judged here, not the clay views
            if read_only:
                raise ValueError("no current preview; the parent agent must render the scene before review")
            prev = await _render(s)
        res = await s.plane.evaluate_visual_qa(
            prev.uri, ortho_uri=ortho, style_anchor_uri=s.state.style_anchor_uri, stage=qa_stage, governor=s.governor,  # type: ignore[arg-type]
            **({"brief": s.state.brief} if s.state.brief else {}),
            **({"placed_markings": list(prev.placed)} if getattr(prev, "placed", ()) else {}))
    return prev, res, tier15_score(res, s.thresholds), decide_action(res, s.thresholds, qa_stage)  # type: ignore[arg-type]


async def _findings(s: Session, prev: Preview, res: Any) -> list[str]:
    """What the review found, as the agent can act on it: each question answered at or above the ambiguity floor, with
    the parts it is on (``locate_defects`` on the badged views, when the plane has it: badge, node id, the views it is
    seen in); then the brief's requirements and the scores. [] when nothing was found and there are no scores."""
    th = s.thresholds
    flagged = {k: p for k, p in sorted(res.defects.items(), key=lambda kv: -kv[1]) if p >= th.ambiguity_lo}
    where: dict[str, list[str]] = {}
    design = prev.design if prev.kind == "views" else None
    locatable = [d for d in flagged if d in MARKED_DEFECT_PHRASES]
    if design is not None and locatable and hasattr(s.plane, "locate_defects"):
        num = {nid: n for n, nid in design.legend.items()}
        seen: dict[int, list[str]] = {}
        for view, nums in (design.renders.visible or {}).items():
            for n in nums:
                seen.setdefault(n, []).append(view)
        try:
            found = await s.plane.locate_defects(design.badged, design.legend, locatable, governor=s.governor)
        except Exception as e:  # noqa: BLE001 - locating is best effort; the finding stands without it
            log_line = f"(parts not located: {type(e).__name__})"
            found = {d: [] for d in locatable}
            where = {d: [log_line] for d in locatable}
        for d, parts in found.items():
            if parts:
                where[d] = [f"[{num[nid]:02d}] {nid} ({', '.join(seen.get(num[nid], [])) or 'not seen'}) p={p:.2f}"
                            for nid, p in parts[:6]]
    lines = [f"finding {d.removeprefix('defect_')} p={p:.2f}" + (": " + "; ".join(where[d]) if where.get(d) else "")
             for d, p in flagged.items()]
    lines += [f"brief: {r}" for r in brief_reasons(res.defects, res.scores, th)]
    if res.scores:
        lines.append("scores (0-4): " + ", ".join(f"{k.removeprefix('score_')} {v:.2f}" for k, v in sorted(res.scores.items())))
    return lines


def _local(uri: str | None) -> Path | None:
    if not uri:
        return None
    p = Path(uri[7:] if uri.startswith("file://") else uri)
    return p if p.is_file() else None


async def _auto_fix(s: Session, action: Action) -> str:
    """Deterministic fixers runnable from here. Caller holds the session lock."""
    if action.fixer == "reground":
        meshes = s.stage.extract_meshes()
        if not meshes:
            return "reground: no meshes"
        dz = -float(min(m.points[:, 2].min() for m in meshes))
        if abs(dz) < 1e-4:
            return "reground: already grounded"
        roots = [n for n in s.stage.base_ir().nodes if n.parent is None]
        ops = [SetTransform(node_id=n.id, transform=n.transform.model_copy(
            update={"translate": (n.transform.translate[0], n.transform.translate[1], n.transform.translate[2] + dz)}))
            for n in roots]
        tid, rep = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=ops)))
        return f"reground dz={dz:+.3f} tx={tid}; re-run QA" if tid else f"reground rolled back: {rep.one_line(100)}"
    key, fn = {"roughness_breakup": ("orm_uri", roughness_breakup), "delight": ("albedo_uri", delight)}.get(action.fixer or "", (None, None))
    if key is None:
        return f"{action.fixer}: no deterministic fixer here"
    ops = []
    for m in s.stage.base_ir().materials:
        src = _local(getattr(m, key))
        if src is None:
            continue
        out = src.with_name(f"{src.stem}_{action.fixer}.png")
        await asyncio.to_thread(lambda: fn(Image.open(src)).save(out))  # noqa: B023 - awaited immediately
        ops.append(UpsertMaterial(material=m.model_copy(update={key: str(out)})))
    if not ops:
        return f"{action.fixer}: not applicable (no local texture maps); needs agent repair"
    tid, _ = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=ops)))
    return f"{action.fixer} applied to {len(ops)} material(s) tx={tid}; re-run QA"


def _edit(s: Session, ops: Any, handoff: bool = False) -> tuple[Callable[[], str], int, Callable[[list], Callable[[], str]]]:
    """``ops`` (a list of op objects) -> (apply callable, op count, extend), ``extend(more_ops)`` being the callable
    that applies the edit and ``more_ops`` after it as one transaction. The legacy string form may also carry a patch
    object ``{"ops": [...]}`` or a full scene ``{"nodes": [...], "materials": [...]}`` (replaces the scene)."""
    payload = _decoded(ops, "ops")
    if isinstance(payload, dict) and "nodes" in payload:
        # handoff: a script's design (run_build), which may carry each part's run (ir_schema.HANDOFF_ONLY)
        scene = ir_schema.parse_tool_scene(s.stage.asset_prim.GetName(), payload["nodes"], payload.get("materials"), handoff=handoff)
        return ((lambda: s.stage.apply_ir(scene)), len(scene.nodes) + len(scene.materials),
                lambda more: (lambda: s.stage.apply_ir(ir.apply_patch(scene, IRPatch(ops=more)))))
    if isinstance(payload, dict) and set(payload) == {"ops"}:
        payload = payload["ops"]
    patch = ir_schema.parse_ops(payload)
    return ((lambda: s.stage.apply_patch(patch)), len(patch.ops),
            lambda more: (lambda: s.stage.apply_patch(IRPatch(ops=[*patch.ops, *more]))))


def _copies(ops: Any) -> str:
    """The ids a mirror / repeat in ``ops`` made, for the reply: the agent edits a copy by its id afterwards."""
    payload = _decoded(ops, "ops")
    payload = payload.get("ops", payload.get("nodes", [])) if isinstance(payload, dict) else payload
    nodes = [o.get("node", o) for o in payload if isinstance(o, dict)] if isinstance(payload, list) else []
    if not any(isinstance(n, dict) and ir_shortcuts.has_shortcuts(n) for n in nodes):
        return ""
    try:
        made = [c["id"] for _, c in ir_shortcuts.expand([n for n in nodes if isinstance(n, dict) and "id" in n])[1]]
    except (ValueError, KeyError, TypeError):
        return ""  # the edit itself reports what is wrong
    shown = ",".join(made[:12]) + (f",+{len(made) - 12}" if len(made) > 12 else "")
    return f" copies={shown}"


def _material_refs(materials: list[Any]) -> list[str]:
    return [m[k] for m in materials if isinstance(m, dict) for k in MAP_KEYS if isinstance(m.get(k), str)]


def image_refs(name: str, args: dict[str, Any]) -> list[str]:
    """Texture paths/URIs a call would bind (the gate screens them before the call runs). Malformed arguments give
    no refs: the tool itself rejects them."""
    try:
        if name == "apply_shader_diff":
            diff = _decoded(args.get("diff"), "diff") or {}
            return _material_refs([diff]) if isinstance(diff, dict) else []
        if name == "apply_scene_ir":
            payload = _decoded(args.get("ops"), "ops") or []
            if isinstance(payload, dict) and "nodes" in payload:
                return _material_refs(payload.get("materials") or [])
            ops = payload.get("ops", []) if isinstance(payload, dict) else payload
            return _material_refs([o.get("material") for o in ops if isinstance(o, dict) and o.get("op") == "upsert_material"])
        if name == "propose_variants":
            variants = _decoded(args.get("variants"), "variants") or []
            ops = [o for v in _variant_list(variants) for o in (v.get("ops") or []) if isinstance(o, dict)]
            return _material_refs([o.get("material") for o in ops if o.get("op") == "upsert_material"])
    except (ValueError, TypeError, AttributeError):
        return []
    return []


# --------------------------------------------------------------------------- BLOCKOUT


async def apply_design(s: Session, ops: Any, before: Callable[[], Any] | None = None, *, attach: bool = False,
                       handoff: bool = False) -> tuple[str | None, Tier1Report, int, int, list[str]]:
    """The edit path of apply_scene_ir (and of run_build, whose design is a full scene): parse ``ops``, then under the
    session lock apply it as one transaction with the separation of coplanar faces, Tier-1 checked and rolled back on a
    critical issue. ``before``: called under the lock just before the apply (run_build updates the session's budget
    there); returns its undo, called if the edit is rolled back or raises. ``attach`` (run_build's full scenes): detached
    parts are attached in the same transaction (``_attached``). Returns (tx id or None when rolled back, the Tier-1
    report, op count, node count, separation and attach notes)."""
    apply, n, extend = _edit(s, ops, handoff)
    note: list[str] = []
    async with s.registry.locked(s.sid):
        undo = before() if before is not None else None
        try:
            edit = _separated(s, apply, extend, note)
            tid, rep = await _commit(s, _attached(s, edit, note) if attach else edit)
        except BaseException:
            if undo is not None:
                undo()
            raise
        if tid is None and undo is not None:
            undo()
        nodes = len(s.stage.base_ir().nodes)
    return tid, rep, n, nodes, note


@_typed(ir_schema.apply_scene_ir_parameters)
@_guard
async def apply_scene_ir(ops: list[dict[str, Any]] | dict[str, Any] | str, ctx: ToolContext) -> str:
    """Atomically apply a Scene IR patch; rolled back if Tier-1 QA fails."""
    s = resolve(ctx)
    tid, rep, n, nodes, note = await apply_design(s, ops)
    if tid is None:
        return f"ROLLED BACK {rep.one_line(200)}"
    return f"OK tx={tid} ops={n} nodes={nodes}{_copies(ops)} {_stats(rep)}{_warn(rep)}" + "".join(f" | {x}" for x in note)


@_guard
async def inspect_manifest(ctx: ToolContext) -> str:
    """Per-node summary of the composed scene: kind, bbox, tris, material."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        return s.stage.manifest(max_chars=MANIFEST_CAP)


@_guard
async def query_region(bounds_min: list[float], bounds_max: list[float], ctx: ToolContext) -> str:
    """List up to 32 prims intersecting a world-space bounding box, in metres. Each bound is [x, y, z]."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        return s.stage.query_region(bounds_min, bounds_max, limit=32, max_chars=1200)


@_guard
async def undo_last_edit(ctx: ToolContext) -> str:
    """Roll back the most recent edit transaction."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        if not s.stage.tx_stack:
            return "nothing to undo"
        s.revoke_approval()
        dropped = s.stage.rollback()
        s.state.last_tier1_ok = False
        s.visuals_changed()
        rep = await _tier1(s)
        s.state.last_tier1_ok = rep.ok
        s.persist_qa()
    return f"UNDONE tx={dropped[0]} open_tx={len(s.state.tx_stack)} | {rep.one_line(120)}"


@_guard
async def render_preview(ctx: ToolContext) -> str:
    """Show you the design: five views with numbered part badges and a legend. Look after every few edits."""
    return _views_reply(await _render_views(resolve(ctx)), "PREVIEW")


@_guard
async def run_visual_qa(ctx: ToolContext) -> str:
    """Visual QA of the current renders; stores tier15, logs the audit delta, applies safe auto-fixes."""
    s = resolve(ctx)
    if refused := _unverifiable_reply(s):
        return refused
    attempt = uuid.uuid4().hex
    async with s.registry.locked(s.sid):
        s.revoke_approval()
        s._set("visual_qa_attempt", attempt)
        s.state.verdict = None
        s.persist_qa()
        target = s.state.budget.target_engine
    try:
        prev, res, t15, action = await _visual_qa(s)
    except DecisionBudgetError as e:
        if s.plane is not None and not s.plane.calibrated and not s.state.preview:
            raise RuntimeError(f"visual QA refused: the native Decisions API is not available on this key; {PREVIEW_SWITCH}") from None
        raise
    async with s.registry.locked(s.sid):
        if (s.geom_version != prev.geom_version or s._res("visual_qa_attempt") != attempt
                or target != s.state.budget.target_engine or action.stage != QA_STAGE.get(s.state.active_stage)):
            return "STALE: scene changed during QA; re-run run_visual_qa"
        if prev.kind in {"eevee", "cycles"} and prev.texture_hash != s.texture_digest():
            return "STALE: material files changed since the render; render again"
        source = res.result.source if res.result.calibrated else "uncalibrated"
        # A preview may be judged on a clay render, which carries no texture hash: pin the material files as they are
        # now, so export still refuses if they change after this verdict.
        texture_hash = prev.texture_hash or (s.texture_digest() if s.state.preview else "")
        s.set_tier15(t15, VisualVerdict(s.state.revision, target, action.stage, prev.kind,
                                      source, action.kind, texture_hash), prev)
        s._set("last_review", {"revision": s.state.revision, "tier15": t15, "scores": dict(res.scores),
                               "defects": dict(res.defects), "views": [str(p) for p in prev.view_paths] or [str(prev.path)]})
        delta = action.audit_delta()
        s.state.add_audit_delta(delta)
        fix = await _auto_fix(s, action) if action.kind == "auto_fix" else ""
    found = await _findings(s, prev, res)
    return delta + (f"\nfix: {fix}" if fix else "") + "".join(f"\n{x}" for x in found)


@_guard
async def qa_probe(ctx: ToolContext) -> str:
    """Read-only visual QA: verdict line, stores nothing."""
    s = resolve(ctx)
    if refused := _unverifiable_reply(s):
        return refused
    prev, res, _t15, action = await _visual_qa(s, read_only=True)
    if s.geom_version != prev.geom_version:
        return "STALE: scene changed during visual review"
    return "PROBE " + action.audit_delta() + "".join(f"\n{x}" for x in await _findings(s, prev, res))


# --------------------------------------------------------------------------- variants


def _variant_list(variants: Any) -> list[dict[str, Any]]:
    """``[{"name", "ops"}]``; the legacy map ``{"open": [ops], ...}`` is read as the same list."""
    if isinstance(variants, dict):
        return [{"name": k, "ops": v.get("ops") if isinstance(v, dict) else v} for k, v in variants.items()]
    if not isinstance(variants, list) or not variants:
        raise ir_schema.IRArgumentError("invalid IR: variants: expected a non-empty list of {name, ops}")
    return variants


@_typed(ir_schema.propose_variants_parameters)
@_guard
async def propose_variants(set_name: str, variants: list[dict[str, Any]] | dict[str, Any] | str, ctx: ToolContext) -> str:
    """Add parallel variants of one variant set, each a patch on the base scene; Tier-1 checked per variant."""
    s = resolve(ctx)
    patches: dict[str, IRPatch] = {}
    for i, v in enumerate(_variant_list(_decoded(variants, "variants"))):
        name = v.get("name") if isinstance(v, dict) else None
        if not isinstance(name, str) or not name:
            raise ir_schema.IRArgumentError(f"invalid IR: variants[{i}].name: expected a non-empty string")
        try:
            patches[name] = ir_schema.parse_ops(v.get("ops"))
        except ir_schema.IRArgumentError as e:
            raise ir_schema.IRArgumentError(str(e).replace("invalid IR: ", f"invalid IR: variants[{i}]({name}).", 1)) from None
    report = []
    async with s.registry.locked(s.sid):
        first: str | None = None
        try:
            for name, patch in patches.items():
                tid = s.stage.add_variant(set_name, name, patch)
                first = first or tid
                probe = s.stage.select_variant(set_name, name)
                rep = await _tier1(s)
                if not rep.ok:
                    s.stage.rollback(first)
                    s.sync_tx()
                    return f"ROLLED BACK variant {name}: {rep.one_line(120)}"
                s.stage.rollback(probe)
                report.append(f"{name}:ok {rep.stats['tris']}t")
        except BaseException:
            if first is not None:
                s.stage.rollback(first)
            s.sync_tx()
            raise
        if first is not None:
            try:
                s.visuals_changed()
            except BaseException:
                s.stage.rollback(first)
                s.sync_tx()
                raise
    return f"VARIANTS {set_name}: {', '.join(report)}; select_variant to inspect, promote_variant to keep"


async def _variant_op(ctx: ToolContext, op: str, set_name: str, variant: str) -> str:
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        fn = s.stage.select_variant if op == "select" else s.stage.promote_variant
        tid, rep = await _commit(s, lambda: fn(set_name, variant))
        if tid is None:
            return f"ROLLED BACK {set_name}={variant}: {rep.one_line(120)}"
    return f"{op.upper().rstrip('E')}ED {set_name}={variant} tx={tid} | {rep.one_line(120)}"


@_guard
async def select_variant(set_name: str, variant: str, ctx: ToolContext) -> str:
    """Compose a variant for inspection/QA (base edits are refused while selected)."""
    return await _variant_op(ctx, "select", set_name, variant)


@_guard
async def promote_variant(set_name: str, variant: str, ctx: ToolContext) -> str:
    """Make a variant the base scene and drop its variant set."""
    return await _variant_op(ctx, "promote", set_name, variant)


# --------------------------------------------------------------------------- LOOKDEV


@_guard
async def forge_pbr_material(material_id: str, prompt: str, ctx: ToolContext, candidates: int = 1) -> str:
    """Generate a tileable PBR material (albedo + ORM + normal) and bind its maps to material_id. prompt: the
    material's surface words (kept as its surface)."""
    s = resolve(ctx)
    if refused := _unverifiable_reply(s):
        return refused
    if (none_left := _no_images(s)) is not None:
        return none_left
    if s.forge is None:
        raise RuntimeError("no image forge attached to this session")
    if s.plane is None:
        return "BLOCKED: no visual policy judge attached"
    async with s.registry.locked(s.sid):
        version = s.geom_version
        current = _material(s, material_id) or MaterialIR(id=material_id)
    if prompt.strip():  # cleaned and bounded by MaterialIR; empty keeps the material's own surface (or its id words)
        current = current.model_copy(update={"surface": MaterialIR(id=material_id, surface=prompt).surface})
    n = max(1, min(MAX_CANDIDATES, s.forge.governor.plan.pbr_candidates, int(candidates)))
    art = s.art  # the session's art direction: its texture brief, map size and palette (None: as before)
    size = art.texture_px(s.state.budget.target_engine, "prop", TEXTURE_PX) if art is not None else engine_texture_px(s.state.budget.target_engine, TEXTURE_PX)
    brief = texture_brief(art)
    material_prompt = texture_prompt(current, brief)
    results = await asyncio.gather(*(s.forge.seamless_albedo(material_prompt, size=size,
                                                             variant_hint=f"option {i + 1}" if n > 1 else "")
                                     for i in range(n)), return_exceptions=True)
    gens = [g for g in results if not isinstance(g, BaseException)]
    if not gens:
        raise RuntimeError(f"no candidate generated: {results[0]}")
    uris = await asyncio.gather(*(s.forge.upload(g.png_bytes) for g in gens))
    scores = await s.plane.rank_candidates("pbr", uris, governor=s.governor)
    # An uncalibrated ranking counts only in a preview session (the plane's preview copy); it never clears policy.
    allowed = [v for v in scores if not v.blocked and not v.error and v.result and (v.result.calibrated or s.plane.preview)]
    if not allowed:
        reasons = sorted({r for v in scores if v.policy for r in v.policy.reasons})
        return f"BLOCKED: all candidates failed the policy gate ({', '.join(reasons)[:100] or 'review required'})"
    best = rank(allowed, "pbr", s.thresholds)[0].index
    maps, _ = await asyncio.to_thread(cached_derive_pbr, s.forge.cache, gens[best].png_bytes,
                                      metallic_hint=current.metallic, convention=current.normal_convention, prepare=True)
    mdir = s.artifacts_dir / "materials"
    mdir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=mdir) as temp:
        files = {key: Path(temp) / (key[:-4] + ".png") for key in MAP_KEYS}
        for key, img in zip(MAP_KEYS, (maps.albedo, maps.orm, maps.normal), strict=True):
            if art is not None:  # the map as it ships (ArtDirection.finish_map)
                img = await asyncio.to_thread(art.finish_map, img, key[:-4], size)
            await asyncio.to_thread(img.save, files[key])
        async with s.registry.locked(s.sid):
            if version != s.geom_version:
                return "STALE: scene changed during material generation"
            k = 1
            while (destination := mdir / f"{material_id}_v{k}").exists():
                k += 1
            Path(temp).rename(destination)
            # who made this albedo, for the package manifest's provenance; kept with the folder the maps went to, so that
            # a package whose material no longer uses these maps does not claim them
            media = s._res("generated_media")
            if media is None:
                media = {}
                s._set("generated_media", media)
            media[material_id] = {"dir": str(destination), **image_identity(
                gens[best], prompt=material_prompt, size=size, variant=f"option {best + 1}" if n > 1 else "",
                offline=bool(getattr(s.forge, "offline", False)))}
            try:
                paths = {key: str(destination / file.name) for key, file in files.items()}
                new = MaterialIR.model_validate({**current.model_dump(), **paths})
                tid, rep = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=[UpsertMaterial(material=new)])))
            except BaseException:
                shutil.rmtree(destination)
                raise
            if tid is None:
                shutil.rmtree(destination)
                return f"ROLLED BACK {rep.one_line(160)}"
    return f"MATERIAL {material_id} v{k}: albedo+orm+normal bound (pick {best + 1}/{len(gens)}, {len(allowed)} passed policy) tx={tid}"


@_typed(ir_schema.apply_shader_diff_parameters)
@_guard
async def apply_shader_diff(material_id: str, diff: dict[str, Any] | str | None = None, ctx: ToolContext = None,  # type: ignore[assignment]
                            assign_to: list[str] | None = None) -> str:
    """Create or change a material (only the given fields) and optionally bind it to nodes."""
    s = resolve(ctx)
    fields = _decoded(diff, "diff") or {}
    if not isinstance(fields, dict):
        raise ir_schema.IRArgumentError("invalid IR: diff: expected an object of material fields")
    fields = dict(fields)
    assign = list(assign_to or []) + list(fields.pop("assign_to", None) or [])  # the legacy form carried it in the diff
    fields.pop("id", None)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", material_id):
        raise ir_schema.IRArgumentError(f"invalid IR: material_id {material_id!r} must match [A-Za-z_][A-Za-z0-9_]* (<= 64 chars)")
    async with s.registry.locked(s.sid):
        current = _material(s, material_id) or MaterialIR(id=material_id)
        new = ir_schema.apply_material_diff(current, fields)
        ops: list[Any] = [UpsertMaterial(material=new)]
        ops += [ir.AssignMaterial(node_id=n, material_id=material_id) for n in assign]
        tid, rep = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=ops)))
    if tid is None:
        return f"ROLLED BACK {rep.one_line(160)}"
    keys = ",".join(sorted(fields)) or "-"
    return f"SHADER {material_id} set={keys} assigned={len(assign)} tx={tid} {_stats(rep)}"


@_guard
async def run_paintover_loop(direction: str, ctx: ToolContext) -> str:
    """Paint-over of the latest render following an art direction; stored as the reference image."""
    s = resolve(ctx)
    if refused := _unverifiable_reply(s):
        return refused
    if (none_left := _no_images(s)) is not None:
        return none_left
    if s.forge is None:
        raise RuntimeError("no image forge attached to this session")
    if s.plane is None:
        return "BLOCKED: no visual policy judge attached"
    prev = s.latest_preview()
    if prev is None or prev.kind == "views":  # the paint-over starts from one clay render, not the badged views
        prev = await _render(s, wireframe=False)
    gen = await s.forge.paint_over(await asyncio.to_thread(prev.path.read_bytes), direction)
    uri = gen.uri or await s.forge.upload(gen.png_bytes)
    verdict = await s.plane.policy_gate(uri, governor=s.governor)
    if not verdict.allowed:
        return f"BLOCKED paint-over by policy gate ({', '.join(verdict.reasons)[:120]})"
    async with s.registry.locked(s.sid):
        if prev.geom_version != s.geom_version:
            return "STALE: scene changed during paint-over"
        path = s.artifacts_dir / f"paintover_{prev.index:03d}_{uuid.uuid4().hex}.png"
        await asyncio.to_thread(path.write_bytes, gen.png_bytes)
        s.state.paintover_uri = uri
    return f"PAINTOVER of preview #{prev.index} stored uri={uri}"


@_guard
async def render_lookdev(ctx: ToolContext, engine: str = "eevee") -> str:
    """Lookdev render via the Blender tier (eevee|cycles|workbench); falls back to the preview tier."""
    s = resolve(ctx)
    if s.pool is None:
        p = await _render(s, wireframe=False)
        return f"BLENDER TIER UNAVAILABLE (no pool attached); preview tier used instead: PREVIEW #{p.index} uri={p.uri}"
    if engine not in {"workbench", "eevee", "cycles"}:
        raise ValueError(f"unknown lookdev engine {engine!r}")
    stem = f"lookdev_{engine}_{uuid.uuid4().hex}"
    src = s.artifacts_dir / f"{stem}.usdc"
    shown = ""
    placed: tuple[str, ...] = ()
    with tempfile.TemporaryDirectory(dir=s.artifacts_dir, prefix=".lookdev-") as td:
        try:
            async with s.registry.locked(s.sid):
                version = s.geom_version
                texture_hash = s.texture_digest()
                b = await _bundle(s)
                checked = _preflight_package(s)
                if checked is not None and _generates_surfaces(s):
                    # deliver_package's checks wrote and passed this scene's package a moment ago: render that one
                    src, placed = checked["usd"], checked["placed"]
                    shown = " | generated surfaces shown (the package the checks passed)"
                elif _generates_surfaces(s):
                    # What ships: the package writer paints every image-less opaque material (kernel.smart_source) into
                    # the shared atlas; render that package's USD, so the agent and the lookdev review see the surfaces
                    # the package will carry, not the flat colours they replace.
                    from antigravity3d.kernel.package import write_engine_package

                    t0 = time.perf_counter()
                    pkg = await _stage_worker(write_engine_package, s.stage, b, s.state.budget, Path(td) / "pkg",
                                              asset_name=s.stage.asset_prim.GetName(), qa=None, art=s.art)
                    src = Path(td) / "pkg" / str(pkg.data.get("usd") or f"{s.stage.asset_prim.GetName()}.usd")
                    shown = f" | generated surfaces shown (package written for the render in {time.perf_counter() - t0:.0f} s)"
                    from antigravity3d.kernel import smart_source

                    # Live on 2026-11-22 (r5/live_native6): the review of this render, not told that the surfaces carry
                    # stencils painted on purpose, escalated the crate to a person for "IP/text risk 0.95" and the export
                    # was refused. The strings go with the preview and every review of it states them (as tactics/look_round).
                    placed = tuple(smart_source.placed_strings(pkg.data))
                else:
                    s.stage.export(src)
            res = await s.pool.call("render_usd", path=str(src), engine=engine, width=512, height=512, transparent=True)
        finally:
            if src.parent == s.artifacts_dir:
                src.unlink(missing_ok=True)
    img = np.asarray(res["image"], dtype=np.float32)
    path = s.artifacts_dir / f"{stem}.png"
    if s.geom_version != version:
        raise RuntimeError("scene changed during render; render again")
    path.write_bytes(blender_frame_png(img))
    try:
        uri = await s.forge.upload(path) if s.forge is not None else path.as_uri()
        async with s.registry.locked(s.sid):
            if s.geom_version != version:
                raise RuntimeError("scene changed during render; render again")
            kind = "clay" if res["fake"] else engine
            if texture_hash != s.texture_digest():
                raise RuntimeError("material files changed during render; render again")
            p = s.add_preview(path, uri, version, kind=kind, texture_hash=texture_hash, placed=placed)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return f"LOOKDEV {res.get('engine', engine)} render #{p.index} uri={uri}{shown}"


def _generates_surfaces(s: Session) -> bool:
    """Whether the package will paint a surface for some material of this scene (``kernel.smart_source``): generated
    surfaces on, and an opaque material without map files."""
    from antigravity3d.kernel import smart_source

    if not s.state.budget.generated_surfaces:
        return False
    with smart_source.using(True):
        return any(smart_source._paintable(m.material) for m in s.stage.extract_meshes())


# --------------------------------------------------------------------------- EXPORT


async def _bundle(s: Session) -> ExportBundle:
    # Caller owns the session lock. Only detached mesh arrays go to the native worker thread.
    cached = s._res("bundle")
    revision = (s.geom_version, s.state.budget)
    if cached and cached[0] == revision:
        bundle = cached[1]
    else:
        # material edits bump geom_version too (Session.visuals_changed), so the revision covers opacity
        # drained like every native step here: a cancelled tool call must not leave xatlas running after the lock is free
        bundle = await _stage_worker(run_export_pipeline, s.stage.extract_meshes(), s.state.budget,
                                     s.stage.asset_prim.GetName(), opaque_materials=opaque_materials(s.stage.scene_ir()))
        s._set("bundle", (revision, bundle))
    art = s.art
    s.stage.ensure_export_uvs(bundle.uvs, crease_deg=art.crease_deg() if art is not None else CREASE_DEG)
    return bundle


async def _stage_worker(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Drain a native stage/file operation before cancellation can release its lock or files."""
    worker = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        finally:
            raise


@_guard
async def generate_lods_and_colliders(ctx: ToolContext) -> str:
    """Unwrap UVs, build LOD chain and UCX convex colliders for the current scene."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        b = await _bundle(s)
    levels = min((len(v) for v in b.lods.values()), default=0)
    lod_tris = [sum(shipped(v[i]).tri_count for v in b.lods.values()) for i in range(levels)]
    stretch = max((u.stretch_mean for u in b.uvs), default=0.0)
    srcs = describe_collider_sources(b.colliders)
    return (f"LODS {len(b.lods)} nodes x {levels} levels tris={'/'.join(map(str, lod_tris))} | "
            f"colliders {len(b.colliders)} hulls ({srcs}) | uv stretch {stretch:.3f} | {b.timings_ms.get('total', 0):.0f}ms")


@_guard
async def run_usd_validator(ctx: ToolContext) -> str:
    """UsdValidation + target-engine compliance of the composed scene, one line."""
    s = resolve(ctx)
    with tempfile.TemporaryDirectory(dir=s.artifacts_dir) as directory:
        return await _validate_snapshot(s, Path(directory) / "validate.usda")


async def _validate_snapshot(s: Session, path: Path) -> str:
    async with s.registry.locked(s.sid):
        revision = s.state.revision
        budget = s.state.budget
        s.stage.export(path)
        meshes = s.stage.extract_meshes()
    from pxr import Usd

    usd = await _stage_worker(validate_usd, path)
    eng = await _stage_worker(check_engine_compliance, Usd.Stage.Open(str(path)), meshes, budget, working_stage=True)
    async with s.registry.locked(s.sid):
        if revision != s.state.revision or budget != s.state.budget:
            return "STALE: scene changed during USD validation"
        passed = not any(i.severity == "critical" for i in [*usd, *eng])
        if not passed:
            s.revoke_approval()
        s.state.validator_ok = passed
        s.persist_qa()

    def fmt(tag: str, issues: list) -> str:
        crit = [i for i in issues if i.severity == "critical"]
        head = f"{tag} {len(crit)} crit/{len(issues) - len(crit)} warn"
        top = list(dict.fromkeys(f"{i.code}@{i.node_id}" if i.node_id else i.code for i in crit or issues))[:3]
        return head + (f" ({'; '.join(top)})" if top else "")

    return f"{fmt('USD', usd)} | {fmt(s.state.budget.target_engine, eng)}"[:300]


async def _blender_asset_qa(s: Session, pkg: Any, glb: Path) -> str | None:
    """The Blender-side check of the shipped GLB (``upstream_checks.asset_qa``): independent of the target engine and of
    whoever judged the asset, so a verified export and a preview export both run it. Returns the refusal when it fails;
    otherwise records the result in the manifest. Without a real worker it is recorded as skipped, never as passed."""
    from antigravity3d.adapters.blender_5_3 import upstream_checks

    try:
        status, detail, data = await upstream_checks.asset_qa(s.pool, str(glb), s.state.budget.target_engine)
    except Exception as e:  # noqa: BLE001 - a worker that errors is a failed check, as for the engine import
        status, detail, data = "failed", f"{type(e).__name__}: {e}"[:300], {}
    if status == "failed":
        return f"blender_asset_qa: {detail}"
    pkg.data["validation"]["blender_asset_qa"] = {"status": status, "detail": detail, **data}
    return None


async def _web_format(pkg: Any) -> None:
    """A web_three package's maps as WebP in its GLBs when the frames show no difference (adapters/engines/web_textures.py;
    run 1 of the website task shipped a 17 MB GLB of PNG maps). Nothing for another target."""
    from antigravity3d.adapters.engines.web_textures import web_format

    await web_format(pkg.out_dir, pkg.data)


async def _verify_engine(s: Session, pkg: Any, asset: str) -> str | None:
    from antigravity3d.adapters.engines.validators import validator_for, verification_gap

    if gap := verification_gap(s.state.budget.target_engine):  # deliver_package asks this first (Session.waits_for_user): the last guard
        return f"{s.state.budget.target_engine} cannot be verified on this machine ({gap})"
    preview = s._res("graded_preview")
    if not preview or not preview.path.is_file() or not s.plane or not s.forge:
        return "missing graded DCC frame or calibrated parity backend"
    dcc = preview.path.read_bytes()
    if hashlib.sha256(dcc).hexdigest() != preview.image_hash:
        return "graded DCC frame changed on disk"
    validator = validator_for(s.state.budget.target_engine)
    glb = pkg.out_dir / f"{asset}.glb"
    smoke = await validator.validate(glb)
    if not smoke.ok:
        return f"{smoke.engine} import: {' '.join((smoke.skipped_reason or smoke.log_tail).split())[-160:]}"
    if issue := smoke.geometry_issue(pkg.data):
        return issue
    if refusal := await _blender_asset_qa(s, pkg, glb):
        return refusal
    image_path = pkg.out_dir / "engine.png"
    capture = await validator.capture(glb, image_path)
    if not capture.ok:
        return f"{capture.engine} capture: {' '.join((capture.skipped_reason or capture.log_tail).split())[-160:]}"
    engine_image = image_path.read_bytes()
    overlap = silhouette_iou(dcc, engine_image)
    least = getattr(validator, "min_silhouette_iou", MIN_SILHOUETTE_IOU)  # the validator says how strict its target's parity is
    if overlap < least:
        return f"target silhouette IoU {overlap:.4f} below {least}"
    parity = await s.plane.evaluate_engine_parity(preview.uri, await s.forge.upload(engine_image), governor=s.governor)
    reasons = engine_parity_reasons(parity, s.thresholds)
    if parity.result.source != "decisions" or not parity.result.calibrated or reasons:
        return "target/DCC parity failed: " + (", ".join(reasons)[:120] or "uncalibrated source")
    (pkg.out_dir / "dcc.png").write_bytes(dcc)
    for name, data in (("dcc.png", dcc), ("engine.png", engine_image)):
        pkg.data["files"].append(name)
        pkg.data["file_hashes"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    pkg.data["validation"]["engine_import"] = {"engine": smoke.engine, "ok": True, "metrics": smoke.metrics, "bounds_m": smoke.bounds_m,
                                               "engine_version": smoke.engine_version, **smoke.extra_record()}
    pkg.data["validation"]["engine_parity"] = {"ok": True, "silhouette_iou": overlap, "scores": parity.scores}
    return None


@_guard
async def export_engine_package(ctx: ToolContext, out_name: str) -> str:
    """Write the engine package (USD+usdz with UVs, LOD variants, colliders; GLBs; textures; manifest.json). Needs QA approval."""
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.publish import publish_directory

    s = resolve(ctx)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", out_name).strip("_") or "package"
    out = s.export_dir / name
    async with s.registry.locked(s.sid):
        st = s.state
        if why := export_block_reason(st, s.plane):
            return f"REFUSED {why}"
        asset = s.stage.asset_prim.GetName()
        assert st.verdict is not None
        if st.verdict.texture_hash != s.texture_digest():
            s.revoke_approval()
            return "REFUSED material files changed after visual QA; render and evaluate again"
        b = await _bundle(s)
        qa = {"tier1_ok": st.last_tier1_ok, "tier15": round(st.last_tier15_score, 3),
              "action": st.verdict.action, "revision": st.revision, "target": st.budget.target_engine,
              "renderer": st.verdict.renderer, "backend": st.verdict.backend, "audit": st.audit_deltas[-2:]}
        with tempfile.TemporaryDirectory(dir=s.export_dir, prefix=".package-") as directory:
            candidate = Path(directory) / name
            pkg = await _stage_worker(write_engine_package, s.stage, b, st.budget, candidate, asset_name=asset, qa=qa, art=s.art)
            if not pkg.ok:
                crit = [i.code for i in pkg.issues if i.severity == "critical"]
                return f"REFUSED package validation failed: {', '.join(dict.fromkeys(crit))[:140]}"
            await _web_format(pkg)  # the same bytes as deliver_package's checked package: its decision, not measured again
            if st.preview:
                checked, glb_sha = _preflight_package(s), _sha256(candidate / f"{asset}.glb")
                if checked is not None and glb_sha and checked["glb_sha256"] == glb_sha:
                    pkg.data["validation"].update(checked["validation"])  # the same GLB bytes passed them in deliver_package
                elif reason := await _preview_engine_import(s, pkg, asset):
                    return f"REFUSED {reason}"
                pkg.data.update(preview_label(s))
                if findings := _review_findings(s):  # the review of an uncalibrated judge: what it found ships with it
                    pkg.data["review"] = findings[0]
            elif reason := await _verify_engine(s, pkg, asset):
                return f"REFUSED {reason}"
            # Last step before the manifest: the FBX is written into this private directory, after every other step has
            # finished (they are awaited one after another under the session lock; nothing else writes in `candidate`).
            # Preview and verified exports alike. The FBX is an extra output: a failure ships the package without it (the
            # manifest says so, and so does the answer below); it never refuses the package and never sets or clears
            # `verified` or `preview`. Placed after the verified / preview branches above, beside the Blender asset check.
            fbx_note = await _export_fbx(s, pkg)
            _add_script(s, pkg.data, candidate)
            _session_provenance(s, pkg.data)
            (candidate / "manifest.json").write_text(json.dumps(pkg.data, indent=1), encoding="utf-8")
            if "fbx" in pkg.data:
                from antigravity3d.kernel.validator import validate_package

                broken = [i for i in validate_package(candidate) if i.severity == "critical" and (i.code.startswith("pkg:fbx") or i.node_id == pkg.data["fbx"].get("file"))]
                if broken and pkg.data["fbx"].get("status") == "written":  # the FBX does not pass the validator: it does not ship, the package does
                    from antigravity3d.kernel.package import drop_fbx

                    drop_fbx(pkg.data, candidate, "PackageValidation", f"{broken[0].code}: {broken[0].detail}")
                    provenance.finalize(pkg.data["provenance"], pkg.data)  # the dropped FBX leaves the record too
                    fbx_note = f"FBX FAILED package validation (not shipped; the package is intact): {broken[0].code} {broken[0].detail}"[:400]
                    (candidate / "manifest.json").write_text(json.dumps(pkg.data, indent=1), encoding="utf-8")
            if st.verdict.texture_hash != s.texture_digest():
                s.revoke_approval()
                return "REFUSED material files changed during package validation"
            publish_directory(candidate, out)
        s.set_last_export(out)
    head = f"EXPORTED PREVIEW {name} (UNVERIFIED; judged by {pkg.data['judge']['label']})" if st.preview else f"EXPORTED {name}"
    # Delivered and review, said apart (as agy3d prop and edit do): the package passed its deterministic checks; what the
    # judge said is a separate fact, and an uncalibrated judge's approval is not a person's.
    judge = pkg.data["judge"]["label"] if st.preview else "Decisions"
    review = (f"review: {st.verdict.action} by {judge} at tier15 {st.last_tier15_score:.2f} on revision {st.revision}"
              + ("; the judge is uncalibrated: a person must look at it before it ships" if st.preview else ""))
    lines = pkg.data.get("review", {}).get("findings") or []  # one sentence per finding, for the agent and the person
    return (f"{head}: usd+glb, {pkg.n_lod_files} LOD glbs, {len(b.colliders)} UCX hulls, "
            f"{len(pkg.data['textures'])} maps, manifest.json | pkg ok | delivered: yes, package validation passed and no "
            f"engine check refused it (manifest lists what ran) | {review} | {fbx_note}" + (f" | spend {s.governor.summary()}" if st.preview else "")
            + "".join(f"\n{x}" for x in [*lines, *_install_step(out)]))


SCRIPT_FILE = "source/build.py"


def _add_script(s: Session, data: dict[str, Any], package: Path) -> None:
    """A session built by run_build ships its script: ``source/build.py`` in the package, listed and hashed like every
    other file, so a change request edits it instead of modelling the asset again."""
    script = s._res(R_SCRIPT)
    if not script:
        return
    raw = script["text"].encode("utf-8")
    (package / "source").mkdir(exist_ok=True)
    (package / SCRIPT_FILE).write_bytes(raw)
    if SCRIPT_FILE not in data["files"]:
        data["files"].append(SCRIPT_FILE)
    data.setdefault("file_hashes", {})[SCRIPT_FILE] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _session_provenance(s: Session, data: dict[str, Any]) -> None:
    """The manifest's ``provenance`` facts of a native session (``kernel/provenance.py``): the scene was edited by the
    conversation's agent, whose model the studio does not see (a scene that only places the user's files is theirs);
    an albedo is credited to the image model only while the material still uses the maps ``forge_pbr_material`` wrote.
    A session built by run_build: the design was built by its script (kind ``script``: path, sha256), the mesh files
    the script made are its outputs, not files the user supplied, and ``source/build.py`` is that script."""
    prov = data.get("provenance")
    if prov is None:
        return
    scene = s.stage.base_ir()
    script = s._res(R_SCRIPT)
    if isinstance(script, dict) and "sha256" in script:
        changed = script.get("geom_version") != s.geom_version
        provenance.set_design(prov, {"kind": "script", "model": None, "path": script["path"], "sha256": script["sha256"],
                                     "note": "built by this script, written in the conversation by its agent (the studio "
                                             "does not see which model that is)"
                                             + ("; changed after the script ran by a deterministic fix" if changed else "")})
        made = set(script.get("parts") or ())
        for key in [k for k in prov["sources"] if k.startswith("supplied:") and k.removeprefix("supplied:") in made]:
            new = "script_output:" + key.removeprefix("supplied:")
            old = prov["sources"].pop(key)
            prov["sources"][new] = {"kind": "script_output", "made_by": "design", "sha256": old.get("sha256"),
                                    "bytes": old.get("bytes")}
            for entry in prov["files"].values():
                entry["derived_from"] = [new if d == key else d for d in entry.get("derived_from", [])]
        if SCRIPT_FILE in data.get("files", []):
            provenance.add_file(prov, SCRIPT_FILE, ["design"], [])
    elif {n.kind for n in scene.nodes} <= {"mesh_ref", "group"}:
        provenance.set_design(prov, {"kind": "user_supplied", "note": "the scene only places the supplied file(s)"})
    else:
        provenance.set_design(prov, {"kind": "model_authored_in_session", "model": None,
                                     "note": "scene edited through apply_scene_ir by the conversation's agent; the studio does not see which model that is"})
    media = s._res("generated_media") or {}
    for m in scene.materials:
        made = media.get(m.id)
        if made and m.albedo_uri and Path(m.albedo_uri).parent == Path(made["dir"]):
            provenance.set_image(prov, f"albedo:{m.id}", {k: v for k, v in made.items() if k != "dir"})
    provenance.finalize(prov, data)


async def _export_fbx(s: Session, pkg: Any) -> str:
    """One line for the tool's answer. The FBX of an unreal_5 / unity_urp / roblox_r15 package, through the helper the prop
    pipeline uses: skipped with its reason without a real Blender worker (nothing is written or recorded then); written and
    recorded (files, hash, ``fbx``); or failed: no FBX, ``fbx.status == "failed"`` in the manifest, the package intact."""
    from antigravity3d.adapters.blender_5_3 import fbx_package

    status, detail, result = await fbx_package.export_package_fbx(s.pool, pkg.out_dir, pkg.data)
    if status == "failed":
        pkg.data["fbx"] = result
        return f"FBX FAILED (not shipped; the package is intact): {detail}"[:500]
    if status == "skipped":
        return f"fbx skipped: {detail}"[:400]
    fbx_package.record(pkg.data, pkg.out_dir, result)
    return f"fbx: {pkg.data['fbx']['file']} re-imported in Blender, not opened in an engine"


def _review_findings(s: Session) -> tuple[dict[str, Any], list[str]] | None:
    """The review of an uncalibrated judge on what ships, as the command reports it (``antigravity3d.delivery``):
    ``(record, finding lines)`` with the record's "findings" filled; None when the verdict is not such a review."""
    from types import SimpleNamespace

    from antigravity3d import delivery

    st = s.state
    last = s._res("last_review")
    if st.verdict is None or not st.verdict.delivers_on_review or not last or last.get("revision") != st.revision:
        return None
    res = SimpleNamespace(tier15=last["tier15"], scores=last["scores"], defects=last["defects"], failed=[], stopped={},
                          judge_unavailable="", rounds=[SimpleNamespace(action=st.verdict.action)])
    review = delivery.review_record(res, s.thresholds, s.plane, where=[str(p) for p in last.get("views", [])])
    review["findings"] = delivery.finding_lines(review)
    return review, review["findings"]


def preview_label(s: Session) -> dict[str, Any]:
    """What a preview package's manifest says about itself: never verified, who judged it and what that means."""
    from antigravity3d.decisions.engine import UNCALIBRATED_MEANING

    plane = s.plane
    return {"verified": False, "preview": True,
            "judge": {"label": plane.judge_label if plane is not None else "none",
                      "calibrated": bool(plane is not None and plane.calibrated),
                      "meaning": UNCALIBRATED_MEANING if plane is not None and not plane.calibrated
                      else "a preview is never verified, whoever judged it"},
            "spend": {"unit": s.governor.unit, "summary": s.governor.summary()}}


async def _preview_engine_import(s: Session, pkg: Any, asset: str) -> str | None:
    """A preview export runs the target engine's headless import when it can: a failed import refuses the package, an
    unavailable importer is recorded. Engine parity is not checked (it would need the native judge)."""
    from antigravity3d.adapters.engines.validators import validator_for

    glb = pkg.out_dir / f"{asset}.glb"
    if refusal := await _blender_asset_qa(s, pkg, glb):  # a geometry check: it does not depend on who judged the asset
        return refusal
    smoke = await validator_for(s.state.budget.target_engine).validate(glb)
    if smoke.skipped:
        pkg.data["validation"]["engine_import"] = {"engine": smoke.engine, "ok": None,
                                                   "skipped": smoke.skipped_reason or "importer unavailable"}
        return None
    if not smoke.ok:
        return f"{smoke.engine} import: {' '.join(smoke.log_tail.split())[-160:]}"
    if issue := smoke.geometry_issue(pkg.data):
        return issue
    pkg.data["validation"]["engine_import"] = {"engine": smoke.engine, "ok": True, "metrics": smoke.metrics, "bounds_m": smoke.bounds_m,
                                               "engine_version": smoke.engine_version, **smoke.extra_record()}
    pkg.data["validation"]["engine_parity"] = {"ok": None, "skipped": "preview: parity needs the native Decisions API"}
    return None


@_guard
async def run_bpy_script(source: str, ctx: ToolContext) -> str:
    """Escape hatch: run a linted bpy script on the Blender pool. Prefer IR tools."""
    s = resolve(ctx)
    try:
        assert_safe(source)
    except UnsafeScriptError as e:
        return "REJECTED " + "; ".join(str(i) for i in e.issues[:2])[:200]
    if s.pool is None:
        return "UNAVAILABLE: no Blender pool attached to this session"
    res = await s.pool.call("run_script", source=source)
    return f"RAN {json.dumps(res, default=str)[:200]}"


# --------------------------------------------------------------------------- tool sets

# --------------------------------------------------------------------------- the short path


# The session resource a session built by run_build keeps its script in (runtime/mcp_server.StudioHost.run_build):
# {"path": relative to the workspace, "sha256", "text", "digest" (of the hand-off folder applied), "geom_version" (after
# that apply), "parts" (file names of the PLYs the script made), "key"}. Its fix instructions name the script.
R_SCRIPT = "build_script"
# In a design a script builds, parts are joined by setting one into the other (a rod ends inside what it joins), so
# Tier-1's interpenetration warning, raised by any crossing at all, is no finding there. Measured 2026-11-22 on a
# lantern of 10 parts written from the reference page: 7 of the reply's 8 finding lines were such joints (1 to 34 % of
# one part's probe points inside the other). At 0.5 the line still cost a live agent two edit rounds on ordinary joints
# (2026-11-23, S/r6/live_smoke1: "fount and filler_neck ... about 53 %", a neck set into a tank; "fount and strut_boss
# ... about 71 %", a boss set into a wall). From this share up a part is buried, which is a fact worth one line.
BURIED_SHARE = 0.9


def script_warning(issue: Any) -> str | None:
    """One Tier-1 warning as run_build words it for a script's author; None when it is no finding there (a joint)."""
    if issue.code == "interpenetration":
        m = re.match(r"(\S+)/(\S+): ~(\d+)%", issue.detail or "")
        if not m or int(m.group(3)) < BURIED_SHARE * 100:
            return None
        return f"warning: {m.group(1)} lies almost entirely inside {m.group(2)} (about {m.group(3)} %): it will not be seen"
    return f"warning: {issue.code}" + (f" on {issue.node_id}" if issue.node_id else "") + (f": {issue.detail}" if issue.detail else "")


# Set when run_build opens a session: a script session from its opening on, before any design is applied (R_SCRIPT is
# set only by an applied design). Found by review: deliver_package on such a session that held nothing yet said
# "build it with apply_scene_ir".
R_SCRIPT_SESSION = "script_session"


def script_session(s: Session) -> bool:
    """Whether run_build opened this session (or built a design in it)."""
    return bool(s._res(R_SCRIPT_SESSION) or s._res(R_SCRIPT))


# Tier-1's critical codes as a script's author reads them: what the engine check would do, never the check's own names.
_CRITICAL_WORDS = {
    "empty_mesh": "has no triangles", "non_finite": "has points that are not numbers", "bad_indices": "refers to points it has not got",
    "degenerate_tris": "has triangles of zero area", "inconsistent_winding": "has faces that point both ways",
    "inverted_normals": "is inside out (its faces point inward)",
}


def script_critical(issue: Any, stats: dict[str, Any]) -> str:
    """One critical Tier-1 issue in a script author's words."""
    if issue.code == "tri_budget":
        return f"it makes {stats.get('tris', '?'):,} triangles and the budget is {stats.get('max_tris', '?'):,} (max_tris in the Asset line)"
    if issue.code == "draw_call_budget":
        return (f"it needs {stats.get('draw_calls')} draw calls and the engine allows {stats.get('max_draw_calls')}: use "
                "fewer materials")
    what = _CRITICAL_WORDS.get(issue.code, issue.code.replace("_", " "))
    return (f"{issue.node_id} {what}" if issue.node_id else what) + (f" ({issue.detail})" if issue.detail else "")


def script_note(line: str) -> str | None:
    """A separation or attach note as run_build words it for a script's author; None when there is nothing to say
    (no flush faces, hidden contact pairs: nothing for the author to act on)."""
    if not line.startswith("separate: "):
        return line
    out = []
    for seg in line.removeprefix("separate: ").split("; "):
        if m := re.match(r"moved (\d+) face\(s\) of (\d+) part\(s\) ([\d.]+) mm apart", seg):
            out.append(f"set apart: {m.group(1)} flush face(s) of {m.group(2)} part(s) moved {m.group(3)} mm so they do not "
                       "flicker in the engine")
        elif m := re.match(r"lifted (\d+) part\(s\) ([\d.]+) mm off the ground plane", seg):
            out.append(f"lifted {m.group(1)} part(s) {m.group(2)} mm off the ground so their bottoms do not flicker")
        elif m := re.match(r"\d+ left \((.*)\)", seg):
            out.append(f"flush faces left as they are (they may flicker in the engine): {m.group(1)}")
    return "; ".join(out) if out else None


def fix_with(s: Session) -> str:
    """How this session's agent fixes the design: "with apply_scene_ir", or in a session built by run_build "in the
    script, then run_build"."""
    return "in the script, then run_build" if script_session(s) else "with apply_scene_ir"


def _enter(s: Session, stage: str) -> None:
    s.state.active_stage = TOOLSET_STAGE[stage]  # type: ignore[assignment]
    set_toolset(s.registry, s.sid, stage)


@_guard
async def deliver_package(ctx: ToolContext, out_name: str = "", dest: str = "") -> str:
    """From a finished design to a delivered package in one call: first the checks that can refuse it (detached parts,
    LODs, colliders, validation, the package's Blender check and engine import), then the lookdev review of what ships
    unless one exists, the export, and what the review found. It answers within 150 s: "STILL WORKING" means the work
    goes on (nothing to fix): call it again with the same arguments for the result.
    dest: the folder the user named for the result, relative to the workspace; the engine files are placed there."""
    s = resolve(ctx)
    if dest:
        try:
            _dest_folder(s, dest)  # refused before any work, in one sentence
        except ValueError as e:
            return f"NOT DELIVERED: {e}."
    if why := s.waits_for_user():
        # refused before any work: no check, render or change of the design can end in a delivery here
        return (f"{NEEDS_A_PERSON} ({why}). Reply to the user with this and stop: {PREVIEW_SWITCH}; then call "
                "deliver_package again. Changing the design does not get past it.")
    async with s.registry.locked(s.sid):
        nodes = len(s.stage.base_ir().nodes)
        tier1_ok = s.state.last_tier1_ok
        asset = s.stage.asset_prim.GetName()
    if not nodes:
        raise ValueError("nothing to deliver: the scene is empty (build it with apply_scene_ir)" if not script_session(s) else
                         "nothing to deliver: the scene is empty (write the script and run_build it)")
    if not tier1_ok:
        if script_session(s):
            return ("REFUSED the current design has not passed the checks: fix what run_build reported in the script, "
                    "run_build, then deliver_package")
        return "REFUSED Tier-1 has not passed on the current scene: fix what apply_scene_ir reported, then deliver_package"
    # The reply leads with the outcome (r5/live_native5: a first reply that led with the review as the gates word it,
    # "agent_repair ... gates win", and ended with the refusal sent the agent back to repair; after a delivered package
    # it went on checking the package itself until its token share ran out).
    review: list[str] = []
    steps: list[str] = []
    try:
        for _ in range(2):  # a second pass only when the review's own deterministic fix changed the scene
            # Fail fast (r5/live_native7: the detached staple was refused by the Blender check after a 77 s lookdev
            # render and review): everything deterministic that can refuse runs first, and a refusal is returned
            # before anything is rendered or the judge is asked.
            refusal, steps = await _preflight(ctx, s, asset)
            if refusal:
                return "\n".join([f"NOT DELIVERED: {refusal}. Fix what it names {fix_with(s)} and call "
                                  "deliver_package again.", *steps])
            v = s.state.verdict
            if v is not None and v.revision == s.state.revision and v.stage in {"lookdev", "final"}:
                break
            # Not run beside the checks: the render and the review would then be made for designs the checks refuse.
            _enter(s, "LOOKDEV")
            if s.pool is not None:
                keeper.step("deliver_package", 3, DELIVER_STEPS, "the lookdev render of the checked package")
                steps.append(await render_lookdev(ctx, "eevee"))
            keeper.step("deliver_package", 4, DELIVER_STEPS, "the lookdev review (a model call)")
            answer = await run_visual_qa(ctx)
            if answer.startswith("STALE"):  # the review's own fix changed the scene while it was judged: again
                continue
            review = answer.split("\n")
            if "\nfix: " not in answer:
                break
        _enter(s, "EXPORT")
        keeper.step("deliver_package", 5, DELIVER_STEPS, "the export")
        exported = await export_engine_package(ctx, out_name or asset)
    finally:
        _drop_preflight(s)
    head, *found = exported.split("\n")
    found = [x for x in found if not x.startswith(NEXT_INSTALL)]  # export's own next step: said once, last
    findings_rule = s.state.verdict is not None and s.state.verdict.delivers_on_review
    # Under the delivery rule (an uncalibrated preview) the review is a set of findings for the person: its verdict
    # lines (the action the gates would take) are left out, the findings, brief and scores kept. Otherwise the review is
    # the gate and is shown as it is.
    shown = [x for x in review if x.startswith(("finding", "brief:", "scores", "fix:"))] if findings_rule else review
    if findings_rule:  # the export line names the review's action ("review: agent_repair by ..."): a finding, not an order
        head = re.sub(r"review: [a-z_]+ by ", "review by ", head)
    if head.startswith("EXPORTED"):
        where = s.last_export
        placed = _place(s, where, dest) if dest and where else []
        web = _web_lines(where)
        checked = _checked_words(where)
        out = [f"DELIVERED: {head}" + (f" | placed in {dest}/" if dest else ""), *placed, *(found or shown), *steps, *web,
               f"FINISHED: the package{f' at {where}' if where else ''} was {checked} by this tool"
               + (f", and its engine files are in {dest}/: nothing is left to copy" if dest else "")
               + ". Reply to the user with its path and the findings above; do not inspect, re-check or copy it.",
               *_install_step(where)]
    elif _needs_a_person(s):
        # Live on 2026-11-22 (r5/live_native6): refused for a person's decision, the agent went back to LOOKDEV and
        # painted its own maps by script until its token share ran out. This refusal ends the agent's work.
        why = next((x.removeprefix("why: ") for x in review if x.startswith("why: ")), head.removeprefix("REFUSED "))
        out = [f"{NEEDS_A_PERSON} ({why}). Reply to the user with this reason and stop: changing the surfaces or the "
               "design to get past it is not wanted.", *shown, *steps, head]
    else:
        out = [f"NOT DELIVERED: {head.removeprefix('REFUSED ')}. Fix what it names {fix_with(s)} and call "
               "deliver_package again.", *shown, *steps]
    return "\n".join(out)


NEEDS_A_PERSON = "NOT DELIVERED: a person must decide"
R_PREFLIGHT = "preflight_package"
# deliver_package's steps as its answer and session_status count them while it runs (harness/keeper.py): the checks
# with the levels of detail and colliders, the package written for the checks with its Blender check and engine import,
# the lookdev render, the review, the export.
DELIVER_STEPS = 5


def _checked_words(package: Path | None) -> str:
    """What the FINISHED line says was done to a delivered package: "imported" only when the target engine's import passed
    (``validation.engine_import.ok``). An engine with no importer (Roblox), or one that was not available (no Godot binary,
    no browser), is said not to have run."""
    try:
        rec = (json.loads((Path(package) / "manifest.json").read_text()).get("validation") or {}).get("engine_import") or {} if package else {}
    except (OSError, ValueError):
        rec = {}
    if rec.get("ok") is True:
        return "validated, imported and checked"
    return f"validated and checked (the engine import was not run: {rec.get('skipped') or 'no result is recorded'})"


NEXT_INSTALL = "NEXT:"


def _install_step(package: Path | None) -> list[str]:
    """The one line that ends the answer for a package that goes into a three.js or Godot project: the exact
    install_into_project call and what it gives. Run 1 of the website task: the answer named no next step, the agent
    read the tool's description ("writing into their project needs their approval"), copied the GLBs by hand and wrote
    its own GLTFLoader code (no levels of detail, no compression). Nothing for the other targets."""
    try:
        man = json.loads((Path(package) / "manifest.json").read_text()) if package else {}
    except (OSError, ValueError):
        return []
    engine, asset = man.get("target_engine"), str(man.get("asset") or "")
    pascal = "".join(p[:1].upper() + p[1:] for p in asset.split("_") if p) or "Asset"
    call = f'install_into_project(package_dir="{package}", dry_run=false)'
    when = ("; pass project_dir when the project is not the workspace; for a project the user already had, call it with "
            "dry_run=true first and show them the plan. Do not copy the files by hand.")
    if engine == "web_three":
        return [f"{NEXT_INSTALL} to use it in a three.js project, {call} gives a loader module to import (src/agy3d/{asset}/index.js: "
                f"load{pascal}() returns a Group holding a THREE.LOD with every level of detail wired), the files where the dev "
                f"server serves them, and KTX2 / meshopt compression when the project has the packages" + when]
    if engine == "godot_4":
        return [f"{NEXT_INSTALL} to use it in a Godot project, {call} gives a scene agy3d/{asset}/{asset}.tscn with the levels of "
                "detail, collision and materials wired, imported and checked in the project's own Godot" + when]
    return []


def _web_lines(package: Path | None) -> list[str]:
    """``web_three.reply_lines`` of a delivered package's manifest (nothing for other targets or an unreadable manifest)."""
    from antigravity3d.adapters.engines.web_three import reply_lines

    try:
        return reply_lines(json.loads((Path(package) / "manifest.json").read_text())) if package else []
    except (OSError, ValueError):
        return []


async def _preflight(ctx: ToolContext, s: Session, asset: str) -> tuple[str | None, list[str]]:
    """deliver_package's deterministic checks, in order, stopping at the first that refuses: the kernel's detached-part
    rule (Tier-1 itself passed before this), LODs and colliders, USD and engine-compliance validation, then the package
    written as the export writes it (package validation), the Blender check of its GLB and the target engine's import.
    Returns (the refusal in one sentence or None, the step lines). The package that passed is kept for the lookdev
    render and the export of this scene (``_preflight_package``): the render shows those surfaces, and the export, whose
    GLB has the same bytes (the package is a function of scene and budget), records these results instead of running
    the same checks on the same file again."""
    from antigravity3d.kernel.package import write_engine_package

    _drop_preflight(s)
    keeper.step("deliver_package", 1, DELIVER_STEPS, "the checks, levels of detail and colliders")
    async with s.registry.locked(s.sid):
        rep = await _tier1(s)
    if detached := [i for i in rep.issues if i.code in DETACHED]:
        return "; ".join(f"{i.node_id} is detached from the rest of the asset ({i.detail})" for i in detached), []
    steps = [await generate_lods_and_colliders(ctx), await run_usd_validator(ctx)]
    if s.state.validator_ok is False:
        return f"USD validation failed: {steps[-1]}", steps
    t0 = time.perf_counter()
    keeper.step("deliver_package", 2, DELIVER_STEPS, "the package written for the checks, its Blender check and engine import")
    directory = Path(tempfile.mkdtemp(dir=s.artifacts_dir, prefix=".preflight-"))
    # Removed whatever step a stop (an edit, a newer build) interrupts: it is registered as checked only at the end.
    # Every step that writes into it drains its thread before a cancellation goes on (_stage_worker here, and
    # web_textures.web_format), so nothing writes into it once it is removed.
    try:
        async with s.registry.locked(s.sid):
            key = (s.geom_version, s.state.budget, s.texture_digest())
            b = await _bundle(s)
            pkg = await _stage_worker(write_engine_package, s.stage, b, s.state.budget, directory / "pkg", asset_name=asset,
                                      qa=None, art=s.art)
        if not pkg.ok:
            shutil.rmtree(directory, ignore_errors=True)
            crit = [i.code for i in pkg.issues if i.severity == "critical"]
            return f"package validation failed: {', '.join(dict.fromkeys(crit))[:140]}", steps
        await _web_format(pkg)  # before the checks, which then check the GLB that ships
        if reason := await _preview_engine_import(s, pkg, asset):
            shutil.rmtree(directory, ignore_errors=True)
            return reason, steps
        from antigravity3d.kernel import smart_source

        s._set(R_PREFLIGHT, {"key": key, "dir": directory, "glb_sha256": _sha256(pkg.out_dir / f"{asset}.glb"),
                             "usd": pkg.out_dir / str(pkg.data.get("usd") or f"{asset}.usd"),
                             "placed": tuple(smart_source.placed_strings(pkg.data)),
                             "validation": {k: v for k, v in pkg.data["validation"].items()
                                            if k in {"blender_asset_qa", "engine_import", "engine_parity"}}})
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    checks = pkg.data["validation"]
    ran = ", ".join(f"{k} {(v.get('status') or ('ok' if v.get('ok') else 'skipped'))}" for k, v in checks.items()
                    if k in {"blender_asset_qa", "engine_import"})
    steps.append(f"CHECKED before the review: package written and validated, {ran} ({time.perf_counter() - t0:.0f} s)")
    return None, steps


def _preflight_package(s: Session) -> dict[str, Any] | None:
    """The package ``_preflight`` passed, while the scene, budget and material files are still the ones it was written
    from; None otherwise."""
    checked = s._res(R_PREFLIGHT)
    if not checked or checked["key"] != (s.geom_version, s.state.budget, s.texture_digest()):
        return None
    return checked


def _drop_preflight(s: Session) -> None:
    if checked := s._res(R_PREFLIGHT):
        shutil.rmtree(checked["dir"], ignore_errors=True)
        s._set(R_PREFLIGHT, None)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
# What deliver_package(dest=...) places in the folder the user named: the files a game engine imports (every GLB of the
# package: the asset, its levels of detail and its colliders; an FBX where the engine takes one; the textures) and the
# manifest (what each file is, the review's findings, provenance; small, and what install_into_project and a later edit
# read). Left in the session's export folder: the USD and USDZ (the editable source and an AR preview, which a game
# project does not import) and inspector.html (a review page). Live on 2026-11-22 (r5/live_native7) an agent told only
# where the package was spent three calls copying it by hand and left the colliders out.
PLACED_SUFFIXES = (".glb", ".fbx")


def _workspace(s: Session) -> Path:
    from antigravity3d.harness.session import R_QA_WORKSPACE

    return Path(s._res(R_QA_WORKSPACE, s.workdir))


def _dest_folder(s: Session, dest: str) -> Path:
    """The folder ``dest`` names inside the workspace, checked (``safe_paths``): relative, no '..' or hidden segment, no
    symbolic link on the way, under the workspace. Raises ValueError with one sentence."""
    from antigravity3d import safe_paths

    rel = dest.strip().rstrip("/")
    try:
        safe_paths.safe_name(rel, "destination")
        path = safe_paths.inside(_workspace(s), rel, "destination", write=True)
    except safe_paths.UnsafePath as e:
        raise ValueError(f"{e} (a folder inside the workspace, relative to it)") from None
    if path.exists() and not path.is_dir():
        raise ValueError(f"destination '{rel}' is not accepted: it is a file, not a folder")
    return path


def _place(s: Session, package: Path, dest: str) -> list[str]:
    """Copy the package's engine files and manifest into ``dest`` (other files there are left as they are; a file of the
    same name is replaced and listed); the lines that say which files the request named and which are the rest. An asset
    a script built takes its script along (``SCRIPT_FILE``): whoever keeps only the folder they asked for can still
    change the asset."""
    import shutil

    from antigravity3d import safe_paths

    root = _workspace(s)
    rel_dest = dest.strip().rstrip("/")
    _dest_folder(s, rel_dest)
    files = sorted(p.relative_to(package).as_posix() for p in package.rglob("*")
                   if p.is_file() and not p.is_symlink()
                   and (p.suffix.lower() in PLACED_SUFFIXES or p.name == "manifest.json" or p.parent.name == "textures"
                        or p.relative_to(package).as_posix() == SCRIPT_FILE))
    replaced = []
    for rel in files:
        target = safe_paths.inside(root, f"{rel_dest}/{safe_paths.safe_name(rel, 'package file')}", "destination", write=True)
        if target.exists():
            replaced.append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package / rel, target, follow_symlinks=False)
    try:  # the package's files are named after its asset, its folder after the export name
        asset = json.loads((package / "manifest.json").read_text())["asset"]
    except (OSError, ValueError, KeyError, TypeError):
        asset = package.name
    main = [f for f in files if f == f"{asset}.glb"]
    tex = [f for f in files if f.startswith("textures/")]
    rest = [f for f in files if f not in main and f not in tex]
    return [f"placed: {', '.join(main) or '(no main GLB)'} (the main file) and {len(tex)} texture file(s) in textures/; "
            f"the rest of the package with them: {', '.join(rest)}"
            + (f"; replaced files of the same name: {', '.join(replaced)}" if replaced else "")
            + "; the USD, USDZ and inspector stay in the session's package"]


def _needs_a_person(s: Session) -> bool:
    """Whether export was refused for something only a person may clear: the review escalated to a person, or the judge
    raised a hard policy flag."""
    v = s.state.verdict
    flags = getattr(s.plane, "hard_policy_flags", None) if getattr(s.plane, "preview", False) else None
    return bool(flags) or (v is not None and v.revision == s.state.revision and v.action == "escalate_human")


STAGE_TOOLS: dict[str, list[ToolFn]] = {
    "BLOCKOUT": [apply_scene_ir, inspect_manifest, undo_last_edit, render_preview, run_visual_qa],
    "TWEAK": [apply_scene_ir, inspect_manifest, query_region, undo_last_edit],
    "VARIANTS": [propose_variants, select_variant, promote_variant, inspect_manifest, run_visual_qa],
    "LOOKDEV": [forge_pbr_material, apply_shader_diff, run_paintover_loop, render_lookdev, run_visual_qa],
    "EXPORT": [inspect_manifest, generate_lods_and_colliders, run_usd_validator, export_engine_package, run_bpy_script],
}
# The tools the short path (SHORT_PATH) calls. A session served over MCP (a native conversation: one agent, no stage
# agents) lists them from the start, before any session exists, and may call them in every stage but VARIANTS; the
# stage-scoped catalogue keeps hiding the finer-control tools. Live on 2026-11-22 (r5/live_native4, arm N): the agent
# listed the server's tools before opening a session, found none of the tools the instructions named (the catalogue
# was stage-scoped and showed them only after open_asset_session), and built the crate by scripting Blender instead.
# Called before a session exists, each answers "no asset session: call open_asset_session first".
SHORT_PATH_TOOLS: list[ToolFn] = [apply_scene_ir, render_preview, run_visual_qa, deliver_package, inspect_manifest,
                                   undo_last_edit]
SESSION_TOOLS: dict[str, list[ToolFn]] = {stage: ([] if stage == "VARIANTS" else list(SHORT_PATH_TOOLS)) for stage in STAGE_TOOLS}
SUBAGENT_TOOLS: dict[str, list[ToolFn]] = {
    "usd_verifier": [inspect_manifest, run_usd_validator],
    "lookdev_critic": [inspect_manifest, qa_probe],
}
# Subagents a stage may invoke: only where they do something the stage's own tools do not.
#   usd_verifier (UsdValidation + engine compliance): VARIANTS (variant-set composition) and LOOKDEV (UsdShade
#     bindings) have no validator of their own. EXPORT has run_usd_validator directly; BLOCKOUT/TWEAK are gated by
#     Tier-1 on every edit and their USD is validated at EXPORT.
#   lookdev_critic (read-only qa_probe in a separate context, verdict not stored or revoked): LOOKDEV. BLOCKOUT and
#     VARIANTS run run_visual_qa themselves; TWEAK cannot render the preview qa_probe needs; EXPORT cannot repair QA.
STAGE_SUBAGENTS: dict[str, tuple[str, ...]] = {
    "BLOCKOUT": (), "TWEAK": (), "VARIANTS": ("usd_verifier",), "LOOKDEV": ("lookdev_critic", "usd_verifier"), "EXPORT": (),
}
ALL_TOOLS: dict[str, ToolFn] = {f.__name__: f for fs in (*STAGE_TOOLS.values(), *SESSION_TOOLS.values(), *SUBAGENT_TOOLS.values())
                                for f in fs}
READ_ONLY_TOOL_NAMES = frozenset(f.__name__ for fs in SUBAGENT_TOOLS.values() for f in fs)
