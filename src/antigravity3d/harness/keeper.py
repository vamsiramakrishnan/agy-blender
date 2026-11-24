"""Tool calls that answer inside the host's time limit and keep their work.

Antigravity ends every MCP tool call after 3 minutes ("MCP tool call to server ... timed out after 3m0s: context
deadline exceeded"): a fact of the host binary, with no setting in the plugin's mcp_config.json or the SDK's server
config. Run 1 of the website task (benchmarks/results/site_run1, 2026-11-24) lost two deliver_package calls on the
70-part containment_gate that way, each cancelled while it wrote the package for its checks, and with the work thrown
away each time the asset could never be delivered by that tool; generate_lods_and_colliders and export_engine_package
on the 49-part anchor_caliper were cut off the same way. So every long call runs as a task of its own, kept by its
session (or by the server, for calls that have no session yet):

* the call answers within ``ANSWER_S`` whatever it is doing: with its result when it is done, otherwise with one line
  that says nothing needs fixing, which step it is at, and to call the same tool again with the same arguments;
* the work goes on after that answer, and when the client abandons the call: its cancel reaches only the waiting,
  never the task;
* the next call of the same tool with the same arguments joins the task (it waits for it, again at most ``ANSWER_S``)
  and so runs no step twice;
* a call that changes the design (``edit=True``) first stops every kept call of its session and drops their answers:
  they were made for a design that no longer exists. Quick edits (``transaction=True``) are not stopped by a later
  edit but run one after the other. A stopped task drains the native step it is in (``tools.
  _stage_worker``) and removes its own folders, as a cancelled call always did;
* any other call waits, within its own time, for a kept call of its session that is still running, so two calls never
  work on one session at once; if that call is not done by then, the answer says which call is still running and to
  call it again first. A call the server keeps (run_build) claims the sessions it works on (``Keeper.claim``), so a
  session's calls wait for it too;
* an answer not yet collected is kept only while the design is the one it was made for (``version``): a change made
  anywhere, also inside another kept call (a review's own fix), makes it stale and the next call runs anew.

What an agent must do to join: call the same tool again with the same arguments, nothing else (no ticket or id to
carry): it is the call it already made, and the answer names it.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("antigravity3d.keeper")

# Every call answers within this many seconds of being called. The host's limit is 180 s (above); 30 s are left for
# the answer to reach the client and for the step that is finishing when the time is up (a call is answered at the
# time, not when its step ends). Kept from DELIVER_ANSWER_S, which bounded only deliver_package's wait for its review:
# live on 2026-11-22 (r5/live_final_N) that review's one judge call took 131 s after 54 s of checks and render.
ANSWER_S = 150.0

_CURRENT: contextvars.ContextVar["Kept | None"] = contextvars.ContextVar("agy3d_kept_call", default=None)
# Set by a caller whose tools take a ``session_id`` argument (the MCP server): the call an answer spells out then names
# its session. Without it the repeat goes to whichever session is active by then: in run 1 of the website task the
# agent had three asset sessions open at once, and the website skill tells it to go on working while a delivery runs,
# so the next thing it does may well be to open another session.
NAMES_SESSION: contextvars.ContextVar[bool] = contextvars.ContextVar("agy3d_names_session", default=False)


@dataclass
class Kept:
    """One kept call: the tool, the arguments it was called with, its task and the step it is at."""

    name: str
    key: str
    shown: str
    task: asyncio.Task
    started: float = field(default_factory=time.monotonic)
    step: tuple[int, int, str] | None = None
    steps: list[tuple[str, float]] = field(default_factory=list)  # (step, seconds into the call it began)
    sink: Callable[[Any], None] | None = None  # the progress sink of the call waiting on it now, if any
    transaction: bool = False  # a quick edit of the design: another edit waits for it instead of stopping it
    version: Any = None  # the design's version when the call finished: its answer holds only for that design

    def where(self) -> str:
        secs = time.monotonic() - self.started
        if self.step is None:
            return f"{secs:.0f} s in"
        i, n, what = self.step
        return f"step {i} of {n} ({what}), {secs:.0f} s in"


def step(tool: str, i: int, n: int, what: str) -> None:
    """Say which step the running call of ``tool`` is at (for its answer and session_status). A nested tool's steps
    (deliver_package calling export_engine_package) are not the call's own and are ignored."""
    k = _CURRENT.get()
    if k is not None and k.name == tool:
        k.step = (i, n, what)
        k.steps.append((what, time.monotonic() - k.started))


def current() -> Kept | None:
    """The kept call this code runs in, if any."""
    return _CURRENT.get()


def _retrieve(task: asyncio.Task) -> None:
    """Mark a task's exception as seen the moment it ends (the call that collects it still gets it raised): a task
    that failed while nobody waited and was then dropped would make asyncio log "Task exception was never retrieved"."""
    if not task.cancelled():
        task.exception()


def failure(task: asyncio.Task) -> BaseException | None:
    return None if not task.done() or task.cancelled() else task.exception()


def inside() -> bool:
    """Whether this code runs inside a kept call (a tool called by another tool is part of it, not a call of its own)."""
    return _CURRENT.get() is not None


def shown_call(name: str, args: dict[str, Any]) -> str:
    """The call as the agent writes it again: ``deliver_package(out_name="crate", dest="assets")``; an argument too long to
    repeat (a list of operations) is named, not shown."""
    parts = []
    for k, v in args.items():
        text = json.dumps(v, default=str)
        parts.append(f"{k}={text}" if len(text) <= 80 else f"{k}=<the same {k} as before>")
    return f"{name}({', '.join(parts)})"


def call_key(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, default=str)


def working(k: Kept) -> str:
    """The answer of a call whose work goes on: unmistakably not a refusal (an agent that reads "NOT DELIVERED" starts
    repairing), what it is doing, and the one thing to do."""
    return (f"STILL WORKING (nothing needs fixing, nothing failed): {k.name} is at {k.where()} and goes on by itself. "
            f"Call {k.shown} again now: that call waits for this work and answers with its result; no step is done twice.")


def busy(k: Kept, name: str) -> str:
    return (f"STILL WORKING (nothing needs fixing, nothing failed): {k.shown}, called earlier, is still running ({k.where()}), "
            f"so {name} has not run yet. Call {k.shown} again now to get its answer, then call {name}.")


class Keeper:
    """The kept calls of one session (or of the server): at most one running at a time, and the answers of finished
    ones until they are collected or made stale by an edit."""

    def __init__(self, version: Callable[[], Any] | None = None) -> None:
        self.calls: dict[tuple[str, str], Kept] = {}
        self.claims: list[Kept] = []  # calls kept elsewhere (the server's run_build) that work on this session now
        self.version = version  # the design's version: an answer made for another version is not given

    def running(self) -> Kept | None:
        """The kept call still at work here: one of this keeper's own, or one kept elsewhere that claimed it."""
        self.claims = [k for k in self.claims if not k.task.done()]
        return next((k for k in [*self.calls.values(), *self.claims] if not k.task.done()), None)

    def claim(self, k: Kept | None) -> None:
        """``k``, kept by another keeper, works on what this keeper keeps calls for until it ends: calls here wait for
        it (an edit too: it cannot be stopped from here)."""
        if k is not None and k not in self.claims:
            self.claims.append(k)

    def _now(self) -> Any:
        return self.version() if self.version is not None else None

    def status(self) -> str:
        """One line for session_status: the call that is running and its step, or an answer waiting to be collected."""
        lines = []
        for k in self.calls.values():
            if not k.task.done():
                lines.append(f"RUNNING: {k.name} is at {k.where()}; call {k.shown} again to get its answer.")
            elif (err := failure(k.task)) is not None:
                lines.append(f"FAILED: {k.shown} stopped with an error ({type(err).__name__}: {str(err)[:160]}); call it "
                             "again to run it anew.")
            elif not k.task.cancelled() and (k.version is None or k.version == self._now()):
                lines.append(f"READY: {k.shown} has finished; call it again to get its answer.")
        return "\n".join(lines)

    async def stop(self, keep: tuple[str, str] | None = None) -> list[str]:
        """Stop every kept call but ``keep`` and the transactions still running (an edit is not made stale by another:
        each applies to the design as it finds it, so they run one after the other) and drop the answers not collected;
        returns the names of the calls that were still running. Waits until each stopped task has finished the native
        step it was in and removed its folders."""
        stopped, tasks = [], []
        for key, k in list(self.calls.items()):
            if key == keep or (k.transaction and not k.task.done()):
                continue
            del self.calls[key]
            if not k.task.done():
                stopped.append(k.name)
                k.task.cancel()
                tasks.append(k.task)
        for t in tasks:
            try:
                await t
            except BaseException:  # noqa: BLE001 - a stopped task ends in CancelledError (or the error it was raising)
                pass
        return stopped

    async def call(self, name: str, args: dict[str, Any] | Callable[[], dict[str, Any]], run: Callable[[], Awaitable[Any]], *, shown: dict[str, Any] | None = None,
                   edit: bool = False, transaction: bool = False, read: bool = False, cancel_others: bool = True,
                   stopped_note: Callable[[list[str]], str] | None = None, wait_for: Kept | None = None) -> Any:
        """Run ``run()`` as a kept call of ``name`` with ``args`` and answer within ``ANSWER_S``.

        ``edit``: the call changes what kept calls were made for: it stops them first (``cancel_others``; otherwise it
        waits for the running one and only drops answers), and ``stopped_note(names)`` is added to its answer.
        ``transaction``: a quick edit applied as one transaction to the design as it finds it: a later edit waits for
        it (they run one after the other) instead of stopping it.
        ``shown``: the arguments as the agent gave them, when ``args`` (what makes two calls the same) holds more.
        ``read``: the call only reads: it starts at once, beside a running call. Any other call waits for the running
        one within its own time.
        ``wait_for``: a call kept elsewhere that this one must not run beside (an install of the package a delivery is
        still writing): waited for first, like a running call here. ``args`` may then be a function, read once that
        call has ended (the package it wrote is the one the call is about)."""
        if inside():  # a tool called by a tool: part of that call
            return await run()
        deadline = time.monotonic() + ANSWER_S
        if wait_for is not None and not wait_for.task.done():
            await asyncio.wait({wait_for.task}, timeout=max(0.0, deadline - time.monotonic()))
            if not wait_for.task.done():
                return busy(wait_for, name)
        if callable(args):
            args = args()
        key = (name, call_key(args))
        k = self.calls.get(key)
        if k is not None and k.task.done() and (failure(k.task) is not None or
                                                (k.version is not None and k.version != self._now())):
            del self.calls[key]  # failed (said in status), or made for a design that has changed since: run anew
            k = None
        if k is None:
            other = self.running()
            stoppable = other is not None and other not in self.claims and not other.transaction
            if other is not None and not read and not (edit and cancel_others and stoppable):
                await asyncio.wait({other.task}, timeout=max(0.0, deadline - time.monotonic()))
                if not other.task.done():
                    return busy(other, name)
            k = self._start(key, name, shown_call(name, args if shown is None else shown), run, edit=edit,
                            transaction=transaction, cancel_others=cancel_others,
                            stopped_note=stopped_note)
        return await self._wait(key, k, deadline)

    def _start(self, key: tuple[str, str], name: str, shown: str, run: Callable[[], Awaitable[Any]], *, edit: bool,
               transaction: bool, cancel_others: bool, stopped_note: Callable[[list[str]], str] | None) -> Kept:
        from antigravity3d.tactics.stream_call import CURRENT_SINK

        for other, k in list(self.calls.items()):  # a newer call of the same tool supersedes an answer not collected
            if k.name == name and k.task.done():
                del self.calls[other]
        holder: list[Kept] = []

        def forward(event: Any) -> None:  # model-call progress goes to whichever call is waiting on this work now
            if holder and holder[0].sink is not None:
                holder[0].sink(event)

        async def body() -> Any:
            stopped = (await self.stop(keep=key)) if edit and cancel_others else []
            if edit and not cancel_others:
                await self.stop(keep=key)  # the others have finished (this call waited for them): drop their answers
            result = await run()
            if stopped and stopped_note is not None:
                result = _append(result, stopped_note(stopped))
            return result

        ctx = contextvars.copy_context()
        ctx.run(CURRENT_SINK.set, forward)
        task = asyncio.get_running_loop().create_task(self._timed(body, holder), context=ctx)
        task.add_done_callback(_retrieve)
        k = Kept(name, key[1], shown, task, transaction=transaction)
        holder.append(k)
        ctx.run(_CURRENT.set, k)
        self.calls[key] = k
        return k

    async def _timed(self, body: Callable[[], Awaitable[Any]], holder: list[Kept]) -> Any:
        try:
            result = await body()
            if holder:
                holder[0].version = self._now()  # the design this answer was made for (its own changes included)
            return result
        finally:
            if holder and holder[0].steps:
                k = holder[0]
                total = time.monotonic() - k.started
                marks = [*k.steps, ("end", total)]
                log.info("%s took %.1f s: %s", k.name, total, "; ".join(
                    f"{what} {marks[i + 1][1] - at:.1f} s" for i, (what, at) in enumerate(marks[:-1])))

    async def _wait(self, key: tuple[str, str], k: Kept, deadline: float) -> Any:
        from antigravity3d.tactics.stream_call import CURRENT_SINK

        k.sink = CURRENT_SINK.get()
        try:
            await asyncio.wait({k.task}, timeout=max(0.0, deadline - time.monotonic()))
        finally:
            k.sink = None
        if not k.task.done():
            return working(k)
        if self.calls.get(key) is k:
            del self.calls[key]
        if k.task.cancelled():
            return (f"STOPPED: {k.name} was stopped because the design changed while it ran (its work was for the design "
                    f"before). Call {k.shown} again for the design as it is now.")
        return k.task.result()


def _append(result: Any, line: str) -> Any:
    """``result`` with one more line; a reply that shows images keeps them."""
    from antigravity3d.harness.tools import ToolReply

    if isinstance(result, ToolReply):
        return ToolReply(f"{result}\n{line}", result.images)
    return f"{result}\n{line}" if isinstance(result, str) else result
