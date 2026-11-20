"""Provider boundary tests. Prices, usage and model outputs here are deterministic fixtures."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from antigravity3d.models import tier
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import BudgetExceeded, Governor, plan_for
from antigravity3d.tactics.metered_author import MeteredAuthor


class Endpoint:
    def __init__(self):
        self.counted = []
        self.generated = []
        self.answer = '{"asset_name":"Crate","nodes":[{"id":"body","kind":"box","params":{"size":[1,1,1]}}]}'

    async def count_tokens(self, **kwargs):
        self.counted.append(kwargs)
        return SimpleNamespace(total_tokens=100)

    async def generate_content(self, **kwargs):
        self.generated.append(kwargs)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return SimpleNamespace(text=self.answer, usage_metadata=SimpleNamespace(prompt_token_count=100,
                               candidates_token_count=30, thoughts_token_count=20, total_token_count=150))


def make_author(monkeypatch, ceiling=1):
    endpoint = Endpoint()
    budget = EngineBudget("godot_4", max_tris=347)
    governor = Governor(replace(plan_for(3), ceiling_usd=ceiling))
    context = {"asset_name": "Crate", "ortho_uri": "https://example.test/ortho",
               "style_uri": "https://example.test/style"}
    rates = {tier(role).model: [1, 2] for role in (governor.plan.builder_role, "director")}
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", json.dumps(rates))
    author = MeteredAuthor(SimpleNamespace(aio=SimpleNamespace(models=endpoint)), governor, budget, context, timeout_s=5)
    return author, endpoint, budget, context, governor


async def test_proposal_and_director_repair_use_exact_inputs_and_target(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    [scene] = await a.propose("green crate", budget=budget, n=1, context=context)
    ep.answer = '{"ops":[{"op":"remove_node","node_id":"body"}]}'
    patch = await a.repair(scene, action=SimpleNamespace(kind="escalate_director", reasons=["floating"]),
                           located={"defect_floating": ["body"]}, manifest="body")
    assert patch.ops[0].node_id == "body"
    assert [x["model"] for x in ep.generated] == [tier(gov.plan.builder_role).model, tier("director").model]
    for counted, generated in zip(ep.counted, ep.generated, strict=True):
        assert counted["contents"] == generated["contents"] and counted["model"] == generated["model"]
        parts = generated["contents"][0].parts
        assert "godot_4" in parts[0].text and "347" in parts[0].text
        assert [p.file_data.file_uri for p in parts if p.file_data] == [context["ortho_uri"], context["style_uri"]]
        assert generated["config"].http_options.retry_options.attempts == 1
    assert gov.spent == pytest.approx(2 * (100 + 2 * 50) / 1_000_000)
    assert [r["output_tokens"] for r in gov.receipts()] == [50, 50]


async def test_insufficient_funds_and_unknown_prices_make_zero_generation_requests(monkeypatch):
    a, ep, budget, context, _ = make_author(monkeypatch, ceiling=0.0001)
    with pytest.raises(BudgetExceeded):
        await a.propose("crate", budget=budget, n=3, context=context)
    assert len(ep.generated) == 0
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", "{}")
    with pytest.raises(BudgetExceeded, match="RATES_JSON"):
        await a.propose("crate", budget=budget, n=1, context=context)
    assert len(ep.counted) == 3 and not ep.generated


async def test_cancelled_or_unparseable_output_charged(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    ep.answer = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await a.propose("crate", budget=budget, n=1, context=context)
    [rec] = gov.receipts()
    assert rec["booked_usd"] == rec["reserved_usd"] and not rec["usage_known"]
    ep.answer = "garbled"
    with pytest.raises(ValueError):
        await a.propose("crate", budget=budget, n=1, context=context)
    assert gov.spent > rec["booked_usd"]
