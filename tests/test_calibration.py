from __future__ import annotations

import numpy as np
import pytest

from antigravity3d.decisions.calibration import (
    LabelledAsset, approval_score, auc, calibrate, calibrate_question, expected_calibration_error, load_studio_policy, roc_curve,
    run_shadow_calibration, write_studio_policy,
)
from antigravity3d.decisions.compose import Thresholds
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient


def test_roc_auc_hand_computed():
    # classic example: scores .1 .4 .35 .8, labels 0 0 1 1 -> AUC 0.75
    fpr, tpr, thr = roc_curve([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1])
    assert list(fpr) == [0, 0, 0.5, 0.5, 1.0] and list(tpr) == [0, 0.5, 0.5, 1.0, 1.0]
    assert thr[1] == 0.8
    assert auc(fpr, tpr) == pytest.approx(0.75)


def test_auc_perfect_inverse_and_ties():
    assert auc(*roc_curve([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0])[:2]) == 1.0
    assert auc(*roc_curve([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1])[:2]) == 0.0
    assert auc(*roc_curve([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0])[:2]) == pytest.approx(0.5)


def test_ece_hand_computed():
    assert expected_calibration_error([0.1, 0.1, 0.9, 0.9], [0, 0, 1, 1], 10) == pytest.approx(0.1)
    # one bin (n_bins=1): |mean(y) - mean(p)| = |0.5 - 0.5|
    assert expected_calibration_error([0.2, 0.8], [0, 1], 1) == pytest.approx(0.0)
    # p=0.8 in bin [0.8,0.9), accuracy 0.5 -> 0.3 ; p=1.0 lands in the last bin, correct -> 0
    assert expected_calibration_error([0.8, 0.8, 1.0, 1.0], [1, 0, 1, 1], 10) == pytest.approx(0.5 * 0.3)
    assert expected_calibration_error([0.7] * 10, [1] * 7 + [0] * 3, 10) == pytest.approx(0.0)


def test_approval_score_mapping():
    assert approval_score("defect_floating", 0.2) == pytest.approx(0.8)
    assert approval_score("score_silhouette", 3.0) == pytest.approx(0.75)


def synthetic(n=2000, seed=0):
    """P(approved) = s by construction (calibrated); uv_seam is uninformative noise."""
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n):
        s = rng.random()
        ok = bool(rng.random() < s)
        recs.append((f"a{i}", "defect_cg_plastic", float(np.clip(1 - s, 0.01, 0.99)), ok))
        recs.append((f"a{i}", "defect_uv_seam", float(rng.random()), ok))
        recs.append((f"a{i}", "score_silhouette", 3.5 if ok else 1.5, ok))
    return recs


def test_threshold_selection_and_gating():
    rep = calibrate(synthetic(), n_bins=10)
    cg = rep.per_question["defect_cg_plastic"]
    assert cg.auc > 0.7 and cg.ece < 0.05 and cg.calibrated
    assert cg.auto_approve is not None and cg.director_max is not None and cg.director_max < cg.auto_approve
    assert cg.auto_approve >= 0.85  # precision >= .95 needs s ~ .9+
    assert cg.director_max <= 0.15
    noise = rep.per_question["defect_uv_seam"]
    assert noise.auc == pytest.approx(0.5, abs=0.1) and noise.auto_approve is None and not noise.calibrated
    assert rep.per_question["score_silhouette"].ece is None and not rep.per_question["score_silhouette"].calibrated
    assert "defect_uv_seam" in rep.not_calibrated()


def test_ece_flag_blocks_auto_gate():
    # perfectly separable but over-confident (p=.99 on everything it says is bad, .5 when good)
    recs = [("a", "defect_x", 0.5, True)] * 20 + [("a", "defect_x", 0.99, False)] * 20
    q = calibrate_question("defect_x", [r[2] for r in recs], [r[3] for r in recs])
    assert q.auc == 1.0 and q.ece > 0.05 and not q.calibrated


def test_auto_threshold_picks_highest_recall_meeting_precision():
    # approval scores: approved at .9 .9 .8 .7 ; rejected at .85 .3  -> precision>=0.95 only for s>=.9 (recall .5)
    preds = [0.1, 0.1, 0.2, 0.3, 0.15, 0.7]  # defect probs -> s = .9 .9 .8 .7 .85 .3
    q = calibrate_question("defect_x", preds, [True, True, True, True, False, False], target_precision=0.95)
    assert q.auto_approve == pytest.approx(0.9) and q.recall_at_auto == pytest.approx(0.5)
    q2 = calibrate_question("defect_x", preds, [True, True, True, True, False, False], target_precision=0.6)
    assert q2.auto_approve == pytest.approx(0.7) and q2.recall_at_auto == pytest.approx(1.0)  # s>=.7: 4 of 5 correct = .8


def test_policy_yaml_roundtrip(tmp_path):
    rep = calibrate(synthetic(600))
    th = rep.thresholds()
    assert th.ambiguity_lo < th.ambiguity_hi < th.defect_high
    path = tmp_path / "studio_policy.yaml"
    write_studio_policy(path, th, rep)
    assert load_studio_policy(path) == th
    assert "calibration" in path.read_text()


async def test_run_shadow_calibration_with_fake_plane():
    def responder(req):
        uri = next(b["uri"] for b in req["input"] if b["type"] == "image")
        good = uri.startswith("good")
        return {"defect_floating": 0.05 if good else 0.9, "score_silhouette": 3.8 if good else 1.0}

    plane = DecisionControlPlane(FakeDecisionsClient(responder=responder, latency_s=0.001))
    assets = [LabelledAsset(f"g{i}", f"good{i}", True, stage="blockout") for i in range(12)] + \
             [LabelledAsset(f"b{i}", f"bad{i}", False, stage="blockout") for i in range(12)]
    rep = await run_shadow_calibration(plane, assets)
    assert rep.n_assets == 24 and rep.per_question["defect_floating"].auc == 1.0
    assert rep.per_question["defect_floating"].n == 24
