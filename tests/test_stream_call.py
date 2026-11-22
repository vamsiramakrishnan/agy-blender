"""The streamed authoring call (tactics/stream_call.py, MeteredAuthor._generate): same contract as the blocking call,
limits on silence, what each kind of broken stream books, cancellation, and progress events.

Recorded streams (tests/data/stream_probe, live 2026-11-22) are replayed with their timing scaled down; failures are
scripted fakes, and the last tests drive the real SDK against a local server that streams server-sent events.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google import genai
from google.genai import types

from antigravity3d import service_answer
from antigravity3d.ir import SceneIR
from antigravity3d.state import EngineBudget
from antigravity3d.tactics import metered_author as M
from antigravity3d.tactics.governor import Governor, plan_for
from antigravity3d.tactics.stream_call import (ModelCallProgress, StreamLimits, StreamState, heading_of, stream_generate,
                                               with_thought_summaries)
from stream_fakes import chunk

DATA = Path(__file__).parent / "data" / "stream_probe"
ANSWER = (DATA / "answer_director.json").read_text()
USAGE = types.GenerateContentResponseUsageMetadata(prompt_token_count=6294, candidates_token_count=6728,
                                                   thoughts_token_count=18042, total_token_count=31064)


async def no_sleep(_s: float) -> None:
    return None


# ---------------------------------------------------------------------------------------------- replayed live streams


def recorded(name: str, scale: float, answer: str = ANSWER) -> list[tuple[float, types.GenerateContentResponse]]:
    """(t, chunk) of a recorded stream, time multiplied by ``scale``: thought chunks carry a summary of the recorded
    size (a heading line, then text), answer chunks the answer cut to the recorded sizes; the last chunk carries the
    finish and the usage, as the live stream's did."""
    rec = json.loads((DATA / name).read_text())
    out, pos = [], 0
    for k, (t, th, an) in enumerate(rec["chunks"]):
        last = k == len(rec["chunks"]) - 1
        thought = (f"**Step {k + 1}**\n\n" + "x" * th)[:th] if th else None
        piece = answer[pos:pos + an] if not last else answer[pos:]
        pos += len(piece)
        out.append((t * scale, chunk(thought=thought, answer=piece or None, finish="STOP" if last else None,
                                     usage=USAGE if last else None)))
    return out


async def timed(script):
    """An async iterator that yields each (t, item) at time t after it is first iterated; an exception item is raised,
    and the item "hang" waits forever."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for t, item in script:
        await asyncio.sleep(max(0.0, t0 + t - loop.time()))
        if item == "hang":
            await asyncio.Event().wait()
        if isinstance(item, BaseException):
            raise item
        yield item


class Api:
    """``client.aio.models`` for stream_generate: each call takes the next script."""

    def __init__(self, *scripts):
        self.scripts, self.sent, self.closed = list(scripts), [], 0

    async def generate_content_stream(self, **kw):
        self.sent.append(kw)
        script = self.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        api = self

        async def gen():
            try:
                async for item in timed(script):
                    yield item
            finally:
                api.closed += 1

        return gen()

    async def count_tokens(self, **_):
        return SimpleNamespace(total_tokens=6294)


def director_request(timeout_s: float = 540.0):
    return M.build_author_request("director", "propose", M.propose_task("crate", index=0, n=1, asset_name="RuggedMilitarySupply",
                                                                       references=False),
                                  budget=EngineBudget("godot_4", max_tris=3000), context={}, timeout_s=timeout_s,
                                  asset_name="RuggedMilitarySupply")


def scaled(limits: StreamLimits, scale: float) -> StreamLimits:
    return StreamLimits(first_chunk_s=limits.first_chunk_s * scale, answer_gap_s=limits.answer_gap_s * scale,
                        total_s=limits.total_s * scale, thinking_gap_s=limits.thinking_gap_s * scale,
                        held_answer_tokens=limits.held_answer_tokens, cap_tokens=limits.cap_tokens,
                        slowest_tokens_per_s=limits.slowest_tokens_per_s / scale if limits.slowest_tokens_per_s else None,
                        headroom=limits.headroom)


CHAIR = (DATA / "answer_builder_chair.json").read_text()


@pytest.mark.parametrize("name, role, answer", [("director_incremental.json", "director", ANSWER), ("builder_held.json", "builder", ANSWER),
                                                ("builder_held_midanswer.json", "builder", CHAIR)])
async def test_the_recorded_live_streams_finish_within_the_shipped_limits(name, role, answer):
    """The shipped limits, scaled with the recording, let every live call finish: the director's answer streamed as it
    was written; a builder answer held for 75.1 s after its last thought and then sent in 0.2 s; and a builder answer
    (no thinking) held for 40.8 s part-way through, after 121 of its 263 chunks."""
    scale = 1 / 400
    req = M.build_author_request(role, "propose", "x", budget=EngineBudget("godot_4"), context={}, timeout_s=M.author_timeout(role))
    limits = scaled(M.stream_limits(req, M.author_timeout(role)), scale)
    api, state, events = Api(recorded(name, scale, answer)), StreamState(), []
    resp = await stream_generate(api, model=req.model, contents=req.contents, config=req.config, limits=limits, state=state,
                                 on_event=lambda e, s: events.append((e, s.heading)))
    assert resp.text == answer and SceneIR.model_validate_json(resp.text).nodes  # thought text never enters the answer
    assert M.usage_tokens(resp) == {"input_tokens": 6294, "output_tokens": 31064 - 6294, "thinking_tokens": 18042}
    assert M.check_response(req, resp, M.usage_tokens(resp)) == answer
    assert api.sent[0]["config"].thinking_config.include_thoughts is True and api.closed == 1
    rec = json.loads((DATA / name).read_text())
    assert state.chunks == len(rec["chunks"]) and state.thought_chars == sum(c[1] for c in rec["chunks"])
    kinds = [e for e, _ in events]
    assert kinds[0] == "sent" and kinds[-1] == "done" and "answering" in kinds
    assert state.thought_chars == 0 or ("thinking", "Step 1") in events


def test_the_shipped_limits_follow_their_rule():
    """IDLE_HEADROOM x the largest measured value, rounded up to 10 s; before the answer, room to write a held answer
    of the answer budget at the slowest measured rate slowed by SLOW_DAY_FACTOR; never above the total."""
    for role in ("worker", "bulk", "builder", "director"):
        req = M.build_author_request(role, "propose", "x", budget=EngineBudget("godot_4"), context={}, timeout_s=M.author_timeout(role))
        total = M.author_timeout(role)
        lim = M.stream_limits(req, total)
        key = M._model_key(role)
        for got, table in ((lim.first_chunk_s, M.MEASURED_STREAM_FIRST_CHUNK_S), (lim.thinking_gap_s, M.MEASURED_STREAM_THOUGHT_GAP_S),
                           (lim.answer_gap_s, M.MEASURED_STREAM_ANSWER_GAP_S)):
            measured = max(table.get(key) or [v for vs in table.values() for v in vs])
            assert got == min(total, 10 * -(-M.IDLE_HEADROOM * measured // 10)) and got >= M.IDLE_HEADROOM * measured or got == total
        rate = M.MEASURED_ANSWER_TOKENS_PER_S.get(key) or min(M.MEASURED_ANSWER_TOKENS_PER_S.values())
        assert lim.silence_after() >= min(total, M.SLOW_DAY_FACTOR * req.budget.answer_tokens / rate)
        # long enough for every held answer measured: 75.1 s after a last thought, and 40.8 s after about 3,100 of
        # 6,699 answer tokens had arrived (carbon)
        assert lim.silence_after() >= 75.1 or role in ("worker", "bulk")
        assert lim.silence_after(None, 3_100, answering=True) >= 40.8 or role in ("worker", "bulk")
        assert lim.silence_after() <= total and lim.first_chunk_s < total
        # the bound shrinks as the answer arrives, never below the floor
        assert lim.silence_after(None, req.budget.answer_tokens, answering=True) == lim.answer_gap_s


def test_thought_headings_and_summaries_are_added_at_send_time_only():
    assert heading_of("**Planning the latch**\n\nI will ...") == "Planning the latch"
    assert heading_of("## Checking contact\nbody") == "Checking contact"
    assert heading_of("continuing the previous summary") is None
    req = director_request()
    sent = with_thought_summaries(req.config)
    assert sent.thinking_config.include_thoughts is True and sent.thinking_config.thinking_level == req.config.thinking_config.thinking_level
    assert req.config.thinking_config.include_thoughts is None  # the request (and the design record's key) is unchanged
    assert with_thought_summaries(sent) == sent


# ---------------------------------------------------------------------------------------------- the phases' limits

# held_answer_tokens=0: before the answer the limit is the thinking-gap floor (no held answer to wait for)
LIM = StreamLimits(first_chunk_s=0.3, answer_gap_s=0.2, total_s=3.0, thinking_gap_s=0.3, held_answer_tokens=0, slowest_tokens_per_s=1.0)
THOUGHT = chunk(thought="**Thinking about it**\nmore")
PART = chunk(answer='{"asset_name":')


async def run(script, limits=LIM):
    api, state = Api(script), StreamState()
    try:
        return await stream_generate(api, model="m", contents=[], config=director_request().config, limits=limits, state=state), state, api
    except BaseException as exc:
        exc.state, exc.api = state, api  # type: ignore[attr-defined]
        raise


async def test_silence_before_the_first_chunk_is_a_stall():
    with pytest.raises(service_answer.StreamStalled) as e:
        await run([(0.0, "hang")])
    assert e.value.seen["phase"] == "waiting" and e.value.seen["chunks"] == 0 and e.value.seen["silent_s"] >= 0.3
    assert e.value.api.closed == 1  # the stream was closed
    assert service_answer.classify(e.value, 0.3, deadline_s=540) == "unknown" and service_answer.is_timeout(e.value)


async def test_silence_while_thinking_is_a_stall_with_what_arrived():
    with pytest.raises(service_answer.StreamStalled) as e:
        await run([(0.0, THOUGHT), (0.1, THOUGHT), (0.1, "hang")])
    seen = e.value.seen
    assert seen["phase"] == "thinking" and seen["chunks"] == 2 and seen["thought_chars"] == 2 * len("**Thinking about it**\nmore")
    assert seen["answer_chars"] == 0 and seen["first_chunk_s"] < 0.05 and seen["limit_s"] == pytest.approx(0.3)


async def test_silence_once_the_answer_flows_with_no_answer_left_to_write_uses_the_floor():
    with pytest.raises(service_answer.StreamStalled) as e:
        await run([(0.0, THOUGHT), (0.0, PART), (0.0, "hang")], StreamLimits(first_chunk_s=1, answer_gap_s=0.1, total_s=3, thinking_gap_s=1,
                                                                            held_answer_tokens=0, slowest_tokens_per_s=1.0))
    assert e.value.seen["phase"] == "answering" and e.value.seen["limit_s"] == pytest.approx(0.1) and e.value.seen["silent_s"] < 0.5


async def test_a_held_answer_gets_the_held_answer_limit_before_and_part_way():
    """The model may write the answer before sending it, and may stop sending part-way and send the rest later: after
    any chunk the limit covers writing what is left of the answer (tokens left / slowest rate), not the gap between
    answer chunks."""
    lim = StreamLimits(first_chunk_s=0.2, answer_gap_s=0.05, total_s=5, thinking_gap_s=0.1, held_answer_tokens=100,
                       slowest_tokens_per_s=200, headroom=2)  # 2 x 100 / 200 = 1 s
    assert lim.silence_after() == 1 and lim.silence_after(None, 50, answering=True) == 0.5
    usage = lambda n: types.GenerateContentResponseUsageMetadata(prompt_token_count=1, candidates_token_count=n)  # noqa: E731
    resp, state, _ = await run([(0.0, THOUGHT), (0.6, chunk(answer='{"asset_name":', usage=usage(10))),
                                (0.6, chunk(answer='"x"}', finish="STOP", usage=USAGE))], lim)
    assert resp.text == '{"asset_name":"x"}' and state.longest_gap_s >= 0.6
    # thinking that has used most of the shared cap leaves less answer to wait for
    assert StreamLimits(1, 1, 5, 0.1, held_answer_tokens=100, cap_tokens=150, slowest_tokens_per_s=200, headroom=2).silence_after(120) == pytest.approx(2 * 30 / 200)
    # without running usage the answer received is estimated from its characters, erring towards a longer limit
    st = StreamState(answer_chars=300)
    assert st.answer_tokens() == 100


async def test_the_total_deadline_is_a_plain_timeout_and_final():
    lim = StreamLimits(first_chunk_s=0.2, answer_gap_s=0.2, total_s=0.5, thinking_gap_s=0.2)
    with pytest.raises(TimeoutError) as e:
        await run([(0.1 * k, THOUGHT) for k in range(20)], lim)
    assert not isinstance(e.value, service_answer.StreamStalled) and "whole deadline" in str(e.value)
    assert service_answer.classify(e.value, 0.5, deadline_s=0.5) == "final"


async def test_a_timeout_raised_by_the_call_itself_is_not_taken_for_a_limit():
    with pytest.raises(TimeoutError, match="from the transport"):
        await run([(0.0, THOUGHT), (0.0, TimeoutError("from the transport"))])


# ---------------------------------------------------------------------------------------------- what each break books


class Transport(Exception):
    """Named like httpx's RemoteProtocolError family: service_answer.is_transport recognises it by class name."""


class RemoteProtocolError(Transport):
    pass


DROP = RemoteProtocolError("peer closed connection without sending complete message body")
OK = [(0.0, THOUGHT), (0.0, chunk(answer=ANSWER[:5000])), (0.0, chunk(answer=ANSWER[5000:])), (0.0, chunk(finish="STOP", usage=USAGE))]


def author(api, *, progress=None):
    gov = Governor(plan_for(3), token_ceiling=10_000_000)
    a = M.MeteredAuthor(SimpleNamespace(aio=SimpleNamespace(models=api)), gov, EngineBudget("godot_4", max_tris=3000),
                        {"asset_name": "RuggedMilitarySupply"}, timeout_s=3.0,
                        retry=service_answer.RetryPolicy(sleep=no_sleep, unknown_retries=1), progress=progress)
    return a, gov


@pytest.fixture
def short_limits(monkeypatch):
    monkeypatch.setattr(M, "stream_limits", lambda req, total: StreamLimits(0.3, 0.2, total, 0.3, held_answer_tokens=0,
                                                                            slowest_tokens_per_s=1.0))


async def propose(a):
    return await a.propose_with_outcomes("crate", budget=a.budget, n=1, context={"asset_name": "RuggedMilitarySupply"})


def authoring(gov):
    return [r for r in gov.receipts() if r.get("category") == "author.propose"]


@pytest.mark.parametrize("case, script", [
    ("a: before any chunk", [(0.0, DROP)]),
    ("b: during thinking", [(0.0, THOUGHT), (0.0, THOUGHT), (0.0, DROP)]),
    ("c: part-way through the answer", [(0.0, THOUGHT), (0.0, chunk(answer=ANSWER[:4000])), (0.0, DROP)]),
    ("stall while thinking", [(0.0, THOUGHT), (0.0, "hang")]),
])
async def test_a_stream_that_breaks_before_its_finish_keeps_the_reservation_and_is_retried_once(short_limits, case, script):
    """It may have run, so the attempt keeps its whole reservation (usage unknown); one retry, whose answer is booked
    by its usage. A partial answer is never parsed."""
    api = Api(script, OK)
    a, gov = author(api)
    res = await propose(a)
    assert res.outcomes[0].status == "produced" and len(api.sent) == 2 and api.closed == 2
    first, second = authoring(gov)
    assert first["usage_known"] is False and first["booked_tokens"] == first["reserved_tokens"]
    assert first["stream"]["answer_chars"] == (4000 if case.startswith("c") else 0)
    assert second["usage_known"] is True and second["booked_tokens"] == 6294 + 31064 - 6294 and second["attempt"] == 2
    assert res.outcomes[0].service_retries[0]["outcome"] == "unknown"


async def test_a_stall_on_every_attempt_ends_as_timed_out_with_both_reservations_kept(short_limits):
    api = Api([(0.0, THOUGHT), (0.0, "hang")], [(0.0, "hang")])
    a, gov = author(api)
    with pytest.raises(M.AuthorTimedOut, match="stream silent"):  # the only candidate is lost: propose raises
        await propose(a)
    first, second = authoring(gov)
    assert first["booked_tokens"] == first["reserved_tokens"] and second["booked_tokens"] == second["reserved_tokens"]
    # the retry's first-chunk limit ran from its own send: it stalled waiting, after 0.3 s, not at once
    assert second["stream"]["phase"] == "waiting" and second["stream"]["silent_s"] >= 0.3
    assert first["stream"]["phase"] == "thinking" and first["finish_reason"] == "TIMEOUT"


async def test_raise_timed_out_when_no_candidate_is_usable(short_limits):
    api = Api([(0.0, "hang")], [(0.0, "hang")])
    a, _ = author(api)
    with pytest.raises(M.AuthorTimedOut, match="stream silent"):
        await a.propose("crate", budget=a.budget, n=1, context={"asset_name": "RuggedMilitarySupply"})


@pytest.mark.parametrize("usage_with_finish", [False, True])
async def test_d_a_break_after_the_finish_uses_the_answer(short_limits, usage_with_finish):
    """The answer is complete and paid for: it is used. Its usage is known only when it came with the finish; without
    it the whole reservation is kept, and the receipt says why. No retry."""
    finish = chunk(finish="STOP", usage=USAGE if usage_with_finish else None)
    api = Api([(0.0, THOUGHT), (0.0, chunk(answer=ANSWER)), (0.0, finish), (0.0, DROP)])
    a, gov = author(api)
    res = await propose(a)
    assert res.outcomes[0].status == "produced" and len(api.sent) == 1
    [r] = authoring(gov)
    assert r["status"] == "ok" and "broke after the finish" in r["booked_reason"] and r["stream"]["broke_after_finish"]
    assert r["usage_known"] is usage_with_finish
    assert (r["booked_tokens"] == r["reserved_tokens"]) is not usage_with_finish


def neon_without_finish():
    """r5/live/stream_gaps2/bulk_2.json: eight thought chunks 8.5-36.9 s, each with running usage (thinking 156 to
    3,601 tokens), no answer and no finish; that call then broke with an httpx ReadError. Here the stream ends cleanly
    instead, the other way the same thing can arrive."""
    script = []
    for k, thinking in enumerate((156, 506, 969, 1429, 2001, 2540, 3025, 3601)):
        u = types.GenerateContentResponseUsageMetadata(prompt_token_count=8030, thoughts_token_count=thinking,
                                                       total_token_count=8030 + thinking)
        script.append((0.0, chunk(thought=f"**Part {k}**\nbody", usage=u)))
    return script


async def test_a_stream_that_ends_without_a_finish_is_a_broken_stream_not_an_answer(short_limits):
    from antigravity3d.tactics.stream_call import StreamIncomplete

    with pytest.raises(StreamIncomplete) as e:
        await run(neon_without_finish())
    assert service_answer.classify(e.value, 30, deadline_s=90) == "unknown"
    api = Api(neon_without_finish(), OK)
    a, gov = author(api)
    assert (await propose(a)).outcomes[0].status == "produced" and len(api.sent) == 2
    first, second = authoring(gov)
    assert first["status"] == "StreamIncomplete" and first["usage_known"] is False and first["booked_tokens"] == first["reserved_tokens"]
    assert first["stream"]["usage_seen"]["thoughts_token_count"] == 3601  # the lower bound of what it may have cost


async def test_the_answer_room_is_reported_when_thinking_eats_the_cap(short_limits):
    """Director, cap 32,768: thinking of 26,000 leaves 6,768 for an answer, less than the largest measured (7,160). It is
    reported on the event and the receipt; the call is not stopped (it may still fit, and did on this brief)."""
    events: list[ModelCallProgress] = []
    big = types.GenerateContentResponseUsageMetadata(prompt_token_count=8030, thoughts_token_count=26_000, total_token_count=34_030)
    script = [(0.0, chunk(thought="**Checking**\nx", usage=big))] + OK
    a, gov = author(Api(script), progress=events.append)
    assert (await propose(a)).outcomes[0].status == "produced"
    warn = next(e for e in events if e.event == "thinking" and e.room_short_of)
    assert warn.answer_room_tokens == 32_768 - 26_000 and warn.room_short_of == 7_160 and "may be cut off" in warn.line()
    assert authoring(gov)[0]["stream"]["answer_room_tokens"] == 32_768 - 18_042  # the final usage's thinking


async def test_silence_after_the_finish_uses_the_answer(short_limits):
    api = Api([(0.0, chunk(answer=ANSWER)), (0.0, chunk(finish="STOP", usage=USAGE)), (0.0, "hang")])
    a, gov = author(api)
    assert (await propose(a)).outcomes[0].status == "produced" and len(api.sent) == 1


async def test_a_turned_away_stream_books_nothing(short_limits):
    class ApiErr(Exception):
        code, status, message = 503, "UNAVAILABLE", "high demand"

    api = Api(ApiErr(), OK)
    a, gov = author(api)
    assert (await propose(a)).outcomes[0].status == "produced"
    first, second = authoring(gov)
    assert first["status"] == "not_served" and first["booked_tokens"] == 0 and second["usage_known"] is True


@pytest.mark.parametrize("finish, error", [("MAX_TOKENS", M.AuthorTruncated), ("SAFETY", M.AuthorNoAnswer)])
async def test_the_blocking_errors_still_come_from_a_streamed_answer(short_limits, finish, error):
    script = [(0.0, THOUGHT), (0.0, chunk(answer=ANSWER[:100])), (0.0, chunk(finish=finish, usage=USAGE))]
    # loop2: a cut-off answer whose output was mostly thinking (USAGE: 18,042 of 24,770) is asked once more at the next
    # lower thinking level (metered_author.LESS_THINKING); here that one is cut off too, so two calls are booked
    api = Api(script, script) if finish == "MAX_TOKENS" else Api(script)
    a, gov = author(api)
    with pytest.raises(error):
        await a.propose("crate", budget=a.budget, n=1, context={"asset_name": "RuggedMilitarySupply"})
    rs = authoring(gov)
    assert len(rs) == (2 if finish == "MAX_TOKENS" else 1)
    assert all(r["finish_reason"] == finish and r["usage_known"] is True for r in rs)


async def test_a_blocked_prompt_has_no_candidate_and_is_no_answer(short_limits):
    api = Api([(0.0, chunk(candidate=False, feedback=SimpleNamespace(block_reason="PROHIBITED_CONTENT"), usage=USAGE))])
    a, _ = author(api)
    with pytest.raises(M.AuthorNoAnswer, match="prompt blocked"):
        await a.propose("crate", budget=a.budget, n=1, context={"asset_name": "RuggedMilitarySupply"})


# ---------------------------------------------------------------------------------------------- cancellation


async def test_cancelling_the_caller_closes_the_stream_and_keeps_the_reservation(short_limits):
    started = asyncio.Event()
    api = Api([(0.0, THOUGHT), (0.0, "hang")])
    a, gov = author(api, progress=lambda e: e.event == "thinking" and started.set())
    task = asyncio.create_task(propose(a))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.closed == 1 and len(api.sent) == 1  # closed, and not retried
    [r] = authoring(gov)
    assert r["status"] == "CancelledError" and r["booked_tokens"] == r["reserved_tokens"] and r["usage_known"] is False


# ---------------------------------------------------------------------------------------------- progress


async def test_progress_events_name_the_call_and_never_cost_it(short_limits):
    events: list[ModelCallProgress] = []
    script = [(0.0, chunk(thought="**Laying out the skids**\nbody")), (0.05, chunk(thought="more of the same summary")),
              (0.1, chunk(answer=ANSWER[:8000])), (0.1, chunk(answer=ANSWER[8000:])), (0.1, chunk(finish="STOP", usage=USAGE))]
    a, _ = author(Api(script), progress=events.append)
    await a.propose_with_outcomes("crate", budget=a.budget, n=1, context={"asset_name": "RuggedMilitarySupply"})
    kinds = [e.event for e in events]
    assert kinds[:2] == ["queued", "sent"] and kinds[-1] == "done" and kinds.count("thinking") == 2 and "answering" in kinds
    assert all(e.kind == "author" and e.role and e.operation == "propose" and e.index == 0 and e.attempt == 1 for e in events)
    thinking = [e for e in events if e.event == "thinking"]
    assert [e.heading for e in thinking] == ["Laying out the skids"] * 2  # a continuation keeps the heading
    done = events[-1]
    assert done.finish_reason == "STOP" and done.output_tokens == 31064 - 6294 and done.answer_chars == len(ANSWER)
    assert "thinking: Laying out the skids" in thinking[0].line() and "#1" in done.line()
    assert set(done.to_dict()) >= {"kind", "event", "model", "role", "operation", "index", "elapsed_s"}

    def broken(_e):
        raise RuntimeError("sink broke")

    b, _ = author(Api(OK), progress=broken)
    assert (await propose(b)).outcomes[0].status == "produced"


async def test_a_retry_is_announced_with_its_reason_and_wait(short_limits):
    events: list[ModelCallProgress] = []
    a, _ = author(Api([(0.0, DROP)], OK), progress=events.append)
    await propose(a)
    [retry] = [e for e in events if e.event == "retrying"]
    assert retry.detail.startswith("unknown: RemoteProtocolError") and retry.wait_s == 2.0 and retry.attempt == 1
    assert [e.attempt for e in events if e.event == "sent"] == [1, 2]


async def test_the_wire_going_quiet_does_not_make_the_sink_quiet(monkeypatch):
    from antigravity3d.tactics import stream_call

    monkeypatch.setattr(stream_call, "HEARTBEAT_S", 0.05)
    events = []
    await stream_generate(Api([(0.0, THOUGHT), (0.4, PART), (0.4, chunk(answer='"x"}', finish="STOP", usage=USAGE))]),
                          model="m", contents=[], config=director_request().config,
                          limits=StreamLimits(1, 1, 5, 1), on_event=lambda e, s: events.append((e, s.phase)))
    assert ("silent", "thinking") in events and events.count(("silent", "thinking")) >= 3


async def test_no_sink_no_events_no_change(short_limits):
    a, gov = author(Api(OK))
    assert a.progress is None and (await propose(a)).outcomes[0].status == "produced"


# ---------------------------------------------------------------------------------------------- the real SDK, a local server


class SSEServer:
    """A local HTTP server that answers every POST with server-sent events from a script: ("event", dict) sends one
    event, ("sleep", s) waits, ("hang",) waits until the client goes away, ("drop",) closes the socket mid-body."""

    def __init__(self, script):
        self.script, self.client_gone = script, asyncio.Event()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")), 0)
        await reader.readexactly(length)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        try:
            for step in self.script:
                if step[0] == "event":
                    data = b"data: " + json.dumps(step[1]).encode() + b"\r\n\r\n"
                    writer.write(b"%x\r\n%s\r\n" % (len(data), data))
                    await writer.drain()
                elif step[0] == "sleep":
                    await asyncio.sleep(step[1])
                elif step[0] == "hang":
                    await reader.read()  # returns when the client closes the connection
                    self.client_gone.set()
                    return
                elif step[0] == "drop":
                    writer.transport.abort()
                    return
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            await reader.read()
            self.client_gone.set()
        except (ConnectionError, OSError):
            self.client_gone.set()
        finally:
            writer.close()


def ev_thought(text):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text, "thought": True}]}}]}


def ev_answer(text, finish=None, usage=None):
    c = {"content": {"role": "model", "parts": [{"text": text}]}}
    if finish:
        c["finishReason"] = finish
    return {"candidates": [c], **({"usageMetadata": usage} if usage else {})}


WIRE_USAGE = {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 7, "totalTokenCount": 22}


async def sdk_call(server, limits, state):
    client = genai.Client(api_key="local-test-only", http_options=types.HttpOptions(base_url=server.url))
    config = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                                         http_options=types.HttpOptions(timeout=10_000, retry_options=types.HttpRetryOptions(attempts=1)))
    return await stream_generate(client.aio.models, model="gemini-4-hydrogen", contents="x", config=config, limits=limits, state=state)


async def test_real_sdk_stream_returns_the_answer_without_thoughts():
    script = [("event", ev_thought("**Planning**\nbody")), ("event", ev_answer('{"a":')), ("event", ev_answer("1}", "STOP", WIRE_USAGE))]
    async with SSEServer(script) as srv:
        state = StreamState()
        resp = await sdk_call(srv, StreamLimits(2, 2, 10, 2), state)
    assert resp.text == '{"a":1}' and resp.usage_metadata.total_token_count == 22 and state.heading == "Planning"
    assert M._name(resp.candidates[0].finish_reason) == "STOP"


async def test_real_sdk_stall_closes_the_connection():
    async with SSEServer([("event", ev_thought("**Planning**\nbody")), ("hang",)]) as srv:
        with pytest.raises(service_answer.StreamStalled):
            await sdk_call(srv, StreamLimits(2, 2, 10, 0.3, held_answer_tokens=0, slowest_tokens_per_s=1.0), StreamState())
        await asyncio.wait_for(srv.client_gone.wait(), 2)  # the server saw the client close the connection


async def test_real_sdk_cancellation_closes_the_connection():
    async with SSEServer([("event", ev_thought("**Planning**\nbody")), ("hang",)]) as srv:
        state = StreamState()
        task = asyncio.create_task(sdk_call(srv, StreamLimits(5, 5, 10, 5), state))
        while state.chunks == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(srv.client_gone.wait(), 2)


async def test_real_sdk_drop_mid_answer_is_an_unknown_outcome():
    async with SSEServer([("event", ev_thought("**Planning**\nbody")), ("event", ev_answer('{"a":')), ("drop",)]) as srv:
        state = StreamState()
        with pytest.raises(Exception) as e:
            await sdk_call(srv, StreamLimits(2, 2, 10, 2), state)
    assert service_answer.classify(e.value, 0.1, deadline_s=10) == "unknown" and state.answer_chars == 5


async def test_real_sdk_drop_after_the_finish_returns_the_answer():
    script = [("event", ev_answer('{"a":1}', "STOP", WIRE_USAGE)), ("drop",)]
    async with SSEServer(script) as srv:
        state = StreamState()
        resp = await sdk_call(srv, StreamLimits(2, 2, 10, 2), state)
    assert resp.text == '{"a":1}'
    # A drop after the last event may also read as a clean end; either way the answer is used.
    assert state.broke_after_finish is None or resp.usage_metadata is not None
