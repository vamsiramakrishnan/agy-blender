from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from antigravity3d.dags.routing import resolve_route
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.state import EngineBudget


async def test_route_resume_preserves_plan_and_isolates_brief_budget_style(tmp_path):
    client = FakeDecisionsClient(answers={"complexity_tier": 3.0}, input_tokens=360, latency_s=.005)
    plane = DecisionControlPlane(client, backend_kind="decisions")
    budget = EngineBudget("unreal_5")

    async def route(prompt="oak barrel", target=budget, style=b"a"):
        return await resolve_route(tmp_path, prompt, target, style, single_candidate=False, plane=plane)

    first, concurrent = await asyncio.gather(route(), route())
    assert len(client.calls) == 1 and {first.reused, concurrent.reused} == {True, False}
    first.governor.charge("hero_generation", .60)  # exceeds the initial routing tier
    resumed = await route()
    assert len(client.calls) == 1 and resumed.reused and resumed.cost_usd == 0
    assert resumed.plan == first.plan and resumed.plan.complexity_tier == 3
    assert resumed.governor.spent == pytest.approx(first.governor.spent)
    changed = [await route(prompt="different asset"), await route(target=replace(budget, max_tris=200)),
               await route(style=b"b")]
    assert all(not r.reused and r.governor.spent < .001 for r in changed)
    assert len({r.governor.journal for r in [first, *changed]}) == 4
