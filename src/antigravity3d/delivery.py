"""What a finished run delivered, what its review found, and the exit code that follows (``agy3d prop`` and ``agy3d edit``).

Why this exists (measured 2026-11-22): a crate package that validated clean and imported into Godot 4.7.2 exited 1
because the fallback judge (uncalibrated; the native judge is not served on that key) scored its silhouette 2.74 against
a target of 3.3, and across 27 live designs no first design reached 3.3 (medians 2.05-2.79). A gate that fails every time
teaches a developer to ignore the exit code and breaks any script that reads it.

The rule:

* Calibrated judge (the native Decisions API, and the offline fakes): its gates decide, as before.
* Uncalibrated judge (a preview): exit 0 when a package was delivered, its deterministic checks passed and the judge
  reviewed it, whatever the review said about score targets or judge-only geometry findings: those are findings the
  last lines state, one sentence each. ``--strict`` makes such a finding exit 3. Exit 1 when no package was delivered, a
  deterministic check failed, the judge could not be asked (nobody looked at the asset; the repair and improvement rounds
  did not run either, and running the command again continues from the paid design), or the judge raised a hard policy
  flag or escalated to a person (content risk, not a quality score: loosening those is the owner's decision).

``gates_ok`` and ``preview_gates_ok`` are computed as before and never read as approval here; the report gains
``delivered``, ``review`` and ``exit``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from antigravity3d.state import EXPORT_MIN_TIER15_SCORE

EXIT_DELIVERED, EXIT_FAILED, EXIT_USAGE, EXIT_REVIEW_STRICT = 0, 1, 2, 3
RULE_GATES = "gates of a calibrated judge"
RULE_DELIVERY = "deterministic delivery (an uncalibrated judge's review is a finding, not a failure)"
# Pyramid steps whose outcome the judge decides. A visual_qa_<pass> step fails only when the judge did not answer (the
# review is then "unjudged"); lod_pop_vision (the judge's LOD-continuity score) and engine_parity (its DCC-against-engine
# score) fail on what the judge said: a finding. Every other failed step is a deterministic check (validation, export,
# the engine import and capture, the geometric LOD pop, textures, compliance).
_UNANSWERED_STEPS = ("visual_qa_",)
_JUDGED_STEPS = ("lod_pop_vision", "engine_parity")


def _is_judge_step(name: str) -> bool:
    return name.startswith(_UNANSWERED_STEPS) or name in _JUDGED_STEPS


def deterministic_failures(res: Any) -> list[str]:
    """The pyramid's failed steps that are checks, not the judge's verdicts."""
    return [f"{n}: {d}"[:200] for n, d in (res.failed if res is not None else []) if not _is_judge_step(n)]


def delivered(*, dag_ok: bool, res: Any, outputs: Mapping[str, Any], need_inspector: bool = True) -> tuple[bool, list[str]]:
    """Whether a package was delivered and every deterministic check on it passed, and if not, why. ``need_inspector``:
    a prop run writes one (its preview gates required it); an edit carries the shipped package's, if any."""
    why = []
    if not dag_ok:
        why.append("a step of the run failed")
    if res is None:
        why.append("the design was never refined or packaged")
    else:
        if not res.tier1_ok:
            why.append("Tier-1 checks failed")
        why += deterministic_failures(res)
    if not any(k in outputs for k in ("glb", "usd", "usdz")):
        why.append("no package was written")
    if need_inspector and "inspector" not in outputs:
        why.append("no inspector was written")
    return not why, why


def _targets(res: Any, th: Any) -> list[dict[str, Any]]:
    from antigravity3d.decisions.compose import BRIEF_SCORE

    sc = res.scores or {}
    rows = [("tier15", "overall score (tier15)", res.tier15, EXPORT_MIN_TIER15_SCORE),
            ("score_silhouette", "silhouette", sc.get("score_silhouette"), th.silhouette_lock),
            ("score_shippability", "shippability", sc.get("score_shippability"), th.export_shippability_min),
            (BRIEF_SCORE, "depiction of the brief", sc.get(BRIEF_SCORE), th.brief_depiction_min)]
    return [{"name": n, "label": label, "score": round(float(v), 2), "target": t, "met": float(v) >= t,
             "short_by": round(max(0.0, t - float(v)), 2)} for n, label, v, t in rows if v is not None]


def _judge_only(res: Any, th: Any) -> list[dict[str, Any]]:
    """Geometry findings only the judge reports, at the confidence the gates act on."""
    from antigravity3d.decisions.compose import BRIEF_DEFECT, GEOMETRY_DEFECTS

    out = []
    for d, p in sorted((res.defects or {}).items()):
        limit = (th.floating_max if d == "defect_floating" else th.ambiguity_lo if d == BRIEF_DEFECT
                 else th.defect_high if d in GEOMETRY_DEFECTS else None)
        if limit is not None and (p > limit if d == "defect_floating" else p >= limit):
            out.append({"name": d, "p": round(float(p), 2), "limit": limit})
    return out


def review_record(res: Any, th: Any, plane: Any, *, where: Iterable[str] = ()) -> dict[str, Any]:
    """The judge's review of what was delivered. ``status``: met | below_target | unjudged. ``hard_flags`` and
    ``escalated`` are content risk and keep the run failing; ``targets`` and ``judge_only`` are findings."""
    calibrated = bool(getattr(plane, "calibrated", True))
    out: dict[str, Any] = {"calibrated": calibrated, "judge": getattr(plane, "judge_label", "unknown"),
                           "where": [w for w in where if w]}
    unjudged = res is None or res.tier15 is None or any(n.startswith(_UNANSWERED_STEPS) for n, _ in res.failed)
    if unjudged:
        why = (getattr(res, "judge_unavailable", "") or "; ".join(f"pass {k} {v}" for k, v in (res.stopped or {}).items()
                                                                 if str(v).startswith("judge unavailable"))) if res else ""
        out.update(status="unjudged", why=why or "the judge never answered", targets=[], judge_only=[])
    else:
        targets, flags = _targets(res, th), _judge_only(res, th)
        flags += [{"name": n, "step": True, "detail": d[:200]} for n, d in res.failed if n in _JUDGED_STEPS]
        out.update(status="below_target" if any(not t["met"] for t in targets) or flags else "met", targets=targets,
                   judge_only=flags)
    out["hard_flags"] = list(getattr(plane, "hard_policy_flags", None) or []) if getattr(plane, "preview", False) else []
    out["escalated"] = bool(res is not None and any(t.action == "escalate_human" for t in res.rounds))
    return out


def exit_record(*, calibrated_rule: bool, gates_ok: bool, is_delivered: bool, review: Mapping[str, Any],
                strict: bool) -> dict[str, Any]:
    """The exit code and the rule that gave it. ``calibrated_rule``: the run's gates decide (a calibrated judge, or a
    verified run); ``gates_ok``: those gates."""
    if calibrated_rule:
        return {"code": EXIT_DELIVERED if gates_ok else EXIT_FAILED, "rule": RULE_GATES}
    if not is_delivered or review.get("status") == "unjudged" or review.get("hard_flags") or review.get("escalated"):
        code = EXIT_FAILED
    elif review.get("status") == "below_target" and strict:
        code = EXIT_REVIEW_STRICT
    else:
        code = EXIT_DELIVERED
    return {"code": code, "rule": RULE_DELIVERY, "strict": strict}


def finding_lines(review: Mapping[str, Any], not_delivered: Iterable[str] = ()) -> list[str]:
    """The last lines: one sentence per finding, saying what, by how much, judged by what, and where to look."""
    judge = review.get("judge", "the judge")
    look = "; look at it before you ship"
    lines = [f"not delivered: {w}" for w in not_delivered]
    if review.get("status") == "unjudged":
        lines.append(f"review: the judge could not be asked ({str(review.get('why'))[:240]}), so nobody has looked at this "
                     "asset and its repair and improvement rounds did not run; running the same command again continues from "
                     "this design")
    for t in review.get("targets") or []:
        if not t["met"]:
            lines.append(f"finding: {t['label']} {t['score']:.2f}, below the target {t['target']:g} by {t['short_by']:.2f}, judged "
                         f"by {judge}{look}")
    for f in review.get("judge_only") or []:
        if f.get("step"):  # a check the judge scores (LOD continuity, engine parity)
            lines.append(f"finding: {f['name']} failed on the answer of {judge} ({f['detail']}){look}")
        else:
            lines.append(f"finding: {f['name']} at P={f['p']:.2f} (acted on from {f['limit']:g}), reported only by {judge}{look}")
    for flag in review.get("hard_flags") or []:
        lines.append(f"blocked: hard policy flag {flag} raised by {judge}; a person must clear it")
    if review.get("escalated"):
        lines.append(f"blocked: {judge} escalated this design to a person")
    if review.get("where") and any(ln.startswith(("finding:", "blocked:")) for ln in lines):
        base = f" (in {review['where_relative_to']})" if review.get("where_relative_to") else ""
        lines.append("where to look: " + ", ".join(review["where"]) + base)
    return lines


def delivery_line(ex: Mapping[str, Any]) -> str:
    """The line above the final ``preview gates:`` line (kept as it was for the parsers that read it): what the exit code
    says, under the delivery rule."""
    code = ex["code"]
    return {EXIT_DELIVERED: "delivered: yes, every deterministic check passed (exit 0; the review above is a finding, not a failure)",
            EXIT_FAILED: "delivered: no, or blocked (exit 1)",
            EXIT_REVIEW_STRICT: "delivered: yes, but the review is below target and --strict makes that exit 3"}[code]
