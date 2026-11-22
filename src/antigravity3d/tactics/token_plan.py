"""Start-up arithmetic of a token-limited run (``--token-ceiling`` / ``--max-images``): checked before any model call.

Nothing here spends or calls a model. Two kinds of figure, kept apart:

* **Worst cases** (``judge_worst_tokens``, ``author_worst_tokens``, ``image_worst_tokens``) bound one call from above with
  the same rules admission uses (request bytes as a token bound where the tokenizer is not asked, the output caps), so
  a ceiling below them can never admit that call: the run is refused at start, naming the smallest ceiling that can.
* **Plan estimates** (``plan_estimate``) are what a run is expected to use, from the measured figures in
  ``tactics.common``; they size and explain, they never admit.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Any

from antigravity3d.tactics.common import GRADE_TOKENS_EST, IMAGE_TOKENS_EST, QA_TOKENS_EST
from antigravity3d.tactics.governor import PLANS, SpendPlan

# A planning figure for one authoring call when the author cannot say: the measured worker propose with the request as
# it ships (2026-11-22, round five's language, five live samples of the crate brief): 10,489 input + 2,751 answer +
# 3,188 thinking (means) = 16,428 tokens. Round four's request measured 16,072 (6,294 + 7,219 + 2,558): the rules text
# grew and the answers shrank, because mirror and repeat write a repeated part once. Derived from metered_author's
# tables so the plan, the author's expected profile and the cap cannot drift apart; it is the expected case, not a
# bound (the worst case is the output cap, ``author_worst_tokens``).
from antigravity3d.tactics.metered_author import MEASURED_CRAFT_INPUT_TOKENS, expected_output_tokens  # noqa: E402

AUTHOR_TOKENS_EST = MEASURED_CRAFT_INPUT_TOKENS["gemini-4-hydrogen/low"] + expected_output_tokens("worker", craft=True)
# One improvement round of pass A (tactics.pyramid.improvement_rounds): its authoring call and its blockout visual QA. The
# call is planned as a design call plus the four renders it carries (1,120 input tokens each at the default resolution,
# decisions.fallback.IMAGE_TOKENS_DEFAULT): its input holds the scene, its answer is a patch. PROVISIONAL until the live
# rounds measurement gives the figure.
IMPROVE_ROUND_TOKENS_EST = AUTHOR_TOKENS_EST + 4 * 1_120 + QA_TOKENS_EST


def judge_worst_tokens(plane: Any) -> int:
    """The largest single judge call a prop run makes, bounded without a network call: request bytes and the plane's
    image allowances, the schema and system instruction bytes, and the output cap, for every chunk and attempt. The
    larger of lookdev visual QA with blueprint and style images, and the largest geometry ranking request of any plan;
    both with a brief of the longest length the judge is given (``engine.MAX_BRIEF_CHARS``), since a prop run always has
    one. 0 for a fake plane; the native input cap for native Decisions."""
    from antigravity3d.decisions import engine as E, questions as Q
    from antigravity3d.decisions.fallback import JUDGE_MAX_OUTPUT_TOKENS, SYSTEM_INSTRUCTION, build_response_schema

    if plane.backend_kind == "fake":
        return 0
    if plane.backend_kind == "decisions":
        return E.MODEL_INPUT_LIMIT
    brief = E.brief_block("x" * E.MAX_BRIEF_CHARS)
    labels = {"sheet": "Image 1", "ortho": "Image 2", "style": "Image 3"}
    asked, _ = Q.select(Q.VISUAL_QA_QUESTIONS, stage="lookdev", have={"ortho", "style", "brief"}, labels=labels)
    tier = E.STAGE_RESOLUTION["lookdev"]
    blocks = [E.text_block("Image 1: contact sheet of renders of the asset under review."), E.image_block("https://x/1", tier), brief,
              E.text_block("Image 2: orthographic blueprint of the intended asset."), E.image_block("https://x/2", tier),
              E.text_block("Image 3: style reference."), E.image_block("https://x/3", tier)]
    requests = [(blocks, asked)]
    n_geometry = max(p.geometry_candidates for p in PLANS.values())
    requests += [(b, q) for _, _, b, q in E.bon_packets("geometry", [f"https://x/c{i}" for i in range(n_geometry)], ["https://x/o"],
                                                         brief="x" * E.MAX_BRIEF_CHARS)]
    # A design's shape is judged on separate views (tactics.judging), one design per request: the blockout review with the
    # blueprint and style images after its views, and one candidate of the geometry ranking with the blueprint.
    from antigravity3d.kernel.raster import JUDGE_VIEWS
    views = [(v, f"https://x/v{k}") for k, v in enumerate(JUDGE_VIEWS)]
    vl = Q.views_labels(1, JUDGE_VIEWS)
    shown = E._at_least(E.STAGE_RESOLUTION["blockout"], E.VIEW_RESOLUTION)
    n = len(JUDGE_VIEWS)
    vqa, _ = Q.select(Q.VISUAL_QA_QUESTIONS, stage="blockout", have={"ortho", "style", "brief"},
                      labels={"sheet": vl["span"], **vl, "ortho": f"Image {n + 1}", "style": f"Image {n + 2}"}, subjects=Q.SHEET_SUBJECTS)
    btier = E.STAGE_RESOLUTION["blockout"]
    requests.append(([E.text_block(f"{vl['span']}: {vl['n_views']} separate renders of the asset under review, one view each."),
                      *E.view_blocks(views, 1, shown), brief,
                      E.text_block(f"Image {n + 1}: orthographic blueprint of the intended asset."), E.image_block("https://x/o", btier),
                      E.text_block(f"Image {n + 2}: style reference."), E.image_block("https://x/s", btier)], vqa))
    requests += [(b, q) for _, _, b, q in E.bon_view_packets("geometry", [views], ["https://x/o"], brief="x" * E.MAX_BRIEF_CHARS)]
    client = getattr(plane, "_client", None)
    chunk = getattr(client, "chunk_size", 40)
    attempts = getattr(client, "max_parse_retries", 1) + 1

    def bound(blocks: list[Any], questions: dict[str, Any]) -> int:
        items, total = list(questions.items()), 0
        for i in range(0, len(items), chunk):
            part = dict(items[i:i + chunk])
            unseen = len(json.dumps(build_response_schema(part), separators=(",", ":")).encode()) + len(SYSTEM_INSTRUCTION.encode())
            total += (E.estimate_input_tokens(blocks, part) + unseen + JUDGE_MAX_OUTPUT_TOKENS) * attempts
        return total

    return max(bound(b, q) for b, q in requests)


def author_worst_tokens(author: Any, *, role: str | None = None, asset_name: str = "asset", kit: bool = False) -> int:
    """Worst case of one authoring call (the author's own token profile), 0 for an author that is not metered.
    ``kit``: a modular kit piece's request (no craft guidance)."""
    profile = getattr(author, "cost_profile", None)
    if profile is None:
        return 0
    extra = {"kit": True} if kit else {}
    pair = profile(role, asset_name=asset_name, **extra) if role else profile(asset_name=asset_name, **extra)
    return 0 if pair is None else int(math.ceil(pair[1]))


def image_worst_tokens(forge: Any, prompt: str, size: int) -> int:
    """Worst case of one generated image of ``size`` px with a text-only prompt (what admission reserves)."""
    from antigravity3d.genmedia.nano_banana import GenAIImageBackend, _size_tier

    if getattr(forge, "offline", True):
        return 0
    return len(prompt.encode("utf-8")) + 64 + GenAIImageBackend.output_limit(_size_tier(size))


@dataclass(frozen=True)
class PlanEstimate:
    plan: str
    images: int  # images planned for a one-material prop
    images_per_extra_material: int
    tokens: int  # expected tokens, one material, every pyramid round used

    def line(self) -> str:
        more = f" (+{self.images_per_extra_material} per extra material)" if self.images_per_extra_material else ""
        return f"{self.plan}: up to {self.images} image(s){more} and about {self.tokens:,} tokens"


def plan_estimate(plan: SpendPlan, *, needs_ortho: bool = True, max_rounds: int = 4, author_tokens: int = AUTHOR_TOKENS_EST,
                  single_candidate: bool = False) -> PlanEstimate:
    """What a prop run on ``plan`` is expected to use: route, blueprint best-of-N, geometry best-of-N, albedo best-of-N
    for one material, pass A rounds (QA + repair), every improvement round the plan allows, three lookdev rounds, the
    LOD-pop check and, for a hero, a paint-over. Improvement rounds generate no image."""
    if single_candidate:
        plan = replace(plan, ortho_candidates=1, geometry_candidates=1, pbr_candidates=1)
    ortho = plan.ortho_candidates if needs_ortho else 0
    images = ortho + plan.pbr_candidates + (1 if plan.paintover_loop else 0)
    tokens = (GRADE_TOKENS_EST  # route
              + (ortho * IMAGE_TOKENS_EST + GRADE_TOKENS_EST if ortho else 0)
              + plan.geometry_candidates * author_tokens + GRADE_TOKENS_EST
              + plan.pbr_candidates * IMAGE_TOKENS_EST + GRADE_TOKENS_EST
              + max_rounds * QA_TOKENS_EST + (max_rounds - 1) * author_tokens
              + plan.improve_rounds * IMPROVE_ROUND_TOKENS_EST
              + 3 * QA_TOKENS_EST + GRADE_TOKENS_EST
              + (IMAGE_TOKENS_EST if plan.paintover_loop else 0))
    return PlanEstimate(plan.label, images, plan.pbr_candidates, tokens)


def plan_lines(*, single_candidate: bool, max_rounds: int, author_tokens: int = AUTHOR_TOKENS_EST) -> list[str]:
    """One line per plan the router can choose (it picks one from the prompt's complexity)."""
    return [plan_estimate(PLANS[t], max_rounds=max_rounds, author_tokens=author_tokens, single_candidate=single_candidate).line()
            for t in sorted(PLANS)]
