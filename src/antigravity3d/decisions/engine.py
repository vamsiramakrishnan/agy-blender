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
from typing import Any, Awaitable, Callable, Mapping, Sequence

from . import questions as Q
from .models import (
    Answer, CandidateScore, DecisionError, DecisionResult, EngineParityResult, ItemQA, LodPopResult, PlaneStats,
    PolicyVerdict, PromptBlockedError, RigRomResult, RouteDecision, Stage, TransientDecisionError, VisualQAResult,
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

PARITY_RESOLUTION = MediaResolution.HIGH  # normal / roughness inversion needs surface detail
SHEET_RESOLUTION = MediaResolution.HIGH  # LOD-pair and pose sheets pack many small renders
MAX_LOCATE_QUESTIONS = 500  # defects x badges per locate_defects request
LOCATE_MIN_P = 0.5

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


def _infer_backend_kind(client: Any) -> str:
    """"decisions" | "generative_fallback" | "fake" (duck-typed so importing fakes/fallback stays optional)."""
    name = type(client).__name__
    if name == "FakeDecisionsClient":
        return "fake"
    if name == "GenerativeDecisionsBackend":
        return "generative_fallback"
    return "decisions"


class DecisionControlPlane:
    def __init__(self, client: Any, model: str = DEFAULT_MODEL, *, max_retries: int = 3, backoff_s: float = 0.5,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, backend_kind: str | None = None) -> None:
        self._client, self.model = client, model
        self.backend_kind = backend_kind or _infer_backend_kind(client)
        self.max_retries, self.backoff_s, self._sleep = max_retries, backoff_s, sleep
        self.stats = PlaneStats()

    @property
    def calibrated(self) -> bool:
        """False when answers come from the generative fallback (self-reported, uncalibrated)."""
        return self.backend_kind != "generative_fallback"

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
        out_tokens = int(_get(usage, "total_output_tokens") or 0)
        # Provenance: the generative fallback marks its responses `calibrated=False` and brings its own
        # (possibly unknown) cost; real Decisions responses carry neither and keep the original path.
        calibrated = bool(_get(resp, "calibrated", True))
        if calibrated:
            cost, cost_known, source = tokens * PRICE_PER_M_INPUT_USD / 1e6, True, self.backend_kind
        else:
            c = _get(resp, "cost_usd")
            cost, cost_known, source = (float(c) if c is not None else 0.0), c is not None, self.backend_kind
        self.stats.record(tag, latency_ms, tokens, cost, attempts, output_tokens=out_tokens, calibrated=calibrated,
                          cost_known=cost_known)
        return DecisionResult(tag, answers, latency_ms, tokens, est, cost, len(questions), attempts,
                              calibrated=calibrated, output_tokens=out_tokens, cost_known=cost_known, source=source)

    async def route_pipeline(self, prompt: str, manifest_txt: str, ref_img_uri: str | None = None) -> RouteDecision:
        blocks = [text_block(f"User request:\n{prompt}\n\nProject manifest:\n{manifest_txt}")]
        if ref_img_uri:
            blocks.append(image_block(ref_img_uri, MediaResolution.LOW))  # coarse routing only
        res = await self.evaluate(blocks, dict(Q.ROUTING_QUESTIONS), tag="route")
        dag, ortho, cx = res.answers["pipeline_dag"], res.answers["needs_ortho_blueprint"], res.answers["complexity_tier"]
        return RouteDecision(dag.choice or "", dag.probabilities, ortho.noul >= 0.5, ortho.noul,  # type: ignore[operator]
                             cx.score, int(round(cx.score)), dag.confidence, res)  # type: ignore[arg-type]

    async def evaluate_visual_qa(self, contact_sheet_uri: str, ortho_uri: str | None = None,
                                 style_anchor_uri: str | None = None, stage: Stage = "lookdev", *,
                                 marked_legend: Mapping[int, str] | None = None) -> VisualQAResult:
        """`marked_legend` (badge number -> part name) switches on set-of-mark mode: the sheet carries numbered badges."""
        res_tier = STAGE_RESOLUTION[stage]
        intro = "Image 1: contact sheet of renders of the asset under review."
        if marked_legend:
            intro += " Each part carries a numbered badge: " + "; ".join(
                f"[{n:02d}] = {_clean_name(v)}" for n, v in sorted(marked_legend.items())) + "."
        blocks = [text_block(intro), image_block(contact_sheet_uri, res_tier)]
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

    # ---- engine parity / LOD pop / rig ROM / set-of-mark ------------------------------------------------

    async def evaluate_engine_parity(self, blender_render_uri: str, engine_render_uri: str,
                                     stage: Stage = "lookdev") -> EngineParityResult:
        """Image 1 = DCC (Blender) render, Image 2 = the same asset, same view, in the target game engine."""
        res_tier = PARITY_RESOLUTION
        blocks = [text_block("Image 1: render of the asset in the DCC tool (Blender)."), image_block(blender_render_uri, res_tier),
                  text_block("Image 2: render of the same asset in the target game engine."), image_block(engine_render_uri, res_tier)]
        asked, skipped = Q.select(Q.ENGINE_PARITY_QUESTIONS, stage=stage, labels={"dcc": "Image 1", "engine": "Image 2"})
        res = await self.evaluate(blocks, asked, tag=f"engine_parity:{stage}")
        return EngineParityResult(res.by_prefix("defect_"), res.by_prefix("score_"), res, skipped, str(res_tier))

    async def _itemized(self, sheet_uri: str, intro: str, bank: dict[str, Q.QSpec], labels: Sequence[str], placeholder: str,
                        extra: Callable[[int, str], dict[str, str]], tag: str) -> tuple[list[ItemQA], DecisionResult]:
        asked: dict[str, Q.Question] = {}
        for i, name in enumerate(labels, 1):
            qs, _ = Q.select(bank, labels={"sheet": "Image 1", placeholder: str(i), **extra(i, name)})
            asked.update({f"{qid}@{i}": q for qid, q in qs.items()})
        res = await self.evaluate([text_block(intro), image_block(sheet_uri, SHEET_RESOLUTION)], asked, tag=tag)
        items = []
        for i, name in enumerate(labels, 1):
            sub = {qid.rsplit("@", 1)[0]: a for qid, a in res.answers.items() if qid.rsplit("@", 1)[1] == str(i)}
            items.append(ItemQA(i, name, {k: a.noul for k, a in sub.items() if k.startswith("defect_")},  # type: ignore[misc]
                                {k: a.score for k, a in sub.items() if k.startswith("score_")}))  # type: ignore[misc]
        return items, res

    async def evaluate_lod_pop(self, pair_sheet_uri: str, n_pairs: int) -> LodPopResult:
        """Sheet of side-by-side LOD(k) | LOD(k+1) renders at the transition distance; one question set per pair."""
        if n_pairs < 1:
            raise ValueError("n_pairs must be >= 1")
        if n_pairs * len(Q.LOD_POP_QUESTIONS) > Q.MAX_QUESTIONS_PER_REQUEST:
            raise ValueError(f"too many pairs ({n_pairs}) for one request")
        items, res = await self._itemized(
            pair_sheet_uri, f"Image 1: sheet of {n_pairs} side-by-side LOD(k) vs LOD(k+1) render pairs.", Q.LOD_POP_QUESTIONS,
            [f"pair {i}" for i in range(1, n_pairs + 1)], "pair", lambda i, n: {}, "lod_pop")
        return LodPopResult(items, res, str(SHEET_RESOLUTION))

    async def evaluate_rig_rom(self, pose_sheet_uri: str, pose_names: Sequence[str]) -> RigRomResult:
        """Range-of-motion pose sheet scoring. Question bank and scoring path only: no rigging backend exists yet."""
        names = [_clean_name(n) for n in pose_names]
        if not names:
            raise ValueError("pose_names must not be empty")
        if len(names) * len(Q.RIG_ROM_QUESTIONS) > Q.MAX_QUESTIONS_PER_REQUEST:
            raise ValueError(f"too many poses ({len(names)}) for one request")
        items, res = await self._itemized(
            pose_sheet_uri, f"Image 1: sheet of {len(names)} range-of-motion pose renders.", Q.RIG_ROM_QUESTIONS, names,
            "pose", lambda i, n: {"pose_name": n}, "rig_rom")
        return RigRomResult(items, res, str(SHEET_RESOLUTION))

    async def locate_defects(self, marked_sheet_uri: str, legend: Mapping[int, str], defects: Sequence[str], *,
                             min_p: float = LOCATE_MIN_P) -> dict[str, list[tuple[str, float]]]:
        """Set-of-mark localisation: per defect, the node ids (legend values) whose badge draws P >= min_p, best first.

        One atomic noul per (defect, badge), all in one request. Pass only defects already flagged by QA:
        defects x badges is capped at MAX_LOCATE_QUESTIONS and exceeding it raises ValueError.
        """
        defects = list(dict.fromkeys(defects))
        if not defects or not legend:
            return {d: [] for d in defects}
        if len(defects) * len(legend) > min(MAX_LOCATE_QUESTIONS, Q.MAX_QUESTIONS_PER_REQUEST):
            raise ValueError(f"{len(defects)} defects x {len(legend)} badges exceeds {MAX_LOCATE_QUESTIONS} questions; "
                             "pass only the defects already flagged")
        asked: dict[str, Q.Question] = {}
        for d in defects:
            for n, name in legend.items():
                asked[f"{d}@{int(n)}"] = Q.render(Q.part_defect_question(d, int(n), _clean_name(name)), {"sheet": "Image 1"})
        blocks = [text_block("Image 1: contact sheet of renders of the asset; each part carries a numbered badge."),
                  image_block(marked_sheet_uri, SHEET_RESOLUTION)]
        res = await self.evaluate(blocks, asked, tag="locate_defects")
        out: dict[str, list[tuple[str, float]]] = {d: [] for d in defects}
        best: dict[str, dict[str, float]] = {d: {} for d in defects}
        for qid, a in res.answers.items():
            d, n = qid.rsplit("@", 1)
            node = legend[int(n)]
            if a.noul is not None and a.noul >= min_p:
                best[d][node] = max(best[d].get(node, 0.0), a.noul)
        for d in defects:
            out[d] = sorted(best[d].items(), key=lambda kv: (-kv[1], kv[0]))
        return out


def _clean_name(name: object) -> str:
    """Part / pose names go into instruction text: drop braces (placeholder syntax) and newlines."""
    return " ".join(str(name).replace("{", "(").replace("}", ")").split())
