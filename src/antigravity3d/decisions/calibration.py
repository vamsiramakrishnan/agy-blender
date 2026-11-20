"""Studio rubric calibration: ROC/AUC, ECE and threshold selection from labelled history.

Everything is expressed as an *approval score* s in [0,1] (higher = more likely approved):
defect_*/policy_* questions use s = 1 - P(defect); score_* rubrics use s = score / 4; other ids use
the raw value. Labels are `approved` booleans from human review.

Threshold ladder on s (the brief's ordering): s >= auto_approve -> AUTO_APPROVE;
s <= director_max -> HUMAN_ART_DIRECTOR (confidently bad, a person decides); in between -> AGENT_REPAIR.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
import yaml

from .compose import Thresholds
from .engine import DecisionControlPlane
from .models import Stage

Record = tuple[str, str, float, bool]  # asset_id, question_id, predicted_probability, approved
ECE_MAX = 0.05
TARGET_PRECISION = 0.95


def approval_score(question_id: str, value: float) -> float:
    if question_id.startswith(("defect_", "policy_")):
        return 1.0 - value
    if question_id.startswith("score_"):
        return value / 4.0
    return value


def roc_curve(y_score: Sequence[float], y_true: Sequence[bool]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(fpr, tpr, thresholds); one point per distinct score, prepended with (0, 0, +inf)."""
    s, y = np.asarray(y_score, float), np.asarray(y_true, bool)
    order = np.argsort(-s, kind="stable")
    s, y = s[order], y[order]
    last = np.r_[np.where(np.diff(s))[0], len(s) - 1]  # last index of each tie group
    tp, fp = np.cumsum(y)[last], np.cumsum(~y)[last]
    p, n = max(int(y.sum()), 1), max(int((~y).sum()), 1)
    return np.r_[0.0, fp / n], np.r_[0.0, tp / p], np.r_[np.inf, s[last]]


def auc(fpr: np.ndarray, tpr: np.ndarray) -> float:
    return float(np.sum(np.diff(fpr) * (tpr[1:] + tpr[:-1]) / 2))


def expected_calibration_error(p: Sequence[float], y: Sequence[bool], n_bins: int = 10) -> float:
    """sum_b (n_b / N) * |mean(y in b) - mean(p in b)| over equal-width bins on [0, 1]."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    idx = np.minimum((p * n_bins).astype(int), n_bins - 1)
    ece = 0.0
    for b in range(n_bins):
        m = idx == b
        if m.any():
            ece += m.mean() * abs(y[m].mean() - p[m].mean())
    return float(ece)


@dataclass(frozen=True)
class QuestionCalibration:
    question_id: str
    n: int
    n_approved: int
    auc: float | None
    ece: float | None  # None for rubric scores (not probabilities)
    calibrated: bool  # eligible to auto-gate
    auto_approve: float | None  # s >= this -> AUTO_APPROVE
    director_max: float | None  # s <= this -> HUMAN_ART_DIRECTOR
    recall_at_auto: float | None = None
    roc: tuple[tuple[float, ...], tuple[float, ...]] = field(default=((), ()), repr=False)

    @property
    def is_defect(self) -> bool:
        return self.question_id.startswith(("defect_", "policy_"))


def _auto_threshold(s: np.ndarray, y: np.ndarray, target: float) -> tuple[float | None, float | None]:
    best_t, best_r = None, -1.0
    for t in np.unique(s)[::-1]:  # high to low: first hit at equal recall keeps the strictest threshold
        sel = s >= t
        prec = y[sel].mean()
        rec = (sel & y).sum() / max(y.sum(), 1)
        if prec >= target and rec > best_r:
            best_t, best_r = float(t), float(rec)
    return best_t, (best_r if best_t is not None else None)


def _director_threshold(s: np.ndarray, y: np.ndarray, target: float, below: float | None) -> float | None:
    """Largest t (strictly under the auto-approve cut) such that every prefix s <= t stays mostly rejected."""
    best = None
    for t in np.unique(s):  # ascending; stop at the first prefix that is no longer confidently bad
        if below is not None and t >= below:
            break
        if (~y[s <= t]).mean() < target:
            break
        best = float(t)
    return best


def calibrate_question(qid: str, preds: Sequence[float], approved: Sequence[bool], *, n_bins: int = 10,
                       target_precision: float = TARGET_PRECISION, ece_max: float = ECE_MAX) -> QuestionCalibration:
    y = np.asarray(approved, bool)
    s = np.array([approval_score(qid, v) for v in preds])
    two_class = 0 < y.sum() < len(y)
    fpr, tpr, _ = roc_curve(s, y) if two_class else (np.zeros(1), np.zeros(1), None)
    a = auc(fpr, tpr) if two_class else None
    is_prob = not qid.startswith("score_")
    ece = expected_calibration_error(s, y, n_bins) if is_prob else None
    at, rec = _auto_threshold(s, y, target_precision) if two_class else (None, None)
    dt = _director_threshold(s, y, target_precision, at) if two_class else None
    return QuestionCalibration(qid, len(y), int(y.sum()), a, ece, bool(ece is not None and ece <= ece_max and at is not None),
                               at, dt, rec, (tuple(map(float, fpr)), tuple(map(float, tpr))))


@dataclass
class CalibrationReport:
    per_question: dict[str, QuestionCalibration]
    n_assets: int
    n_bins: int = 10
    target_precision: float = TARGET_PRECISION

    def not_calibrated(self) -> list[str]:
        return sorted(q for q, c in self.per_question.items() if not c.calibrated)

    def thresholds(self, base: Thresholds | None = None) -> Thresholds:
        """Fold per-defect results into global defect-probability cuts (medians over gate-eligible defects)."""
        base = base or Thresholds()
        el = [c for c in self.per_question.values() if c.is_defect and c.calibrated]
        if not el:
            return base
        lo = float(np.median([1 - c.auto_approve for c in el]))  # type: ignore[operator]
        dm = [1 - c.director_max for c in el if c.director_max is not None]
        hi_cut = float(np.median(dm)) if dm else base.defect_high
        hi_cut = max(hi_cut, min(lo + 0.1, 0.99))
        amb_hi = max(lo, min(base.ambiguity_hi, hi_cut - 0.03))
        return Thresholds.from_dict({**base.to_dict(), "ambiguity_lo": lo, "defect_high": hi_cut, "ambiguity_hi": amb_hi})


def calibrate(records: Sequence[Record], *, n_bins: int = 10, target_precision: float = TARGET_PRECISION) -> CalibrationReport:
    by_q: dict[str, tuple[list[float], list[bool]]] = {}
    for _, qid, p, ok in records:
        pr, lb = by_q.setdefault(qid, ([], []))
        pr.append(float(p))
        lb.append(bool(ok))
    return CalibrationReport({q: calibrate_question(q, pr, lb, n_bins=n_bins, target_precision=target_precision)
                              for q, (pr, lb) in sorted(by_q.items())},
                             len({r[0] for r in records}), n_bins, target_precision)


def write_studio_policy(path: str | Path, thresholds: Thresholds, report: CalibrationReport | None = None) -> None:
    doc: dict = {"thresholds": thresholds.to_dict()}
    if report is not None:
        doc["calibration"] = {
            "n_assets": report.n_assets, "n_bins": report.n_bins, "target_precision": report.target_precision,
            "not_calibrated": report.not_calibrated(),
            "questions": {q: {"n": c.n, "auc": c.auc, "ece": c.ece, "calibrated": c.calibrated,
                              "auto_approve": c.auto_approve, "director_max": c.director_max}
                          for q, c in report.per_question.items()},
        }
    Path(path).write_text(yaml.safe_dump(doc, sort_keys=False))


def load_studio_policy(path: str | Path) -> Thresholds:
    return Thresholds.load(path)


@dataclass(frozen=True)
class LabelledAsset:
    asset_id: str
    contact_sheet_uri: str
    approved: bool
    ortho_uri: str | None = None
    style_anchor_uri: str | None = None
    stage: Stage = "final"


async def run_shadow_calibration(plane: DecisionControlPlane, assets: Sequence[LabelledAsset], *, n_bins: int = 10,
                                 target_precision: float = TARGET_PRECISION, concurrency: int = 4) -> CalibrationReport:
    """Run visual QA over human-labelled assets and calibrate every defect/rubric question."""
    sem = asyncio.Semaphore(concurrency)

    async def one(a: LabelledAsset) -> list[Record]:
        async with sem:
            r = await plane.evaluate_visual_qa(a.contact_sheet_uri, a.ortho_uri, a.style_anchor_uri, a.stage)
        return [(a.asset_id, q, float(v), a.approved) for q, v in {**r.defects, **r.scores}.items()]

    recs = [x for batch in await asyncio.gather(*(one(a) for a in assets)) for x in batch]
    return calibrate(recs, n_bins=n_bins, target_precision=target_precision)
