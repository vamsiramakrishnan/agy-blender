"""Multi-resolution refinement: coarse silhouette lock (A) -> lookdev (B) -> final verification (C).

Everything is deterministic orchestration around two injected, non-deterministic parts: the ``IRAuthor``
(repairs) and the image forge (textures). Every step is recorded as passed / failed / skipped-with-reason;
nothing that did not run is ever reported as passed.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from pxr import Usd

from antigravity3d.adapters.engines.validators import MIN_SILHOUETTE_IOU, blender_frame_png, silhouette_iou, validator_for
from antigravity3d.adapters.engines import web_three
from antigravity3d.adapters.engines.web_three import WebThreeValidator
from antigravity3d.decisions.compose import Action, Thresholds, decide_action, lod_pop_ok, lod_pop_reasons, tier15_score
from antigravity3d.decisions.models import VisualQAResult
from antigravity3d.decisions.questions import MARKED_DEFECT_PHRASES
from antigravity3d.genmedia.pbr_deriver import PBRMaps, delight, edge_wear, roughness_breakup
from antigravity3d.ir import (
    AssignMaterial, IRPatch, MaterialIR, RemoveNode, SceneIR, SetParam, SetTransform, UpsertMaterial, UpsertNode, apply_patch,
)
from antigravity3d.art_direction import texture_brief
from antigravity3d.kernel.compile_ir import CREASE_DEG, crease_normals
from antigravity3d.kernel.fast_ops import CAPPED_MARK, ExportBundle, describe_collider_sources, run_export_pipeline, tier1_qa
from antigravity3d.kernel.lod_diff import PopThresholds, lod_pop_report
from antigravity3d.kernel.raster import CLAY, render_buffers
from antigravity3d.kernel.settle import settle
from antigravity3d.kernel.som import marked_contact_sheet
from antigravity3d.kernel.validator import check_engine_compliance, validate_usd
from antigravity3d.state import EXPORT_MIN_TIER15_SCORE, EngineBudget
from antigravity3d.tactics.cached import cached_export_pipeline
from antigravity3d.tactics.common import IMAGE_USD, Uploader, png_bytes, uploader_for
from antigravity3d.tactics.distill import FixLog, FixRecord
from antigravity3d.tactics.governor import BudgetExceeded, Governor
from antigravity3d.tactics import reviewed_rules
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.tactics.package import merged_lods
from antigravity3d.tactics.tournament import GRADE_USD_EST, PBRTournamentResult, pbr_tournament

EPS = 1e-9
LOOKDEV_ROUNDS = 3
QA_EST_USD = 0.002  # conservative cost of one visual-QA (+ locate) round when checking the ceiling


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


@dataclass
class StepResult:
    name: str
    status: str  # "passed" | "warned" | "failed" | "skipped"
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
    tier15: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    defects: dict[str, float] = field(default_factory=dict)
    needs_review: bool = False
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> path / uri
    objects: dict[str, Any] = field(default_factory=dict)  # live objects (bundle, meshes, maps); not serialised
    author_calls: int = 0

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
    def warned(self) -> list[tuple[str, str]]:
        return [(s.name, s.detail) for s in self.steps if s.status == "warned"]

    @property
    def ok(self) -> bool:
        return (not self.failed and self.tier1_ok and self.tier15 >= EXPORT_MIN_TIER15_SCORE and not self.needs_review
                and (not self.engine_truth_required or self.live_lookdev and self.live_parity))

    def summary(self) -> str:
        r = "; ".join(f"{t.stage}{t.n}:{t.action}{'(rolled back)' if t.rolled_back else ''} t15={t.tier15:.2f}" for t in self.rounds)
        st = ", ".join(f"{s.name}={s.status}" for s in self.steps)
        return f"pyramid[{self.asset}] ok={self.ok} tier={self.render_tier} cost=${self.cost_usd:.4f} | {r} | {st}"

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in ("asset", "stopped", "converged", "render_tier", "cost_usd", "tier1_ok", "tier15", "scores",
                                           "defects", "needs_review", "artifacts", "author_calls", "audit", "ok", "verified", "live_lookdev", "live_parity", "engine_truth_required")}
        d["rounds"] = [vars(r) for r in self.rounds]
        d["steps"] = [vars(s) for s in self.steps]
        d["fix_records"] = [vars(f) for f in self.fix_records]
        d["skipped"] = [{"step": n, "reason": r} for n, r in self.skipped]
        return d


# --------------------------------------------------------------------------- material synthesis (speculative PBR)


async def synthesize_materials(
    scene: SceneIR, forge: Any, *, plane: Any, governor: Governor, prompt: str, uploader: Uploader | None = None,
    cache: Any = None, size: int | None = None, concurrency: int | None = None, brief: str = "",
) -> dict[str, PBRTournamentResult]:
    """One ``pbr_tournament`` per material, concurrently; N is sized so all materials together fit the budget.

    ``brief``: an art direction's texture brief, appended to every material prompt ('' appends nothing)."""
    size = size or default_texture_px(forge)
    k = max(1, len(scene.materials))
    n = governor.candidates("pbr", unit_cost=k * (IMAGE_USD["albedo"] + GRADE_USD_EST))
    outs = await asyncio.gather(*(pbr_tournament(
        f"{prompt}: {m.id} surface" + (f". {brief}" if brief else ""), forge, plane=plane, governor=governor, material_id=m.id, metallic_hint=m.metallic,
        uploader=uploader, n=n, size=size, cache=cache, concurrency=concurrency) for m in scene.materials))
    return {m.id: o for m, o in zip(scene.materials, outs)}


def default_texture_px(forge: Any) -> int:
    """Side of generated PBR maps when no art direction sets one: small on the offline fakes, 2K live."""
    return 256 if forge.offline else 2048


# --------------------------------------------------------------------------- helpers


def _crit(t1: Any) -> int:
    return sum(i.severity == "critical" for i in t1.issues)


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


@dataclass
class _QA:
    result: VisualQAResult
    action: Action
    t15: float
    uri: str
    cost: float
    legend: dict[int, str] | None = None


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


class _Run:
    def __init__(self, sm: Any, **kw: Any) -> None:
        self.sm = sm
        self.__dict__.update(kw)
        self.res = PyramidResult(asset=sm.asset_prim.GetName(), engine_truth_required=self.require_verified or self.governor.plan.engine_truth)
        self.maps: dict[str, PBRMaps] = {}
        self.render_png: bytes | None = None
        self.locate_noted = False
        self.bundle_revision: tuple[str, ...] | None = None
        self.export_bundle: ExportBundle | None = None
        self.bundle_cache_hit: bool | None = None
        self.crease = self.art.crease_deg() if self.art is not None else CREASE_DEG

    def texture_px(self) -> int | None:
        """The art direction's map size for this engine (None: the pipeline's default)."""
        if self.art is None or self.art.texture_px_prop is None or self.forge is None:
            return None
        return self.art.texture_px(self.budget.target_engine, "prop", default_texture_px(self.forge))

    # ---- bookkeeping --------------------------------------------------------------------------------

    def step(self, name: str, status: str, detail: str = "", t0: float | None = None) -> None:
        self.res.steps.append(StepResult(name, status, detail, 0.0 if t0 is None else (time.perf_counter() - t0) * 1e3))

    def snapshot(self, t1_ok: bool, q: _QA) -> None:
        r = self.res
        r.tier1_ok, r.tier15, r.scores, r.defects = t1_ok, q.t15, dict(q.result.scores), dict(q.result.defects)
        r.needs_review = q.action.kind == "escalate_human" or not getattr(self.plane, "calibrated", True)

    async def qa(self, stage: str, png: bytes, legend: dict[int, str] | None = None) -> _QA:
        uri = await self.upload(png)
        r = await self.plane.evaluate_visual_qa(uri, self.ortho_uri, self.style_uri, stage, marked_legend=legend, governor=self.governor)
        action = decide_action(r, self.th, stage)
        cost = r.result.booked_usd
        self.res.cost_usd += cost
        return _QA(r, action, tier15_score(r, self.th), uri, cost, legend)

    def trace(self, stage: str, n: int, t1: Any, q: _QA, tier: str) -> RoundTrace:
        t = RoundTrace(stage, n, t1.one_line() if t1 else "n/a", bool(t1.ok) if t1 else True, q.t15, q.result.scores.get("score_silhouette"),
                       q.result.defects.get("defect_floating", 0.0), q.action.kind, q.action.fixer, q.action.reasons[:4], q.cost, tier)
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
        movable = (floating & ids) or (ids if any(i.code == "ungrounded" for i in t1.issues) or extra == set() else set())
        if not movable:
            return None, "nothing to settle"
        try:
            sr = settle(meshes, movable)
            ops = sr.to_ir_ops(self.sm.base_ir())
        except (ValueError, KeyError) as e:
            return None, f"settle unsupported: {e}"
        return (IRPatch(ops=ops), f"settled {len(ops)} node(s)") if ops else (None, "already settled")

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
            for d, lst in (await fn(q.uri, q.legend, flagged, governor=self.governor, on_settle=charges.append)).items():
                located[d] = [n for n, _ in lst]
        except Exception as e:  # noqa: BLE001 - localisation is best-effort
            self.step("locate_defects", "failed", f"{type(e).__name__}: {e}")
        self.res.cost_usd += sum(charges)
        return located

    async def author_repair(self, q: _QA, located: dict[str, list[str]]) -> tuple[Any, bool]:
        """One author repair call: ``(patch, failed)``. A failed call is a recorded result, not a crash.

        A truncated repair (or one that ended STOP with an empty answer) on a tier below the director is retried once on
        the director tier when the plan allows it (a larger budget the plan already grants, not the same request again).
        Every other typed failure (blocked, invalid, timed out, not admitted) ends the pass with a stop reason naming the
        cause. Any other exception type is a bug and propagates.
        """
        from antigravity3d.tactics.metered_author import AuthorNoAnswer, AuthorOutcomeError, AuthorTruncated, MeteredAuthor

        res = self.res
        sm = self.sm

        def fail(reason: str) -> tuple[None, bool]:
            res.stopped["A"] = f"author repair failed: {reason}"
            res.converged["A"] = False
            self.step("author_repair", "warned", reason)
            return None, True

        res.author_calls += 1
        try:
            return await self.author.repair(sm.base_ir(), action=q.action, located=located, manifest=sm.manifest()), False
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
                patch = await self.author.repair(sm.base_ir(), action=q.action, located=located, manifest=sm.manifest(), role="director")
                self.step("author_repair", "passed", f"{kind} on {first.role} tier; succeeded on the director tier")
                return patch, False
            except BudgetExceeded as e:
                return fail(f"{kind} on {first.role} tier; director retry not admitted: {e}")
            except AuthorOutcomeError as second:
                return fail(f"{kind} on {first.role} tier; director retry also {type(second).__name__} ({second.finish_reason}): {second}")

    async def pass_a(self) -> None:
        sm, th, res = self.sm, self.th, self.res
        if not sm.extract_meshes():
            res.stopped["A"] = "empty scene"
            self.step("pass_a", "failed", "scene has no meshes")
            return
        pending: _Pending | None = None
        t0 = time.perf_counter()
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
        for rnd in range(1, self.max_rounds + 1):
            meshes = sm.extract_meshes()
            t1 = tier1_qa(meshes, self.budget)
            sheet = marked_contact_sheet(meshes, size=256)
            q = await self.qa("blockout", png_bytes(sheet.image), sheet.legend)
            tr = self.trace("A", rnd, t1, q, "preview")
            if pending is not None:
                if _crit(t1) > pending.n_crit or q.t15 < pending.t15 - EPS:
                    sm.rollback(pending.tx)
                    tr.rolled_back, tr.note = True, f"regressed (tier1 crit {pending.n_crit}->{_crit(t1)}, tier15 {pending.t15:.2f}->{q.t15:.2f}); rolled back"
                    res.tier1_ok, res.tier15, res.scores, res.defects = pending.tier1_ok, pending.t15, pending.scores, pending.defects
                    res.stopped["A"] = "repair regressed; rolled back"
                    res.converged["A"] = False
                    return
                if q.t15 > pending.t15 + EPS:
                    self.fix_record(pending, q)
                pending = None
            self.snapshot(t1.ok, q)
            sil, fp = q.result.scores.get("score_silhouette"), q.result.defects.get("defect_floating", 0.0)
            if t1.ok and (sil is None or sil >= th.silhouette_lock) and fp <= th.floating_max and q.action.kind != "escalate_human":
                res.converged["A"], res.stopped["A"] = True, f"converged in {rnd} round(s)"
                return
            if q.action.kind == "escalate_human":
                res.stopped["A"] = "escalate_human: " + "; ".join(q.action.reasons[:2])
                return
            if rnd == self.max_rounds:
                res.stopped["A"] = "rounds exhausted"
                return
            if not self.governor.can_afford(QA_EST_USD):
                res.stopped["A"] = "budget exhausted"
                return
            before = dict(n_crit=_crit(t1), t15=q.t15, tier1_ok=t1.ok, scores=dict(q.result.scores), defects=dict(q.result.defects))
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
                if q.action.kind == "escalate_director" and not self.governor.may_escalate_to_director(QA_EST_USD):
                    res.stopped["A"] = "escalate_director not permitted by spend plan"
                    return
                patch, failed = await self.author_repair(q, located)
                label, why = "author", f"{q.action.kind}: {q.action.reasons[0] if q.action.reasons else ''}"
                if failed:
                    return  # res.stopped["A"] names the cause
                if patch is None:
                    res.stopped["A"] = "author declined to repair"
                    return
            try:
                tx = sm.apply_patch(patch)
            except Exception as e:  # noqa: BLE001 - invalid patch must not kill the run
                tr.note = f"patch rejected: {type(e).__name__}: {e}"
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
                               origin=origin, **before)

    # ---- Pass B ---------------------------------------------------------------------------------------

    async def resolve_textures(self) -> dict[str, PBRMaps]:
        t = self.textures
        if t is None and self.forge is not None:
            t = await synthesize_materials(self.sm.base_ir(), self.forge, plane=self.plane, governor=self.governor, prompt=self.prompt or self.res.asset,
                                           uploader=self.upload, cache=self.cache, size=self.texture_px(), brief=texture_brief(self.art))
        elif inspect.isawaitable(t):
            t = await t
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
                self.export_bundle = await asyncio.to_thread(run_export_pipeline, meshes, self.budget, scene.asset_name)
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
        if not self.maps and not self.res.engine_truth_required:
            self.step("textures", "skipped", "no PBR textures available (no forge and none supplied)")
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
        pending: tuple[dict[str, PBRMaps], float, str, dict[str, float], list[str]] | None = None
        for rnd in range(1, min(self.max_rounds, LOOKDEV_ROUNDS) + 1):
            png, tier = await self.render_lookdev()
            self.render_png = png
            t1 = tier1_qa(self.sm.extract_meshes(), self.budget)
            q = await self.qa("lookdev", png)
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
            levels = [sum(len(v[k].triangles) for v in bundle.lods.values()) for k in range(min((len(v) for v in bundle.lods.values()), default=0))]
            bad = [u.node_id for u in bundle.uvs if not u.within(budget)]
            desc = f"uv={len(bundle.uvs)} lod_tris={levels} hulls={len(bundle.colliders)}"
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
            for n in ("usd_package", "usd_validate", "engine_compliance", "lod_pop", "lod_pop_vision", "export_glb", "engine_import", "engine_parity", "web_runtime"):
                self.step(n, "skipped", "UV/LOD/collider stage failed")
            return

        if self.maps:
            self.write_textures()
        usd_path = pkg / f"{scene.asset_name}.usd"

        async def usd() -> tuple[str, str]:
            m = await asyncio.to_thread(write_engine_package, sm, bundle, budget, pkg, asset_name=scene.asset_name,
                                        qa={"tier15": res.tier15, "tier1_ok": res.tier1_ok, "defects": res.defects, "audit": res.audit},
                                        art=self.art)
            res.objects["package"] = m
            res.artifacts.update(usd=str(usd_path), manifest=str(pkg / "manifest.json"))
            stage = Usd.Stage.Open(str(usd_path))
            self.step("engine_compliance", *self.compliance(stage, meshes))
            crit = [i for i in m.issues if i.severity == "critical"]
            return ("failed", f"{len(crit)} critical: {crit[0].code} {crit[0].detail[:80]}") if crit else ("passed", f"write_engine_package ok; {len(m.issues)} non-critical message(s); files={len(m.files)}")

        await self.guarded("usd_validate", usd)
        if not any(x.name == "engine_compliance" for x in res.steps):
            self.step("engine_compliance", "skipped", "package USD was not written")

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

        await self.guarded("lod_pop", pop)

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
            return ("passed", "all LOD pairs imperceptible") if lod_pop_ok(r, self.th) else ("failed", "; ".join(reasons[:3]))

        await self.guarded("lod_pop_vision", pop_vision)

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

        # Independent of the target engine's own import, so it runs while those steps wait on their engine.
        web_check = asyncio.ensure_future(self.guarded_result(web_runtime))
        try:
            await self._engine_steps(validator_for(budget.target_engine))
        except BaseException:
            web_check.cancel()  # pass C was cancelled or crashed: stop the verification with it, and let it clean up
            await asyncio.wait({web_check})
            raise
        status, detail, t0 = await web_check
        self.step("web_runtime", status, detail, t0)

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
                res.objects["engine_import"] = {"engine": rep.engine, "metrics": rep.metrics, "bounds_m": rep.bounds_m}
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
            return "passed", "engine matches DCC"

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
) -> PyramidResult:
    """Run passes A (silhouette lock), B (lookdev) and C (final verification) on the stage's current scene.

    ``textures``: ``{material_id: PBRMaps | PBRTournamentResult}`` or an awaitable/task of it (speculative synthesis
    started earlier); with None and a ``forge`` the textures are synthesised here.
    ``art``: the art direction (``antigravity3d.art_direction.ArtDirection``) or None. It sets the size and palette of the
    written maps, the shading crease (export normals, the package and the LOD pop check), the texture sampler of the
    package GLBs and the texture/paint-over brief. ``budget`` is expected to be its effective budget already.
    """
    run = _Run(
        stage_manager, plane=plane, author=author, governor=governor, budget=budget, th=thresholds or Thresholds(), ortho_uri=ortho_uri,
        style_uri=style_uri, max_rounds=max_rounds, forge=forge, upload=uploader or uploader_for(forge), pool=pool, cache=cache,
        workdir=Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="agy3d-pyramid-")), fixlog=fixlog, asset_id=asset_id,
        prompt=prompt, textures=textures, settle_first=settle_first, engine_render_uri=engine_render_uri, require_verified=require_verified,
        art=art)
    run.workdir.mkdir(parents=True, exist_ok=True)
    # Pass C ends with the web-runtime check: its browser starts now, while passes A and B run, and is closed again
    # if this run fails or is cancelled before it is used.
    browser = web_three.prestart() if run_pass_c else None
    ok = False
    try:
        await run.pass_a()
        if run_pass_b and run.res.stopped.get("A") != "empty scene":
            await run.pass_b()
        if run_pass_c and run.res.stopped.get("A") != "empty scene":
            await run.pass_c()
        ok = run.res.ok
    finally:
        if browser is not None:
            await browser.aclose(keep=ok)
    run.res.objects["maps"] = run.maps
    return run.res
