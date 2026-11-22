"""Progress where a developer looks (antigravity3d.progress_view, the judge plane's start and end events, the MCP sink):
what is printed and when, that stdout is never written, and that a broken sink or a broken stderr never breaks a run."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest

from antigravity3d import progress_view as PV
from antigravity3d.decisions import questions as Q
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.tactics.stream_call import CURRENT_SINK, ModelCallProgress, emit


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def author(event: str, **kw) -> ModelCallProgress:
    return ModelCallProgress("author", event, "gemini-4-argon", "director", "propose", 0, **kw)


def test_appended_mode_prints_starts_ends_new_headings_and_spaced_answer_and_silence_lines():
    out, clock = io.StringIO(), Clock()
    view = PV.ProgressView(out, tty=False, clock=clock)
    view(author("queued"))
    view(author("sent"))
    for t, heading in ((12, "Reviewing Crate Requirements"), (14, "Reviewing Crate Requirements"), (18, "Analyzing Part Composition")):
        clock.t = t
        view(author("thinking", elapsed_s=t, heading=heading))
    for s in (10, 20, 30, 40, 60):  # the stream sends a silent event every 10 s; one line per 30 s of silence
        view(author("silent", elapsed_s=18 + s, phase="thinking", silent_s=s))
    for t in (240, 241, 250, 256, 270):  # answer events every second or so; one line per 15 s
        clock.t = t
        view(author("answering", elapsed_s=t, answer_chars=100 * t))
    view(author("done", elapsed_s=282, finish_reason="STOP", output_tokens=32450))
    lines = out.getvalue().splitlines()
    assert [ln.split("] ", 1)[1].split(None, 1)[1] for ln in lines] == [
        "sent to gemini-4-argon",
        "thinking: Reviewing Crate Requirements",
        "thinking: Analyzing Part Composition",
        "no new thought for 30 s: still thinking, or writing the answer, which some calls send at the end",
        "no new thought for 60 s: still thinking, or writing the answer, which some calls send at the end",
        "answer arriving: 24,000 characters",
        "answer arriving: 25,600 characters",
        "finished STOP, 32,450 output tokens"]
    assert all(ln.startswith("[author director propose #1]") for ln in lines) and not view.active


def test_appended_mode_says_what_is_still_running_when_nothing_was_printed_for_a_while():
    out, clock = io.StringIO(), Clock()
    view = PV.ProgressView(out, tty=False, clock=clock)
    view(ModelCallProgress("judge", "sent", "gemini-4-neon", operation="visual_qa:lookdev", call="j1", detail="1 judge call(s) in flight"))
    clock.t = 10
    view._quiet()
    assert out.getvalue().count("\n") == 1  # not yet
    clock.t = PV.QUIET_EVERY_S + 1
    view._quiet()
    assert out.getvalue().splitlines()[-1].startswith("still running: judge visual_qa:lookdev 31s")


def test_terminal_mode_rewrites_one_line_and_keeps_ends_in_the_scrollback():
    out, clock = io.StringIO(), Clock()
    view = PV.ProgressView(out, tty=True, clock=clock)
    view(author("sent"))
    clock.t = 5
    view(author("thinking", elapsed_s=5, heading="Planning"))
    view(ModelCallProgress("judge", "sent", "gemini-4-neon", operation="lod_pop", call="j1"))
    clock.t = 9
    assert view.status() == ("[author director propose #1]     9s thinking: Planning | judge: 1 call(s) in flight (lod_pop), "
                             "oldest 4s")
    view(author("done", elapsed_s=9, finish_reason="STOP"))
    text = out.getvalue()
    assert text.count("\r") >= 3 and "\x1b[K" in text and "\n" not in text.split("finished STOP")[0]
    assert "[author director propose #1]     9s finished STOP\n" in text  # the end has a line of its own


async def test_a_broken_stderr_or_a_raising_sink_never_breaks_the_run():
    class Broken(io.StringIO):
        def write(self, s):
            raise BrokenPipeError

        def isatty(self):
            raise OSError

    async with PV.ProgressView(Broken()) as view:  # isatty raising: treated as not a terminal
        view(author("sent"))
        view(author("done"))
        await asyncio.sleep(0)

    def raising(_e):
        raise RuntimeError("sink broke")

    emit(author("sent"), raising)  # dropped, not raised
    token = CURRENT_SINK.set(raising)
    try:
        plane = DecisionControlPlane(FakeDecisionsClient(answers={}))
        r = await plane.evaluate("x", {"q": Q.noul("is it?")}, tag="t")
        assert r is not None
    finally:
        CURRENT_SINK.reset(token)


async def test_the_judge_says_when_each_call_starts_and_ends_and_how_many_are_in_flight():
    events: list[ModelCallProgress] = []
    plane = DecisionControlPlane(FakeDecisionsClient(answers={}))
    token = CURRENT_SINK.set(events.append)
    try:
        await asyncio.gather(*(plane.evaluate("x", {"q": Q.noul("is it?")}, tag=f"t{i}") for i in range(3)))
    finally:
        CURRENT_SINK.reset(token)
    sent = [e for e in events if e.event == "sent"]
    done = [e for e in events if e.event == "done"]
    assert len(sent) == len(done) == 3 and {e.kind for e in events} == {"judge"}
    assert len({e.call for e in sent}) == 3 and {e.call for e in sent} == {e.call for e in done}
    assert all(e.detail.endswith("judge call(s) in flight") for e in sent) and done[-1].detail == "0 still in flight"
    assert plane._in_flight == [0]
    # an explicit sink wins over the context's; a preview copy counts with the plane it was copied from
    mine: list[ModelCallProgress] = []
    copy = plane.preview_copy()
    copy.progress = mine.append
    assert copy._in_flight is plane._in_flight


async def test_the_mcp_tool_wrapper_reports_its_calls_as_progress_notifications(monkeypatch):
    from antigravity3d.runtime import mcp_server as S

    sent: list[tuple] = []

    class Ctx:
        async def report_progress(self, progress, total=None, message=None):
            sent.append((progress, total, message))

    host = S.StudioHost()

    async def call_tool(name, fn, sid, kwargs):  # what a judging tool does: its calls emit into the context's sink
        emit(ModelCallProgress("judge", "sent", "gemini-4-neon", operation="visual_qa:lookdev", call="j1"))
        emit(ModelCallProgress("judge", "done", "gemini-4-neon", operation="visual_qa:lookdev", call="j1", elapsed_s=34))
        return "OK"

    monkeypatch.setattr(host, "call_tool", call_tool)
    from antigravity3d.harness import tools as T

    tool = S._wrapper("run_visual_qa", T.ALL_TOOLS["run_visual_qa"], host)
    assert await tool(ctx=Ctx()) == "OK"
    await asyncio.sleep(0)
    assert [p for p, _, _ in sent] == [1, 2] and sent[1][2].endswith("34s finished")
    assert CURRENT_SINK.get() is None  # reset after the call

    class Gone:
        async def report_progress(self, *a, **k):
            raise ConnectionError("client went away")

    assert await tool(ctx=Gone()) == "OK"  # a failed send is dropped
    await asyncio.sleep(0)
    server = S.build_server(host)
    t = server._tool_manager.get_tool("run_visual_qa")
    assert t.context_kwarg == "ctx" and "ctx" not in t.parameters.get("properties", {})
