"""Generate candidates concurrently and judge eligible candidates in budgeted, shared-input batches."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from antigravity3d.decisions.compose import Thresholds, rank
from antigravity3d.decisions.models import CandidateScore
from antigravity3d.genmedia.pbr_deriver import PBRMaps, derive_pbr, prepare_albedo
from antigravity3d.ir import SceneIR
from antigravity3d.kernel.fast_ops import tier1_qa
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.cached import CACHED_TAG, cached_derive_pbr, image_identity
from antigravity3d.tactics.common import (
    GEOMETRY_JUDGE_TOKENS_EST, GEOMETRY_JUDGE_USD_EST, GRADE_TOKENS_EST, IMAGE_TOKENS_EST, IMAGE_USD, Uploader, load_png, png_bytes,
    to_thread_drained, uploader_for,
)
from antigravity3d.tactics.governor import Governor
from antigravity3d.tactics.judging import JUDGE_VIEW_PX, design_views

GRADE_USD_EST = 0.0002  # one low-resolution Decisions request; used only to size N under a ceiling


def money(fields: dict[str, float], unit: str) -> dict[str, Any]:
    """Dollar fields as written: unchanged on a money ledger; null (with spend_unit) on a token ledger, where no price
    is applied and no dollar figure exists. A token number never goes into a *_usd key."""
    if unit == "usd":
        return fields
    return {**{k: None for k in fields}, "spend_unit": unit}
EMPHASES = ("overall proportions and a readable silhouette", "construction details, joints and fasteners",
            "strict left-right symmetry and view alignment", "clean flat shading and measurable dimensions")
ALBEDO_VARIANTS = ("even, medium contrast", "slightly worn, fine grain", "darker, more saturated", "lighter, subtle patina",
                   "coarse grain, low contrast", "high-frequency detail, muted colour")


@dataclass
class Candidate:
    index: int
    uri: str | None = None
    payload: Any = None
    gen_cost_usd: float = 0.0
    gen_ms: float = 0.0
    disqualified: str | None = None  # set => never sent to the vision plane
    meta: dict[str, Any] = field(default_factory=dict)
    # The candidate as separate views, (view name, uri) (tactics.judging); the judge is shown these instead of ``uri``
    # (then the first view's uri). None: one image.
    views: tuple[tuple[str, str], ...] | None = None


@dataclass
class TournamentResult:
    kind: str
    n_planned: int
    winner: Candidate | None
    ranking: list[CandidateScore]  # graded candidates, best first
    candidates: dict[int, Candidate]  # every successful generation
    failures: dict[int, str] = field(default_factory=dict)
    disqualified: dict[int, str] = field(default_factory=dict)
    cancelled: list[int] = field(default_factory=list)
    gen_ms: dict[int, float] = field(default_factory=dict)
    grade_ms: dict[int, float] = field(default_factory=dict)
    wall_ms: float = 0.0
    max_in_flight: int = 0
    gen_cost_usd: float = 0.0
    grade_cost_usd: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)
    unit: str = "usd"  # the ledger's unit; on a token ledger the dollar figures are not computed and are written as null

    @property
    def cost_usd(self) -> float:
        return self.gen_cost_usd + self.grade_cost_usd

    @property
    def winner_score(self) -> CandidateScore | None:
        return self.ranking[0] if self.winner is not None and self.ranking else None

    def summary(self) -> str:
        w = self.winner.index if self.winner else None
        u = self.winner_score.utility if self.winner_score else None
        return (f"{self.kind}: winner={w}" + (f" utility={u:.3f}" if u is not None else "") +
                f" n={self.n_planned} graded={len(self.ranking)} dq={len(self.disqualified)} failed={len(self.failures)} "
                f"cancelled={len(self.cancelled)} wall={self.wall_ms:.0f}ms "
                + (f"cost=${self.cost_usd:.4f}" if self.unit == "usd" else "cost=n/a (token ledger, no price applied)"))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "n_planned": self.n_planned, "winner": self.winner.index if self.winner else None,
                "ranking": [{"index": s.index, "utility": s.utility, "blocked": s.blocked} for s in self.ranking],
                "failures": self.failures, "disqualified": self.disqualified, "cancelled": self.cancelled,
                "gen_ms": self.gen_ms, "grade_ms": self.grade_ms, "wall_ms": self.wall_ms, "max_in_flight": self.max_in_flight,
                **money({"cost_usd": self.cost_usd, "gen_cost_usd": self.gen_cost_usd, "grade_cost_usd": self.grade_cost_usd}, self.unit),
                "notes": self.notes}


def grade_est(governor: Any) -> float:
    """One grading call, in the ledger's unit (sizing only)."""
    return governor.est(usd=GRADE_USD_EST, tokens=GRADE_TOKENS_EST) if hasattr(governor, "est") else GRADE_USD_EST


def image_est(governor: Any, key: str) -> float:
    """One generated image plus its grading, in the ledger's unit (sizing only)."""
    if hasattr(governor, "est"):
        return governor.est(usd=IMAGE_USD[key] + GRADE_USD_EST, tokens=IMAGE_TOKENS_EST + GRADE_TOKENS_EST)
    return IMAGE_USD[key] + GRADE_USD_EST


def plan_n(kind: str, governor: Governor, n: int | None, unit_cost: float | None, images_each: int = 0) -> int:
    """N from the governor; an explicit ``n`` is only ever reduced (never raised) by the budget."""
    fit = governor.candidates(kind, unit_cost=unit_cost, images_each=images_each) if images_each else governor.candidates(kind, unit_cost=unit_cost)
    return fit if n is None else (min(n, fit) if unit_cost is not None else n)


async def run_tournament(
    kind: str,
    generate: Callable[[int], Awaitable[Candidate]],
    n: int | None,
    *,
    plane: Any,
    governor: Governor,
    context_uris: Sequence[str] = (),
    early_stop: float | None = None,
    unit_cost: float | None = None,
    concurrency: int | None = None,
    thresholds: Thresholds | None = None,
    charge_as: str | None = None,
    generation_precharged: bool = False,
    images_each: int = 0,
    brief: str | None = None,
) -> TournamentResult:
    """Generate concurrently, then score every eligible candidate against one rubric.

    ``brief``: the user's request, given to the judge with the candidates (``rank_candidates(brief=)``); None or empty:
    the ranking request is exactly what it was without one.

    With an explicit early-stop threshold we generate and judge in waves of the chosen
    concurrency. Once a wave qualifies, remaining generators are never dispatched.
    """
    th = thresholds or Thresholds()
    planned = plan_n(kind, governor, n, unit_cost, images_each)
    limit = max(1, concurrency or planned)
    sem = asyncio.Semaphore(limit)
    t0 = time.perf_counter()
    res = TournamentResult(kind, planned, None, [], {})
    res.unit = getattr(governor, "unit", "usd")
    scores: dict[int, CandidateScore] = {}
    in_flight = 0

    async def one(i: int) -> None:
        nonlocal in_flight
        try:
            async with sem:
                in_flight += 1
                res.max_in_flight = max(res.max_in_flight, in_flight)
                g0 = time.perf_counter()
                try:
                    cand = replace(await generate(i), index=i)
                finally:
                    in_flight -= 1
            res.candidates[i] = cand
            res.gen_ms[i] = cand.gen_ms or (time.perf_counter() - g0) * 1e3
            res.gen_cost_usd += cand.gen_cost_usd
            if cand.disqualified:
                res.disqualified[i] = cand.disqualified
            elif not cand.uri:
                raise ValueError("candidate has no uri to grade")
        except Exception as exc:
            res.failures[i] = f"{type(exc).__name__}: {exc}"

    wave_size = limit if early_stop is not None else max(1, planned)
    for start in range(0, planned, wave_size):
        stop = min(planned, start + wave_size)
        await asyncio.gather(*(one(i) for i in range(start, stop)))
        eligible = [i for i in range(start, stop) if i not in res.failures and i not in res.disqualified]
        if not eligible:
            continue
        r0 = time.perf_counter()
        try:
            graded = await plane.rank_candidates(kind, [res.candidates[i].views or res.candidates[i].uri for i in eligible], context_uris, governor=governor,
                                                 **({"brief": brief} if brief else {}))
        except Exception as exc:
            res.failures.update({i: f"{type(exc).__name__}: {exc}" for i in eligible})
            continue
        duration = (time.perf_counter() - r0) * 1e3
        for i, score in zip(eligible, graded, strict=True):
            res.grade_ms[i] = duration
            if score.error:
                res.failures[i] = score.error
                continue
            scores[i] = replace(score, index=i)
            if score.blocked:
                reason = ", ".join(score.policy.reasons) if score.policy else "blocked"
                res.disqualified[i] = f"policy gate: {reason}"
        # All candidates in a packet share one billed result, including policy-filtered packets.
        packets = {id(score.result): score.result for score in graded if score.result is not None}
        res.grade_cost_usd += sum(result.booked_usd for result in packets.values()) + sum(score.billed_on_error for score in graded)
        if early_stop is not None and any(
            not top.blocked and top.utility >= early_stop and all(p < th.defect_high for p in top.defects.values())
            for top in rank(list(scores.values()), kind, th)
        ):
            res.cancelled.extend(range(stop, planned))
            break

    res.ranking = rank(list(scores.values()), kind, th)
    if res.ranking and not res.ranking[0].blocked:
        res.winner = res.candidates[res.ranking[0].index]
    res.wall_ms = (time.perf_counter() - t0) * 1e3
    res.cancelled.sort()
    cat = charge_as or kind
    if not generation_precharged:
        governor.charge(f"{cat}_gen", res.gen_cost_usd)
    return res


def geometry_grade_est(governor: Any) -> float:
    """One geometry candidate's judgement (five high views, a request of its own), in the ledger's unit (sizing only)."""
    if hasattr(governor, "est"):
        return governor.est(usd=GEOMETRY_JUDGE_USD_EST, tokens=GEOMETRY_JUDGE_TOKENS_EST)
    return GEOMETRY_JUDGE_USD_EST


def _note_judgements(res: TournamentResult) -> None:
    """Per candidate judged alone: what its request booked and how long it took from sending to answer (admission wait
    and retries included), and one line on how the judgements ran, for the step detail."""
    judged = {s.index: s.result for s in res.ranking if s.result is not None}
    if not judged:
        return
    spans = {i: (r.latency_ms, r.booked_tokens, r.booked_usd, r.input_tokens, r.output_tokens, r.attempts) for i, r in judged.items()}
    res.notes["judged"] = {i: {"ms": round(ms), "booked_tokens": bt, **money({"booked_usd": bu}, res.unit), "input_tokens": it,
                               "output_tokens": ot, "attempts": at} for i, (ms, bt, bu, it, ot, at) in sorted(spans.items())}
    longest = max(ms for ms, *_ in spans.values())
    total = sum(ms for ms, *_ in spans.values())
    wall = max(res.grade_ms.values()) if res.grade_ms else longest
    # judgements in flight on average: the summed request time over the ranking's wall time
    together = max(1, min(len(spans), round(total / wall))) if wall > 0 else len(spans)
    res.notes["judging"] = (f"{len(spans)} candidate(s) judged separately, about {together} at a time: "
                            f"{wall / 1e3:.1f} s for the ranking, {longest / 1e3:.1f} s for the longest judgement")


# --------------------------------------------------------------------------- concrete tournaments


def _gen_cost(image: Any, key: str) -> float:
    if getattr(image, "receipt", None) is not None:
        return image.receipt.booked_usd
    return 0.0 if image.model.endswith(CACHED_TAG) else IMAGE_USD[key]


async def ortho_tournament(
    prompt: str, forge: Any, *, plane: Any, governor: Governor, uploader: Uploader | None = None,
    style_anchor: bytes | None = None, context_uris: Sequence[str] = (), n: int | None = None,
    early_stop: float | None = None, concurrency: int | None = None, thresholds: Thresholds | None = None,
) -> TournamentResult:
    """Best-of-N four-view blueprints; each candidate stresses a different structural emphasis."""
    precharged = hasattr(forge, "with_governor")
    if precharged:
        forge = forge.with_governor(governor)
    up = uploader or uploader_for(forge)

    async def generate(i: int) -> Candidate:
        emphasis = EMPHASES[i % len(EMPHASES)]
        g = await forge.ortho_sheet(prompt, emphasis=emphasis, style_anchor=style_anchor)
        return Candidate(i, g.uri or await up(g.png_bytes), g, _gen_cost(g, "ortho"), g.latency_ms, meta={"emphasis": emphasis})

    # Every planned sheet already in the image cache (a rerun in the same --out): generating them needs no image and no
    # budget, so the plan's sizing must not drop them for the first run's images that the asset's ledger still counts.
    # Without the blueprint the authoring request differs and the recorded design is not found (round five).
    has = getattr(forge, "has_ortho", None)
    planned = governor.plan.ortho_candidates if n is None else n
    all_cached = bool(has) and planned > 0 and all(
        has(prompt, emphasis=EMPHASES[i % len(EMPHASES)], style_anchor=style_anchor) for i in range(planned))
    return await run_tournament("ortho", generate, n, plane=plane, governor=governor, context_uris=context_uris, early_stop=early_stop,
                                generation_precharged=precharged, unit_cost=0.0 if all_cached else image_est(governor, "ortho"),
                                concurrency=concurrency, thresholds=thresholds, images_each=0 if all_cached else 1)


@dataclass
class PBRTournamentResult:
    material_id: str
    tournament: TournamentResult
    maps: PBRMaps | None = None
    albedo_png: bytes | None = None  # prepared winning albedo
    prep: str = ""  # one-line description of the deterministic albedo preparation (or why it was skipped)
    derive_ms: float = 0.0
    derive_cache_hit: bool | None = None
    image: dict[str, Any] | None = None  # who made the winning albedo (``cached.image_identity``): the manifest's provenance


def albedo_variant(i: int, seed_offset: int = 0) -> str:
    """The variant hint of candidate ``i`` of a material's tournament (also the cache key's, see ``CachedForge.has_albedo``)."""
    return ALBEDO_VARIANTS[(i + seed_offset) % len(ALBEDO_VARIANTS)]


async def pbr_tournament(
    prompt: str, forge: Any, *, plane: Any, governor: Governor, material_id: str = "material", metallic_hint: float = 0.0,
    uploader: Uploader | None = None, n: int | None = None, size: int = 2048, cache: Any = None, seed_offset: int = 0,
    early_stop: float | None = None, concurrency: int | None = None, thresholds: Thresholds | None = None,
    all_cached: bool = False,
) -> PBRTournamentResult:
    """Screen and rank raw albedos once. Prepare and derive maps only for the winner.

    ``all_cached``: every one of the ``n`` candidate images is in the image cache (the caller checked with
    ``CachedForge.has_albedo``), so generating them costs no budget and no image: the plan's own sizing must not trim
    candidates for lack of either (a rerun's ledger holds the first run's images). Judging them is still a call."""
    precharged = hasattr(forge, "with_governor")
    if precharged:
        forge = forge.with_governor(governor)
    up = uploader or uploader_for(forge)

    identities: dict[int, dict[str, Any]] = {}  # candidate index -> who made it (read for the winner below)

    async def generate(i: int) -> Candidate:
        hint = albedo_variant(i, seed_offset)
        g = await forge.seamless_albedo(prompt, size=size, variant_hint=hint)
        identities[i] = image_identity(g, prompt=prompt, size=size, variant=hint, offline=bool(getattr(forge, "offline", False)))
        uri = g.uri or await up(g.png_bytes)
        return Candidate(i, uri, g.png_bytes, _gen_cost(g, "albedo"), g.latency_ms, meta={"hint": hint})

    t = await run_tournament("pbr", generate, n, plane=plane, governor=governor, early_stop=early_stop, concurrency=concurrency,
                             unit_cost=0.0 if all_cached else image_est(governor, "albedo"), thresholds=thresholds,
                             generation_precharged=precharged, images_each=0 if all_cached else 1)
    out = PBRTournamentResult(material_id, t)
    if t.winner is None:
        return out
    out.image = identities.get(t.winner.index)
    d0 = time.perf_counter()
    def prepare_winner() -> tuple[bytes, str]:
        img, report = prepare_albedo(load_png(t.winner.payload), metallic=metallic_hint >= 0.5)
        return (png_bytes(img) if report.changed else t.winner.payload), report.one_line()

    out.albedo_png, out.prep = await to_thread_drained(prepare_winner)
    if cache is not None:
        out.maps, out.derive_cache_hit = await to_thread_drained(cached_derive_pbr, cache, out.albedo_png, metallic_hint=metallic_hint)
    else:
        out.maps = await to_thread_drained(derive_pbr, load_png(out.albedo_png), metallic_hint=metallic_hint)
    out.derive_ms = (time.perf_counter() - d0) * 1e3
    return out


async def geometry_tournament(
    author: Any, stage_manager: Any, prompt: str, *, plane: Any, governor: Governor, budget: EngineBudget,
    uploader: Uploader, context_uris: Sequence[str] = (), scenes: list[SceneIR] | None = None, set_name: str = "blockout",
    context: dict[str, Any] | None = None, size: int = JUDGE_VIEW_PX, n: int | None = None, author_cost_usd: float = 0.0,
    early_stop: float | None = None, concurrency: int | None = None, thresholds: Thresholds | None = None,
) -> TournamentResult:
    """Author N candidate IRs as variants of one USD variant set, Tier-1 each, vision-grade the survivors, promote the winner.

    Candidates failing a critical Tier-1 check are disqualified without a vision call. If every candidate is
    disqualified the one with the fewest critical issues is promoted anyway (``notes['fallback_promoted']``) so
    the pyramid has something to repair.

    Each survivor is shown to the judge as the separate views of ``tactics.judging`` (``size`` px each) and judged in a
    request of its own; the requests go out together and admission queues them under the ceiling. What each judgement
    booked and took is in ``notes['judged']``, and ``notes['judging']`` says how many ran at once.
    """
    n_plan = plan_n("geometry", governor, n, geometry_grade_est(governor))
    if scenes is None:
        ctx = {"asset_name": stage_manager.asset_prim.GetName(), "ortho_uri": context_uris[0] if context_uris else None, **(context or {})}
        scenes = await author.propose(prompt, budget=budget, n=n_plan, context=ctx)
        governor.charge("author", author_cost_usd)
    scenes = list(scenes)[:n_plan]
    lock = asyncio.Lock()
    names = {i: f"cand_{i:02d}" for i in range(len(scenes))}
    crit_counts: dict[int, tuple[int, int]] = {}

    async def generate(i: int) -> Candidate:
        async with lock:  # stage edits are serialised; no await between select and rollback
            stage_manager.add_variant(set_name, names[i], scenes[i])
            sel = stage_manager.select_variant(set_name, names[i])
            try:
                meshes = stage_manager.extract_meshes()
            finally:
                stage_manager.rollback(sel)
        t1 = await asyncio.to_thread(tier1_qa, meshes, budget)
        crit = [x for x in t1.issues if x.severity == "critical"]
        meta = {"variant": names[i], "tier1": t1.one_line(), "tris": t1.stats["tris"], "warnings": len(t1.issues) - len(crit)}
        if crit:
            crit_counts[i] = (len(crit), t1.stats["tris"])
            return Candidate(i, None, scenes[i], 0.0, 0.0, t1.one_line(), meta)
        views = await design_views(meshes, uploader, size=size)
        return Candidate(i, views[0][1], scenes[i], 0.0, 0.0, None, meta, views=views)

    res = await run_tournament("geometry", generate, len(scenes), plane=plane, governor=governor, context_uris=context_uris,
                               early_stop=early_stop, concurrency=concurrency, thresholds=thresholds, brief=prompt)
    _note_judgements(res)
    if res.winner is None and crit_counts and len(crit_counts) == len(scenes):
        best = min(crit_counts, key=lambda i: crit_counts[i])
        res.winner = res.candidates[best]
        res.notes["fallback_promoted"] = best
    if res.winner is None:
        if res.candidates or res.failures:
            raise RuntimeError(f"geometry tournament produced no usable candidate: {res.failures or res.disqualified}")
        raise RuntimeError("geometry tournament: author proposed no scenes")
    stage_manager.promote_variant(set_name, names[res.winner.index])
    res.notes["variant"] = names[res.winner.index]
    return res
