"""Typed builders for Decisions API questions plus the studio question banks.

Wire format (verified against the installed SDK, `CreateDecisionRequest`):
  noul   {"type","instructions","criteria"?: {"true_description","false_description"}}
  choice {"type","instructions","criteria": {option_id: description}}   (2-255 options)
  score  {"type","instructions","criteria": [level0, level1, ...]}      (list, 2-255 levels)

Rules every bank obeys: questions are scored in parallel and cannot see each other, so each
one is atomic and self-contained; image references use placeholders ({sheet}, {ortho},
{style}, {cand}, {ctx}) rendered to the real "Image N" ordinal at request time, and a
question whose image is absent is dropped (see `select`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

Question = dict[str, Any]
MAX_QUESTIONS_PER_REQUEST = 1000


def noul(instructions: str, true_description: str | None = None, false_description: str | None = None) -> Question:
    q: Question = {"type": "noul", "instructions": instructions}
    crit = {k: v for k, v in (("true_description", true_description), ("false_description", false_description)) if v}
    if crit:
        q["criteria"] = crit
    return q


def choice(instructions: str, options: Mapping[str, str]) -> Question:
    if not 2 <= len(options) <= 255:
        raise ValueError("choice questions need 2-255 options")
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions: str, levels: Iterable[str]) -> Question:
    """`levels[i]` describes score level i; the wire format is a list, not a dict."""
    levels = list(levels)
    if not 2 <= len(levels) <= 255 or not all(levels):
        raise ValueError("score questions need 2-255 non-empty levels")
    return {"type": "score", "instructions": instructions, "criteria": levels}


@dataclass(frozen=True)
class QSpec:
    id: str
    question: Question
    stages: tuple[str, ...] = ("blockout", "lookdev", "final")
    needs: tuple[str, ...] = ()  # optional inputs required: "ortho", "style", "ctx"


_PH = re.compile(r"\{(\w+)\}")


def render(question: Question, labels: Mapping[str, str]) -> Question:
    def sub(v: Any) -> Any:
        if isinstance(v, str):
            return _PH.sub(lambda m: labels.get(m.group(1), m.group(0)), v)
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v

    return sub(question)


def select(
    bank: Mapping[str, QSpec], *, stage: str | None = None, have: Iterable[str] = (), labels: Mapping[str, str] | None = None
) -> tuple[dict[str, Question], tuple[str, ...]]:
    """Wire-ready questions valid for this stage and available images, plus the skipped ids."""
    have = set(have)
    asked: dict[str, Question] = {}
    skipped: list[str] = []
    for qid, spec in bank.items():
        if (stage is not None and stage not in spec.stages) or not set(spec.needs) <= have:
            skipped.append(qid)
            continue
        asked[qid] = render(spec.question, labels or {})
    return asked, tuple(skipped)


def _bank(*specs: QSpec) -> dict[str, QSpec]:
    out = {s.id: s for s in specs}
    if len(out) != len(specs):
        raise ValueError("duplicate question id")
    return out


GEO = ("blockout", "lookdev", "final")
SURF = ("lookdev", "final")
_SHEET = "{sheet} is a contact sheet of renders of one 3D asset from several angles. "
_CAND = "{cand} shows one candidate image. "


def _d(qid: str, text: str, stages: tuple[str, ...] = SURF, needs: tuple[str, ...] = (), prefix: str = _SHEET,
       true: str | None = None) -> QSpec:
    return QSpec(f"defect_{qid}", noul(prefix + text, true_description=true), stages, needs)


def _s(qid: str, text: str, levels: list[str], stages: tuple[str, ...] = GEO, needs: tuple[str, ...] = (),
       prefix: str = _SHEET) -> QSpec:
    return QSpec(f"score_{qid}", score(prefix + text, levels), stages, needs)


ROUTING_QUESTIONS: dict[str, Question] = {
    "pipeline_dag": choice(
        "Given the user's request and the project manifest, which production pipeline fits best? "
        "Consider the reference image only if one is provided.",
        {
            "prop_to_engine": "A single hero or background prop that must be modelled, textured and exported to a game engine.",
            "batch_sanitize": "Many existing meshes to clean up, retopologise, rename or re-export in bulk.",
            "environment_kit": "A modular environment kit: walls, floors, trim sheets and tileable materials.",
            "character_ready": "A character or creature that needs a rig-ready, deformation-friendly mesh.",
            "lookdev_relight": "An existing asset that only needs material, lighting or presentation changes.",
        },
    ),
    "needs_ortho_blueprint": noul(
        "Would generating a four-view orthographic blueprint (front, side, back, top) before modelling "
        "materially improve the accuracy of this asset?",
        "A blueprint is needed: the shape is non-trivial or proportions matter.",
        "No blueprint is needed: the shape is primitive or already fully specified.",
    ),
    "complexity_tier": score(
        "How complex is the requested asset to model and texture?",
        ["Trivial: one primitive or simple extrusion.", "Simple: a few primitives, one material.",
         "Moderate: many parts, several materials, some organic shapes.",
         "High: intricate or articulated, many materials, hard-surface plus organic."],
    ),
}

_LV = lambda a, b, c, d, e: [a, b, c, d, e]  # noqa: E731  (five-level rubric helper)

VISUAL_QA_QUESTIONS: dict[str, QSpec] = _bank(
    # --- geometry defects (asked at every stage) ---
    _d("floating", "Does any part of the asset visibly float above the ground or hover detached from the part it should touch?", GEO),
    _d("clipping", "Do any parts of the asset visibly intersect or interpenetrate each other where they should not?", GEO),
    _d("faceted", "Do surfaces that should be smooth (curved or rounded forms) look faceted, low-poly or visibly polygonal?", GEO),
    _d("inverted_faces", "Are there patches of the asset that render black, inside-out or with obviously inverted shading?", GEO),
    _d("zfighting", "Is there a flickering, striped or noisy pattern typical of coplanar surfaces z-fighting?", GEO),
    _d("visible_holes", "Are there holes, gaps or missing surface patches in the mesh that should be solid?", GEO),
    _d("scale_implausible", "Are the asset's proportions relative to a human-scale environment implausible for what it is (for example a mug as large as a door)?", GEO),
    _d("missing_parts", "Compared with the blueprint in {ortho}, is any major part visible in the blueprint absent from the asset?", GEO, ("ortho",),
       prefix=_SHEET + "{ortho} is a four-view orthographic blueprint of the intended asset. "),
    _d("asymmetry", "The blueprint in {ortho} is left-right symmetric. Is the asset noticeably asymmetric where the blueprint is symmetric?", GEO, ("ortho",),
       prefix=_SHEET + "{ortho} is a four-view orthographic blueprint of the intended asset. "),
    # --- surface / lookdev defects (never asked on a clay blockout render) ---
    _d("cg_plastic", "Do the materials look like uniform, overly clean CG plastic, with no roughness variation, wear or micro-detail?"),
    _d("uv_seam", "Are texture seams visible, such as hard lines or pattern discontinuities along UV island borders?"),
    _d("baked_light", "Does the albedo texture contain baked-in directional lighting or shadows that do not match the render lighting?"),
    _d("ip_or_text", "Does the asset show a recognisable trademarked logo, legible text or a watermark?"),
    _d("texture_blur", "Is the texture blurry or low in texel density compared with the size of the surface it covers?"),
    _d("tiling_repetition", "Is there an obviously repeating tile pattern visible across a surface?"),
    _d("exposure_bad", "Is the render itself badly over-exposed or under-exposed so that detail is lost?"),
    _d("stretched_texture", "Is the texture visibly stretched or sheared on any surface?"),
    _d("wrong_material", "Does any surface read as the wrong material class for the object, such as fabric that looks like metal?"),
    _d("style_mismatch", "Compared with the style reference in {style}, does the asset's visual style (stylisation, colour palette, detail level) clearly differ?", SURF, ("style",),
       prefix=_SHEET + "{style} is a style reference image. "),
    # --- rubrics, 0-4 ---
    _s("silhouette", "Rate how readable and well-formed the asset's silhouette is.",
       _LV("Unrecognisable or broken silhouette.", "Weak: major shape errors.", "Acceptable but generic or lumpy.",
           "Good: clear, recognisable, minor flaws.", "Excellent: crisp, characteristic and production-ready.")),
    _s("proportion_accuracy", "Rate how accurate the asset's proportions are compared with the blueprint in {ortho}.",
       _LV("Proportions are wrong throughout.", "Several parts clearly off.", "Mostly right with noticeable errors.",
           "Close, small deviations only.", "Matches the blueprint proportions precisely."),
       needs=("ortho",), prefix=_SHEET + "{ortho} is a four-view orthographic blueprint of the intended asset. "),
    _s("pbr_realism", "Rate the physical plausibility of the asset's PBR materials (albedo, roughness, metalness response).",
       _LV("Flat, wrong or unusable materials.", "Poor: obviously synthetic.", "Acceptable but plain.",
           "Good: convincing response and variation.", "Excellent: photoreal material behaviour."), SURF),
    _s("material_separation", "Rate how clearly distinct materials (metal, wood, fabric, etc.) are separated and appropriately assigned.",
       _LV("All one undifferentiated material.", "Poorly separated, bleeding regions.", "Separated but crude.",
           "Well separated with minor errors.", "Cleanly separated and correctly assigned."), SURF),
    _s("shippability", "Rate overall whether this asset could ship in a game with no further work.",
       _LV("Unusable.", "Needs major rework.", "Needs moderate rework.", "Minor polish only.", "Ship as is."), GEO),
    # --- model's own recommendation: one input among several, never the authority ---
    QSpec("next_action", choice(
        _SHEET + "What is the single best next step for this asset?",
        {"approve_export": "The asset is good enough to export now.",
         "rework_pbr": "The materials or textures need rework.",
         "rework_geometry": "The shape or mesh needs rework.",
         "rework_lighting": "The lighting or exposure of the presentation needs rework.",
         "escalate_human": "The situation is ambiguous or risky and a human should decide."}), SURF),
)

BON_QUESTIONS: dict[str, dict[str, QSpec]] = {
    "ortho": _bank(
        QSpec("score_symmetry", score(_CAND + "Rate how left-right symmetric the depicted object is across its views, where the object should be symmetric.",
              _LV("Strongly asymmetric.", "Clearly asymmetric.", "Slight asymmetry.", "Nearly symmetric.", "Perfectly symmetric."))),
        QSpec("score_ortho_flatness", score(_CAND + "Rate how purely orthographic the views are: no perspective convergence, no foreshortening, flat lighting.",
              _LV("Strong perspective.", "Obvious perspective.", "Mild perspective.", "Nearly orthographic.", "Perfectly orthographic."))),
        QSpec("score_proportion_clarity", score(_CAND + "Rate how clearly the views make the object's proportions measurable.",
              _LV("Unreadable.", "Murky.", "Readable with effort.", "Clear.", "Crisp and unambiguous."))),
        QSpec("score_view_consistency", score(_CAND + "Rate whether the front, side, back and top views depict the same object consistently (same parts, same sizes, aligned).",
              _LV("Views show different objects.", "Major inconsistencies.", "Some inconsistencies.", "Minor mismatches.", "Fully consistent."))),
    ),
    "geometry": _bank(
        QSpec("score_silhouette_vs_blueprint", score(
            _CAND + "{ctx} is the intended orthographic blueprint. Rate how closely the candidate's silhouette matches it.",
            _LV("No match.", "Poor match.", "Partial match.", "Close match.", "Near-exact match.")), needs=("ctx",)),
        QSpec("defect_floating", noul(_CAND + "Does any part of the object visibly float or hover detached from where it should connect?")),
        QSpec("defect_clipping", noul(_CAND + "Do any parts of the object visibly intersect or interpenetrate where they should not?")),
    ),
    "pbr": _bank(
        QSpec("defect_baked_light", noul(_CAND + "Does this texture contain baked-in directional lighting, gradients or cast shadows?")),
        QSpec("defect_uv_seam", noul(_CAND + "Is a visible seam or hard discontinuity present where this texture would wrap or tile?")),
        QSpec("score_tileability", score(_CAND + "Rate how seamlessly this texture would tile when repeated.",
              _LV("Obvious seams and repetition.", "Visible seams.", "Faint seams.", "Almost seamless.", "Perfectly seamless."))),
        QSpec("score_realism", score(_CAND + "Rate how physically realistic and detailed this material texture looks.",
              _LV("Fake or flat.", "Poor.", "Acceptable.", "Good.", "Photoreal."))),
    ),
}

POLICY_QUESTIONS: dict[str, Question] = {
    "policy_trademark_logo": noul("Does {img} show a recognisable trademarked logo or brand mark?"),
    "policy_legible_text": noul("Does {img} contain legible text, a signature or a watermark?"),
    "policy_real_person": noul("Does {img} depict the likeness of a real, identifiable person?"),
    "policy_unsafe_content": noul("Does {img} contain unsafe content such as graphic violence, sexual content or hate symbols?"),
}
