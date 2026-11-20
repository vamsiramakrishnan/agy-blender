from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from types import SimpleNamespace

import pytest
from google.genai import errors, types
from PIL import Image

from antigravity3d.decisions import fallback as F
from antigravity3d.decisions import make_control_plane
from antigravity3d.decisions import questions as Q
from antigravity3d.decisions.compose import Thresholds, decide_action
from antigravity3d.decisions.engine import DecisionControlPlane, image_block, image_data_block, text_block
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.decisions.models import DecisionResult, PromptBlockedError, VisualQAResult

QS = {
    "is_red": Q.noul("Is Image 1 red?"),
    "colour": Q.choice("Which colour?", {"red": "r", "green": "g", "blue": "b"}),
    "sat": Q.score("How saturated?", ["none", "low", "mid", "high", "vivid"]),
}
GOOD = {"is_red": 0.9, "colour": {"red": 0.8, "green": 0.1, "blue": 0.1}, "sat": {"0": 0, "1": 0, "2": 0.1, "3": 0.2, "4": 0.7}}


class StubGen:
    """Stands in for `genai.Client`: `.aio.models.generate_content` returns canned text (str / callable / Exception per call)."""

    def __init__(self, *responses, tokens=(100, 20, 5), latency=0.0):
        self.responses, self.tokens, self.latency = list(responses), tokens, latency
        self.calls: list[dict] = []
        self.in_flight = self.max_in_flight = 0
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.latency)
        finally:
            self.in_flight -= 1
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if callable(r):
            r = r(config)
        if isinstance(r, Exception):
            raise r
        text = r if isinstance(r, str) else json.dumps(r)
        p, c, t = self.tokens
        return SimpleNamespace(text=text, prompt_feedback=None, candidates=[],
                               usage_metadata=SimpleNamespace(prompt_token_count=p, candidates_token_count=c, thoughts_token_count=t))


def backend(stub, **kw) -> F.GenerativeDecisionsBackend:
    kw.setdefault("thinking_level", "LOW")
    return F.GenerativeDecisionsBackend(stub, model="gemini-4-neon", **kw)


def img_input():
    return [text_block("Image 1: swatch."), image_data_block(b"\x89PNG-bytes", "low")]


# ---- schema / prompt / parts -------------------------------------------------------------------------------

def test_schema_per_type():
    s = F.build_response_schema(QS)
    assert s["required"] == list(QS)
    assert s["properties"]["is_red"]["minimum"] == 0.01 and s["properties"]["is_red"]["maximum"] == 0.99
    assert set(s["properties"]["colour"]["properties"]) == {"red", "green", "blue"}
    assert set(s["properties"]["sat"]["properties"]) == {"0", "1", "2", "3", "4"}
    txt = F.render_questions(QS)
    assert "### is_red" in txt and "option 'red'" in txt and "level 4: vivid" in txt
    assert "independently" in F.SYSTEM_INSTRUCTION and "independently" in txt


def test_blocks_to_parts_faithful(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"localbytes")
    blocks = [text_block("hello"), image_data_block(b"abc", "high", "image/jpeg"), image_block("gs://b/x.png", "ultra_high"),
              image_block(str(png), "low"), {"type": "image", "uri": "https://e.com/i.png", "mime_type": "image/webp"}]
    p = F.blocks_to_parts(blocks)
    assert [x.text for x in p[:1]] == ["hello"]
    assert p[1].inline_data.data == b"abc" and p[1].inline_data.mime_type == "image/jpeg"
    assert p[1].media_resolution.level == types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH
    assert p[2].file_data.file_uri == "gs://b/x.png" and p[2].media_resolution.level.name == "MEDIA_RESOLUTION_ULTRA_HIGH"
    assert p[3].inline_data.data == b"localbytes" and p[3].media_resolution.level.name == "MEDIA_RESOLUTION_LOW"
    assert p[4].file_data.mime_type == "image/webp" and p[4].media_resolution is None
    assert F.blocks_to_parts("plain")[0].text == "plain"
    assert F.blocks_to_parts(blocks, honor_resolution=False)[1].media_resolution is None
    with pytest.raises(ValueError):
        F.blocks_to_parts([{"type": "audio"}])
    with pytest.raises(ValueError):
        F.blocks_to_parts([{"type": "image"}])


# ---- answers through the control plane -------------------------------------------------------------------------

async def test_plane_answers_marked_uncalibrated_with_tokens_and_unknown_cost():
    stub = StubGen(GOOD)
    plane = DecisionControlPlane(backend(stub))
    assert plane.backend_kind == "generative_fallback" and plane.calibrated is False
    r = await plane.evaluate(img_input(), QS, tag="t")
    assert r.calibrated is False and r.source == "generative_fallback"
    assert r.noul("is_red") == pytest.approx(0.9) and r.choice("colour") == "red"
    assert r.score("sat") == pytest.approx(0.1 * 2 + 0.2 * 3 + 0.7 * 4)
    assert r.answers["colour"].probabilities["red"] == pytest.approx(0.8)
    assert (r.input_tokens, r.output_tokens) == (100, 25)  # thinking tokens count as output
    assert r.cost_known is False and r.est_cost_usd == 0.0
    s = plane.stats.summary()
    assert s["uncalibrated_calls"] == 1 and s["unknown_cost_calls"] == 1 and s["output_tokens"] == 25
    # the request faithfully carried the image bytes, in order, then the question text
    parts = stub.calls[0]["contents"][0].parts
    assert parts[0].text == "Image 1: swatch." and parts[1].inline_data.data == b"\x89PNG-bytes" and "### is_red" in parts[2].text
    assert stub.calls[0]["config"].response_json_schema["required"] == list(QS)


async def test_cost_reported_only_when_prices_configured(monkeypatch):
    monkeypatch.setitem(F.PRICE_TABLE_USD_PER_M, "gemini-4-neon", (1.0, 4.0))
    r = await DecisionControlPlane(backend(StubGen(GOOD))).evaluate(img_input(), QS, tag="t")
    assert r.cost_known and r.est_cost_usd == pytest.approx(100 * 1.0 / 1e6 + 25 * 4.0 / 1e6)
    monkeypatch.setitem(F.PRICE_TABLE_USD_PER_M, "gemini-4-neon", (1.0, None))
    assert F.cost_usd("gemini-4-neon", 10, 10) is None and F.cost_usd("unknown-model", 1, 1) is None
    assert all(v == (None, None) for v in [(F.PRICE_NEON_INPUT_PER_M_USD, F.PRICE_NEON_OUTPUT_PER_M_USD)])  # shipped unset


async def test_repairs_unnormalised_and_clamps():
    raw = {"is_red": 1.0, "colour": {"red": 6, "green": 3, "blue": 1}, "sat": [1, 1, 1, 1, 1]}
    r = await DecisionControlPlane(backend(StubGen(raw))).evaluate(img_input(), QS, tag="t")
    assert r.noul("is_red") == 0.99  # clamped into [0.01, 0.99]
    assert r.answers["colour"].probabilities == pytest.approx({"red": 0.6, "green": 0.3, "blue": 0.1}) and r.choice("colour") == "red"
    assert r.score("sat") == pytest.approx(2.0) and sum(r.answers["sat"].probabilities.values()) == pytest.approx(1)
    r2 = await DecisionControlPlane(backend(StubGen({**raw, "is_red": 0.0, "colour": {"red": 1}}))).evaluate(img_input(), QS, tag="t")
    assert r2.noul("is_red") == 0.01 and r2.answers["colour"].probabilities["green"] == 0.0  # absent option = 0, not invented
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    assert (await DecisionControlPlane(backend(StubGen(fenced))).evaluate(img_input(), QS, tag="t")).choice("colour") == "red"


@pytest.mark.parametrize("bad", [
    "not json at all",
    "",
    "[1, 2, 3]",
    {"is_red": 0.9, "colour": {"red": 1}},  # missing answer
    {**GOOD, "is_red": 1.7},  # out of range: not a probability
    {**GOOD, "is_red": "high"},
    {**GOOD, "is_red": True},
    {**GOOD, "colour": {"purple": 1.0}},  # none of the options
    {**GOOD, "colour": {"red": 0, "green": 0, "blue": 0}},  # all zero
    {**GOOD, "colour": {"red": -1, "green": 2}},
    {**GOOD, "sat": {"9": 1}},
    {**GOOD, "sat": "mid"},
])
async def test_malformed_raises_typed_error_never_defaults(bad):
    stub = StubGen(bad)
    with pytest.raises(F.MalformedGenerativeAnswer):
        await DecisionControlPlane(backend(stub)).evaluate(img_input(), QS, tag="t")
    assert len(stub.calls) == 2  # one re-ask, then give up
    assert issubclass(F.MalformedGenerativeAnswer, F.GenerativeDecisionError)


async def test_malformed_then_valid_is_retried_and_both_calls_billed():
    stub = StubGen("garbage", GOOD)
    r = await DecisionControlPlane(backend(stub)).evaluate(img_input(), QS, tag="t")
    assert r.choice("colour") == "red" and r.input_tokens == 200 and r.output_tokens == 50


async def test_zero_retries_raises_immediately():
    stub = StubGen("garbage", GOOD)
    with pytest.raises(F.MalformedGenerativeAnswer):
        await DecisionControlPlane(backend(stub, max_parse_retries=0)).evaluate(img_input(), QS, tag="t")
    assert len(stub.calls) == 1


async def test_large_sets_are_chunked_concurrently_and_merged():
    qs = {f"defect_{i}": Q.noul(f"Is thing {i} present in Image 1?") for i in range(7)}

    def answer(cfg):
        return {k: 0.1 * (int(k.split("_")[1]) + 1) for k in cfg.response_json_schema["required"]}

    stub = StubGen(answer, latency=0.05)
    r = await DecisionControlPlane(backend(stub, chunk_size=3)).evaluate(img_input(), qs, tag="t")
    assert len(stub.calls) == 3 and stub.max_in_flight == 3
    assert sorted(len(c["config"].response_json_schema["required"]) for c in stub.calls) == [1, 3, 3]
    assert {k: round(a.noul, 2) for k, a in r.answers.items()} == {f"defect_{i}": round(0.1 * (i + 1), 2) for i in range(7)}
    assert r.input_tokens == 300 and r.n_questions == 7
    sem = backend(StubGen(answer, latency=0.05), chunk_size=1, max_concurrency=2)
    await DecisionControlPlane(sem).evaluate(img_input(), qs, tag="t")
    assert sem._client.max_in_flight == 2


async def test_thinking_level_stepped_up_when_rejected():
    rej = errors.ClientError(400, {"error": {"message": "Thinking level MINIMAL is not supported for this model.", "status": "INVALID_ARGUMENT"}})
    stub = StubGen(rej, GOOD)
    b = backend(stub, thinking_level="MINIMAL")
    r = await DecisionControlPlane(b).evaluate(img_input(), QS, tag="t")
    assert r.choice("colour") == "red" and b.thinking_level == "LOW"
    assert [c["config"].thinking_config.thinking_level.name for c in stub.calls] == ["MINIMAL", "LOW"]


async def test_other_api_errors_propagate_and_transient_ones_retry_in_the_plane():
    boom = errors.ClientError(400, {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}})
    with pytest.raises(errors.ClientError):
        await DecisionControlPlane(backend(StubGen(boom))).evaluate(img_input(), QS, tag="t")

    async def nosleep(_):
        return None

    stub = StubGen(errors.ServerError(503, {"error": {"message": "overloaded"}}), GOOD)
    r = await DecisionControlPlane(backend(stub), sleep=nosleep).evaluate(img_input(), QS, tag="t")
    assert r.attempts == 2 and r.choice("colour") == "red"


async def test_blocked_prompt_surfaces_as_prompt_blocked_error():
    class Blocked(StubGen):
        async def _gen(self, **kw):
            return SimpleNamespace(text=None, prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(name="SAFETY"), block_reason_message="no"),
                                   candidates=[], usage_metadata=None)

    with pytest.raises(PromptBlockedError) as ei:
        await DecisionControlPlane(backend(Blocked(GOOD))).evaluate(img_input(), QS, tag="t")
    assert ei.value.reason == "SAFETY"


async def test_fallback_serves_real_visual_qa_bank_and_cannot_auto_approve():
    stub = StubGen(lambda cfg: {k: (0.02 if k.startswith("defect_") else
                                    ({"approve_export": 1.0} if k == "next_action" else {"0": 0, "1": 0, "2": 0, "3": 0, "4": 1}))
                                for k in cfg.response_json_schema["required"]})
    plane = DecisionControlPlane(backend(stub, chunk_size=100))
    qa = await plane.evaluate_visual_qa("gs://s.png", stage="lookdev")
    assert qa.result.calibrated is False and qa.next_action == "approve_export"
    act = decide_action(qa)
    assert act.kind == "escalate_director" and "uncalibrated" in act.reasons[0]
    assert decide_action(qa, Thresholds(trust_uncalibrated=True)).kind == "approve"


# ---- factory -----------------------------------------------------------------------------------------------------

class Http(Exception):
    def __init__(self, code):
        super().__init__(f"http {code}")
        self.code = code


class ProbeClient(StubGen):
    def __init__(self, *outcomes, **kw):
        super().__init__(GOOD, **kw)
        self.outcomes, self.probes = list(outcomes), []
        self.aio.decisions = SimpleNamespace(create=self._probe)

    async def _probe(self, **kw):
        self.probes.append(kw)
        o = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(o, Exception):
            raise o
        return o


@pytest.mark.parametrize("exc", [Http(404), Http(403)])
async def test_factory_falls_back_on_unavailable_with_one_warning(exc, caplog):
    c = ProbeClient(exc)
    with caplog.at_level(logging.WARNING, logger="antigravity3d.decisions"):
        plane = await make_control_plane(c)
    assert plane.backend_kind == "generative_fallback" and plane.calibrated is False
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and str(exc.code) in warns[0].getMessage() and "UNCALIBRATED" in warns[0].getMessage()
    assert len(c.probes) == 1 and len(c.probes[0]["questions"]) == 1
    r = await plane.evaluate(img_input(), QS, tag="t")
    assert r.calibrated is False


async def test_factory_keeps_decisions_when_probe_works(caplog):
    c = ProbeClient(SimpleNamespace(answers={}))
    plane = await make_control_plane(c)
    assert plane.backend_kind == "decisions" and plane.calibrated and not caplog.records and len(c.probes) == 1


async def test_factory_transient_retried_once_then_fallback_and_hard_errors_propagate():
    c = ProbeClient(Http(503), SimpleNamespace(answers={}))
    assert (await make_control_plane(c)).backend_kind == "decisions" and len(c.probes) == 2
    c2 = ProbeClient(Http(503))
    assert (await make_control_plane(c2)).backend_kind == "generative_fallback" and len(c2.probes) == 2
    with pytest.raises(Http):
        await make_control_plane(ProbeClient(Http(401)))
    slow = ProbeClient(asyncio.TimeoutError())
    assert (await make_control_plane(slow, probe_timeout_s=0.1)).backend_kind == "generative_fallback"


async def test_factory_prefer_generative_and_fake():
    c = ProbeClient(Http(404))
    assert (await make_control_plane(c, prefer="generative")).backend_kind == "generative_fallback" and not c.probes
    fake = FakeDecisionsClient()
    p = await make_control_plane(fake)
    assert p.backend_kind == "fake" and p.calibrated and not fake.calls
    with pytest.raises(ValueError):
        await make_control_plane(c, prefer="nope")


# ---- live ----------------------------------------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY")
async def test_live_fallback_red_square(capsys):
    import time

    from google import genai

    client = genai.Client(http_options=types.HttpOptions(timeout=60_000))
    plane = await make_control_plane(client, prefer="generative")
    assert plane.backend_kind == "generative_fallback"
    buf = io.BytesIO()
    Image.new("RGB", (128, 128), (220, 20, 20)).save(buf, "PNG")
    qs = {
        "is_red": Q.noul("Is the dominant colour of Image 1 red?"),
        "colour": Q.choice("Which colour best describes Image 1?", {"red": "red", "green": "green", "blue": "blue"}),
        "saturation": Q.score("How saturated is the colour in Image 1?", [
            "Grey, no saturation", "Slightly saturated", "Moderately saturated", "Strongly saturated", "Fully vivid"]),
    }
    t0 = time.perf_counter()
    r = await plane.evaluate([text_block("Image 1: a flat colour swatch."), image_data_block(buf.getvalue())], qs, tag="live_fallback")
    dt = time.perf_counter() - t0
    assert r.calibrated is False and r.source == "generative_fallback"
    assert r.noul("is_red") >= 0.9
    assert r.choice("colour") == "red" and r.answers["colour"].probabilities["red"] > 0.7
    assert sum(r.answers["colour"].probabilities.values()) == pytest.approx(1.0)
    assert r.score("saturation") >= 3.0
    assert r.input_tokens > 0 and r.output_tokens > 0 and r.cost_known is False
    with capsys.disabled():
        print(f"\nLIVE fallback: model={plane._client.model} thinking={plane._client.thinking_level} latency={dt:.2f}s "
              f"in_tokens={r.input_tokens} out_tokens={r.output_tokens} answers="
              f"{ {k: a.value for k, a in r.answers.items()} }")


# ---- truncation ----------------------------------------------------------------------------------------------------

def test_evidence_no_strict_prefix_of_an_answer_parses_as_complete():
    """Every truncation point of a valid answer is rejected by the parser (compact and pretty-printed JSON)."""
    for text in (json.dumps(GOOD), json.dumps(GOOD, indent=2)):
        parsed = 0
        for cut in range(1, len(text)):
            try:
                F.parse_response_text(text[:cut], QS)
                parsed += 1
            except F.MalformedGenerativeAnswer:
                pass
        assert parsed == 0
    # ...but a complete answer followed by whitespace the model kept emitting until the cap DOES parse, so the
    # parser alone cannot tell a MAX_TOKENS response from a complete one: the finish reason has to be checked.
    F.parse_response_text(json.dumps(GOOD) + "\n" * 50, QS)


class Truncated(StubGen):
    async def _gen(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=json.dumps(GOOD) + "\n" * 50, prompt_feedback=None,
                               candidates=[SimpleNamespace(finish_reason=types.FinishReason.MAX_TOKENS)],
                               usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=65000,
                                                              thoughts_token_count=536))


async def test_max_tokens_is_a_typed_outcome_never_parsed_nor_reasked():
    stub = Truncated(GOOD)
    with pytest.raises(F.GenerativeTruncated) as ei:
        await DecisionControlPlane(backend(stub)).evaluate(img_input(), QS, tag="t")
    assert len(stub.calls) == 1  # not retried as "malformed"
    assert ei.value.finish_reason == "MAX_TOKENS"
    assert ei.value.usage.total_output_tokens == 65536 and ei.value.usage.thoughts_tokens == 536
    assert not issubclass(F.GenerativeTruncated, F.MalformedGenerativeAnswer)
