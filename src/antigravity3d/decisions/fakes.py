"""FakeDecisionsClient: duck-typed stand-in for `genai.Client` that enforces the real wire format.

Requests are checked by hand (the SDK models silently accept unknown keys such as the wrong
`media_resolution`) and then by constructing the SDK's own `CreateDecisionRequest`; responses are
real SDK `CreateDecisionResponse` objects.
"""

from __future__ import annotations

import asyncio
import math
import re
from types import SimpleNamespace
from typing import Any, Callable

from google.genai._gaos.types.decisions import (
    ChoiceAnswer, CreateDecisionRequest, CreateDecisionResponse, NoulAnswer, PromptFeedback, ScoreAnswer,
)
from google.genai._gaos.types.interactions import usage as _usage

RESOLUTIONS = {"low", "medium", "high", "ultra_high"}
_IMG_REF = re.compile(r"\bImage (\d+)\b")
_IMG_SPAN = re.compile(r"\bImages (\d+) to (\d+)\b")  # a design given as separate views (engine.view_blocks)
_BLOCK_KEYS = {"text": {"type", "text"}, "image": {"type", "uri", "data", "mime_type", "resolution"}}


class FakeWireError(ValueError):
    """The request would be rejected by the real API (or silently mis-parsed by the SDK)."""


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def validate_request(model: str, input: Any, questions: dict[str, Any]) -> int:
    """Raise FakeWireError on wire-format violations; return the number of image blocks."""
    if not model:
        raise FakeWireError("model is required")
    blocks = [{"type": "text", "text": input}] if isinstance(input, str) else input
    if not isinstance(blocks, list) or not blocks:
        raise FakeWireError("input must be a non-empty str or list of content blocks")
    n_images = 0
    for b in blocks:
        t = b.get("type") if isinstance(b, dict) else None
        if t not in _BLOCK_KEYS:
            raise FakeWireError(f"unsupported input block {b!r}")
        if extra := set(b) - _BLOCK_KEYS[t]:
            raise FakeWireError(f"unknown {t} block fields {sorted(extra)} (image field is `resolution`, not `media_resolution`)")
        if t == "image":
            n_images += 1
            if not (b.get("uri") or b.get("data")):
                raise FakeWireError("image block needs uri or data")
            if "resolution" in b and b["resolution"] not in RESOLUTIONS:
                raise FakeWireError(f"bad resolution {b['resolution']!r}")
    if not 1 <= len(questions) <= 1000:
        raise FakeWireError(f"need 1-1000 questions, got {len(questions)}")
    for qid, q in questions.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", qid):
            raise FakeWireError(f"bad question id {qid!r}")
        t = q.get("type")
        if t not in ("noul", "choice", "score"):
            raise FakeWireError(f"{qid}: unknown question type {t!r}")
        if not isinstance(q.get("instructions"), (str, list)) or not q["instructions"]:
            raise FakeWireError(f"{qid}: instructions required")
        crit = q.get("criteria")
        if t == "noul":
            if set(q) - {"type", "instructions", "criteria"} or (crit is not None and (
                    not isinstance(crit, dict) or set(crit) - {"true_description", "false_description"})):
                raise FakeWireError(f"{qid}: malformed noul question")
        elif t == "choice":
            if not isinstance(crit, dict) or not 2 <= len(crit) <= 255 or not all(isinstance(k, str) and k for k in crit):
                raise FakeWireError(f"{qid}: choice criteria must be a dict of 2-255 options")
        else:
            if not isinstance(crit, list):
                raise FakeWireError(f"{qid}: score criteria must be a list (index = level), not {type(crit).__name__}")
            if not 2 <= len(crit) <= 255 or not all(isinstance(c, str) and c for c in crit):
                raise FakeWireError(f"{qid}: score criteria need 2-255 non-empty levels")
        for s in _strings(q):
            for m in _IMG_REF.finditer(s):
                if int(m.group(1)) > n_images:
                    raise FakeWireError(f"{qid}: references Image {m.group(1)} but input has {n_images} image(s)")
            for m in _IMG_SPAN.finditer(s):
                if not 1 <= int(m.group(1)) < int(m.group(2)) <= n_images:
                    raise FakeWireError(f"{qid}: references Images {m.group(1)} to {m.group(2)} but input has {n_images} image(s)")
            if "contact sheet" in s and _IMG_SPAN.search(s):
                raise FakeWireError(f"{qid}: calls separate views a contact sheet")
    CreateDecisionRequest.model_validate({"model": model, "input": input, "questions": questions})
    return n_images


def score_answer(value: float, levels: int) -> ScoreAnswer:
    lo = min(int(math.floor(value)), levels - 1)
    hi = min(lo + 1, levels - 1)
    p_hi = 0.0 if hi == lo else value - lo
    probs = {str(i): 0.0 for i in range(levels)}
    probs[str(lo)] += 1 - p_hi
    probs[str(hi)] += p_hi
    return ScoreAnswer(score=value, confidence=max(probs.values()), probabilities=probs,
                       legend={str(i): f"level {i}" for i in range(levels)})


class _Decisions:
    def __init__(self, owner: "FakeDecisionsClient") -> None:
        self._o = owner

    async def create(self, *, model: str, input: Any, questions: dict[str, Any]) -> CreateDecisionResponse:
        return await self._o._create(model, input, questions)


class _Sync:
    def __init__(self, owner: "FakeDecisionsClient") -> None:
        self._o = owner

    def create(self, *, model: str, input: Any, questions: dict[str, Any]) -> CreateDecisionResponse:
        return asyncio.run(self._o._create(model, input, questions))


class _Aio:
    def __init__(self, owner: "FakeDecisionsClient") -> None:
        self.decisions = _Decisions(owner)


class FakeDecisionsClient:
    """Programmable fake.

    answers   qid -> float (noul P(true) / score expected value) | str (choice winner) | SDK answer object
    default_noul / default_score  used for unprogrammed noul / score questions
    responder callable(request dict) -> answers dict, overriding `answers` per call
    errors    exceptions raised (one per call, FIFO) before any success
    block_reason / block_if(request)  return prompt_feedback with a block reason and no answers
    uncalibrated  responses are tagged `calibrated=False` (as the generative fallback does)

    Programmed values for per-item questions (ids "<bank id>__<n>": LOD pairs, rig poses, set-of-mark badges)
    are looked up by the full id first, then by the bare bank id.
    """

    def __init__(self, answers: dict[str, Any] | None = None, *, default_noul: float = 0.05, default_score: float = 3.6,
                 responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None, errors: list[Exception] | None = None,
                 block_reason: str | None = None, block_if: Callable[[dict[str, Any]], bool] | None = None, latency_s: float = 0.0, input_tokens: int | None = None,
                 uncalibrated: bool = False) -> None:
        self.answers, self.default_noul, self.default_score = answers or {}, default_noul, default_score
        self.responder, self.errors, self.block_reason, self.block_if = responder, list(errors or []), block_reason, block_if
        self.latency_s, self.input_tokens, self.uncalibrated = latency_s, input_tokens, uncalibrated
        self.calls: list[dict[str, Any]] = []
        self.in_flight = self.max_in_flight = 0
        self.aio = _Aio(self)
        self.decisions = _Sync(self)

    async def _create(self, model: str, input: Any, questions: dict[str, Any]) -> CreateDecisionResponse:
        n_images = validate_request(model, input, questions)
        req = {"model": model, "input": input, "questions": questions, "n_images": n_images}
        self.calls.append(req)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.latency_s)
        finally:
            self.in_flight -= 1
        if self.errors:
            raise self.errors.pop(0)
        usage = _usage.Usage(total_input_tokens=self.input_tokens) if self.input_tokens else None
        if self.block_reason or (self.block_if and self.block_if(req)):
            return CreateDecisionResponse(prompt_feedback=PromptFeedback(block_reason=self.block_reason or "image_safety"), usage=usage)
        prog = self.responder(req) if self.responder else self.answers
        out: dict[str, Any] = {}
        for qid, q in questions.items():
            v = prog.get(qid)
            if v is None and "__" in qid:
                v = prog.get(qid.rsplit("__", 1)[0])
            if v is not None and not isinstance(v, (int, float, str)):
                out[qid] = v
            elif q["type"] == "noul":
                p = min(0.99, max(0.01, float(self.default_noul if v is None else v)))
                out[qid] = NoulAnswer(noul=p, confidence=max(p, 1 - p))
            elif q["type"] == "score":
                levels = len(q["criteria"])
                value = min(self.default_score, levels - 1) if v is None else float(v)
                out[qid] = score_answer(value, levels)
            else:
                opts = list(q["criteria"])
                win = opts[0] if v is None else str(v)
                if win not in opts:
                    raise FakeWireError(f"{qid}: programmed choice {win!r} not in options")
                rest = 0.1 / (len(opts) - 1)
                out[qid] = ChoiceAnswer(choice=win, confidence=0.9, probabilities={o: 0.9 if o == win else rest for o in opts})
        resp = CreateDecisionResponse(answers=out, usage=usage)
        if self.uncalibrated:
            return SimpleNamespace(answers=resp.answers, usage=resp.usage, prompt_feedback=None, calibrated=False, cost_usd=None)
        return resp
