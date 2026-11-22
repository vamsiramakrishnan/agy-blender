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
import base64
import json
import math
import os
import time
import weakref
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Literal

from google.genai import types

from antigravity3d import ir_schema, service_answer
from antigravity3d.authoring_contract import PROP_CRAFT_GUIDANCE, repair_scope
from antigravity3d.decisions.fallback import PRICE_TABLE_USD_PER_M
from antigravity3d.ir import IRPatch, SceneIR
from antigravity3d.kernel.kit_metric import context_brief
from antigravity3d.models import ARGON, CARBON, HYDROGEN, NEON, tier
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import BudgetExceeded, Governor, usage_cost_usd
from antigravity3d.tactics.stream_call import ModelCallProgress, StreamLimits, StreamState, stream_generate

# "improve": a patch to a design that already passes its checks, asked for so that it reads as the finished object
# (pass A's improvement rounds). The same schema and output row as a repair; its own operation so that the ledger
# (author.improve), the receipts and the design record tell the two apart.
# "look": the look round's material changes (tactics.look_round): material operations only, read against the scene
# (ir_schema.apply_look, which the caller binds), ledger author.look.
Operation = Literal["propose", "repair", "improve", "correct", "look"]
PATCH_OPERATIONS = ("repair", "improve")


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
MEASURED_CRAFT_INPUT_TOKENS_R4: dict[str, int] = {"gemini-4-hydrogen/low": 6_294}
MEASURED_CRAFT_ANSWER_TOKENS: dict[str, tuple[int, ...]] = {"gemini-4-hydrogen/low": (7_145, 7_682, 6_831)}
MEASURED_CRAFT_THINKING_TOKENS: dict[str, tuple[int, ...]] = {"gemini-4-hydrogen/low": (2_399, 2_714, 2_562)}
MEASURED_CRAFT_PROPOSE_WALL_S_R4: dict[str, tuple[float, float]] = {"gemini-4-hydrogen/low": (32.0, 35.0)}
# The request with round five's language (new shapes, mirror / repeat, SHAPE_SEMANTICS; 2026-11-22, the same crate
# brief, hydrogen/low, five samples, all valid, finish STOP): the rules text is longer (input 10,489) and the answers
# are shorter, because a symmetric or repeated part is written once: 19-25 authored nodes that expand to 45-68 parts,
# answers 2,073-3,998 tokens, thinking 2,232-3,616, 23-29 s. These are the expected case of a prop request now. The
# round-four samples above stay the bound on the cap: a design written without shortcuts can still be 90 parts at
# 124 answer tokens each (tests/test_metered_author.py).
MEASURED_CRAFT_INPUT_TOKENS: dict[str, int] = {"gemini-4-hydrogen/low": 10_489}
MEASURED_SHAPES_ANSWER_TOKENS: dict[str, tuple[int, ...]] = {"gemini-4-hydrogen/low": (2_386, 3_998, 3_000, 2_298, 2_073)}
MEASURED_SHAPES_THINKING_TOKENS: dict[str, tuple[int, ...]] = {"gemini-4-hydrogen/low": (2_232, 3_565, 3_141, 3_385, 3_616)}
MEASURED_CRAFT_PROPOSE_WALL_S: dict[str, tuple[float, float]] = {"gemini-4-hydrogen/low": (23.0, 29.0)}
# A correction (``correct_task``) writes only the nodes that failed, not the design. Measured live (2026-11-22,
# gemini-4-hydrogen/low, the real failed crate answer replayed three times, two nodes to replace, all three rescued):
# answers 369, 213 and 270 tokens, thinking 311, 301 and 811, 3.4-5.2 s. Twice the largest answer (738) would bound a
# four-node correction only; the case to bound is one that replaces many nodes: at the largest measured 185 answer
# tokens per node, 4,096 allows 22 replacement nodes (a third of a 60-part design; past that it is a redesign, which
# this call is not for). Thinking keeps the role's allowance (8,192 for the worker, 10x the largest seen).
CORRECTION_ANSWER_TOKENS = 4_096
# A look answer (tactics.look_round) writes a few fields of a few materials: about 40-80 tokens per material changed by the
# size of one upsert_material, so 4,096 holds every material of a prop many times over (a prop has 2-8). Not measured
# live yet; thinking keeps the role's allowance, since judging colours against the request is the work of the call.
LOOK_ANSWER_TOKENS = 4_096
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
# Total deadline per authoring role. Rule (asserted in tests/test_metered_author.py): at least TIMEOUT_HEADROOM x the
# slowest measured call of the role's model, rounded up to a multiple of 30 s. A call that runs out its deadline keeps
# its whole reservation and yields nothing, so the deadline must not sit near the healthy range. The table follows the
# measurements, not a ranking of the tiers: carbon's slowest call was slower than argon's.
# The call is streamed (tactics/stream_call.py), and this is the stream's whole deadline, enforced by stream_call; it
# is also the request's httpx timeout, which on a stream bounds each read, not the call. Silence is caught earlier by
# the limits below (stream_limits). Blocking, nothing survived 300 s: on 2026-11-22 blocking builder calls were cut by
# the far end at 300.0-300.009 s with no response (crate brief once; chair brief both attempts, r5/live/m1_before), so
# these totals were unreachable and a slow call lost its reservation. The cut is on silence, not on total time: two
# streamed builder calls of the chair brief ran 403.9 s and 431.1 s and reached their finish (r5/live/stream_chair).
# On a connection that does not stream (a proxy that buffers the whole response; MeteredAuthor._generate asks again
# without streaming), the blocking call gets this same deadline: this project adds no 300 s cut of its own, so a call that
# thinks long can still complete wherever the path between allows it; a direct blocking call meets the far end's cut.
TIMEOUT_HEADROOM = 2.0
AUTHOR_TIMEOUT_S: dict[str, float] = {"worker": 90.0, "bulk": 90.0, "builder": 570.0, "director": 540.0}
# Silence limits of a streamed authoring call (stream_call.StreamLimits). Measured live 2026-11-22 on the shipped propose
# request (crate brief; prompt 8,030 tokens) streamed with thought summaries: r5/live/stream_gaps2 (23 calls: worker x5,
# bulk x3, builder x4, director x4, director repair x2, worker without summaries x5), plus r5/live/stream_probe and
# stream_probe2 (one builder and one director each). Every chunk, thought chunks included, carried the running usage;
# no chunk carried a continuation_token (so the SDK never resumed a stream on its own).
# Thought summaries are asked for at send time (stream_call.with_thought_summaries), not in the request, so the design
# record's key and the benchmark's request do not change. They do not change what is billed: the prompt was 8,030 tokens
# with and without them, and worker thinking was 2,809-3,441 tokens with them against 2,433-3,315 without (five each;
# answers 7,171-7,684 against 4,572-8,547). Without them the worker sent nothing for 11.4-14.1 s (its whole thinking);
# with them its first chunk came at 2.1-2.9 s.
IDLE_HEADROOM = 4.0
# Seconds from the send to the first chunk. The 28.4 s (carbon) and 44.6 s (argon) came on a busy night: 3-6x the others.
MEASURED_STREAM_FIRST_CHUNK_S: dict[str, tuple[float, ...]] = {
    "gemini-4-hydrogen/low": (2.95, 2.46, 2.68, 2.40, 2.09),
    "gemini-4-neon/low": (4.18, 4.78, 8.48),
    "gemini-4-carbon/medium": (6.42, 8.92, 6.70, 5.42, 6.95, 28.39),
    "gemini-4-argon/high": (12.35, 13.30, 6.97, 6.69, 44.61, 10.39, 19.00, 9.98),  # the last two: repairs
}
# Longest gap between two thought chunks of one call.
MEASURED_STREAM_THOUGHT_GAP_S: dict[str, tuple[float, ...]] = {
    "gemini-4-hydrogen/low": (2.07, 2.14, 2.59, 2.24, 2.17),
    "gemini-4-neon/low": (3.92, 3.92, 4.42),
    "gemini-4-carbon/medium": (7.21, 6.94, 6.34, 6.92, 7.45, 6.76),
    "gemini-4-argon/high": (4.55, 4.31, 4.32, 5.11, 5.89, 9.77, 6.62),  # the last two: repairs
}
# Longest gap between two answer chunks once the answer flows (incremental answers and bursts). Not a safe limit on its
# own: an answer can also be held part-way (r5/live/stream_chair builder, chair brief, no thinking: 121 answer chunks to
# 34.3 s, 40.8 s of silence, then the other 142 and STOP at 75.2 s), so it is only the floor under the held-answer bound.
MEASURED_STREAM_ANSWER_GAP_S: dict[str, tuple[float, ...]] = {
    "gemini-4-hydrogen/low": (0.37, 0.52, 0.55, 0.52, 0.21, 0.64, 0.29, 0.49, 0.28, 0.36),
    "gemini-4-neon/low": (0.25, 0.44),
    "gemini-4-carbon/medium": (0.71, 0.67, 0.87, 0.56, 0.01, 0.01),
    "gemini-4-argon/high": (0.39, 0.38, 0.43, 0.33, 0.61, 0.17, 0.25),
}
# Answer tokens per second from the last thought chunk to the finish, the slowest propose of each model (repairs of 234
# tokens are all latency and say nothing about a rate). Measured: hydrogen 312-341, neon 155-156, carbon 91-112, argon
# 147-167. Not every answer streams as it is written: 3 of 8 carbon answers and both argon repairs were held after the
# last thought and sent at the end in under 2 s (carbon: 6,861 tokens after 75.1 s of silence, 6,717 after 67.6 s), so a
# silence after the last thought may be an answer being written.
MEASURED_ANSWER_TOKENS_PER_S: dict[str, float] = {
    "gemini-4-hydrogen/low": 312.0, "gemini-4-neon/low": 155.0, "gemini-4-carbon/medium": 91.0, "gemini-4-argon/high": 146.0}
# How much slower a slow day is: carbon's slowest call (285 s, MEASURED_PROPOSE_WALL_S) ran at about 70 output tokens/s
# against 94-97 on its other samples. A held answer is waited for at the slowest measured rate divided by this.
SLOW_DAY_FACTOR = 1.4
# Answer tokens of the crafted propose in the same sample, the largest per model: an answer that cannot fit the room
# thinking leaves under the shared cap ends MAX_TOKENS (stream_call progress reports the room as the call thinks).
MEASURED_STREAM_ANSWER_TOKENS: dict[str, tuple[int, ...]] = {
    "gemini-4-hydrogen/low": (7_345, 7_171, 7_684, 7_626, 7_171),
    "gemini-4-neon/low": (5_512, 6_926),
    "gemini-4-carbon/medium": (6_603, 6_861, 6_402, 7_145, 6_642, 6_717),
    "gemini-4-argon/high": (6_728, 6_640, 7_160, 6_926, 6_594),
}


def _model_key(role: str) -> str:
    mt = tier(role)  # type: ignore[arg-type]
    return f"{mt.model}/{mt.thinking.value if mt.thinking else 'none'}"


def _bound(table: dict[str, tuple[float, ...]], key: str) -> float:
    """The model's largest measured value; the largest of every model when this one was not measured."""
    values = table.get(key) or tuple(v for vs in table.values() for v in vs)
    return max(values)


def stream_limits(req: AuthorRequest, total_s: float) -> StreamLimits:
    """The silence limits of one authoring call. Rule (asserted in tests/test_stream_call.py): IDLE_HEADROOM x the largest
    measured value of the model (of every model, for one not measured), rounded up to 10 s, for the first chunk and,
    as floors, for the gaps between thought chunks and between answer chunks. Any silence after a chunk may be an answer
    being written and held (before it starts, and part-way through it), so the limit after a chunk is also long enough
    to write the rest of the answer budget (less the answer received; less again if the shared cap leaves less after
    the thinking and answer seen so far) at the model's slowest measured rate slowed by SLOW_DAY_FACTOR
    (stream_call.StreamLimits.silence_after). The answer budget is itself at least twice the largest measured answer
    (AUTHOR_OUTPUT_BUDGETS). Never above the total. A false stall costs a whole call; a late one costs only waiting,
    which the total bounded before."""
    key = _model_key(req.role)
    up = lambda s: float(min(total_s, 10 * math.ceil(IDLE_HEADROOM * s / 10)))  # noqa: E731
    rate = MEASURED_ANSWER_TOKENS_PER_S.get(key) or min(MEASURED_ANSWER_TOKENS_PER_S.values())
    return StreamLimits(first_chunk_s=up(_bound(MEASURED_STREAM_FIRST_CHUNK_S, key)),
                        answer_gap_s=up(_bound(MEASURED_STREAM_ANSWER_GAP_S, key)), total_s=total_s,
                        thinking_gap_s=up(_bound(MEASURED_STREAM_THOUGHT_GAP_S, key)),
                        held_answer_tokens=req.budget.answer_tokens, cap_tokens=req.max_output_tokens,
                        slowest_tokens_per_s=rate, headroom=SLOW_DAY_FACTOR)
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
    """A complete answer that does not parse or validate. Billed usage is booked. ``answer_text`` is the answer as
    written and ``cause`` the parse error (both None when the failure is not the answer's text)."""

    answer_text: str | None = None
    cause: BaseException | None = None


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


def propose_task(prompt: str, *, index: int, n: int, asset_name: str, references: bool, brief: str = "") -> str:
    """``brief``: constraints the answer must meet exactly, appended last (a modular kit piece's metric,
    ``kernel.kit_metric.context_brief``); with one the request is unchanged byte for byte. Without one (a prop) the task
    is followed by ``PROP_CRAFT_GUIDANCE``."""
    refs = "Use the attached references. " if references else ""
    task = (f"Build design {index + 1}/{n} for {prompt}. {refs}Return one JSON SceneIR with asset_name={asset_name!r}, "
            f"nodes and materials. Ground all parts. Include material assignments.")
    return f"{task} {brief}" if brief else f"{task}\n{PROP_CRAFT_GUIDANCE}"


def error_text(exc: BaseException) -> str:
    """One line per finding of a parse error: ``nodes.3.cylinder.repeat.offset: Field required (got ...)``."""
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return " ".join(str(exc).split())
    out = []
    for e in exc.errors(include_url=False):
        got = "" if e["type"] == "missing" else f" (got {json.dumps(e.get('input'), default=str)[:60]})"
        out.append(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}{got}")
    return "; ".join(out)


def correct_task(prompt: str, *, index: int, n: int, failed: dict[str, Any], error: BaseException) -> str:
    """The request to fix a design that did not read: the brief in one line, the exact messages, the failing nodes and
    materials as written (``ir_schema.located_nodes``), every node id, and the answer's shape. The whole answer is not
    sent: the author keeps what was right by not restating it."""
    nodes, mats = ir_schema.located_nodes(failed, error)
    ids = [n.get("id") for n in failed.get("nodes") or [] if isinstance(n, dict)]
    messages = error_text(error)
    parts = (f"Failing nodes as written: {json.dumps(nodes, separators=(',', ':'))}\n" if nodes else "") + \
            (f"Failing materials as written: {json.dumps(mats, separators=(',', ':'))}\n" if mats else "")
    return (f"Your design {index + 1}/{n} for {prompt} did not read. Errors: {messages[:2000]}\n{parts}"
            f"Node ids in the design: {json.dumps(ids)}\n"
            "Return one JSON object {\"nodes\": [...], \"materials\": [...], \"remove\": [...]} with only what must change: "
            "each node or material whole, with the id it replaces (or a new id to add one); remove lists ids to delete. "
            "Every part not listed stays as it is.")


@dataclass(frozen=True)
class DesignViews:
    """Renders of the current design that an author request carries (pass A): the marked views the judge's sheet is made
    of, one image each, and the legend that maps their numbered badges to node ids (``kernel.som.MarkedSheet``).
    ``images``: (label, uri) in the order they are attached; ``verdict``: what the judge said about this design, one
    line per score and per flagged question (``pyramid.verdict_lines``)."""

    images: tuple[tuple[str, str], ...]
    legend: str
    verdict: tuple[str, ...] = ()
    # what the author's last patch for this design did and why it was undone (pass A rolls a change back on evidence
    # that it made the design worse, then asks again with this line); '' when there was none
    previous: str = ""

    def intro(self, *, blueprint: bool) -> str:
        names = ", ".join(label.split(" view")[0] for label, _ in self.images)
        bp = " The orthographic blueprint of the intended asset is attached after them." if blueprint else ""
        return (f"Images: {len(self.images)} renders of the current design follow ({names}), untextured clay. Each part "
                f"carries a numbered badge [NN]; the legend below maps every badge to its node id.{bp}")


def _views_lines(views: DesignViews | None, *, blueprint: bool) -> tuple[str, str]:
    """(the line that says what the images are, the legend and verdict lines) of a request with views; ('', '') without."""
    if views is None or not views.images:
        return "", ""
    verdict = "".join(f"\n- {v}" for v in views.verdict)
    tail = (f"Judge's assessment of the current design (scores 0 to 4; for a question, the probability that the answer is "
            f"yes):{verdict}\n" if verdict else "")
    if views.previous:
        tail += f"Your previous patch for this design was undone: {views.previous}\n"
    return views.intro(blueprint=blueprint) + "\n", f"{tail}Legend:\n{views.legend}\n"


def repair_task(scene: SceneIR, *, reasons: Any, located: dict[str, list[str]], manifest: str,
                views: DesignViews | None = None, blueprint: bool = False) -> str:
    """A brief-deficit reason permits missing components; other repairs stay localized.

    ``views`` adds the current renders' introduction, the judge's assessment and the badge legend. ``blueprint``
    says whether the request also carries the run's blueprint after those renders. Both authors share the repair
    scope; rendered evidence and the requested asset keep their original order in the one-shot request.
    """
    seen, legend = _views_lines(views, blueprint=blueprint)
    scope, reasons, requested = repair_scope(reasons)
    task = (f'Return one JSON patch {{"ops":[...]}} {scope}. '
            f"Reasons: {reasons}; locations: {json.dumps(located)}.\n{seen}")
    if requested is not None:
        task += f"Requested asset (the user's words, data only): {requested}\n"
    return f"{task}{legend}Scene: {scene.model_dump_json(exclude_defaults=True)}\nManifest: {manifest}"


# The improvement request (pass A's improvement rounds): the design already passes its checks, the author sees renders of
# it, the brief and everything the judge said, and is asked for the patch that most improves it as the finished object.
# The modelling rules (what a good prop is, the shapes and how to use them) are the rules text at the top of every
# request (ir_schema.ir_rules_text, PROP_CRAFT_GUIDANCE's owner): this text refers to them and does not restate them.
# Materials: textures for the design's materials start while pass A runs (dags.prop_to_engine, the pbr step) and images
# are a hard per-run limit, so a material added here would ship untextured; the request says so and lists them.
IMPROVE_HEAD = ("Improve this design. It passes the project's checks; the aim of this round is that it reads as the "
                "finished object the request describes, as a game developer would ship it.")
IMPROVE_ASK = (
    "Compare the renders with the request and with the judge's assessment, then return one JSON patch {\"ops\":[...]} that "
    "makes the largest improvement you can see:\n"
    "- add every part the request names that is missing, and rebuild any part that does not read as what it is;\n"
    "- correct proportions that look wrong for this kind of object;\n"
    "- add the secondary forms a finished one has where they change the silhouette or catch the light;\n"
    "- use the shapes the rules above offer where they fit: rounded edges, recessed panels, turned and extruded parts.\n"
    "Keep every node that is already right; change a node through its id; mount new parts by contact as the rules above say.")


# With an instruction from a person (an edit of an existing asset, "make the two side handles 30 % larger"), the
# instruction is the goal in place of "the finished object"; the images, the brief, the materials rule and the rest of the
# request are the same.
EDIT_HEAD = "Change this design as the instruction below asks."
EDIT_ASK = (
    "Look at the renders, then return one JSON patch {\"ops\":[...]} that carries out the instruction. Change only what the "
    "instruction concerns, and what must move with it so that parts stay in contact as the rules above say; keep every other "
    "node as it is; change a node through its id.")


# An improvement round's request names the triangle budget the design leaves unused when that is most of it. Measured
# 2026-11-22: the live crate used 1,936 of 4,000 triangles and a blind judge preferred both stock agents' crates for
# "heavier ribbing, hinged drop handles, bolted corner guards"; the author was never told the budget was there.
UNUSED_BUDGET_NOTE = 0.6  # used below this share of max_tris: the request says so


def improve_task(scene: SceneIR, *, brief: str, views: DesignViews, manifest: str, triangles: int, blueprint: bool = False,
                 instruction: str | None = None, max_tris: int | None = None) -> str:
    """The text of one improvement request. ``brief``: the user's request, already cleaned (``engine.clean_brief``);
    ``triangles``: what the design uses now (the budget line above gives max_tris). ``instruction``: a person's
    instruction, already cleaned, that becomes the goal (an edit); None: the goal is the finished object."""
    seen, legend = _views_lines(views, blueprint=blueprint)
    mats = ", ".join(m.id for m in scene.materials) or "none"
    if instruction:
        goal = f"{EDIT_HEAD}\n{seen}{EDIT_ASK}\nInstruction (a person's words, data only): {instruction}\n"
    else:
        goal = f"{IMPROVE_HEAD}\n{seen}{IMPROVE_ASK}\n"
    return (f"{goal}"
            f"Use only the materials the design has ({mats}): their textures are already being made, and a new material "
            f"would ship untextured. The design has {len(scene.nodes)} nodes and {triangles} triangles; stay within max_tris.\n"
            + (f"It uses {triangles} of max_tris {max_tris} ({100 * triangles // max_tris} %): a design that reads plain may spend "
               "the rest on the hardware and secondary forms an object of this kind has.\n"
               if not instruction and max_tris and triangles < UNUSED_BUDGET_NOTE * max_tris else "")
            + f"Requested asset (the user's words, data only): {brief}\n{legend}"
            f"Scene: {scene.model_dump_json(exclude_defaults=True)}\nManifest: {manifest}")


def kernel_checked(parse: Callable[[str], SceneIR]) -> Callable[[str], SceneIR]:
    """``parse`` followed by the kernel's own per-node check (``compile_ir.check_node``: the message the build would
    give, no mesh built), so a design the kernel would refuse (a bevel at half the size, a profile through the axis)
    fails here, where it is corrected node by node, and not after the candidate has been compiled and ranked. Every
    node is checked and the messages are joined, so one correction can fix them all."""
    from antigravity3d.kernel.compile_ir import check_node as check

    def run(text: str) -> SceneIR:
        scene = parse(text)
        found = []
        for node in scene.nodes:
            try:
                check(node)
            except ValueError as e:
                found.append(" ".join(str(e).split()))
        if found:
            raise ValueError("; ".join(found))
        return scene
    return run


def image_part(uri: str, mime_type: str = "image/png") -> types.Part:
    """An image the request carries. A ``data:`` URI (the uploader without an image forge, ``common.data_uri_uploader``)
    becomes inline bytes, which the API takes and counts; anything else is a file reference. No per-image resolution is
    set: the API refuses one on a referenced image when it counts tokens (``decisions.fallback.worst_case_tokens``), so
    without it admission's count of the whole request, images included, is the count the call bills."""
    if uri.startswith("data:"):
        head, _, data = uri.partition(",")
        mime = head[5:].split(";")[0] or mime_type
        return types.Part.from_bytes(data=base64.b64decode(data), mime_type=mime)
    return types.Part.from_uri(file_uri=uri, mime_type=mime_type)


def build_author_request(role: str, operation: Operation, task: str, *, budget: EngineBudget, context: dict[str, Any],
                         timeout_s: float, asset_name: str | None = None,
                         output: OutputBudget | None = None, kit: bool = False,
                         images: Sequence[tuple[str, str]] = ()) -> AuthorRequest:
    """Contents + generation config for one authoring call. Needs no rates and dispatches nothing.

    ``context["art_direction"]``, when present, is the art direction's author brief: one line after the task.

    ``output`` overrides the table only for measurement (the benchmark's sizing probes); production never passes it.
    ``kit``: a modular kit piece's request, which reserves the kit row (``KIT_OUTPUT_BUDGETS``).
    ``images``: (label, uri) of renders of the current design (pass A's repair and improvement requests,
    ``DesignViews.images``), each attached after the text with its label and before the references; none: the request
    is the same byte for byte.
    """
    mt = tier(role)  # type: ignore[arg-type]
    out = output or output_budget(role, kit=kit)
    if operation == "correct" and output is None:
        out = OutputBudget(answer_tokens=CORRECTION_ANSWER_TOKENS, thinking_tokens=out.thinking_tokens)
    if operation == "look" and output is None:
        out = OutputBudget(answer_tokens=LOOK_ANSWER_TOKENS, thinking_tokens=out.thinking_tokens)
    limit = MODEL_OUTPUT_TOKEN_LIMIT.get(mt.model)
    if limit is not None and out.max_output_tokens > limit:
        raise ValueError(f"{role} {operation}: {out.max_output_tokens} output tokens exceed {mt.model}'s limit {limit}")
    if operation == "propose":
        schema, parse = ir_schema.scene_response_schema(asset_name), kernel_checked(ir_schema.parse_scene)
    elif operation in PATCH_OPERATIONS:
        schema, parse = ir_schema.patch_response_schema(), ir_schema.parse_patch
    elif operation == "correct":  # the caller binds the parse to the failed answer (MeteredAuthor._correct)
        schema, parse = ir_schema.correction_response_schema(), ir_schema.WireCorrection.model_validate_json
    elif operation == "look":  # the caller binds the parse to the scene (MeteredAuthor.look)
        schema, parse = ir_schema.look_response_schema(), ir_schema.WireLook.model_validate_json
    else:
        raise ValueError(f"unknown authoring operation {operation!r}")
    text = f"{ir_schema.ir_rules_text()}\nBudget: {json.dumps(asdict(budget))}\n{task}"
    if art := context.get("art_direction"):  # the art direction's author brief (art_direction.author_text); absent: nothing
        text += f"\n{art}"
    parts = [types.Part.from_text(text=text)]
    for label, uri in images:
        parts.extend([types.Part.from_text(text=label), image_part(uri)])
    for key, label in _REFERENCES:
        if uri := context.get(key):
            parts.extend([types.Part.from_text(text=label), image_part(uri)])
    http = types.HttpOptions(timeout=int(timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1))
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_json_schema=schema,
        max_output_tokens=out.max_output_tokens, candidate_count=1,
        thinking_config=types.ThinkingConfig(thinking_level=mt.thinking.value.upper()) if mt.thinking else None,
        http_options=http, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    return AuthorRequest(role, operation, mt.model, [types.Content(role="user", parts=parts)], config, out, schema, parse)


# A call that ends MAX_TOKENS having spent most of its output on thinking is asked again once at the next lower thinking
# level: the level is the only lever (live 2026-11-22: a thinking budget is refused together with a level, and ignored
# alone). Measured that night with the new scene language: the director truncated 5 of 9 first designs and the builder 2
# of 9, and a director repair of the hydrant ended at 32,768 output tokens with 27,223 of them thinking.
LESS_THINKING = {"HIGH": "MEDIUM", "MEDIUM": "LOW"}
LESS_THINKING_SHARE = 0.5  # thinking above this share of the output: the cap went to thinking, not to the answer


def _thinking_level(req: AuthorRequest) -> str | None:
    tc = getattr(req.config, "thinking_config", None)
    level = getattr(tc, "thinking_level", None) if tc is not None else None
    return _name(level).upper() or None if level is not None else None


def less_thinking(req: AuthorRequest, exc: AuthorOutcomeError) -> AuthorRequest | None:
    """The same request at the next lower thinking level, when ``exc`` is a cut-off answer whose output was mostly
    thinking and a lower level exists; else None."""
    level = _thinking_level(req)
    usage = exc.usage or {}
    thinking, output = usage.get("thinking_tokens") or 0, usage.get("output_tokens") or 0
    if level not in LESS_THINKING or not output or thinking <= LESS_THINKING_SHARE * output:
        return None
    config = req.config.model_copy(update={"thinking_config": types.ThinkingConfig(thinking_level=LESS_THINKING[level])})
    return replace(req, config=config)


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
    reported = getattr(u, "thoughts_token_count", None)
    thinking = reported or 0
    output = max(total - prompt, (getattr(u, "candidates_token_count", None) or 0) + thinking)
    out = {"input_tokens": prompt, "output_tokens": output, "thinking_tokens": thinking}
    if reported is None:  # the answer reported no thinking count: on a tier asked to think, it did not (output = total - prompt)
        # Seen live 2026-11-22: two of three gemini-4-carbon (MEDIUM) designs of one request had no thinking billed, took
        # 67-72 s against 223 s, and carried 79 and 32 Tier-1 warnings against 4. The receipt says so instead of a bare 0.
        out["thinking_reported"] = False
    return out


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
    ``craft``: a prop request (it carries ``PROP_CRAFT_GUIDANCE``) uses the samples of the request as it ships where the
    tier has them (five, hydrogen/low only) and the older tables otherwise; a kit piece (``craft=False``) is the request measured without it."""
    mt = tier(role)  # type: ignore[arg-type]
    key = f"{mt.model}/{mt.thinking.value if mt.thinking else 'none'}"
    answers, thoughts = MEASURED_ANSWER_TOKENS.get(key), MEASURED_THINKING_TOKENS.get(key)
    if craft and key in MEASURED_SHAPES_ANSWER_TOKENS:  # the request as it ships (round five's language)
        answers, thoughts = MEASURED_SHAPES_ANSWER_TOKENS[key], MEASURED_SHAPES_THINKING_TOKENS[key]
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
    # the answer did not read and one correction was asked for: "rescued: <first error>" or "failed: <both errors>"
    correction: str | None = None

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
        if self.correction is None:  # only for a candidate that needed a correction
            del d["correction"]
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


def _json_object(text: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _add_usage(a: dict[str, int] | None, b: dict[str, int] | None) -> dict[str, int] | None:
    if a is None or b is None:
        return a or b
    return {k: a.get(k, 0) + b.get(k, 0) for k in {*a, *b}}


def _one_line(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# Clients whose connection was found not to stream (see MeteredAuthor._generate), with the reason, so every later call of
# the run sends its request without streaming at once instead of first losing the first-chunk limit. Keyed by the client
# object, which the authors of one run share (AgentAuthor.bind); a client that cannot be weakly referenced is remembered
# by its author only.
_NOT_STREAMING: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()


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
                 record: Any = None, progress: Callable[[ModelCallProgress], None] | None = None) -> None:
        self.client, self.governor, self.budget = client, governor, budget
        # Receives a ModelCallProgress (stream_call) as each call goes (queued, sent, thinking, answering, done, retrying,
        # failed, reused). Called synchronously from the call's task; an exception it raises is dropped, never the call.
        # None: no events, nothing else changes.
        self.progress = progress
        # tactics.cached.DesignRecord: answered requests of earlier runs in the same --out, reused without a call (None: off)
        self.record = record
        self.context, self.timeout_s = dict(context), timeout_s
        # None: the shared schedule (service_answer.DEFAULT_POLICY, read at call time) with AUTHOR_UNKNOWN_RETRIES
        self.retry = retry
        # Retries of an attempt whose outcome is unknown; a caller with a recovery round of its own (a kit) sets 0.
        self.unknown_retries = AUTHOR_UNKNOWN_RETRIES
        self.last_service_retries: list[dict[str, Any]] = []  # the failed attempts of the latest repair (waits included)
        # Who answered each request this author made, for the package manifest's provenance (``_note_authored``)
        self.authored: list[dict[str, Any]] = []
        self._not_streaming: str | None = None  # why this author's connection does not stream (None: it streams)
        self._chunks_seen = False  # a chunk has arrived on a streamed call: this connection streams

    def not_streaming(self) -> str | None:
        """Why calls are sent without streaming on this run's connection, or None while it streams."""
        if self._not_streaming is None:
            try:
                self._not_streaming = _NOT_STREAMING.get(self.client)
            except TypeError:
                pass
        return self._not_streaming

    def _mark_not_streaming(self, reason: str) -> None:
        self._not_streaming = reason
        try:
            _NOT_STREAMING[self.client] = reason
        except TypeError:  # a client that cannot be weakly referenced (a test's namespace): this author remembers it
            pass

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

    def _emit(self, req: AuthorRequest, event: str, index: int | None, attempt: int = 1, state: StreamState | None = None,
              **extra: Any) -> None:
        if self.progress is None:
            return
        if state is not None:
            out = getattr(state.usage, "total_token_count", None), getattr(state.usage, "prompt_token_count", None)
            extra = dict(elapsed_s=round(state.elapsed(), 1), phase=state.phase, heading=state.heading or None,
                         thought_chars=state.thought_chars, answer_chars=state.answer_chars,
                         output_tokens=out[0] - out[1] if None not in out else None, **extra)
            if event == "silent":
                extra["silent_s"] = round(state.elapsed() - (state.last_chunk_s or 0.0), 1)
            room = self._answer_room(req, state)
            if room is not None:
                largest = max(MEASURED_STREAM_ANSWER_TOKENS.get(_model_key(req.role)) or (0,))
                extra.update(answer_room_tokens=room, room_short_of=largest if req.operation == "propose" and room < largest else None)
        try:
            self.progress(ModelCallProgress("author", event, req.model, req.role, req.operation, index, attempt, **extra))
        except Exception:  # noqa: BLE001 - a progress sink never costs the call it reports on
            pass

    @staticmethod
    def _answer_room(req: AuthorRequest, state: StreamState) -> int | None:
        """Output tokens the cap leaves for the answer after the thinking the running usage reports; None: not reported."""
        thinking = state.thinking_tokens()
        return None if thinking is None else req.max_output_tokens - thinking

    def _seen(self, req: AuthorRequest, state: StreamState) -> dict[str, Any]:
        seen = state.seen()
        if (room := self._answer_room(req, state)) is not None:
            seen["answer_room_tokens"] = room  # a MAX_TOKENS finish with little room left was decided by the thinking
        return seen

    async def _generate(self, req: AuthorRequest, trace: dict[str, Any] | None = None, *, index: int | None = None) -> Any:
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
        fell_back = False  # whether this call was already asked again without streaming
        waited = 0.0
        while True:
            receipt = dict(base_receipt)
            if tracker.attempts:  # a retried call: which attempt this is, and what came before it
                receipt.update(attempt=tracker.n, service_retries=tracker.record())
            t0 = time.monotonic()
            self._emit(req, "queued", index, tracker.n)
            try:
                # Waits for outstanding reservations to settle; fails at once only when this can never fit the ceiling.
                hold = await self.governor.reserve_wait(f"author.{req.operation}", limit, receipt=receipt)
            except BudgetExceeded as e:
                raise BudgetExceeded(f"plan {self.governor.plan.label!r}: {e}; request: {detail}") from e
            waited += time.monotonic() - t0
            trace.update(admission_wait_s=round(waited, 3), **{"reserved_tokens" if tokens else "reserved_usd": limit})
            sent = time.monotonic()
            # Streamed with thought summaries (stream_call); every attempt has its own limits from its own send. On a
            # connection found not to stream (below), the blocking call, with the request's whole deadline.
            state, attempt = StreamState(), tracker.n
            no_stream = self.not_streaming()
            try:
                if no_stream is None:
                    result = await stream_generate(
                        api, model=req.model, contents=req.contents, config=req.config, limits=stream_limits(req, deadline_s),
                        state=state, on_event=lambda event, st: event != "done" and self._emit(req, event, index, attempt, st))
                else:
                    state.sent_at = time.monotonic()
                    self._emit(req, "sent", index, attempt, state, detail=f"without streaming: {no_stream}")
                    result = await asyncio.wait_for(api.generate_content(model=req.model, contents=req.contents, config=req.config),
                                                    deadline_s)
            except BaseException as exc:
                elapsed = time.monotonic() - sent
                outcome = service_answer.classify(exc, elapsed, deadline_s=deadline_s)
                receipt["status"] = type(exc).__name__
                receipt["stream"] = self._seen(req, state) if no_stream is None else {"streamed": False, "why": no_stream}
                # A stream that never sent a chunk within the first-chunk limit has said nothing about the model: the path
                # may not deliver incrementally (a proxy that buffers the response; live 2026-11-22, the comparison
                # harness's relay: every streamed call stalled at 20 s with 0 chunks, where the blocking call had run in
                # round four). Once per call it is asked again without streaming, and the run remembers it; not when a
                # chunk has ever arrived on this author's calls (the connection streams; the model is what went silent).
                # A stream that stalls after chunks arrived keeps the behaviour above (one retry, streamed).
                self._chunks_seen = self._chunks_seen or state.chunks > 0
                fallback = (no_stream is None and not fell_back and not self._chunks_seen
                            and isinstance(exc, service_answer.StreamStalled) and state.chunks == 0)
                if fallback:
                    why = (f"no chunk in {state.limit_s:.0f} s: this connection does not stream; asked again without "
                           f"streaming")
                    receipt["stream_fallback"] = why
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
                if fallback:
                    fell_back = True
                    self._mark_not_streaming(f"no chunk arrived within {state.limit_s:.0f} s of a streamed call")
                    tracker.decide(exc, elapsed, outcome)  # recorded as a failed attempt; asked again at once, unstreamed
                    trace["service_retries"] = tracker.record()
                    self._emit(req, "retrying", index, attempt, state, detail=why, wait_s=0.0)
                    continue
                wait = tracker.decide(exc, elapsed, outcome) if outcome != "final" else None
                trace["service_retries"] = tracker.record()
                if wait is not None:
                    self._emit(req, "retrying", index, attempt, state, detail=f"{outcome}: {service_answer.describe(exc, 160)}",
                               wait_s=wait)
                    await tracker.policy.sleep(wait)
                    continue
                self._emit(req, "failed", index, attempt, state, detail=service_answer.describe(exc, 160))
                if is_timeout(exc):
                    stalled = f": {exc}" if isinstance(exc, service_answer.StreamStalled) else ""
                    raise AuthorTimedOut(
                        f"{req.role} {req.operation} on {req.model}: timed out after {elapsed:.0f}s of {deadline_s:.0f}s "
                        f"({type(exc).__name__}{stalled}); the whole reservation {self.governor.fmt(limit)} is retained"
                        + (f"; {tracker.summary()}" if len(tracker.attempts) > 1 else ""), finish_reason="TIMEOUT",
                        **_meta(req, None)) from exc
                if outcome == "final":
                    raise  # a client error (4xx): a bug in the request, not an outcome
                raise self._unavailable(req, exc, tracker, "generating", booked_any=booked_any) from exc
            break
        booked = None
        self._chunks_seen = self._chunks_seen or state.chunks > 0
        receipt["stream"] = self._seen(req, state) if no_stream is None else {"streamed": False, "why": no_stream}
        if state.broke_after_finish:  # (d): the answer is complete; its usage is known only if it came with the finish
            receipt["booked_reason"] = (f"the stream broke after the finish reason arrived ({state.broke_after_finish}): the "
                                        "answer is complete and used" + ("" if state.finish_carried_usage else
                                        "; its usage never arrived, so the whole reservation is kept"))
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
                invalid = AuthorInvalid(f"{req.role} {req.operation} on {req.model}: answer does not validate: "
                                        f"{_one_line(exc)}; usage {usage}", finish_reason=receipt["finish_reason"],
                                        **_meta(req, usage))
                invalid.answer_text, invalid.cause = text, exc
                raise invalid from exc
            receipt["status"] = "ok"
            self._emit(req, "done", index, attempt, state, finish_reason=receipt["finish_reason"])
            return parsed
        except BaseException as exc:
            receipt["status"] = type(exc).__name__
            self._emit(req, "failed", index, attempt, state, finish_reason=receipt.get("finish_reason"),
                       detail=f"{type(exc).__name__}: {_one_line(exc, 160)}")
            raise
        finally:
            receipt["usage_known"] = booked is not None
            hold.settle(booked, receipt=receipt)

    async def _answer(self, req: AuthorRequest, trace: dict[str, Any], *, index: int | None,
                      keep: Callable[[Any], bool] = lambda answer: True, lowered: bool = False) -> Any:
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
                self._emit(req, "reused", index)
                trace.update(recorded=hit.original, **{"reserved_tokens" if self.governor.unit == "tokens" else "reserved_usd": 0.0})
                self._note_authored(req, index, trace)  # the manifest's provenance: the same facts for a reuse as for a paid answer
                return hit.answer
        try:
            answer = await self._generate(req, trace, index=index)
        except AuthorTruncated as first:
            lower = None if lowered else less_thinking(req, first)  # once per call
            if lower is None:
                raise
            # Asked once more with the next lower thinking level: a different request, so it can succeed where the same
            # one would most likely run out the same way (see LESS_THINKING).
            trace["less_thinking"] = {"from": _thinking_level(req), "to": _thinking_level(lower), "first_usage": first.usage}
            self._emit(lower, "less_thinking", index)
            try:
                return await self._answer(lower, trace, index=index, keep=keep, lowered=True)
            except AuthorTruncated as second:
                raise AuthorTruncated(f"{second}; the first attempt at {_thinking_level(req)} thinking was cut off too ({first})",
                                      finish_reason=second.finish_reason, **_meta(lower, second.usage)) from first
        self._note_authored(req, index, trace)
        if key is not None and keep(answer):
            original = {"unit": self.governor.unit, "booked": trace.get("booked"), "usage": trace.get("usage"),
                        "model": req.model, "role": req.role, "operation": req.operation}
            await asyncio.shield(asyncio.to_thread(record.put, key, answer, original))  # a stop never leaves it half written
        return answer

    async def _record_rescued(self, asked: AuthorRequest, index: int | None, answer: Any, trace: dict[str, Any]) -> None:
        """Record an answer a correction rescued under the request that was asked, too (it is already under the
        correction's own key). Without this a rerun asks the same request, misses (only the correction was recorded), and
        pays for a new answer that differs, and every later request that carries the design then misses too: live
        2026-11-23, the comparison's repeat of the crate (r5/live_scorecard, r5_crate_C.repeat) reused nothing and paid
        298,912 tokens again. The cost recorded is both calls'."""
        record = self.record
        if record is None:
            return
        original = {"unit": self.governor.unit, "booked": trace.get("booked"), "usage": trace.get("usage"), "model": asked.model,
                    "role": asked.role, "operation": asked.operation, "rescued_by_correction": True}
        await asyncio.shield(asyncio.to_thread(record.put, record.key(asked, index), answer, original))

    def _note_authored(self, req: AuthorRequest, index: int | None, trace: dict[str, Any]) -> None:
        """Who answered this request, for the package manifest's provenance (``kernel.provenance``): identities and the
        request's hash, no time and no cost, so a reused answer and a paid one leave the same entry."""
        from antigravity3d.tactics.cached import request_digest

        facts = {"model": req.model, "role": req.role, "operation": req.operation, "index": index,
                 "request_sha256": request_digest(req, index, self.record.cache.upload_digest if self.record is not None else None)}
        trace["provenance"] = facts
        self.authored.append(facts)

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
                kit: bool | None = None, images: Sequence[tuple[str, str]] = ()) -> AuthorRequest:
        """``kit`` None: this author is bound to a kit piece when its context carries the kit metric."""
        kit = bool(context_brief(self.context)) if kit is None else kit
        return build_author_request(role, operation, task, budget=self.budget, context=self.context,
                                    timeout_s=self.timeout_for(role), asset_name=asset_name, kit=kit, images=images)

    @property
    def has_blueprint(self) -> bool:
        return bool(self.context.get("ortho_uri"))

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
            correction = None
            try:
                req = self.request(role, "propose", task, asset_name=asset, kit=bool(brief))
                try:
                    scene = await self._answer(req, trace, index=i, keep=lambda s: s.asset_name == asset and bool(s.nodes))
                except AuthorInvalid as first:
                    failed = _json_object(first.answer_text)
                    if failed is None or first.cause is None:
                        raise  # not JSON, or not the text's fault: nothing to correct
                    try:
                        scene = await self._correct(role, prompt, i, n, asset, failed, first, trace, kit=bool(brief))
                    except AuthorInvalid as again:
                        again.args = (f"{_one_line(first)}; correction: {_one_line(again)}",)
                        again.usage = trace.get("usage")  # both calls
                        correction = f"failed: {_one_line(first.cause, 200)}"
                        raise again from first
                    correction = f"rescued: {_one_line(first.cause, 200)}"
                    if scene.asset_name == asset and scene.nodes:
                        await self._record_rescued(req, i, scene, trace)
                if scene.asset_name != asset or not scene.nodes:
                    raise AuthorInvalid(f"{role} propose: candidate must contain nodes for the requested asset {asset!r}",
                                        finish_reason="STOP", **_meta(req, trace.get("usage")))
            except Exception as exc:  # noqa: BLE001 - classified and recorded; nothing is dropped unnamed
                status = outcome_status(exc)
                text = _one_line(exc) if status != "error" else f"{type(exc).__name__}: {_one_line(exc)}"
                return None, CandidateOutcome(i, status, text, getattr(exc, "finish_reason", None),
                                              getattr(exc, "usage", None) or trace.get("usage"),
                                              trace.get("admission_wait_s", 0.0), trace.get("reserved_usd"),
                                              trace.get("reserved_tokens"), trace.get("service_retries") or None,
                                              correction=correction), exc
            return scene, CandidateOutcome(i, "produced", "", "STOP", trace.get("usage"), trace.get("admission_wait_s", 0.0),
                                           trace.get("reserved_usd"), trace.get("reserved_tokens"),
                                           trace.get("service_retries") or None, trace.get("recorded"), correction), None

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

    async def _correct(self, role: str, prompt: str, index: int, n: int, asset: str, failed: dict[str, Any],
                       first: AuthorInvalid, trace: dict[str, Any], *, kit: bool) -> SceneIR:
        """One correction of a candidate whose answer did not read: the same role, only the failing parts asked for
        (``correct_task``), applied to the answer as written and read again with the same parse. Booked and recorded
        like any authoring call (ledger tag ``author.correct``); its usage is added to the candidate's. Only for a
        JSON answer that failed validation: a truncated, blocked or empty answer, or a timeout, never reaches here."""
        task = correct_task(prompt, index=index, n=n, failed=failed, error=first.cause)
        req = self.request(role, "correct", task, kit=kit)
        req = replace(req, parse=kernel_checked(lambda text: ir_schema.apply_correction(failed, text)))
        sub: dict[str, Any] = {}
        try:
            return await self._answer(req, sub, index=index, keep=lambda s: s.asset_name == asset and bool(s.nodes))
        finally:
            trace["usage"] = _add_usage(trace.get("usage") or first.usage, sub.get("usage"))
            for k in ("reserved_usd", "reserved_tokens", "booked"):
                if sub.get(k) is not None:
                    trace[k] = (trace.get(k) or 0.0) + sub[k]

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
                     role: str | None = None, views: DesignViews | None = None) -> IRPatch | None:
        """``role`` overrides the tier (the pyramid's one escalation to ``director``); it must be allowed by the plan.
        ``views``: renders of the current design to attach (the request's text then says what they are)."""
        role = role or self.governor.plan.builder_role
        if action.kind == "escalate_director":
            role = "director"
        if role == "director" and not self.governor.plan.allow_director:
            raise BudgetExceeded("director escalation is outside this asset's plan")
        task = repair_task(scene, reasons=action.reasons, located=located, manifest=manifest, views=views,
                           blueprint=self.has_blueprint)
        return await self._patch(self.request(role, "repair", task, images=views.images if views else ()))

    async def improve(self, scene: SceneIR, *, brief: str, views: DesignViews, manifest: str, triangles: int,
                      role: str | None = None, instruction: str | None = None) -> IRPatch | None:
        """One improvement request (pass A's improvement rounds, ``improve_task``) on ``role`` (default the plan's
        ``improve_role``, else its builder). None: the author proposed no change. Failures raise as a repair's do.
        ``instruction``: a person's instruction that becomes the goal (an edit; judging what comes back, and keeping
        or undoing it, is the caller's); it is cleaned as the brief is (``engine.clean_brief``)."""
        plan = self.governor.plan
        role = role or getattr(plan, "improve_role", None) or plan.builder_role
        if role == "director" and not plan.allow_director:
            raise BudgetExceeded("the director tier is outside this asset's plan")
        if instruction is not None:
            from antigravity3d.decisions.engine import clean_brief

            instruction = clean_brief(instruction, 2000) or None
        task = improve_task(scene, brief=brief, views=views, manifest=manifest, triangles=triangles, blueprint=self.has_blueprint,
                            instruction=instruction, max_tris=self.budget.max_tris)
        return await self._patch(self.request(role, "improve", task, images=views.images))

    async def _patch(self, req: AuthorRequest) -> IRPatch | None:
        trace: dict[str, Any] = {}
        self.last_service_retries = []
        try:
            patch = await self._answer(req, trace, index=None)
        finally:
            self.last_service_retries = list(trace.get("service_retries") or [])
        return patch if patch.ops else None

    async def look(self, scene: SceneIR, task: str, *, images: Sequence[tuple[str, str]], role: str | None = None,
                   correct: Callable[[str, BaseException], str] | None = None) -> IRPatch | None:
        """One look request (``tactics.look_round``): ``task`` with ``images`` (the renders), answered with material
        operations read against ``scene`` (``ir_schema.apply_look``). None: no change. An answer that does not read is
        corrected once (``correct(answer, error)`` gives the request text; ledger author.correct, the same schema), as a
        candidate design is; a second failure raises ``AuthorInvalid`` with both messages."""
        role = role or self.governor.plan.builder_role
        parse = lambda text: ir_schema.apply_look(scene, text)  # noqa: E731
        req = replace(self.request(role, "look", task, images=images), parse=parse)
        trace: dict[str, Any] = {}
        try:
            # "No change" (None) is recorded too: it was paid for, and a rerun asking the same request continues from it
            return await self._answer(req, trace, index=None)
        except AuthorInvalid as first:
            if correct is None or _json_object(first.answer_text) is None or first.cause is None:
                raise
            fix = replace(self.request(role, "look", correct(first.answer_text or "", first.cause), images=images),
                          parse=parse, operation="correct")
            sub: dict[str, Any] = {}
            try:
                patch = await self._answer(fix, sub, index=None)
            except AuthorInvalid as again:
                again.args = (f"{_one_line(first)}; correction: {_one_line(again)}",)
                raise
            # rescued ("no change" included): recorded under the look request too, at both calls' cost
            trace["usage"] = _add_usage(trace.get("usage") or first.usage, sub.get("usage"))
            trace["booked"] = (trace.get("booked") or 0.0) + (sub.get("booked") or 0.0)
            await self._record_rescued(req, None, patch, trace)
            return patch
