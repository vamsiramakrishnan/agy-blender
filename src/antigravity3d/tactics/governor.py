"""Cost/quality budget governor.

Maps an asset's complexity score to a ``SpendPlan`` (how many Best-of-N
candidates to draw, which models may run, which render quality to use) and
tracks dollar spend against that plan's hard ceiling.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
import uuid
import weakref
from pathlib import Path
from dataclasses import dataclass, replace

_TOLERANCE = 1e-9


@dataclass(frozen=True)
class SpendPlan:
    complexity_tier: int  # 0..3
    label: str  # clutter, simple, standard or hero
    ortho_candidates: int  # Best-of-N for orthographic blueprints
    pbr_candidates: int  # Best-of-N for albedo textures
    geometry_candidates: int  # Best-of-N for geometry seeds
    builder_role: str  # a role name from models.py
    allow_director: bool  # may escalate to the director model
    paintover_loop: bool
    engine_truth: bool  # run the headless engine parity check
    reuse_trim_sheet: bool  # prefer shared trim sheet over unique textures
    final_render: str  # "preview" | "eevee" | "cycles"
    ceiling_usd: float  # hard spend ceiling for this asset


_CLUTTER = SpendPlan(
    complexity_tier=0,
    label="clutter",
    ortho_candidates=1,
    pbr_candidates=2,
    geometry_candidates=1,
    builder_role="worker",
    allow_director=False,
    paintover_loop=False,
    engine_truth=False,
    reuse_trim_sheet=True,
    final_render="preview",
    ceiling_usd=0.15,
)

_STANDARD = SpendPlan(
    complexity_tier=2,
    label="standard",
    ortho_candidates=4,
    pbr_candidates=4,
    geometry_candidates=4,
    builder_role="worker",
    allow_director=True,
    paintover_loop=False,
    engine_truth=False,
    reuse_trim_sheet=False,
    final_render="eevee",
    ceiling_usd=0.85,
)

_HERO = SpendPlan(
    complexity_tier=3,
    label="hero",
    ortho_candidates=4,
    pbr_candidates=6,
    geometry_candidates=8,
    builder_role="director",
    allow_director=True,
    paintover_loop=True,
    engine_truth=True,
    reuse_trim_sheet=False,
    final_render="cycles",
    ceiling_usd=2.20,
)

PLANS: dict[int, SpendPlan] = {
    0: replace(_CLUTTER, complexity_tier=0),
    1: replace(_CLUTTER, complexity_tier=1, label="simple", ortho_candidates=2, geometry_candidates=2, ceiling_usd=0.35),
    2: _STANDARD,
    3: _HERO,
}

_KINDS = {"ortho": "ortho_candidates", "pbr": "pbr_candidates", "geometry": "geometry_candidates"}


def plan_for(complexity_score: float) -> SpendPlan:
    """Return the plan for an expected complexity score in [0, 3].

    The score is rounded half up to the nearest integer tier and clamped to
    0..3. NaN is rejected.
    """
    score = float(complexity_score)
    if math.isnan(score):
        raise ValueError("complexity_score must not be NaN")
    clamped = min(3.0, max(0.0, score))
    tier_index = int(math.floor(clamped + 0.5))
    return PLANS[min(3, max(0, tier_index))]


class BudgetExceeded(RuntimeError):
    """No provider request was dispatched because it exceeded the available budget."""


def _amount(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("charge must be finite and non-negative")
    return value


@dataclass(frozen=True)
class Reservation:
    governor: Governor
    id: str
    limit: float

    def settle(self, usd: float | None = None, *, receipt: dict | None = None) -> float:
        """Record the billed amount, or retain the full reservation for an ambiguous response."""
        booked = self.limit if usd is None else _amount(usd)
        db = self.governor._db
        with self.governor._lock, db:
            changed = db.execute(
                "UPDATE spend SET booked=?, receipt=? WHERE id=? AND booked IS NULL",
                (booked, json.dumps(receipt) if receipt else None, self.id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("reservation already settled")
        self.governor._wake_waiters()  # after the commit; callable from any thread
        return booked


@dataclass(eq=False)
class _Waiter:
    """One request parked in :meth:`Governor.reserve_wait`; ``future`` belongs to ``loop`` and is replaced on each wait."""

    loop: asyncio.AbstractEventLoop
    future: asyncio.Future | None = None


def _resolve(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


class Governor:
    """Atomic per-asset spend ledger. An optional SQLite path survives process restarts."""

    def __init__(self, plan: SpendPlan, *, journal: Path | None = None) -> None:
        self._plan = plan
        if journal is not None:
            journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal = journal
        self._db = sqlite3.connect(journal or ":memory:", check_same_thread=False)
        weakref.finalize(self, self._db.close)
        self._lock = threading.RLock()
        self._waiters: list[_Waiter] = []  # FIFO of parked reserve_wait calls; guarded by self._lock
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS spend (id TEXT PRIMARY KEY, category TEXT NOT NULL, "
            "reserved REAL NOT NULL, booked REAL, receipt TEXT)"
        )

    @property
    def plan(self) -> SpendPlan:
        return self._plan

    def _try_insert(self, category: str, limit: float, receipt: dict | None) -> Reservation | None:
        """Atomically book ``limit`` against the ceiling, or return None when it does not fit right now."""
        reservation = Reservation(self, uuid.uuid4().hex, limit)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if not self.can_afford(limit):
                return None
            self._db.execute("INSERT INTO spend VALUES (?, ?, ?, NULL, ?)",
                             (reservation.id, category, limit, json.dumps(receipt) if receipt else None))
        return reservation

    def reserve(self, category: str, usd: float, *, receipt: dict | None = None) -> Reservation:
        """Reserve now or raise :class:`BudgetExceeded`. Never waits."""
        limit = _amount(usd)
        reservation = self._try_insert(category, limit, receipt)
        if reservation is None:
            raise BudgetExceeded(f"{category} needs ${limit:.5f}, ${self.remaining:.5f} available")
        return reservation

    def _ledger(self) -> tuple[float, int]:
        """(settled spend, number of unsettled reservations)."""
        with self._lock:
            booked, open_ = self._db.execute(
                "SELECT COALESCE(SUM(booked),0), COALESCE(SUM(booked IS NULL),0) FROM spend").fetchone()
        return booked, int(open_)

    def _wake_waiters(self) -> None:
        """Wake every parked request so the head of the queue re-checks. Safe from any thread and any loop."""
        with self._lock:
            parked = [(w.loop, w.future) for w in self._waiters if w.future is not None]
        for loop, future in parked:
            try:
                loop.call_soon_threadsafe(_resolve, future)
            except RuntimeError:  # that loop is closed; its waiter is gone
                pass

    async def reserve_wait(self, category: str, usd: float, *, receipt: dict | None = None) -> Reservation:
        """Reserve ``usd``, waiting for outstanding reservations to settle when it does not fit yet.

        * Fits now: reserved at once (when no earlier request is still waiting; waiters are served first-come
          first-served so a large request is not starved by later small ones).
        * Cannot fit even if every outstanding reservation settled at zero (``usd`` > ceiling - settled spend):
          :class:`BudgetExceeded` at once, with the numbers. The same check runs again after each wake-up, because a
          settlement that books more than expected can turn "will fit" into "never fits".
        * Otherwise it parks until a settlement wakes it; there is no polling. Waiting needs an outstanding
          reservation, whose owner settles it in a ``finally``; with none outstanding the request would already fit
          or already have failed, so an idle ledger cannot park anything.

        The check and the insert are one critical section (this lock plus ``BEGIN IMMEDIATE``), exactly as in
        :meth:`reserve`, so booked + reserved never exceeds the ceiling at any moment. A waiter holds nothing, so
        waiters cannot deadlock each other. Cancellation removes the waiter and hands the head of the queue on.
        Settlements wake waiters from any thread (``call_soon_threadsafe``); each waiter lives on its own loop.
        """
        limit = _amount(usd)
        waiter = _Waiter(asyncio.get_running_loop())
        queued = False
        try:
            while True:
                with self._lock:
                    first = not self._waiters if not queued else self._waiters[0] is waiter
                    booked, outstanding = self._ledger()
                    ceiling = self._plan.ceiling_usd
                    if limit > ceiling - booked + _TOLERANCE:
                        raise BudgetExceeded(
                            f"{category} needs ${limit:.5f} but can never fit: ceiling ${ceiling:.5f}, "
                            f"${booked:.5f} already settled, ${max(0.0, ceiling - booked):.5f} at most available")
                    if first:
                        reservation = self._try_insert(category, limit, receipt)
                        if reservation is not None:
                            if queued:
                                self._waiters.remove(waiter)
                                queued = False
                            handoff = bool(self._waiters)
                            break
                        if outstanding == 0:  # unreachable: nothing to wait for and it does not fit
                            raise BudgetExceeded(f"{category} needs ${limit:.5f}, ${self.remaining:.5f} available")
                    waiter.future = waiter.loop.create_future()
                    if not queued:
                        self._waiters.append(waiter)
                        queued = True
                    future = waiter.future
                await future
        finally:
            if queued:
                with self._lock:
                    if waiter in self._waiters:
                        self._waiters.remove(waiter)
                self._wake_waiters()  # the head may have changed
        if handoff:
            self._wake_waiters()  # more may fit behind this one
        return reservation

    def charge(self, category: str, usd: float) -> None:
        amount = _amount(usd)
        with self._lock, self._db:
            self._db.execute("INSERT INTO spend VALUES (?, ?, 0, ?, NULL)", (uuid.uuid4().hex, category, amount))

    @property
    def spent(self) -> float:
        with self._lock:
            return self._db.execute("SELECT COALESCE(SUM(booked),0) FROM spend").fetchone()[0]

    @property
    def by_category(self) -> dict[str, float]:
        with self._lock:
            return dict(self._db.execute("SELECT category, SUM(booked) FROM spend WHERE booked IS NOT NULL GROUP BY category"))

    @property
    def remaining(self) -> float:
        with self._lock:
            committed, = self._db.execute("SELECT COALESCE(SUM(COALESCE(booked,reserved)),0) FROM spend").fetchone()
        return max(0.0, self.plan.ceiling_usd - committed)

    def can_afford(self, usd: float) -> bool:
        return _amount(usd) <= self.remaining + _TOLERANCE

    def receipts(self) -> list[dict]:
        with self._lock:
            records = self._db.execute(
                "SELECT id,category,reserved,booked,receipt FROM spend WHERE receipt IS NOT NULL ORDER BY rowid").fetchall()
        return [{**json.loads(payload), "reservation_id": key, "category": cat, "reserved_usd": reserved, "booked_usd": booked,
                 "overrun_usd": max(0.0, booked - reserved) if booked is not None else None}
                for key, cat, reserved, booked, payload in records]

    def candidates(self, kind: str, *, unit_cost: float | None = None, admit_cost: float | None = None) -> int:
        """Best-of-N for ``kind``, reduced so N * unit_cost fits the remaining budget.

        Without ``unit_cost`` the plan's N is returned unchanged. The result is zero when a single candidate does
        not fit. ``unit_cost`` is what a candidate is *expected* to cost. ``admit_cost`` (default ``unit_cost``) is
        what admission must be able to reserve for one candidate, its worst case; candidates run one after another
        under the ceiling, so the last one is admitted when the others have settled near ``unit_cost`` and N is the
        largest value with ``(N - 1) * unit_cost + admit_cost <= remaining``.
        """
        if kind not in _KINDS:
            raise ValueError(f"unknown candidate kind {kind!r}; expected one of {sorted(_KINDS)}")
        base = getattr(self._plan, _KINDS[kind])
        if unit_cost is None:
            return base
        if unit_cost < 0 or (admit_cost is not None and admit_cost < 0):
            raise ValueError(f"costs must be non-negative, got unit_cost={unit_cost!r} admit_cost={admit_cost!r}")
        if unit_cost == 0:
            return base
        admit = unit_cost if admit_cost is None else max(admit_cost, unit_cost)
        room = self.remaining + _TOLERANCE - admit
        if room < 0:
            return 0
        return max(0, min(base, 1 + math.floor(room / unit_cost)))

    def may_escalate_to_director(self, estimated_usd: float) -> bool:
        return self._plan.allow_director and self.can_afford(estimated_usd)

    def summary(self) -> str:
        parts = ", ".join(f"{name} ${amount:.4f}" for name, amount in sorted(self.by_category.items()))
        return f"{self._plan.label}: ${self.spent:.4f} of ${self._plan.ceiling_usd:.2f} ({parts})"


def usage_cost_usd(
    prompt_tokens: int,
    output_tokens: int,
    *,
    usd_per_m_in: float,
    usd_per_m_out: float,
) -> float:
    """Dollar cost of one model call given token counts and per-million-token prices."""
    if prompt_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    return (prompt_tokens * usd_per_m_in + output_tokens * usd_per_m_out) / 1_000_000
