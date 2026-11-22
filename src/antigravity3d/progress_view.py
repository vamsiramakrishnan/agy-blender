"""What a developer sees on stderr while a command's model calls run (``tactics.stream_call.ModelCallProgress``).

Stdout stays the command's machine-readable report; everything here goes to stderr, and nothing here can fail a run (a
write that fails is dropped).

* A terminal: one line that rewrites itself every second: the newest author call in flight (role, tier's model, seconds so
  far, the latest thought heading or how much of the answer has arrived) and the judge calls in flight with the oldest
  one's age. A call that ends, fails, is retried or is reused gets a line of its own above it, so the scrollback keeps
  what happened.
* Not a terminal (a CI log, a captured file): appended lines only, few enough to read: each call's start and end, each
  new thought heading, the answer's start and then every ``ANSWER_EVERY_S``, a silence every ``SILENT_EVERY_S``, every
  retry, and, when nothing at all was printed for ``QUIET_EVERY_S``, one line naming what is still in flight, so a
  five-minute call never looks dead.

Measured on 2026-11-22: a director call sends a thought summary every 2-6 s for up to 240 s and its answer for 40 s; a
builder may hold its answer silent for 68-76 s; a judge call (not streamed) took 15-335 s.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from dataclasses import replace
from typing import Any, Callable, TextIO

from antigravity3d.tactics.stream_call import ModelCallProgress

ANSWER_EVERY_S = 15.0  # appended mode: one "answer arriving" line per call this often
SILENT_EVERY_S = 30.0  # appended mode: one line per call for each this many seconds of silence
QUIET_EVERY_S = 30.0  # appended mode: a "still running" line when nothing was printed for this long
REDRAW_S = 1.0  # terminal mode: how often the line is rewritten
_ENDS = ("done", "failed", "reused")


class ProgressView:
    """A progress sink (``MeteredAuthor(progress=)``, ``DecisionControlPlane.progress``, ``stream_call.CURRENT_SINK``).
    Use as ``async with ProgressView() as view:`` so the terminal line is redrawn and cleared, or call it directly."""

    def __init__(self, stream: TextIO | None = None, *, tty: bool | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.stream = stream if stream is not None else sys.stderr
        if tty is None:
            try:
                tty = self.stream.isatty() and os.environ.get("TERM") != "dumb"
            except Exception:  # noqa: BLE001 - a stream without isatty is not a terminal
                tty = False
        self.tty, self.clock = bool(tty), clock
        self.active: dict[tuple[Any, ...], tuple[ModelCallProgress, float]] = {}  # call -> (latest event, when received)
        self._said: dict[tuple[Any, ...], dict[str, float | str]] = {}  # appended mode: what was printed per call
        self._last_print = clock()
        self._line_shown = False
        self._task: asyncio.Task[None] | None = None

    # ---- the sink
    def __call__(self, e: ModelCallProgress) -> None:
        try:
            self._take(e)
        except Exception:  # noqa: BLE001 - a view never breaks a run
            pass

    def _take(self, e: ModelCallProgress) -> None:
        now, key = self.clock(), e.key()
        if e.event in _ENDS:
            self.active.pop(key, None)
        else:
            self.active[key] = (e, now)
        if self.tty:
            if e.event in (*_ENDS, "retrying"):
                self._permanent(e.line())
            self._redraw()
            return
        line = self._appended(e, key, now)
        if e.event in _ENDS:
            self._said.pop(key, None)
        if line:
            self._write(line + "\n")

    def _appended(self, e: ModelCallProgress, key: tuple[Any, ...], now: float) -> str | None:
        said = self._said.setdefault(key, {})
        if e.event in ("sent", "retrying", "failed", "done", "reused"):
            return e.line()
        if e.event == "thinking":
            if e.heading and e.heading != said.get("heading") or e.room_short_of is not None and not said.get("room"):
                said["heading"] = e.heading or ""
                said["room"] = 1 if e.room_short_of is not None else 0
                return e.line()
            return None
        if e.event == "answering":
            last = said.get("answer")
            if last is None or now - float(last) >= ANSWER_EVERY_S:
                said["answer"] = now
                return e.line()
            return None
        if e.event == "silent":
            step = int((e.silent_s or 0.0) // SILENT_EVERY_S)
            if step >= 1 and step > int(said.get("silent", 0)):
                said["silent"] = step
                return e.line()
            return None
        return None  # queued: admission is usually instant; a long wait shows in the quiet line

    # ---- terminal line
    def status(self) -> str:
        """The rewriting line: the newest author call, then the judge calls in flight."""
        now = self.clock()
        authors = [(e, t) for e, t in self.active.values() if e.kind != "judge"]
        judges = [(e, t) for e, t in self.active.values() if e.kind == "judge"]
        parts = []
        if authors:
            e, t = max(authors, key=lambda x: x[1])
            parts.append(replace(e, elapsed_s=e.elapsed_s + (now - t),
                                 silent_s=None if e.silent_s is None else e.silent_s + (now - t)).line())
            if len(authors) > 1:
                parts.append(f"{len(authors) - 1} more author call(s) running")
        if judges:
            oldest = max(e.elapsed_s + (now - t) for e, t in judges)
            ops = sorted({e.operation or "?" for e, _ in judges})
            parts.append(f"judge: {len(judges)} call(s) in flight ({', '.join(ops)[:60]}), oldest {oldest:.0f}s")
        return " | ".join(parts)

    def _redraw(self) -> None:
        text = self.status()
        if not text and not self._line_shown:
            return
        width = max(20, shutil.get_terminal_size((120, 20)).columns - 1)
        self._write("\r" + text[:width] + "\x1b[K")
        self._line_shown = bool(text)

    def _permanent(self, line: str) -> None:
        self._write("\r\x1b[K" + line + "\n")
        self._line_shown = False

    # ---- appended mode: the quiet line
    def _quiet(self) -> None:
        if not self.active or self.clock() - self._last_print < QUIET_EVERY_S:
            return
        now = self.clock()
        running = "; ".join(f"{' '.join(str(x) for x in (e.kind, e.role, e.operation) if x)}"
                            f"{f' #{e.index + 1}' if e.index is not None else ''} {e.elapsed_s + now - t:.0f}s"
                            f"{f' ({e.phase})' if e.phase else ''}" for e, t in self.active.values())
        self._write(f"still running: {running}\n")

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:  # noqa: BLE001 - a closed or broken stderr never stops the run
            return
        self._last_print = self.clock()

    # ---- lifetime
    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(REDRAW_S)
            try:
                self._redraw() if self.tty else self._quiet()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> ProgressView:
        self._task = asyncio.create_task(self._tick())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.tty and self._line_shown:
            self._write("\r\x1b[K")
            self._line_shown = False
