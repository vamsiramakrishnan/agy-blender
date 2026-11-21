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

from antigravity3d import ir_schema, service_answer
from antigravity3d.decisions.fallback import PRICE_TABLE_USD_PER_M
from antigravity3d.ir import IRPatch, SceneIR
from antigravity3d.kernel.kit_metric import context_brief
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
# (prompt "wooden crate with iron corner brackets", schema-constrained; every sample finished STOP with a valid SceneIR;
# the last three samples of each tier, one for argon, are the run in benchmarks/results/round1/live.json):
#
#   role      model             level   answer tokens (nodes)               thinking tokens         -> answer + thinking
#   worker    gemini-4-hydrogen low     630-1,553 (9-13), 7 samples         804-1,263, 11 samples   ->  16,384 + 8,192
#   bulk      gemini-4-neon     low     1,747-1,919 (25-26), 6 samples      1,374-2,418, 9 samples  ->  8,192 +  8,192
#   builder   gemini-4-carbon   medium  9,965-12,189 (123-146), 6 samples   7,642-13,244, 6 samples -> 16,384 + 16,384
#   director  gemini-4-argon    high    6,337-17,634 (78-168), 3 samples    7,155-7,893, 3 samples  -> 16,384 + 16,384
#
# The worker row was 8,192 + 8,192 for 9-13 node answers. With PROP_CRAFT_GUIDANCE a prop is 55-64 nodes (three live
# samples, 2026-11-21: answers 7,145, 7,682 and 6,831 tokens, thinking 2,399, 2,714 and 2,562, so 9,544, 10,396 and 9,393 of
# 16,384) and the guidance asks for up to 90: at about 113-124 answer tokens per part that is ~10,800 answer tokens, and thinking varied 2.2x
# across the old samples (992-2,152), so ~6,000 is possible: ~16,800 > 16,384. A truncated answer is a lost candidate billed
# at the whole cap, so the worker row is 16,384 answer + 8,192 thinking = 24,576 (hydrogen's limit is 65,536): twice the
# largest crafted answer (15,364 <= 16,384), twice the largest thinking (5,428 <= 8,192) and 2.4x the largest total
# (tests/test_metered_author.py). Only the reservation and a runaway's bill grow; a normal call is billed its usage.
# Both operations use the row, a repair of a 90-part scene included. A kit piece's request is unchanged, so it keeps the
# old row (KIT_OUTPUT_BUDGETS): a larger reservation would admit fewer pieces at once under a ceiling for no gain.
# Headroom: the other LOW tier (neon) gets >= 4.2x the largest answer and >= 3.3x the largest thinking seen (cap 16,384
# of the model's 65,536). carbon and argon are bounded by their output_token_limit (32,768), so their cap IS the model
# maximum; the answer/thinking split there is nominal (both share one cap: argon's largest answer alone was 17,634).
# The worst observed samples used 25,433 (carbon) and 25,527 (argon) tokens, 78% of the cap: a residual truncation
# risk that no larger request can remove, surfaced as AuthorTruncated.
# A hero (director) call is sized by argon's own measurement and limit, never by a worker-tier figure.
# Wall time grows with the answer: carbon took 200-285 s and argon 128-262 s for a full answer (one argon call hit a
# 504 DEADLINE_EXCEEDED at the 300 s request timeout), so callers' timeout_s must allow for it.
# Repairs (hydrogen, 2 flagged nodes): 92 answer + 515 thinking tokens.
AUTHOR_OUTPUT_BUDGETS: dict[str, OutputBudget] = {
    "worker": OutputBudget(answer_tokens=16_384, thinking_tokens=8_192),
    "bulk": OutputBudget(answer_tokens=8_192, thinking_tokens=8_192),
    "builder": OutputBudget(answer_tokens=16_384, thinking_tokens=16_384),
    "director": OutputBudget(answer_tokens=16_384, thinking_tokens=16_384),
}
# The rows a modular-kit piece reserves: the table above as it was before the prop guidance, because a kit piece's request
# and answers did not change. Every role but worker is the same row as for a prop.
KIT_OUTPUT_BUDGETS: dict[str, OutputBudget] = {**AUTHOR_OUTPUT_BUDGETS, "worker": OutputBudget(answer_tokens=8_192, thinking_tokens=8_192)}
# Raw evidence behind the table (propose request). Older hydrogen/neon thinking samples were taken without a response
# schema (benchmarks/results/round1/live.json); answer sizes only from schema-constrained responses.
MEASURED_THINKING_TOKENS: dict[str, tuple[int, ...]] = {
    "gemini-4-hydrogen/low": (856, 944, 1119, 804, 1072, 940, 1082, 880, 1079, 1263, 1066),
    "gemini-4-neon/low": (1374, 1471, 1707, 1401, 1482, 1704, 1988, 1410, 2418),
    "gemini-4-carbon/medium": (7642, 10649, 13244, 9846, 9289, 9650),
    "gemini-4-argon/high": (7179, 7155, 7893),
}
MEASURED_ANSWER_TOKENS: dict[str, tuple[int, ...]] = {
    "gemini-4-hydrogen/low": (664, 1143, 1132, 1135, 1553, 630, 1140),
    "gemini-4-neon/low": (1919, 1799, 1902, 1893, 1747, 1919),
    "gemini-4-carbon/medium": (11178, 12189, 10110, 10004, 10286, 9965),
    "gemini-4-argon/high": (11208, 6337, 17634),
}
# The same request with PROP_CRAFT_GUIDANCE (a prop, 2026-11-21, "military supply crate", hydrogen/low, finish STOP). Three
# samples, so the expected case and not a range. Samples 1 and 2 were made with the first wording of the guidance (parts
# sunk 1-2 mm into what they sit on): 63 and 64 parts, input 6,285, answers 7,145 and 7,682, thinking 2,399 and 2,714, 33
# and 35 s. Sample 3 is the only one made with the wording that ships (exact contact): 55 parts, 1,004 triangles, input
# 6,294, answer 6,831, thinking 2,562, 32 s. The input figure is the shipped wording's. Only
# hydrogen/low was measured with the guidance: the other tiers expect what they were measured at without it (argon and
# carbon already wrote 78-168 nodes unguided), the worst case of every tier stays its cap.
MEASURED_CRAFT_INPUT_TOKENS: dict[str, int] = {"gemini-4-hydrogen/low": 6_294}
MEASURED_CRAFT_ANSWER_TOKENS: dict[str, tuple[int, ...]] = {"gemini-4-hydrogen/low": (7_145, 7_682, 6_831)}
MEASURED_CRAFT_THINKING_TOKENS: dict[str, tuple[int, ...]] = {"gemini-4-hydrogen/low": (2_399, 2_714, 2_562)}
MEASURED_CRAFT_PROPOSE_WALL_S: dict[str, tuple[float, float]] = {"gemini-4-hydrogen/low": (32.0, 35.0)}
# models.get(...).output_token_limit on this key, 2026-11-20.
MODEL_OUTPUT_TOKEN_LIMIT = {HYDROGEN: 65_536, NEON: 65_536, CARBON: 32_768, ARGON: 32_768}
# Measured wall time of one propose call (min, max) in seconds, 2026-11-20, same samples as the token tables above,
# the maximum rounded up to a whole second. (The crafted worker calls, 32 to 35 s for 9,393 to 10,396 output tokens, are
# inside the hydrogen range, so its timeout stays; at that rate, about 300 tokens/s, a 90-part answer takes about 50 s.) argon also had one call end in 504 DEADLINE_EXCEEDED at the 300 s request
# timeout. The slowest carbon call (285 s for 19,850 tokens, about 70 tokens/s against 94-97 on its other samples)
# came while the provider was slow; it completed and was valid, so it counts.
MEASURED_PROPOSE_WALL_S: dict[str, tuple[float, float]] = {
    "gemini-4-hydrogen/low": (10.0, 38.0),
    "gemini-4-neon/low": (26.0, 39.0),
    "gemini-4-carbon/medium": (200.0, 285.0),
    "gemini-4-argon/high": (128.0, 262.0),
}
# Total request timeout per authoring role. Rule (asserted in tests/test_metered_author.py): at least
# TIMEOUT_HEADROOM x the slowest measured call of the role's model, rounded up to a multiple of 30 s. A call that times
# out is billed its whole reservation and yields nothing, so a timeout must not sit near the healthy range: argon's
# slowest healthy call (262 s) was within reach of the old 300 s for every tier. The table follows the measurements,
# not a ranking of the tiers: carbon's slowest call was slower than argon's. A total, not an idle, timeout: the
# thinking phase of a call is silent (no chunk arrives until thinking ends, ~100 s on argon), so a stream cannot tell
# a stalled call from a thinking one without include_thoughts, which would change the billed request.
TIMEOUT_HEADROOM = 2.0
AUTHOR_TIMEOUT_S: dict[str, float] = {"worker": 90.0, "bulk": 90.0, "builder": 570.0, "director": 540.0}
# Prompt tokens the API bills for response_json_schema, per byte of compact schema JSON. Measured on gemini-4-neon,
# same request with both spellings of the schema: the propose schema written out in full (7,954 bytes, descriptions,
# tuples repeated) billed 7,305 prompt tokens, the compact one (6,064 bytes) 5,928: the schema itself 5,085 -> 3,708
# tokens (0.64 and 0.61 per byte). The repair schema was measured only in the older, larger spelling (9,939 bytes ->
# 7,066 tokens, 0.71/byte; 9,289 bytes now). 1.0 bounds all of them.
SCHEMA_INPUT_TOKENS_PER_BYTE = 1.0
# Retries of an authoring attempt whose outcome is unknown (a broken connection, a 500): each keeps its whole
# reservation (an argon repair reserved 45,486 tokens of a 210,000 ceiling live), so one. A request the service said it
# did not serve books nothing and follows the shared schedule (service_answer.DEFAULT_POLICY).
AUTHOR_UNKNOWN_RETRIES = 1


def output_budget(role: str, *, kit: bool = False) -> OutputBudget:
    try:
        return (KIT_OUTPUT_BUDGETS if kit else AUTHOR_OUTPUT_BUDGETS)[role]
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


class AuthorUnavailable(AuthorOutcomeError):
    """The service gave no answer after the shared retry schedule (``service_answer``): it said it did not serve the
    request (503 UNAVAILABLE, 429 RESOURCE_EXHAUSTED), or failed on its side (500, 502), or the connection broke.
    ``attempts``: every failed attempt with its outcome and the wait after it. ``nothing_booked``: every attempt was
    turned away, so the ledger holds nothing for this call; otherwise the attempts that may have run kept their
    reservation."""

    def __init__(self, message: str, *, attempts: list[dict[str, Any]], nothing_booked: bool, code: int | None,
                 status: str | None, **meta: Any) -> None:
        super().__init__(message, **meta)
        self.attempts, self.nothing_booked, self.code, self.status = attempts, nothing_booked, code, status


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


# What a good prop is. The author's whole instruction used to be the format rules and one sentence, and nothing said what
# a good prop looks like. Measured live on 2026-11-21 (gemini-4-hydrogen, same schema, thinking level and cap) on "a rugged
# military supply crate kit-bashed from panels, corner guards, latches and two side handles", budget 3,000 triangles:
# without this text five designs had 15-21 parts and 272-356 triangles (9-12% of the budget; answers 2,037-2,662 tokens,
# thinking 992-2,152, about 14 s); with it two designs had 63 and 64 parts and 976 and 936 triangles (33% and 31%; input
# 6,285 tokens against 6,024, answers 7,145 and 7,682, thinking 2,399 and 2,714, 33 and 35 s, finish STOP). Contact is
# exact, not overlapped: a first version told the author to sink parts 1-2 mm into what they sit on, and Tier-1 reported
# 103-116 interpenetration and 28-38 z-fighting warnings (their hidden back faces end up coplanar); hidden contact faces
# are removed at export (kernel/package.py), so the text says to mount on the surface. A modular-kit piece does not get it:
# its geometry obeys the kit metric (its ``brief``), tuned separately.
PROP_CRAFT_GUIDANCE = (
    "Model a production game prop, not a blockout.\n"
    "- Every part the brief names gets its own nodes, built from the pieces a real one has: a latch is a base plate, a clasp "
    "and a hinge barrel; a handle is two brackets and a grip; a corner guard wraps its corner.\n"
    "- Add the secondary forms an object of this kind has wherever they change the silhouette or catch the light: rims, ribs, "
    "frames, skids or feet, straps, hinges, bolt heads. Break large flat faces with raised or recessed panels made from thin "
    "boxes.\n"
    "- Aim for 50 to 90 parts, within max_tris: a box is 12 triangles, a cylinder 4 x segments (8 to 12 segments is enough "
    "for a small part), a sphere or capsule costs hundreds. Do not pad with hidden or repeated filler.\n"
    "- Keep parts in proportion: thin plates and slim bars, not chunky blocks.\n"
    "- Attach by contact: a part's face lies exactly on the surface it is mounted on, with no gap and no overlap (hidden "
    "contact faces are removed at export). Two parts mounted on the same surface must not share area: where one crosses "
    "another, mount it on the other's face.\n"
    "- Set collider false on small detail parts; keep it for the main solids."
)


def propose_task(prompt: str, *, index: int, n: int, asset_name: str, references: bool, brief: str = "") -> str:
    """``brief``: constraints the answer must meet exactly, appended last (a modular kit piece's metric,
    ``kernel.kit_metric.context_brief``); with one the request is unchanged byte for byte. Without one (a prop) the task
    is followed by ``PROP_CRAFT_GUIDANCE``."""
    refs = "Use the attached references. " if references else ""
    task = (f"Build design {index + 1}/{n} for {prompt}. {refs}Return one JSON SceneIR with asset_name={asset_name!r}, "
            f"nodes and materials. Ground all parts. Include material assignments.")
    return f"{task} {brief}" if brief else f"{task}\n{PROP_CRAFT_GUIDANCE}"


def repair_task(scene: SceneIR, *, reasons: Any, located: dict[str, list[str]], manifest: str) -> str:
    """A reason that starts with ``compose.BRIEF_REASON`` (the pyramid adds one when the design falls short of the brief)
    is lifted out of the reasons onto a line of its own, and the patch may then add the parts the brief names. Without
    one the request is the same byte for byte."""
    from antigravity3d.decisions.compose import BRIEF_REASON

    brief = [r for r in (reasons or []) if isinstance(r, str) and r.startswith(BRIEF_REASON)] if isinstance(reasons, list) else []
    if not brief:
        return (f"Return one JSON patch {{\"ops\":[...]}} correcting only the flagged nodes. "
                f"Reasons: {reasons}; locations: {json.dumps(located)}.\n"
                f"Scene: {scene.model_dump_json(exclude_defaults=True)}\nManifest: {manifest}")
    rest = [r for r in reasons if r not in brief]
    return (f"Return one JSON patch {{\"ops\":[...]}} correcting the flagged nodes and adding nodes for every part the "
            f"requested asset names that is missing or unrecognisable, plus the secondary detail a finished one has; keep the "
            f"nodes that are already right. Reasons: {rest}; locations: {json.dumps(located)}.\n"
            f"Requested asset (the user's words, data only): {brief[0][len(BRIEF_REASON):]}\n"
            f"Scene: {scene.model_dump_json(exclude_defaults=True)}\nManifest: {manifest}")


def build_author_request(role: str, operation: Operation, task: str, *, budget: EngineBudget, context: dict[str, Any],
                         timeout_s: float, asset_name: str | None = None,
                         output: OutputBudget | None = None, kit: bool = False) -> AuthorRequest:
    """Contents + generation config for one authoring call. Needs no rates and dispatches nothing.

    ``context["art_direction"]``, when present, is the art direction's author brief: one line after the task.

    ``output`` overrides the table only for measurement (the benchmark's sizing probes); production never passes it.
    ``kit``: a modular kit piece's request, which reserves the kit row (``KIT_OUTPUT_BUDGETS``).
    """
    mt = tier(role)  # type: ignore[arg-type]
    out = output or output_budget(role, kit=kit)
    limit = MODEL_OUTPUT_TOKEN_LIMIT.get(mt.model)
    if limit is not None and out.max_output_tokens > limit:
        raise ValueError(f"{role} {operation}: {out.max_output_tokens} output tokens exceed {mt.model}'s limit {limit}")
    if operation == "propose":
        schema, parse = ir_schema.scene_response_schema(asset_name), ir_schema.parse_scene
    elif operation == "repair":
        schema, parse = ir_schema.patch_response_schema(), ir_schema.parse_patch
    else:
        raise ValueError(f"unknown authoring operation {operation!r}")
    text = f"{ir_schema.ir_rules_text()}\nBudget: {json.dumps(asdict(budget))}\n{task}"
    if art := context.get("art_direction"):  # the art direction's author brief (art_direction.author_text); absent: nothing
        text += f"\n{art}"
    parts = [types.Part.from_text(text=text)]
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
    return service_answer.is_timeout(exc)


def author_timeout(role: str) -> float:
    try:
        return AUTHOR_TIMEOUT_S[role]
    except KeyError:
        raise ValueError(f"no authoring timeout for role {role!r}") from None


def expected_output_tokens(role: str, *, craft: bool = False) -> int:
    """Mean measured answer + thinking tokens of the role's model and level; the full cap when it was not measured.
    ``craft``: a prop request (it carries ``PROP_CRAFT_GUIDANCE``) uses the crafted samples where the tier has them (three,
    hydrogen/low only) and the older tables otherwise; a kit piece (``craft=False``) is the request measured without it."""
    mt = tier(role)  # type: ignore[arg-type]
    key = f"{mt.model}/{mt.thinking.value if mt.thinking else 'none'}"
    answers, thoughts = MEASURED_ANSWER_TOKENS.get(key), MEASURED_THINKING_TOKENS.get(key)
    if craft and key in MEASURED_CRAFT_ANSWER_TOKENS:
        answers, thoughts = MEASURED_CRAFT_ANSWER_TOKENS[key], MEASURED_CRAFT_THINKING_TOKENS[key]
    if not answers or not thoughts:
        return output_budget(role, kit=not craft).max_output_tokens
    return min(output_budget(role, kit=not craft).max_output_tokens, round(sum(answers) / len(answers) + sum(thoughts) / len(thoughts)))


@dataclass(frozen=True)
class CandidateOutcome:
    """What happened to one requested candidate. ``status``: produced, not_admitted, truncated, no_answer, invalid,
    timed_out, unavailable (no answer from the service after the shared retry schedule) or error."""

    index: int
    status: str
    detail: str = ""
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    admission_wait_s: float = 0.0
    reserved_usd: float | None = None
    reserved_tokens: float | None = None  # token ledgers only (then reserved_usd stays None)
    # the failed attempts before the outcome, each with the service's answer and the wait after it (service_answer)
    service_retries: list[dict[str, Any]] | None = None
    # taken from the design record (no call was made): what the original answer cost (DesignRecord)
    recorded: dict[str, Any] | None = None

    @property
    def produced(self) -> bool:
        return self.status == "produced"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.reserved_tokens is None:  # a money outcome keeps exactly the keys it always had
            del d["reserved_tokens"]
        if not self.service_retries:  # only when there was a retry
            del d["service_retries"]
        if self.recorded is None:  # only for an answer from the design record
            del d["recorded"]
        return d


def outcome_status(exc: BaseException) -> str:
    """The candidate-outcome status of an authoring failure."""
    return ("not_admitted" if isinstance(exc, BudgetExceeded) else "truncated" if isinstance(exc, AuthorTruncated)
            else "timed_out" if isinstance(exc, AuthorTimedOut) else "unavailable" if isinstance(exc, AuthorUnavailable)
            else "no_answer" if isinstance(exc, AuthorNoAnswer) else "invalid" if isinstance(exc, AuthorInvalid) else "error")


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
                 timeout_s: float | None = None, retry: service_answer.RetryPolicy | None = None,
                 record: Any = None) -> None:
        self.client, self.governor, self.budget = client, governor, budget
        # tactics.cached.DesignRecord: answered requests of earlier runs in the same --out, reused without a call (None: off)
        self.record = record
        self.context, self.timeout_s = dict(context), timeout_s
        # None: the shared schedule (service_answer.DEFAULT_POLICY, read at call time) with AUTHOR_UNKNOWN_RETRIES
        self.retry = retry
        # Retries of an attempt whose outcome is unknown; a caller with a recovery round of its own (a kit) sets 0.
        self.unknown_retries = AUTHOR_UNKNOWN_RETRIES
        self.last_service_retries: list[dict[str, Any]] = []  # the failed attempts of the latest repair (waits included)

    def timeout_for(self, role: str) -> float:
        return author_timeout(role) if self.timeout_s is None else self.timeout_s

    def cost_profile(self, role: str | None = None, *, asset_name: str = "asset", kit: bool = False) -> tuple[float, float] | None:
        """(expected, worst-case) USD of one propose call for sizing a best-of-N, or None when no rates are known.
        On a token ledger the same pair in tokens (never None).

        ``kit``: the request of a modular kit piece (no craft guidance, the older measurements).
        Expected = mean measured output tokens (``expected_output_tokens``); worst case = the full cap, which is what
        admission reserves. Input is estimated from the request text (3 bytes per token) plus the schema bound, because
        counting needs a network call; sizing only, admission uses the counted figure.
        """
        role = role or self.governor.plan.builder_role
        req = self.request(role, "propose", propose_task("x", index=0, n=1, asset_name=asset_name, brief="kit" if kit else "",
                                                          references=has_references(self.context)), asset_name=asset_name, kit=kit)
        text = "".join(p.text or "" for c in req.contents for p in c.parts)
        tokens_in = math.ceil(len(text.encode()) / 3) + schema_input_token_bound(req.schema)
        if self.governor.unit == "tokens":  # a token ledger: the same figures in tokens, no price applied
            return float(tokens_in + expected_output_tokens(role, craft=not kit)), float(tokens_in + req.max_output_tokens)
        try:
            pin, pout = _rates(req.model)
        except BudgetExceeded:
            return None
        expected = usage_cost_usd(tokens_in, expected_output_tokens(role, craft=not kit), usd_per_m_in=pin, usd_per_m_out=pout)
        worst = usage_cost_usd(tokens_in, req.max_output_tokens, usd_per_m_in=pin, usd_per_m_out=pout)
        return expected, worst

    async def _generate(self, req: AuthorRequest, trace: dict[str, Any] | None = None) -> Any:
        trace = {} if trace is None else trace
        tokens = self.governor.unit == "tokens"  # a token ledger: worst case and booking in tokens, no price applied
        pin, pout = (None, None) if tokens else _rates(req.model)
        api = self.client.aio.models
        policy = self.retry or service_answer.DEFAULT_POLICY.with_unknown_retries(self.unknown_retries)
        deadline_s = req.config.http_options.timeout / 1000
        tracker = service_answer.Tracker(policy)
        while True:  # counting is free: a count the service turned away, or whose connection broke, is asked again
            sent = time.monotonic()
            try:
                counted = await api.count_tokens(model=req.model, contents=req.contents,
                                                 config=types.CountTokensConfig(http_options=req.config.http_options))
                break
            except Exception as exc:
                elapsed = time.monotonic() - sent
                outcome = service_answer.classify(exc, elapsed, deadline_s=deadline_s)
                if outcome == "unknown":
                    outcome = "not_served"  # nothing is billed for counting, whatever happened to it
                if outcome == "final" or not await tracker.failed(exc, elapsed, outcome):
                    if outcome == "final":
                        raise
                    raise self._unavailable(req, exc, tracker, "counting its input", booked_any=False) from exc
        trace["service_retries"] = tracker.record()
        n = counted.total_tokens
        if not isinstance(n, int) or n <= 0:
            raise BudgetExceeded(f"no input token count for {req.model}")
        schema_bound = schema_input_token_bound(req.schema)
        input_bound = n + schema_bound

        def cost(inputs: int, outputs: int) -> float:
            if tokens:
                return float(inputs + outputs)
            return usage_cost_usd(inputs, outputs, usd_per_m_in=pin, usd_per_m_out=pout)

        limit = cost(input_bound, req.max_output_tokens)
        detail = (f"{req.role} ({req.model}) {req.operation}: up to {input_bound} input tokens ({n} counted + {schema_bound} "
                  f"schema bound) and {req.max_output_tokens} output tokens ({req.budget.answer_tokens} answer + "
                  f"{req.budget.thinking_tokens} thinking) " + (f"= {limit:,.0f} tokens (no price applied)" if tokens
                                                                  else f"at ${pin}/${pout} per M = ${limit:.5f}"))
        receipt: dict[str, Any] = {"model": req.model, "role": req.role, "counted_input_tokens": n,
                                   "schema_input_tokens_bound": schema_bound, "max_output_tokens": req.max_output_tokens,
                                   "answer_tokens": req.budget.answer_tokens, "thinking_tokens_allowed": req.budget.thinking_tokens}
        if not tokens:
            receipt.update(input_usd_per_m=pin, output_usd_per_m=pout)
        base_receipt = receipt
        booked_any = False  # whether an attempt of this call kept a reservation (it may have run)
        waited = 0.0
        while True:
            receipt = dict(base_receipt)
            if tracker.attempts:  # a retried call: which attempt this is, and what came before it
                receipt.update(attempt=tracker.n, service_retries=tracker.record())
            t0 = time.monotonic()
            try:
                # Waits for outstanding reservations to settle; fails at once only when this can never fit the ceiling.
                hold = await self.governor.reserve_wait(f"author.{req.operation}", limit, receipt=receipt)
            except BudgetExceeded as e:
                raise BudgetExceeded(f"plan {self.governor.plan.label!r}: {e}; request: {detail}") from e
            waited += time.monotonic() - t0
            trace.update(admission_wait_s=round(waited, 3), **{"reserved_tokens" if tokens else "reserved_usd": limit})
            sent = time.monotonic()
            try:
                result = await api.generate_content(model=req.model, contents=req.contents, config=req.config)
            except BaseException as exc:
                elapsed = time.monotonic() - sent
                outcome = service_answer.classify(exc, elapsed, deadline_s=deadline_s)
                receipt["status"] = type(exc).__name__
                if outcome == "not_served":  # the service said it did not run it: nothing is booked
                    receipt.update(service_answer.not_served_receipt(exc, tracker.n, elapsed), usage_known=True)
                    hold.settle(0.0, receipt=receipt)
                else:  # it may have run: the whole reservation is retained
                    if is_timeout(exc):  # raised as AuthorTimedOut below, and the receipt says so (as it always did)
                        receipt.update(finish_reason="TIMEOUT", status="AuthorTimedOut" if outcome == "final" else receipt["status"])
                    if isinstance(exc, Exception):
                        receipt.update(service_answer=service_answer.describe(exc), elapsed_s=round(elapsed, 3))
                    receipt["usage_known"] = False
                    hold.settle(None, receipt=receipt)
                    booked_any = True
                if not isinstance(exc, Exception):
                    raise
                if outcome != "final" and await tracker.failed(exc, elapsed, outcome):
                    trace["service_retries"] = tracker.record()
                    continue
                trace["service_retries"] = tracker.record()
                if is_timeout(exc):
                    raise AuthorTimedOut(
                        f"{req.role} {req.operation} on {req.model}: timed out after {elapsed:.0f}s of {deadline_s:.0f}s "
                        f"({type(exc).__name__}); the whole reservation {self.governor.fmt(limit)} is retained"
                        + (f"; {tracker.summary()}" if len(tracker.attempts) > 1 else ""), finish_reason="TIMEOUT",
                        **_meta(req, None)) from exc
                if outcome == "final":
                    raise  # a client error (4xx): a bug in the request, not an outcome
                raise self._unavailable(req, exc, tracker, "generating", booked_any=booked_any) from exc
            break
        booked = None
        try:
            usage = usage_tokens(result)
            trace["usage"] = usage
            if usage is not None:
                booked = cost(usage["input_tokens"], usage["output_tokens"])
                receipt.update(usage)
            trace["booked"] = booked if booked is not None else limit  # what the ledger keeps for this answer
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

    async def _answer(self, req: AuthorRequest, trace: dict[str, Any], *, index: int | None,
                      keep: Callable[[Any], bool] = lambda answer: True) -> Any:
        """The answer to ``req``: from the design record when an earlier run in this ``--out`` asked exactly this (no
        call, not even a count; a receipt that says where it came from and what it originally cost, nothing booked),
        else from :meth:`_generate`, and then recorded at once, so a run that ends later keeps what it paid for.
        ``keep``: whether an answer is worth recording (a candidate for another asset is not)."""
        record = self.record
        key = record.key(req, index) if record is not None else None
        if key is not None:
            hit = await asyncio.to_thread(record.get, key)
            if hit is not None:
                self._book_reuse(req, hit)
                record.note_reuse(hit)
                trace.update(recorded=hit.original, **{"reserved_tokens" if self.governor.unit == "tokens" else "reserved_usd": 0.0})
                return hit.answer
        answer = await self._generate(req, trace)
        if key is not None and keep(answer):
            original = {"unit": self.governor.unit, "booked": trace.get("booked"), "usage": trace.get("usage"),
                        "model": req.model, "role": req.role, "operation": req.operation}
            await asyncio.shield(asyncio.to_thread(record.put, key, answer, original))  # a stop never leaves it half written
        return answer

    def _book_reuse(self, req: AuthorRequest, hit: Any) -> None:
        receipt = {"model": req.model, "role": req.role, "source": "record", "status": "ok", "usage_known": True,
                   "record_key": hit.key[:23], "original": hit.original,
                   "booked_reason": "the same request was answered in an earlier run of this --out (design record): "
                                    "no call was made and nothing is booked; 'original' is what that answer cost"}
        self.governor.reserve(f"author.{req.operation}", 0.0, receipt=receipt).settle(0.0, receipt=receipt)

    def _unavailable(self, req: AuthorRequest, exc: BaseException, tracker: service_answer.Tracker, doing: str, *,
                     booked_any: bool) -> AuthorUnavailable:
        code, status = service_answer.code_status(exc)
        rejected = sum(a.outcome == "not_served" for a in tracker.attempts)
        booking = ("nothing booked: the service turned every attempt away" if not booked_any
                   else f"nothing booked for the {rejected} attempt(s) turned away; the attempts that may have run kept their reservation"
                   if rejected else "every attempt that may have run kept its reservation")
        return AuthorUnavailable(
            f"{req.role} {req.operation} on {req.model}: no answer while {doing} after {len(tracker.attempts)} attempt(s) over "
            f"{tracker.waited_s:.0f} s of waiting; the service answered {service_answer.describe(exc)} ({booking})",
            attempts=tracker.record(), nothing_booked=not booked_any, code=code, status=status,
            finish_reason=status or type(exc).__name__, **_meta(req, None))

    def request(self, role: str, operation: Operation, task: str, *, asset_name: str | None = None,
                kit: bool | None = None) -> AuthorRequest:
        """``kit`` None: this author is bound to a kit piece when its context carries the kit metric."""
        kit = bool(context_brief(self.context)) if kit is None else kit
        return build_author_request(role, operation, task, budget=self.budget, context=self.context,
                                    timeout_s=self.timeout_for(role), asset_name=asset_name, kit=kit)

    async def propose_with_outcomes(self, prompt: str, *, budget: EngineBudget, n: int, context: dict[str, Any]) -> ProposeResult:
        """All ``n`` requested candidates, each with its recorded outcome. Raises only when none is usable."""
        if budget != self.budget:
            raise ValueError("author bound to another asset budget")
        asset = context["asset_name"]
        refs = has_references(self.context)
        role = self.governor.plan.builder_role
        brief = context_brief(context)  # a kit piece's metric (and, on its second request, what the first got wrong)

        async def candidate(i: int) -> tuple[SceneIR | None, CandidateOutcome, BaseException | None]:
            trace: dict[str, Any] = {}
            task = propose_task(prompt, index=i, n=n, asset_name=asset, references=refs, brief=brief)
            try:
                req = self.request(role, "propose", task, asset_name=asset, kit=bool(brief))
                scene = await self._answer(req, trace, index=i, keep=lambda s: s.asset_name == asset and bool(s.nodes))
                if scene.asset_name != asset or not scene.nodes:
                    raise AuthorInvalid(f"{role} propose: candidate must contain nodes for the requested asset {asset!r}",
                                        finish_reason="STOP", **_meta(req, trace.get("usage")))
            except Exception as exc:  # noqa: BLE001 - classified and recorded; nothing is dropped unnamed
                status = outcome_status(exc)
                text = _one_line(exc) if status != "error" else f"{type(exc).__name__}: {_one_line(exc)}"
                return None, CandidateOutcome(i, status, text, getattr(exc, "finish_reason", None),
                                              getattr(exc, "usage", None) or trace.get("usage"),
                                              trace.get("admission_wait_s", 0.0), trace.get("reserved_usd"),
                                              trace.get("reserved_tokens"), trace.get("service_retries") or None), exc
            return scene, CandidateOutcome(i, "produced", "", "STOP", trace.get("usage"), trace.get("admission_wait_s", 0.0),
                                           trace.get("reserved_usd"), trace.get("reserved_tokens"),
                                           trace.get("service_retries") or None, trace.get("recorded")), None

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

    def recorded_candidates(self, prompt: str, *, n: int, context: dict[str, Any]) -> int:
        """How many of the ``n`` candidates :meth:`propose_with_outcomes` would take from the design record (no call is
        made to find out: the keys of the very requests it would send)."""
        if self.record is None or self.record.fresh:
            return 0
        asset, refs, role, brief = context["asset_name"], has_references(self.context), self.governor.plan.builder_role, context_brief(context)
        return sum(self.record.get(self.record.key(self.request(role, "propose", propose_task(
            prompt, index=i, n=n, asset_name=asset, references=refs, brief=brief), asset_name=asset, kit=bool(brief)), i)) is not None
            for i in range(n))

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
        trace: dict[str, Any] = {}
        self.last_service_retries = []
        try:
            patch = await self._answer(self.request(role, "repair", task), trace, index=None)
        finally:
            self.last_service_retries = list(trace.get("service_retries") or [])
        return patch if patch.ops else None
