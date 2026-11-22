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
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

Question = dict[str, Any]
MAX_QUESTIONS_PER_REQUEST = 1000
QUESTION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


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


def resubject(question: Question, subjects: Iterable[tuple[str, str]]) -> Question:
    """``question`` with the sentence that says what its image is replaced: the first of ``subjects`` (old, new) whose
    old text starts the instructions. Raises when none does, so a question cannot keep a sentence that would be false
    of the images it is sent with (a "contact sheet" when the request carries separate views)."""
    text = question["instructions"]
    for old, new in subjects:
        if text.startswith(old):
            return {**question, "instructions": new + text[len(old):]}
    raise ValueError(f"no known subject sentence starts {text[:80]!r}")


def select(
    bank: Mapping[str, QSpec], *, stage: str | None = None, have: Iterable[str] = (), labels: Mapping[str, str] | None = None,
    subjects: Iterable[tuple[str, str]] | None = None,
) -> tuple[dict[str, Question], tuple[str, ...]]:
    """Wire-ready questions valid for this stage and available images, plus the skipped ids. ``subjects``: replace each
    question's subject sentence before rendering (``resubject``); None leaves every question as written."""
    have = set(have)
    subjects = None if subjects is None else tuple(subjects)
    asked: dict[str, Question] = {}
    skipped: list[str] = []
    for qid, spec in bank.items():
        if (stage is not None and stage not in spec.stages) or not set(spec.needs) <= have:
            skipped.append(qid)
            continue
        q = spec.question if subjects is None else resubject(spec.question, subjects)
        asked[qid] = render(q, labels or {})
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
_CAND_SHEET = _CAND + "It is a contact sheet of renders of one 3D asset from several angles. "

# ---------------------------------------------------------------------------------------------------
# A design shown as separate views (tactics.judging: one image per view, decided 2026-11-22 from the live measurement in
# r5/live/judge_truth.py) instead of one contact sheet. The same questions, with the sentence that says what the images
# are replaced (``resubject``), so the sheet requests stay byte for byte what they were. Placeholders: {sheet} / {cand}
# -> "Images 1 to 5", {n_views} -> "five".
# ---------------------------------------------------------------------------------------------------
VIEW_WORDS = {
    "front": "front (orthographic)", "back": "back (orthographic)", "left": "left side (orthographic)",
    "right": "right side (orthographic)", "top": "top (orthographic)",
    "persp": "perspective from above the front left corner", "persp_rear": "perspective from above the back right corner",
}
_NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
# The subject sentence does not list the views: every image is captioned with its view in the input, and the sentence
# is repeated in every question (a list there would add ~200 bytes to each of 14 blockout questions, which the admission
# estimate counts as tokens).
VIEWS_SUBJECT = "{sheet} are {n_views} separate renders of one 3D asset from different angles, one view each. "
VIEWS_CAND = "{cand} show one candidate: {n_views} separate renders of one 3D asset from different angles, one view each. "
SHEET_SUBJECTS = ((_SHEET, VIEWS_SUBJECT),)
CAND_SUBJECTS = ((_CAND_SHEET, VIEWS_CAND), (_CAND, VIEWS_CAND))  # longest first


def view_list(names: Iterable[str]) -> str:
    return ", ".join(VIEW_WORDS.get(n, n) for n in names)


def views_labels(first: int, names: Iterable[str]) -> dict[str, str]:
    """The placeholder values for views that are Images ``first``.. of a request, in the order of ``names``."""
    names = list(names)
    if len(names) < 2:
        raise ValueError("separate views need at least two images")
    span = f"Images {first} to {first + len(names) - 1}"
    return {"span": span, "n_views": _NUMBER_WORDS.get(len(names), str(len(names))), "view_list": view_list(names),
            "view_names": ", ".join(names)}


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

# ---------------------------------------------------------------------------------------------------
# The brief. When a run has one (the user's request), the engine puts it in the request's INPUT as one quoted text
# block (``engine.brief_block``), never into a question: the question texts below are fixed and carry no user text.
# These questions need "brief" and are asked only when it is there; without a brief every request is unchanged.
# Neither needs a blueprint: until 2026-11-21 nothing asked whether the asset has the parts the brief names.
# ---------------------------------------------------------------------------------------------------
BRIEF_PART_MISSING = ("Is any part or feature that the quoted request names explicitly missing from the asset, or present only as "
                      "a shape that cannot be recognised as that part?")
BRIEF_PART_MISSING_TRUE = "At least one part the request names is absent or unrecognisable."
BRIEF_PART_MISSING_FALSE = "Every part the request names is present and recognisable, or the request names no parts."
BRIEF_DEPICTION = ("Judge shape only and ignore colour, materials and textures. Rate how completely and convincingly the asset "
                   "depicts the object the quoted request describes, as a finished game prop: every part the request names "
                   "present and recognisable, believable proportions for that object, and the secondary detail a real one has "
                   "(for example panels, trims, ribs, fasteners, handles), as opposed to a rough blockout of simple primitives.")
BRIEF_DEPICTION_LEVELS = _LV(
    "Does not depict the requested object: a different object, or unrecognisable.",
    "A crude blockout: the overall mass is roughly right, but parts the request names are missing and there is almost no "
    "secondary detail.",
    "Recognisable but plain: most named parts are present, yet the forms are simple primitives with little secondary detail; "
    "it reads as a blockout.",
    "Convincing: every named part is present and recognisable, the proportions are believable and there is some secondary "
    "detail; a few details a real one has are missing.",
    "Complete and convincing: every named part present and well formed, believable proportions and the secondary detail of a "
    "real one; it stands as a finished game prop.")


def _brief_specs(prefix: str) -> tuple[QSpec, QSpec]:
    return (QSpec("defect_brief_part_missing", noul(prefix + BRIEF_PART_MISSING, BRIEF_PART_MISSING_TRUE, BRIEF_PART_MISSING_FALSE),
                  GEO, ("brief",)),
            QSpec("score_brief_depiction", score(prefix + BRIEF_DEPICTION, BRIEF_DEPICTION_LEVELS), GEO, ("brief",)))

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
    _brief_specs(_SHEET)[0],
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
    _brief_specs(_SHEET)[1],
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

# The look round (tactics.look_round): the finished asset rendered in colour as it ships, asked about its surfaces only.
# Its stage "look" asks every surface question of the bank (those of "lookdev" and "final", never a geometry question: a
# change of colour cannot move one, and its noise would decide whether a colour change is kept) and one question no
# other stage asks: whether the colours and materials are what the request names. The two brief questions above judge
# shape only and say so, so until this question nothing asked about the request's colour and material words.
LOOK = "look"
BRIEF_LOOK = ("Ignore shape and judge colour and material only. Rate how well the asset's colours and materials match what the "
              "quoted request names or implies (a named colour or finish; painted against bare metal, wood, rubber or glass), and "
              "whether parts made of different materials read as different materials.")
BRIEF_LOOK_LEVELS = _LV(
    "Contradicts the request: the wrong colour, or everything one undifferentiated material.",
    "Mostly wrong: the colour or the materials the request names are not recognisable.",
    "Recognisable but off: a named colour too pale, too saturated or the wrong hue, or materials that should differ look alike.",
    "Matches the request, with small errors in one material.",
    "Every material reads as what the request names, or what a real one is made of, and each stands apart from its neighbours.")
VISUAL_QA_QUESTIONS = {qid: (replace(s, stages=(*s.stages, LOOK)) if s.stages == SURF else s) for qid, s in VISUAL_QA_QUESTIONS.items()}
VISUAL_QA_QUESTIONS["score_brief_look"] = QSpec("score_brief_look", score(_SHEET + BRIEF_LOOK, BRIEF_LOOK_LEVELS), (LOOK,), ("brief",))

# A pairwise comparison of two states of one asset against the request (engine.compare_states): which state is closer,
# or neither, and one sentence of the judge's own. Measured live on 2026-11-22 (r5/live/look_worker, judge_look): a look
# change a person calls plainly better (pale sage paint to olive drab) moved the look stage's separate 0-4 ratings by
# 0.02 (score_brief_look 2.06 -> 2.08, three repeats each), while the same judge shown both states side by side and asked
# which is closer preferred the change 4 of 4, in both orders. Asked only of the generative fallback (its "text" answer
# type is its own); the fixed texts carry no user text, the request is quoted in the input as everywhere else.
COMPARE_ASPECTS = {
    "look": ("Ignore shape and judge colour and material only: the colours and finishes the quoted request names or implies, "
             "and whether parts made of different materials read as different materials."),
    "shape": ("Ignore colour and material and judge shape only: the parts the quoted request names or implies, their "
              "proportions, and how they are put together."),
}
COMPARE_CLOSER = "compare_closer"
COMPARE_REASON = "compare_reason"


def compare_questions(aspect: str, first: str, second: str) -> dict[str, Question]:
    """The two questions of a pairwise comparison: ``first`` and ``second`` say which images show state A and state B
    ("Images 1 to 2"); ``aspect`` is a key of ``COMPARE_ASPECTS``."""
    head = (f"{first} show one state of a 3D asset (state A) and {second} show another state of the same asset (state B), "
            f"from the same views. {COMPARE_ASPECTS[aspect]} ")
    return {
        COMPARE_CLOSER: choice(head + "Which state is closer to the quoted request?", {
            "A": f"State A ({first}) is closer to the request.",
            "B": f"State B ({second}) is closer to the request.",
            "neither": "Neither: they are equally close to the request, or they do not differ in this."}),
        COMPARE_REASON: {"type": "text", "instructions": head + (
            "In one sentence, say which state is closer to the quoted request and the main visible difference that makes "
            "it so, or say that they do not differ in this.")},
    }

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
        *_brief_specs(_CAND_SHEET),
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

def placed_markings_fact(strings: Sequence[str]) -> str:
    """The input sentence that states, as a fact about the asset, the text this pipeline painted on it on purpose
    (kernel/smart_materials.py markings, from the package manifest's texture_atlas members): generic stencils from a
    closed vocabulary, no brand. Empty when nothing was placed."""
    if not strings:
        return ""
    return ("This pipeline painted these stencil markings on the asset on purpose; they are part of its design: "
            + ", ".join(f'"{s}"' for s in dict.fromkeys(strings)) + ".")


def ip_or_text_beyond(strings: Sequence[str]) -> Question:
    """``defect_ip_or_text`` when markings were placed: the same id, kind and thresholds, asking about any OTHER legible
    text, logo or watermark (the placed ones are listed in the input by ``placed_markings_fact``)."""
    return noul(_SHEET + "Apart from the stencil markings listed in the input as painted on purpose, does the asset show a "
                "recognisable trademarked logo, other legible text or a watermark?")


POLICY_QUESTIONS: dict[str, Question] = {
    "policy_trademark_logo": noul("Does {img} show a recognisable trademarked logo or brand mark?"),
    "policy_legible_text": noul("Does {img} contain legible text, a signature or a watermark?"),
    "policy_real_person": noul("Does {img} depict the likeness of a real, identifiable person?"),
    "policy_unsafe_content": noul("Does {img} contain unsafe content such as graphic violence, sexual content or hate symbols?"),
}

# A geometry candidate is a contact sheet of the project's own renders, and the renderer draws a small label in the
# corner of each tile (kernel.raster.contact_sheet: "front", "right", "top", "persp"). Those labels are legible text, so
# the question as it is put to a generated image is truthfully answered yes for every sheet. Seen live on 2026-11-21:
# the fallback judge failed the only candidate of a run on policy_legible_text. On a sheet the labels are named and
# set aside; the other three questions are the same.
SHEET_POLICY_QUESTIONS: dict[str, Question] = {
    **POLICY_QUESTIONS,
    "policy_legible_text": noul("Each tile of {img} has a small view label in its corner, drawn by the renderer (front, right, top, persp). "
                                "Apart from those labels, does {img} contain legible text, a signature or a watermark?"),
}
SHEET_KINDS = frozenset({"geometry"})  # best-of-N kinds whose candidate image is a labelled contact sheet
# A candidate shown as separate views: {img} -> "Images 1 to 5", {view_names} -> the labels as drawn ("front, right, top,
# persp, persp_rear"); the renderer draws its view's name in the top-left corner of each image, and (when the views are
# marked) numbered badges on the parts. Both are named and set aside, as on a sheet.
VIEWS_POLICY_QUESTIONS: dict[str, Question] = {
    "policy_trademark_logo": noul("Do any of {img} show a recognisable trademarked logo or brand mark?"),
    "policy_legible_text": noul("Each of {img} has a small label in its top-left corner naming its view ({view_names}), drawn by the "
                                "renderer{badges}. Apart from those labels and any badges, do any of {img} contain legible text, a "
                                "signature or a watermark?"),
    "policy_real_person": noul("Do any of {img} depict the likeness of a real, identifiable person?"),
    "policy_unsafe_content": noul("Do any of {img} contain unsafe content such as graphic violence, sexual content or hate symbols?"),
}
VIEWS_BADGES_NOTE = ", and numbered badges of the form [NN] that identify the parts"


def policy_questions(kind: str | None = None) -> dict[str, Question]:
    """The policy questions for a best-of-N candidate of ``kind`` (None: a generated image)."""
    return SHEET_POLICY_QUESTIONS if kind in SHEET_KINDS else POLICY_QUESTIONS


# ---------------------------------------------------------------------------------------------------
# Engine parity: DCC render vs the same asset rendered in the target game engine.
# Placeholders: {dcc} -> Image 1, {engine} -> Image 2 (both always present).
# ---------------------------------------------------------------------------------------------------
_PARITY = ("{dcc} is a render of a 3D asset from the DCC tool (Blender). {engine} is a render of the same asset, from "
           "the same viewpoint, in the target game engine. ")


def _p(qid: str, text: str, stages: tuple[str, ...] = SURF, true: str | None = None) -> QSpec:
    return QSpec(f"defect_{qid}", noul(_PARITY + text, true_description=true), stages)


ENGINE_PARITY_QUESTIONS: dict[str, QSpec] = _bank(
    _p("normal_inverted", "Comparing the two images, does the fine surface detail (bumps, grooves, rivets) in {engine} appear lit from "
       "the opposite side to the same detail in {dcc}, so that raised detail looks sunken or the reverse?"),
    _p("roughness_inverted", "Comparing the two images, are surfaces that are matte in {dcc} glossy in {engine}, or surfaces that are "
       "glossy in {dcc} matte in {engine}?"),
    _p("color_shift", "Comparing the two images, is the overall colour, brightness or contrast of the asset in {engine} noticeably "
       "different from {dcc}, by more than the slight difference two reasonable tonemappers would produce?"),
    _p("missing_material", "In {engine}, does any part of the asset render with a missing, untextured, flat default or "
       "pink/magenta placeholder material?"),
    _p("missing_parts", "Compared with {dcc}, is any part of the mesh visible in {dcc} absent from {engine}?", GEO),
    _p("wrong_orientation", "Compared with {dcc}, is the asset in {engine} rotated or flipped so that its up direction or facing "
       "direction differs (for example lying on its side or upside down)?", GEO),
    _p("wrong_scale", "Compared with {dcc}, does the asset in {engine} fill a clearly different portion of the frame, as if it were "
       "much larger or smaller?", GEO),
    QSpec("score_engine_parity", score(_PARITY + "Rate how closely the asset in {engine} matches the asset in {dcc} in shape, "
          "material response, colour and orientation.",
          _LV("Completely different or broken in the engine.", "Major differences.", "Noticeable differences.",
              "Close, only minor differences.", "Indistinguishable apart from tonemapper differences.")), GEO),
)

# ---------------------------------------------------------------------------------------------------
# LOD pop. One question set per pair, all in one request. Placeholders: {sheet} -> Image 1,
# {pair} -> 1-based pair ordinal. Ids are "<bank id>@<pair>".
# ---------------------------------------------------------------------------------------------------
_LOD = ("{sheet} is a sheet of side-by-side render pairs of one 3D asset. In each pair the left render is LOD level k and the "
        "right render is LOD level k+1, both shown at the distance where the engine switches between them. Pairs are numbered "
        "from 1 in reading order (left to right, then top to bottom). Consider only pair number {pair}. ")

LOD_POP_QUESTIONS: dict[str, QSpec] = _bank(
    QSpec("defect_silhouette_change", noul(_LOD + "Does the outline of the asset visibly change between the left and right render of this pair?"),
          GEO),
    QSpec("defect_holes", noul(_LOD + "Does the right render of this pair show holes, gaps or missing parts that are not present in the left render?"),
          GEO),
    QSpec("defect_shading_discontinuity", noul(_LOD + "Does the shading, colour or brightness of the surface visibly jump between the left and right render of this pair?"),
          SURF),
    QSpec("score_lod_continuity", score(_LOD + "Rate how imperceptible the switch between the left and right render of this pair would be to a player.",
          _LV("Glaring pop.", "Obvious pop.", "Noticeable pop.", "Barely noticeable.", "Imperceptible.")), GEO),
)

# ---------------------------------------------------------------------------------------------------
# Rig range of motion. One question set per pose. Placeholders: {sheet} -> Image 1, {pose} -> 1-based
# ordinal, {pose_name} -> name. Ids are "<bank id>@<pose ordinal>".
# NOTE: there is no rigging backend in this repo yet; this is the question bank + scoring path only.
# ---------------------------------------------------------------------------------------------------
_ROM = ("{sheet} is a sheet of renders of one rigged 3D character or object, one render per range-of-motion test pose. Poses are "
        "numbered from 1 in reading order (left to right, then top to bottom). Consider only pose number {pose}, named "
        "'{pose_name}'. ")

RIG_ROM_QUESTIONS: dict[str, QSpec] = _bank(
    QSpec("defect_joint_pinch", noul(_ROM + "Does the mesh visibly pinch, collapse or lose volume at any bent joint in this pose?"), GEO),
    QSpec("defect_candy_wrapper", noul(_ROM + "Does any limb or body segment look twisted like a candy wrapper, with the surface pinched to a thin neck where it twists?"), GEO),
    QSpec("defect_self_penetration", noul(_ROM + "Does any part of the mesh visibly pass through another part of the same mesh in this pose?"), GEO),
    QSpec("defect_joint_texture_stretch", noul(_ROM + "Is the texture visibly stretched or smeared around any joint in this pose?"), SURF),
    QSpec("score_deformation", score(_ROM + "Rate how clean and believable the mesh deformation is in this pose.",
          _LV("Broken deformation.", "Poor: severe artefacts.", "Acceptable with visible artefacts.", "Good: minor artefacts.",
              "Production-quality deformation.")), GEO),
)

# ---------------------------------------------------------------------------------------------------
# Set-of-mark: the contact sheet carries numbered badges, one per part. Each (defect, badge) pair is one
# atomic yes/no question, ids "<defect id>@<badge>". Phrases complete "Does the part labelled [NN] ...".
# ---------------------------------------------------------------------------------------------------
MARKED_SHEET_NOTE = ("{sheet} is a contact sheet of renders of one 3D asset from several angles. Each part of the asset carries a "
                     "numbered badge of the form [NN] that identifies it. ")
MARKED_DEFECT_PHRASES: dict[str, str] = {
    "defect_floating": "visibly float above the ground or hover detached from the part it should touch",
    "defect_clipping": "visibly intersect or interpenetrate another part where it should not",
    "defect_faceted": "look faceted, low-poly or visibly polygonal where it should be smooth",
    "defect_inverted_faces": "render black, inside-out or with obviously inverted shading",
    "defect_zfighting": "show a flickering, striped or noisy pattern typical of coplanar surfaces z-fighting",
    "defect_visible_holes": "have holes, gaps or missing surface patches where it should be solid",
    "defect_stretched_texture": "show a visibly stretched or sheared texture",
    "defect_uv_seam": "show visible texture seams or pattern discontinuities",
    "defect_baked_light": "show baked-in directional lighting or shadows in its texture",
    "defect_texture_blur": "have a blurry or low-resolution texture",
    "defect_wrong_material": "read as the wrong material class for what it is",
    "defect_cg_plastic": "look like uniform, overly clean CG plastic with no roughness variation or wear",
}


# The same, for separate marked views (tactics.judging author views): {sheet} -> "Images 1 to 5".
MARKED_VIEWS_NOTE = (VIEWS_SUBJECT + "Each part of the asset carries a numbered badge of the form [NN] that identifies it, with the "
                     "same number in every view. ")


def part_defect_question(defect: str, badge: int, part_name: str | None = None, note: str = MARKED_SHEET_NOTE) -> Question:
    """Atomic, self-contained noul: 'Does the part labelled [07] (named 'wheel_l') visibly float ...?'."""
    if defect not in MARKED_DEFECT_PHRASES:
        raise ValueError(f"no per-part phrasing for {defect!r}; known: {sorted(MARKED_DEFECT_PHRASES)}")
    who = f"the part labelled [{badge:02d}]" + (f" (named '{part_name}')" if part_name else "")
    return noul(f"{note}Does {who} {MARKED_DEFECT_PHRASES[defect]}?")
