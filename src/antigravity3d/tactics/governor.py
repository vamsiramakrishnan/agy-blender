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
    # How hard pass A tries for a better design once it passes its checks (tactics.pyramid, improvement rounds): at most
    # ``improve_rounds`` rounds, ending once score_brief_depiction reaches ``improve_target`` or after ``improve_patience``
    # rounds in a row that did not raise the progress figure (each such round is rolled back). ``improve_role``: the
    # author tier of those rounds (None: ``builder_role``). Defaults keep a plan persisted before these fields loadable
    # (dags.routing) and mean "no improvement rounds".
    improve_rounds: int = 0
    improve_target: float = 4.0
    improve_patience: int = 1
    improve_role: str | None = None
    # How improvement rounds are validated (tactics.pyramid): each round is kept provisionally unless there is evidence it
    # made things worse; every ``improve_validate_every`` provisional rounds, and at the end, the design is judged
    # ``improve_judgements`` times in parallel and the rounds stand only if the mean depiction rose by the band for that
    # many judgements against the best validated state (else that state is restored).
    improve_judgements: int = 3
    improve_validate_every: int = 2
    # Look rounds (tactics.look_round): after the design is final and before its package is written, the asset is
    # rendered in colour as it ships, the judge is asked about its look and the author may change its materials. 0: no
    # look round, and the run is what it was. One in every built-in plan: a second can only help after a first was kept
    # (the author then sees renders of its own change) and is left for the live measurement to justify.
    look_rounds: int = 0


# score_brief_depiction an improvement round aims for: midway between level 3 ("convincing: every named part present
# and recognisable, believable proportions, some secondary detail; a few details a real one has are missing") and level
# 4 ("complete and convincing ... it stands as a finished game prop"), decisions.questions.BRIEF_DEPICTION_LEVELS.
# PROVISIONAL, as are the round counts below: chosen from the level texts until the live curve of score per round exists.
IMPROVE_TARGET = 3.5

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
    improve_rounds=1,
    improve_target=IMPROVE_TARGET,
    look_rounds=1,
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
    improve_rounds=3,
    improve_target=IMPROVE_TARGET,
    look_rounds=1,
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
    improve_rounds=5,
    improve_target=IMPROVE_TARGET,
    look_rounds=1,
)

PLANS: dict[int, SpendPlan] = {
    0: replace(_CLUTTER, complexity_tier=0),
    1: replace(_CLUTTER, complexity_tier=1, label="simple", ortho_candidates=2, geometry_candidates=2, ceiling_usd=0.35,
               improve_rounds=2),
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


USD, TOKENS = "usd", "tokens"
# Recorded on every token-unit receipt and summary: a token figure is never a price.
NO_PRICE_NOTE = "no price applied"


@dataclass(frozen=True)
class Reservation:
    governor: Governor
    id: str
    limit: float
    images: int = 0

    def settle(self, usd: float | None = None, *, receipt: dict | None = None, images: int | None = None) -> float:
        """Record the billed amount (in the ledger's unit; the name ``usd`` is kept for the money callers), or retain
        the full reservation for an ambiguous response. ``images``: generated images to keep counted (None keeps the
        reserved count: an outcome that is not known to have produced no image counts as one)."""
        booked = self.limit if usd is None else _amount(usd)
        kept = self.images if images is None else max(0, min(self.images, int(images)))
        db = self.governor._db
        with self.governor._lock, db:
            changed = db.execute(
                "UPDATE spend SET booked=?, receipt=?, images=? WHERE id=? AND booked IS NULL",
                (booked, json.dumps(receipt) if receipt else None, kept, self.id),
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
    """Atomic per-asset spend ledger. An optional SQLite path survives process restarts.

    Unit. By default the ledger is in US dollars and its ceiling is the plan's ``ceiling_usd``. With ``token_ceiling``
    it is in tokens (input + output, thinking included) and the ceiling is that number: no price is applied anywhere,
    the plan's dollar ceiling is not used, and every receipt and summary says so. A journal records its unit and cannot
    be reopened in the other one. ``max_images`` is a second, hard limit kept in the same ledger: the number of
    generated images, reserved before each image call and kept when the call's outcome is unknown.
    """

    def __init__(self, plan: SpendPlan, *, journal: Path | None = None, token_ceiling: int | None = None,
                 max_images: int | None = None) -> None:
        self._plan = plan
        if token_ceiling is not None and (int(token_ceiling) != token_ceiling or token_ceiling <= 0):
            raise ValueError(f"token_ceiling must be a positive whole number of tokens, got {token_ceiling!r}")
        if max_images is not None and (int(max_images) != max_images or max_images < 0):
            raise ValueError(f"max_images must be a non-negative whole number, got {max_images!r}")
        self.unit = TOKENS if token_ceiling is not None else USD
        self.token_ceiling = None if token_ceiling is None else int(token_ceiling)
        self.max_images = None if max_images is None else int(max_images)
        if journal is not None:
            journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal = journal
        self._db = sqlite3.connect(journal or ":memory:", check_same_thread=False)
        weakref.finalize(self, self._db.close)
        self._lock = threading.RLock()
        self._waiters: list[_Waiter] = []  # FIFO of parked reserve_wait calls; guarded by self._lock
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS spend (id TEXT PRIMARY KEY, category TEXT NOT NULL, "
                "reserved REAL NOT NULL, booked REAL, receipt TEXT, images INTEGER NOT NULL DEFAULT 0)"
            )
            if "images" not in {row[1] for row in self._db.execute("PRAGMA table_info(spend)")}:  # a journal from before
                self._db.execute("ALTER TABLE spend ADD COLUMN images INTEGER NOT NULL DEFAULT 0")
            self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = self._db.execute("SELECT value FROM meta WHERE key='unit'").fetchone()
            used = self._db.execute("SELECT COUNT(*) FROM spend").fetchone()[0]
            recorded = row[0] if row else (USD if used else None)  # rows without a recorded unit predate tokens: dollars
            if recorded is not None and recorded != self.unit:
                raise ValueError(f"spend journal {journal} is kept in {recorded}; it cannot be reopened in {self.unit}")
            if row is None:
                self._db.execute("INSERT INTO meta VALUES ('unit', ?)", (self.unit,))

    @property
    def plan(self) -> SpendPlan:
        return self._plan

    @property
    def ceiling(self) -> float:
        """The ceiling in the ledger's unit."""
        return float(self.token_ceiling) if self.unit == TOKENS else self._plan.ceiling_usd

    def fmt(self, amount: float) -> str:
        """An amount in the ledger's unit, written so that it cannot be read as the other unit."""
        return f"{amount:,.0f} tokens" if self.unit == TOKENS else f"${amount:.5f}"

    def key(self, name: str) -> str:
        """``name`` with the ledger's unit as suffix (``spent_usd`` / ``spent_tokens``)."""
        return f"{name}_{self.unit}"

    def est(self, *, usd: float, tokens: float) -> float:
        """A planning estimate in the ledger's unit."""
        return tokens if self.unit == TOKENS else usd

    @property
    def images_used(self) -> int:
        """Images counted: settled ones plus those held by open reservations."""
        with self._lock:
            return int(self._db.execute("SELECT COALESCE(SUM(images),0) FROM spend").fetchone()[0])

    def _images_fit(self, images: int) -> bool:
        return self.max_images is None or self.images_used + images <= self.max_images

    def _try_insert(self, category: str, limit: float, receipt: dict | None, images: int = 0) -> Reservation | None:
        """Atomically book ``limit`` (and ``images``) against the ceilings, or return None when it does not fit now."""
        reservation = Reservation(self, uuid.uuid4().hex, limit, images)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if not self.can_afford(limit) or not self._images_fit(images):
                return None
            self._db.execute("INSERT INTO spend VALUES (?, ?, ?, NULL, ?, ?)",
                             (reservation.id, category, limit, json.dumps(receipt) if receipt else None, images))
        return reservation

    def _image_refusal(self, category: str, images: int) -> BudgetExceeded:
        return BudgetExceeded(f"{category} needs {images} image(s): {self.images_used} of {self.max_images} images already counted")

    def reserve(self, category: str, usd: float, *, receipt: dict | None = None, images: int = 0) -> Reservation:
        """Reserve now or raise :class:`BudgetExceeded`. Never waits. ``usd`` is in the ledger's unit."""
        limit = _amount(usd)
        reservation = self._try_insert(category, limit, receipt, images)
        if reservation is None:
            if not self._images_fit(images):
                raise self._image_refusal(category, images)
            raise BudgetExceeded(f"{category} needs {self.fmt(limit)}, {self.fmt(self.remaining)} available")
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

    async def reserve_wait(self, category: str, usd: float, *, receipt: dict | None = None, images: int = 0) -> Reservation:
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
                    ceiling = self.ceiling
                    if limit > ceiling - booked + _TOLERANCE:
                        f = self.fmt
                        raise BudgetExceeded(
                            f"{category} needs {f(limit)} but can never fit: ceiling {f(ceiling)}, "
                            f"{f(booked)} already settled, {f(max(0.0, ceiling - booked))} at most available")
                    if self.max_images is not None and images > self.max_images - self._settled_images():
                        raise self._image_refusal(category, images)
                    if first:
                        reservation = self._try_insert(category, limit, receipt, images)
                        if reservation is not None:
                            if queued:
                                self._waiters.remove(waiter)
                                queued = False
                            handoff = bool(self._waiters)
                            break
                        if outstanding == 0:  # unreachable: nothing to wait for and it does not fit
                            raise BudgetExceeded(f"{category} needs {self.fmt(limit)}, {self.fmt(self.remaining)} available")
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

    def _settled_images(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COALESCE(SUM(images),0) FROM spend WHERE booked IS NOT NULL").fetchone()[0])

    def charge(self, category: str, usd: float) -> None:
        amount = _amount(usd)
        with self._lock, self._db:
            self._db.execute("INSERT INTO spend (id, category, reserved, booked, receipt) VALUES (?, ?, 0, ?, NULL)",
                             (uuid.uuid4().hex, category, amount))

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
        return max(0.0, self.ceiling - committed)

    def can_afford(self, usd: float) -> bool:
        return _amount(usd) <= self.remaining + _TOLERANCE

    def receipts(self) -> list[dict]:
        with self._lock:
            records = self._db.execute(
                "SELECT id,category,reserved,booked,receipt,images FROM spend WHERE receipt IS NOT NULL ORDER BY rowid").fetchall()
        if self.unit == USD:
            out = [{**json.loads(payload), "reservation_id": key, "category": cat, "reserved_usd": reserved, "booked_usd": booked,
                    "overrun_usd": max(0.0, booked - reserved) if booked is not None else None}
                   for key, cat, reserved, booked, payload, _ in records]
        else:  # token ledger: no *_usd key, and every receipt says that no price was applied
            out = []
            for key, cat, reserved, booked, payload, _ in records:
                rec = {k: v for k, v in json.loads(payload).items() if not k.endswith("_usd")}
                out.append({**rec, "reservation_id": key, "category": cat, "unit": TOKENS, "price_applied": False,
                            "reserved_tokens": int(reserved), "booked_tokens": None if booked is None else int(booked),
                            "overrun_tokens": max(0, int(booked - reserved)) if booked is not None else None})
        if self.max_images is not None:
            for rec, row in zip(out, records):
                rec["images"] = int(row[5])
        return out

    def candidates(self, kind: str, *, unit_cost: float | None = None, admit_cost: float | None = None,
                   images_each: int = 0) -> int:
        """Best-of-N for ``kind``, reduced so N * unit_cost fits the remaining budget.

        Without ``unit_cost`` the plan's N is returned unchanged. The result is zero when a single candidate does
        not fit. ``unit_cost`` is what a candidate is *expected* to cost. ``admit_cost`` (default ``unit_cost``) is
        what admission must be able to reserve for one candidate, its worst case; candidates run one after another
        under the ceiling, so the last one is admitted when the others have settled near ``unit_cost`` and N is the
        largest value with ``(N - 1) * unit_cost + admit_cost <= remaining``. ``images_each``: images one candidate
        generates; with an image limit, N never asks for more images than remain under it.
        """
        if kind not in _KINDS:
            raise ValueError(f"unknown candidate kind {kind!r}; expected one of {sorted(_KINDS)}")
        base = getattr(self._plan, _KINDS[kind])
        if images_each > 0 and self.max_images is not None:  # each candidate generates images_each images
            base = min(base, max(0, self.max_images - self.images_used) // images_each)
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

    def allocate(self, kind: str, k: int, *, unit_cost: float | None = None, images_each: int = 0) -> list[int]:
        """Best-of-N for each of ``k`` items (ordered by priority, first = most important), sharing what is left.

        Every item gets one candidate before any gets a second, up to the plan's N each, while the total fits the
        remaining budget (``total * unit_cost <= remaining``, the rule of :meth:`candidates`) and the image limit (each
        candidate generates ``images_each`` images). When everything fits, every item gets the plan's N, as
        ``candidates`` would give each; when not, the leftover goes to the earlier items instead of being lost to a
        uniform N that is zero as soon as one round does not fit for all."""
        if kind not in _KINDS:
            raise ValueError(f"unknown candidate kind {kind!r}; expected one of {sorted(_KINDS)}")
        if k <= 0:
            return []
        per = getattr(self._plan, _KINDS[kind])
        total = per * k
        if images_each > 0 and self.max_images is not None:
            total = min(total, max(0, self.max_images - self.images_used) // images_each)
        if unit_cost is not None and unit_cost < 0:
            raise ValueError(f"costs must be non-negative, got unit_cost={unit_cost!r}")
        if unit_cost:
            room = self.remaining + _TOLERANCE - unit_cost
            total = 0 if room < 0 else min(total, 1 + math.floor(room / unit_cost))
        counts = [0] * k
        for i in range(total):  # round robin: one each, then a second each, ...
            counts[i % k] += 1
        return counts

    def may_escalate_to_director(self, estimated_usd: float) -> bool:
        return self._plan.allow_director and self.can_afford(estimated_usd)

    def images_summary(self) -> str:
        return "" if self.max_images is None else f"{self.images_used} of {self.max_images} images"

    def summary(self) -> str:
        imgs = f"; {self.images_summary()}" if self.max_images is not None else ""
        if self.unit == TOKENS:
            parts = ", ".join(f"{name} {amount:,.0f}" for name, amount in sorted(self.by_category.items()))
            return f"{self._plan.label}: {self.spent:,.0f} of {self.token_ceiling:,} tokens ({NO_PRICE_NOTE}){imgs} ({parts})"
        parts = ", ".join(f"{name} ${amount:.4f}" for name, amount in sorted(self.by_category.items()))
        return f"{self._plan.label}: ${self.spent:.4f} of ${self._plan.ceiling_usd:.2f}{imgs} ({parts})"


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
