"""DecisionControlPlane: the single choke point for every Decisions API call.

Questions in one request are scored in parallel and cannot see each other, and shared input is
billed once per request: pack all questions about one asset state into one call, keep each
question atomic, and compose conditionals in Python (see compose.py).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from enum import StrEnum
from typing import Any, Awaitable, Callable, Sequence

from . import questions as Q
from .models import (
    Answer, CandidateScore, DecisionError, DecisionResult, PlaneStats, PolicyVerdict, PromptBlockedError,
    RouteDecision, Stage, TransientDecisionError, VisualQAResult,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-decision-1"
PRICE_PER_M_INPUT_USD = 0.10  # $ per 1M input tokens; no output tokens are generated
PRICE_PER_M_OUTPUT_USD = 0.0
LATENCY_KNEE_TOKENS = 16_000  # beyond this the low-latency regime is lost: warn

# APPROXIMATE per-image token costs by resolution tier. Not published figures; calibrate against
# `usage.total_input_tokens` in the shadow runs and edit here.
IMAGE_TOKENS_APPROX = {"low": 280, "medium": 560, "high": 1120, "ultra_high": 2240}
DEFAULT_IMAGE_RESOLUTION = "medium"
CHARS_PER_TOKEN = 4


class MediaResolution(StrEnum):
    """Mirrors the SDK's `MediaResolution` literal ('low', 'medium', 'high', 'ultra_high')."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ULTRA_HIGH = "ultra_high"


# Resolution tiering: coarse checks and sweeps stay low; high tiers only for fine PBR audits.
STAGE_RESOLUTION: dict[str, MediaResolution] = {
    "blockout": MediaResolution.LOW, "lookdev": MediaResolution.HIGH, "final": MediaResolution.ULTRA_HIGH,
}
BON_RESOLUTION = MediaResolution.LOW
POLICY_RESOLUTION = MediaResolution.MEDIUM
POLICY_BLOCK_P = 0.5
POLICY_REVIEW_P = 0.25

RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}

Block = dict[str, Any]


def text_block(text: str) -> Block:
    return {"type": "text", "text": text}


def image_block(uri: str, resolution: MediaResolution | str = MediaResolution.LOW, mime_type: str = "image/png") -> Block:
    return {"type": "image", "uri": uri, "mime_type": mime_type, "resolution": str(MediaResolution(resolution))}


def image_data_block(data: bytes, resolution: MediaResolution | str = MediaResolution.LOW, mime_type: str = "image/png") -> Block:
    return {"type": "image", "data": base64.b64encode(data).decode(), "mime_type": mime_type,
            "resolution": str(MediaResolution(resolution))}


def estimate_input_tokens(input_blocks: str | Sequence[Block], questions: dict[str, Q.Question]) -> int:
    """Rough request size: text chars / 4 plus the approximate per-image cost table."""
    blocks = [text_block(input_blocks)] if isinstance(input_blocks, str) else list(input_blocks)
    chars = sum(len(b.get("text", "")) for b in blocks if b.get("type") == "text")
    chars += len(json.dumps(questions, separators=(",", ":")))
    images = sum(IMAGE_TOKENS_APPROX.get(str(b.get("resolution") or DEFAULT_IMAGE_RESOLUTION), IMAGE_TOKENS_APPROX["medium"])
                 for b in blocks if b.get("type") == "image")
    return chars // CHARS_PER_TOKEN + images


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def normalise_answer(raw: Any) -> Answer:
    """SDK answer object (or dict) -> plain Answer. Unknown answer types raise."""
    kind = _get(raw, "type")
    if kind not in ("noul", "choice", "score"):
        raise DecisionError(f"unrecognised answer type: {kind!r}")
    probs = {str(k): float(v) for k, v in (_get(raw, "probabilities") or {}).items()}
    return Answer(
        kind=kind, confidence=float(_get(raw, "confidence", 0.0) or 0.0),
        noul=float(_get(raw, "noul")) if kind == "noul" else None,
        choice=_get(raw, "choice") if kind == "choice" else None,
        score=float(_get(raw, "score")) if kind == "score" else None,
        probabilities=probs, legend={str(k): v for k, v in (_get(raw, "legend") or {}).items()},
    )


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError)):
        return True
    code = _get(exc, "code") or _get(exc, "status_code")
    return isinstance(code, int) and code in RETRYABLE_CODES


class DecisionControlPlane:
    def __init__(self, client: Any, model: str = DEFAULT_MODEL, *, max_retries: int = 3, backoff_s: float = 0.5,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self._client, self.model = client, model
        self.max_retries, self.backoff_s, self._sleep = max_retries, backoff_s, sleep
        self.stats = PlaneStats()

    async def evaluate(self, input_blocks: str | Sequence[Block], questions: dict[str, Q.Question], *, tag: str) -> DecisionResult:
        if not questions:
            raise ValueError("evaluate() needs at least one question")
        if len(questions) > Q.MAX_QUESTIONS_PER_REQUEST:
            raise ValueError(f"too many questions ({len(questions)} > {Q.MAX_QUESTIONS_PER_REQUEST})")
        est = estimate_input_tokens(input_blocks, questions)
        if est > LATENCY_KNEE_TOKENS:
            log.warning("decision request %r ~%d input tokens exceeds the ~%d low-latency knee", tag, est, LATENCY_KNEE_TOKENS)
        payload = input_blocks if isinstance(input_blocks, str) else list(input_blocks)
        attempts, t0 = 0, time.perf_counter()
        while True:
            attempts += 1
            try:
                resp = await self._client.aio.decisions.create(model=self.model, input=payload, questions=questions)
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _is_transient(exc):
                    raise
                if attempts > self.max_retries:
                    raise TransientDecisionError(f"{tag}: gave up after {attempts} attempts: {exc}") from exc
                delay = self.backoff_s * 2 ** (attempts - 1)
                log.warning("decision %r transient failure (%s); retry %d in %.2fs", tag, exc, attempts, delay)
                await self._sleep(delay)
        latency_ms = (time.perf_counter() - t0) * 1000
        fb = _get(resp, "prompt_feedback")
        if fb is not None and _get(fb, "block_reason"):
            raise PromptBlockedError(str(_get(fb, "block_reason")), _get(fb, "block_reason_message"), tag)
        answers = {qid: normalise_answer(a) for qid, a in (_get(resp, "answers") or {}).items()}
        if missing := set(questions) - set(answers):
            raise DecisionError(f"{tag}: response missing answers for {sorted(missing)}")
        usage = _get(resp, "usage")
        tokens = int(_get(usage, "total_input_tokens") or 0) or est
        cost = tokens * PRICE_PER_M_INPUT_USD / 1e6
        self.stats.record(tag, latency_ms, tokens, cost, attempts)
        return DecisionResult(tag, answers, latency_ms, tokens, est, cost, len(questions), attempts)

    async def route_pipeline(self, prompt: str, manifest_txt: str, ref_img_uri: str | None = None) -> RouteDecision:
        blocks = [text_block(f"User request:\n{prompt}\n\nProject manifest:\n{manifest_txt}")]
        if ref_img_uri:
            blocks.append(image_block(ref_img_uri, MediaResolution.LOW))  # coarse routing only
        res = await self.evaluate(blocks, dict(Q.ROUTING_QUESTIONS), tag="route")
        dag, ortho, cx = res.answers["pipeline_dag"], res.answers["needs_ortho_blueprint"], res.answers["complexity_tier"]
        return RouteDecision(dag.choice or "", dag.probabilities, ortho.noul >= 0.5, ortho.noul,  # type: ignore[operator]
                             cx.score, int(round(cx.score)), dag.confidence, res)  # type: ignore[arg-type]

    async def evaluate_visual_qa(self, contact_sheet_uri: str, ortho_uri: str | None = None,
                                 style_anchor_uri: str | None = None, stage: Stage = "lookdev") -> VisualQAResult:
        res_tier = STAGE_RESOLUTION[stage]
        blocks = [text_block("Image 1: contact sheet of renders of the asset under review."), image_block(contact_sheet_uri, res_tier)]
        labels, have = {"sheet": "Image 1"}, set()
        for key, uri, desc in (("ortho", ortho_uri, "orthographic blueprint of the intended asset"),
                               ("style", style_anchor_uri, "style reference")):
            if uri:  # a question about "Image N" must never be sent without Image N
                n = sum(b["type"] == "image" for b in blocks) + 1
                labels[key] = f"Image {n}"
                have.add(key)
                blocks += [text_block(f"Image {n}: {desc}."), image_block(uri, res_tier)]
        asked, skipped = Q.select(Q.VISUAL_QA_QUESTIONS, stage=stage, have=have, labels=labels)
        res = await self.evaluate(blocks, asked, tag=f"visual_qa:{stage}")
        na = res.answers.get("next_action")
        return VisualQAResult(
            stage=stage, defects={k: v for k, v in res.by_prefix("defect_").items()},  # type: ignore[misc]
            scores=res.by_prefix("score_"),  # type: ignore[arg-type]
            next_action=na.choice if na else None, next_action_probs=na.probabilities if na else {},
            skipped=skipped, result=res, resolution=str(res_tier))

    async def rank_candidates(self, kind: str, candidate_uris: Sequence[str], context_uris: Sequence[str] = (),
                              *, resolution: MediaResolution = BON_RESOLUTION) -> list[CandidateScore]:
        """Best-of-N scoring, returned in input order (use compose.rank to order).

        One request per candidate, fired concurrently, each asking the same atomic bank. Candidates
        are therefore scored independently against an identical rubric, so scores are comparable and
        absolute (they can be thresholded and calibrated), cost scales linearly, and position/order
        bias and cross-candidate contamination are impossible. A single "which is best?" request would
        be a relative, order-sensitive judgement that yields no calibrated per-criterion probabilities.
        """
        bank = Q.BON_QUESTIONS[kind]
        labels, have = {"cand": "Image 1"}, set()
        if context_uris:
            labels["ctx"] = "Image 2"
            have.add("ctx")
        asked, _ = Q.select(bank, have=have, labels=labels)

        async def one(i: int, uri: str) -> CandidateScore:
            blocks = [text_block("Image 1: the candidate."), image_block(uri, resolution)]
            if context_uris:
                blocks += [text_block("Image 2: reference."), image_block(context_uris[0], resolution)]
            try:
                r = await self.evaluate(blocks, asked, tag=f"bon:{kind}")
            except PromptBlockedError:
                return CandidateScore(i, uri, kind, blocked=True)
            return CandidateScore(i, uri, kind, r.by_prefix("defect_"), r.by_prefix("score_"), result=r)  # type: ignore[arg-type]

        return list(await asyncio.gather(*(one(i, u) for i, u in enumerate(candidate_uris))))

    async def policy_gate(self, image_uri: str) -> PolicyVerdict:
        qs = {k: Q.render(v, {"img": "Image 1"}) for k, v in Q.POLICY_QUESTIONS.items()}
        try:
            res = await self.evaluate([text_block("Image 1: generated image to screen."), image_block(image_uri, POLICY_RESOLUTION)],
                                      qs, tag="policy")
        except PromptBlockedError as e:
            return PolicyVerdict(False, True, {}, True, (f"input filter: {e.reason}",))
        flags = {k: a.noul for k, a in res.answers.items()}  # type: ignore[misc]
        hard = [k for k, p in flags.items() if p >= POLICY_BLOCK_P]
        soft = [k for k, p in flags.items() if POLICY_REVIEW_P <= p < POLICY_BLOCK_P]
        return PolicyVerdict(not hard, bool(hard or soft), flags, False, tuple(hard + soft))
