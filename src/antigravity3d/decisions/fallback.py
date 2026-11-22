"""Generative fallback for the Decisions API.

When `client.decisions.create` is unavailable for a project / key (404, permission denied), the
pipeline still has to run. `GenerativeDecisionsBackend` answers the same typed question dict by asking
a generative model for probabilities under a JSON response schema. It plugs in exactly where the genai
client does: `DecisionControlPlane(GenerativeDecisionsBackend(client))`, which calls
`.aio.decisions.create(model=, input=, questions=)`.

THE NUMBERS ARE SELF-REPORTED, NOT CALIBRATED. A generative model asked "P(defect)?" produces plausible
looking probabilities with no calibration guarantee. Every response is therefore tagged `calibrated=False`
(-> `DecisionResult.calibrated`), and compose.py refuses to auto-approve or auto-fix on such a result unless
`Thresholds.trust_uncalibrated` is set. Nothing here ever invents an answer: malformed or incomplete model
output is repaired only where the repair is unambiguous (renormalising probabilities, clamping to
[0.01, 0.99]) and otherwise raises `MalformedGenerativeAnswer`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from antigravity3d import service_answer

from .models import DecisionError

log = logging.getLogger(__name__)

# --- price table -------------------------------------------------------------------------------------
# PLACEHOLDERS, deliberately unset: fill in from the current price sheet (USD per 1M tokens). While a model's
# prices are None its cost is reported as *unknown* (DecisionResult.cost_known == False), never as 0 or a guess.
# Thinking tokens are billed as output tokens, so they are included in `total_output_tokens`.
PRICE_NEON_INPUT_PER_M_USD: float | None = None
PRICE_NEON_OUTPUT_PER_M_USD: float | None = None
PRICE_HYDROGEN_INPUT_PER_M_USD: float | None = None
PRICE_HYDROGEN_OUTPUT_PER_M_USD: float | None = None
PRICE_CARBON_INPUT_PER_M_USD: float | None = None
PRICE_CARBON_OUTPUT_PER_M_USD: float | None = None

PRICE_TABLE_USD_PER_M: dict[str, tuple[float | None, float | None]] = {
    "gemini-4-neon": (PRICE_NEON_INPUT_PER_M_USD, PRICE_NEON_OUTPUT_PER_M_USD),
    "gemini-4-hydrogen": (PRICE_HYDROGEN_INPUT_PER_M_USD, PRICE_HYDROGEN_OUTPUT_PER_M_USD),
    "gemini-4-carbon": (PRICE_CARBON_INPUT_PER_M_USD, PRICE_CARBON_OUTPUT_PER_M_USD),
}


def set_price(model: str, input_per_m_usd: float | None, output_per_m_usd: float | None) -> None:
    """Configure (or clear, with None) the price of one model."""
    PRICE_TABLE_USD_PER_M[model] = (input_per_m_usd, output_per_m_usd)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Real cost, or None when either price is not configured."""
    pin, pout = PRICE_TABLE_USD_PER_M.get(model, (None, None))
    if pin is None or pout is None:
        return None
    return input_tokens * pin / 1e6 + output_tokens * pout / 1e6


# --- constants ---------------------------------------------------------------------------------------
P_MIN, P_MAX = 0.01, 0.99
DEFAULT_CHUNK_SIZE = 40  # questions per generate_content call; chunks run concurrently
DEFAULT_MAX_CONCURRENCY = 8
# Deadline of one judge request (sent to the service, which answers 504 DEADLINE_EXCEEDED when it runs out). Measured
# on gemini-4-neon, thinking LOW, on 2026-11-21: route (3 questions, text) 7.6 s; geometry ranking (6 questions, one
# 512 px sheet) 16 to 18 s; texture ranking (8 questions, one albedo) 176 s once and no answer within 60 s (twice) and
# 240 s (once), with the same result on a 512 px copy of the image, so its size is not the cause. A trivial question on
# the same image took 5 s. The 60 s this used to be was never measured; it discarded answers the service was still
# working on, and each discarded attempt keeps its whole reservation.
DEFAULT_TIMEOUT_S = 600.0
_RESOLUTION_LEVEL = {"low": "MEDIA_RESOLUTION_LOW", "medium": "MEDIA_RESOLUTION_MEDIUM", "high": "MEDIA_RESOLUTION_HIGH",
                     "ultra_high": "MEDIA_RESOLUTION_ULTRA_HIGH"}
# Input tokens one image bills at each per-image resolution. Measured on gemini-4-neon on 2026-11-21: 1,120 with no
# resolution given and 280 at "low"; "medium" and "ultra_high" are the documented figures. Only used to bound a count
# (see worst_case_tokens), never to bill.
IMAGE_TOKENS_DEFAULT = 1120
IMAGE_TOKENS_AT = {"low": 280, "medium": 560, "high": 1120, "ultra_high": 2240}
_THINKING_LADDER = ("MINIMAL", "LOW", "MEDIUM", "HIGH")  # stepped up when a model rejects the requested level
# max_output_tokens of one judge request (answer + thinking share it). Measured 2026-11-20 on gemini-4-neon, thinking
# LOW, the fallback answering the pipeline's own question sets (benchmarks/results/round1/live.json, judge group):
#   blockout,  9 questions: 167-169 answer + 740-837 thinking  (907-1,006 output tokens), 3 samples
#   lookdev,  21 questions: 397-408 answer + 1,115-1,307 thinking (1,512-1,715 output tokens), 2 samples
# A chunk holds at most DEFAULT_CHUNK_SIZE (40) questions: about twice the lookdev answer, ~820 tokens. 8,192 is 4.7x the
# largest output seen. Without a cap the bound was the model's own limit (65,536 per attempt), which no token ceiling
# a person would state can admit. A cut-off answer raises GenerativeTruncated and is never re-asked at the same size.
JUDGE_MAX_OUTPUT_TOKENS = 8_192

SYSTEM_INSTRUCTION = (
    "You are a calibrated visual quality judge answering a batch of independent questions about the provided inputs. "
    "Answer EVERY question independently, exactly as if it were the only question you had been asked: never let your "
    "answer to one question influence another, and never refer to other questions. For a yes/no question give the "
    "probability that the answer is yes. For a choice question give a probability for each option. For a score question "
    "give a probability for each score level. Be honest about uncertainty: do not give 0 or 1. Output only JSON matching "
    "the response schema, keyed by question id."
)


class GenerativeDecisionError(DecisionError):
    """The generative fallback could not produce answers."""


class MalformedGenerativeAnswer(GenerativeDecisionError):
    """The model's output was unusable and could not be repaired unambiguously (never replaced by defaults)."""


class GenerativeTruncated(GenerativeDecisionError):
    """finish_reason MAX_TOKENS: the answer is incomplete, so it is never parsed and never re-asked.

    Judge requests are capped at ``JUDGE_MAX_OUTPUT_TOKENS`` (4.7x the largest measured output), so reaching it is a
    runaway generation that an identical re-ask would most likely repeat at the same cost.
    ``usage`` is what the provider billed for the truncated attempt (None when its answer reported none).
    """

    def __init__(self, message: str, *, finish_reason: str, usage: "Usage | None") -> None:
        super().__init__(message)
        self.finish_reason, self.usage = finish_reason, usage


# --- response objects (duck-typed to what DecisionControlPlane reads) ------------------------------------
@dataclass
class Usage:
    total_input_tokens: int = 0
    total_output_tokens: int = 0  # candidates + thinking tokens
    thoughts_tokens: int = 0


def _reported(resp: Any) -> Usage | None:
    """The usage one answer reports. None when it reports none: a missing count is not a count of zero."""
    um = getattr(resp, "usage_metadata", None)
    prompt = getattr(um, "prompt_token_count", None)
    if prompt is None:
        return None
    thoughts = int(getattr(um, "thoughts_token_count", 0) or 0)
    return Usage(int(prompt), int(getattr(um, "candidates_token_count", 0) or 0) + thoughts, thoughts)


def _total(usages: Sequence[Usage | None]) -> Usage | None:
    """The sum of what several dispatches used. None when any of them is not known, so that a call is never booked at
    less than it used: the engine books a known usage as reported and keeps the whole reservation for an unknown one."""
    total = Usage()
    for u in usages:
        if u is None:
            return None
        total.total_input_tokens += u.total_input_tokens
        total.total_output_tokens += u.total_output_tokens
        total.thoughts_tokens += u.thoughts_tokens
    return total


@dataclass
class GenerativeDecisionResponse:
    answers: dict[str, dict[str, Any]]
    usage: Usage | None  # None: what the call used is not known (see ``_total``)
    cost_usd: float | None
    model: str
    prompt_feedback: Any = None
    calibrated: bool = False
    n_chunks: int = 1
    thinking_level: str | None = None


# --- wire blocks -> generate_content parts -----------------------------------------------------------------
def blocks_to_parts(input_blocks: str | Sequence[Mapping[str, Any]], *, honor_resolution: bool = True) -> list[Any]:
    """Faithful conversion of Decisions wire input (text / image uri / image inline data) to `types.Part`s, order kept."""
    from google.genai import types

    blocks = [{"type": "text", "text": input_blocks}] if isinstance(input_blocks, str) else list(input_blocks)
    parts: list[Any] = []
    for b in blocks:
        kind = b.get("type")
        if kind == "text":
            parts.append(types.Part(text=b["text"]))
            continue
        if kind != "image":
            raise ValueError(f"unsupported input block {b!r}")
        mime = b.get("mime_type") or "image/png"
        if b.get("data"):
            part = types.Part(inline_data=types.Blob(data=base64.b64decode(b["data"]), mime_type=mime))
        elif uri := b.get("uri"):
            local = Path(uri[7:]) if uri.startswith("file://") else (Path(uri) if "://" not in uri and Path(uri).is_file() else None)
            if local is not None:
                part = types.Part(inline_data=types.Blob(data=local.read_bytes(), mime_type=mime))
            else:
                part = types.Part(file_data=types.FileData(file_uri=uri, mime_type=mime))
        else:
            raise ValueError("image block needs uri or data")
        if honor_resolution and (lvl := _RESOLUTION_LEVEL.get(str(b.get("resolution") or ""))):
            part.media_resolution = types.PartMediaResolution(level=getattr(types.PartMediaResolutionLevel, lvl))
        parts.append(part)
    return parts


# --- schema + prompt ----------------------------------------------------------------------------------------
def _levels(q: Mapping[str, Any]) -> list[str]:
    return [str(i) for i in range(len(q["criteria"]))]


def build_response_schema(questions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """JSON schema keyed by question id: noul -> P(true); choice -> P per option; score -> P per level."""
    props: dict[str, Any] = {}
    unit = {"type": "number", "minimum": 0, "maximum": 1}
    for qid, q in questions.items():
        t = q["type"]
        if t == "noul":
            props[qid] = {"type": "number", "minimum": P_MIN, "maximum": P_MAX, "description": "probability the answer is yes"}
        elif t == "choice":
            opts = list(q["criteria"])
            props[qid] = {"type": "object", "properties": {o: unit for o in opts}, "required": opts,
                          "description": "probability of each option; should sum to 1"}
        elif t == "score":
            lv = _levels(q)
            props[qid] = {"type": "object", "properties": {k: unit for k in lv}, "required": lv,
                          "description": "probability of each score level; should sum to 1"}
        else:
            raise ValueError(f"{qid}: unknown question type {t!r}")
    return {"type": "object", "properties": props, "required": list(questions)}


def render_questions(questions: Mapping[str, Mapping[str, Any]]) -> str:
    lines = ["Answer each question below independently. Respond with JSON keyed by question id.", ""]
    for qid, q in questions.items():
        lines.append(f"### {qid}  [{q['type']}]")
        lines.append(str(q["instructions"]))
        crit = q.get("criteria")
        if q["type"] == "noul":
            if crit:
                lines += [f"- true means: {crit['true_description']}" if crit.get("true_description") else "",
                          f"- false means: {crit['false_description']}" if crit.get("false_description") else ""]
            lines.append("Answer: one number, the probability of yes.")
        elif q["type"] == "choice":
            lines += [f"- option {o!r}: {d}" for o, d in crit.items()]
            lines.append("Answer: an object with a probability for every option.")
        else:
            lines += [f"- level {i}: {d}" for i, d in enumerate(crit)]
            lines.append("Answer: an object with a probability for every level (keys are the level numbers).")
        lines.append("")
    return "\n".join(x for x in lines if x is not None)


# --- parsing / repair ----------------------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise MalformedGenerativeAnswer(f"{what}: expected a number, got {v!r}")
    try:
        f = float(v)
    except ValueError:
        raise MalformedGenerativeAnswer(f"{what}: not a number: {v!r}") from None
    if not math.isfinite(f):
        raise MalformedGenerativeAnswer(f"{what}: non-finite {v!r}")
    return f


def _distribution(raw: Any, keys: list[str], what: str) -> dict[str, float]:
    """Probabilities per key, normalised to sum 1. Missing keys count as 0 only if some key is present."""
    if isinstance(raw, list) and len(raw) == len(keys):  # a bare list of per-level numbers is unambiguous
        raw = dict(zip(keys, raw))
    if not isinstance(raw, dict):
        raise MalformedGenerativeAnswer(f"{what}: expected an object of probabilities, got {type(raw).__name__}")
    got = {str(k): _num(v, f"{what}[{k}]") for k, v in raw.items() if str(k) in keys}
    if not got:
        raise MalformedGenerativeAnswer(f"{what}: none of the expected keys {keys[:6]} present in {sorted(map(str, raw))[:6]}")
    if any(v < 0 for v in got.values()):
        raise MalformedGenerativeAnswer(f"{what}: negative probability")
    total = sum(got.values())
    if total <= 0:
        raise MalformedGenerativeAnswer(f"{what}: probabilities sum to zero")
    return {k: got.get(k, 0.0) / total for k in keys}


def parse_answer(qid: str, q: Mapping[str, Any], raw: Any) -> dict[str, Any]:
    """One model value -> the answer dict `engine.normalise_answer` accepts. Raises MalformedGenerativeAnswer."""
    t = q["type"]
    if t == "noul":
        p = _num(raw, qid)
        if not 0.0 <= p <= 1.0:
            raise MalformedGenerativeAnswer(f"{qid}: probability {p} outside [0, 1]")
        p = min(P_MAX, max(P_MIN, p))
        return {"type": "noul", "noul": p, "confidence": max(p, 1 - p), "probabilities": {"true": p, "false": 1 - p}}
    if t == "choice":
        opts = list(q["criteria"])
        dist = _distribution(raw, opts, qid)
        win = max(opts, key=lambda o: (dist[o], -opts.index(o)))
        return {"type": "choice", "choice": win, "confidence": dist[win], "probabilities": dist}
    if t == "score":
        keys = _levels(q)
        dist = _distribution(raw, keys, qid)
        expected = sum(int(k) * p for k, p in dist.items())  # computed here, never trusted from the model
        return {"type": "score", "score": expected, "confidence": max(dist.values()), "probabilities": dist,
                "legend": {k: q["criteria"][int(k)] for k in keys}}
    raise MalformedGenerativeAnswer(f"{qid}: unknown question type {t!r}")


def parse_response_text(text: str | None, questions: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if not text or not text.strip():
        raise MalformedGenerativeAnswer("empty model response")
    try:
        data = json.loads(_FENCE.sub("", text))
    except json.JSONDecodeError as e:
        raise MalformedGenerativeAnswer(f"model response is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise MalformedGenerativeAnswer(f"model response is {type(data).__name__}, expected an object keyed by question id")
    if missing := [qid for qid in questions if qid not in data]:
        raise MalformedGenerativeAnswer(f"model response missing answers for {missing[:5]}{'...' if len(missing) > 5 else ''}")
    return {qid: parse_answer(qid, q, data[qid]) for qid, q in questions.items()}


# --- the backend ---------------------------------------------------------------------------------------------
class _Blocked(Exception):
    """A dispatch the service answered with a block instead of an answer. ``usage`` is what that answer and the chunk's
    earlier attempts reported (a block is an answer: its prompt, and whatever was generated before a stop, are used
    tokens); None when it reported none."""

    def __init__(self, reason: str, detail: str | None, usage: Usage | None = None) -> None:
        super().__init__(reason)
        self.reason, self.detail, self.usage = reason, detail, usage


@dataclass
class _Dispatches:
    """What happened to the generate_content dispatches of one ``create``: how many returned an answer, and the
    ``service_answer`` outcome of each that failed."""

    served: int = 0
    failed: list[tuple[str, BaseException]] = field(default_factory=list)


class GenerativeDecisionsBackend:
    """Drop-in for the genai client's `.aio.decisions.create`, answered by `generate_content`.

    client       a `genai.Client` (anything with `.aio.models.generate_content`)
    model        default: `antigravity3d.models.tier("bulk").model`
    thinking_level  default: the bulk tier's level (MINIMAL). If the model rejects it (the live API rejects MINIMAL on
                 the gemini-4 models) the level is stepped up and remembered; see `.thinking_level`.
    """

    def __init__(self, client: Any, *, model: str | None = None, thinking_level: str | None = None,
                 chunk_size: int = DEFAULT_CHUNK_SIZE, max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
                 timeout_s: float = DEFAULT_TIMEOUT_S, max_parse_retries: int = 1) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._client = client
        tier_model, tier_level = _bulk_tier()
        self.model = model or tier_model
        self.thinking_level: str | None = (thinking_level or tier_level or "MINIMAL").upper()
        self.chunk_size, self.max_concurrency, self.timeout_s = chunk_size, max_concurrency, timeout_s
        self.max_parse_retries = max_parse_retries
        self._honor_resolution = True
        self.aio = _Aio(self)

    # engine.py calls create(model=..., input=..., questions=...); `model` is the Decisions model id and is ignored.
    async def create(self, *, input: str | Sequence[Mapping[str, Any]], questions: Mapping[str, Mapping[str, Any]],
                     model: str | None = None) -> GenerativeDecisionResponse:
        if not questions:
            raise ValueError("create() needs at least one question")
        chunks = self._chunks(questions)
        sem = asyncio.Semaphore(self.max_concurrency)
        seen = _Dispatches()

        async def run(c: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], Usage | None]:
            async with sem:
                return await self._answer_chunk(input, c, seen)

        # Every chunk is awaited, so what each dispatch did is known before this call says how it ended. A cancellation
        # still cancels every chunk (gather) and propagates.
        results = await asyncio.gather(*(run(c) for c in chunks), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        for e in errors:
            if not isinstance(e, Exception):
                raise e
        if blocked := next((e for e in errors if isinstance(e, _Blocked)), None):
            fb = type("PromptFeedback", (), {"block_reason": blocked.reason, "block_reason_message": blocked.detail})()
            # A blocked call has no answers, and it still used tokens: the chunks that were answered, and the blocked one's
            # own prompt and output. The engine books the usage this response carries, so it carries all of it, or None
            # when a chunk's usage is not known (a block that reported none, a chunk that failed another way).
            used = _total([r.usage if isinstance(r, _Blocked) else None if isinstance(r, BaseException) else r[1] for r in results])
            return GenerativeDecisionResponse({}, used, None, self.model, prompt_feedback=fb, n_chunks=len(chunks))
        if errors:
            raise self._failure(errors, seen)
        answers: dict[str, dict[str, Any]] = {}
        for a, _ in results:
            answers.update(a)
        usage = _total([u for _, u in results])
        cost = cost_usd(self.model, usage.total_input_tokens, usage.total_output_tokens) if usage is not None else None
        return GenerativeDecisionResponse(answers, usage, cost, self.model, n_chunks=len(chunks), thinking_level=self.thinking_level)

    @staticmethod
    def _failure(errors: list[BaseException], seen: _Dispatches) -> BaseException:
        """The exception one failed ``create`` raises. The engine books an attempt by it: a not-served answer books
        nothing, so it is raised as such only when NO dispatch of this call was served and every failed one was turned
        away. When some were served (their usage is lost with the call) or the failures differ, the outcome of the call is
        unknown and its whole reservation is kept (``OutcomeUnknown``, which says how many were served and turned away)."""
        first = errors[0]
        outcomes = [o for o, _ in seen.failed]
        turned_away = outcomes.count("not_served")
        if not seen.failed:
            return first  # a failure after an answer (unparseable, truncated) or before any dispatch: as it always was
        if final := next((e for o, e in seen.failed if o == "final"), None):
            return final  # a request the service refused (a client error) or that ran out its deadline: as it always was
        if seen.served == 0 and len(errors) == len(seen.failed) and len(set(outcomes)) == 1:
            return first  # every dispatch failed the same way and none was served: the engine classifies it as it is
        return service_answer.OutcomeUnknown(
            f"{len(seen.failed)} of {seen.served + len(seen.failed)} judge dispatch(es) failed ({turned_away} turned away by "
            f"the service), {seen.served} served: {service_answer.describe(first)}", cause=first, served=seen.served,
            failed=len(seen.failed), turned_away=turned_away)

    def _chunks(self, questions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        items = list(questions.items())
        return [dict(items[i:i + self.chunk_size]) for i in range(0, len(items), self.chunk_size)]

    async def worst_case_tokens(self, input_blocks: str | Sequence[Mapping[str, Any]],
                                questions: Mapping[str, Mapping[str, Any]]) -> int:
        """The most tokens one ``create`` can bill: for every chunk and every attempt it may make (one plus
        ``max_parse_retries``), the counted input (contents and rendered questions, by the model's tokenizer), one token
        per byte of the response schema and of the system instruction (billed but not countable, as for authoring),
        and ``JUDGE_MAX_OUTPUT_TOKENS``. Counting is free; nothing is generated.

        The images are counted without their per-image resolutions. The API refuses a resolution on an image given by
        file reference when it counts (400 INVALID_ARGUMENT), though it takes one on an inline image and takes both
        when it generates: found on the first live preview run, where every ranking call stopped here. And a request
        whose resolutions are refused at generation is re-sent without them (``_generate``). An image counted at the
        default resolution is never billed more than that, whichever happens, except one asked for above the default,
        which is given the difference."""
        from google.genai import types

        blocks = [] if isinstance(input_blocks, str) else list(input_blocks)
        above_default = sum(max(0, IMAGE_TOKENS_AT.get(str(b.get("resolution") or ""), 0) - IMAGE_TOKENS_DEFAULT)
                            for b in blocks if b.get("type") == "image") if self._honor_resolution else 0
        total = 0
        for chunk in self._chunks(questions):
            parts = blocks_to_parts(input_blocks, honor_resolution=False)
            parts.append(types.Part(text=render_questions(chunk)))
            counted = await self._client.aio.models.count_tokens(model=self.model, contents=[types.Content(role="user", parts=parts)])
            n = getattr(counted, "total_tokens", None)
            if not isinstance(n, int) or n <= 0:
                raise GenerativeDecisionError(f"no input token count for {self.model}: cannot bound the judge call")
            unseen = len(json.dumps(build_response_schema(chunk), separators=(",", ":")).encode()) + len(SYSTEM_INSTRUCTION.encode())
            total += (n + above_default + unseen + JUDGE_MAX_OUTPUT_TOKENS) * (self.max_parse_retries + 1)
        return total

    async def _answer_chunk(self, input_blocks: Any, questions: dict[str, Any],
                            seen: _Dispatches | None = None) -> tuple[dict[str, dict[str, Any]], Usage | None]:
        used: list[Usage | None] = []  # one per attempt of this chunk
        last: Exception | None = None
        for attempt in range(self.max_parse_retries + 1):
            try:
                text, u = await self._generate(input_blocks, questions, seen)
            except _Blocked as b:
                b.usage = _total([*used, b.usage])
                raise
            used.append(u)
            try:
                return parse_response_text(text, questions), _total(used)
            except MalformedGenerativeAnswer as e:
                last = e
                log.warning("generative fallback: unusable answer (%s); %s", e, "retrying" if attempt < self.max_parse_retries else "giving up")
        raise MalformedGenerativeAnswer(f"{last} (after {self.max_parse_retries + 1} attempts)") from last

    async def _generate(self, input_blocks: Any, questions: dict[str, Any],
                        seen: _Dispatches | None = None) -> tuple[str | None, Usage | None]:
        from google.genai import errors, types

        seen = seen if seen is not None else _Dispatches()

        while True:
            parts = blocks_to_parts(input_blocks, honor_resolution=self._honor_resolution)
            parts.append(types.Part(text=render_questions(questions)))
            cfg = types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION, response_mime_type="application/json",
                response_json_schema=build_response_schema(questions),
                thinking_config=types.ThinkingConfig(thinking_level=getattr(types.ThinkingLevel, self.thinking_level)),
                max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS,
                http_options=types.HttpOptions(timeout=int(self.timeout_s * 1000)))
            sent = time.monotonic()
            try:
                try:
                    resp = await self._client.aio.models.generate_content(
                        model=self.model, contents=[types.Content(role="user", parts=parts)], config=cfg)
                except Exception as e:
                    if not (isinstance(e, errors.ClientError) and getattr(e, "code", None) == 400):  # a 400 is retried below or raised
                        seen.failed.append((service_answer.classify(e, time.monotonic() - sent, deadline_s=self.timeout_s), e))
                    raise
                seen.served += 1
            except errors.ClientError as e:
                msg = str(e).lower()
                if getattr(e, "code", None) == 400 and "thinking level" in msg and self.thinking_level in _THINKING_LADDER[:-1]:
                    nxt = _THINKING_LADDER[_THINKING_LADDER.index(self.thinking_level) + 1]
                    log.info("generative fallback: %s rejected thinking level %s; using %s", self.model, self.thinking_level, nxt)
                    self.thinking_level = nxt
                    continue
                if getattr(e, "code", None) == 400 and "resolution" in msg and self._honor_resolution:
                    log.info("generative fallback: per-image media_resolution rejected; sending images without it")
                    self._honor_resolution = False
                    continue
                raise
            break
        usage = _reported(resp)
        fb = getattr(resp, "prompt_feedback", None)
        if fb is not None and getattr(fb, "block_reason", None):
            raise _Blocked(str(getattr(fb.block_reason, "name", fb.block_reason)), getattr(fb, "block_reason_message", None), usage)
        cands = getattr(resp, "candidates", None) or []
        fr = getattr(cands[0], "finish_reason", None) if cands else None
        name = str(getattr(fr, "name", fr) or "")
        if name == "MAX_TOKENS":  # an incomplete answer is an outcome of its own, never parsed as if it were complete
            spent = (f"after {usage.total_output_tokens} output tokens ({usage.thoughts_tokens} thinking)" if usage is not None
                     else "its usage was not reported")
            raise GenerativeTruncated(f"judge answer cut off (finish_reason MAX_TOKENS) {spent}", finish_reason=name, usage=usage)
        text = getattr(resp, "text", None)
        if not text and name and name not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            raise _Blocked(name.lower(), "generation stopped without an answer", usage)
        return text, usage


class _Decisions:
    def __init__(self, owner: GenerativeDecisionsBackend) -> None:
        self._o = owner

    async def worst_case_tokens(self, *, input: Any, questions: Mapping[str, Any]) -> int:
        return await self._o.worst_case_tokens(input, questions)

    async def create(self, *, model: str | None = None, input: Any, questions: Mapping[str, Any]) -> GenerativeDecisionResponse:
        return await self._o.create(model=model, input=input, questions=questions)


class _Aio:
    def __init__(self, owner: GenerativeDecisionsBackend) -> None:
        self.decisions = _Decisions(owner)


def _bulk_tier() -> tuple[str, str | None]:
    """(model, thinking level name) of the bulk tier; falls back to literals if the tier table cannot be imported."""
    try:
        from antigravity3d.models import tier

        t = tier("bulk")
        return t.model, (t.thinking.name if t.thinking is not None else None)
    except Exception:  # noqa: BLE001 - optional dependency (google.antigravity) must not break the fallback
        return os.environ.get("AGY3D_MODEL_BULK", "gemini-4-neon"), "MINIMAL"
