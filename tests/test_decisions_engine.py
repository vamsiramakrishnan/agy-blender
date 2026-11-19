from __future__ import annotations

import asyncio
import logging
import os

import pytest
from google.genai._gaos.types.decisions import CreateDecisionRequest

from antigravity3d.decisions import questions as Q
from antigravity3d.decisions.engine import (
    LATENCY_KNEE_TOKENS, PRICE_PER_M_INPUT_USD, DecisionControlPlane, MediaResolution, estimate_input_tokens, image_block,
    text_block,
)
from antigravity3d.decisions.fakes import FakeDecisionsClient, FakeWireError, validate_request
from antigravity3d.decisions.models import DecisionError, PromptBlockedError, TransientDecisionError


class HttpErr(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"http {code}")
        self.code = code


async def _nosleep(_: float) -> None:
    return None


def plane(client: FakeDecisionsClient, **kw) -> DecisionControlPlane:
    return DecisionControlPlane(client, sleep=_nosleep, **kw)


ALL_LABELS = {"sheet": "Image 1", "ortho": "Image 2", "style": "Image 3", "cand": "Image 1", "ctx": "Image 2", "img": "Image 1"}


# ---- question banks / wire format -------------------------------------------------------------

def test_banks_unique_ids_and_size():
    assert len(Q.VISUAL_QA_QUESTIONS) >= 25
    for bank in (Q.VISUAL_QA_QUESTIONS, *Q.BON_QUESTIONS.values()):
        assert all(k == s.id for k, s in bank.items())
    assert set(Q.BON_QUESTIONS) == {"ortho", "geometry", "pbr"}
    assert set(Q.ROUTING_QUESTIONS) == {"pipeline_dag", "needs_ortho_blueprint", "complexity_tier"}
    assert set(Q.ROUTING_QUESTIONS["pipeline_dag"]["criteria"]) == {
        "prop_to_engine", "batch_sanitize", "environment_kit", "character_ready", "lookdev_relight"}
    for qid in Q.VISUAL_QA_QUESTIONS:
        assert qid.startswith(("defect_", "score_")) or qid == "next_action"
    assert all(s.stages for s in Q.VISUAL_QA_QUESTIONS.values())


def test_banks_validate_against_sdk_models():
    banks = {**{k: v.question for k, v in Q.VISUAL_QA_QUESTIONS.items()}, **Q.ROUTING_QUESTIONS, **Q.POLICY_QUESTIONS}
    for b in Q.BON_QUESTIONS.values():
        banks |= {f"{k}_{id(b)}": v.question for k, v in b.items()}
    req = CreateDecisionRequest.model_validate({"model": "m", "input": "x", "questions": banks})
    assert {q.type for q in req.questions.values()} == {"noul", "choice", "score"}
    # and rendered, through the strict fake
    rendered = {k: Q.render(v, ALL_LABELS) for k, v in banks.items()}
    validate_request("m", [{"type": "image", "uri": f"u{i}"} for i in range(3)], rendered)


def test_builders():
    assert Q.score("x", ["a", "b"])["criteria"] == ["a", "b"]
    with pytest.raises(ValueError):
        Q.score("x", ["only"])
    with pytest.raises(ValueError):
        Q.choice("x", {"a": "1"})
    assert "criteria" not in Q.noul("x")


def test_fake_rejects_bad_wire():
    ok_q = {"a": Q.noul("Is Image 1 red?")}
    img = [{"type": "image", "uri": "u"}]
    validate_request("m", img, ok_q)
    with pytest.raises(FakeWireError, match="media_resolution"):
        validate_request("m", [{"type": "image", "uri": "u", "media_resolution": "low"}], ok_q)
    with pytest.raises(FakeWireError, match="list"):
        validate_request("m", img, {"a": {"type": "score", "instructions": "x", "criteria": {"0": "a", "1": "b"}}})
    with pytest.raises(FakeWireError, match="unknown question type"):
        validate_request("m", img, {"a": {"type": "rank", "instructions": "x"}})
    with pytest.raises(FakeWireError, match="1-1000"):
        validate_request("m", img, {f"q{i}": Q.noul("x") for i in range(1001)})
    with pytest.raises(FakeWireError, match="Image 2"):
        validate_request("m", img, {"a": Q.noul("Compare Image 1 with Image 2")})


# ---- estimator -----------------------------------------------------------------------------------

def test_estimator_and_knee_warning(caplog):
    qs = {"a": Q.noul("x" * 400)}
    base = estimate_input_tokens([text_block("y" * 400)], qs)
    with_img = estimate_input_tokens([text_block("y" * 400), image_block("u", "high")], qs)
    assert with_img > base
    assert estimate_input_tokens([image_block("u", "ultra_high")], {}) > estimate_input_tokens([image_block("u", "low")], {})

    async def go():
        p = plane(FakeDecisionsClient())
        with caplog.at_level(logging.WARNING):
            await p.evaluate([text_block("z" * (LATENCY_KNEE_TOKENS * 4 + 8))], qs, tag="big")

    asyncio.run(go())
    assert any("knee" in r.message for r in caplog.records)


# ---- evaluate ------------------------------------------------------------------------------------

async def test_evaluate_normalises_and_costs():
    c = FakeDecisionsClient({"defect_floating": 0.9, "score_silhouette": 3.5, "next_action": "rework_pbr"}, input_tokens=2000)
    p = plane(c)
    qs = {k: v.question for k, v in Q.VISUAL_QA_QUESTIONS.items() if k in ("defect_floating", "score_silhouette", "next_action")}
    r = await p.evaluate("hello", qs, tag="t")
    assert r.noul("defect_floating") == pytest.approx(0.9)
    assert r.score("score_silhouette") == pytest.approx(3.5)
    assert r.choice("next_action") == "rework_pbr"
    assert r.input_tokens == 2000 and r.est_cost_usd == pytest.approx(2000 * PRICE_PER_M_INPUT_USD / 1e6)
    assert r.latency_ms >= 0
    s = p.stats.summary()
    assert s["calls"] == 1 and s["input_tokens"] == 2000 and s["p95_ms"] >= s["p50_ms"]


async def test_retry_then_success_and_exhaustion():
    c = FakeDecisionsClient(errors=[HttpErr(503), HttpErr(429)])
    p = plane(c)
    r = await p.evaluate("x", {"a": Q.noul("q")}, tag="t")
    assert r.attempts == 3 and p.stats.retries == 2
    c2 = FakeDecisionsClient(errors=[HttpErr(503)] * 10)
    with pytest.raises(TransientDecisionError):
        await plane(c2, max_retries=2).evaluate("x", {"a": Q.noul("q")}, tag="t")
    assert len(c2.calls) == 3
    c3 = FakeDecisionsClient(errors=[HttpErr(400)])
    with pytest.raises(HttpErr):  # non-transient: no retry
        await plane(c3).evaluate("x", {"a": Q.noul("q")}, tag="t")


async def test_prompt_feedback_block_is_typed():
    p = plane(FakeDecisionsClient(block_reason="image_safety"))
    with pytest.raises(PromptBlockedError) as ei:
        await p.evaluate("x", {"a": Q.noul("q")}, tag="t")
    assert ei.value.reason == "image_safety"
    with pytest.raises(ValueError):
        await p.evaluate("x", {}, tag="t")


async def test_route_pipeline():
    c = FakeDecisionsClient({"pipeline_dag": "environment_kit", "needs_ortho_blueprint": 0.8, "complexity_tier": 2.4})
    d = await plane(c).route_pipeline("a modular castle wall", "engine: unreal", "gs://ref.png")
    assert d.pipeline == "environment_kit" and d.needs_ortho and d.complexity_tier == 2
    assert c.calls[0]["n_images"] == 1 and c.calls[0]["input"][1]["resolution"] == "low"
    c2 = FakeDecisionsClient()
    await plane(c2).route_pipeline("p", "m")
    assert c2.calls[0]["n_images"] == 0


# ---- visual QA -----------------------------------------------------------------------------------

async def test_blockout_is_geometry_only_and_low_res():
    c = FakeDecisionsClient()
    r = await plane(c).evaluate_visual_qa("gs://sheet.png", stage="blockout")
    asked = set(c.calls[0]["questions"])
    assert not asked & {"defect_cg_plastic", "defect_uv_seam", "defect_baked_light", "score_pbr_realism", "next_action",
                        "score_material_separation", "defect_ip_or_text"}
    assert {"defect_floating", "score_silhouette", "defect_clipping"} <= asked
    assert all(b["resolution"] == "low" for b in c.calls[0]["input"] if b["type"] == "image")
    assert r.resolution == "low" and "defect_cg_plastic" in r.skipped and r.next_action is None


@pytest.mark.parametrize("stage,res", [("lookdev", "high"), ("final", "ultra_high")])
async def test_stage_resolution(stage, res):
    c = FakeDecisionsClient({"next_action": "approve_export"})
    r = await plane(c).evaluate_visual_qa("gs://s.png", "gs://o.png", "gs://st.png", stage=stage)
    imgs = [b for b in c.calls[0]["input"] if b["type"] == "image"]
    assert len(imgs) == 3 and all(b["resolution"] == res for b in imgs)
    assert r.next_action == "approve_export" and "defect_cg_plastic" in r.defects and "score_shippability" in r.scores
    assert r.skipped == ()


async def test_missing_images_drop_their_questions():
    c = FakeDecisionsClient()
    r = await plane(c).evaluate_visual_qa("gs://s.png", stage="lookdev")  # fake would raise if Image 2/3 were referenced
    asked = set(c.calls[0]["questions"])
    assert not asked & {"defect_missing_parts", "defect_asymmetry", "score_proportion_accuracy", "defect_style_mismatch"}
    assert {"defect_missing_parts", "defect_style_mismatch"} <= set(r.skipped)
    # style only: its ordinal shifts to Image 2
    c2 = FakeDecisionsClient()
    await plane(c2).evaluate_visual_qa("gs://s.png", None, "gs://st.png", stage="lookdev")
    assert "Image 2" in c2.calls[0]["questions"]["defect_style_mismatch"]["instructions"]
    assert "defect_missing_parts" not in c2.calls[0]["questions"]


# ---- best of N / policy --------------------------------------------------------------------------

async def test_rank_candidates_concurrent_and_independent():
    scores = {"s1": 0.9, "s2": 0.1, "s3": 0.5, "s4": 0.2}

    def responder(req):
        uri = [b["uri"] for b in req["input"] if b["type"] == "image"][0]
        return {"defect_floating": scores[uri]}

    c = FakeDecisionsClient(responder=responder, latency_s=0.05)
    out = await plane(c).rank_candidates("geometry", list(scores), ["gs://blueprint.png"])
    assert len(c.calls) == 4 and c.max_in_flight == 4
    assert [o.defects["defect_floating"] for o in out] == pytest.approx(list(scores.values()))
    assert "score_silhouette_vs_blueprint" in out[0].scores
    assert all(b["resolution"] == "low" for b in c.calls[0]["input"] if b["type"] == "image")
    # without context the blueprint question is dropped
    c2 = FakeDecisionsClient()
    out2 = await plane(c2).rank_candidates("pbr", ["a", "b"])
    assert set(out2[0].scores) == {"score_tileability", "score_realism"} and set(out2[0].defects) == {"defect_baked_light", "defect_uv_seam"}


async def test_rank_candidates_blocked_candidate_marked():
    c = FakeDecisionsClient(block_if=lambda req: any(b.get("uri") == "bad" for b in req["input"]))
    out = await plane(c).rank_candidates("ortho", ["good", "bad"])
    assert [o.blocked for o in out] == [False, True]


async def test_policy_gate():
    p = plane(FakeDecisionsClient({"policy_trademark_logo": 0.9}))
    v = await p.policy_gate("gs://x.png")
    assert not v.allowed and v.needs_review and "policy_trademark_logo" in v.reasons
    ok = await plane(FakeDecisionsClient()).policy_gate("gs://x.png")
    assert ok.allowed and not ok.needs_review
    blocked = await plane(FakeDecisionsClient(block_reason="prohibited_content")).policy_gate("gs://x.png")
    assert not blocked.allowed and blocked.blocked_by_filter


# ---- live ----------------------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY")
async def test_live_decisions():
    from google import genai

    p = DecisionControlPlane(genai.Client())
    try:
        r = await p.route_pipeline("a wooden crate prop for a game", "target: unreal_5")
    except Exception as e:  # noqa: BLE001
        if getattr(e, "status_code", getattr(e, "code", None)) == 404 or type(e).__name__ == "NotFoundError":
            pytest.skip(f"decisions endpoint/model not available for this key: {e}")
        raise
    assert r.pipeline in Q.ROUTING_QUESTIONS["pipeline_dag"]["criteria"]
