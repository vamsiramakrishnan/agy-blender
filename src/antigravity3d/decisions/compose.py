"""Deterministic composition of Decisions API answers. Pure functions, no I/O.

The model's `next_action` is one input; the gates are the thresholds below.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml

from .models import CandidateScore, Stage, VisualQAResult


@dataclass(frozen=True)
class Thresholds:
    defect_high: float = 0.78  # P(defect) at/above: high-confidence
    ambiguity_lo: float = 0.30  # [lo, defect_high) with no high-confidence signal: ask the director
    ambiguity_hi: float = 0.75
    silhouette_lock: float = 3.3
    floating_max: float = 0.10  # any P(floating) above this blocks approval
    export_shippability_min: float = 3.4
    cg_plastic_silhouette_floor: float = 3.2  # auto-fix roughness only if geometry is sound
    approve_dissent_max: float = 0.20  # model P(approve_export) below this vetoes an otherwise clean approve
    defect_penalty_cap: float = 2.0

    def to_dict(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Thresholds":
        known = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in d.items() if k in known})

    @classmethod
    def load(cls, path: str | Path) -> "Thresholds":
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.from_dict(data.get("thresholds", data))

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(yaml.safe_dump({"thresholds": self.to_dict()}, sort_keys=False))


# Rubric weights (renormalised over the rubrics actually asked at this stage).
RUBRIC_WEIGHTS = {"score_shippability": 0.35, "score_silhouette": 0.20, "score_pbr_realism": 0.20,
                  "score_proportion_accuracy": 0.15, "score_material_separation": 0.10}
# Penalty (in score points) of a certain defect; scaled by how far P exceeds the ambiguity floor.
DEFECT_SEVERITY = {"defect_floating": 1.0, "defect_clipping": 1.0, "defect_inverted_faces": 1.0, "defect_ip_or_text": 1.0,
                   "defect_missing_parts": 1.0, "defect_visible_holes": 1.0}
DEFAULT_SEVERITY = 0.5
MINOR_SEVERITY = {"defect_exposure_bad": 0.25, "defect_tiling_repetition": 0.25, "defect_texture_blur": 0.25,
                  "defect_style_mismatch": 0.25, "defect_faceted": 0.25}
FIXERS = {"defect_cg_plastic": "roughness_breakup", "defect_floating": "reground", "defect_baked_light": "delight"}
REWORK_TARGET = {"rework_pbr": "pbr", "rework_geometry": "geometry", "rework_lighting": "lighting"}
GEOMETRY_DEFECTS = {"defect_floating", "defect_clipping", "defect_inverted_faces", "defect_zfighting", "defect_missing_parts",
                    "defect_asymmetry", "defect_visible_holes", "defect_faceted", "defect_scale_implausible"}


def _severity(qid: str) -> float:
    return DEFECT_SEVERITY.get(qid, MINOR_SEVERITY.get(qid, DEFAULT_SEVERITY))


def tier15_score(result: VisualQAResult, th: Thresholds | None = None) -> float:
    """Aggregate quality on the 0-4 scale.

    base    = weighted mean of the rubric expected scores present (RUBRIC_WEIGHTS, renormalised)
    penalty = min(cap, sum_d severity_d * clamp((p_d - ambiguity_lo) / (1 - ambiguity_lo), 0, 1))
    tier15  = clamp(base - penalty, 0, 4)

    Defects below the ambiguity floor cost nothing, so noise does not erode good assets.
    """
    th = th or Thresholds()
    present = {k: w for k, w in RUBRIC_WEIGHTS.items() if k in result.scores}
    base = sum(result.scores[k] * w for k, w in present.items()) / sum(present.values()) if present else 0.0
    penalty = sum(_severity(k) * min(1.0, max(0.0, (p - th.ambiguity_lo) / (1 - th.ambiguity_lo))) for k, p in result.defects.items())
    return float(min(4.0, max(0.0, base - min(th.defect_penalty_cap, penalty))))


@dataclass(frozen=True)
class Action:
    kind: Literal["approve", "auto_fix", "agent_repair", "escalate_director", "escalate_human"]
    fixer: str | None = None
    reasons: list[str] = field(default_factory=list)
    target: str | None = None  # geometry | pbr | lighting hint for agent_repair
    stage: str = "lookdev"
    tier15: float = 0.0

    def audit_delta(self) -> str:
        """Two lines; the only record of this QA pass that is persisted to model context."""
        head = f"QA[{self.stage}] {self.kind}" + (f" fixer={self.fixer}" if self.fixer else "") + (
            f" target={self.target}" if self.target else "") + f" tier15={self.tier15:.2f}"
        why = " | ".join(r.replace("\n", " ") for r in self.reasons[:4]) or "no findings"
        return f"{head}\nwhy: {why[:240]}"


def decide_action(result: VisualQAResult, th: Thresholds | None = None, stage: Stage | None = None) -> Action:
    th = th or Thresholds()
    stage = stage or result.stage
    t15 = tier15_score(result, th)
    sil = result.scores.get("score_silhouette")
    d = result.defects
    mk = lambda kind, reasons, fixer=None, target=None: Action(kind, fixer, reasons, target, stage, t15)  # noqa: E731

    high = {k: p for k, p in d.items() if p >= th.defect_high}
    mid = {k: p for k, p in d.items() if th.ambiguity_lo <= p < th.defect_high}
    ambiguous = {k: p for k, p in mid.items() if p <= th.ambiguity_hi}
    fmt = lambda m: ", ".join(f"{k}={p:.2f}" for k, p in sorted(m.items()))  # noqa: E731

    if "defect_ip_or_text" in high:
        return mk("escalate_human", [f"IP/text risk {high['defect_ip_or_text']:.2f}: legal call, never auto-resolved"])
    if ambiguous and not high:
        return mk("escalate_director", [f"ambiguous defects with no high-confidence signal: {fmt(ambiguous)}"])

    if high:
        if len(high) == 1 and not mid:
            (qid, p), = high.items()
            fixer = FIXERS.get(qid)
            if fixer and not (qid == "defect_cg_plastic" and (sil is None or sil < th.cg_plastic_silhouette_floor)):
                return mk("auto_fix", [f"isolated high-confidence {qid}={p:.2f}; deterministic fixer {fixer}"], fixer)
            why = [f"{qid}={p:.2f} high-confidence" + ("" if fixer else ", no deterministic fixer")]
            if fixer:
                why.append(f"silhouette {sil if sil is None else round(sil, 2)} below cg-plastic floor {th.cg_plastic_silhouette_floor}")
            return mk("agent_repair", why, target="geometry" if qid in GEOMETRY_DEFECTS or fixer else "pbr")
        tgt = "geometry" if any(k in GEOMETRY_DEFECTS for k in high) else "pbr"
        return mk("agent_repair", [f"multiple/entangled defects: {fmt({**mid, **high})}"], target=tgt)

    reasons: list[str] = []
    target: str | None = None
    if (fp := d.get("defect_floating", 0.0)) > th.floating_max:
        reasons.append(f"defect_floating={fp:.2f} > floating_max {th.floating_max}")
        target = "geometry"
    if mid:  # likely-but-not-high (ambiguity_hi < p < defect_high)
        reasons.append(f"likely defects below high-confidence cut: {fmt(mid)}")
        target = target or ("geometry" if any(k in GEOMETRY_DEFECTS for k in mid) else "pbr")
    if sil is not None and sil < th.silhouette_lock:
        reasons.append(f"silhouette {sil:.2f} < lock {th.silhouette_lock}")
        target = target or "geometry"
    if stage != "blockout":
        shp = result.scores.get("score_shippability")
        if shp is not None and shp < th.export_shippability_min:
            reasons.append(f"shippability {shp:.2f} < {th.export_shippability_min}")
            target = target or "pbr"
        if t15 < th.export_shippability_min:
            reasons.append(f"tier15 {t15:.2f} < {th.export_shippability_min}")
            target = target or "pbr"
    if reasons:
        na = result.next_action
        if na in REWORK_TARGET and target is None:
            target = REWORK_TARGET[na]
        if na == "approve_export":
            reasons.append("model suggested approve_export but gates fail (gates win)")
        return mk("agent_repair", reasons, target=target)

    if result.next_action_probs.get("approve_export", 1.0) < th.approve_dissent_max:
        return mk("escalate_director", [f"gates pass but model P(approve_export)={result.next_action_probs['approve_export']:.2f}; "
                                        f"model prefers {result.next_action}"])
    return mk("approve", [f"all gates pass (tier15 {t15:.2f}" + (f", silhouette {sil:.2f}" if sil is not None else "") + ")"])


def _goodness(qid: str, v: float) -> float:
    return 1.0 - v if qid.startswith("defect_") else v / 4.0


def rank(candidates: list[CandidateScore], kind: str, th: Thresholds | None = None) -> list[CandidateScore]:
    """Best first.

    utility = weighted mean of per-criterion goodness in [0,1] (score/4, or 1 - P(defect));
    critical defects (DEFECT_SEVERITY) weigh 2, everything else 1. Sort key:
    (blocked, number of high-confidence defects, -utility rounded to 6dp, original index),
    so any candidate with a confirmed defect loses to a clean one regardless of average, and ties
    resolve to the earlier candidate deterministically.
    """
    th = th or Thresholds()

    def utility(c: CandidateScore) -> float:
        items = {**{k: v for k, v in c.defects.items()}, **c.scores}
        if not items:
            return 0.0
        w = {k: 2.0 if k in DEFECT_SEVERITY else 1.0 for k in items}
        return sum(w[k] * _goodness(k, v) for k, v in items.items()) / sum(w.values())

    scored = [replace(c, utility=utility(c)) for c in candidates]
    return sorted(scored, key=lambda c: (c.blocked, sum(p >= th.defect_high for p in c.defects.values()),
                                         -round(c.utility or 0.0, 6), c.index))
