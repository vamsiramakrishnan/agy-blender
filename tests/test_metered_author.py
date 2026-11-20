"""Provider boundary tests. Prices, usage and model outputs here are deterministic fixtures."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from google.genai import types

from antigravity3d.models import tier
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import BudgetExceeded, Governor, plan_for
from antigravity3d import ir_schema
from antigravity3d.tactics.metered_author import (
    AUTHOR_OUTPUT_BUDGETS, MEASURED_THINKING_TOKENS, MODEL_OUTPUT_TOKEN_LIMIT, AuthorNoAnswer, AuthorTruncated,
    MeteredAuthor, build_author_request, propose_task, schema_input_token_bound,
)


class Endpoint:
    def __init__(self):
        self.counted = []
        self.generated = []
        # A schema-conforming answer: the request carries response_json_schema, which requires transform/materials.
        self.answer = ('{"asset_name":"Crate","materials":[],"nodes":[{"id":"body","kind":"box","params":{"size":[1,1,1]},'
                       '"transform":{"translate":[0,0,0.5]}}]}')
        self.finish = "STOP"
        self.usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=30, thoughts_token_count=20,
                                     total_token_count=150)

    async def count_tokens(self, **kwargs):
        self.counted.append(kwargs)
        return SimpleNamespace(total_tokens=100)

    async def generate_content(self, **kwargs):
        self.generated.append(kwargs)
        if isinstance(self.answer, BaseException):
            raise self.answer
        candidates = [] if self.finish is None else [SimpleNamespace(finish_reason=types.FinishReason(self.finish))]
        return SimpleNamespace(text=self.answer, usage_metadata=self.usage, candidates=candidates, prompt_feedback=None)


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


# --- request shape, budgets and outcomes --------------------------------------------------------------------------


def cost(inputs, outputs, rates=(1, 2)):
    return (inputs * rates[0] + outputs * rates[1]) / 1_000_000


async def test_dispatched_request_is_the_shared_builders_request(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    await a.propose("green crate", budget=budget, n=1, context=context)
    role = gov.plan.builder_role
    [sent] = ep.generated
    task = propose_task("green crate", index=0, n=1, asset_name="Crate", references=True)
    expected = build_author_request(role, "propose", task, budget=budget, context=context, timeout_s=5, asset_name="Crate")
    assert sent["contents"] == expected.contents and sent["config"] == expected.config
    assert sent["config"].response_json_schema == ir_schema.scene_response_schema("Crate")
    assert sent["config"].max_output_tokens == AUTHOR_OUTPUT_BUDGETS[(role, "propose")].max_output_tokens
    assert sent["config"].thinking_config.thinking_level.name == tier(role).thinking.name
    assert sent["contents"][0].parts[0].text.startswith(ir_schema.ir_rules_text())


async def test_repair_sends_the_patch_schema_and_the_directors_budget(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    scene = (await a.propose("crate", budget=budget, n=1, context=context))[0]
    ep.answer = '{"ops":[{"op":"set_param","node_id":"body","name":"size","value":[1,1,2]}]}'
    patch = await a.repair(scene, action=SimpleNamespace(kind="escalate_director", reasons=["short"]), located={}, manifest="")
    assert patch.ops[0].value == (1, 1, 2)
    cfg = ep.generated[-1]["config"]
    assert cfg.response_json_schema == ir_schema.patch_response_schema()
    assert cfg.max_output_tokens == AUTHOR_OUTPUT_BUDGETS[("director", "repair")].max_output_tokens


async def test_reservation_covers_counted_input_schema_bound_and_the_whole_output_cap(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    await a.propose("crate", budget=budget, n=1, context=context)
    [rec] = gov.receipts()
    out = AUTHOR_OUTPUT_BUDGETS[(gov.plan.builder_role, "propose")]
    bound = schema_input_token_bound(ir_schema.scene_response_schema("Crate"))
    assert rec["reserved_usd"] == pytest.approx(cost(100 + bound, out.max_output_tokens))
    assert rec["max_output_tokens"] == out.answer_tokens + out.thinking_tokens
    assert rec["booked_usd"] == pytest.approx(cost(100, 50)) and rec["finish_reason"] == "STOP"


def test_schema_input_bound_covers_the_measured_schema_tokens():
    # Live: prompt_token_count 6,045 with the propose schema vs count_tokens 960 for the same contents (+5,085).
    assert schema_input_token_bound(ir_schema.scene_response_schema("WoodenCrateWithIron")) >= 5_085


def test_output_budget_table_fits_each_model_and_measured_thinking():
    for (role, op), out in AUTHOR_OUTPUT_BUDGETS.items():
        mt = tier(role)
        assert out.max_output_tokens <= MODEL_OUTPUT_TOKEN_LIMIT[mt.model], (role, op)
        seen = MEASURED_THINKING_TOKENS.get(f"{mt.model}/{mt.thinking.value}")
        if seen:
            assert out.thinking_tokens >= 2 * max(seen), (role, op, seen)
    assert {r for r, _ in AUTHOR_OUTPUT_BUDGETS} >= {"worker", "bulk", "builder", "director"}
    by = {r: AUTHOR_OUTPUT_BUDGETS[(r, "propose")].thinking_tokens for r in ("worker", "builder", "director")}
    assert by["worker"] < by["builder"] < by["director"]  # a hero call is never sized by a worker-tier assumption


async def test_plan_that_cannot_admit_its_tier_fails_loudly_with_the_numbers(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", json.dumps({tier("director").model: [1, 100]}))
    with pytest.raises(BudgetExceeded) as e:
        await a.propose("crate", budget=budget, n=1, context=context)
    out = AUTHOR_OUTPUT_BUDGETS[("director", "propose")]
    msg = str(e.value)
    assert "ceiling $1.00" in msg and f"{out.max_output_tokens} output tokens" in msg and f"{out.thinking_tokens} thinking" in msg
    assert not ep.generated and not gov.receipts()


async def test_truncation_is_a_typed_error_and_books_the_billed_usage(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    ep.finish, ep.answer = "MAX_TOKENS", '{"asset_name":"Crate","materials":[],"nodes":[{"id":"bo'
    ep.usage = SimpleNamespace(prompt_token_count=6000, candidates_token_count=3, thoughts_token_count=4093,
                               total_token_count=10096)
    with pytest.raises(AuthorTruncated) as e:
        await a.propose("crate", budget=budget, n=1, context=context)
    assert e.value.finish_reason == "MAX_TOKENS"
    assert e.value.usage == {"input_tokens": 6000, "output_tokens": 4096, "thinking_tokens": 4093}
    [rec] = gov.receipts()
    assert rec["status"] == "AuthorTruncated" and rec["finish_reason"] == "MAX_TOKENS" and rec["usage_known"]
    assert rec["booked_usd"] == pytest.approx(cost(6000, 4096)) and gov.spent == pytest.approx(cost(6000, 4096))


@pytest.mark.parametrize("finish,answer,blocked", [
    ("SAFETY", "", False), ("RECITATION", '{"asset_name":"Crate"}', False), (None, None, False), ("STOP", "", False),
    ("STOP", None, True),
])
async def test_no_usable_candidate_is_a_typed_error_and_settles(monkeypatch, finish, answer, blocked):
    a, ep, budget, context, gov = make_author(monkeypatch)
    ep.finish, ep.answer = finish, answer
    if blocked:
        real = ep.generate_content

        async def blocked_call(**kw):
            r = await real(**kw)
            r.prompt_feedback = SimpleNamespace(block_reason=types.BlockedReason.PROHIBITED_CONTENT)
            r.candidates = []
            return r
        ep.generate_content = blocked_call
    with pytest.raises(AuthorNoAnswer) as e:
        await a.propose("crate", budget=budget, n=1, context=context)
    assert e.value.usage["input_tokens"] == 100
    [rec] = gov.receipts()
    assert rec["booked_usd"] == pytest.approx(cost(100, 50)) and rec["status"] == "AuthorNoAnswer"
