"""Multi-resolution refinement: coarse silhouette lock (A) -> lookdev (B) -> final verification (C).

Everything is deterministic orchestration around two injected, non-deterministic parts: the ``IRAuthor``
(repairs) and the image forge (textures). Every step is recorded as passed / failed / skipped-with-reason;
nothing that did not run is ever reported as passed.
"""

from __future__ import annotations

import asyncio
import math
import inspect
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from pxr import Usd

from antigravity3d.adapters.blender_5_3 import fbx_package, upstream_checks
from antigravity3d.adapters.engines.validators import MIN_SILHOUETTE_IOU, blender_frame_png, silhouette_iou, validator_for
from antigravity3d.adapters.engines import web_three
from antigravity3d.adapters.engines.web_three import WebThreeValidator
from antigravity3d.decisions.compose import (
    BRIEF_REASON, Action, Thresholds, brief_reasons, decide_action, has_brief_answers, lod_pop_ok, lod_pop_reasons, progress_score,
    tier15_score,
)
from antigravity3d.decisions.models import VisualQAResult
from antigravity3d.decisions.questions import MARKED_DEFECT_PHRASES
from antigravity3d.genmedia.material_prompt import texture_prompt
from antigravity3d.genmedia.pbr_deriver import PBRMaps, delight, edge_wear, roughness_breakup
from antigravity3d.ir import (
    AssignMaterial, IRPatch, MaterialIR, RemoveNode, SceneIR, SetParam, SetTransform, UpsertMaterial, UpsertNode, apply_patch,
)
from antigravity3d.art_direction import texture_brief
from antigravity3d.kernel.compile_ir import CREASE_DEG, crease_normals
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import (
    CAPPED_MARK, ExportBundle, describe_collider_sources, describe_texel_density, run_export_pipeline, shipped, texel_density_report,
    shipped_masks, tier1_qa,
)
from antigravity3d.kernel.lod_diff import PopThresholds, lod_pop_report
from antigravity3d.kernel.raster import CLAY, render_buffers
from antigravity3d.kernel.separate import separate_coplanar
from antigravity3d.kernel.settle import beside_a_part, mesh_bounds, settle
from antigravity3d.tactics.judging import JUDGE_VIEW_PX, JudgedDesign, judge_design
from antigravity3d.kernel.validator import check_engine_compliance, engine_texture_px, validate_usd
from antigravity3d.state import EXPORT_MIN_TIER15_SCORE, EngineBudget
from antigravity3d.tactics.cached import cached_export_pipeline
from antigravity3d.tactics.common import QA_TOKENS_EST, Uploader, png_bytes, uploader_for
from antigravity3d.tactics.distill import FixLog, FixRecord
from antigravity3d.tactics.governor import BudgetExceeded, Governor
from antigravity3d.tactics import reviewed_rules
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.tactics.package import merged_lods
from antigravity3d.tactics.tournament import PBRTournamentResult, TournamentResult, albedo_variant, image_est, pbr_tournament

EPS = 1e-9
LOOKDEV_ROUNDS = 3
QA_EST_USD = 0.002  # conservative cost of one visual-QA (+ locate) round when checking the ceiling
# Side of each badged view an author request carries (repairs and improvement rounds): the judge's own views
# (tactics.judging: five separate renders, front, right, top and two opposite perspectives), with the badges drawn on
# the same renders, so the author looks at exactly what was judged. One image at the default resolution billed 1,120
# input tokens on gemini-4-neon (decisions.fallback.IMAGE_TOKENS_DEFAULT), so five are about 5,600 tokens a request;
# admission counts them exactly (count_tokens, no per-image resolution set).
AUTHOR_VIEW_PX = JUDGE_VIEW_PX
# How much a score must rise for an improvement round to be kept: twice the judge's own spread on one unchanged
# design, since a smaller rise cannot be told from noise. Measured live 2026-11-22 on the fallback judge under the
# viewing condition that ships (tactics.judging: five separate 512 px views, each design in its own request), through
# judge_design itself: 16 designs and variants (crate, hydrant, chair), three judgements each, pooled standard
# deviation 0.096 for score_brief_depiction, 0.097 for score_silhouette, 0.099 for score_shippability (largest single
# cell 0.23). Under the previous condition (one sheet of four 256 px views) the same figure was 0.12 and the band 0.25.
IMPROVE_NOISE_BAND = 0.20
# Pass A gates that are scores, not locatable faults (``_Run.gates``): a design that fails only these goes to improvement
# rounds instead of a repair. The silhouette lock is not lowered by this: it stays a gate, and the rounds aim at it.
SCORE_GATES = frozenset({"silhouette", "brief_depiction", "brief_part_missing"})
# Repairs in a row that may be rolled back before pass A stops: after one, the author is asked again with what its patch
# did and why it was undone (a person told what their fix broke tries again); after a second the same request at the same
# state has failed twice. Each attempt is still one of the pass's ``max_rounds``.
REPAIR_PATIENCE = 2
# Defects the kernel checks itself (fast_ops.tier1_qa codes): the judge's answer on one of them counts only when the
# kernel finds it too. Measured live 2026-11-22 (r5/live/m2_worker/crate, improvement round 1): the judge newly reported
# inverted faces 0.85 and holes 0.65 on a design Tier-1 found neither on, and a rendering of it shows neither: one
# judgement's false alarm undid a good round.
KERNEL_CHECKED = {
    "defect_inverted_faces": frozenset({"inverted_normals", "inconsistent_winding"}),
    "defect_visible_holes": frozenset({"not_watertight"}),
    "defect_zfighting": frozenset({"z_fighting"}),
    "defect_floating": frozenset({"floating_part", "floating_group", "ungrounded"}),
}
# A defect only the judge can see (clipping where it should not be, faceting, a named part missing, scale) that a change
# newly brings is asked about once more before it undoes anything: it stands when both judgements report it.
CONFIRM_JUDGE_ONLY = True
# The judge's spread on one unchanged design (pooled standard deviation of score_brief_depiction, see IMPROVE_NOISE_BAND):
# the validation of improvement rounds compares means of k judgements, whose difference has sd JUDGE_SD * sqrt(2 / k).
JUDGE_SD = 0.10


def validation_band(k: int) -> float:
    """The rise in mean depiction over ``k`` judgements of each design that is not noise: two standard deviations of the
    difference of two such means (k = 1 gives 0.28; k = 3 gives 0.16)."""
    return round(2 * JUDGE_SD * math.sqrt(2 / max(1, k)), 3)


def floating_ids(t1: Any) -> set[str]:
    """Every part Tier-1 finds floating, group members included (a group's detail names them: "a, b touch each other")."""
    out: set[str] = set()
    for i in t1.issues:
        if i.code == "floating_part" and i.node_id:
            out.add(i.node_id)
        elif i.code == "floating_group":
            out |= {n.strip() for n in i.detail.split(" touch each other")[0].split(",") if n.strip()}
    return out


# How far tidy() drops a floating part the patch changed: a clean-up, not a redesign. A part farther above everything
# than the larger of these is left for the author's correction.
SETTLE_MIN_M = 0.02
SETTLE_SHARE = 0.03  # of the asset's largest dimension


def tidy(sm: Any, budget: EngineBudget, touched: set[str]) -> list[str]:
    """The deterministic clean-up a first design gets, applied after an author's patch to what the patch changed and to
    nothing else (each step its own transaction above the patch, so rolling the patch back undoes them): parts Tier-1
    finds floating are settled onto what is below them (kernel/settle.py); curved parts lose segments until the design
    fits the triangle budget (authoring._shrink); flush faces are separated (kernel/separate.py). Each step is kept only
    if it raises no Tier-1 critical issue. Returns what was done, one line each. Measured live 2026-11-22
    (r5/live/m2_worker): rounds were lost to four lid pads a few millimetres above the lid (crate, improvement round 4),
    a design 276 triangles over a 4,000 budget (hydrant) and parts left floating by a repair (chair, twice)."""
    from antigravity3d.kernel.fast_ops import shipped_masks
    from antigravity3d.kernel.separate import separate_coplanar
    from antigravity3d.tactics.authoring import _shrink, count_tris

    notes: list[str] = []

    def guarded(ops: list[Any], what: str) -> bool:
        if not ops:
            return False
        before = _crit(tier1_qa(sm.extract_meshes(), budget))
        tx = sm.apply_patch(IRPatch(ops=ops))
        if _crit(tier1_qa(sm.extract_meshes(), budget)) > before:
            sm.rollback(tx)
            notes.append(f"{what}: not applied (it would raise a Tier-1 critical issue)")
            return False
        return True

    meshes = sm.extract_meshes()
    movable = floating_ids(tier1_qa(meshes, budget)) & touched & {m.node_id for m in meshes}
    if movable:
        pts = np.concatenate([np.asarray(m.points, np.float64) for m in meshes])
        reach = max(SETTLE_MIN_M, SETTLE_SHARE * float((pts.max(0) - pts.min(0)).max()))
        try:
            sr = settle(meshes, movable)
            # Only a small drop is a clean-up (a pad a few millimetres above the lid): a part far above anything was put
            # there on purpose or wrongly, and either way the author decides (the correction says it floats).
            far = {n for n, ns in sr.nodes.items() if abs(ns.dz) > reach}
            if far:
                sr.nodes = {n: ns for n, ns in sr.nodes.items() if n not in far}
            ops = sr.to_ir_ops(sm.base_ir())
        except (ValueError, KeyError) as e:
            ops, far = [], set()
            notes.append(f"settle: not possible ({e})")
        if guarded(ops, "settle"):
            notes.append(f"settled {len(ops)} part(s) the patch left floating within {reach * 1e3:.0f} mm: "
                         f"{', '.join(sorted(movable - far)[:8])}")
    scene = sm.base_ir()
    if count_tris(scene) > budget.max_tris:
        ops = _shrink(scene, budget.max_tris, only=touched)
        if guarded(ops, "shrink"):
            notes.append(f"fewer segments on {len({o.node_id for o in ops})} part(s) the patch changed: "
                         f"{count_tris(sm.base_ir())} of max_tris {budget.max_tris}")
    meshes = sm.extract_meshes()
    try:
        r = separate_coplanar(sm.base_ir(), meshes, hidden=shipped_masks(meshes, budget))
    except (ValueError, KeyError):
        r = None
    if r is not None and r.ops:
        ops = [o for o in r.ops if getattr(o, "node_id", None) in touched]
        left = sorted({o.node_id for o in r.ops if getattr(o, "node_id", None) not in touched})
        if guarded(ops, "separate"):
            notes.append(f"separated flush faces of {len({o.node_id for o in ops})} part(s) the patch changed")
        if left:
            notes.append("flush faces left (fixing them would move parts the patch did not change): " + ", ".join(left[:8]))
    return notes


def tidy_findings(t1_before: Any, t1_after: Any, touched: set[str], budget: EngineBudget) -> str:
    """What is still wrong with a patch after ``tidy``, on the kernel's own evidence, as a message the author can act on
    (its correction), or '' when nothing is."""
    out = []
    new_float = sorted(floating_ids(t1_after) - floating_ids(t1_before))
    if new_float:
        out.append(f"these parts float (touch nothing they could rest on, even after settling): {', '.join(new_float[:10])}")
    over = [i for i in t1_after.issues if i.code == "tri_budget"]
    if over:
        out.append(f"the design is over the triangle budget ({over[0].detail}) even with fewer segments on the parts you "
                   "changed: remove or simplify parts")
    crit = [i for i in t1_after.issues if i.severity == "critical" and i.code != "tri_budget"]
    if _crit(t1_after) > _crit(t1_before):
        out.append("the project's check finds new critical issues: " + "; ".join(f"{i.code} at {i.node_id}: {i.detail}" for i in crit[:4]))
    return "; ".join(out)


# Export gates (no warn bands). UV: every mesh's mean area distortion must be within
# EngineBudget.max_uv_stretch; IR primitives get exact developable charts (0 distortion) and computed charts
# are size-capped to stay inside the budget (see fast_ops.unwrap_scene). LOD pop: levels are generated to
# lod_diff's screen-space error budget and silhouette tolerance, so every transition must meet these thresholds.
LOD_POP_THRESHOLDS = PopThresholds()

@dataclass
class RoundTrace:
    stage: str  # "A" | "B"
    n: int
    tier1: str
    tier1_ok: bool
    tier15: float
    silhouette: float | None
    floating_p: float
    action: str
    fixer: str | None
    reasons: list[str]
    cost_usd: float
    render_tier: str
    note: str = ""
    rolled_back: bool = False
    # Pass A's progress figure (compose.progress_score: tier15 with the brief's answers); None when no brief was asked
    # about (it then equals tier15 and is not written).
    progress: float | None = None
    # The improvement round (1-based) this verdict judged; None for every other round (and then not written).
    improve: int | None = None
    # Pass A gates this verdict's design failed, each with its figure ("defect:defect_clipping=0.90", "silhouette=2.69"),
    # so failures can be counted by gate and defect; None when it passed them all (and then not written).
    gates: list[str] | None = None


@dataclass
class StepResult:
    name: str
    # "passed" | "warned" | "failed" | "skipped" | "output_failed". "failed" is a check OF the package that did not pass: it
    # blocks. "output_failed" is an extra output that could not be made (the FBX): it reads as a failure, is recorded in the
    # manifest and printed, and does not block, because the package (USD, GLB) stands on its own checks.
    status: str
    detail: str = ""  # what was checked or produced: a fact about the asset, shipped in the package manifest
    ms: float = 0.0
    run: str = ""  # a fact about this run only (cache state); with ``ms`` it stays in the run report


@dataclass
class PyramidResult:
    asset: str
    rounds: list[RoundTrace] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    fix_records: list[FixRecord] = field(default_factory=list)
    audit: list[str] = field(default_factory=list)
    stopped: dict[str, str] = field(default_factory=dict)  # pass -> why iteration ended
    converged: dict[str, bool] = field(default_factory=dict)
    render_tier: str = "preview"
    cost_usd: float = 0.0
    tier1_ok: bool = False
    tier15: float | None = 0.0  # None: the judge never answered (``judge_unavailable`` says why); never a zero it did not give
    scores: dict[str, float] = field(default_factory=dict)
    defects: dict[str, float] = field(default_factory=dict)
    needs_review: bool = False
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> path / uri
    objects: dict[str, Any] = field(default_factory=dict)  # live objects (bundle, meshes, maps); not serialised
    author_calls: int = 0
    judge_unavailable: str = ""  # why a visual QA verdict is missing (the pass's stop reason and a failed step say it too)
    unit: str = "usd"  # the ledger's unit; on a token ledger no dollar figure exists and cost_usd is written as null
    brief_unmet: list[str] = field(default_factory=list)  # why the shipped state does not meet the brief (compose.brief_reasons)

    engine_truth_required: bool = False
    live_lookdev: bool = False
    live_parity: bool = False

    @property
    def verified(self) -> bool:
        passed = {step.name for step in self.steps if step.status == "passed"}
        return (self.ok and self.live_lookdev and self.live_parity and self.converged.get("A", False)
                and self.converged.get("B", False) and {"usd_validate", "engine_import", "engine_capture", "engine_parity"} <= passed)

    @property
    def skipped(self) -> list[tuple[str, str]]:
        return [(s.name, s.detail) for s in self.steps if s.status == "skipped"]

    @property
    def failed(self) -> list[tuple[str, str]]:
        return [(s.name, s.detail) for s in self.steps if s.status == "failed"]

    @property
    def failed_outputs(self) -> list[tuple[str, str]]:
        return [(s.name, s.detail) for s in self.steps if s.status == "output_failed"]

    @property
    def warned(self) -> list[tuple[str, str]]:
        return [(s.name, s.detail) for s in self.steps if s.status == "warned"]

    @property
    def ok(self) -> bool:
        return (not self.failed and self.tier1_ok and self.tier15 is not None and self.tier15 >= EXPORT_MIN_TIER15_SCORE
                and not self.needs_review and not self.brief_unmet
                and (not self.engine_truth_required or self.live_lookdev and self.live_parity))

    def summary(self) -> str:
        r = "; ".join(f"{t.stage}{t.n}:{t.action}{'(rolled back)' if t.rolled_back else ''} t15={t.tier15:.2f}" for t in self.rounds)
        st = ", ".join(f"{s.name}={s.status}" for s in self.steps)
        cost = f"${self.cost_usd:.4f}" if self.unit == "usd" else "n/a (token ledger)"
        return f"pyramid[{self.asset}] ok={self.ok} tier={self.render_tier} cost={cost} | {r} | {st}"

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in ("asset", "stopped", "converged", "render_tier", "cost_usd", "tier1_ok", "tier15", "scores",
                                           "defects", "needs_review", "artifacts", "author_calls", "audit", "ok", "verified", "live_lookdev", "live_parity", "engine_truth_required")}
        if self.judge_unavailable:  # only when it happened: a run the judge answered writes the keys it always wrote
            d["judge_unavailable"] = self.judge_unavailable
        if self.brief_unmet:  # only when it happened: a run without a brief (or meeting it) writes the keys it always wrote
            d["brief_unmet"] = list(self.brief_unmet)
        d["rounds"] = [{k: v for k, v in vars(r).items() if k not in ("progress", "improve", "gates") or v is not None} for r in self.rounds]
        if self.unit != "usd":  # no price applied: no dollar figure (a token number never goes into a *_usd key)
            d.update(cost_usd=None, spend_unit=self.unit)
            d["rounds"] = [{**r, "cost_usd": None} for r in d["rounds"]]
        d["steps"] = [vars(s) for s in self.steps]
        d["fix_records"] = [vars(f) for f in self.fix_records]
        d["skipped"] = [{"step": n, "reason": r} for n, r in self.skipped]
        if self.failed_outputs:  # only when there is one: a run without a failed output writes the keys it always wrote
            d["failed_outputs"] = [{"step": n, "reason": r} for n, r in self.failed_outputs]
        return d


# --------------------------------------------------------------------------- material synthesis (speculative PBR)


async def synthesize_materials(
    scene: SceneIR, forge: Any, *, plane: Any, governor: Governor, prompt: str, uploader: Uploader | None = None,
    cache: Any = None, size: int | None = None, concurrency: int | None = None, brief: str = "",
) -> dict[str, PBRTournamentResult]:
    """One ``pbr_tournament`` per material, concurrently, with best-of-N allocated per material.

    ``scene.materials`` is in priority order (the caller puts the largest surfaces first: ``materials_by_area``). Every
    material gets one candidate before any gets a second, up to the plan's N, within the images and budget left
    (``Governor.allocate``); when everything fits, each gets the plan's N. A material that gets no candidate is in the
    result with no maps and its reason in ``tournament.notes["not_textured"]``, never silently left flat.

    Each material's albedo prompt is ``texture_prompt(m, brief)``: its surface words, never the asset brief.
    ``brief``: an art direction's texture brief, appended to every material prompt ('' appends nothing). ``prompt`` is
    not used for the textures (the asset brief drew object parts into them)."""
    size = size or default_texture_px(forge)
    mats = list(scene.materials)
    unit = image_est(governor, "albedo")
    # A material whose every planned candidate image is already in the image cache (a rerun in the same --out) needs no
    # image and no budget to generate, so it neither takes a slot of the image limit nor of the allowance, which the
    # first run's images already count against (the ledger belongs to the asset). The rest are allocated as before; a
    # material with only some of its variants cached stays in that conservative allocation, and says how many were.
    per = governor.plan.pbr_candidates
    has = getattr(forge, "has_albedo", None)
    prompts = {m.id: texture_prompt(m, brief) for m in mats}
    cached = {m.id: sum(bool(has and has(prompts[m.id], size=size, variant_hint=albedo_variant(i))) for i in range(per)) for m in mats}
    free = {mid for mid, c in cached.items() if per > 0 and c == per}
    paid = [m for m in mats if m.id not in free]
    allocated = dict(zip((m.id for m in paid), governor.allocate("pbr", len(paid), unit_cost=unit, images_each=1)))
    counts = [per if m.id in free else allocated[m.id] for m in mats]
    outs = await asyncio.gather(*(pbr_tournament(
        prompts[m.id], forge, plane=plane, governor=governor, material_id=m.id, metallic_hint=m.metallic,
        uploader=uploader, n=n, size=size, cache=cache, concurrency=concurrency, all_cached=m.id in free)
        for m, n in zip(mats, counts) if n > 0))
    done = iter(outs)
    result: dict[str, PBRTournamentResult] = {}
    for m, n in zip(mats, counts):
        if n > 0:
            result[m.id] = next(done)
            if cached[m.id]:  # the report says what the cache covered, whatever it decided
                result[m.id].tournament.notes["cached_variants"] = f"{cached[m.id]} of {n} candidate image(s) were in the cache"
            continue
        # Name the limit that refused. The tournaments above have settled, so what is left is what one more candidate faces.
        images_left = None if getattr(governor, "max_images", None) is None else governor.max_images - governor.images_used
        fmt = getattr(governor, "fmt", lambda x: f"${x:.5f}")
        if images_left is not None and images_left < 1:
            why = f"image limit reached ({governor.images_summary()}): larger surfaces were textured first"
        elif governor.remaining + 1e-9 < unit:
            why = f"budget: {fmt(governor.remaining)} left, one candidate needs about {fmt(unit)}; larger surfaces were textured first"
        else:
            why = "the spend plan allows no candidate for this material"
        t = TournamentResult("pbr", 0, None, [], {}, notes={"not_textured": why})
        t.unit = getattr(governor, "unit", "usd")
        result[m.id] = PBRTournamentResult(m.id, t)
    return result


def materials_by_area(meshes: list[Any], materials: list[MaterialIR]) -> list[MaterialIR]:
    """``materials`` ordered by the surface area they cover in ``meshes`` (largest first; ties keep their order)."""
    area: dict[str | None, float] = {}
    for m in meshes:
        pts, tri = np.asarray(m.points, np.float64), np.asarray(m.triangles)
        if len(tri):
            a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
            area[m.material_id] = area.get(m.material_id, 0.0) + float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum() / 2)
    return sorted(materials, key=lambda mat: -area.get(mat.id, 0.0))


def _why_untextured(r: PBRTournamentResult) -> str:
    t = r.tournament
    if t.notes.get("not_textured"):
        return t.notes["not_textured"]
    first = next(iter((t.failures or t.disqualified).values()), None)
    if first is not None:
        return f"every candidate failed ({len(t.failures)} failed, {len(t.disqualified)} disqualified); first: {first}"[:200]
    return "no candidate won its tournament"


def default_texture_px(forge: Any) -> int:
    """Side of generated PBR maps when no art direction sets one: small on the offline fakes, 2K live."""
    return 256 if forge.offline else 2048


# --------------------------------------------------------------------------- helpers


def _crit(t1: Any) -> int:
    return sum(i.severity == "critical" for i in t1.issues)


def _patch_summary(patch: IRPatch, limit: int = 12) -> str:
    """What a patch did, for the request that follows its rollback: "16 op(s): upsert_node latch_clasp_l, ..."."""
    def target(op: Any) -> str:
        return str(getattr(op, "node_id", None) or getattr(getattr(op, "node", None), "id", None)
                   or getattr(getattr(op, "material", None), "id", None) or "")

    names = [f"{op.op} {target(op)}".strip() for op in patch.ops]
    more = f", and {len(names) - limit} more" if len(names) > limit else ""
    return f"your patch had {len(patch.ops)} op(s): {', '.join(names[:limit])}{more}"


def _patch_params(patch: IRPatch) -> list[str]:
    out: list[str] = []
    for op in patch.ops:
        if isinstance(op, SetParam):
            out.append(f"{op.node_id}.{op.name}")
        elif isinstance(op, SetTransform):
            out.append(f"{op.node_id}.translate")
        elif isinstance(op, UpsertNode):
            out.append(f"{op.node.id}.node")
        elif isinstance(op, RemoveNode):
            out.append(f"{op.node_id}.removed")
        elif isinstance(op, AssignMaterial):
            out.append(f"{op.node_id}.material")
        elif isinstance(op, UpsertMaterial):
            out.append(f"{op.material.id}.material")
    return out


def preview_sheet(meshes: list[Any], tint: dict[str | None, tuple[float, float, float]], size: int = 256) -> Image.Image:
    """Cheap lookdev preview: clay shading modulated by each material's mean albedo (not a PBR render)."""
    views = ("front", "right", "top", "persp")
    sheet = Image.new("RGB", (2 * size, 2 * size), (34, 36, 42))
    for i, v in enumerate(views):
        b = render_buffers(meshes, v, size)
        shade = b.color.astype(np.float32) / CLAY[None, None, :]
        col = np.array([tint.get(m.material_id, (0.8, 0.8, 0.8)) for m in meshes] + [(0.8, 0.8, 0.8)], np.float32) * 255.0
        pix = col[np.where(b.prim_id >= 0, b.prim_id, len(meshes))]
        img = np.where((b.prim_id >= 0)[..., None], np.clip(shade * pix, 0, 255), b.color).astype(np.uint8)
        tile = Image.fromarray(img)
        ImageDraw.Draw(tile).text((5, 2), v, fill=(255, 255, 255))
        sheet.paste(tile, ((i % 2) * size, (i // 2) * size))
    return sheet


def _question_text(qid: str) -> tuple[str, list[str]]:
    """The judge's question ``qid`` as an author can read it (the sheet's own description and image placeholders taken
    out) and its score levels ([] for a yes/no question); ('', []) for a question this bank does not hold."""
    from antigravity3d.decisions import questions as Q

    spec = Q.VISUAL_QA_QUESTIONS.get(qid)
    if spec is None:
        return "", []
    text = str(spec.question["instructions"])
    for prefix in (Q._SHEET, "{ortho} is a four-view orthographic blueprint of the intended asset. ", "{style} is a style reference image. "):
        text = text.replace(prefix, "")
    text = text.replace("{ortho}", "the blueprint").replace("{style}", "the style reference").replace("{sheet}", "the renders")
    levels = spec.question.get("criteria") if spec.question["type"] == "score" else None
    return text, list(levels or [])


# Tier-1 findings an author request lists one by one (each names its parts, e.g. "lid_core/latch_hinge_L: ~32% of probe
# points inside"); a live worker crate had 28 (26 interpenetrations, 2 z-fighting). The rest are counted, not dropped silently.
MAX_TIER1_LINES = 60


def verdict_lines(result: VisualQAResult, th: Thresholds, located: dict[str, list[str]] | None = None,
                  tier1: Any = None) -> tuple[str, ...]:
    """What the judge said about a design, for the author: every score with the level it is nearest to, and every
    question it answered yes to at or above the ambiguity floor (``Thresholds.ambiguity_lo``, where a defect starts to
    count) with the parts it was located on. The judge returns numbers, not text; without these lines an author sees only
    the first reasons of the action. ``tier1``: the project's own check of the same design (``tier1_qa``): every finding
    with its detail, which names the parts (the pair that interpenetrates, how deep), so a judge's "parts clip" comes
    with where; without it, the parts Tier-1 located (``located["tier1:<code>"]``)."""
    located = located or {}
    lines: list[str] = []
    for qid, v in sorted(result.scores.items()):
        text, levels = _question_text(qid)
        if not text:
            continue
        level = min(len(levels) - 1, max(0, int(round(v)))) if levels else None
        lines.append(f"{qid} {v:.2f}: {text}" + (f" Nearest level {level}: {levels[level]}" if level is not None else ""))
    for qid, p in sorted(result.defects.items()):
        text, _ = _question_text(qid)
        if p < th.ambiguity_lo or not text:
            continue
        where = located.get(qid) or []
        codes = KERNEL_CHECKED.get(qid)
        denied = (codes is not None and tier1 is not None and hasattr(tier1, "issues")
                  and not any(i.code in codes for i in tier1.issues))
        lines.append(f"{qid} P={p:.2f}: {text}" + (f" Located on: {', '.join(where)}." if where else "")
                     + (" The project's own check of the geometry finds none: do not change anything for this." if denied else ""))
    issues = list(getattr(tier1, "issues", None) or [])
    if issues:
        for i in issues[:MAX_TIER1_LINES]:
            lines.append(f"project check {i.code} ({i.severity})" + (f" at {i.node_id}" if i.node_id and i.node_id not in i.detail else "")
                         + f": {i.detail}")
        if len(issues) > MAX_TIER1_LINES:
            lines.append(f"project check: {len(issues) - MAX_TIER1_LINES} more finding(s) not listed")
        return tuple(lines)
    for key, ids in sorted(located.items()):
        if key.startswith("tier1:") and ids:
            lines.append(f"project check {key[6:]} on: {', '.join(ids)}")
    return tuple(lines)


class JudgeUnavailable(Exception):
    """A visual QA verdict could not be had: the judge call failed after the shared retry schedule (``service_answer``),
    was not admitted, or answered nothing usable. A pass that meets it stops, never approves, and the run goes on to
    export what exists with its gates failed and this reason recorded."""


def _judge_failure(exc: BaseException) -> bool:
    """A judge (or its upload) failure, as opposed to a bug here: the decisions error family, a refused admission, an
    outcome the retries could not get past, or a service / transport failure."""
    from antigravity3d import service_answer
    from antigravity3d.decisions.models import DecisionError

    # Not DecisionBudgetError: that is a configuration refusal (an unpriced judge on a governed run), which stays one.
    return (isinstance(exc, (DecisionError, BudgetExceeded, service_answer.OutcomeUnknown))
            or isinstance(exc, Exception) and service_answer.classify(exc, float("inf")) != "final"
            or service_answer.is_timeout(exc) and isinstance(exc, Exception))


@dataclass
class _QA:
    result: VisualQAResult
    action: Action
    t15: float
    uri: str
    cost: float
    legend: dict[int, str] | None = None
    progress: float = 0.0  # compose.progress_score (equals t15 when the brief was not asked about)
    judged: JudgedDesign | None = None  # pass A: the design as judged, with the views an author request carries


@dataclass
class _Pending:
    tx: str
    n_crit: int
    t15: float
    tier1_ok: bool
    scores: dict[str, float]
    defects: dict[str, float]
    ops: list[str]
    params: list[str]
    note: str
    rule: str = ""
    scene_hash: str = ""
    origin: str = "unknown"
    progress: float = 0.0
    t1: Any = None  # the Tier-1 result and the verdict of the design before the change (judge_change compares with them)
    q: Any = None
    summary: str = ""  # what the change did (_patch_summary), for the request that follows its rollback


class _Run:
    def __init__(self, sm: Any, **kw: Any) -> None:
        self.sm = sm
        self.__dict__.update(kw)
        self.res = PyramidResult(asset=sm.asset_prim.GetName(), engine_truth_required=self.require_verified or self.governor.plan.engine_truth,
                                 unit=getattr(self.governor, "unit", "usd"))
        self.maps: dict[str, PBRMaps] = {}
        self.render_png: bytes | None = None
        self.locate_noted = False
        self.bundle_revision: tuple[str, ...] | None = None
        self.export_bundle: ExportBundle | None = None
        self.bundle_cache_hit: bool | None = None
        self.crease = self.art.crease_deg() if self.art is not None else CREASE_DEG

    def texture_px(self) -> int | None:
        """The art direction's map size for this engine (None: the pipeline's default)."""
        if self.forge is None:
            return None
        if self.art is None or self.art.texture_px_prop is None:  # the default size, capped to the engine's (Roblox: 1024)
            default = default_texture_px(self.forge)
            capped = engine_texture_px(self.budget.target_engine, default)
            return capped if capped < default else None
        return self.art.texture_px(self.budget.target_engine, "prop", default_texture_px(self.forge))

    # ---- bookkeeping --------------------------------------------------------------------------------

    def step(self, name: str, status: str, detail: str = "", t0: float | None = None) -> None:
        self.res.steps.append(StepResult(name, status, detail, 0.0 if t0 is None else (time.perf_counter() - t0) * 1e3))

    def snapshot(self, t1_ok: bool, q: _QA) -> None:
        r = self.res
        r.tier1_ok, r.tier15, r.scores, r.defects = t1_ok, q.t15, dict(q.result.scores), dict(q.result.defects)
        r.needs_review = q.action.kind == "escalate_human" or not getattr(self.plane, "calibrated", True)

    async def qa(self, stage: str, png: bytes, legend: dict[int, str] | None = None) -> _QA:
        try:
            uri = await self.upload(png)
            brief = {"brief": self.prompt} if self.prompt else {}  # the user's request; none: the request is as it was
            r = await self.plane.evaluate_visual_qa(uri, self.ortho_uri, self.style_uri, stage, marked_legend=legend, governor=self.governor,
                                                    **brief)
        except Exception as e:
            if not _judge_failure(e):
                raise
            from antigravity3d import service_answer

            raise JudgeUnavailable(f"{stage} visual QA: {service_answer.describe(e, 300)}") from e
        return self.verdict(stage, r, uri, legend)

    def verdict(self, stage: str, r: VisualQAResult, uri: str, legend: dict[int, str] | None, judged: JudgedDesign | None = None,
                *, reused: bool = False) -> _QA:
        action = decide_action(r, self.th, stage)
        cost = 0.0 if reused else r.result.booked_usd  # a verdict made (and booked) elsewhere is not counted again
        self.res.cost_usd += cost
        return _QA(r, action, tier15_score(r, self.th), uri, cost, legend, progress_score(r, self.th), judged)

    async def qa_design(self, meshes: list[Any], judged: JudgedDesign | None = None) -> _QA:
        """Pass A's blockout visual QA of the current design (``tactics.judging.judge_design``). ``judged``: a verdict already made for
        exactly this design (the ranking's of its winner), used instead of asking again."""
        reused = judged is not None
        if judged is None:
            try:
                judged = await judge_design(self.plane, meshes, brief=self.prompt or None, governor=self.governor, uploader=self.upload,
                                             ortho_uri=self.ortho_uri, style_uri=self.style_uri, crease_deg=self.crease)
            except Exception as e:
                if not _judge_failure(e):
                    raise
                from antigravity3d import service_answer

                raise JudgeUnavailable(f"blockout visual QA: {service_answer.describe(e, 300)}") from e
        return self.verdict("blockout", judged.result, judged.views[0][1], judged.legend, judged, reused=reused)

    def gate_figures(self, t1: Any, q: _QA) -> list[str] | None:
        from antigravity3d.decisions.compose import BRIEF_DEFECT, BRIEF_SCORE

        figure = {"silhouette": q.result.scores.get("score_silhouette"), "floating": q.result.defects.get("defect_floating"),
                  "brief_part_missing": q.result.defects.get(BRIEF_DEFECT), "brief_depiction": q.result.scores.get(BRIEF_SCORE)}
        out = []
        for g in sorted(self.gates(t1, q)):
            v = q.result.defects.get(g[7:]) if g.startswith("defect:") else figure.get(g)
            out.append(f"{g}={v:.2f}" if isinstance(v, (int, float)) else g)
        return out or None

    def trace(self, stage: str, n: int, t1: Any, q: _QA, tier: str) -> RoundTrace:
        t = RoundTrace(stage, n, t1.one_line() if t1 else "n/a", bool(t1.ok) if t1 else True, q.t15, q.result.scores.get("score_silhouette"),
                       q.result.defects.get("defect_floating", 0.0), q.action.kind, q.action.fixer, q.action.reasons[:4], q.cost, tier,
                       progress=q.progress if has_brief_answers(q.result.defects) or has_brief_answers(q.result.scores) else None)
        if stage == "A" and t1 is not None:
            t.gates = self.gate_figures(t1, q)
        self.res.rounds.append(t)
        self.res.audit.append(f"{stage}{n} " + q.action.audit_delta().replace("\n", " | "))
        return t

    def fix_record(self, p: _Pending, q: _QA) -> None:
        rec = FixRecord(self.asset_id or self.res.asset, p.defects, p.ops, p.params, p.t15, q.t15, p.note,
                        rule=p.rule, scene_hash=p.scene_hash, origin=(
                            p.origin if q.result.result.calibrated and q.result.result.source == "decisions"
                            else f"{p.origin}/unverified_followup"))
        self.res.fix_records.append(rec)
        if self.fixlog is not None:
            self.fixlog.append(rec)

    # ---- deterministic settle -------------------------------------------------------------------------

    def settle_patch(self, t1: Any, extra: set[str] | None = None) -> tuple[IRPatch | None, str]:
        meshes = self.sm.extract_meshes()
        ids = {m.node_id for m in meshes}
        floating = {i.node_id for i in t1.issues if i.code == "floating_part" and i.node_id} | (extra or set())
        bounds = mesh_bounds(meshes)
        beside = {n for n in floating if n in bounds and beside_a_part(bounds, n)}  # left to the author, see kernel/settle.py
        floating -= beside
        movable = (floating & ids) or (ids if any(i.code == "ungrounded" for i in t1.issues) or extra == set() else set())
        if not movable:
            return None, "floating part beside another part, not above anything: left to the author" if beside else "nothing to settle"
        try:
            sr = settle(meshes, movable)
            ops = sr.to_ir_ops(self.sm.base_ir())
        except (ValueError, KeyError) as e:
            return None, f"settle unsupported: {e}"
        return (IRPatch(ops=ops), f"settled {len(ops)} node(s)") if ops else (None, "already settled")

    # ---- deterministic separation of coplanar faces (kernel/separate.py) --------------------------------

    def separate_step(self, name: str) -> None:
        """Move single faces so no two parts have same-facing faces in one plane (no model call), recorded as step
        ``name``: what moved and by how much, the contact faces that do not ship, what was left to the author. Rolled
        back (and said so) if it would raise the Tier-1 critical count or make a part float."""
        t0 = time.perf_counter()
        sm = self.sm
        scene = sm.base_ir()
        meshes = sm.extract_meshes()
        try:  # faces the exporter leaves out (its own rule, fast_ops.shipped_masks) are not drawn: reported, not moved
            r = separate_coplanar(scene, meshes, hidden=shipped_masks(meshes, self.budget))
        except (ValueError, KeyError) as e:
            self.step(name, "skipped", f"separation unsupported: {e}", t0)
            return
        why = r.describe()
        if not r.ops:
            self.step(name, "warned" if r.left else "skipped", why, t0)
            return
        t1 = tier1_qa(meshes, self.budget)
        tx = sm.apply_patch(IRPatch(ops=r.ops))
        t2 = tier1_qa(sm.extract_meshes(), self.budget)
        floats = lambda t: {(i.code, i.node_id) for i in t.issues if i.code in ("floating_part", "floating_group")}  # noqa: E731
        if _crit(t2) > _crit(t1) or floats(t2) - floats(t1):
            sm.rollback(tx)
            cause = (f"Tier-1 critical count {_crit(t1)}->{_crit(t2)}" if _crit(t2) > _crit(t1)
                     else "floating " + ", ".join(sorted(n or "?" for _, n in floats(t2) - floats(t1))))
            self.step(name, "warned", f"rolled back ({cause}): {why}", t0)
            return
        self.step(name, "warned" if r.left else "passed", why, t0)

    # ---- Pass A ---------------------------------------------------------------------------------------

    async def locate(self, t1: Any, q: _QA) -> dict[str, list[str]]:
        located: dict[str, list[str]] = {}
        for i in t1.issues:
            lst = located.setdefault(f"tier1:{i.code}", [])
            if i.node_id and i.node_id not in lst:
                lst.append(i.node_id)
        flagged = [d for d, p in q.result.defects.items() if p >= self.th.ambiguity_lo and d in MARKED_DEFECT_PHRASES]
        if not flagged or not q.legend:
            return located
        fn = getattr(self.plane, "locate_defects", None)
        if fn is None:
            if not self.locate_noted:
                self.step("locate_defects", "skipped", "plane has no locate_defects; repairs use Tier-1 locations only")
                self.locate_noted = True
            return located
        charges: list[float] = []
        try:
            # Parts are located on the badged views (the judge scored the plain ones: a legend of part names raised its
            # depiction scores by up to 0.8 when measured, so it never sees them); a design judged as one marked sheet
            # (lookdev) is located on that sheet.
            j = q.judged
            marked = (tuple((name, uri) for (name, _), (_, uri) in zip(j.views, j.author_views))
                      if j is not None and len(j.author_views) >= 2 and len(j.author_views) == len(j.views) else q.uri)
            for d, lst in (await fn(marked, q.legend, flagged, governor=self.governor, on_settle=charges.append)).items():
                located[d] = [n for n, _ in lst]
        except Exception as e:  # noqa: BLE001 - localisation is best-effort
            self.step("locate_defects", "failed", f"{type(e).__name__}: {e}")
        self.res.cost_usd += sum(charges)
        return located

    def judge_unavailable(self, pass_name: str, rnd: int, e: JudgeUnavailable, rolled_back: str = "") -> None:
        """Stop the pass on a missing verdict: never approved, the reason recorded where the gates, the report, the
        manifest (its evidence steps) and the last lines carry it. Tier-1 keeps its own (deterministic) result."""
        res = self.res
        reason = f"judge unavailable: {e}"
        res.stopped[pass_name] = reason
        res.converged[pass_name] = False
        res.needs_review = True  # nothing judged this state: a person must look
        res.judge_unavailable = res.judge_unavailable or f"pass {pass_name} round {rnd}: {e}"
        self.step(f"visual_qa_{pass_name}", "failed", (f"{reason} (round {rnd})" + (f"; {rolled_back}" if rolled_back else ""))[:400])

    async def author_repair(self, q: _QA, located: dict[str, list[str]], t1: Any = None, previous: str = "") -> tuple[Any, bool]:
        """One author repair call: ``(patch, failed)``. A failed call is a recorded result, not a crash.

        A truncated repair (or one that ended STOP with an empty answer) on a tier below the director is retried once on
        the director tier when the plan allows it (a larger budget the plan already grants, not the same request again).
        Every other typed failure (blocked, invalid, timed out, not admitted) ends the pass with a stop reason naming the
        cause. Any other exception type is a bug and propagates.
        """
        from antigravity3d.tactics.metered_author import AuthorNoAnswer, AuthorOutcomeError, AuthorTruncated, MeteredAuthor

        res = self.res
        sm = self.sm

        def waits() -> str:
            """The service's refusals and the waits before this repair's answer (a fact about the run: ``StepResult.run``)."""
            tries = getattr(self.author, "last_service_retries", None) or []
            return "; ".join(f"attempt {a['attempt']} {a['outcome']}: {a['answer'][:120]}"
                             + (f", waited {a['wait_s']:.0f} s" if a.get("wait_s") is not None else "") for a in tries)

        def fail(reason: str) -> tuple[None, bool]:
            res.stopped["A"] = f"author repair failed: {reason}"
            res.converged["A"] = False
            self.step("author_repair", "warned", reason)
            res.steps[-1].run = waits()
            return None, True

        action = self.author_action(q)
        if previous and not isinstance(self.author, MeteredAuthor):  # a metered author reads it with the views
            from dataclasses import replace

            action = replace(action, reasons=[*action.reasons, f"your previous patch for this design was undone: {previous}"])
        res.author_calls += 1
        # A metered author is shown the design it is asked to repair (other authors take the request as it always was).
        seen = {"views": self.author_views(q, located, t1, previous)} if isinstance(self.author, MeteredAuthor) else {}
        try:
            patch = await self.author.repair(sm.base_ir(), action=action, located=located, manifest=sm.manifest(), **seen)
            if run := waits():  # served after the service turned attempts away: the waits are recorded
                self.step("author_repair", "passed", "answered after the service turned attempt(s) away")
                res.steps[-1].run = run
            return patch, False
        except BudgetExceeded as e:
            return fail(f"not admitted: {e}")
        except AuthorOutcomeError as first:
            kind = type(first).__name__
            empty = isinstance(first, AuthorNoAnswer) and first.finish_reason == "STOP"
            if not (isinstance(first, AuthorTruncated) or empty):
                return fail(f"{kind} on {first.model} ({first.finish_reason}): {first}")
            if not isinstance(self.author, MeteredAuthor) or first.role == "director":
                return fail(f"{kind} on {first.role} tier ({first.finish_reason}); no larger tier to retry on: {first}")
            if not self.governor.plan.allow_director:
                return fail(f"{kind} on {first.role} tier ({first.finish_reason}); director retry not permitted by spend plan")
            res.author_calls += 1
            try:
                patch = await self.author.repair(sm.base_ir(), action=action, located=located, manifest=sm.manifest(), role="director",
                                                 **seen)
                self.step("author_repair", "passed", f"{kind} on {first.role} tier; succeeded on the director tier")
                return patch, False
            except BudgetExceeded as e:
                return fail(f"{kind} on {first.role} tier; director retry not admitted: {e}")
            except AuthorOutcomeError as second:
                return fail(f"{kind} on {first.role} tier; director retry also {type(second).__name__} ({second.finish_reason}): {second}")

    def author_views(self, q: _QA, located: dict[str, list[str]], t1: Any = None, previous: str = "") -> Any:
        """The renders an author request carries (``metered_author.DesignViews``): the marked views of the design the
        verdict ``q`` judged (``JudgedDesign.author_views``, uploaded with it through the run's uploader: the cached forge
        notes each one's digest, so the design record keys a rerun's identical render as the same request), the badge
        legend and the judge's verdict on it. None when ``q`` carries no judged design (a verdict of another kind)."""
        from antigravity3d.tactics.metered_author import DesignViews

        if q.judged is None or not q.judged.author_views:
            return None
        return DesignViews(q.judged.author_views, q.judged.legend_text, verdict_lines(q.result, self.th, located, t1), previous)

    def author_action(self, q: _QA) -> Action:
        """The action the author is given. When the design falls short of the brief, its reasons end with what the judge
        found and the brief itself (``BRIEF_REASON``): the judge returns probabilities, so without the request the author
        cannot know what to add. The recorded action (trace, audit) is ``q.action``, unchanged; so is every other repair."""
        found = brief_reasons(q.result.defects, q.result.scores, self.th)
        if not found or not self.prompt:
            return q.action
        from dataclasses import replace

        from antigravity3d.decisions.engine import clean_brief

        return replace(q.action, reasons=[*q.action.reasons, *found, BRIEF_REASON + clean_brief(self.prompt, 2000)])

    def gates(self, t1: Any, q: _QA) -> set[str]:
        """The pass A gates this design fails: "tier1", "silhouette" (below the lock), "floating", "escalate_human",
        "brief_part_missing", "brief_depiction" (the brief's requirements, when asked), and "defect:<id>" for a geometry
        defect the judge answers with high confidence (>= ``Thresholds.defect_high``). Empty: pass A converged.

        The last is new in round five: before it, pass A converged on a design the judge said clips (P 0.9) as long as
        nothing floated, and nothing sent it back. With separate views the judge flags real interpenetration (live
        2026-11-22: 0.82 on a design with 26 Tier-1 interpenetration warnings, 0.08-0.20 on designs without), so a
        confirmed defect now goes to a repair, which is shown the pairs Tier-1 names."""
        th = self.th
        sil, fp = q.result.scores.get("score_silhouette"), q.result.defects.get("defect_floating", 0.0)
        out = set()
        if not t1.ok:
            out.add("tier1")
        if sil is not None and sil < th.silhouette_lock:
            out.add("silhouette")
        if fp > th.floating_max and self.kernel_confirms(t1, "defect_floating") is not False:
            out.add("floating")
        if q.action.kind == "escalate_human":
            out.add("escalate_human")
        from antigravity3d.decisions.compose import BRIEF_DEFECT, BRIEF_SCORE, GEOMETRY_DEFECTS

        out |= {f"defect:{d}" for d, p in q.result.defects.items()
                if p >= th.defect_high and d in GEOMETRY_DEFECTS and d not in (BRIEF_DEFECT, "defect_floating")
                and self.kernel_confirms(t1, d) is not False}

        if (p := q.result.defects.get(BRIEF_DEFECT)) is not None and p >= th.ambiguity_lo:
            out.add("brief_part_missing")
        if (v := q.result.scores.get(BRIEF_SCORE)) is not None and v < th.brief_depiction_min:
            out.add("brief_depiction")
        return out

    def adopt(self, judged: JudgedDesign | None, note: str) -> None:
        """Pass A for a design taken as given (``refine(run_pass_a=False)``): its verdict, when there is one, becomes the
        state's (one recorded round, no change made); without one the judge's scores are missing, never zero."""
        res = self.res
        meshes = self.sm.extract_meshes()
        if not meshes:
            res.stopped["A"] = "empty scene"
            self.step("pass_a", "failed", "scene has no meshes")
            return
        t1 = tier1_qa(meshes, self.budget)
        res.stopped["A"] = note
        if judged is None:
            res.tier1_ok, res.tier15, res.scores, res.defects = t1.ok, None, {}, {}
            res.converged["A"] = False
            return
        q = self.verdict("blockout", judged.result, judged.views[0][1], judged.legend, judged, reused=True)
        self.trace("A", 1, t1, q, "preview")
        self.snapshot(t1.ok, q)
        res.converged["A"] = self.passes(t1, q)

    def passes(self, t1: Any, q: _QA) -> bool:
        """Pass A's convergence: Tier-1 clean, silhouette locked, nothing floating, nothing for a person, the brief met."""
        return not self.gates(t1, q)

    def kernel_confirms(self, t1: Any, defect: str) -> bool | None:
        """Whether the kernel's own check finds ``defect`` on this design (KERNEL_CHECKED); None for a defect only the
        judge can see."""
        codes = KERNEL_CHECKED.get(defect)
        if codes is None or t1 is None or not hasattr(t1, "issues"):
            return None
        return any(i.code in codes for i in t1.issues)

    def flagged(self, q: _QA, t1: Any = None) -> set[str]:
        """The defects the judge reports at or above the ambiguity floor, without those the kernel checks and does not
        find on this design (``t1``: its Tier-1 report; None: every one the judge reports)."""
        return {d for d, p in q.result.defects.items() if p >= self.th.ambiguity_lo and self.kernel_confirms(t1, d) is not False}

    def score_only(self, t1: Any, q: _QA) -> bool:
        """The design fails only score gates (silhouette below its lock, depiction below the brief's bar, a named part
        the judge cannot find) and has no defect to locate and repair: Tier-1 clean, nothing flagged but the brief's own
        question. A score below its gate is not a fault a repair can name; it is what an improvement round is for."""
        from antigravity3d.decisions.compose import BRIEF_DEFECT

        return bool(self.gates(t1, q)) and self.gates(t1, q) <= SCORE_GATES and not (self.flagged(q, t1) - {BRIEF_DEFECT})

    def can_improve(self, q: _QA) -> bool:
        from antigravity3d.decisions.compose import BRIEF_SCORE

        return (self.governor.plan.improve_rounds > 0 and bool(self.prompt) and getattr(self.author, "improve", None) is not None
                and q.result.scores.get(BRIEF_SCORE) is not None)

    def judge_change(self, t1: Any, q: _QA, t1n: Any, qn: _QA) -> tuple[bool, str]:
        """Whether a change (a repair or an improvement round) made the design better, on evidence, and the evidence.

        Worse, and rolled back, on any of: more Tier-1 critical issues; a part or group Tier-1 now finds floating (a
        warning to Tier-1, a defect to a game developer: the rule separate_step applies too); a defect the judge newly
        flags (P >= ambiguity_lo, where its answers separate: planted defects 0.8-0.9, intact designs at most 0.2, live
        2026-11-22); a pass A gate that held and now fails; a score that fell by more than ``IMPROVE_NOISE_BAND``.
        Better, and kept when nothing is worse, on any of: a flagged defect no longer flagged; fewer Tier-1 critical
        issues or floating parts; a score that rose by at least the band. Neither: rolled back, since nothing shows the
        change did anything the judge's own spread on an unchanged design would not. A score difference inside the band
        counts as neither: a change is never kept or undone on noise."""
        band = IMPROVE_NOISE_BAND
        floats = lambda t: {(i.code, i.node_id) for i in t.issues if i.code in ("floating_part", "floating_group")}  # noqa: E731
        before, after = q.result.scores, qn.result.scores
        worse: list[str] = []
        if _crit(t1n) > _crit(t1):
            worse.append(f"Tier-1 critical issues {_crit(t1)}->{_crit(t1n)}")
        if new := sorted(n or "?" for _, n in floats(t1n) - floats(t1)):
            worse.append("Tier-1 finds a new floating part: " + ", ".join(new))
        if raised := sorted(self.flagged(qn, t1n) - self.flagged(q, t1)):
            worse.append("newly flagged: " + ", ".join(f"{d}={qn.result.defects[d]:.2f}" for d in raised))
        if failing := sorted(self.gates(t1n, qn) - self.gates(t1, q)):
            worse.append("gate now failing: " + ", ".join(failing))
        if fell := [f"{k} {v:.2f}->{after[k]:.2f}" for k, v in sorted(before.items()) if k in after and after[k] < v - band - EPS]:
            worse.append(f"fell by more than the judge's noise band ({band}): " + ", ".join(fell))
        if worse:
            return False, "; ".join(worse)
        better: list[str] = []
        if cleared := sorted(self.flagged(q, t1) - self.flagged(qn, t1n)):
            better.append("cleared " + ", ".join(f"{d} {q.result.defects[d]:.2f}->{qn.result.defects.get(d, 0.0):.2f}" for d in cleared))
        if _crit(t1n) < _crit(t1):
            better.append(f"Tier-1 critical issues {_crit(t1)}->{_crit(t1n)}")
        if landed := sorted(n or "?" for _, n in floats(t1) - floats(t1n)):
            better.append("no longer floating: " + ", ".join(landed))
        if rose := [f"{k} {v:.2f}->{after[k]:.2f}" for k, v in sorted(before.items()) if k in after and after[k] >= v + band - EPS]:
            better.append(f"rose by the judge's noise band ({band}) or more: " + ", ".join(rose))
        if not better:
            return False, f"no change beyond the judge's noise band ({band}) and no defect cleared"
        return True, "; ".join(better)

    # ---- Pass A improvement rounds ----------------------------------------------------------------------

    def improvement_need(self) -> float:
        """What one improvement round is expected to cost (the author's expected call on the round's tier plus its visual
        QA) and what must stay for pass B and C after it (three lookdev rounds and one grading call), in the ledger's
        unit. Planning only: admission reserves each call's counted worst case."""
        from antigravity3d.tactics.common import GRADE_TOKENS_EST

        plan = self.governor.plan
        role = plan.improve_role or plan.builder_role
        profile = self.author.cost_profile(role) if hasattr(self.author, "cost_profile") else None
        author = profile[0] if profile else 0.0
        later = self.governor.est(usd=LOOKDEV_ROUNDS * QA_EST_USD, tokens=LOOKDEV_ROUNDS * QA_TOKENS_EST + GRADE_TOKENS_EST)
        # a round's own review and its share of a validation (k judgements every V rounds). Not reserved: a second
        # judgement of a newly reported defect (rare) and the final validation, whose refusal at admission restores the
        # last validated design (validate: "not judged"), which is safe
        k, v = max(1, plan.improve_judgements), max(1, plan.improve_validate_every)
        judgements = 1 + k / v
        return author + self.governor.est(usd=judgements * QA_EST_USD, tokens=judgements * QA_TOKENS_EST) + later

    async def judge_k(self, meshes: list[Any], n: int) -> list[_QA]:
        """``n`` independent blockout reviews of one design, asked in parallel (one slot of wall time)."""
        if n <= 0:
            return []
        return list(await asyncio.gather(*(self.qa_design(meshes) for _ in range(n))))

    def averaged(self, qs: list[_QA]) -> _QA:
        """One verdict from several judgements of one design: every score and defect probability averaged (the verdict
        the state carries after a validation); booked once already, so not counted again."""
        if len(qs) == 1:
            return qs[0]
        keys_s = set().union(*(x.result.scores for x in qs))
        keys_d = set().union(*(x.result.defects for x in qs))
        mean = lambda vals: sum(vals) / len(vals)  # noqa: E731
        r = replace(qs[0].result, scores={k: mean([x.result.scores[k] for x in qs if k in x.result.scores]) for k in keys_s},
                    defects={k: mean([x.result.defects[k] for x in qs if k in x.result.defects]) for k in keys_d})
        return self.verdict("blockout", r, qs[0].uri, qs[0].legend, qs[0].judged, reused=True)

    async def confirmed(self, q: _QA, t1: Any, qn: _QA, t1n: Any, meshes: list[Any]) -> tuple[_QA, str]:
        """``qn`` with every defect only the judge can see that it newly reports asked about once more: it stands only
        if both judgements report it (the lower probability is kept for it; the other answers are the two averaged).
        Returns the verdict and a note of what the second judgement said ('' when none was needed)."""
        raised = {d for d in self.flagged(qn, t1n) - self.flagged(q, t1) if self.kernel_confirms(t1n, d) is None}
        if not CONFIRM_JUDGE_ONLY or not raised:
            return qn, ""
        (second,) = await self.judge_k(meshes, 1)
        both = self.averaged([qn, second])
        defects = dict(both.result.defects)
        for d in raised:
            defects[d] = min(qn.result.defects.get(d, 0.0), second.result.defects.get(d, 0.0))
        merged = self.verdict("blockout", replace(both.result, defects=defects), qn.uri, qn.legend, qn.judged, reused=True)
        said = ", ".join(f"{d} {qn.result.defects[d]:.2f} then {second.result.defects.get(d, 0.0):.2f}" for d in sorted(raised))
        return merged, f"asked again about {said}"

    async def improvement_rounds(self, rnd: int, t1: Any, q: _QA) -> None:
        """Rounds whose aim is a better design (``SpendPlan.improve_*``): after pass A converged, or instead of a repair
        when the design fails only score gates or the judge is unsure (``score_only``, an ambiguous verdict).

        Each round shows the author its renders, the brief and the judge's whole verdict with Tier-1's findings and the
        unused triangle budget (``MeteredAuthor.improve``), applies the patch, cleans up what the patch changed (``tidy``:
        settle, shrink, separate), corrects once what is still wrong by the kernel's evidence (``tidy_findings``) and asks
        the same blockout review (a newly reported defect only the judge can see is asked about again, ``confirmed``).

        A round is kept PROVISIONALLY unless there is evidence it made things worse (``judge_change``'s "worse": a new
        Tier-1 critical issue or floating part, a confirmed newly flagged defect, a gate that now fails, a score down by
        more than the band). Every ``improve_validate_every`` provisional rounds and at the end, the design is judged
        ``improve_judgements`` times in parallel and compared with the best validated state (at first the design the
        rounds started from, judged as many times): the rounds since then stand if the mean depiction rose by
        ``validation_band(k)``, else that state is restored. Real gains per round are smaller than one judgement's noise
        (live 2026-11-22: crate rounds of +0.05 and +0.10 were each undone alone); a downward drift of small steps is
        caught at the next validation, which compares with the validated state, never with the last round.

        Rounds end when the validated design passes pass A and its mean depiction reaches ``improve_target``, after
        ``improve_patience`` rounds in a row undone (or a failed validation), at ``improve_rounds``, when the budget would
        not leave pass B and C their share, or when a round cannot be had; the design shipped is the best validated one.
        Each round is a RoundTrace and a step ``improve_A<k>``; each validation a step ``validate_A<k>`` with every
        judgement."""
        from antigravity3d.decisions.compose import BRIEF_SCORE
        from antigravity3d.decisions.engine import clean_brief
        from antigravity3d.tactics.metered_author import AuthorInvalid, AuthorOutcomeError

        plan, res, sm = self.governor.plan, self.res, self.sm
        base = res.stopped["A"]

        if plan.improve_rounds <= 0 or not self.prompt:  # no rounds planned, or no brief to improve toward: as before
            return
        fn = getattr(self.author, "improve", None)
        if fn is None:
            self.step("improve_A", "skipped", f"{type(self.author).__name__} has no improvement request")
            return
        if q.result.scores.get(BRIEF_SCORE) is None:
            self.step("improve_A", "skipped", "the judge did not answer how well the design depicts the brief")
            return
        k_judge = max(1, int(getattr(plan, "improve_judgements", 1)))
        every = max(1, int(getattr(plan, "improve_validate_every", 1)))
        band_k = validation_band(k_judge)
        dep = lambda x: x.result.scores.get(BRIEF_SCORE, 0.0)  # noqa: E731
        try:  # the design the rounds start from, judged as many times as every later validation
            start_qs = [q, *await self.judge_k(sm.extract_meshes(), k_judge - 1)]
        except JudgeUnavailable as e:
            self.step("improve_A", "warned", f"not started: the start design could not be judged {k_judge} times ({e})"[:400])
            return
        best_q, best_t1 = self.averaged(start_qs), t1
        best_mean, start_mean = dep(best_q), dep(best_q)
        self.snapshot(best_t1.ok, best_q)
        pending: list[str] = []  # transactions of the provisionally kept rounds since the best validated state
        kept = misses = 0
        previous = ""
        brief = clean_brief(self.prompt, 2000)

        def done() -> str:
            return (f"{kept} round(s) kept, depiction (mean of {k_judge}) {start_mean:.2f}->{best_mean:.2f}, "
                    f"start judged {', '.join(f'{dep(x):.2f}' for x in start_qs)}")

        async def validate(k: int, final: bool = False) -> bool | None:
            """Judge the design ``k_judge`` times and decide the rounds since the best validated state: they stand (True)
            when the mean depiction rose by the band (or a flagged defect is gone and nothing got worse); that state is
            restored (False) when the mean fell by the band, or at the end (``final``) when it did not rise; otherwise the
            evidence is not in yet (None): the rounds stay provisional and the next validation sees them with the ones
            after. Small real gains (0.05-0.10 a round) add up across rounds this way; a downward drift is caught as soon
            as it exceeds the band, against the validated state, never against the last round."""
            nonlocal pending, best_q, best_t1, best_mean, q, t1, kept
            if not pending:
                return True
            t0 = time.perf_counter()
            meshes = sm.extract_meshes()
            try:
                qs = await self.judge_k(meshes, k_judge)
            except JudgeUnavailable as e:
                sm.rollback(pending[0])
                pending = []
                q, t1 = best_q, best_t1
                self.snapshot(t1.ok, q)
                self.step(f"validate_A{k}", "warned", f"not judged ({e}); the rounds since the last validated state are undone"[:400], t0)
                return False
            mean = sum(dep(x) for x in qs) / len(qs)
            said = (f"depiction {', '.join(f'{dep(x):.2f}' for x in qs)} (mean {mean:.2f}) against {best_mean:.2f} "
                    f"(band {band_k} for {k_judge} judgements), {len(pending)} round(s)")
            t1v = tier1_qa(meshes, self.budget)
            avg = self.averaged(qs)
            new_gates = self.gates(t1v, avg) - self.gates(best_t1, best_q)
            cleared = sorted(self.flagged(best_q, best_t1) - self.flagged(avg, t1v))
            # The rounds stand when the design reads better (mean depiction up by the band for k judgements), or when a
            # defect the judge flagged is gone, depiction no lower than the band, and no gate newly fails.
            rose = mean >= best_mean + band_k - EPS
            fixed = bool(cleared) and mean >= best_mean - band_k - EPS
            if cleared:
                said += f"; cleared {', '.join(cleared)}"
            fell = mean <= best_mean - band_k + EPS
            if (rose or fixed) and not new_gates:
                kept += len(pending)
                best_q, best_t1, best_mean, pending = avg, t1v, mean, []
                q, t1 = best_q, best_t1
                self.snapshot(t1.ok, q)
                self.step(f"validate_A{k}", "passed", f"kept: {said}"[:400], t0)
                return True
            if not (fell or final or new_gates):
                self.step(f"validate_A{k}", "skipped", f"not decided yet, the rounds stay provisional: {said}"[:400], t0)
                return None
            sm.rollback(pending[0])
            pending = []
            q, t1 = best_q, best_t1
            self.snapshot(t1.ok, q)
            why = "it fell by the band" if fell else "a gate newly fails" if new_gates else "it did not rise by the band"
            self.step(f"validate_A{k}", "warned", f"restored the last validated design ({why}): {said}"[:400], t0)
            return False

        async def stop(why: str, k: int) -> None:
            await validate(k, final=True)
            res.converged["A"] = self.passes(best_t1, best_q)
            res.stopped["A"] = f"{base}; {why}; {done()}"

        for k in range(1, plan.improve_rounds + 1):
            if not pending and self.passes(best_t1, best_q) and best_mean >= plan.improve_target - EPS:
                await stop(f"improvement: target met (mean depiction {best_mean:.2f} >= {plan.improve_target})", k)
                return
            need = self.improvement_need()
            if not self.governor.can_afford(need):
                await stop(f"improvement: budget ({self.governor.fmt(self.governor.remaining)} left; a round and what pass B and C "
                           f"need after it come to about {self.governor.fmt(need)})", k)
                return
            t0 = time.perf_counter()
            located = await self.locate(t1, q)
            views = self.author_views(q, located, t1, previous)
            if views is None:  # an improvement is asked for by showing the design
                await stop("improvement: no renders of the design to show", k)
                return
            name = f"improve_A{k}"
            before_ir = sm.base_ir()
            tris = sum(len(m.triangles) for m in sm.extract_meshes())
            # One correction per round: an answer that does not validate, a patch the kernel refuses, or a patch that the
            # clean-up cannot make sound (still floating, still over budget, a new critical issue) is asked again at once
            # with the exact message; its good operations are not thrown away with a rollback and a fresh round.
            tx, refusal, notes = None, "", []
            for attempt in (1, 2):
                res.author_calls += 1
                try:
                    patch = await fn(before_ir, brief=brief, views=replace(views, previous=refusal) if refusal else views,
                                     manifest=sm.manifest(), triangles=tris)
                except AuthorInvalid as e:
                    refusal = f"your last answer did not validate: {e}"[:900]
                    if attempt == 1:
                        continue
                    patch = None
                    self.step(name, "warned", f"no improvement: the answer did not validate twice ({e})"[:400], t0)
                    break
                except (BudgetExceeded, AuthorOutcomeError) as e:
                    why = f"not admitted: {e}" if isinstance(e, BudgetExceeded) else f"{type(e).__name__} on {e.model} ({e.finish_reason}): {e}"
                    self.step(name, "warned", f"no improvement: the call failed ({why})"[:400], t0)
                    await stop(f"improvement round {k} failed ({type(e).__name__})", k)
                    return
                if patch is None:
                    break
                try:
                    tx = sm.apply_patch(patch)
                except Exception as e:  # noqa: BLE001 - an invalid patch must not kill the run
                    refusal = f"{_patch_summary(patch)}; it was refused before it could be judged: {type(e).__name__}: {e}"[:900]
                    if attempt == 2:
                        self.step(name, "warned", f"patch refused twice: {refusal}"[:400], t0)
                    continue
                from antigravity3d.dags.edit_asset import changed_nodes

                touched = changed_nodes(before_ir, sm.base_ir())
                notes = tidy(sm, self.budget, touched) if self.settle_first else []
                wrong = tidy_findings(t1, tier1_qa(sm.extract_meshes(), self.budget), touched, self.budget)
                if wrong and attempt == 1:
                    sm.rollback(tx)
                    tx = None
                    refusal = f"{_patch_summary(patch)}; after clean-up it was still not sound, so it was not judged: {wrong}"[:900]
                    continue
                break
            if tx is None:
                if refusal:  # refused, invalid or unsound twice: a lost round, and the next request says why
                    previous = refusal
                    misses += 1
                    if misses >= plan.improve_patience:
                        await stop(f"improvement: round {k}'s answer was refused twice", k)
                        return
                    continue
                self.step(name, "skipped", "the author proposed no change", t0)
                await stop(f"improvement: the author proposed no change in round {k}", k)
                return
            new_meshes = sm.extract_meshes()
            t1n = tier1_qa(new_meshes, self.budget)
            try:
                qn = await self.qa_design(new_meshes)
                qn, again = await self.confirmed(q, t1, qn, t1n, new_meshes)
            except JudgeUnavailable as e:  # what ships is what was judged: the unjudged round is undone
                sm.rollback(tx)
                self.snapshot(t1.ok, q)
                self.step(name, "warned", f"not judged ({e}); rolled back"[:400], t0)
                await stop(f"improvement round {k} not judged (judge unavailable): rolled back", k)
                return
            tr = self.trace("A", rnd + k, t1n, qn, "preview")
            tr.improve = k
            depn = qn.result.scores.get(BRIEF_SCORE)
            moved = (f"progress {q.progress:.2f}->{qn.progress:.2f}, depiction {dep(q):.2f}->"
                     + (f"{depn:.2f}" if depn is not None else "n/a") + f", tier15 {q.t15:.2f}->{qn.t15:.2f}, "
                     f"tier1 critical {_crit(t1)}->{_crit(t1n)}, {len(patch.ops)} op(s)"
                     + (f"; clean-up: {'; '.join(notes)}" if notes else "") + (f"; {again}" if again else ""))
            _, evidence = self.judge_change(t1, q, t1n, qn)
            worse = self.worse(t1, q, t1n, qn)
            if worse or depn is None:
                why = worse or "the score of the brief was not answered"
                sm.rollback(tx)
                self.snapshot(t1.ok, q)
                tr.rolled_back, tr.note = True, f"improve {k}: {moved}; rolled back ({why})"
                self.step(name, "warned", f"rolled back ({why}): {moved}"[:400], t0)
                previous = f"{_patch_summary(patch)}; it was undone because {why}"[:600]
                misses += 1
                if misses >= plan.improve_patience:
                    await stop(f"improvement: round {k} made the design worse ({why})", k)
                    return
                continue
            pending.append(tx)
            q, t1, misses, previous = qn, t1n, 0, ""
            self.snapshot(t1.ok, q)
            tr.note = f"improve {k}: {moved}; kept provisionally" + (f" ({evidence})" if evidence else "")
            self.step(name, "passed", (f"kept provisionally ({evidence}): {moved}" if evidence else f"kept provisionally: {moved}")[:400], t0)
            if len(pending) % every == 0 and await validate(k) is False:
                misses += 1
                previous = "the last rounds were undone: judged several times, they did not make the design read better"
                if misses >= plan.improve_patience:
                    await stop(f"improvement: the rounds up to {k} did not validate", k)
                    return
        await stop(f"improvement: {plan.improve_rounds} round(s) used", plan.improve_rounds)

    def worse(self, t1: Any, q: _QA, t1n: Any, qn: _QA) -> str:
        """The evidence that a change made the design worse ('' when there is none): ``judge_change``'s "worse" list."""
        keep, evidence = self.judge_change(t1, q, t1n, qn)
        if keep or evidence.startswith("no change beyond"):
            return ""
        return evidence

    async def pass_a(self) -> None:
        sm, th, res = self.sm, self.th, self.res
        if not sm.extract_meshes():
            res.stopped["A"] = "empty scene"
            self.step("pass_a", "failed", "scene has no meshes")
            return
        pending: _Pending | None = None
        t0 = time.perf_counter()
        unchanged = tuple(sm.tx_stack)
        if self.settle_first:  # deterministic pre-pass: no model call
            t1 = tier1_qa(sm.extract_meshes(), self.budget)
            patch = None
            if t1.issues and all(i.code == "floating_part" for i in t1.issues) and reviewed_rules.is_enabled():
                patch = reviewed_rules.seat_patch(sm.base_ir(), [i.node_id for i in t1.issues])
            if patch is None:
                patch, why = self.settle_patch(t1)
            else:
                why = reviewed_rules.RULE
            if patch is None:
                self.step("settle_pre", "skipped", why)
            else:
                tx = sm.apply_patch(patch)
                if _crit(tier1_qa(sm.extract_meshes(), self.budget)) > _crit(t1):
                    sm.rollback(tx)
                    self.step("settle_pre", "failed", "settle raised Tier-1 critical count; rolled back", t0)
                else:
                    self.step("settle_pre", "passed", why, t0)
            self.separate_step("separate_coplanar")
        if self.judged is not None and tuple(sm.tx_stack) != unchanged:  # settled or separated: not the design it judged
            self.judged = None
        reuse: tuple[Any, _QA] | None = None  # after a rollback: the kept design's own verdict, not asked again
        previous, misses = "", 0  # what the last rolled-back repair did and why; repairs rolled back in a row
        for rnd in range(1, self.max_rounds + 1):
            meshes = sm.extract_meshes()
            if reuse is not None:
                (t1, q), reuse = reuse, None
            else:
                t1 = tier1_qa(meshes, self.budget)
                try:  # the starting design's verdict may come made (the ranking's of its winner): only before any change
                    q = await self.qa_design(meshes, self.judged if rnd == 1 else None)
                except JudgeUnavailable as e:
                    undone = ""
                    if pending is not None:  # the change this verdict was to judge is undone: what ships is what was judged
                        sm.rollback(pending.tx)
                        res.tier1_ok, res.tier15, res.scores, res.defects = pending.tier1_ok, pending.t15, pending.scores, pending.defects
                        undone = (f"rolled back the unjudged {pending.origin} change of round {rnd - 1} "
                                  f"({len(pending.ops)} op(s): {', '.join(pending.ops[:4])}; {pending.note[:80]})")
                    else:  # never judged: Tier-1 says what it found, the judge's scores are missing, not zero
                        res.tier1_ok, res.tier15, res.scores, res.defects = t1.ok, None, {}, {}
                    self.judge_unavailable("A", rnd, e, undone)
                    return
                tr = self.trace("A", rnd, t1, q, "preview")
                if pending is not None:
                    # Kept or undone on evidence (judge_change): a new Tier-1 critical issue or floating part, a newly
                    # flagged defect, a gate that now fails or a score down by more than the judge's noise band undo it;
                    # a cleared defect or a score up by the band keeps it; a change shown to do neither is undone too.
                    keep, evidence = self.judge_change(pending.t1, pending.q, t1, q)
                    if not keep:
                        sm.rollback(pending.tx)
                        moved = (f"tier15 {pending.t15:.2f}->{q.t15:.2f}" if tr.progress is None else
                                 f"tier15 {pending.t15:.2f}->{q.t15:.2f}, progress {pending.progress:.2f}->{q.progress:.2f}")
                        tr.rolled_back, tr.note = True, f"rolled back ({evidence}; {moved})"
                        res.tier1_ok, res.tier15, res.scores, res.defects = pending.tier1_ok, pending.t15, pending.scores, pending.defects
                        self.snapshot(pending.t1.ok, pending.q)
                        misses += 1
                        previous = f"{pending.summary}; it was undone because {evidence}"[:600]
                        if misses >= REPAIR_PATIENCE:
                            res.stopped["A"] = f"repair rolled back {misses} time(s) in a row; last: {evidence}"[:400]
                            res.converged["A"] = False
                            return
                        reuse, pending = (pending.t1, pending.q), None
                        continue
                    if q.t15 > pending.t15 + EPS:
                        self.fix_record(pending, q)
                    pending, previous, misses = None, "", 0
            self.snapshot(t1.ok, q)
            if self.passes(t1, q):
                res.converged["A"], res.stopped["A"] = True, f"converged in {rnd} round(s)"
                await self.improvement_rounds(rnd, t1, q)
                return
            if q.action.kind == "escalate_human":
                res.stopped["A"] = "escalate_human: " + "; ".join(q.action.reasons[:2])
                return
            if self.score_only(t1, q) and self.can_improve(q):  # a score short of its gate: not a fault to repair
                res.converged["A"] = False
                res.stopped["A"] = (f"round {rnd}: short only of score gate(s) ({', '.join(sorted(self.gates(t1, q)))}), "
                                    "no defect to repair: improvement rounds")
                await self.improvement_rounds(rnd, t1, q)
                return
            if q.action.kind == "escalate_director" and self.can_improve(q):
                # The judge is unsure (defects in its ambiguous band, none confirmed): the author is shown the views and every
                # answer in an improvement round on the plan's tier, where it can see what the judge half-saw. Not a director
                # repair: live 2026-11-22 the hydrant's went 107 operations, 7 % over budget, then truncated on the retry
                # (the director truncated 5 of 9 first designs that night).
                res.converged["A"] = False
                res.stopped["A"] = (f"round {rnd}: the judge is unsure ({'; '.join(q.action.reasons[:1])[:160]}): improvement "
                                    "rounds with the views instead of a director repair")
                await self.improvement_rounds(rnd, t1, q)
                return
            if rnd == self.max_rounds:
                res.stopped["A"] = "rounds exhausted"
                return
            if not self.governor.can_afford(self.governor.est(usd=QA_EST_USD, tokens=QA_TOKENS_EST)):
                res.stopped["A"] = "budget exhausted"
                return
            before = dict(n_crit=_crit(t1), t15=q.t15, tier1_ok=t1.ok, scores=dict(q.result.scores), defects=dict(q.result.defects),
                          progress=q.progress, t1=t1, q=q)
            located = await self.locate(t1, q)
            before_ir = sm.base_ir()
            candidate = reviewed_rules.seat_patch(before_ir, located.get("tier1:floating_part", []))
            if (candidate is not None and q.action.kind == "agent_repair"
                    and q.result.result.source == "decisions" and q.result.result.calibrated
                    and set(k for k, v in q.result.defects.items() if v >= th.ambiguity_lo) == {"defect_floating"}
                    and reviewed_rules.is_enabled()):
                patch, label, why = candidate, "reviewed_rule", reviewed_rules.RULE
            elif q.action.kind == "auto_fix" and q.action.fixer == "reground":
                patch, why = self.settle_patch(t1, {n for n in located.get("defect_floating", [])})
                label = "settle"
                if patch is None:
                    res.stopped["A"] = f"auto_fix reground: {why}"
                    return
            else:
                if q.action.kind == "escalate_director" and not self.governor.may_escalate_to_director(self.governor.est(usd=QA_EST_USD, tokens=QA_TOKENS_EST)):
                    res.stopped["A"] = "escalate_director not permitted by spend plan"
                    return
                patch, failed = await self.author_repair(q, located, t1, previous)
                label, why = "author", f"{q.action.kind}: {q.action.reasons[0] if q.action.reasons else ''}"
                if failed:
                    return  # res.stopped["A"] names the cause
                if patch is None:
                    res.stopped["A"] = "author declined to repair"
                    return
            try:
                tx = sm.apply_patch(patch)
            except Exception as e:  # noqa: BLE001 - invalid patch must not kill the run
                refusal = f"{_patch_summary(patch)}; it was refused before it could be judged: {type(e).__name__}: {e}"[:900]
                tx = None
                if label == "author":  # one correction with the exact error, as an improvement round gets
                    patch, failed = await self.author_repair(q, located, t1, refusal)
                    if failed:
                        return  # res.stopped["A"] names the cause
                    if patch is not None:
                        try:
                            tx = sm.apply_patch(patch)
                        except Exception as e2:  # noqa: BLE001
                            refusal = f"{_patch_summary(patch)}; refused again: {type(e2).__name__}: {e2}"[:900]
                if tx is None:
                    tr.note = f"patch rejected: {refusal}"[:400]
                    self.step("author_repair", "warned", f"patch rejected: {refusal}"[:400])
                    res.stopped["A"] = "patch rejected"
                    return
            tr.note = f"{label}: {len(patch.ops)} op(s)"
            from antigravity3d.tactics.metered_author import MeteredAuthor

            exact = candidate is not None and sm.base_ir() == apply_patch(before_ir, candidate)
            source = q.result.result.source if q.result.result.calibrated else "uncalibrated"
            origin = f"{source}/{label}"
            if label == "author" and not isinstance(self.author, MeteredAuthor):
                origin = f"{source}/{type(self.author).__name__}"
            pending = _Pending(tx, ops=[o.op for o in patch.ops], params=_patch_params(patch), note=why[:160],
                               rule=reviewed_rules.RULE if exact else "", scene_hash=reviewed_rules.scene_hash(before_ir),
                               origin=origin, summary=_patch_summary(patch), **before)
            if self.settle_first:  # the clean-up a first design gets, on what the repair changed (rolled back with it)
                from antigravity3d.dags.edit_asset import changed_nodes

                notes = tidy(sm, self.budget, changed_nodes(before_ir, sm.base_ir()))
                self.step(f"tidy_A{rnd}", "passed" if notes else "skipped", "; ".join(notes)[:400] or "nothing to clean up")
        # the last round's repair was rolled back and no round was left to ask again
        res.stopped.setdefault("A", f"rounds exhausted; the last repair was rolled back ({previous})"[:400])
        res.converged.setdefault("A", False)

    # ---- Pass B ---------------------------------------------------------------------------------------

    async def resolve_textures(self) -> dict[str, PBRMaps]:
        t = self.textures
        synth = lambda: synthesize_materials(  # noqa: E731 - the materials the scene has now, largest surfaces first
            self.sm.base_ir().model_copy(update={"materials": materials_by_area(self.sm.extract_meshes(), self.sm.base_ir().materials)}),
            self.forge, plane=self.plane, governor=self.governor, prompt=self.prompt or self.res.asset, uploader=self.upload,
            cache=self.cache, size=self.texture_px(), brief=texture_brief(self.art))
        speculative = False
        if t is None and self.forge is not None:
            t = await synth()
        elif inspect.isawaitable(t):
            t = await t
            speculative = t is not None
            if t is None and self.forge is not None:  # the speculative synthesis was skipped: synthesise here, as above
                t = await synth()
        self.untextured = {k: _why_untextured(v) for k, v in (t or {}).items()
                           if isinstance(v, PBRTournamentResult) and v.maps is None}
        if speculative:  # textures made for the geometry winner's materials while pass A ran: a material a repair or an
            # improvement round added since has none, and ships its flat colour; it is named, never left flat in silence
            for m in self.sm.base_ir().materials:
                if m.id not in t:
                    self.untextured[m.id] = ("added by a pass A repair or improvement round after its textures were started "
                                             "for the chosen design's materials")
        return {k: (v.maps if isinstance(v, PBRTournamentResult) else v) for k, v in (t or {}).items() if (v.maps if isinstance(v, PBRTournamentResult) else v) is not None}

    def write_textures(self) -> dict[str, dict[str, str]]:
        d = self.workdir / "textures"
        d.mkdir(parents=True, exist_ok=True)
        out: dict[str, dict[str, str]] = {}
        size = self.texture_px()
        for mid, m in self.maps.items():
            out[mid] = {}
            for kind, img in (("albedo", m.albedo), ("orm", m.orm), ("normal", m.normal)):
                if self.art is not None:  # the map as it ships: art direction's size and palette (see ArtDirection.finish_map)
                    img = self.art.finish_map(img, kind, size)
                (d / f"{mid}_{kind}.png").write_bytes(png_bytes(img))
                out[mid][kind] = f"textures/{mid}_{kind}.png"
        return out

    def tint(self) -> dict[str | None, tuple[float, float, float]]:
        t: dict[str | None, tuple[float, float, float]] = {m.id: m.base_color for m in self.sm.base_ir().materials}
        for mid, m in self.maps.items():
            t[mid] = tuple(float(x) for x in np.asarray(m.albedo.convert("RGB"), np.float32).reshape(-1, 3).mean(0) / 255.0)  # type: ignore[assignment]
        return t

    async def prepare_export_bundle(self) -> ExportBundle:
        revision = tuple(self.sm.tx_stack)
        if self.export_bundle is None or self.bundle_revision != revision:
            meshes, scene = self.sm.extract_meshes(), self.sm.scene_ir()
            if self.cache is None:
                self.export_bundle = await asyncio.to_thread(run_export_pipeline, meshes, self.budget, scene.asset_name,
                                                             opaque_materials=opaque_materials(scene))
            else:
                self.export_bundle, self.bundle_cache_hit = await asyncio.to_thread(
                    cached_export_pipeline, self.cache, scene, meshes, self.budget)
            self.bundle_revision = revision
        self.sm.ensure_export_uvs(self.export_bundle.uvs, crease_deg=self.crease)
        return self.export_bundle

    async def render_lookdev(self) -> tuple[bytes, str]:
        engine = self.governor.plan.final_render
        if self.require_verified and engine == "preview":
            engine = "eevee"
        if self.pool is None or engine == "preview":
            if engine != "preview":
                self.note_tier("no Blender pool provided")
        elif getattr(self.pool, "fake", False):
            self.note_tier("preview (pool is fake: no real Blender)")
        else:
            try:
                await self.prepare_export_bundle()
                with tempfile.TemporaryDirectory(prefix="lookdev-", dir=self.workdir) as td:
                    usd = Path(td) / "scene.usdc"
                    self.sm.export(usd)
                    r = await self.pool.call("render_usd", path=str(usd.resolve()), engine=engine,
                                             width=512, height=512, samples=16, transparent=True)
                if r.get("fake") or np.shape(r["image"]) != (512, 512, 4):
                    raise ValueError("Blender returned no usable DCC frame")
                png = blender_frame_png(r["image"])
                silhouette_iou(png, png)  # reject an empty, opaque or transparent frame before judging it
                self.res.render_tier = f"blender:{engine}"
                return png, self.res.render_tier
            except Exception as e:  # noqa: BLE001 - fall back to the preview rasteriser
                self.note_tier(f"preview (Blender render failed: {type(e).__name__}: {e})")
        self.res.render_tier = "preview"
        return png_bytes(preview_sheet(self.sm.extract_meshes(), self.tint())), "preview"

    def note_tier(self, why: str) -> None:
        if not any(s.name == "render_tier" for s in self.res.steps):
            self.step("render_tier", "skipped", f"{self.governor.plan.final_render} render not used: {why}")

    def apply_fixer(self, fixer: str, t1: Any) -> str:
        if fixer == "roughness_breakup":
            for mid, m in self.maps.items():
                self.maps[mid] = PBRMaps(edge_wear(m.albedo, m.normal, 0.35), roughness_breakup(m.orm, 0.6, seed=len(mid)), m.normal)
            return "roughness_breakup + edge_wear on all materials"
        if fixer == "delight":
            for mid, m in self.maps.items():
                self.maps[mid] = PBRMaps(delight(m.albedo), m.orm, m.normal)
            return "delight on all albedo maps"
        if fixer == "reground":
            patch, why = self.settle_patch(t1)
            if patch is not None:
                self.sm.apply_patch(patch)
            return why
        raise ValueError(f"unknown deterministic fixer {fixer!r}")

    async def pass_b(self) -> None:
        t0 = time.perf_counter()
        try:
            self.maps = await self.resolve_textures()
        except Exception as e:  # noqa: BLE001
            self.step("textures", "failed", f"{type(e).__name__}: {e}", t0)
            return
        missing = getattr(self, "untextured", {})
        why = "; ".join(f"{k}: {v}" for k, v in missing.items())
        if not self.maps and not self.res.engine_truth_required:
            # the true cause: nothing could be generated or supplied, or every material's synthesis came back empty
            self.step("textures", "skipped", f"no material was textured: {why}" if missing
                      else "no PBR textures available (no forge and none supplied)")
            self.res.stopped["B"] = "no textures"
            return
        scene = self.sm.base_ir()
        paths = self.write_textures()
        abs_dir = self.workdir.resolve()
        ops = [UpsertMaterial(material=m.model_copy(update={
            "albedo_uri": str(abs_dir / paths[m.id]["albedo"]), "orm_uri": str(abs_dir / paths[m.id]["orm"]),
            "normal_uri": str(abs_dir / paths[m.id]["normal"]), "base_color": self.tint()[m.id]})) for m in scene.materials if m.id in paths]
        if ops:
            self.sm.apply_patch(IRPatch(ops=ops))
        self.step("textures", "passed", f"{len(paths)} material(s) textured", t0)
        if missing:  # a material without a texture ships its flat colour: say which and why
            self.step("textures_missing", "warned", f"{len(missing)} material(s) left untextured (flat colour): {why}"[:400])
        pending: tuple[dict[str, PBRMaps], float, str, dict[str, float], list[str]] | None = None
        for rnd in range(1, min(self.max_rounds, LOOKDEV_ROUNDS) + 1):
            png, tier = await self.render_lookdev()
            judged_png, self.render_png = self.render_png, png
            t1 = tier1_qa(self.sm.extract_meshes(), self.budget)
            try:
                q = await self.qa("lookdev", png)
            except JudgeUnavailable as e:
                undone = ""
                if pending is not None:  # the fixer this verdict was to judge is undone: the maps that ship were judged
                    old, _, fx, _, _ = pending
                    self.maps = old
                    self.write_textures()
                    self.render_png = judged_png  # the render of the maps that ship
                    undone = f"rolled back the unjudged deterministic fixer {fx} of round {rnd - 1}"
                self.judge_unavailable("B", rnd, e, undone)
                break
            tr = self.trace("B", rnd, t1, q, tier)
            if pending is not None:
                old, old_t15, fx, defects, params = pending
                if q.t15 < old_t15 - EPS:
                    self.maps = old
                    self.write_textures()
                    tr.rolled_back, tr.note = True, f"fixer {fx} regressed tier15 {old_t15:.2f}->{q.t15:.2f}; reverted"
                    self.res.stopped["B"] = "fixer regressed; reverted"
                    return
                if q.t15 > old_t15 + EPS:
                    self.fix_record(_Pending("", 0, old_t15, True, {}, defects, [fx], params, f"deterministic {fx}"), q)
                pending = None
            self.snapshot(t1.ok, q)
            if q.action.kind == "approve":
                self.res.live_lookdev = tier.startswith("blender:") and q.result.result.calibrated and q.result.result.source == "decisions"
                self.res.converged["B"], self.res.stopped["B"] = True, f"approved in {rnd} round(s)"
                break
            if q.action.kind != "auto_fix" or not q.action.fixer:
                self.res.stopped["B"] = f"{q.action.kind} needs an agent/human: " + "; ".join(q.action.reasons[:2])
                break
            if rnd == min(self.max_rounds, LOOKDEV_ROUNDS):
                self.res.stopped["B"] = "rounds exhausted"
                break
            old = dict(self.maps)
            note = self.apply_fixer(q.action.fixer, t1)  # deterministic: zero model calls
            self.write_textures()
            tr.note = f"auto_fix {q.action.fixer}: {note}"
            pending = (old, q.t15, q.action.fixer, dict(q.result.defects), [f"{m}.{'orm' if q.action.fixer == 'roughness_breakup' else 'albedo'}" for m in self.maps])
        await self.paint_over()

    async def paint_over(self) -> None:
        plan = self.governor.plan
        if not plan.paintover_loop:
            self.step("paintover", "skipped", f"spend plan {plan.label!r} has paintover_loop disabled")
        elif self.forge is None or self.render_png is None:
            self.step("paintover", "skipped", "needs an image forge and a lookdev render")
        else:
            t0 = time.perf_counter()
            direction = (f"push toward this art direction: {texture_brief(self.art)}" if texture_brief(self.art)
                         else "push realism: weathering, edge wear and colour variation")
            g = await self.forge.with_governor(self.governor).paint_over(self.render_png, direction)
            self.res.cost_usd += g.receipt.booked_usd
            self.res.artifacts["paintover_uri"] = await self.upload(g.png_bytes)
            self.step("paintover", "passed", "reference paint-over generated (a target for review; not baked into textures)", t0)

    # ---- Pass C ---------------------------------------------------------------------------------------

    async def guarded_result(self, fn: Callable[[], Awaitable[tuple[str, str]]]) -> tuple[str, str, float]:
        """Run a sub-step and return (status, detail, start time) without recording it (for overlapping steps)."""
        t0 = time.perf_counter()
        try:
            status, detail = await fn()
        except Exception as e:  # noqa: BLE001 - a failed sub-step is a result, not a crash
            status, detail = "failed", f"{type(e).__name__}: {e}"
        return status, detail, t0

    async def _timed(self, fn: Callable[[], Awaitable[tuple[str, str]]]) -> tuple[str, str, float]:
        """A sub-step run as a task: (status, detail, its own milliseconds), recorded by the caller where it belongs."""
        status, detail, t0 = await self.guarded_result(fn)
        return status, detail, (time.perf_counter() - t0) * 1e3

    async def await_verdict(self) -> None:
        """A verdict that was still being asked when pass C started (an edit: ``refine(judged=<awaitable>)``) is adopted
        before anything that carries it (the package's QA record) is written."""
        pending, self.pending_verdict = getattr(self, "pending_verdict", None), None
        if pending is None:
            return
        judged = await pending
        self.adopt(judged, self.res.stopped.get("A", ""))
        self.res.brief_unmet = brief_reasons(self.res.defects, self.res.scores, self.th)

    async def guarded(self, name: str, fn: Callable[[], Awaitable[tuple[str, str]]]) -> None:
        self.step(name, *await self.guarded_result(fn))

    async def pass_c(self) -> None:
        sm, budget, res = self.sm, self.budget, self.res
        meshes = sm.extract_meshes()
        scene = sm.base_ir()
        pkg = self.workdir / "package"
        bundle: ExportBundle | None = None
        res.stopped.setdefault("C", "verification run")

        async def export() -> tuple[str, str]:
            nonlocal bundle
            bundle = await self.prepare_export_bundle()
            res.objects["bundle"] = bundle
            levels = [sum(shipped(v[k]).tri_count for v in bundle.lods.values()) for k in range(min((len(v) for v in bundle.lods.values()), default=0))]
            bad = [u.node_id for u in bundle.uvs if not u.within(budget)]
            desc = f"uv={len(bundle.uvs)} lod_tris={levels} hulls={len(bundle.colliders)}"
            density = texel_density_report(bundle.uvs, budget.texel_density_px_per_m)
            if density["materials"]:
                desc += f" texel_density: {describe_texel_density(density)}"
            if any(h.source == "body_hull" or CAPPED_MARK in h.reason for h in bundle.colliders):  # a fallback or cap is never silent
                desc += f" ({describe_collider_sources(bundle.colliders)})"
            if any(b >= a for a, b in zip(levels, levels[1:])):
                return "failed", f"LOD triangle counts do not strictly descend: {levels}"
            if bad:
                worst = max(u.stretch_mean for u in bundle.uvs)
                return "failed", f"UV stretch {worst:.3f} above budget {budget.max_uv_stretch} on {bad}; {desc}"
            return "passed", desc

        await self.guarded("uv_lod_colliders", export)
        if self.bundle_cache_hit is not None:  # the same bundle either way: where it came from is not part of the package
            res.steps[-1].run = f"cache={'hit' if self.bundle_cache_hit else 'miss'}"
        if bundle is None:
            for n in ("usd_package", "usd_validate", "engine_compliance", "lod_pop", "lod_pop_vision", "export_glb", "engine_import", "engine_parity", "web_runtime", "blender_asset_qa", "export_fbx"):
                self.step(n, "skipped", "UV/LOD/collider stage failed")
            return

        if self.maps:
            self.write_textures()
        usd_path = pkg / f"{scene.asset_name}.usd"

        # Coplanar overlap between parts is a Tier-1 warning (z_fighting: same-facing faces in one plane) or, in the
        # package, an engine warning (double_sided_contact: back-to-back faces that a double-sided material draws):
        # neither blocks the export, both are visible defects, so the run report and the manifest carry them.
        t1_final = tier1_qa(meshes, budget).issues
        zfight = [f"{i.node_id}: {i.detail}" for i in t1_final if i.code == "z_fighting"]
        # the other Tier-1 warnings (an open mesh on an engine that draws them, a part that floats or is enclosed, ...)
        t1_warnings = [f"{i.code}@{i.node_id}: {i.detail}" if i.node_id else f"{i.code}: {i.detail}"
                       for i in t1_final if i.severity == "warning" and i.code != "z_fighting"]

        async def usd() -> tuple[str, str]:
            m = await asyncio.to_thread(write_engine_package, sm, bundle, budget, pkg, asset_name=scene.asset_name,
                                        qa={"tier15": res.tier15, "tier1_ok": res.tier1_ok, "defects": res.defects, "audit": res.audit,
                                            "z_fighting": zfight, "tier1_warnings": t1_warnings},
                                        art=self.art)
            res.objects["package"] = m
            sided = [i.detail for i in m.issues if i.code.endswith(":double_sided_contact")]
            if zfight or sided:
                self.step("z_fighting", "warned", "; ".join((zfight + sided)[:3])[:400])
            res.artifacts.update(usd=str(usd_path), manifest=str(pkg / "manifest.json"))
            stage = Usd.Stage.Open(str(usd_path))
            self.step("engine_compliance", *self.compliance(stage, meshes))
            crit = [i for i in m.issues if i.severity == "critical"]
            return ("failed", f"{len(crit)} critical: {crit[0].code} {crit[0].detail[:80]}") if crit else ("passed", f"write_engine_package ok; {len(m.issues)} non-critical message(s); files={len(m.files)}")

        async def pop() -> tuple[str, str]:
            lods = merged_lods(bundle)
            if self.crease != CREASE_DEG:  # judge the levels with the normals they ship with (lod_diff.shipped_normals)
                for lod in lods:
                    lod.normals = crease_normals(np.asarray(lod.points, np.float64), np.asarray(lod.triangles), self.crease)
            if len(lods) < 2:
                return "skipped", "LOD0 only: no coarser level within the screen-space error budget saves enough triangles"
            rep = await asyncio.to_thread(lod_pop_report, lods)
            res.objects["lod_pop"] = rep
            return ("passed" if rep.ok(LOD_POP_THRESHOLDS) else "failed"), rep.one_line(LOD_POP_THRESHOLDS)

        async def pop_vision() -> tuple[str, str]:
            fn = getattr(self.plane, "evaluate_lod_pop", None)
            rep = res.objects.get("lod_pop")
            if fn is None:
                return "skipped", "plane.evaluate_lod_pop unavailable"
            if rep is None or not rep.transitions:
                return "skipped", "no LOD pop sheet to judge"
            r = await fn(await self.upload(png_bytes(rep.pair_sheet())), len(rep.transitions), governor=self.governor)
            self.res.cost_usd += r.result.booked_usd
            reasons = lod_pop_reasons(r, self.th)
            judged = "" if r.result.calibrated else " (judged by the generative fallback, uncalibrated: not verification)"
            return ("passed", "all LOD pairs imperceptible" + judged) if lod_pop_ok(r, self.th) else ("failed", "; ".join(reasons[:3]))

        # The LOD pop check needs only the levels (the bundle), not the package files or a verdict: it is measured first
        # and its vision question (a judge call, tens of seconds) runs while the package is written, the verdict an edit
        # is waiting for arrives (``refine(judged=<awaitable>)``) and the engine checks run. Its steps are recorded where
        # they always were, after the package's.
        pop_res = await self.guarded_result(pop)
        vision = asyncio.ensure_future(self._timed(pop_vision))
        try:
            await self.await_verdict()
            await self.guarded("usd_validate", usd)
        except BaseException:
            vision.cancel()
            raise
        if not any(x.name == "engine_compliance" for x in res.steps):
            self.step("engine_compliance", "skipped", "package USD was not written")
        self.step("lod_pop", *pop_res)
        vision_at = len(res.steps)

        async def glb() -> tuple[str, str]:
            p = pkg / f"{scene.asset_name}.glb"
            if not p.exists():
                return "failed", "write_engine_package produced no GLB"
            res.artifacts["glb"] = str(p)
            return "passed", f"{p.name} {p.stat().st_size} bytes (+{len(list(pkg.glob('*_LOD*.glb')))} LOD GLB)"

        await self.guarded("export_glb", glb)

        async def web_runtime() -> tuple[str, str]:
            """Load the package as shipped (GLB, LOD GLBs, manifest, USD) in three.js under headless Chromium."""
            if "glb" not in res.artifacts:
                return "skipped", "no GLB exported"
            with tempfile.TemporaryDirectory(prefix="web_three_", dir=self.workdir) as td:
                rep = await WebThreeValidator().validate(pkg, out_dir=Path(td))
                if rep.skipped:
                    return "skipped", rep.skipped_reason or "web runtime verifier unavailable"
                if rep.screenshot:  # evidence for review; kept beside the package, not inside it
                    shot = self.workdir / "web_three.png"
                    shutil.copyfile(rep.screenshot, shot)
                    res.artifacts["web_render"] = str(shot)
            res.objects["web_runtime"] = {"checks": [(c.name, c.status, c.detail) for c in rep.checks],
                                          "silhouette_iou": rep.silhouette_iou, "timings_s": rep.timings_s}
            if rep.silhouette_iou is not None:
                res.scores["web_silhouette_iou"] = rep.silhouette_iou
            if not rep.ok:
                why = "; ".join(f"{c.name}: {c.detail}" for c in rep.failures[:3]) or rep.log_tail[-200:].replace("\n", " ")
                return "failed", why[:400]
            done = sum(c.status == "pass" for c in rep.checks)
            iou = "" if rep.silhouette_iou is None else f", silhouette IoU {rep.silhouette_iou:.4f} against the USD"
            return "passed", f"three.js loaded and rendered the package: {done} checks passed{iou}"

        async def blender_asset_qa() -> tuple[str, str]:
            """EL4CTEO/blender-skills' qa_check on the GLB the engines import, vertices merged part by part (upstream_checks.asset_qa)."""
            if "glb" not in res.artifacts:
                return "skipped", "no GLB exported"
            status, detail, data = await upstream_checks.asset_qa(self.pool, res.artifacts["glb"], budget.target_engine)
            if data:
                res.objects["blender_asset_qa"] = data
            return status, detail

        # Both are independent of the target engine's own import, so they run while those steps wait on their engine.
        side = {"web_runtime": asyncio.ensure_future(self.guarded_result(web_runtime)),
                "blender_asset_qa": asyncio.ensure_future(self.guarded_result(blender_asset_qa))}
        try:
            await self._engine_steps(validator_for(budget.target_engine))
        except BaseException:
            for task in (*side.values(), vision):
                task.cancel()  # pass C was cancelled or crashed: stop the verification with it, and let it clean up
            await asyncio.wait({*side.values(), vision})
            raise
        for name, task in side.items():
            status, detail, t0 = await task
            self.step(name, status, detail, t0)
        status, detail, ms = await vision
        res.steps.insert(vision_at, StepResult("lod_pop_vision", status, detail, ms))

        async def export_fbx() -> tuple[str, str]:
            """The package's FBX (fbx_package.export_package_fbx), re-imported in Blender and compared with its GLB files.

            It writes into the package folder, so it runs LAST: ``_engine_steps`` has returned (engine_capture writes
            engine.png there) and both side tasks above have been awaited (web_runtime and blender_asset_qa read the
            folder), and this coroutine starts nothing that outlives it. Nothing else writes to the folder while it
            runs; the package node of the DAG writes manifest.json afterwards and records the FBX in it (res.objects)."""
            if "glb" not in res.artifacts or "package" not in res.objects:
                return "skipped", "no package exported"
            if not any(s.name == "usd_validate" and s.status == "passed" for s in res.steps):
                return "skipped", "the package did not pass its own validation"
            status, detail, result = await fbx_package.export_package_fbx(self.pool, pkg, res.objects["package"].data)
            if status == "failed":
                res.objects["fbx_failed"] = result  # the manifest says it: no FBX ships, and why
            elif result:
                res.objects["fbx"] = result
            return status, detail

        status, detail, t0 = await self.guarded_result(export_fbx)
        # An output that could not be made is "output_failed", not "failed": it does not block (see StepResult).
        self.step("export_fbx", "output_failed" if status == "failed" else status, detail, t0)

    async def _engine_steps(self, validator: Any) -> None:
        """engine_import, engine_capture and engine_parity on the exported package (pass C, after the export)."""
        budget, res = self.budget, self.res
        pkg = self.workdir / "package"

        async def engine() -> tuple[str, str]:
            if "glb" not in res.artifacts:
                return "skipped", "no GLB exported"
            rep = await validator.validate(res.artifacts["glb"])
            if rep.ok:
                if issue := rep.geometry_issue(res.objects["package"].data):
                    return "failed", issue
                res.objects["engine_import"] = {"engine": rep.engine, "metrics": rep.metrics, "bounds_m": rep.bounds_m,
                                                "engine_version": rep.engine_version}
            if rep.skipped:
                return "skipped", rep.skipped_reason or "engine validator skipped"
            return ("passed" if rep.ok else "failed"), rep.log_tail[-160:].replace("\n", " ")

        await self.guarded("engine_import", engine)

        async def capture() -> tuple[str, str]:
            if not res.engine_truth_required:
                return "skipped", "engine truth disabled in spend plan"
            if not any(s.name == "engine_import" and s.status == "passed" for s in res.steps):
                return "skipped", "no passing import of the exported GLB"
            png = pkg / "engine.png"
            rep = await validator.capture(res.artifacts["glb"], png)
            if rep.skipped:
                return "skipped", rep.skipped_reason or "capture unavailable"
            if not rep.ok:
                return "failed", rep.log_tail[-300:]
            res.artifacts["engine_render"] = str(png)
            self.engine_render_uri = await self.upload(png.read_bytes())
            if self.render_png and res.render_tier.startswith("blender:"):
                dcc = pkg / "dcc.png"
                dcc.write_bytes(self.render_png)
                res.artifacts["dcc_render"] = str(dcc)
            return "passed", f"captured {budget.target_engine} at 512x512"

        await self.guarded("engine_capture", capture)

        async def parity() -> tuple[str, str]:
            if not res.engine_truth_required:
                return "skipped", f"spend plan {self.governor.plan.label!r} has engine_truth disabled"
            fn = getattr(self.plane, "evaluate_engine_parity", None)
            if fn is None:
                return "skipped", "plane.evaluate_engine_parity unavailable"
            if self.engine_render_uri is None:
                return "skipped", "no engine render available"
            if self.render_png is None or not res.render_tier.startswith("blender:"):
                return "skipped", "parity requires the material-bearing DCC render"
            if "engine_render" in res.artifacts:
                iou = silhouette_iou(self.render_png, Path(res.artifacts["engine_render"]).read_bytes())
                res.scores["engine_silhouette_iou"] = iou
                if iou < MIN_SILHOUETTE_IOU:
                    return "failed", f"silhouette IoU {iou:.4f} below {MIN_SILHOUETTE_IOU:.2f}"
            from antigravity3d.decisions.compose import engine_parity_ok, engine_parity_reasons

            r = await fn(await self.upload(self.render_png), self.engine_render_uri, governor=self.governor)
            self.res.cost_usd += r.result.booked_usd
            if not engine_parity_ok(r, self.th):
                return "failed", "; ".join(engine_parity_reasons(r, self.th)[:3])
            res.live_parity = bool(r.result.calibrated and r.result.source == "decisions" and "engine_render" in res.artifacts)
            return "passed", "engine matches DCC" + ("" if r.result.calibrated else " (judged by the generative fallback, uncalibrated: not verification)")

        await self.guarded("engine_parity", parity)

    def compliance(self, stage: Usd.Stage, meshes: list[Any]) -> tuple[str, str]:
        issues = check_engine_compliance(stage, meshes, self.budget)
        crit = [i for i in issues if i.severity == "critical"]
        warn = len(issues) - len(crit)
        return ("failed", f"{len(crit)} critical: " + "; ".join(f"{i.code}@{i.node_id}" for i in crit[:3])) if crit else ("passed", f"{warn} warning(s)")


async def refine(
    stage_manager: Any, *, plane: Any, author: Any, governor: Governor, budget: EngineBudget, thresholds: Thresholds | None = None,
    ortho_uri: str | None = None, style_uri: str | None = None, max_rounds: int = 4, forge: Any = None, uploader: Uploader | None = None,
    pool: Any = None, cache: Any = None, workdir: Path | str | None = None, fixlog: FixLog | None = None, asset_id: str | None = None,
    prompt: str = "", textures: Any = None, settle_first: bool = True, engine_render_uri: str | None = None,
    run_pass_c: bool = True, run_pass_b: bool = True, require_verified: bool = False, art: Any = None,
    judged: JudgedDesign | None = None, run_pass_a: bool = True, pass_a_note: str = "",
) -> PyramidResult:
    """Run passes A (silhouette lock), B (lookdev) and C (final verification) on the stage's current scene.

    ``textures``: ``{material_id: PBRMaps | PBRTournamentResult}`` or an awaitable/task of it (speculative synthesis
    started earlier); with None and a ``forge`` the textures are synthesised here.
    ``art``: the art direction (``antigravity3d.art_direction.ArtDirection``) or None. It sets the size and palette of the
    written maps, the shading crease (export normals, the package and the LOD pop check), the texture sampler of the
    package GLBs and the texture/paint-over brief. ``budget`` is expected to be its effective budget already.
    ``judged``: the starting design's verdict when one was already made (the geometry ranking's of its winner); pass A's
    first visual QA uses it instead of asking again, unless the deterministic pre-pass changed the design.
    ``run_pass_a=False`` (an edit of a shipped asset, ``dags.edit_asset``): the stage's design is taken as it is: no
    settling, separation, repair or improvement round touches it. ``judged`` is then its verdict (the edit's own
    blockout review of exactly this design), recorded as pass A's one round; ``pass_a_note`` is pass A's stop reason.
    """
    run = _Run(
        stage_manager, plane=plane, author=author, governor=governor, budget=budget, th=thresholds or Thresholds(), ortho_uri=ortho_uri,
        style_uri=style_uri, max_rounds=max_rounds, forge=forge, upload=uploader or uploader_for(forge), pool=pool, cache=cache,
        workdir=Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="agy3d-pyramid-")), fixlog=fixlog, asset_id=asset_id,
        prompt=prompt, textures=textures, settle_first=settle_first, engine_render_uri=engine_render_uri, require_verified=require_verified,
        art=art, judged=judged)
    run.workdir.mkdir(parents=True, exist_ok=True)
    # Pass C ends with the web-runtime check: its browser starts now, while passes A and B run, and is closed again
    # if this run fails or is cancelled before it is used.
    browser = web_three.prestart() if run_pass_c else None
    ok = False
    try:
        if run_pass_a:
            await run.pass_a()
        elif inspect.isawaitable(judged):  # still being asked: adopted in pass C before the package is written
            run.pending_verdict = judged
            run.res.stopped["A"] = pass_a_note or "pass A not run: the design was taken as given"
        else:
            run.adopt(judged, pass_a_note or "pass A not run: the design was taken as given")
        if run_pass_b and run.res.stopped.get("A") != "empty scene":
            await run.pass_b()
        # The export decision (PyramidResult.ok) adds the brief's requirements on the state that ships: its last verdict
        # (passes A and B leave res.defects / res.scores as judged, rolled-back changes included; pass C adds no answer).
        run.res.brief_unmet = brief_reasons(run.res.defects, run.res.scores, run.th)
        if run_pass_c and run.res.stopped.get("A") != "empty scene":
            await run.pass_c()
        ok = run.res.ok
    finally:
        if browser is not None:
            await browser.aclose(keep=ok)
    run.res.objects["maps"] = run.maps
    return run.res
