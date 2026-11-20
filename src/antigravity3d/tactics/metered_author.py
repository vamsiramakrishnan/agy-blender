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


def _rates(model: str) -> tuple[float, float]:
    rates = json.loads(os.environ.get("AGY3D_TEXT_RATES_JSON", "{}"))
    pair = rates.get(model, PRICE_TABLE_USD_PER_M.get(model))
    if (not isinstance(pair, (list, tuple)) or len(pair) != 2
            or any(not isinstance(x, (float, int)) or not math.isfinite(x) or x < 0 for x in pair)):
        raise BudgetExceeded(f"configure AGY3D_TEXT_RATES_JSON with [USD/M input, USD/M output] for {model}")
    return float(pair[0]), float(pair[1])


class MeteredAuthor:
    """Bound to one route's budget, spend ledger and image references, including during repair.

    No automatic re-request on truncation. The cap is sized from measurements with headroom (and is the model maximum
    for carbon / argon, so there is no larger budget to retry with); a MAX_TOKENS finish therefore means a runaway
    generation that a re-request would most likely repeat, billing the cap twice.
    The typed error carries the finish reason and billed usage; the caller decides.
    """

    def __init__(self, client: Any, governor: Governor, budget: EngineBudget, context: dict, *, timeout_s: float) -> None:
        self.client, self.governor, self.budget = client, governor, budget
        self.context, self.timeout_s = dict(context), timeout_s

    async def _generate(self, req: AuthorRequest) -> Any:
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
        plan = self.governor.plan
        if limit > plan.ceiling_usd:
            raise BudgetExceeded(f"plan {plan.label!r} ceiling ${plan.ceiling_usd:.2f} cannot admit one {detail}")
        receipt: dict[str, Any] = {"model": req.model, "role": req.role, "counted_input_tokens": n,
                                   "schema_input_tokens_bound": schema_bound, "max_output_tokens": req.max_output_tokens,
                                   "answer_tokens": req.budget.answer_tokens, "thinking_tokens_allowed": req.budget.thinking_tokens,
                                   "input_usd_per_m": pin, "output_usd_per_m": pout}
        try:
            hold = self.governor.reserve(f"author.{req.operation}", limit, receipt=receipt)
        except BudgetExceeded as e:
            raise BudgetExceeded(f"{e}; request: {detail}") from e
        booked = None
        try:
            result = await api.generate_content(model=req.model, contents=req.contents, config=req.config)
            usage = usage_tokens(result)
            if usage is not None:
                booked = cost(usage["input_tokens"], usage["output_tokens"])
                receipt.update(usage)
            candidates = getattr(result, "candidates", None) or []
            receipt["finish_reason"] = _name(getattr(candidates[0], "finish_reason", None)) if candidates else None
            text = check_response(req, result, usage)
            receipt["status"] = "ok"
            return req.parse(text)
        except BaseException as exc:
            receipt["status"] = type(exc).__name__
            raise
        finally:
            receipt["usage_known"] = booked is not None
            hold.settle(booked, receipt=receipt)

    def request(self, role: str, operation: Operation, task: str, *, asset_name: str | None = None) -> AuthorRequest:
        return build_author_request(role, operation, task, budget=self.budget, context=self.context,
                                    timeout_s=self.timeout_s, asset_name=asset_name)

    async def propose(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> list[SceneIR]:
        if budget != self.budget:
            raise ValueError("author bound to another asset budget")
        asset = context["asset_name"]
        refs = has_references(self.context)

        async def candidate(i: int) -> SceneIR:
            task = propose_task(prompt, index=i, n=n, asset_name=asset, references=refs)
            scene = await self._generate(self.request(self.governor.plan.builder_role, "propose", task, asset_name=asset))
            if scene.asset_name != asset or not scene.nodes:
                raise ValueError("candidate must contain nodes for the requested asset")
            return scene

        results = await asyncio.gather(*(candidate(i) for i in range(n)), return_exceptions=True)
        scenes = [r for r in results if isinstance(r, SceneIR)]
        if not scenes:
            if results:
                first_error = results[0]
                if isinstance(first_error, BaseException):
                    raise first_error
            raise BudgetExceeded("no geometry candidate fits the current plan")
        return scenes

    async def repair(self, scene: SceneIR, *, action: Any, located: dict[str, list[str]], manifest: str) -> IRPatch | None:
        role = self.governor.plan.builder_role
        if action.kind == "escalate_director":
            if not self.governor.plan.allow_director:
                raise BudgetExceeded("director escalation is outside this asset's plan")
            role = "director"
        task = repair_task(scene, reasons=action.reasons, located=located, manifest=manifest)
        patch = await self._generate(self.request(role, "repair", task))
        return patch if patch.ops else None
