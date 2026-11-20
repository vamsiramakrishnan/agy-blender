"""One headless Chromium per process, shared by every web-runtime verification in it.

Starting Playwright's driver and a Chromium costs about half a second and is the same work for every package, so a
process keeps one browser running and gives each verification a fresh browser context of its own instead (its own
request routing, console, page errors and blocked requests): nothing one package's check sees can come from another's.

Playwright's asyncio objects belong to the event loop that created them, but callers run one ``asyncio.run`` per asset,
call from worker threads, or verify two packages at once. The browser therefore lives on a private event loop in a
daemon thread, and callers hand it coroutines from whatever loop or thread they are on (``SharedChromium.run``).

Lifetime:

* ``lease()`` marks a user (a pipeline run, or one verification). A lease taken with ``prestart`` starts the browser
  and its warm-up work in the background, so a pipeline run overlaps the start-up with its own earlier work.
* When the last lease is released after success, the browser stays up for the next verification and is closed after
  ``IDLE_CLOSE_S`` without users. When it is released after a failure or a cancellation, the browser is closed at once:
  a failed run leaves nothing behind.
* A browser that crashed, was closed, or cannot open a context within ``CONTEXT_TIMEOUT_S`` is discarded (killed if it
  does not close) and a fresh one is started for the waiting verification. A verification that times out or is
  cancelled has its context closed; if even that does not finish, the browser is discarded.
* At interpreter exit (and at the end of a ``multiprocessing`` worker, which skips ``atexit``) the browser and the
  driver are closed, and killed if they do not close in time. A forked child starts with no browser of its own and
  never touches its parent's.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import os
import signal
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

#: seconds a browser with no users stays up for the next verification
IDLE_CLOSE_S = 120.0
#: seconds a reused browser gets to open a context before it is treated as hung and replaced
CONTEXT_TIMEOUT_S = 20.0
#: seconds closing a context, a browser or the driver may take before the processes are killed
CLOSE_TIMEOUT_S = 10.0


def _children() -> dict[int, list[int]]:
    """Parent pid -> child pids, from /proc (Linux); empty where /proc does not exist."""
    out: dict[int, list[int]] = {}
    with contextlib.suppress(OSError):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    stat = f.read()
            except OSError:
                continue
            ppid = int(stat[stat.rindex(b")") + 2:].split()[1])
            out.setdefault(ppid, []).append(int(name))
    return out


def kill_tree(pid: int) -> None:
    """SIGKILL ``pid`` and every descendant (the driver, the browser it launched, the browser's helpers)."""
    tree, todo, seen = _children(), [pid], []
    while todo:
        p = todo.pop()
        seen.append(p)
        todo.extend(tree.get(p, ()))
    for p in reversed(seen):
        with contextlib.suppress(OSError):
            os.kill(p, signal.SIGKILL)


def _kill_driver(driver: Any) -> None:
    """Kill a Playwright driver that has not exited, with the browser it launched (its pid is not reused before exit)."""
    if getattr(driver, "returncode", 0) is None:
        kill_tree(driver.pid)


@dataclass
class Session:
    """One Playwright driver and the Chromium it launched. Only touched on the pool's loop."""

    exe: str | None
    args: tuple[str, ...]
    stop: Callable[[], Awaitable[None]]
    browser: Any
    driver: Any  # asyncio.subprocess.Process of the Playwright driver, when it can be found
    generation: int
    broken: bool = False

    def alive(self) -> bool:
        return not self.broken and self.browser.is_connected()


class SharedChromium:
    """The process's browser. ``SHARED`` is the instance every verifier uses."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._pid = os.getpid()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._leases = 0
        # the rest is only touched on the pool's loop
        self._session: Session | None = None
        self._start_lock: asyncio.Lock | None = None
        self._memo: dict[Any, asyncio.Future] = {}
        self._idle: asyncio.TimerHandle | None = None
        self._drivers: set = set()  # driver processes not yet confirmed gone (read at exit from another thread)
        #: browsers launched by this process so far (tests and benchmarks read it)
        self.launches = 0

    # ------------------------------------------------------------------ loop plumbing (any thread)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        # Playwright is imported here, on the caller's thread, never first on the browser's: importing its greenlet
        # extension on one thread while another thread imports pxr can deadlock both imports.
        import playwright.async_api  # noqa: F401
        with self._mutex:
            if self._pid != os.getpid():  # forked: the parent's thread and browser are not ours
                self._reset()
            if self._loop is None or self._thread is None or not self._thread.is_alive():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=self._serve, args=(loop,), name="agy3d-web-browser", daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
                _register_exit_hooks()
            return self._loop

    @staticmethod
    def _serve(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def submit(self, coro: Awaitable[Any]) -> concurrent.futures.Future:
        """Run ``coro`` on the browser's loop; the returned future is safe to wait on from any thread."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        # retrieved, so that a failed background start is not logged: the verification that needs the browser reports it
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
        return fut

    async def _on_pool(self, coro: Awaitable[Any]) -> Any:
        """Await ``coro`` on the browser's loop from the caller's loop. Cancelling the caller cancels it there too."""
        return await asyncio.wrap_future(self.submit(coro))

    # ------------------------------------------------------------------ leases (any thread)

    def lease(self, prestart: Callable[[Session], Awaitable[Any]] | None = None, *, exe: str | None = None,
              args: tuple[str, ...] = ()) -> Lease:
        """Mark a user. With ``prestart``, start the browser now (and run ``prestart`` on it) without waiting."""
        self._ensure_loop()
        with self._mutex:
            self._leases += 1
        self._loop.call_soon_threadsafe(self._cancel_idle)
        if prestart is not None:
            self.submit(self._prestart(exe, args, prestart))
        return Lease(self)

    def _release(self, keep: bool) -> concurrent.futures.Future | None:
        with self._mutex:
            self._leases -= 1
            last = self._leases == 0
        if not last or self._loop is None:
            return None
        return self.submit(self._after_last(keep))

    def _cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    async def _after_last(self, keep: bool) -> None:
        with self._mutex:
            if self._leases:  # a new user arrived since
                return
        self._cancel_idle()
        if keep:
            self._idle = asyncio.get_running_loop().call_later(IDLE_CLOSE_S, lambda: self.submit(self._close_if_idle()))
        else:
            await self._close_if_idle()

    async def _close_if_idle(self) -> None:
        with self._mutex:
            if self._leases:
                return
        await self._close()

    async def _close(self) -> None:
        """Close the browser, after a launch in flight has finished (so that launch cannot leave one behind)."""
        async with self._lock():
            await self._discard()

    def _lock(self) -> asyncio.Lock:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        return self._start_lock

    # ------------------------------------------------------------------ the browser (pool loop only)

    async def _prestart(self, exe: str | None, args: tuple[str, ...], warm: Callable[[Session], Awaitable[Any]]) -> None:
        await warm(await self._get(exe, args))

    async def _get(self, exe: str | None, args: tuple[str, ...]) -> Session:
        async with self._lock():
            s = self._session
            if s is not None and s.alive() and (s.exe, s.args) == (exe, args):
                return s
            await self._discard()
            self._session = await self._launch(exe, args)
            return self._session

    async def _launch(self, exe: str | None, args: tuple[str, ...]) -> Session:
        from playwright.async_api import (
            async_playwright,  # already imported by _ensure_loop
        )

        cm = async_playwright()
        pw = await cm.start()
        # the driver's process, so that a driver that will not stop can be killed with what it launched
        driver = getattr(getattr(getattr(cm, "_client", None), "_transport", None), "_proc", None)
        if driver is not None:
            self._drivers.add(driver)
        try:
            browser = await pw.chromium.launch(executable_path=exe, args=list(args))
        except BaseException:
            await self._stop_or_kill(pw.stop, driver)
            raise
        self.launches += 1
        s = Session(exe, args, pw.stop, browser, driver, self.launches)
        browser.on("disconnected", lambda _b: setattr(s, "broken", True))
        return s

    async def _discard(self, s: Session | None = None) -> None:
        """Close the current browser (or ``s`` if it is still current) and its driver; kill them if that hangs."""
        s = s or self._session
        if s is None:
            return
        if self._session is s:
            self._session = None
        s.broken = True
        with contextlib.suppress(BaseException):  # hung or already gone: the kill below covers it
            await asyncio.wait_for(s.browser.close(), CLOSE_TIMEOUT_S)
        await self._stop_or_kill(s.stop, s.driver)

    async def _stop_or_kill(self, stop: Callable[[], Awaitable[None]], driver: Any) -> None:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(stop(), CLOSE_TIMEOUT_S)
        if driver is not None:
            _kill_driver(driver)  # stop() waits for the driver to exit: one still running is hung
            self._drivers.discard(driver)

    async def memo(self, key: Any, fn: Callable[[], Awaitable[Any]]) -> Any:
        """``fn()`` once per process and ``key`` (pool loop only). It runs as a task of its own, so a waiter that is
        cancelled does not cancel it for the others; a failure is not remembered."""
        task = self._memo.get(key)
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            task = self._memo[key] = asyncio.ensure_future(fn())
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return await asyncio.shield(task)

    async def _in_context(self, exe: str | None, args: tuple[str, ...], context_options: dict,
                          fn: Callable[[Session, Any], Awaitable[Any]]) -> Any:
        for attempt in (1, 2):
            s = await self._get(exe, args)
            try:
                ctx = await asyncio.wait_for(s.browser.new_context(**context_options), CONTEXT_TIMEOUT_S)
            except Exception:
                # A browser that cannot open a context crashed or hangs: it is not the package's fault, so replace it
                # and give the verification a fresh one (once).
                await self._discard(s)
                if attempt == 2:
                    raise
                continue
            try:
                return await fn(s, ctx)
            finally:
                try:
                    await asyncio.wait_for(asyncio.shield(ctx.close()), CLOSE_TIMEOUT_S)
                except BaseException:  # noqa: BLE001  a context that will not close means a browser that cannot be trusted
                    await asyncio.shield(self._discard(s))
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ public (any loop or thread)

    async def run(self, fn: Callable[[Session, Any], Awaitable[Any]], *, exe: str | None, args: tuple[str, ...] = (),
                  context_options: dict | None = None) -> Any:
        """``await fn(session, context)`` on the browser's loop, with a fresh context that is closed afterwards."""
        lease = self.lease()
        try:
            out = await self._on_pool(self._in_context(exe, tuple(args), dict(context_options or {}), fn))
        except asyncio.CancelledError:
            lease.release(keep=False)  # a cancelled verification belongs to a run that is ending: leave nothing behind
            raise
        except BaseException:
            lease.release(keep=True)
            raise
        lease.release(keep=True)
        return out

    def shutdown(self, timeout: float = 2 * CLOSE_TIMEOUT_S + 5) -> None:
        """Close the browser and the driver now (blocking; any thread but the pool's own)."""
        with self._mutex:
            loop, thread = self._loop, self._thread
            if loop is None or thread is None or not thread.is_alive() or self._pid != os.getpid():
                return
        fut = asyncio.run_coroutine_threadsafe(self._close(), loop)
        try:
            fut.result(timeout)
        except BaseException:  # noqa: BLE001
            for driver in list(self._drivers):
                _kill_driver(driver)

    @property
    def running(self) -> bool:
        s = self._session
        return s is not None and s.alive()


class Lease:
    """One user of the shared browser. Release it exactly once; ``keep=False`` closes the browser if it was the last.

    ``Lease(None)`` holds nothing: it is what a caller gets where no browser can run (no Playwright, no Chromium), so
    the caller releases it the same way and nothing here is imported or started."""

    def __init__(self, pool: SharedChromium | None) -> None:
        self._pool, self._done = pool, False

    def release(self, keep: bool = True) -> concurrent.futures.Future | None:
        if self._done or self._pool is None:
            return None
        self._done = True
        return self._pool._release(keep)

    async def aclose(self, keep: bool = True) -> None:
        """Release and, when that closes the browser, wait until it is closed."""
        fut = self.release(keep)
        if fut is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(asyncio.wrap_future(fut))


SHARED = SharedChromium()

_hooks_registered = False


def _register_exit_hooks() -> None:
    global _hooks_registered
    if _hooks_registered:
        return
    _hooks_registered = True
    atexit.register(SHARED.shutdown)
    # multiprocessing workers leave through os._exit after their own finalizers, so atexit never runs there
    import multiprocessing.util

    multiprocessing.util.Finalize(None, SHARED.shutdown, exitpriority=100)


def _after_fork_in_child() -> None:
    global _hooks_registered
    _hooks_registered = False
    SHARED._mutex = threading.Lock()
    SHARED._reset()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)
