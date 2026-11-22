"""Stand-in streams for tests whose fake models endpoints script a blocking answer.

``MeteredAuthor`` sends every authoring call with ``generate_content_stream`` (``tactics.stream_call``). The fakes in
the author tests script their answers as a blocking response (``generate_content`` returning text, candidates with a
finish reason, usage metadata, prompt feedback) because that is the shape their assertions are written against.
:class:`StreamFromBlocking` gives such a fake the streaming method: it asks the fake's own ``generate_content`` for
the scripted answer (so everything the fake records and raises stays as it was) and delivers it the way the live
service does (r5/live/stream_probe, 2026-11-22): thought-summary chunks, the answer in several chunks, and a last chunk
that carries the finish reason and the usage. An exception the fake raises surfaces where the real SDK raises a
rejected request: when the stream is requested.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from google.genai import types

THOUGHT = "**Laying out the parts**\n\nWorking out which parts the brief names and where each one is mounted."
_USAGE_FIELDS = ("prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count")


def _usage(u: Any) -> types.GenerateContentResponseUsageMetadata | None:
    if u is None:
        return None
    if isinstance(u, types.GenerateContentResponseUsageMetadata):
        return u
    return types.GenerateContentResponseUsageMetadata(**{k: getattr(u, k, None) for k in _USAGE_FIELDS})


def _feedback(f: Any) -> types.GenerateContentResponsePromptFeedback | None:
    if f is None or isinstance(f, types.GenerateContentResponsePromptFeedback):
        return f
    return types.GenerateContentResponsePromptFeedback(block_reason=getattr(f, "block_reason", None))


def chunk(*, thought: str | None = None, answer: str | None = None, finish: Any = None, usage: Any = None,
          feedback: Any = None, candidate: bool = True) -> types.GenerateContentResponse:
    """One streamed chunk as the SDK yields it."""
    parts = ([types.Part(text=thought, thought=True)] if thought else []) + ([types.Part(text=answer)] if answer else [])
    cands = [types.Candidate(content=types.Content(role="model", parts=parts or None), finish_reason=finish, index=0)] if candidate else None
    return types.GenerateContentResponse(candidates=cands, usage_metadata=_usage(usage), prompt_feedback=_feedback(feedback))


def chunks_of(response: Any, *, pieces: int = 3) -> list[types.GenerateContentResponse]:
    """A blocking-shaped response as the chunks a stream delivers it in."""
    candidates = getattr(response, "candidates", None) or []
    usage, feedback = getattr(response, "usage_metadata", None), getattr(response, "prompt_feedback", None)
    if not candidates:
        return [chunk(feedback=feedback, usage=usage, candidate=False)]
    text = getattr(response, "text", None) or ""
    step = max(1, -(-len(text) // pieces))
    out = [chunk(thought=THOUGHT)]
    out += [chunk(answer=text[i:i + step]) for i in range(0, len(text), step)]
    out.append(chunk(finish=getattr(candidates[0], "finish_reason", None), usage=usage, feedback=feedback))
    return out


async def replay(chunks: list[types.GenerateContentResponse]) -> AsyncIterator[types.GenerateContentResponse]:
    for c in chunks:
        yield c


class StreamFromBlocking:
    """Mixin for a fake models endpoint that defines ``generate_content``: adds ``generate_content_stream``."""

    async def generate_content_stream(self, **kwargs: Any) -> AsyncIterator[types.GenerateContentResponse]:
        response = await self.generate_content(**kwargs)  # type: ignore[attr-defined]
        return replay(chunks_of(response))
