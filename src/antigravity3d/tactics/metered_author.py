"""One-shot, budget-admitted Gemini authoring for governed asset runs.

The request a model receives is built in exactly one place, :func:`build_author_request` (no rates, no governor), so
production (:class:`MeteredAuthor`) and the live benchmark send the same bytes.

Facts this module is built on (measured live on 2026-11-20; see ``AUTHOR_OUTPUT_BUDGETS`` for the numbers):

* Thinking tokens are counted against ``max_output_tokens`` on gemini-4 hydrogen / neon / carbon / argon:
  every ``MAX_TOKENS`` response had ``candidates_token_count + thoughts_token_count == max_output_tokens``. A cap sized
  for the answer alone therefore starves the answer, so each request is sized answer budget + thinking allowance.
* ``thinking_budget`` and ``thinking_level`` cannot be combined (HTTP 400 "You can only set only one of thinking budget
  and thinking level"), and a ``thinking_budget`` alone is not a hard limit (64 requested, 197 used). The tier's
  thinking *level* is kept; the cap gives it room.
* ``response_json_schema`` is billed as prompt input but ``count_tokens`` cannot see it (the Developer API rejects
  ``generation_config`` in countTokens). The admission bound adds ``SCHEMA_INPUT_TOKENS_PER_BYTE`` per byte of the
  compact schema JSON.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal

from google.genai import types

from antigravity3d import ir_schema
from antigravity3d.decisions.fallback import PRICE_TABLE_USD_PER_M
from antigravity3d.ir import IRPatch, SceneIR
from antigravity3d.models import ARGON, CARBON, HYDROGEN, NEON, tier
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import BudgetExceeded, Governor, usage_cost_usd

Operation = Literal["propose", "repair"]


@dataclass(frozen=True)
class OutputBudget:
    """Output tokens one request may bill: the answer plus the thinking that shares the same cap."""

    answer_tokens: int
    thinking_tokens: int

    @property
    def max_output_tokens(self) -> int:
        return self.answer_tokens + self.thinking_tokens


# ``max_output_tokens`` per authoring role = answer budget + thinking allowance for the role's thinking level. Both
# operations use the role's row: a repair may restate up to the whole scene. Measured 2026-11-20 on the propose request
# (prompt "wooden crate with iron corner brackets", schema-constrained; every sample finished STOP with a valid SceneIR):
#
#   role      model             level   answer tokens (nodes)              thinking tokens        -> answer + thinking
#   worker    gemini-4-hydrogen low     664-1,143 (9), 4 samples           804-1,119, 8 samples   ->  8,192 +  8,192
#   bulk      gemini-4-neon     low     1,799-1,919 (25-26), 3 samples     1,374-1,707, 6 samples ->  8,192 +  8,192
#   builder   gemini-4-carbon   medium  10,110-12,189 (132-146), 3 samples 7,642-13,244           -> 16,384 + 16,384
#   director  gemini-4-argon    high    6,337-11,208 (78-112), 2 samples   7,155-7,179            -> 16,384 + 16,384
#
# Headroom: the LOW tiers get >= 4.3x the largest answer and >= 4.8x the largest thinking seen (cap 16,384 of the
# model's 65,536). carbon and argon are bounded by their output_token_limit (32,768), so their cap IS the model
# maximum; the answer/thinking split there is nominal (both share one cap). carbon's worst observed sample used 25,433
# (78% of the cap): a residual truncation risk that no larger request can remove, surfaced as AuthorTruncated.
# A hero (director) call is sized by argon's own measurement and limit, never by a worker-tier figure.
# Wall time grows with the answer: carbon took 200-209 s and argon 128-233 s for a full answer (one argon call hit a
# 504 DEADLINE_EXCEEDED at the 300 s request timeout), so callers' timeout_s must allow for it.
# Repairs (hydrogen, 2 flagged nodes): 92 answer + 515 thinking tokens.
AUTHOR_OUTPUT_BUDGETS: dict[str, OutputBudget] = {
    "worker": OutputBudget(answer_tokens=8_192, thinking_tokens=8_192),
    "bulk": OutputBudget(answer_tokens=8_192, thinking_tokens=8_192),
    "builder": OutputBudget(answer_tokens=16_384, thinking_tokens=16_384),
    "director": OutputBudget(answer_tokens=16_384, thinking_tokens=16_384),
}
# Raw evidence behind the table (propose request). Older hydrogen/neon thinking samples were taken without a response
# schema (benchmarks/results/live.json); answer sizes only from schema-constrained responses.
MEASURED_THINKING_TOKENS: dict[str, tuple[int, ...]] = {
    "gemini-4-hydrogen/low": (856, 944, 1119, 804, 1072, 940, 1082, 880),
    "gemini-4-neon/low": (1374, 1471, 1707, 1401, 1482, 1704),
    "gemini-4-carbon/medium": (7642, 10649, 13244),
    "gemini-4-argon/high": (7179, 7155),
}
MEASURED_ANSWER_TOKENS: dict[str, tuple[int, ...]] = {
    "gemini-4-hydrogen/low": (664, 1143, 1132, 1135),
    "gemini-4-neon/low": (1919, 1799, 1902),
    "gemini-4-carbon/medium": (11178, 12189, 10110),
    "gemini-4-argon/high": (11208, 6337),
}
# models.get(...).output_token_limit on this key, 2026-11-20.
MODEL_OUTPUT_TOKEN_LIMIT = {HYDROGEN: 65_536, NEON: 65_536, CARBON: 32_768, ARGON: 32_768}
# Measured wall time of one propose call (min, max) in seconds, 2026-11-20, same samples as the token tables above.
# argon's 128-233 s range also had one call end in 504 DEADLINE_EXCEEDED at the 300 s request timeout.
MEASURED_PROPOSE_WALL_S: dict[str, tuple[float, float]] = {
    "gemini-4-hydrogen/low": (10.0, 38.0),
    "gemini-4-neon/low": (26.0, 38.0),
    "gemini-4-carbon/medium": (200.0, 209.0),
    "gemini-4-argon/high": (128.0, 233.0),
}
# Total request timeout per authoring role. Rule (asserted in tests/test_metered_author.py): at least
# TIMEOUT_HEADROOM x the slowest measured call of the role's model, rounded up to a multiple of 30 s. A call that times
# out is billed its whole reservation and yields nothing, so a timeout must not sit near the healthy range: argon's
# slowest healthy call (233 s) was within reach of the old 300 s for every tier. A total, not an idle, timeout: the
# thinking phase of a call is silent (no chunk arrives until thinking ends, ~100 s on argon), so a stream cannot tell
# a stalled call from a thinking one without include_thoughts, which would change the billed request.
TIMEOUT_HEADROOM = 2.0
AUTHOR_TIMEOUT_S: dict[str, float] = {"worker": 90.0, "bulk": 90.0, "builder": 420.0, "director": 480.0}
# Prompt tokens the API bills for response_json_schema, per byte of compact schema JSON. Measured: the propose schema
# (6,055 bytes) added 5,085 prompt tokens (0.84/byte), the repair schema (9,939 bytes) 7,066 (0.71/byte). 1.0 bounds both.
SCHEMA_INPUT_TOKENS_PER_BYTE = 1.0


def output_budget(role: str) -> OutputBudget:
    try:
        return AUTHOR_OUTPUT_BUDGETS[role]
    except KeyError:
        raise ValueError(f"no authoring output budget for role {role!r}") from None


def schema_input_token_bound(schema: dict[str, Any]) -> int:
    return math.ceil(len(json.dumps(schema, separators=(",", ":")).encode()) * SCHEMA_INPUT_TOKENS_PER_BYTE)


class AuthorOutcomeError(RuntimeError):
    """A dispatched authoring request produced no usable answer. Carries what the provider billed."""

    def __init__(self, message: str, *, model: str, role: str, operation: str, finish_reason: str,
                 usage: dict[str, int] | None, max_output_tokens: int) -> None:
        super().__init__(message)
        self.model, self.role, self.operation = model, role, operation
        self.finish_reason, self.usage, self.max_output_tokens = finish_reason, usage, max_output_tokens


class AuthorTruncated(AuthorOutcomeError):
    """finish_reason MAX_TOKENS: the cap (answer + thinking) was exhausted before the answer was complete."""


class AuthorNoAnswer(AuthorOutcomeError):
    """Blocked prompt, no candidate, a non-STOP finish (SAFETY, RECITATION, ...) or an empty answer."""


class AuthorTimedOut(AuthorOutcomeError):
    """The request timed out (client timeout or 504 DEADLINE_EXCEEDED). Its whole reservation is retained."""


class AuthorInvalid(AuthorOutcomeError, ValueError):
    """A complete answer that does not parse or validate. Billed usage is booked."""


@dataclass(frozen=True)
class AuthorRequest:
    role: str
    operation: str
    model: str
    contents: list[types.Content]
    config: types.GenerateContentConfig
    budget: OutputBudget
    schema: dict[str, Any]
    parse: Callable[[str], Any]

    @property
    def max_output_tokens(self) -> int:
        return self.budget.max_output_tokens


_REFERENCES = (("ortho_uri", "Orthographic blueprint"), ("style_uri", "Style reference"))


def has_references(context: dict[str, Any]) -> bool:
    return any(context.get(key) for key, _ in _REFERENCES)


def propose_task(prompt: str, *, index: int, n: int, asset_name: str, references: bool) -> str:
    refs = "Use the attached references. " if references else ""
    return (f"Build design {index + 1}/{n} for {prompt}. {refs}Return one JSON SceneIR with asset_name={asset_name!r}, "
            f"nodes and materials. Ground all parts. Include material assignments.")


def repair_task(scene: SceneIR, *, reasons: Any, located: dict[str, list[str]], manifest: str) -> str:
    return (f"Return one JSON patch {{\"ops\":[...]}} correcting only the flagged nodes. "
            f"Reasons: {reasons}; locations: {json.dumps(located)}.\n"
            f"Scene: {scene.model_dump_json(exclude_defaults=True)}\nManifest: {manifest}")


def build_author_request(role: str, operation: Operation, task: str, *, budget: EngineBudget, context: dict[str, Any],
                         timeout_s: float, asset_name: str | None = None,
                         output: OutputBudget | None = None) -> AuthorRequest:
    """Contents + generation config for one authoring call. Needs no rates and dispatches nothing.

    ``output`` overrides the table only for measurement (the benchmark's sizing probes); production never passes it.
    """
    mt = tier(role)  # type: ignore[arg-type]
    out = output or output_budget(role)
    limit = MODEL_OUTPUT_TOKEN_LIMIT.get(mt.model)
    if limit is not None and out.max_output_tokens > limit:
        raise ValueError(f"{role} {operation}: {out.max_output_tokens} output tokens exceed {mt.model}'s limit {limit}")
    if operation == "propose":
        schema, parse = ir_schema.scene_response_schema(asset_name), ir_schema.parse_scene
    elif operation == "repair":
        schema, parse = ir_schema.patch_response_schema(), ir_schema.parse_patch
    else:
        raise ValueError(f"unknown authoring operation {operation!r}")
    parts = [types.Part.from_text(text=f"{ir_schema.ir_rules_text()}\nBudget: {json.dumps(asdict(budget))}\n{task}")]
    for key, label in _REFERENCES:
        if uri := context.get(key):
            parts.extend([types.Part.from_text(text=label), types.Part.from_uri(file_uri=uri, mime_type="image/png")])
    http = types.HttpOptions(timeout=int(timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1))
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_json_schema=schema,
        max_output_tokens=out.max_output_tokens, candidate_count=1,
        thinking_config=types.ThinkingConfig(thinking_level=mt.thinking.value.upper()) if mt.thinking else None,
        http_options=http, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    return AuthorRequest(role, operation, mt.model, [types.Content(role="user", parts=parts)], config, out, schema, parse)


def _name(value: Any) -> str:
    return str(getattr(value, "name", value) or "")


def check_response(req: AuthorRequest, response: Any, usage: dict[str, int] | None) -> str:
    """The answer text of a complete response, or a typed :class:`AuthorOutcomeError`."""
    meta = dict(model=req.model, role=req.role, operation=req.operation, usage=usage, max_output_tokens=req.max_output_tokens)
    where = f"{req.role} {req.operation} on {req.model}"
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        reason = _name(feedback.block_reason)
        raise AuthorNoAnswer(f"{where}: prompt blocked ({reason}); usage {usage}", finish_reason=reason, **meta)
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise AuthorNoAnswer(f"{where}: no candidate returned; usage {usage}", finish_reason="NO_CANDIDATE", **meta)
    finish = _name(getattr(candidates[0], "finish_reason", None)) or "FINISH_REASON_UNSPECIFIED"
    if finish == "MAX_TOKENS":
        raise AuthorTruncated(f"{where}: output cut off at max_output_tokens={req.max_output_tokens} "
                              f"({req.budget.answer_tokens} answer + {req.budget.thinking_tokens} thinking); usage {usage}",
                              finish_reason=finish, **meta)
    if finish != "STOP":
        raise AuthorNoAnswer(f"{where}: finish_reason {finish}; usage {usage}", finish_reason=finish, **meta)
    text = getattr(response, "text", None) or ""
    if not text.strip():
        raise AuthorNoAnswer(f"{where}: STOP with an empty answer; usage {usage}", finish_reason=finish, **meta)
    return text


def usage_tokens(response: Any) -> dict[str, int] | None:
    """Billed tokens from usage metadata: input, output (answer + thinking) and the thinking share; None if absent."""
    u = getattr(response, "usage_metadata", None)
    prompt, total = getattr(u, "prompt_token_count", None), getattr(u, "total_token_count", None)
    if prompt is None or total is None:
        return None
    thinking = getattr(u, "thoughts_token_count", None) or 0
    output = max(total - prompt, (getattr(u, "candidates_token_count", None) or 0) + thinking)
    return {"input_tokens": prompt, "output_tokens": output, "thinking_tokens": thinking}


def is_timeout(exc: BaseException) -> bool:
    """A request timeout from asyncio, httpx / httpcore, or the API's own 504 DEADLINE_EXCEEDED."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    if any("Timeout" in cls.__name__ for cls in type(exc).__mro__):
        return True
    return getattr(exc, "code", None) == 504 or getattr(exc, "status", None) == "DEADLINE_EXCEEDED"


def author_timeout(role: str) -> float:
    try:
        return AUTHOR_TIMEOUT_S[role]
    except KeyError:
        raise ValueError(f"no authoring timeout for role {role!r}") from None


def expected_output_tokens(role: str) -> int:
    """Mean measured answer + thinking tokens of the role's model and level; the full cap when it was not measured."""
    mt = tier(role)  # type: ignore[arg-type]
    key = f"{mt.model}/{mt.thinking.value if mt.thinking else 'none'}"
    answers, thoughts = MEASURED_ANSWER_TOKENS.get(key), MEASURED_THINKING_TOKENS.get(key)
    if not answers or not thoughts:
        return output_budget(role).max_output_tokens
    return min(output_budget(role).max_output_tokens, round(sum(answers) / len(answers) + sum(thoughts) / len(thoughts)))


@dataclass(frozen=True)
class CandidateOutcome:
    """What happened to one requested candidate. ``status``: produced, not_admitted, truncated, no_answer, invalid,
    timed_out or error."""

    index: int
    status: str
    detail: str = ""
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    admission_wait_s: float = 0.0
    reserved_usd: float | None = None

    @property
    def produced(self) -> bool:
        return self.status == "produced"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposeResult:
    scenes: list[SceneIR]
    outcomes: list[CandidateOutcome]  # one per requested candidate, in index order

    @property
    def n_requested(self) -> int:
        return len(self.outcomes)

    def summary(self) -> str:
        return "; ".join(f"#{o.index} {o.status}" + (f" ({o.detail})" if o.detail else "") for o in self.outcomes)


def _rates(model: str) -> tuple[float, float]:
    rates = json.loads(os.environ.get("AGY3D_TEXT_RATES_JSON", "{}"))
    pair = rates.get(model, PRICE_TABLE_USD_PER_M.get(model))
    if (not isinstance(pair, (list, tuple)) or len(pair) != 2
            or any(not isinstance(x, (float, int)) or not math.isfinite(x) or x < 0 for x in pair)):
        raise BudgetExceeded(f"configure AGY3D_TEXT_RATES_JSON with [USD/M input, USD/M output] for {model}")
    return float(pair[0]), float(pair[1])


def _meta(req: AuthorRequest, usage: dict[str, int] | None) -> dict[str, Any]:
    return dict(model=req.model, role=req.role, operation=req.operation, usage=usage, max_output_tokens=req.max_output_tokens)


def _one_line(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class MeteredAuthor:
    """Bound to one route's budget, spend ledger and image references, including during repair.

    No automatic re-request on truncation. The cap is sized from measurements with headroom (and is the model maximum
    for carbon / argon, so there is no larger budget to retry with); a MAX_TOKENS finish therefore means a runaway
    generation that a re-request would most likely repeat, billing the cap twice.
    The typed error carries the finish reason and billed usage; the caller decides (the pyramid retries a truncated
    worker repair once on the director tier only when the plan grants it: that is a larger budget, not the same request).

    Admission waits instead of dropping: each call reserves its worst case with ``Governor.reserve_wait``, so N
    candidates run as many at a time as the ceiling admits and the rest start as earlier ones settle. Nothing is
    reduced silently: every requested candidate ends with a :class:`CandidateOutcome`.

    ``timeout_s=None`` uses the per-tier ``AUTHOR_TIMEOUT_S``; a number overrides it for every tier.
    """

    def __init__(self, client: Any, governor: Governor, budget: EngineBudget, context: dict, *,
                 timeout_s: float | None = None) -> None:
        self.client, self.governor, self.budget = client, governor, budget
        self.context, self.timeout_s = dict(context), timeout_s

    def timeout_for(self, role: str) -> float:
        return author_timeout(role) if self.timeout_s is None else self.timeout_s

    def cost_profile(self, role: str | None = None, *, asset_name: str = "asset") -> tuple[float, float] | None:
        """(expected, worst-case) USD of one propose call for sizing a best-of-N, or None when no rates are known.

        Expected = mean measured output tokens (``expected_output_tokens``); worst case = the full cap, which is what
        admission reserves. Input is estimated from the request text (3 bytes per token) plus the schema bound, because
        counting needs a network call; sizing only, admission uses the counted figure.
        """
        role = role or self.governor.plan.builder_role
        req = self.request(role, "propose", propose_task("x", index=0, n=1, asset_name=asset_name,
                                                          references=has_references(self.context)), asset_name=asset_name)
        try:
            pin, pout = _rates(req.model)
        except BudgetExceeded:
            return None
        text = "".join(p.text or "" for c in req.contents for p in c.parts)
        tokens_in = math.ceil(len(text.encode()) / 3) + schema_input_token_bound(req.schema)
        expected = usage_cost_usd(tokens_in, expected_output_tokens(role), usd_per_m_in=pin, usd_per_m_out=pout)
        worst = usage_cost_usd(tokens_in, req.max_output_tokens, usd_per_m_in=pin, usd_per_m_out=pout)
        return expected, worst

    async def _generate(self, req: AuthorRequest, trace: dict[str, Any] | None = None) -> Any:
        trace = {} if trace is None else trace
        pin, pout = _rates(req.model)
        api = self.client.aio.models
        counted = await api.count_tokens(model=req.model, contents=req.contents,
                                         config=types.CountTokensConfig(http_options=req.config.http_options))
        n = counted.total_tokens
        if not isinstance(n, int) or n <= 0:
            raise BudgetExceeded(f"no input token count for {req.model}")
        schema_bound = schema_input_token_bound(req.schema)
        input_bound = n + schema_bound

        def cost(inputs: int, outputs: int) -> float:
            return usage_cost_usd(inputs, outputs, usd_per_m_in=pin, usd_per_m_out=pout)

        limit = cost(input_bound, req.max_output_tokens)
        detail = (f"{req.role} ({req.model}) {req.operation}: up to {input_bound} input tokens ({n} counted + {schema_bound} "
                  f"schema bound) and {req.max_output_tokens} output tokens ({req.budget.answer_tokens} answer + "
                  f"{req.budget.thinking_tokens} thinking) at ${pin}/${pout} per M = ${limit:.5f}")
        receipt: dict[str, Any] = {"model": req.model, "role": req.role, "counted_input_tokens": n,
                                   "schema_input_tokens_bound": schema_bound, "max_output_tokens": req.max_output_tokens,
                                   "answer_tokens": req.budget.answer_tokens, "thinking_tokens_allowed": req.budget.thinking_tokens,
                                   "input_usd_per_m": pin, "output_usd_per_m": pout}
        t0 = time.monotonic()
        try:
            # Waits for outstanding reservations to settle; fails at once only when this can never fit the ceiling.
            hold = await self.governor.reserve_wait(f"author.{req.operation}", limit, receipt=receipt)
        except BudgetExceeded as e:
            raise BudgetExceeded(f"plan {self.governor.plan.label!r}: {e}; request: {detail}") from e
        trace.update(admission_wait_s=round(time.monotonic() - t0, 3), reserved_usd=limit)
        booked = None
        try:
            try:
                result = await api.generate_content(model=req.model, contents=req.contents, config=req.config)
            except Exception as exc:
                if is_timeout(exc):
                    receipt["finish_reason"] = "TIMEOUT"
                    raise AuthorTimedOut(
                        f"{req.role} {req.operation} on {req.model}: timed out after {req.config.http_options.timeout / 1000:.0f}s "
                        f"({type(exc).__name__}); the whole reservation ${limit:.5f} is retained", finish_reason="TIMEOUT",
                        **_meta(req, None)) from exc
                raise
            usage = usage_tokens(result)
            trace["usage"] = usage
            if usage is not None:
                booked = cost(usage["input_tokens"], usage["output_tokens"])
                receipt.update(usage)
            candidates = getattr(result, "candidates", None) or []
            receipt["finish_reason"] = _name(getattr(candidates[0], "finish_reason", None)) if candidates else None
            text = check_response(req, result, usage)
            try:
                parsed = req.parse(text)
            except ValueError as exc:
                raise AuthorInvalid(f"{req.role} {req.operation} on {req.model}: answer does not validate: {_one_line(exc)}; "
                                    f"usage {usage}", finish_reason=receipt["finish_reason"], **_meta(req, usage)) from exc
            receipt["status"] = "ok"
            return parsed
        except BaseException as exc:
            receipt["status"] = type(exc).__name__
            raise
        finally:
            receipt["usage_known"] = booked is not None
            hold.settle(booked, receipt=receipt)

    def request(self, role: str, operation: Operation, task: str, *, asset_name: str | None = None) -> AuthorRequest:
        return build_author_request(role, operation, task, budget=self.budget, context=self.context,
                                    timeout_s=self.timeout_for(role), asset_name=asset_name)

    async def propose_with_outcomes(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> ProposeResult:
        """All ``n`` requested candidates, each with its recorded outcome. Raises only when none is usable."""
        if budget != self.budget:
            raise ValueError("author bound to another asset budget")
        asset = context["asset_name"]
        refs = has_references(self.context)
        role = self.governor.plan.builder_role

        async def candidate(i: int) -> tuple[SceneIR | None, CandidateOutcome, BaseException | None]:
            trace: dict[str, Any] = {}
            task = propose_task(prompt, index=i, n=n, asset_name=asset, references=refs)
            try:
                req = self.request(role, "propose", task, asset_name=asset)
                scene = await self._generate(req, trace)
                if scene.asset_name != asset or not scene.nodes:
                    raise AuthorInvalid(f"{role} propose: candidate must contain nodes for the requested asset {asset!r}",
                                        finish_reason="STOP", **_meta(req, trace.get("usage")))
            except Exception as exc:  # noqa: BLE001 - classified and recorded; nothing is dropped unnamed
                status = ("not_admitted" if isinstance(exc, BudgetExceeded) else "truncated" if isinstance(exc, AuthorTruncated)
                          else "timed_out" if isinstance(exc, AuthorTimedOut) else "no_answer" if isinstance(exc, AuthorNoAnswer)
                          else "invalid" if isinstance(exc, AuthorInvalid) else "error")
                text = _one_line(exc) if status != "error" else f"{type(exc).__name__}: {_one_line(exc)}"
                return None, CandidateOutcome(i, status, text, getattr(exc, "finish_reason", None),
                                              getattr(exc, "usage", None) or trace.get("usage"),
                                              trace.get("admission_wait_s", 0.0), trace.get("reserved_usd")), exc
            return scene, CandidateOutcome(i, "produced", "", "STOP", trace.get("usage"), trace.get("admission_wait_s", 0.0),
                                           trace.get("reserved_usd")), None

        # A cancellation of this task cancels every candidate (gather) and propagates; a candidate that itself raises a
        # non-Exception BaseException (CancelledError, KeyboardInterrupt) propagates too.
        results = await asyncio.gather(*(candidate(i) for i in range(n)), return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r
        scenes = [scene for scene, _, _ in results if scene is not None]
        outcomes = [outcome for _, outcome, _ in results]
        if scenes:
            return ProposeResult(scenes, outcomes)
        if not results:
            raise BudgetExceeded("no geometry candidate was requested")
        errors = [exc for _, _, exc in results]
        first = next((e for e in errors if not isinstance(e, BudgetExceeded)), errors[0])
        if len(outcomes) > 1:  # keep the first error's type and attributes; name what happened to every candidate
            names = ProposeResult([], outcomes).summary()
            first.args = (f"{first.args[0] if first.args else first}; no usable candidate of {len(outcomes)} requested: {names}",)
        raise first

    async def propose(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> list[SceneIR]:
        return (await self.propose_with_outcomes(prompt, budget=budget, n=n, context=context)).scenes

    async def repair(self, scene: SceneIR, *, action: Any, located: dict[str, list[str]], manifest: str,
                     role: str | None = None) -> IRPatch | None:
        """``role`` overrides the tier (the pyramid's one escalation to ``director``); it must be allowed by the plan."""
        role = role or self.governor.plan.builder_role
        if action.kind == "escalate_director":
            role = "director"
        if role == "director" and not self.governor.plan.allow_director:
            raise BudgetExceeded("director escalation is outside this asset's plan")
        task = repair_task(scene, reasons=action.reasons, located=located, manifest=manifest)
        patch = await self._generate(self.request(role, "repair", task))
        return patch if patch.ops else None
