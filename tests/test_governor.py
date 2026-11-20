from __future__ import annotations

import math

import pytest

from antigravity3d.tactics.governor import PLANS, Governor, SpendPlan, plan_for, usage_cost_usd


# --- plan table -------------------------------------------------------------

@pytest.mark.parametrize(("tier_index", "label", "pools", "ceiling"), [
    (0, "clutter", (1, 2, 1), 0.15), (1, "simple", (2, 2, 2), 0.35),
])
def test_modest_tiers_use_hydrogen(tier_index, label, pools, ceiling):
    p = PLANS[tier_index]
    assert p.complexity_tier == tier_index and p.label == label
    assert (p.ortho_candidates, p.pbr_candidates, p.geometry_candidates) == pools
    assert p.builder_role == "worker"
    assert not p.allow_director and not p.paintover_loop and not p.engine_truth
    assert p.reuse_trim_sheet and p.final_render == "preview"
    assert p.ceiling_usd == ceiling


def test_standard_row():
    p = PLANS[2]
    assert p == SpendPlan(
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


def test_hero_row():
    p = PLANS[3]
    assert p == SpendPlan(
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


def test_plans_table_has_exactly_four_tiers():
    assert sorted(PLANS) == [0, 1, 2, 3]


# --- plan_for rounding and clamping -----------------------------------------

@pytest.mark.parametrize(
    "score, expected_tier",
    [
        (0.0, 0),
        (0.4, 0),
        (0.5, 1),  # half rounds up
        (1.0, 1),
        (1.49, 1),
        (1.5, 2),
        (2.0, 2),
        (2.5, 3),
        (2.49, 2),
        (3.0, 3),
    ],
)
def test_plan_for_rounds_half_up(score, expected_tier):
    assert plan_for(score).complexity_tier == expected_tier


@pytest.mark.parametrize("score", [-0.5, -10.0, -math.inf])
def test_plan_for_clamps_low(score):
    assert plan_for(score).complexity_tier == 0


@pytest.mark.parametrize("score", [3.4, 3.5, 7.0, math.inf])
def test_plan_for_clamps_high(score):
    assert plan_for(score).complexity_tier == 3


def test_plan_for_returns_table_rows():
    assert plan_for(0.2) == PLANS[0]
    assert plan_for(1.2) == PLANS[1]
    assert plan_for(2.2) == PLANS[2]
    assert plan_for(2.9) == PLANS[3]
    assert plan_for(2.9).label == "hero"


def test_plan_for_rejects_nan():
    with pytest.raises(ValueError):
        plan_for(math.nan)


# --- Governor charging and ceiling ------------------------------------------

def test_fresh_governor_state():
    g = Governor(PLANS[2])
    assert g.spent == 0.0
    assert g.by_category == {}
    assert g.remaining == pytest.approx(0.85)
    assert g.plan is PLANS[2]


def test_charge_accumulates_per_category():
    g = Governor(PLANS[3])
    g.charge("image", 0.25)
    g.charge("image", 0.15)
    g.charge("decision", 0.002)
    assert g.by_category == {"image": pytest.approx(0.40), "decision": pytest.approx(0.002)}
    assert g.spent == pytest.approx(0.402)
    assert g.remaining == pytest.approx(2.20 - 0.402)


def test_by_category_returns_a_copy():
    g = Governor(PLANS[2])
    g.charge("image", 0.1)
    snapshot = g.by_category
    snapshot["image"] = 99.0
    snapshot["other"] = 1.0
    assert g.by_category == {"image": pytest.approx(0.1)}


def test_negative_charge_raises():
    g = Governor(PLANS[2])
    with pytest.raises(ValueError):
        g.charge("image", -0.01)
    assert g.spent == 0.0


def test_non_finite_charge_raises():
    g = Governor(PLANS[2])
    with pytest.raises(ValueError):
        g.charge("image", math.nan)
    with pytest.raises(ValueError):
        g.charge("image", math.inf)


def test_zero_charge_is_allowed():
    g = Governor(PLANS[0])
    g.charge("decision", 0.0)
    assert g.by_category == {"decision": 0.0}


def test_remaining_never_negative_when_overspent():
    g = Governor(PLANS[0])  # ceiling 0.15
    g.charge("image", 0.20)
    assert g.spent == pytest.approx(0.20)
    assert g.remaining == 0.0


def test_can_afford_respects_ceiling():
    g = Governor(PLANS[0])  # ceiling 0.15
    g.charge("image", 0.10)
    assert g.can_afford(0.05)
    assert not g.can_afford(0.06)


def test_can_afford_tolerance_at_exact_ceiling():
    g = Governor(PLANS[0])
    g.charge("image", 0.10)
    assert g.can_afford(0.05)  # exactly reaches the ceiling
    assert g.can_afford(0.05 + 5e-10)  # within the 1e-9 tolerance
    assert not g.can_afford(0.05 + 1e-6)


# --- candidates -------------------------------------------------------------

@pytest.mark.parametrize(
    "kind, tier_index, expected",
    [
        ("ortho", 0, 1),
        ("pbr", 0, 2),
        ("geometry", 0, 1),
        ("ortho", 2, 4),
        ("pbr", 2, 4),
        ("geometry", 2, 4),
        ("ortho", 3, 4),
        ("pbr", 3, 6),
        ("geometry", 3, 8),
    ],
)
def test_candidates_without_unit_cost_returns_plan_value(kind, tier_index, expected):
    assert Governor(PLANS[tier_index]).candidates(kind) == expected


def test_candidates_unknown_kind_raises():
    g = Governor(PLANS[3])
    with pytest.raises(ValueError):
        g.candidates("albedo")
    with pytest.raises(ValueError):
        g.candidates("")


def test_candidates_reduced_to_what_fits():
    g = Governor(PLANS[3])  # ceiling 2.20, nothing spent
    # 8 candidates at 0.25 = 2.00 fits; the plan's 8 is kept.
    assert g.candidates("geometry", unit_cost=0.25) == 8
    # 8 * 0.30 = 2.40 does not fit; floor(2.20 / 0.30) = 7.
    assert g.candidates("geometry", unit_cost=0.30) == 7
    # 6 * 0.5 = 3.0 does not fit; floor(2.2 / 0.5) = 4.
    assert g.candidates("pbr", unit_cost=0.5) == 4


def test_candidates_reduced_by_remaining_budget():
    g = Governor(PLANS[3])
    g.charge("image", 1.20)  # remaining 1.00
    assert g.candidates("pbr", unit_cost=0.25) == 4
    assert g.candidates("ortho", unit_cost=0.25) == 4


def test_candidates_never_exceeds_plan_value():
    g = Governor(PLANS[0])  # ceiling 0.15
    assert g.candidates("pbr", unit_cost=0.001) == 2


def test_candidates_returns_zero_when_nothing_fits():
    g = Governor(PLANS[0])  # ceiling 0.15
    assert g.candidates("pbr", unit_cost=1.0) == 0
    g.charge("image", 0.15)  # budget exhausted
    assert g.candidates("ortho", unit_cost=0.01) == 0


def test_candidates_zero_unit_cost_keeps_plan_value():
    assert Governor(PLANS[3]).candidates("geometry", unit_cost=0.0) == 8


def test_candidates_negative_unit_cost_raises():
    with pytest.raises(ValueError):
        Governor(PLANS[3]).candidates("geometry", unit_cost=-0.1)


# --- director escalation ----------------------------------------------------

def test_director_never_allowed_on_clutter():
    g = Governor(PLANS[0])
    assert g.may_escalate_to_director(0.0) is False
    assert g.may_escalate_to_director(0.01) is False


def test_director_allowed_on_standard_and_hero_when_affordable():
    for tier_index in (2, 3):
        g = Governor(PLANS[tier_index])
        assert g.may_escalate_to_director(0.10) is True


def test_director_denied_when_estimate_unaffordable():
    g = Governor(PLANS[2])  # ceiling 0.85
    g.charge("image", 0.80)
    assert g.may_escalate_to_director(0.05) is True
    assert g.may_escalate_to_director(0.06) is False


def test_director_clutter_plan_denied_even_if_cheap():
    g = Governor(PLANS[1])
    assert g.may_escalate_to_director(0.0001) is False


# --- summary ----------------------------------------------------------------

def test_summary_exact_string_for_known_sequence():
    g = Governor(PLANS[3])
    g.charge("builder", 0.0291)
    g.charge("image", 0.4000)
    g.charge("decision", 0.0021)
    assert g.summary() == "hero: $0.4312 of $2.20 (builder $0.0291, decision $0.0021, image $0.4000)"


def test_summary_sorts_categories_by_name():
    g = Governor(PLANS[2])
    g.charge("zeta", 0.1)
    g.charge("alpha", 0.2)
    assert g.summary() == "standard: $0.3000 of $0.85 (alpha $0.2000, zeta $0.1000)"


def test_summary_with_no_spend():
    assert Governor(PLANS[0]).summary() == "clutter: $0.0000 of $0.15 ()"


def test_summary_is_one_line():
    g = Governor(PLANS[3])
    g.charge("image", 0.1)
    assert "\n" not in g.summary()


# --- usage cost helper ------------------------------------------------------

def test_usage_cost_usd_basic():
    cost = usage_cost_usd(1_000_000, 500_000, usd_per_m_in=2.0, usd_per_m_out=8.0)
    assert cost == pytest.approx(2.0 + 4.0)


def test_usage_cost_usd_small_call():
    cost = usage_cost_usd(1200, 300, usd_per_m_in=0.5, usd_per_m_out=1.5)
    assert cost == pytest.approx((1200 * 0.5 + 300 * 1.5) / 1_000_000)


def test_usage_cost_usd_zero_tokens():
    assert usage_cost_usd(0, 0, usd_per_m_in=3.0, usd_per_m_out=15.0) == 0.0


def test_usage_cost_usd_rejects_negative_tokens():
    with pytest.raises(ValueError):
        usage_cost_usd(-1, 0, usd_per_m_in=1.0, usd_per_m_out=1.0)
    with pytest.raises(ValueError):
        usage_cost_usd(0, -1, usd_per_m_in=1.0, usd_per_m_out=1.0)


# --- waiting admission ------------------------------------------------------

import asyncio  # noqa: E402
import random  # noqa: E402
import threading  # noqa: E402
from dataclasses import replace  # noqa: E402

from antigravity3d.tactics.governor import BudgetExceeded  # noqa: E402


def ledger_committed(g: Governor) -> float:
    return g._db.execute("SELECT COALESCE(SUM(COALESCE(booked,reserved)),0) FROM spend").fetchone()[0]


def gov(ceiling: float) -> Governor:
    return Governor(replace(PLANS[3], ceiling_usd=ceiling))


async def settle_soon():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_reserve_wait_admits_at_once_when_it_fits():
    g = gov(1.0)
    hold = await asyncio.wait_for(g.reserve_wait("x", 0.4), 1)
    assert hold.limit == 0.4 and g.remaining == pytest.approx(0.6) and not g._waiters


async def test_reserve_wait_waits_for_a_settlement_then_admits():
    g = gov(1.0)
    first = g.reserve("x", 0.7)
    waiter = asyncio.ensure_future(g.reserve_wait("x", 0.6))
    await settle_soon()
    assert not waiter.done() and len(g._waiters) == 1
    first.settle(0.1)  # most of the reservation is released
    hold = await asyncio.wait_for(waiter, 1)
    assert hold.limit == 0.6 and ledger_committed(g) == pytest.approx(0.7) and not g._waiters


async def test_reserve_wait_never_fitting_fails_at_once_with_the_numbers():
    g = gov(1.0)
    g.charge("image", 0.5)
    outstanding = g.reserve("x", 0.3)  # unsettled reservations do not make a request unfittable, booked spend does
    with pytest.raises(BudgetExceeded, match=r"never fit.*ceiling \$1\.00000.*\$0\.50000 already settled"):
        await asyncio.wait_for(g.reserve_wait("y", 0.6), 1)
    assert not g._waiters
    outstanding.settle(0.0)


async def test_overrun_that_makes_a_waiter_unfittable_fails_it_instead_of_hanging():
    g = gov(1.0)
    first = g.reserve("x", 0.6)
    waiter = asyncio.ensure_future(g.reserve_wait("x", 0.5))
    await settle_soon()
    first.settle(0.8)  # billed more than reserved
    with pytest.raises(BudgetExceeded, match="never fit"):
        await asyncio.wait_for(waiter, 1)
    assert not g._waiters


async def test_sync_reserve_still_fails_at_once():
    g = gov(1.0)
    g.reserve("x", 0.7)
    with pytest.raises(BudgetExceeded):
        g.reserve("y", 0.6)


async def test_cancelled_waiter_leaves_no_trace_and_hands_the_queue_on():
    g = gov(1.0)
    first = g.reserve("x", 0.9)
    head = asyncio.ensure_future(g.reserve_wait("a", 0.5))
    behind = asyncio.ensure_future(g.reserve_wait("b", 0.5))
    await settle_soon()
    assert len(g._waiters) == 2
    head.cancel()
    with pytest.raises(asyncio.CancelledError):
        await head
    assert len(g._waiters) == 1
    first.settle(0.0)
    hold = await asyncio.wait_for(behind, 1)
    assert hold.limit == 0.5 and not g._waiters and ledger_committed(g) == pytest.approx(0.5)


async def test_waiters_are_served_in_arrival_order():
    g = gov(1.0)
    first = g.reserve("x", 1.0)
    order: list[str] = []

    async def take(name, usd):
        hold = await g.reserve_wait(name, usd)
        order.append(name)
        return hold

    big = asyncio.ensure_future(take("big", 0.8))
    await settle_soon()
    small = asyncio.ensure_future(take("small", 0.1))  # would fit before big does, but arrived later
    await settle_soon()
    first.settle(0.0)
    holds = await asyncio.wait_for(asyncio.gather(big, small), 1)
    assert order == ["big", "small"] and ledger_committed(g) == pytest.approx(0.9) and len(holds) == 2


def test_settlement_from_a_worker_thread_wakes_a_waiter_on_another_loop():
    g = gov(1.0)
    first = g.reserve("x", 0.9)
    ready = threading.Event()

    async def waiter():
        task = asyncio.ensure_future(g.reserve_wait("w", 0.5))
        await settle_soon()
        ready.set()
        return await asyncio.wait_for(task, 5)

    threading.Timer(0.05, lambda: (ready.wait(5), first.settle(0.0))).start()
    hold = asyncio.run(waiter())
    assert hold.limit == 0.5 and not g._waiters


async def test_ceiling_is_never_exceeded_while_many_reservers_race_with_threads():
    g = gov(1.0)
    rng = random.Random(7)
    peak = 0.0
    lock = threading.Lock()

    def note():
        nonlocal peak
        with lock:
            peak = max(peak, ledger_committed(g))

    async def work(i):
        limit = rng.choice([0.2, 0.35, 0.5, 0.45])
        hold = await g.reserve_wait("x", limit)
        note()
        await asyncio.sleep(rng.random() * 0.01)
        # settle from a worker thread half of the time
        billed = limit * rng.choice([0.2, 0.5, 1.0])
        if i % 2:
            await asyncio.to_thread(hold.settle, billed)
        else:
            hold.settle(billed)
        note()
        return billed

    billed = await asyncio.wait_for(asyncio.gather(*(work(i) for i in range(40)), return_exceptions=True), 30)
    ok = [b for b in billed if isinstance(b, float)]
    failed = [b for b in billed if isinstance(b, BudgetExceeded)]  # only once settled spend left too little room
    assert len(ok) + len(failed) == 40 and peak <= 1.0 + 1e-9
    assert ledger_committed(g) <= 1.0 + 1e-9 and not g._waiters
    assert g.spent == pytest.approx(sum(ok))


async def test_waiters_are_not_leaked_when_the_loop_of_a_waiter_has_closed():
    g = gov(1.0)
    first = g.reserve("x", 0.9)
    loop = asyncio.new_event_loop()
    def park():
        with pytest.raises(TimeoutError):
            loop.run_until_complete(asyncio.wait_for(g.reserve_wait("w", 0.5), 0.2))

    t = threading.Thread(target=park, daemon=True)
    t.start()
    t.join()  # the waiter timed out and was cancelled
    loop.close()
    assert not g._waiters
    first.settle(0.0)  # waking with nobody parked must not raise


def test_candidates_sized_by_expected_cost_admit_the_last_one_by_its_worst_case():
    g = gov(2.2)
    assert g.candidates("geometry", unit_cost=0.3) == 7  # floor(2.2 / 0.3), the old rule
    # expected 0.3, worst case 0.5: 1 + floor((2.2 - 0.5) / 0.3) = 6, capped by the plan's 8
    assert g.candidates("geometry", unit_cost=0.3, admit_cost=0.5) == 6
    assert g.candidates("geometry", unit_cost=0.05, admit_cost=0.5) == 8
    assert g.candidates("geometry", unit_cost=0.3, admit_cost=2.5) == 0
