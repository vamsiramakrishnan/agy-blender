from __future__ import annotations

import os

import pytest
from google.genai._gaos.types.decisions import CreateDecisionRequest

from antigravity3d.decisions import questions as Q
from antigravity3d.decisions.engine import (
    LATENCY_KNEE_TOKENS, PRICE_PER_M_INPUT_USD, DecisionControlPlane, MediaResolution, estimate_input_tokens, image_block,
    text_block,
)
from antigravity3d.decisions.fakes import FakeDecisionsClient, FakeWireError, validate_request
from antigravity3d.decisions.models import DecisionBudgetError, DecisionError, PromptBlockedError, TransientDecisionError


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

async def test_estimator_rejects_at_the_exact_knee_before_dispatch():
    qs = {"a": Q.noul("x" * 400)}
    base = estimate_input_tokens([text_block("y" * 400)], qs)
    with_img = estimate_input_tokens([text_block("y" * 400), image_block("u", "high")], qs)
    assert with_img > base
    assert estimate_input_tokens([image_block("u", "ultra_high")], {}) > estimate_input_tokens([image_block("u", "low")], {})

    c = FakeDecisionsClient()
    p = plane(c)
    base = estimate_input_tokens("", qs)
    await p.evaluate("z" * (LATENCY_KNEE_TOKENS - base - 1), qs, tag="below")
    with pytest.raises(DecisionBudgetError, match="16000"):
        await p.evaluate("z" * (LATENCY_KNEE_TOKENS - base), qs, tag="at")
    assert len(c.calls) == 1
    assert estimate_input_tokens("雪", qs) > estimate_input_tokens("a", qs)


# ---- evaluate ------------------------------------------------------------------------------------

async def test_evaluate_normalises_and_costs():
    c = FakeDecisionsClient({"defect_floating": 0.9, "score_silhouette": 3.5, "next_action": "rework_pbr"}, input_tokens=2000)
    p = plane(c, backend_kind="decisions")
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

@pytest.mark.parametrize("kind,n", [("ortho", 4), ("pbr", 6), ("geometry", 8)])
async def test_rank_candidates_one_native_packet_with_indexed_policy_and_shared_reference(kind, n):
    key = {"ortho": "score_symmetry", "pbr": "score_tileability", "geometry": "score_silhouette_vs_blueprint"}[kind]
    values = [i % 5 for i in range(n)]
    answers = {f"{key}__{i + 1}": value for i, value in enumerate(values)}
    answers["policy_legible_text__2"] = 0.8
    client = FakeDecisionsClient(answers, input_tokens=2187)
    subject = plane(client)
    uris = [f"gs://cand{i}" for i in range(n)]
    out = await subject.rank_candidates(kind, uris, ["gs://blueprint"])
    assert [s.scores[key] for s in out] == values and [s.uri for s in out] == uris
    assert [s.index for s in out if s.blocked] == [1]
    assert len(client.calls) == 1 and subject.stats.input_tokens == 2187
    assert all(s.result is out[0].result for s in out)
    request = client.calls[0]
    image_uris = [b["uri"] for b in request["input"]]
    assert image_uris == (["gs://blueprint"] if kind == "geometry" else []) + uris
    assert all(b["resolution"] == "low" for b in request["input"])
    for i, uri in enumerate(uris, 1):
        label = f"Image {image_uris.index(uri) + 1}"
        assert label in request["questions"][f"{key}__{i}"]["instructions"]
        assert label in request["questions"][f"policy_legible_text__{i}"]["instructions"]
    assert estimate_input_tokens(request["input"], request["questions"]) < LATENCY_KNEE_TOKENS


async def test_rank_partitions_without_regrading_and_deduplicates_images():
    client = FakeDecisionsClient()
    uris = [f"candidate{i}" for i in range(18)]
    uris[-1] = uris[-2]
    out = await plane(client).rank_candidates("pbr", uris, resolution=MediaResolution.ULTRA_HIGH)
    assert [s.uri for s in out] == uris and all(not s.blocked for s in out)
    assert 1 < len(client.calls) < len(uris)
    all_ids = []
    for call in client.calls:
        assert estimate_input_tokens(call["input"], call["questions"]) < LATENCY_KNEE_TOKENS
        assert len({b["uri"] for b in call["input"]}) == len(call["input"])
        all_ids.extend(call["questions"])
    assert len(set(all_ids)) == len(all_ids) == 18 * (len(Q.BON_QUESTIONS["pbr"]) + len(Q.POLICY_QUESTIONS))


async def test_provider_filtered_batch_is_quarantined_without_resubmission():
    c = FakeDecisionsClient(block_if=lambda req: any(b.get("uri") == "bad" for b in req["input"]), input_tokens=545)
    p = plane(c)
    out = await p.rank_candidates("ortho", ["good", "bad"])
    assert [o.blocked for o in out] == [True, True]
    assert all(o.policy.blocked_by_filter for o in out)
    assert len(c.calls) == p.stats.calls == 1 and p.stats.input_tokens == 545


async def test_fallback_and_review_band_cannot_select_a_winner():
    out = await plane(FakeDecisionsClient(uncalibrated=True)).rank_candidates("pbr", ["a", "b"])
    assert all(s.blocked and s.policy.needs_review for s in out)
    review = await plane(FakeDecisionsClient({"policy_legible_text": 0.26})).policy_gate("image")
    assert not review.allowed and review.needs_review and "review policy_legible_text" in review.reasons


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
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY") and os.getenv("AGY3D_REQUIRE_NATIVE") != "1", reason="needs GEMINI_API_KEY")
async def test_live_decisions():
    from google import genai

    p = DecisionControlPlane(genai.Client())
    try:
        r = await p.route_pipeline("a wooden crate prop for a game", "target: unreal_5")
    except Exception as e:  # noqa: BLE001
        if os.getenv("AGY3D_REQUIRE_NATIVE") != "1" and (
                getattr(e, "status_code", getattr(e, "code", None)) == 404 or type(e).__name__ == "NotFoundError"):
            pytest.skip(f"decisions endpoint/model not available for this key: {e}")
        raise
    assert r.pipeline in Q.ROUTING_QUESTIONS["pipeline_dag"]["criteria"]
    assert r.result.source == "decisions" and r.result.calibrated and r.result.cost_known
    assert 0 < r.result.input_tokens < LATENCY_KNEE_TOKENS and r.result.output_tokens == 0
    assert r.result.est_cost_usd == pytest.approx(r.result.input_tokens * PRICE_PER_M_INPUT_USD / 1e6)

    # Exercise image ordinals, inline media and indexed question keys against the actual endpoint.
    import base64
    import io

    from PIL import Image

    images = []
    for colour in ("red", "green", "blue", "yellow"):
        data = io.BytesIO()
        Image.new("RGB", (64, 64), colour).save(data, format="PNG")
        images.append("data:image/png;base64," + base64.b64encode(data.getvalue()).decode("ascii"))
    out = await p.rank_candidates("ortho", images)
    assert [s.index for s in out] == [0, 1, 2, 3] and p.stats.by_tag["bon:ortho"] == 1
    packet = out[0].result
    assert packet and packet.source == "decisions" and packet.calibrated and packet.cost_known
    assert all(s.result is packet and not s.error for s in out) and len(packet.answers) == 32
    assert 0 < packet.input_tokens < LATENCY_KNEE_TOKENS and packet.output_tokens == 0
    assert packet.est_cost_usd == pytest.approx(packet.input_tokens * PRICE_PER_M_INPUT_USD / 1e6)


# ---- engine parity / LOD pop / rig ROM / set-of-mark ---------------------------------------------------------

def _validated(bank, labels):
    qs = {k: Q.render(s.question, labels) for k, s in bank.items()}
    validate_request("m", [{"type": "image", "uri": "u"}] * 2, qs)
    CreateDecisionRequest.model_validate({"model": "m", "input": "x", "questions": qs})
    return qs


def test_new_banks_follow_conventions():
    for bank in (Q.ENGINE_PARITY_QUESTIONS, Q.LOD_POP_QUESTIONS, Q.RIG_ROM_QUESTIONS):
        assert all(k == s.id and s.stages for k, s in bank.items())
        assert all(k.startswith(("defect_", "score_")) for k in bank)
    assert set(Q.ENGINE_PARITY_QUESTIONS) == {
        "defect_normal_inverted", "defect_roughness_inverted", "defect_color_shift", "defect_missing_material", "defect_missing_parts",
        "defect_wrong_orientation", "defect_wrong_scale", "score_engine_parity"}
    assert set(Q.LOD_POP_QUESTIONS) == {"defect_silhouette_change", "defect_holes", "defect_shading_discontinuity", "score_lod_continuity"}
    assert set(Q.RIG_ROM_QUESTIONS) == {"defect_joint_pinch", "defect_candy_wrapper", "defect_self_penetration",
                                        "defect_joint_texture_stretch", "score_deformation"}
    qs = _validated(Q.ENGINE_PARITY_QUESTIONS, {"dcc": "Image 1", "engine": "Image 2"})
    assert all("{" not in q["instructions"] for q in qs.values())  # every placeholder rendered
    assert all("Image 1" in q["instructions"] and "Image 2" in q["instructions"] for q in qs.values())  # atomic and self-contained
    lod = _validated(Q.LOD_POP_QUESTIONS, {"sheet": "Image 1", "pair": "3"})
    assert all("pair number 3" in q["instructions"] for q in lod.values())
    rig = _validated(Q.RIG_ROM_QUESTIONS, {"sheet": "Image 1", "pose": "2", "pose_name": "arm_up"})
    assert all("'arm_up'" in q["instructions"] for q in rig.values())
    assert len(Q.part_defect_question("defect_floating", 7, "wheel")["instructions"]) > 50
    assert "[07]" in Q.part_defect_question("defect_floating", 7)["instructions"]
    with pytest.raises(ValueError):
        Q.part_defect_question("defect_nope", 1)
    # stage tags: surface-only questions are not asked at blockout
    asked, skipped = Q.select(Q.ENGINE_PARITY_QUESTIONS, stage="blockout", labels={"dcc": "Image 1", "engine": "Image 2"})
    assert "defect_normal_inverted" in skipped and "defect_missing_parts" in asked


async def test_evaluate_engine_parity():
    c = FakeDecisionsClient({"defect_normal_inverted": 0.8, "score_engine_parity": 2.0})
    r = await plane(c).evaluate_engine_parity("gs://dcc.png", "gs://eng.png")
    req = c.calls[0]
    imgs = [b for b in req["input"] if b["type"] == "image"]
    assert [b["uri"] for b in imgs] == ["gs://dcc.png", "gs://eng.png"] and req["n_images"] == 2
    assert r.defects["defect_normal_inverted"] == pytest.approx(0.8) and r.scores["score_engine_parity"] == pytest.approx(2.0)
    assert set(r.defects) == {k for k in Q.ENGINE_PARITY_QUESTIONS if k.startswith("defect_")}
    assert r.resolution == "high" and r.result.calibrated


async def test_evaluate_lod_pop_one_question_set_per_pair():
    c = FakeDecisionsClient({"defect_holes__2": 0.9, "score_lod_continuity__3": 1.0, "defect_silhouette_change": 0.4})
    r = await plane(c).evaluate_lod_pop("gs://sheet.png", 3)
    assert len(c.calls) == 1 and len(c.calls[0]["questions"]) == 3 * len(Q.LOD_POP_QUESTIONS) and c.calls[0]["n_images"] == 1
    assert "pair number 2" in c.calls[0]["questions"]["defect_holes__2"]["instructions"]
    assert [i.index for i in r.items] == [1, 2, 3]
    assert r.items[1].defects["defect_holes"] == pytest.approx(0.9) and r.items[0].defects["defect_holes"] == pytest.approx(0.05)
    assert r.items[2].scores["score_lod_continuity"] == pytest.approx(1.0) and r.items[0].scores["score_lod_continuity"] == pytest.approx(3.6)
    assert all(i.defects["defect_silhouette_change"] == pytest.approx(0.4) for i in r.items)  # bare-id programming applies to all
    with pytest.raises(ValueError):
        await plane(c).evaluate_lod_pop("gs://sheet.png", 0)
    with pytest.raises(ValueError):
        await plane(c).evaluate_lod_pop("gs://sheet.png", 500)


async def test_evaluate_rig_rom():
    c = FakeDecisionsClient({"defect_candy_wrapper__2": 0.85})
    r = await plane(c).evaluate_rig_rom("gs://poses.png", ["t_pose", "forearm {twist}", "squat"])
    q = c.calls[0]["questions"]
    assert len(q) == 3 * len(Q.RIG_ROM_QUESTIONS) and "'forearm (twist)'" in q["defect_candy_wrapper__2"]["instructions"]
    assert [i.label for i in r.items] == ["t_pose", "forearm (twist)", "squat"]
    assert r.items[1].defects["defect_candy_wrapper"] == pytest.approx(0.85) and r.items[0].defects["defect_candy_wrapper"] == pytest.approx(0.05)
    assert "score_deformation" in r.items[2].scores
    with pytest.raises(ValueError):
        await plane(c).evaluate_rig_rom("gs://poses.png", [])


async def test_locate_defects_single_request_sorted_thresholded():
    legend = {1: "body", 7: "wheel_l", 12: "wheel_r"}
    c = FakeDecisionsClient({"defect_floating__7": 0.9, "defect_floating__12": 0.6, "defect_floating__1": 0.2, "defect_clipping__12": 0.7})
    out = await plane(c).locate_defects("gs://marked.png", legend, ["defect_floating", "defect_clipping"])
    assert len(c.calls) == 1 and len(c.calls[0]["questions"]) == 6
    assert out == {"defect_floating": [("wheel_l", pytest.approx(0.9)), ("wheel_r", pytest.approx(0.6))],
                   "defect_clipping": [("wheel_r", pytest.approx(0.7))]}
    q = c.calls[0]["questions"]["defect_floating__7"]
    assert "[07]" in q["instructions"] and "wheel_l" in q["instructions"] and "Image 1" in q["instructions"]
    out2 = await plane(FakeDecisionsClient({"defect_floating__12": 0.6})).locate_defects("gs://m.png", legend, ["defect_floating"], min_p=0.7)
    assert out2 == {"defect_floating": []}
    assert await plane(c).locate_defects("gs://m.png", legend, []) == {}
    with pytest.raises(ValueError, match="already flagged"):
        await plane(c).locate_defects("gs://m.png", {i: f"n{i}" for i in range(300)}, ["defect_floating", "defect_clipping"])
    with pytest.raises(ValueError):
        await plane(c).locate_defects("gs://m.png", legend, ["defect_made_up"])


async def test_marked_visual_qa_mode_and_default_unchanged():
    c = FakeDecisionsClient()
    await plane(c).evaluate_visual_qa("gs://s.png", marked_legend={1: "body", 2: "lid"})
    t = c.calls[0]["input"][0]["text"]
    assert "[01] = body" in t and "[02] = lid" in t
    c2 = FakeDecisionsClient()
    await plane(c2).evaluate_visual_qa("gs://s.png")
    assert c2.calls[0]["input"][0]["text"] == "Image 1: contact sheet of renders of the asset under review."


async def test_backend_kind_and_uncalibrated_fake():
    assert plane(FakeDecisionsClient()).backend_kind == "fake"
    assert DecisionControlPlane(object()).backend_kind == "decisions"
    c = FakeDecisionsClient(uncalibrated=True, input_tokens=50)
    p = plane(c)
    r = await p.evaluate("x", {"a": Q.noul("q")}, tag="t")
    assert r.calibrated is False and r.cost_known is False and r.input_tokens == 50 and p.stats.uncalibrated_calls == 1
    assert (await plane(FakeDecisionsClient()).evaluate("x", {"a": Q.noul("q")}, tag="t")).calibrated is True


async def test_native_admission_reconciles_retries_and_known_usage(tmp_path):
    from antigravity3d.decisions.engine import MODEL_INPUT_LIMIT
    from antigravity3d.tactics.governor import Governor, plan_for

    gov = Governor(plan_for(0), journal=tmp_path / "spend.db")
    c = FakeDecisionsClient(errors=[HttpErr(503)], input_tokens=2000)
    subject = plane(c, backend_kind="decisions")
    out = await subject.rank_candidates("pbr", ["https://example.com/one.png", "https://example.com/two.png"], governor=gov)
    assert all(not x.blocked for x in out) and out[0].result is out[1].result
    assert out[0].result.booked_usd == pytest.approx((MODEL_INPUT_LIMIT + 2000) * PRICE_PER_M_INPUT_USD / 1e6)
    receipts = gov.receipts()
    assert len(receipts) == 2 and gov.spent == pytest.approx(out[0].result.booked_usd)
    assert [r["source"] for r in receipts] == ["reservation_estimate", "provider_usage"]
    assert receipts[1]["input_tokens"] == 2000 and all(r["category"] == "pbr_vision" for r in receipts)


async def test_missing_usage_and_no_admission_are_distinct(tmp_path):
    from antigravity3d.tactics.governor import Governor, plan_for

    gov = Governor(plan_for(0), journal=tmp_path / "spend.db")
    c = FakeDecisionsClient()
    subject = plane(c, backend_kind="decisions")
    res = await subject.policy_gate("https://example.com/image.png", governor=gov)
    assert res.allowed and gov.receipts()[0]["source"] == "reservation_estimate"
    assert gov.receipts()[0]["input_tokens"] is None and gov.spent > 0
    gov.charge("previous", gov.remaining)
    out = await subject.rank_candidates("ortho", ["https://example.com/a"], governor=gov)
    assert out[0].blocked and "BudgetExceeded" in out[0].error and len(c.calls) == 1

    fallback = FakeDecisionsClient()
    out = await plane(fallback, backend_kind="generative_fallback").rank_candidates("pbr", ["img"], governor=gov)
    assert out[0].blocked and out[0].policy.needs_review and not fallback.calls


async def test_sdk_http_filter_and_question_ids():
    failure = HttpErr(400)
    failure.body = '{"error":{"code":"image_safety","message":"blocked"}}'
    c = FakeDecisionsClient(errors=[failure])
    p = plane(c, backend_kind="decisions")
    with pytest.raises(ValueError, match="question IDs"):
        await p.evaluate("x", {"bad@1": Q.noul("question")}, tag="bad")
    with pytest.raises(ValueError, match="HTTPS"):
        await p.policy_gate("gs://bucket/image.png")
    assert not c.calls
    verdict = await p.policy_gate("data:image/png;base64,YWJj")
    assert not verdict.allowed and verdict.blocked_by_filter
    assert c.calls[0]["input"][0] == {"type": "image", "mime_type": "image/png", "resolution": "medium", "data": "YWJj"}


async def test_pose_sheet_overflow_keeps_all_labels_and_reconciles_per_packet():
    c = FakeDecisionsClient(input_tokens=256)
    names = [f"shoulder {i}" for i in range(40)]
    out = await plane(c).evaluate_rig_rom("poses", names)
    assert len(c.calls) > 1
    assert [it.label for it in out.items] == names
    assert all(estimate_input_tokens(req["input"], req["questions"]) < LATENCY_KNEE_TOKENS for req in c.calls)
    assert all(len(it.defects) + len(it.scores) == len(Q.RIG_ROM_QUESTIONS) for it in out.items)
    assert out.result.input_tokens == len(c.calls) * 256
