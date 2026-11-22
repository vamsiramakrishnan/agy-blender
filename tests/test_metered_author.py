"""Provider boundary tests. Prices, usage and model outputs here are deterministic fixtures."""

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from google.genai import types

from antigravity3d.models import tier
from stream_fakes import StreamFromBlocking  # noqa: E402 - tests/ is on sys.path (rootdir conftest layout)
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import BudgetExceeded, Governor, plan_for
from antigravity3d import ir_schema
from antigravity3d.tactics.metered_author import (
    AUTHOR_OUTPUT_BUDGETS, KIT_OUTPUT_BUDGETS, MEASURED_CRAFT_ANSWER_TOKENS, MEASURED_CRAFT_INPUT_TOKENS, MEASURED_CRAFT_THINKING_TOKENS,
    MEASURED_CRAFT_PROPOSE_WALL_S, MEASURED_ANSWER_TOKENS, MEASURED_THINKING_TOKENS, MODEL_OUTPUT_TOKEN_LIMIT, AuthorNoAnswer, AuthorTruncated,
    MeteredAuthor, build_author_request, propose_task, schema_input_token_bound,
)


class Endpoint(StreamFromBlocking):  # MeteredAuthor streams: the scripted answer is delivered as a stream
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
    # The call is streamed with thought summaries, which are added at send time (stream_call.with_thought_summaries) and
    # are the only difference from the shared builder's request: the request itself, and so the design record's key and
    # the benchmark's bytes, do not carry them.
    from antigravity3d.tactics.stream_call import with_thought_summaries

    assert sent["contents"] == expected.contents and sent["config"] == with_thought_summaries(expected.config)
    assert sent["config"].thinking_config.include_thoughts is True and expected.config.thinking_config.include_thoughts is None
    assert sent["config"].response_json_schema == ir_schema.scene_response_schema("Crate")
    assert sent["config"].max_output_tokens == AUTHOR_OUTPUT_BUDGETS[role].max_output_tokens
    assert sent["config"].thinking_config.thinking_level.name == tier(role).thinking.name
    assert sent["contents"][0].parts[0].text.startswith(ir_schema.ir_rules_text())


def _task_text(req):
    return "".join(p.text or "" for c in req.contents for p in c.parts)


async def test_prop_proposals_carry_the_craft_guidance_after_the_task_and_kit_pieces_and_repairs_do_not(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    await a.propose("green crate", budget=budget, n=2, context=context)
    for sent in ep.generated:
        text = sent["contents"][0].parts[0].text
        assert PROP_CRAFT_GUIDANCE in text and text.index("Include material assignments.") < text.index(PROP_CRAFT_GUIDANCE)
        assert text.endswith(PROP_CRAFT_GUIDANCE)
    # a kit piece (its context carries the metric): the request is the one sent before the guidance existed, byte for byte
    from antigravity3d.kernel.kit_metric import KIT_TEMPLATES, KitMetric, context_brief

    kit_ctx = {"asset_name": "Piece00", "template": sorted(KIT_TEMPLATES)[0], "seed": 0, "kit_metric": KitMetric.interior(),
               "ortho_uri": context["ortho_uri"], "style_uri": context["style_uri"]}
    brief = context_brief(kit_ctx)
    assert brief
    gov = Governor(replace(plan_for(2), ceiling_usd=1))  # the worker (hydrogen) plan: the tier whose row grew
    assert gov.plan.builder_role == "worker"
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", json.dumps({tier(r).model: [1, 2] for r in ("worker", "director")}))
    prop_author = MeteredAuthor(a.client, gov, budget, context, timeout_s=5)
    await prop_author.propose("green crate", budget=budget, n=1, context=context)
    ep.answer = ep.answer.replace('"Crate"', '"Piece00"')
    kit_author = MeteredAuthor(a.client, gov, budget, kit_ctx, timeout_s=5)
    await kit_author.propose("an interior wall piece", budget=budget, n=1, context=kit_ctx)
    kit_text = ep.generated[-1]["contents"][0].parts[0].text
    legacy_task = ("Build design 1/1 for an interior wall piece. Use the attached references. Return one JSON SceneIR with "
                   "asset_name='Piece00', nodes and materials. Ground all parts. Include material assignments. " + brief)
    assert kit_text == f"{ir_schema.ir_rules_text()}\nBudget: {json.dumps(asdict(budget))}\n{legacy_task}"
    assert PROP_CRAFT_GUIDANCE not in kit_text
    # the kit keeps the row it had (a larger reservation would admit fewer pieces at once); a prop gets the larger one
    assert ep.generated[-1]["config"].max_output_tokens == 16_384 == KIT_OUTPUT_BUDGETS["worker"].max_output_tokens
    assert ep.generated[-2]["config"].max_output_tokens == AUTHOR_OUTPUT_BUDGETS["worker"].max_output_tokens == 24_576
    # a repair is untouched
    a, ep, budget, context, gov = make_author(monkeypatch)
    scene = (await a.propose("crate", budget=budget, n=1, context=context))[0]
    ep.answer = '{"ops":[{"op":"remove_node","node_id":"body"}]}'
    await a.repair(scene, action=SimpleNamespace(kind="agent_repair", reasons=["floating"]), located={}, manifest="body")
    assert PROP_CRAFT_GUIDANCE not in ep.generated[-1]["contents"][0].parts[0].text


async def test_craft_guidance_negative_control(monkeypatch):
    """The guidance test above must fail when the text is missing: the same request without it does not contain it."""
    a, ep, budget, context, gov = make_author(monkeypatch)
    plain = _task_text(a.request("worker", "propose", propose_task("x", index=0, n=1, asset_name="A", references=False,
                                                                   brief="a kit metric"), asset_name="A"))
    assert PROP_CRAFT_GUIDANCE not in plain and "production game prop" not in plain
    assert "production game prop" in _task_text(a.request("worker", "propose", propose_task("x", index=0, n=1, asset_name="A",
                                                                                            references=False), asset_name="A"))


def test_craft_guidance_says_contact_not_overlap():
    assert "exactly on the surface" in PROP_CRAFT_GUIDANCE and "no gap and no overlap" in PROP_CRAFT_GUIDANCE
    assert "sink" not in PROP_CRAFT_GUIDANCE and "millimetre" not in PROP_CRAFT_GUIDANCE


async def test_repair_sends_the_patch_schema_and_the_directors_budget(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    scene = (await a.propose("crate", budget=budget, n=1, context=context))[0]
    ep.answer = '{"ops":[{"op":"set_param","node_id":"body","name":"size","value":[1,1,2]}]}'
    patch = await a.repair(scene, action=SimpleNamespace(kind="escalate_director", reasons=["short"]), located={}, manifest="")
    assert patch.ops[0].value == (1, 1, 2)
    cfg = ep.generated[-1]["config"]
    assert cfg.response_json_schema == ir_schema.patch_response_schema()
    assert cfg.max_output_tokens == AUTHOR_OUTPUT_BUDGETS["director"].max_output_tokens


async def test_reservation_covers_counted_input_schema_bound_and_the_whole_output_cap(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    await a.propose("crate", budget=budget, n=1, context=context)
    [rec] = gov.receipts()
    out = AUTHOR_OUTPUT_BUDGETS[gov.plan.builder_role]
    bound = schema_input_token_bound(ir_schema.scene_response_schema("Crate"))
    assert rec["reserved_usd"] == pytest.approx(cost(100 + bound, out.max_output_tokens))
    assert rec["max_output_tokens"] == out.answer_tokens + out.thinking_tokens
    assert rec["booked_usd"] == pytest.approx(cost(100, 50)) and rec["finish_reason"] == "STOP"


def test_schema_input_bound_covers_the_measured_schema_tokens():
    # Live: the propose schema in its earlier, fuller spelling billed +5,085 prompt tokens over count_tokens (which cannot
    # see it); in the current compact spelling the same request billed 1,377 fewer prompt tokens (+3,708).
    bound = schema_input_token_bound(ir_schema.scene_response_schema("WoodenCrateWithIron"))
    assert bound >= 3_708
    assert schema_input_token_bound(ir_schema.patch_response_schema()) >= 7_066  # repair, measured in the fuller spelling


def test_output_budget_table_fits_each_model_and_measured_usage():
    assert set(AUTHOR_OUTPUT_BUDGETS) >= {"worker", "bulk", "builder", "director"}
    for role, out in AUTHOR_OUTPUT_BUDGETS.items():
        mt = tier(role)
        key = f"{mt.model}/{mt.thinking.value}"
        limit = MODEL_OUTPUT_TOKEN_LIMIT[mt.model]
        assert out.max_output_tokens <= limit, role
        # the crafted-prop samples (a 63-64 part answer) count where the tier has them: they are the larger answers
        thinking = MEASURED_THINKING_TOKENS[key] + MEASURED_CRAFT_THINKING_TOKENS.get(key, ())
        answer = MEASURED_ANSWER_TOKENS[key] + MEASURED_CRAFT_ANSWER_TOKENS.get(key, ())
        worst = max(answer) + max(thinking)
        assert out.max_output_tokens > worst, role
        assert out.max_output_tokens >= min(limit, 1.7 * worst), role
        if out.max_output_tokens < limit:  # below the model limit: demand headroom on both parts
            assert out.thinking_tokens >= 2 * max(thinking) and out.answer_tokens >= 2 * max(answer), role
    by = {r: AUTHOR_OUTPUT_BUDGETS[r].max_output_tokens for r in ("worker", "builder", "director")}
    assert by["worker"] < by["builder"] <= by["director"]  # a hero call is never sized by a worker-tier assumption


def test_worker_cap_leaves_room_for_the_largest_prop_the_guidance_asks_for():
    """The guidance asks for 50-90 parts. Three live answers of 63, 64 and 55 parts used 9,544, 10,396 and 9,393 tokens (answer +
    thinking); per part that is 113-124 answer tokens, and thinking varied 2.2x across the older samples. A truncated
    answer is a lost candidate billed at the whole cap, so the cap holds 90 parts at the largest answer rate and the
    largest thinking seen scaled by that variation."""
    key = "gemini-4-hydrogen/low"
    answers, thoughts = MEASURED_CRAFT_ANSWER_TOKENS[key], MEASURED_CRAFT_THINKING_TOKENS[key]
    per_part = max(a / n for a, n in zip(answers, (63, 64, 55)))
    old = MEASURED_THINKING_TOKENS["gemini-4-hydrogen/low"]
    out = AUTHOR_OUTPUT_BUDGETS["worker"]
    assert 90 * per_part + max(thoughts) * 2.2 < out.max_output_tokens  # ~11,200 + ~6,000 against 24,576
    assert out.answer_tokens >= 2 * max(answers) and out.thinking_tokens >= 2 * max(thoughts)
    assert min(old) < max(old) and max(old) / min(old) < 2.5  # where the 2.2 variation comes from


async def test_plan_that_cannot_admit_its_tier_fails_loudly_with_the_numbers(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", json.dumps({tier("director").model: [1, 100]}))
    with pytest.raises(BudgetExceeded) as e:
        await a.propose("crate", budget=budget, n=1, context=context)
    out = AUTHOR_OUTPUT_BUDGETS["director"]
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
    # loop2: the hero plan's director thinks HIGH, and this answer was mostly thinking (4,093 of 4,096): it is asked once
    # more at MEDIUM (metered_author.LESS_THINKING), which this endpoint cuts off too; each call books what it billed
    recs = gov.receipts()
    assert len(recs) == 2 and all(r["status"] == "AuthorTruncated" and r["finish_reason"] == "MAX_TOKENS" and r["usage_known"]
                                  for r in recs)
    assert all(r["booked_usd"] == pytest.approx(cost(6000, 4096)) for r in recs) and gov.spent == pytest.approx(2 * cost(6000, 4096))
    assert "the first attempt at HIGH thinking was cut off too" in str(e.value)


# (None, None, False), a response with no candidate and no prompt feedback, moved to the test below: the call is
# streamed, and a stream that ends with neither is one that was cut before its finish, not an answer.
@pytest.mark.parametrize("finish,answer,blocked", [
    ("SAFETY", "", False), ("RECITATION", '{"asset_name":"Crate"}', False), ("STOP", "", False),
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


async def test_a_stream_with_no_candidate_no_feedback_and_no_finish_was_cut_and_keeps_its_reservation(monkeypatch):
    """Blocking, such a response was a complete answer, booked by its usage. Streamed, it can only be a stream cut before
    its last event (every live stream of 2026-11-22 ended with a finish reason or broke: r5/live/stream_gaps2), so it is a
    broken stream: outcome unknown, the whole reservation kept, retried once (tactics/stream_call.StreamIncomplete)."""
    from antigravity3d.service_answer import RetryPolicy
    from antigravity3d.tactics.metered_author import AuthorUnavailable

    a, ep, budget, context, gov = make_author(monkeypatch)
    a.retry = RetryPolicy(sleep=lambda s: asyncio.sleep(0))  # the retry's wait is not under test
    ep.finish, ep.answer = None, None
    with pytest.raises(AuthorUnavailable, match="StreamIncomplete"):
        await a.propose("crate", budget=budget, n=1, context=context)
    assert len(ep.generated) == 2
    assert all(r["usage_known"] is False and r["booked_usd"] == r["reserved_usd"] for r in gov.receipts())


async def test_live_benchmark_sends_the_production_request(monkeypatch):
    """benchmarks/perf_live.py's text group must dispatch exactly build_author_request's contents and config."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "benchmarks" / "perf_live.py"
    spec = importlib.util.spec_from_file_location("perf_live_under_test", path)
    perf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(perf)
    sent = []

    class Models:
        async def generate_content(self, **kw):
            sent.append(kw)
            return SimpleNamespace(text="{}", usage_metadata=None, prompt_feedback=None,
                                   candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)])

    client = perf.CountedClient(SimpleNamespace(aio=SimpleNamespace(models=Models(), decisions=None)), perf.Limiter(10, 10**9))
    await perf.text_group(client, (("worker", 1), ("director", 1)), {}, lambda: None)
    from antigravity3d.tactics.authoring import _slug

    asset = _slug(perf.ASSET_PROMPT)
    task = propose_task(perf.ASSET_PROMPT, index=0, n=1, asset_name=asset, references=False)
    for kw, role in zip(sent, ("worker", "director"), strict=True):
        req = build_author_request(role, "propose", task, budget=EngineBudget("unreal_5"), context={},
                                   timeout_s=perf.text_timeout(role), asset_name=asset)
        assert kw["model"] == req.model and kw["contents"] == req.contents and kw["config"] == req.config


# --- admission that waits, recorded outcomes, per-tier timeouts ---------------------------------------------------


from antigravity3d.tactics.metered_author import (  # noqa: E402
    AUTHOR_TIMEOUT_S, MEASURED_PROPOSE_WALL_S, PROP_CRAFT_GUIDANCE, TIMEOUT_HEADROOM, AuthorInvalid, AuthorTimedOut, author_timeout,
    expected_output_tokens,
)

DIRECTOR_LIMIT_TOKENS = AUTHOR_OUTPUT_BUDGETS["director"].max_output_tokens


class ScriptedEndpoint(Endpoint):
    """Endpoint that tracks concurrency and answers per candidate ('design i/n' in the task) from a script."""

    def __init__(self, script=None, delay=0.01):
        super().__init__()
        self.script, self.delay, self.running, self.peak = script or {}, delay, 0, 0

    async def generate_content(self, **kwargs):
        text = kwargs["contents"][0].parts[0].text
        index = int(text.split("design ")[1].split("/")[0]) - 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
            what = self.script.get(index, "ok")
            if isinstance(what, BaseException):
                self.generated.append(kwargs)
                raise what
            if what == "truncated":
                self.finish = "MAX_TOKENS"
            elif what == "empty":
                self.finish, self.answer = "STOP", " "
            elif what == "garbled":
                self.finish, self.answer = "STOP", "not json"
            else:
                self.finish = "STOP"
                self.answer = Endpoint().answer
            return await Endpoint.generate_content(self, **kwargs)
        finally:
            self.running -= 1


def reservation_limit(n_counted=100, role="director"):
    schema = ir_schema.scene_response_schema("Crate")
    return cost(n_counted + schema_input_token_bound(schema), AUTHOR_OUTPUT_BUDGETS[role].max_output_tokens)


def author_with(monkeypatch, endpoint, ceiling):
    a, _, budget, context, gov = make_author(monkeypatch, ceiling=ceiling)
    a.client = SimpleNamespace(aio=SimpleNamespace(models=endpoint))
    return a, budget, context, gov


async def test_admission_sequences_candidates_under_the_ceiling_and_none_is_dropped(monkeypatch):
    limit = reservation_limit()
    ep = ScriptedEndpoint()
    a, budget, context, gov = author_with(monkeypatch, ep, ceiling=2.5 * limit)  # two worst cases fit at once, not three
    result = await a.propose_with_outcomes("crate", budget=budget, n=6, context=context)
    assert len(result.scenes) == 6 and [o.status for o in result.outcomes] == ["produced"] * 6
    assert ep.peak == 2 and len(ep.generated) == 6
    assert sum(1 for o in result.outcomes if o.admission_wait_s > 0) == 4  # four started when an earlier one settled
    assert all(o.reserved_usd == pytest.approx(limit) for o in result.outcomes)
    assert len(gov.receipts()) == 6 and gov.spent <= 2.5 * limit


async def test_every_candidate_has_an_outcome_with_its_reason(monkeypatch):
    limit = reservation_limit()
    script = {1: "truncated", 2: "empty", 3: "garbled", 4: TimeoutError(), 5: RuntimeError("boom")}
    ep = ScriptedEndpoint(script)
    a, budget, context, gov = author_with(monkeypatch, ep, ceiling=10 * limit)
    result = await a.propose_with_outcomes("crate", budget=budget, n=6, context=context)
    by = {o.index: o for o in result.outcomes}
    assert [by[i].status for i in range(6)] == ["produced", "truncated", "no_answer", "invalid", "timed_out", "error"]
    assert by[1].finish_reason == "MAX_TOKENS" and by[1].usage["output_tokens"] == 50
    assert by[2].finish_reason == "STOP" and "empty answer" in by[2].detail
    assert "does not validate" in by[3].detail and by[4].finish_reason == "TIMEOUT" and "RuntimeError: boom" in by[5].detail
    assert len(result.scenes) == 1 and result.n_requested == 6
    by_id = {r["status"] for r in gov.receipts()}
    assert {"ok", "AuthorTruncated", "AuthorNoAnswer", "AuthorInvalid", "AuthorTimedOut", "RuntimeError"} == by_id
    timed_out = next(r for r in gov.receipts() if r["status"] == "AuthorTimedOut")
    assert timed_out["booked_usd"] == timed_out["reserved_usd"] and not timed_out["usage_known"]


async def test_candidate_that_can_no_longer_be_admitted_is_recorded_with_the_numbers(monkeypatch):
    limit = reservation_limit()
    ep = ScriptedEndpoint()
    ep.usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=20000, thoughts_token_count=12000,
                               total_token_count=32100)  # the first call books nearly its whole reservation
    a, budget, context, gov = author_with(monkeypatch, ep, ceiling=1.4 * limit)
    result = await a.propose_with_outcomes("crate", budget=budget, n=2, context=context)
    first, second = result.outcomes
    assert first.status == "produced" and second.status == "not_admitted"
    assert "never fit" in second.detail and "already settled" in second.detail and f"${limit:.5f}" in second.detail
    assert len(ep.generated) == 1 and len(result.scenes) == 1


async def test_no_usable_candidate_names_what_happened_to_each(monkeypatch):
    limit = reservation_limit()
    ep = ScriptedEndpoint({0: "truncated", 1: "empty", 2: TimeoutError()})
    a, budget, context, gov = author_with(monkeypatch, ep, ceiling=10 * limit)
    with pytest.raises(AuthorTruncated) as e:  # the first error keeps its type and its usage
        await a.propose("crate", budget=budget, n=3, context=context)
    msg = str(e.value)
    assert e.value.usage["input_tokens"] == 100
    assert "#0 truncated" in msg and "#1 no_answer" in msg and "#2 timed_out" in msg and "3 requested" in msg


async def test_single_unusable_candidate_keeps_its_typed_error(monkeypatch):
    ep = ScriptedEndpoint({0: "garbled"})
    a, budget, context, _ = author_with(monkeypatch, ep, ceiling=1)
    with pytest.raises(AuthorInvalid) as e:
        await a.propose("crate", budget=budget, n=1, context=context)
    assert e.value.finish_reason == "STOP" and isinstance(e.value, ValueError)


async def test_waiting_candidates_do_not_start_a_request_before_admission(monkeypatch):
    limit = reservation_limit()
    ep = ScriptedEndpoint(delay=0.02)
    a, budget, context, gov = author_with(monkeypatch, ep, ceiling=1.2 * limit)  # one at a time
    seen = []
    orig = gov.reserve_wait

    async def spy(*args, **kw):
        hold = await orig(*args, **kw)
        seen.append((len(ep.generated), ep.running))
        return hold

    gov.reserve_wait = spy
    await a.propose("crate", budget=budget, n=3, context=context)
    assert ep.peak == 1 and [running for _, running in seen] == [0, 0, 0]


async def test_cancelling_propose_releases_waiters_and_settles_what_was_dispatched(monkeypatch):
    limit = reservation_limit()
    ep = ScriptedEndpoint(delay=0.05)
    a, budget, context, gov = author_with(monkeypatch, ep, ceiling=1.2 * limit)
    task = asyncio.ensure_future(a.propose("crate", budget=budget, n=4, context=context))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not gov._waiters and all(r["booked_usd"] is not None for r in gov.receipts())


# --- timeouts -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(AUTHOR_TIMEOUT_S))
def test_tier_timeouts_follow_the_measured_latency_with_stated_headroom(role):
    mt = tier(role)
    low, slowest = MEASURED_PROPOSE_WALL_S[f"{mt.model}/{mt.thinking.value}"]
    timeout = AUTHOR_TIMEOUT_S[role]
    # Rule: at least TIMEOUT_HEADROOM x the slowest measured call, rounded up to 30 s, and no more than that rounding.
    assert TIMEOUT_HEADROOM == 2.0 and timeout >= TIMEOUT_HEADROOM * slowest and timeout % 30 == 0
    assert timeout < TIMEOUT_HEADROOM * slowest + 30 and low < slowest


def test_timeouts_differ_by_tier_the_way_the_latencies_do():
    # the two small tiers answer in seconds, the two large ones in minutes; which large tier is slower is a measurement
    assert AUTHOR_TIMEOUT_S["worker"] == AUTHOR_TIMEOUT_S["bulk"] < min(AUTHOR_TIMEOUT_S["builder"], AUTHOR_TIMEOUT_S["director"])
    assert AUTHOR_TIMEOUT_S["director"] > 300  # argon already ended in 504 at 300 s
    assert set(AUTHOR_TIMEOUT_S) == set(AUTHOR_OUTPUT_BUDGETS)
    with pytest.raises(ValueError):
        author_timeout("decision")


async def test_requests_carry_the_tier_timeout_unless_one_is_forced(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    a.timeout_s = None
    await a.propose("crate", budget=budget, n=1, context=context)
    assert ep.generated[0]["config"].http_options.timeout == int(AUTHOR_TIMEOUT_S[gov.plan.builder_role] * 1000)
    assert a.request("worker", "propose", "t").config.http_options.timeout == 90_000
    a.timeout_s = 7
    assert a.request("director", "propose", "t").config.http_options.timeout == 7_000


def test_agent_author_defaults_to_the_per_tier_timeouts(monkeypatch):
    from antigravity3d.tactics.authoring import AgentAuthor

    ep = Endpoint()
    bound = AgentAuthor(client=SimpleNamespace(aio=SimpleNamespace(models=ep))).bind(
        Governor(plan_for(2)), EngineBudget("godot_4"), {"asset_name": "Crate"})
    assert bound.timeout_s is None and bound.timeout_for("director") == AUTHOR_TIMEOUT_S["director"]
    assert AgentAuthor(timeout_s=12).timeout_s == 12


# --- sizing ---------------------------------------------------------------------------------------------------------


def test_expected_output_tokens_come_from_the_measurements_and_stay_under_the_cap():
    for role in ("worker", "bulk", "builder", "director"):
        assert 0 < expected_output_tokens(role) < AUTHOR_OUTPUT_BUDGETS[role].max_output_tokens
    assert 18_000 < expected_output_tokens("director") < 20_000  # argon: mean 11,726 answer + 7,409 thinking
    # a prop on hydrogen/low: the mean of the five samples of the request as it ships (round five's language, which
    # writes symmetric and repeated parts once; it was 9,778 from the round-four samples); an unmeasured tier and a kit
    # piece keep the older tables
    assert expected_output_tokens("worker", craft=True) == round((2_386 + 3_998 + 3_000 + 2_298 + 2_073) / 5
                                                                 + (2_232 + 3_565 + 3_141 + 3_385 + 3_616) / 5) == 5_939
    assert expected_output_tokens("worker") < 2_100 and expected_output_tokens("director", craft=True) == expected_output_tokens("director")


def test_planning_figures_agree_with_the_measurement_and_each_other(monkeypatch):
    from antigravity3d.tactics.token_plan import AUTHOR_TOKENS_EST, author_worst_tokens, plan_estimate
    from antigravity3d.tactics.governor import PLANS

    key = "gemini-4-hydrogen/low"
    measured = MEASURED_CRAFT_INPUT_TOKENS[key] + expected_output_tokens("worker", craft=True)
    assert AUTHOR_TOKENS_EST == measured == 16_428  # 10,489 input + 2,751 answer + 3,188 thinking (was 16,072 in round four)
    assert 10 <= min(MEASURED_CRAFT_PROPOSE_WALL_S[key]) and max(MEASURED_CRAFT_PROPOSE_WALL_S[key]) <= MEASURED_PROPOSE_WALL_S[key][1]
    a, *_ = make_author(monkeypatch)
    a.governor.unit = "tokens"
    expected, worst = a.cost_profile("worker")  # input here is the deliberately high bound (bytes / 3 + schema bytes)
    # the plan's figure is the measured (billed) input plus the expected output: not above the profile's, below the cap
    assert AUTHOR_TOKENS_EST <= expected < worst
    assert worst - AUTHOR_OUTPUT_BUDGETS["worker"].max_output_tokens == pytest.approx(expected - expected_output_tokens("worker", craft=True))
    assert author_worst_tokens(a, role="worker") == int(worst) + (1 if worst != int(worst) else 0)
    # a kit piece is estimated without the guidance (fewer input tokens) and by the older measurements
    kit_expected, kit_worst = a.cost_profile("worker", kit=True)
    assert kit_expected < expected and kit_worst < worst
    # every plan is larger than before and counts author calls by the new figure
    assert plan_estimate(PLANS[sorted(PLANS)[0]]).tokens > plan_estimate(PLANS[sorted(PLANS)[0]], author_tokens=9_444).tokens


def test_cost_profile_is_expected_below_worst_case_and_absent_without_rates(monkeypatch):
    a, *_ = make_author(monkeypatch)
    expected, worst = a.cost_profile()
    assert 0 < expected < worst
    assert worst > cost(0, DIRECTOR_LIMIT_TOKENS)  # the whole output cap plus the estimated input
    assert expected / worst < 0.7  # sizing on the worst case would cut the plan by about a third
    monkeypatch.setenv("AGY3D_TEXT_RATES_JSON", "{}")
    assert a.cost_profile() is None


async def test_repair_role_override_is_bounded_by_the_plan(monkeypatch):
    a, ep, budget, context, gov = make_author(monkeypatch)
    act = SimpleNamespace(kind="agent_repair", reasons=["x"])
    scene = (await a.propose("crate", budget=budget, n=1, context=context))[0]
    ep.answer = '{"ops":[{"op":"remove_node","node_id":"body"}]}'
    await a.repair(scene, action=act, located={}, manifest="m", role="director")
    assert ep.generated[-1]["model"] == tier("director").model
    a.governor._plan = replace(gov.plan, allow_director=False)
    with pytest.raises(BudgetExceeded, match="outside this asset's plan"):
        await a.repair(scene, action=act, located={}, manifest="m", role="director")
