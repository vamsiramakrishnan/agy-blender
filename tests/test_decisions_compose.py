from __future__ import annotations

import pytest

from antigravity3d.decisions.compose import (
    Thresholds, decide_action, engine_parity_ok, engine_parity_reasons, lod_pop_ok, lod_pop_reasons, rank, rig_rom_ok, tier15_score,
)
from antigravity3d.decisions.models import (
    CandidateScore, DecisionResult, EngineParityResult, ItemQA, LodPopResult, RigRomResult, VisualQAResult,
)

TH = Thresholds()
GOOD = {"score_silhouette": 3.8, "score_pbr_realism": 3.8, "score_shippability": 3.8,
        "score_proportion_accuracy": 3.8, "score_material_separation": 3.8}


def qa(defects=None, scores=None, stage="lookdev", na="approve_export", na_probs=None) -> VisualQAResult:
    res = DecisionResult("t", {}, 1.0, 1, 1, 0.0)
    all_scores = (GOOD if stage != "blockout" else {}) | (scores or {})
    return VisualQAResult(stage, defects or {}, all_scores, na, na_probs if na_probs is not None else {"approve_export": 0.9}, (), res)


def test_thresholds_defaults_and_yaml_roundtrip(tmp_path):
    assert (TH.defect_high, TH.ambiguity_lo, TH.ambiguity_hi, TH.silhouette_lock, TH.floating_max,
            TH.export_shippability_min, TH.cg_plastic_silhouette_floor) == (0.78, 0.30, 0.75, 3.3, 0.10, 3.4, 3.2)
    t = Thresholds(defect_high=0.8, floating_max=0.07)
    t.dump(tmp_path / "p.yaml")
    assert Thresholds.load(tmp_path / "p.yaml") == t
    assert Thresholds.from_dict({"defect_high": 0.9, "bogus": 1}).defect_high == 0.9


def test_tier15():
    assert tier15_score(qa()) == pytest.approx(3.8)
    assert tier15_score(qa({"defect_cg_plastic": 0.2})) == pytest.approx(3.8)  # below floor: free
    # p=1.0 on a severity-1 defect costs exactly 1 point
    assert tier15_score(qa({"defect_floating": 1.0})) == pytest.approx(2.8)
    assert tier15_score(qa({"defect_cg_plastic": 0.65})) == pytest.approx(3.8 - 0.5 * 0.5)
    many = {k: 1.0 for k in ("defect_floating", "defect_clipping", "defect_inverted_faces", "defect_missing_parts")}
    assert tier15_score(qa(many)) == pytest.approx(3.8 - TH.defect_penalty_cap)
    assert tier15_score(qa(scores={k: 0.0 for k in GOOD}, defects=many)) == 0.0
    assert tier15_score(qa(stage="blockout", scores={"score_silhouette": 3.0})) == pytest.approx(3.0)


def test_approve():
    a = decide_action(qa(), TH, "lookdev")
    assert a.kind == "approve" and a.fixer is None
    lines = a.audit_delta().split("\n")
    assert len(lines) == 2 and "approve" in lines[0] and "tier15" in lines[0]


def test_blockout_approve_only_needs_geometry():
    r = qa(stage="blockout", scores={"score_silhouette": 3.5}, na=None, na_probs={})
    assert decide_action(r, TH, "blockout").kind == "approve"
    low = qa(stage="blockout", scores={"score_silhouette": 3.0}, na=None, na_probs={})
    a = decide_action(low, TH, "blockout")
    assert a.kind == "agent_repair" and a.target == "geometry"


def test_auto_fix_cg_plastic_isolated():
    a = decide_action(qa({"defect_cg_plastic": 0.9}), TH, "lookdev")
    assert a.kind == "auto_fix" and a.fixer == "roughness_breakup"
    assert "roughness_breakup" in a.audit_delta()


def test_cg_plastic_with_weak_silhouette_goes_to_agent():
    a = decide_action(qa({"defect_cg_plastic": 0.9}, {"score_silhouette": 3.0}), TH, "lookdev")
    assert a.kind == "agent_repair" and a.fixer is None


def test_auto_fix_floating_reground():
    a = decide_action(qa({"defect_floating": 0.9}), TH, "lookdev")
    assert a.kind == "auto_fix" and a.fixer == "reground"


def test_high_defect_without_fixer_is_agent_repair():
    a = decide_action(qa({"defect_clipping": 0.9}), TH, "lookdev")
    assert a.kind == "agent_repair" and a.fixer is None and a.target == "geometry"


def test_multiple_high_defects_agent_repair():
    a = decide_action(qa({"defect_cg_plastic": 0.9, "defect_baked_light": 0.85}), TH, "lookdev")
    assert a.kind == "agent_repair" and a.fixer is None


def test_high_plus_ambiguous_is_not_isolated():
    a = decide_action(qa({"defect_cg_plastic": 0.9, "defect_uv_seam": 0.5}), TH, "lookdev")
    assert a.kind == "agent_repair"


def test_ambiguous_only_escalates_director():
    a = decide_action(qa({"defect_uv_seam": 0.5}), TH, "lookdev")
    assert a.kind == "escalate_director" and "defect_uv_seam" in a.reasons[0]


def test_ambiguous_with_high_signal_does_not_go_to_director():
    assert decide_action(qa({"defect_uv_seam": 0.5, "defect_clipping": 0.95}), TH, "lookdev").kind == "agent_repair"


def test_ip_or_text_escalates_human():
    assert decide_action(qa({"defect_ip_or_text": 0.9}), TH, "final").kind == "escalate_human"


def test_floating_gate_blocks_approval_below_ambiguity_band():
    a = decide_action(qa({"defect_floating": 0.15}), TH, "lookdev")
    assert a.kind == "agent_repair" and "floating_max" in a.reasons[0]
    assert decide_action(qa({"defect_floating": 0.10}), TH, "lookdev").kind == "approve"


def test_likely_but_below_high_cut_is_agent_repair():
    assert decide_action(qa({"defect_uv_seam": 0.76}), TH, "lookdev").kind == "agent_repair"


def test_low_shippability_and_silhouette_gates():
    a = decide_action(qa(scores={"score_shippability": 3.0}), TH, "final")
    assert a.kind == "agent_repair" and any("shippability" in r for r in a.reasons)
    a = decide_action(qa(scores={"score_silhouette": 3.2}), TH, "final")
    assert a.kind == "agent_repair" and a.target == "geometry"


def test_model_is_not_the_authority():
    a = decide_action(qa(scores={"score_shippability": 2.0}, na="approve_export"), TH, "final")
    assert a.kind == "agent_repair" and any("gates win" in r for r in a.reasons)
    # rework hint used for the repair target when the gates give none
    a = decide_action(qa(scores={"score_shippability": 3.0}, na="rework_lighting"), TH, "final")
    assert a.kind == "agent_repair"
    # clean gates but model strongly dissents -> a human director looks
    a = decide_action(qa(na="escalate_human", na_probs={"approve_export": 0.05}), TH, "final")
    assert a.kind == "escalate_director"
    # mild model disagreement does not block
    assert decide_action(qa(na="rework_pbr", na_probs={"approve_export": 0.4}), TH, "final").kind == "approve"


def test_audit_delta_two_lines_even_with_newlines():
    a = decide_action(qa({"defect_uv_seam": 0.5}), TH, "lookdev")
    assert len(a.audit_delta().split("\n")) == 2


def cand(i, defects=None, scores=None, blocked=False):
    return CandidateScore(i, f"u{i}", "pbr", defects or {}, scores or {}, blocked)


def test_rank_orders_and_breaks_ties_deterministically():
    cs = [cand(0, {"defect_baked_light": 0.5}, {"score_realism": 2.0}),
          cand(1, {"defect_baked_light": 0.1}, {"score_realism": 3.5}),
          cand(2, {"defect_baked_light": 0.1}, {"score_realism": 3.5}),
          cand(3, blocked=True)]
    out = rank(cs, "pbr")
    assert [c.index for c in out] == [1, 2, 0, 3]
    assert out[0].utility == pytest.approx((0.9 + 3.5 / 4) / 2)
    assert [c.index for c in rank(list(reversed(cs)), "pbr")] == [1, 2, 0, 3]


def test_rank_confirmed_defect_loses_despite_higher_average():
    bad = cand(0, {"defect_floating": 0.95}, {"s_a": 4.0, "score_x": 4.0, "score_y": 4.0, "score_z": 4.0})
    ok = cand(1, {"defect_floating": 0.2}, {"score_x": 2.0})
    assert [c.index for c in rank([bad, ok], "geometry")] == [1, 0]


# ---- uncalibrated results (generative fallback) ----------------------------------------------------------------

def uncal(qa_result: VisualQAResult) -> VisualQAResult:
    from dataclasses import replace

    return replace(qa_result, result=replace(qa_result.result, calibrated=False))


def test_calibrated_behaviour_unchanged_and_flag_default():
    assert Thresholds().trust_uncalibrated is False
    assert decide_action(qa()).kind == "approve"
    assert decide_action(qa({"defect_baked_light": 0.9})).kind == "auto_fix"


def test_uncalibrated_never_auto_approves():
    good = {k: 4.0 for k in GOOD}
    a = decide_action(uncal(qa(scores=good, defects={"defect_cg_plastic": 0.02})))
    assert a.kind == "escalate_director" and "uncalibrated" in a.reasons[0] and "approve withheld" in a.reasons[0]
    assert "uncalibrated" in a.audit_delta()
    # near a gate -> agent_repair on that target instead
    near = decide_action(uncal(qa(scores={"score_silhouette": 3.4})))  # lock 3.3 + margin 0.2
    assert near.kind == "agent_repair" and near.target == "geometry" and "near a gate" in near.reasons[1]
    near_d = decide_action(uncal(qa(defects={"defect_texture_blur": 0.2}, scores=good)))
    assert near_d.kind == "agent_repair" and near_d.target == "pbr"
    # opt in restores identical behaviour
    opt = Thresholds(trust_uncalibrated=True)
    assert decide_action(uncal(qa(scores=good)), opt) == decide_action(qa(scores=good), opt)
    assert decide_action(uncal(qa(scores=good)), opt).kind == "approve"


def test_uncalibrated_never_auto_fixes_but_other_actions_pass_through():
    r = decide_action(uncal(qa({"defect_baked_light": 0.9})))
    assert r.kind == "agent_repair" and r.fixer is None and r.target == "pbr" and "auto_fix withheld" in r.reasons[0]
    assert decide_action(uncal(qa({"defect_floating": 0.9}))).kind == "agent_repair"
    assert decide_action(uncal(qa({"defect_ip_or_text": 0.9}))).kind == "escalate_human"
    assert decide_action(uncal(qa({"defect_cg_plastic": 0.5}))).kind == "escalate_director"
    assert decide_action(uncal(qa({"defect_cg_plastic": 0.5}))).reasons == decide_action(qa({"defect_cg_plastic": 0.5})).reasons


def test_thresholds_new_fields_roundtrip(tmp_path):
    t = Thresholds(trust_uncalibrated=True, engine_parity_min=3.5, lod_continuity_min=2.5)
    t.dump(tmp_path / "p.yaml")
    t2 = Thresholds.load(tmp_path / "p.yaml")
    assert t2 == t and t2.trust_uncalibrated is True and Thresholds.load(tmp_path / "p.yaml").to_dict()["trust_uncalibrated"] is True
    assert Thresholds.from_dict(Thresholds().to_dict()) == Thresholds()
    assert Thresholds.from_dict({"trust_uncalibrated": 0.0}).trust_uncalibrated is False


def _parity(defects=None, score=3.8, calibrated=True) -> EngineParityResult:
    base = {k: 0.05 for k in ("defect_normal_inverted", "defect_roughness_inverted", "defect_color_shift", "defect_missing_material")}
    return EngineParityResult({**base, **(defects or {})}, {"score_engine_parity": score},
                              DecisionResult("t", {}, 1.0, 1, 1, 0.0, calibrated=calibrated))


def test_engine_parity_gate():
    assert engine_parity_ok(_parity())
    assert not engine_parity_ok(_parity({"defect_normal_inverted": 0.3}))  # at the threshold fails
    assert engine_parity_ok(_parity({"defect_normal_inverted": 0.29}))
    assert not engine_parity_ok(_parity(score=2.9)) and engine_parity_ok(_parity(score=3.0))
    reasons = engine_parity_reasons(_parity({"defect_color_shift": 0.9}, 1.0))
    assert any("defect_color_shift" in r for r in reasons) and any("score_engine_parity" in r for r in reasons)
    assert engine_parity_ok(_parity(defects={"defect_normal_inverted": 0.5}), Thresholds(engine_parity_defect_max=0.6))
    assert not engine_parity_ok(_parity(calibrated=False))
    assert engine_parity_ok(_parity(calibrated=False), Thresholds(trust_uncalibrated=True))
    assert not engine_parity_ok(EngineParityResult({}, {}, DecisionResult("t", {}, 1.0, 1, 1, 0.0)))  # missing score never passes


def _lod(pairs, calibrated=True) -> LodPopResult:
    return LodPopResult([ItemQA(i, f"pair {i}", d, {"score_lod_continuity": s}) for i, (d, s) in enumerate(pairs, 1)],
                        DecisionResult("t", {}, 1.0, 1, 1, 0.0, calibrated=calibrated))


def test_lod_pop_gate_every_pair_must_pass():
    ok = ({"defect_holes": 0.05, "defect_silhouette_change": 0.1}, 3.6)
    assert lod_pop_ok(_lod([ok, ok]))
    assert not lod_pop_ok(_lod([ok, ({"defect_holes": 0.8}, 3.6)]))
    assert any("pair 2" in r for r in lod_pop_reasons(_lod([ok, ({"defect_holes": 0.8}, 2.0)])))
    assert not lod_pop_ok(_lod([({}, 2.9)])) and lod_pop_ok(_lod([({}, 2.9)]), Thresholds(lod_continuity_min=2.5))
    assert not lod_pop_ok(_lod([ok], calibrated=False)) and lod_pop_ok(_lod([ok], calibrated=False), Thresholds(trust_uncalibrated=True))


def test_rig_rom_gate():
    r = RigRomResult([ItemQA(1, "t_pose", {"defect_joint_pinch": 0.1}, {"score_deformation": 3.5}),
                      ItemQA(2, "squat", {"defect_joint_pinch": 0.6}, {"score_deformation": 3.5})],
                     DecisionResult("t", {}, 1.0, 1, 1, 0.0))
    assert not rig_rom_ok(r) and rig_rom_ok(RigRomResult(r.items[:1], r.result))
