"""A streamed model call with the contract of a blocking one, limits on silence, and progress a person can watch.

:func:`stream_generate` sends a request with ``generate_content_stream`` and thought summaries, and returns one
``GenerateContentResponse`` that reads like the blocking answer: the answer text (thought text never enters it), the
finish reason, the final ``usage_metadata``, ``prompt_feedback`` and the candidate list. Callers keep their checks
(``metered_author.check_response`` / ``usage_tokens``) unchanged.

Why stream (measured live 2026-11-22, crate brief, r5/live/stream_probe and tier_crate): a blocking director call is
silent for 263 s and a blocking builder attempt was cut by the far end at 300.009 s with no response, its reservation
booked for nothing; streamed, the same requests send a chunk every 3.6-7.2 s while thinking.

Limits (:class:`StreamLimits`), by what has arrived, because the wire says which phase a call is in only by what it
has sent:

* nothing yet: ``first_chunk_s``;
* after any chunk: :meth:`StreamLimits.silence_after`, long enough to write the rest of the answer unseen. A model may
  hold its answer and send it later, before it starts (live 2026-11-22, stream_probe2 builder: last thought at 171.7 s,
  75.1 s of silence, then 6,861 answer tokens in 0.2 s) and part-way through it (stream_chair builder: 121 answer
  chunks to 34.3 s, 40.8 s of silence, then the other 142), so no phase of a stream has a short safe silence;
* always: ``total_s``, the request's whole deadline (a stream's httpx timeout is per read, not total).

A silence past its limit raises ``service_answer.StreamStalled`` (outcome "unknown": the attempt may have run, keeps
its reservation and may be retried); the total raises a plain ``TimeoutError`` (a spent deadline: "final").

A stream that breaks after the finish reason arrived returns the complete answer; ``state.broke_after_finish`` says
so, and the usage counts as unknown unless the chunk that carried the finish also carried the usage. A stream that
breaks earlier raises what broke it; a partial answer is never returned.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from google.genai import types

from antigravity3d import service_answer

log = logging.getLogger(__name__)

# Thought summaries are written for people: each starts with a bold heading line ("**Planning the latch**").
HEADING_CHARS = 120
# Progress events for a flowing answer at most this often (answer chunks arrive every 0.14-0.22 s: 260 per call).
ANSWER_EVENT_EVERY_S = 1.0
# While the wire is silent, a "silent" event this often, so a sink does not go quiet with the wire. Above the longest gap
# between thought chunks measured (7.21 s, carbon), so a call that is thinking normally does not produce them.
HEARTBEAT_S = 10.0
# Characters per answer token, above the measured 2.52-2.70 (SceneIR JSON, 2026-11-22: 16,979 / 6,728, 17,264 / 6,861,
# 18,094 / 6,699), for a stream without running usage.
CHARS_PER_TOKEN = 3.0


class StreamIncomplete(ConnectionError):
    """The stream ended without a finish reason or prompt feedback: the service closed it before the answer was
    complete. Treated as a broken connection (``service_answer.is_transport`` by its ``ConnectionError`` base, outcome
    "unknown": it may have run and keeps its reservation), never as an empty answer. ``seen``: what had arrived."""

    def __init__(self, message: str, *, seen: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.seen = dict(seen or {})


@dataclass(frozen=True)
class StreamLimits:
    """Seconds of silence allowed, and the whole deadline.

    After a chunk: ``max(floor, headroom * tokens / slowest_tokens_per_s)``, where ``tokens`` is the answer that may still
    be unwritten: ``held_answer_tokens`` less the answer already received, or less again when the running usage shows
    that thinking has used part of the shared cap (``cap_tokens - thinking - answer so far``); ``floor`` is
    ``thinking_gap_s`` before answer text and ``answer_gap_s`` after. ``slowest_tokens_per_s`` None: no rate known, the
    limit is the total."""

    first_chunk_s: float
    answer_gap_s: float
    total_s: float
    thinking_gap_s: float
    held_answer_tokens: int = 0
    cap_tokens: int | None = None
    slowest_tokens_per_s: float | None = None
    headroom: float = 1.0

    def silence_after(self, thinking_tokens_seen: int | None = None, answer_tokens_seen: int = 0,
                      answering: bool = False) -> float:
        if not self.slowest_tokens_per_s:
            return self.total_s
        tokens = self.held_answer_tokens - answer_tokens_seen
        if thinking_tokens_seen is not None and self.cap_tokens is not None:
            tokens = min(tokens, self.cap_tokens - thinking_tokens_seen - answer_tokens_seen)
        floor = self.answer_gap_s if answering else self.thinking_gap_s
        return min(self.total_s, max(floor, self.headroom * max(0, tokens) / self.slowest_tokens_per_s))


@dataclass
class StreamState:
    """What a stream delivered so far. Read by the caller for receipts and progress, also after a failure."""

    sent_at: float = 0.0  # loop time of the send
    chunks: int = 0
    first_chunk_s: float | None = None
    last_chunk_s: float | None = None
    first_answer_s: float | None = None
    last_thought_s: float | None = None
    longest_gap_s: float = 0.0
    thought_chars: int = 0
    answer_chars: int = 0
    heading: str = ""  # the latest thought summary's heading
    finish_reason: Any = None
    finish_carried_usage: bool = False
    usage: Any = None  # the latest usage_metadata seen (the final one once the stream ended)
    prompt_feedback: Any = None
    response_id: str | None = None
    candidate_seen: bool = False
    broke_after_finish: str | None = None  # what broke the stream after the finish reason arrived
    limit_s: float = 0.0  # the silence limit in force
    phase: str = "waiting"  # waiting | thinking | answering | done
    answer: list[str] = field(default_factory=list, repr=False)

    def elapsed(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.sent_at

    def seen(self, now: float | None = None) -> dict[str, Any]:
        """For a receipt: what arrived, and how long the wire has been silent."""
        now = time.monotonic() if now is None else now
        last = self.last_chunk_s if self.last_chunk_s is not None else 0.0
        out: dict[str, Any] = {"phase": self.phase, "chunks": self.chunks, "first_chunk_s": _r(self.first_chunk_s),
                               "first_answer_s": _r(self.first_answer_s), "silent_s": _r(self.elapsed(now) - last),
                               "longest_gap_s": _r(self.longest_gap_s), "thought_chars": self.thought_chars,
                               "answer_chars": self.answer_chars, "limit_s": _r(self.limit_s)}
        if self.usage is not None:
            out["usage_seen"] = {k: getattr(self.usage, k, None) for k in
                                 ("prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count")}
        if self.broke_after_finish:
            out["broke_after_finish"] = self.broke_after_finish
        return out

    def answer_tokens(self) -> int:
        """Answer tokens received: the running usage's count, else estimated from the characters at CHARS_PER_TOKEN
        (an over-estimate of characters per token counts fewer tokens received, so the limit errs long)."""
        n = getattr(self.usage, "candidates_token_count", None) if self.usage is not None else None
        return n if isinstance(n, int) else math.floor(self.answer_chars / CHARS_PER_TOKEN)

    def thinking_tokens(self) -> int | None:
        n = getattr(self.usage, "thoughts_token_count", None) if self.usage is not None else None
        return n if isinstance(n, int) else None


@dataclass(frozen=True)
class ModelCallProgress:
    """One progress event of a model call, as plain data: :meth:`line` for a terminal, :meth:`to_dict` for a log or an
    MCP ``notifications/progress`` message (``progress`` = ``elapsed_s``, which only grows within an attempt;
    ``message`` = ``line()``).

    ``kind``: author | judge | image (the call's plane: the author streams, the judge reports start and end). ``event``:
    queued (waiting for budget admission), sent, thinking (a thought summary arrived; ``heading`` is its first line),
    silent (no chunk for a while: ``phase`` says whether before any chunk, while thinking, or writing an answer the
    model holds back), answering (answer text arriving; ``answer_chars``), done (``finish_reason``, ``output_tokens``),
    retrying (``detail``: why; ``wait_s``), failed (``detail``), reused (answered from the design record, no call)."""

    kind: str
    event: str
    model: str
    role: str | None = None
    operation: str | None = None
    index: int | None = None  # the candidate, for a call that is one of several
    attempt: int = 1
    elapsed_s: float = 0.0  # since this attempt was sent
    phase: str | None = None
    heading: str | None = None
    thought_chars: int = 0
    answer_chars: int = 0
    output_tokens: int | None = None
    finish_reason: str | None = None
    detail: str | None = None
    wait_s: float | None = None
    silent_s: float | None = None  # seconds since the last chunk (a "silent" event)
    # Output tokens the shared cap still leaves for the answer after the thinking reported so far (running usage).
    answer_room_tokens: int | None = None
    # The largest answer measured for this model, when the room left is smaller: the call may end MAX_TOKENS. Not a
    # certainty (answers vary), so it is reported, never acted on.
    room_short_of: int | None = None
    # Tells concurrent calls apart (a judge's calls share an operation name); None: role, operation and index do.
    call: str | None = None

    def key(self) -> tuple[Any, ...]:
        """The call this event belongs to, for a view that shows each call in flight once."""
        return (self.kind, self.call) if self.call is not None else (self.kind, self.role, self.operation, self.index)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    def line(self) -> str:
        who = " ".join(str(x) for x in (self.kind, self.role, self.operation) if x)
        if self.index is not None:
            who += f" #{self.index + 1}"
        if self.attempt > 1:
            who += f" (attempt {self.attempt})"
        t = f"{self.elapsed_s:5.0f}s"
        if self.event == "queued":
            what = "waiting for budget admission"
        elif self.event == "sent":
            what = f"sent to {self.model}" + (f" ({self.detail})" if self.detail else "")
        elif self.event == "thinking":
            what = f"thinking: {self.heading}" if self.heading else "thinking"
            if self.room_short_of is not None:
                what += (f" [only {self.answer_room_tokens:,} output tokens left for the answer; answers measured up to "
                         f"{self.room_short_of:,}: it may be cut off]")
        elif self.event == "silent":
            quiet = f"{self.silent_s:.0f} s" if self.silent_s is not None else "a while"
            # The wire cannot tell thinking from an answer the model writes and sends at the end, so this says both.
            what = {"waiting": f"waiting for the first reply ({quiet})",
                    "thinking": f"no new thought for {quiet}: still thinking, or writing the answer, which some calls send at the end",
                    "answering": f"answer paused for {quiet}: some calls write the rest before sending it"}.get(self.phase or "", f"silent for {quiet}")
        elif self.event == "answering":
            what = f"answer arriving: {self.answer_chars:,} characters"
        elif self.event == "done":
            what = (f"finished {self.finish_reason or ''}".rstrip() + (f", {self.output_tokens:,} output tokens" if self.output_tokens else "")
                    + (f" ({self.detail})" if self.detail else ""))
        elif self.event == "retrying":
            what = f"retrying in {self.wait_s:.0f} s: {self.detail}" if self.wait_s is not None else f"retrying: {self.detail}"
        elif self.event == "reused":
            what = "answer taken from the design record, no call made"
        else:
            what = f"{self.event}: {self.detail}" if self.detail else self.event
        return f"[{who}] {t} {what}"


# The progress sink of the work running in this context, for calls whose owner has no sink of its own: the command sets it
# for its run, the MCP server for each tool call (so two sessions' calls never report into each other's client).
CURRENT_SINK: ContextVar[Callable[[ModelCallProgress], None] | None] = ContextVar("agy3d_progress_sink", default=None)


def emit(event: ModelCallProgress, sink: Callable[[ModelCallProgress], None] | None = None) -> None:
    """Hand ``event`` to ``sink``, else to the context's sink. A sink that raises is dropped for this event: progress never
    costs the call it reports on."""
    target = sink or CURRENT_SINK.get()
    if target is None:
        return
    try:
        target(event)
    except Exception:  # noqa: BLE001
        log.debug("progress sink failed on %s", event.event, exc_info=True)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def heading_of(text: str) -> str | None:
    """The heading of a thought summary: its first line when that line is a markdown heading or bold line; None for a
    part that continues the previous summary."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("**", "#")):
            line = line.strip("#* ").strip()
            return line[:HEADING_CHARS] or None
        return None
    return None


def with_thought_summaries(config: types.GenerateContentConfig) -> types.GenerateContentConfig:
    """The config sent: the request's own, asking for thought summaries at the request's thinking level. Added here and
    not in the request, so the request (and with it the design record's key, ``tactics.cached.DesignRecord``) is the
    same whether or not summaries are asked for: they change what is reported while the call runs, not what is asked."""
    tc = config.thinking_config
    if tc is None:
        return config
    return config.model_copy(update={"thinking_config": tc.model_copy(update={"include_thoughts": True})})


def _final(state: StreamState) -> types.GenerateContentResponse:
    candidates = []
    if state.candidate_seen:
        answer = "".join(state.answer)
        content = types.Content(role="model", parts=[types.Part(text=answer)] if answer else None)
        candidates = [types.Candidate(content=content, finish_reason=state.finish_reason, index=0)]
    usage = state.usage if (state.broke_after_finish is None or state.finish_carried_usage) else None
    return types.GenerateContentResponse(candidates=candidates, usage_metadata=usage, prompt_feedback=state.prompt_feedback,
                                         response_id=state.response_id)


def _emit(on_event: Callable[[str, StreamState], None] | None, event: str, state: StreamState) -> None:
    if on_event is None:
        return
    try:
        on_event(event, state)
    except Exception:  # noqa: BLE001 - a progress sink never costs the call it reports on
        log.debug("progress sink failed on %s", event, exc_info=True)


async def stream_generate(api: Any, *, model: str, contents: Any, config: types.GenerateContentConfig, limits: StreamLimits,
                          state: StreamState | None = None,
                          on_event: Callable[[str, StreamState], None] | None = None) -> types.GenerateContentResponse:
    """One streamed attempt. ``api``: ``client.aio.models``. ``state`` (optional) is filled as chunks arrive, so the
    caller can read what was seen when this raises. ``on_event(event, state)``: "sent", "thinking" (a thought chunk),
    "answering" (answer text; at most every ``ANSWER_EVENT_EVERY_S``), "silent" (no chunk for ``HEARTBEAT_S``, then
    every ``HEARTBEAT_S``) and "done". Cancellation closes the stream (the cancel is delivered into the SDK's
    generators, which close the HTTP response) and propagates."""
    state = StreamState() if state is None else state
    loop = asyncio.get_running_loop()
    state.sent_at = time.monotonic()
    start = loop.time()
    total_at = start + limits.total_s
    state.limit_s = limits.first_chunk_s
    stream = None
    last_answer_event = -math.inf
    heartbeat: asyncio.TimerHandle | None = None

    def beat() -> None:
        nonlocal heartbeat
        _emit(on_event, "silent", state)
        heartbeat = loop.call_later(HEARTBEAT_S, beat)

    def rearm() -> None:
        nonlocal heartbeat
        if heartbeat is not None:
            heartbeat.cancel()
        heartbeat = loop.call_later(HEARTBEAT_S, beat) if on_event is not None else None

    _emit(on_event, "sent", state)
    rearm()
    cm: asyncio.Timeout | None = None
    try:
        async with asyncio.timeout_at(min(start + limits.first_chunk_s, total_at)) as cm:
            try:
                stream = await api.generate_content_stream(model=model, contents=contents, config=with_thought_summaries(config))
                async for chunk in stream:
                    now = loop.time()
                    t = now - start
                    gap = t - (state.last_chunk_s if state.last_chunk_s is not None else 0.0)
                    state.longest_gap_s = max(state.longest_gap_s, gap)
                    state.chunks += 1
                    state.last_chunk_s = t
                    if state.first_chunk_s is None:
                        state.first_chunk_s = t
                    thought = answer = 0
                    for cand in (chunk.candidates or [])[:1]:  # candidate_count is 1
                        state.candidate_seen = True
                        for part in (cand.content.parts if cand.content and cand.content.parts else []):
                            if not part.text:
                                continue
                            if part.thought:
                                thought += len(part.text)
                                if (h := heading_of(part.text)) is not None:
                                    state.heading = h
                            else:
                                answer += len(part.text)
                                state.answer.append(part.text)
                        if cand.finish_reason is not None:
                            state.finish_reason = cand.finish_reason
                            state.finish_carried_usage = chunk.usage_metadata is not None
                    if chunk.usage_metadata is not None:
                        state.usage = chunk.usage_metadata
                    if chunk.prompt_feedback is not None:
                        state.prompt_feedback = chunk.prompt_feedback
                    state.response_id = getattr(chunk, "response_id", None) or state.response_id
                    state.thought_chars += thought
                    state.answer_chars += answer
                    if answer:
                        if state.first_answer_s is None:
                            state.first_answer_s = t
                        state.phase = "answering"
                        state.limit_s = limits.silence_after(state.thinking_tokens(), state.answer_tokens(), True)
                    elif state.first_answer_s is None:
                        state.phase = "thinking"
                        if thought:
                            state.last_thought_s = t
                        state.limit_s = limits.silence_after(state.thinking_tokens())
                    cm.reschedule(min(now + state.limit_s, total_at))
                    rearm()
                    if thought:
                        _emit(on_event, "thinking", state)
                    if answer and now - last_answer_event >= ANSWER_EVENT_EVERY_S:
                        last_answer_event = now
                        _emit(on_event, "answering", state)
            except Exception as exc:
                if state.finish_reason is None or isinstance(exc, asyncio.CancelledError):
                    raise
                # (d) the answer is complete and was paid for: keep it; the usage is known only if it came with it
                state.broke_after_finish = service_answer.describe(exc)
    except TimeoutError:
        if cm is None or not cm.expired():  # a timeout raised by the call itself, not by these limits
            raise
        now = loop.time()
        if state.finish_reason is not None:  # silent after the finish: complete, as in (d)
            state.broke_after_finish = f"no end of stream {now - start - (state.last_chunk_s or 0):.0f} s after the finish"
        elif now >= total_at - 1e-3:
            raise TimeoutError(f"stream ran out its whole deadline of {limits.total_s:.0f} s ({state.phase}, "
                               f"{state.chunks} chunks)") from None
        else:
            silent = now - start - (state.last_chunk_s or 0.0)
            raise service_answer.StreamStalled(
                f"stream silent for {silent:.1f} s while {state.phase} (limit {state.limit_s:.1f} s; {state.chunks} chunks, "
                f"{state.thought_chars} thought and {state.answer_chars} answer characters received)",
                seen=state.seen()) from None
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()  # a no-op once the generator has ended; closes it when we stopped early
            except Exception:  # noqa: BLE001 - closing a broken stream may raise what broke it
                pass
    if state.finish_reason is None and state.prompt_feedback is None:
        raise StreamIncomplete(f"stream ended without a finish reason ({state.phase}, {state.chunks} chunks, "
                               f"{state.thought_chars} thought and {state.answer_chars} answer characters)", seen=state.seen())
    state.phase = "done"
    _emit(on_event, "done", state)
    return _final(state)
