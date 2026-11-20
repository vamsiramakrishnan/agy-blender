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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

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
DEFAULT_TIMEOUT_S = 60.0
_RESOLUTION_LEVEL = {"low": "MEDIA_RESOLUTION_LOW", "medium": "MEDIA_RESOLUTION_MEDIUM", "high": "MEDIA_RESOLUTION_HIGH",
                     "ultra_high": "MEDIA_RESOLUTION_ULTRA_HIGH"}
_THINKING_LADDER = ("MINIMAL", "LOW", "MEDIUM", "HIGH")  # stepped up when a model rejects the requested level

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


# --- response objects (duck-typed to what DecisionControlPlane reads) ------------------------------------
@dataclass
class Usage:
    total_input_tokens: int = 0
    total_output_tokens: int = 0  # candidates + thinking tokens
    thoughts_tokens: int = 0


@dataclass
class GenerativeDecisionResponse:
    answers: dict[str, dict[str, Any]]
    usage: Usage
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
    def __init__(self, reason: str, detail: str | None) -> None:
        super().__init__(reason)
        self.reason, self.detail = reason, detail


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
        items = list(questions.items())
        chunks = [dict(items[i:i + self.chunk_size]) for i in range(0, len(items), self.chunk_size)]
        sem = asyncio.Semaphore(self.max_concurrency)

        async def run(c: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], Usage]:
            async with sem:
                return await self._answer_chunk(input, c)

        try:
            done = await asyncio.gather(*(run(c) for c in chunks))
        except _Blocked as b:
            fb = type("PromptFeedback", (), {"block_reason": b.reason, "block_reason_message": b.detail})()
            return GenerativeDecisionResponse({}, Usage(), None, self.model, prompt_feedback=fb, n_chunks=len(chunks))
        answers: dict[str, dict[str, Any]] = {}
        usage = Usage()
        for a, u in done:
            answers.update(a)
            usage.total_input_tokens += u.total_input_tokens
            usage.total_output_tokens += u.total_output_tokens
            usage.thoughts_tokens += u.thoughts_tokens
        cost = cost_usd(self.model, usage.total_input_tokens, usage.total_output_tokens)
        return GenerativeDecisionResponse(answers, usage, cost, self.model, n_chunks=len(chunks), thinking_level=self.thinking_level)

    async def _answer_chunk(self, input_blocks: Any, questions: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], Usage]:
        total = Usage()
        last: Exception | None = None
        for attempt in range(self.max_parse_retries + 1):
            text, u = await self._generate(input_blocks, questions)
            total.total_input_tokens += u.total_input_tokens
            total.total_output_tokens += u.total_output_tokens
            total.thoughts_tokens += u.thoughts_tokens
            try:
                return parse_response_text(text, questions), total
            except MalformedGenerativeAnswer as e:
                last = e
                log.warning("generative fallback: unusable answer (%s); %s", e, "retrying" if attempt < self.max_parse_retries else "giving up")
        raise MalformedGenerativeAnswer(f"{last} (after {self.max_parse_retries + 1} attempts)") from last

    async def _generate(self, input_blocks: Any, questions: dict[str, Any]) -> tuple[str | None, Usage]:
        from google.genai import errors, types

        while True:
            parts = blocks_to_parts(input_blocks, honor_resolution=self._honor_resolution)
            parts.append(types.Part(text=render_questions(questions)))
            cfg = types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION, response_mime_type="application/json",
                response_json_schema=build_response_schema(questions),
                thinking_config=types.ThinkingConfig(thinking_level=getattr(types.ThinkingLevel, self.thinking_level)),
                http_options=types.HttpOptions(timeout=int(self.timeout_s * 1000)))
            try:
                resp = await self._client.aio.models.generate_content(
                    model=self.model, contents=[types.Content(role="user", parts=parts)], config=cfg)
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
        fb = getattr(resp, "prompt_feedback", None)
        if fb is not None and getattr(fb, "block_reason", None):
            raise _Blocked(str(getattr(fb.block_reason, "name", fb.block_reason)), getattr(fb, "block_reason_message", None))
        um = getattr(resp, "usage_metadata", None)
        thoughts = int(getattr(um, "thoughts_token_count", 0) or 0)
        usage = Usage(int(getattr(um, "prompt_token_count", 0) or 0),
                      int(getattr(um, "candidates_token_count", 0) or 0) + thoughts, thoughts)
        text = getattr(resp, "text", None)
        if not text:
            cands = getattr(resp, "candidates", None) or []
            fr = getattr(cands[0], "finish_reason", None) if cands else None
            name = str(getattr(fr, "name", fr) or "")
            if name and name not in ("STOP", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"):
                raise _Blocked(name.lower(), "generation stopped without an answer")
        return text, usage


class _Decisions:
    def __init__(self, owner: GenerativeDecisionsBackend) -> None:
        self._o = owner

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
