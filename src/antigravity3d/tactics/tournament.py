"""Speculative Best-of-N: generate N candidates concurrently, grade each the moment it lands, rank, pick.

Grading goes through ``plane.rank_candidates(kind, [uri])`` one candidate at a time: the control plane
already scores every candidate independently against an identical rubric, so grading as they land is
equivalent to grading them together but overlaps vision latency with the remaining generations.
A generation or grading failure of one candidate never sinks the tournament.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from antigravity3d.decisions.compose import Thresholds, rank
from antigravity3d.decisions.models import CandidateScore
from antigravity3d.genmedia import pbr_deriver
from antigravity3d.genmedia.pbr_deriver import PBRMaps, derive_pbr
from antigravity3d.ir import SceneIR
from antigravity3d.kernel.fast_ops import tier1_qa
from antigravity3d.kernel.raster import contact_sheet
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.cached import CACHED_TAG, cached_derive_pbr
from antigravity3d.tactics.common import IMAGE_USD, Uploader, load_png, png_bytes, uploader_for
from antigravity3d.tactics.governor import Governor

GRADE_USD_EST = 0.0002  # one low-resolution Decisions request; used only to size N under a ceiling
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
                f"cancelled={len(self.cancelled)} wall={self.wall_ms:.0f}ms cost=${self.cost_usd:.4f}")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "n_planned": self.n_planned, "winner": self.winner.index if self.winner else None,
                "ranking": [{"index": s.index, "utility": s.utility, "blocked": s.blocked} for s in self.ranking],
                "failures": self.failures, "disqualified": self.disqualified, "cancelled": self.cancelled,
                "gen_ms": self.gen_ms, "grade_ms": self.grade_ms, "wall_ms": self.wall_ms, "max_in_flight": self.max_in_flight,
                "cost_usd": self.cost_usd, "gen_cost_usd": self.gen_cost_usd, "grade_cost_usd": self.grade_cost_usd, "notes": self.notes}


def plan_n(kind: str, governor: Governor, n: int | None, unit_cost: float | None) -> int:
    """N from the governor; an explicit ``n`` is only ever reduced (never raised) by the budget."""
    fit = governor.candidates(kind, unit_cost=unit_cost)
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
) -> TournamentResult:
    """Fire all generations concurrently (bounded), grade each as it lands, rank with ``compose.rank``.

    ``early_stop``: once a candidate scores utility >= this with no high-confidence defect, candidates that
    have not completed are cancelled (those still queued behind the concurrency bound cost nothing).
    """
    th = thresholds or Thresholds()
    planned = plan_n(kind, governor, n, unit_cost)
    sem = asyncio.Semaphore(max(1, concurrency or planned))
    t0 = time.perf_counter()
    res = TournamentResult(kind, planned, None, [], {})
    scores: dict[int, CandidateScore] = {}
    in_flight = 0
    tasks: dict[int, asyncio.Task[None]] = {}

    def stop_others(me: int) -> None:
        for j, t in tasks.items():
            if j != me and not t.done():
                t.cancel()

    async def one(i: int) -> None:
        nonlocal in_flight
        try:
            async with sem:
                in_flight += 1
                res.max_in_flight = max(res.max_in_flight, in_flight)
                g0 = time.perf_counter()
                try:
                    cand = await generate(i)
                finally:
                    in_flight -= 1
            cand = replace(cand, index=i)
            res.candidates[i] = cand
            res.gen_ms[i] = cand.gen_ms or (time.perf_counter() - g0) * 1e3
            res.gen_cost_usd += cand.gen_cost_usd
            if cand.disqualified:
                res.disqualified[i] = cand.disqualified
                return
            if cand.uri is None:
                raise ValueError("candidate has no uri to grade")
            r0 = time.perf_counter()
            graded = (await plane.rank_candidates(kind, [cand.uri], context_uris))[0]
            res.grade_ms[i] = (time.perf_counter() - r0) * 1e3
            graded = replace(graded, index=i)
            scores[i] = graded
            if graded.result is not None:
                res.grade_cost_usd += graded.result.est_cost_usd
            if early_stop is not None and not graded.blocked:
                top = rank([graded], kind, th)[0]
                if (top.utility or 0.0) >= early_stop and not any(p >= th.defect_high for p in top.defects.values()):
                    stop_others(i)
        except asyncio.CancelledError:
            res.cancelled.append(i)
            raise
        except Exception as e:  # noqa: BLE001 - one candidate must not sink the tournament
            res.failures[i] = f"{type(e).__name__}: {e}"

    for i in range(planned):
        tasks[i] = asyncio.ensure_future(one(i))
    await asyncio.gather(*tasks.values(), return_exceptions=True)

    res.ranking = rank(list(scores.values()), kind, th)
    if res.ranking and not res.ranking[0].blocked:
        res.winner = res.candidates[res.ranking[0].index]
    res.wall_ms = (time.perf_counter() - t0) * 1e3
    res.cancelled.sort()
    cat = charge_as or kind
    governor.charge(f"{cat}_gen", res.gen_cost_usd)
    governor.charge(f"{cat}_vision", res.grade_cost_usd)
    return res


# --------------------------------------------------------------------------- concrete tournaments


def _gen_cost(model: str, key: str) -> float:
    return 0.0 if model.endswith(CACHED_TAG) else IMAGE_USD[key]


async def ortho_tournament(
    prompt: str, forge: Any, *, plane: Any, governor: Governor, uploader: Uploader | None = None,
    style_anchor: bytes | None = None, context_uris: Sequence[str] = (), n: int | None = None,
    early_stop: float | None = None, concurrency: int | None = None, thresholds: Thresholds | None = None,
) -> TournamentResult:
    """Best-of-N four-view blueprints; each candidate stresses a different structural emphasis."""
    up = uploader or uploader_for(forge)

    async def generate(i: int) -> Candidate:
        emphasis = EMPHASES[i % len(EMPHASES)]
        g = await forge.ortho_sheet(prompt, emphasis=emphasis, style_anchor=style_anchor)
        return Candidate(i, await up(g.png_bytes), g, _gen_cost(g.model, "ortho"), g.latency_ms, meta={"emphasis": emphasis})

    return await run_tournament("ortho", generate, n, plane=plane, governor=governor, context_uris=context_uris, early_stop=early_stop,
                                unit_cost=IMAGE_USD["ortho"] + GRADE_USD_EST, concurrency=concurrency, thresholds=thresholds)


@dataclass
class PBRTournamentResult:
    material_id: str
    tournament: TournamentResult
    maps: PBRMaps | None = None
    albedo_png: bytes | None = None  # prepared winning albedo
    prep: str = ""  # one-line description of the deterministic albedo preparation (or why it was skipped)
    derive_ms: float = 0.0
    derive_cache_hit: bool | None = None


async def pbr_tournament(
    prompt: str, forge: Any, *, plane: Any, governor: Governor, material_id: str = "material", metallic_hint: float = 0.0,
    uploader: Uploader | None = None, n: int | None = None, size: int = 512, cache: Any = None, seed_offset: int = 0,
    early_stop: float | None = None, concurrency: int | None = None, thresholds: Thresholds | None = None,
) -> PBRTournamentResult:
    """Albedo candidates -> policy gate -> deterministic preparation -> rank; ORM/normal derived for the winner only."""
    up = uploader or uploader_for(forge)
    prepare = getattr(pbr_deriver, "prepare_albedo", None)

    async def generate(i: int) -> Candidate:
        hint = ALBEDO_VARIANTS[(i + seed_offset) % len(ALBEDO_VARIANTS)]
        g = await forge.seamless_albedo(prompt, size=size, variant_hint=hint)
        png, uri = g.png_bytes, await up(g.png_bytes)
        cost = _gen_cost(g.model, "albedo")
        verdict = await plane.policy_gate(uri)
        if not verdict.allowed:
            return Candidate(i, uri, png, cost, g.latency_ms, f"policy gate: {', '.join(verdict.reasons) or 'blocked'}", {"hint": hint})
        prep = "albedo preparation unavailable (pbr_deriver.prepare_albedo not present)"
        if prepare is not None:
            img, rep = await asyncio.to_thread(prepare, load_png(png), metallic=metallic_hint >= 0.5)
            prep = rep.one_line()
            if rep.changed:
                png = png_bytes(img)
                uri = await up(png)
        return Candidate(i, uri, png, cost, g.latency_ms, None, {"hint": hint, "prep": prep})

    t = await run_tournament("pbr", generate, n, plane=plane, governor=governor, early_stop=early_stop, concurrency=concurrency,
                             unit_cost=IMAGE_USD["albedo"] + GRADE_USD_EST, thresholds=thresholds, charge_as="pbr")
    out = PBRTournamentResult(material_id, t)
    if t.winner is None:
        return out
    out.albedo_png, out.prep = t.winner.payload, t.winner.meta.get("prep", "")
    d0 = time.perf_counter()
    if cache is not None:
        out.maps, out.derive_cache_hit = await asyncio.to_thread(cached_derive_pbr, cache, out.albedo_png, metallic_hint=metallic_hint)
    else:
        out.maps = await asyncio.to_thread(derive_pbr, load_png(out.albedo_png), metallic_hint=metallic_hint)
    out.derive_ms = (time.perf_counter() - d0) * 1e3
    return out


async def geometry_tournament(
    author: Any, stage_manager: Any, prompt: str, *, plane: Any, governor: Governor, budget: EngineBudget,
    uploader: Uploader, context_uris: Sequence[str] = (), scenes: list[SceneIR] | None = None, set_name: str = "blockout",
    context: dict[str, Any] | None = None, size: int = 256, n: int | None = None, author_cost_usd: float = 0.0,
    early_stop: float | None = None, concurrency: int | None = None, thresholds: Thresholds | None = None,
) -> TournamentResult:
    """Author N candidate IRs as variants of one USD variant set, Tier-1 each, vision-grade the survivors, promote the winner.

    Candidates failing a critical Tier-1 check are disqualified without a vision call. If every candidate is
    disqualified the one with the fewest critical issues is promoted anyway (``notes['fallback_promoted']``) so
    the pyramid has something to repair.
    """
    n_plan = plan_n("geometry", governor, n, GRADE_USD_EST)
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
        sheet = await asyncio.to_thread(contact_sheet, meshes, size=size)
        return Candidate(i, await uploader(png_bytes(sheet)), scenes[i], 0.0, 0.0, None, meta)

    res = await run_tournament("geometry", generate, len(scenes), plane=plane, governor=governor, context_uris=context_uris,
                               early_stop=early_stop, concurrency=concurrency or 1, thresholds=thresholds)
    if res.winner is None and crit_counts and not res.ranking:
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
