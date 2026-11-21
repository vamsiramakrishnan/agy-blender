"""Deterministic composition of Decisions API answers. Pure functions, no I/O.

The model's `next_action` is one input; the gates are the thresholds below.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml

from .models import CandidateScore, EngineParityResult, LodPopResult, RigRomResult, Stage, VisualQAResult


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
    # An uncalibrated result (generative fallback: self-reported probabilities) can never auto_fix or auto-approve
    # unless a studio opts in here. Existing behaviour for calibrated results is unchanged.
    trust_uncalibrated: bool = False
    near_miss_margin: float = 0.20  # uncalibrated "clean" approvals within this of a gate are repaired, not just escalated
    # Engine parity (DCC render vs game-engine render)
    engine_parity_defect_max: float = 0.30  # any parity defect P at/above this fails the gate
    engine_parity_min: float = 3.0  # score_engine_parity expected 0-4
    # LOD pop (per pair)
    lod_pop_defect_max: float = 0.30
    lod_continuity_min: float = 3.0
    # Rig range of motion (per pose)
    rig_defect_max: float = 0.30
    deformation_min: float = 3.0
    # The brief (asked only when a run has one; see questions.BRIEF_*). UNCALIBRATED: 2.5 is the midpoint between level 2
    # of score_brief_depiction ("recognisable but plain ... reads as a blockout") and level 3 ("convincing"), chosen from the
    # level texts, not from measured answers. A part the brief names counts as missing at P >= ambiguity_lo.
    brief_depiction_min: float = 2.5

    def to_dict(self) -> dict[str, float | bool]:
        return {k: v if isinstance(v, bool) else float(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Thresholds":
        flags = {f.name for f in fields(cls) if f.type in (bool, "bool")}
        known = {f.name for f in fields(cls)}
        return cls(**{k: bool(v) if k in flags else float(v) for k, v in d.items() if k in known})

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
                    "defect_asymmetry", "defect_visible_holes", "defect_faceted", "defect_scale_implausible",
                    "defect_brief_part_missing"}
# The brief's answers (present only when a brief was given). They never enter tier15 (a composite can rise as well as
# fall, and no gate may get easier): they are separate requirements (``brief_reasons``), weigh like the critical
# criteria in a ranking, and enter pass A's progress figure (``progress_score``).
BRIEF_DEFECT, BRIEF_SCORE = "defect_brief_part_missing", "score_brief_depiction"
BRIEF_IDS = frozenset({BRIEF_DEFECT, BRIEF_SCORE})
PROGRESS_WEIGHTS = {**RUBRIC_WEIGHTS, BRIEF_SCORE: 0.20}  # depiction weighs as much as the silhouette
PROGRESS_SEVERITY = {BRIEF_DEFECT: 1.0}  # as defect_missing_parts
RANK_CRITICAL = frozenset(DEFECT_SEVERITY) | BRIEF_IDS  # weigh 2 in a best-of-N utility
# A repair reason that quotes the brief for the author (the judge returns probabilities, not text: the author needs
# the request to know what to add). ``metered_author.repair_task`` lifts it onto its own line.
BRIEF_REASON = "the asset was requested as: "


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
    defects = {k: p for k, p in result.defects.items() if k not in BRIEF_IDS}  # the brief's answers: see BRIEF_IDS
    return _composite(result.scores, defects, RUBRIC_WEIGHTS, _severity, th)


def _composite(scores: Mapping[str, float], defects: Mapping[str, float], weights: Mapping[str, float],
               severity: Any, th: Thresholds) -> float:
    present = {k: w for k, w in weights.items() if k in scores}
    base = sum(scores[k] * w for k, w in present.items()) / sum(present.values()) if present else 0.0
    penalty = sum(severity(k) * min(1.0, max(0.0, (p - th.ambiguity_lo) / (1 - th.ambiguity_lo))) for k, p in defects.items())
    return float(min(4.0, max(0.0, base - min(th.defect_penalty_cap, penalty))))


def progress_score(result: VisualQAResult, th: Thresholds | None = None) -> float:
    """Pass A's progress figure (0-4): ``tier15_score`` with the brief's answers added, depiction weighted as
    ``PROGRESS_WEIGHTS`` and a missing named part penalised as ``PROGRESS_SEVERITY``. It only judges whether a repair
    regressed (a repair that adds the named parts and loses a little silhouette is progress); it gates nothing. Without
    the brief's answers it equals ``tier15_score`` exactly."""
    th = th or Thresholds()
    return _composite(result.scores, result.defects, PROGRESS_WEIGHTS, lambda k: PROGRESS_SEVERITY.get(k, _severity(k)), th)


def has_brief_answers(answers: Mapping[str, Any]) -> bool:
    return any(k in BRIEF_IDS for k in answers)


def brief_reasons(defects: Mapping[str, float], scores: Mapping[str, float], th: Thresholds | None = None) -> list[str]:
    """Why the asset does not meet the brief, [] when it does or when no brief was asked about. A requirement added to
    approval (pass A's convergence, decide_action, the export decision); never a substitute for another gate."""
    th = th or Thresholds()
    out = []
    if (p := defects.get(BRIEF_DEFECT)) is not None and p >= th.ambiguity_lo:
        out.append(f"a part the brief names is missing or unrecognisable ({BRIEF_DEFECT}={p:.2f})")
    if (v := scores.get(BRIEF_SCORE)) is not None and v < th.brief_depiction_min:
        out.append(f"brief depiction {v:.2f} < {th.brief_depiction_min} (reads as a blockout of what was asked)")
    return out


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
    action = _decide_action(result, th, stage)
    if action.kind not in ("approve", "auto_fix") or th.trust_uncalibrated or getattr(result.result, "calibrated", True):
        return action
    return _downgrade_uncalibrated(result, th, action)


UNCALIBRATED_NOTE = "decision source was uncalibrated (generative fallback, self-reported probabilities)"


def _downgrade_uncalibrated(result: VisualQAResult, th: Thresholds, action: Action) -> Action:
    """Uncalibrated results never auto_fix / approve.

    auto_fix  -> agent_repair on the same issue (an agent re-checks instead of a blind deterministic fixer).
    approve   -> agent_repair if the asset only just clears a gate (a defect probability at half the ambiguity floor or
                 more, silhouette / shippability / tier15 within `near_miss_margin` of its gate), else escalate_director
                 so a calibrated source or a person confirms the clean-looking result.
    """
    note = f"{UNCALIBRATED_NOTE}; {action.kind} withheld"
    if action.kind == "auto_fix":
        qid = next((k for k, p in result.defects.items() if p >= th.defect_high), "")
        return replace(action, kind="agent_repair", fixer=None, target="geometry" if qid in GEOMETRY_DEFECTS else "pbr",
                       reasons=[note, *action.reasons])
    near: list[str] = []
    near += [f"{k}={p:.2f}" for k, p in sorted(result.defects.items()) if p >= th.ambiguity_lo / 2]
    sil = result.scores.get("score_silhouette")
    if sil is not None and sil < th.silhouette_lock + th.near_miss_margin:
        near.append(f"silhouette {sil:.2f}")
    if result.stage != "blockout":
        shp = result.scores.get("score_shippability")
        if shp is not None and shp < th.export_shippability_min + th.near_miss_margin:
            near.append(f"shippability {shp:.2f}")
        if action.tier15 < th.export_shippability_min + th.near_miss_margin:
            near.append(f"tier15 {action.tier15:.2f}")
    if near:
        tgt = "geometry" if any(n.split("=")[0] in GEOMETRY_DEFECTS or n.startswith("silhouette") for n in near) else "pbr"
        return replace(action, kind="agent_repair", target=tgt, reasons=[note, "near a gate: " + ", ".join(near), *action.reasons])
    return replace(action, kind="escalate_director", reasons=[note, *action.reasons])


def _decide_action(result: VisualQAResult, th: Thresholds, stage: Stage | None = None) -> Action:
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
    if (dep := result.scores.get(BRIEF_SCORE)) is not None and dep < th.brief_depiction_min:
        reasons.append(f"brief depiction {dep:.2f} < {th.brief_depiction_min}")
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
    critical defects (DEFECT_SEVERITY) and the brief's two answers weigh 2, everything else 1. Sort key:
    (blocked, number of high-confidence defects, -utility rounded to 6dp, original index),
    so any candidate with a confirmed defect loses to a clean one regardless of average, and ties
    resolve to the earlier candidate deterministically.
    """
    th = th or Thresholds()

    def utility(c: CandidateScore) -> float:
        items = {**{k: v for k, v in c.defects.items()}, **c.scores}
        if not items:
            return 0.0
        w = {k: 2.0 if k in RANK_CRITICAL else 1.0 for k in items}
        return sum(w[k] * _goodness(k, v) for k, v in items.items()) / sum(w.values())

    scored = [replace(c, utility=utility(c)) for c in candidates]
    return sorted(scored, key=lambda c: (c.blocked, sum(p >= th.defect_high for p in c.defects.values()),
                                         -round(c.utility or 0.0, 6), c.index))


# ---- engine parity / LOD pop / rig ROM gates ------------------------------------------------------------

def _uncalibrated(src: Any, th: Thresholds) -> list[str]:
    return [UNCALIBRATED_NOTE] if not th.trust_uncalibrated and not getattr(src.result, "calibrated", True) else []


def _gate(defects: Mapping[str, float], scores: Mapping[str, float], score_id: str, defect_max: float, score_min: float,
          prefix: str = "") -> list[str]:
    out = [f"{prefix}{k}={p:.2f} >= {defect_max}" for k, p in sorted(defects.items()) if p >= defect_max]
    if (v := scores.get(score_id)) is None:
        out.append(f"{prefix}{score_id} missing")
    elif v < score_min:
        out.append(f"{prefix}{score_id} {v:.2f} < {score_min}")
    return out


def engine_parity_reasons(result: EngineParityResult, th: Thresholds | None = None) -> list[str]:
    th = th or Thresholds()
    return _uncalibrated(result, th) + _gate(result.defects, result.scores, "score_engine_parity",
                                             th.engine_parity_defect_max, th.engine_parity_min)


def engine_parity_ok(result: EngineParityResult, th: Thresholds | None = None) -> bool:
    """True iff no parity defect reaches engine_parity_defect_max and score_engine_parity >= engine_parity_min.

    An uncalibrated result fails unless th.trust_uncalibrated.
    """
    return not engine_parity_reasons(result, th)


def lod_pop_reasons(result: LodPopResult, th: Thresholds | None = None) -> list[str]:
    th = th or Thresholds()
    out = _uncalibrated(result, th)
    for it in result.items:
        out += _gate(it.defects, it.scores, "score_lod_continuity", th.lod_pop_defect_max, th.lod_continuity_min, f"{it.label}: ")
    return out


def lod_pop_ok(result: LodPopResult, th: Thresholds | None = None) -> bool:
    """Every LOD pair must pass: no defect at/above lod_pop_defect_max and score_lod_continuity >= lod_continuity_min."""
    return not lod_pop_reasons(result, th)


def rig_rom_reasons(result: RigRomResult, th: Thresholds | None = None) -> list[str]:
    th = th or Thresholds()
    out = _uncalibrated(result, th)
    for it in result.items:
        out += _gate(it.defects, it.scores, "score_deformation", th.rig_defect_max, th.deformation_min, f"{it.label}: ")
    return out


def rig_rom_ok(result: RigRomResult, th: Thresholds | None = None) -> bool:
    return not rig_rom_reasons(result, th)
