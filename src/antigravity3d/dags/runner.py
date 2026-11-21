"""A tiny async DAG runner: named nodes, dependencies, concurrent execution of independent nodes.

* every node runs as soon as all its dependencies finished (so independent branches overlap);
* a node that raises is ``failed``; everything downstream of it is ``skipped`` (blocked) with the reason;
* a node may call ``ctx.skip(reason)``: it is ``skipped`` but its dependents still run (they get ``None``);
* a late input (``node(..., late=("x",))``): the consumer starts without waiting for ``x`` and awaits it inside, with
  ``await ctx.late("x")``, only where it needs it. ``x`` is still a consumer relation for the stop rule; if ``x`` failed,
  was blocked or stopped, the await raises :class:`LateInputFailed` with the upstream reason (so the consumer fails);
  if ``x`` was skipped it gives ``None``. A consumer that finishes without awaiting it no longer needs it;
* opt-in (``Dag(..., stop_unusable=True)``): a running node none of whose consumers can run any more (each depends on a
  node that failed, was blocked or was stopped) is cancelled and reads ``stopped``, with the reason. Its downstream is
  blocked as after a failure. A node registered with ``keep=True`` always finishes, and a node without consumers (a
  leaf: an output of the run) is never stopped. Cancellation reaches the node's code as ``CancelledError``: a provider
  call already sent keeps its reservation (the spenders settle an unknown outcome in full), and nothing is refunded.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class Skip(Exception):
    """Raise from a node to mark it skipped (not failed) with a reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class NodeReport:
    name: str
    deps: tuple[str, ...]
    status: str = "pending"  # pending | running | ok | failed | skipped | stopped
    start_ms: float = 0.0
    end_ms: float = 0.0
    error: str | None = None
    reason: str | None = None
    blocked: bool = False  # skipped because an upstream node failed (or was stopped)
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    late: tuple[str, ...] = ()  # late inputs: awaited inside the node, not before it starts

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "deps": list(self.deps), "status": self.status, "start_ms": round(self.start_ms, 2),
                "end_ms": round(self.end_ms, 2), "duration_ms": round(self.duration_ms, 2), "error": self.error,
                "reason": self.reason, "blocked": self.blocked, "notes": self.notes, "data": self.data,
                **({"late": list(self.late)} if self.late else {})}


@dataclass
class DagReport:
    name: str
    nodes: dict[str, NodeReport]
    wall_ms: float = 0.0
    results: dict[str, Any] = field(default_factory=dict, repr=False)  # node -> return value; not serialised
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(n.status == "failed" for n in self.nodes.values())

    def by_status(self, status: str) -> list[str]:
        return [n.name for n in self.nodes.values() if n.status == status]

    def max_overlap(self) -> int:
        """Largest number of nodes that were running at the same instant."""
        ran = ("ok", "failed", "stopped")
        ev = sorted([(n.start_ms, 1) for n in self.nodes.values() if n.status in ran]
                    + [(n.end_ms, -1) for n in self.nodes.values() if n.status in ran], key=lambda e: (e[0], e[1]))
        cur = best = 0
        for _, d in ev:
            cur += d
            best = max(best, cur)
        return best

    def to_dict(self) -> dict[str, Any]:
        return {"dag": self.name, "ok": self.ok, "wall_ms": round(self.wall_ms, 2), "max_overlap": self.max_overlap(),
                "nodes": [n.to_dict() for n in self.nodes.values()], **self.extras}

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def summary(self) -> str:
        c = {s: len(self.by_status(s)) for s in ("ok", "failed", "skipped", "stopped")}
        stopped = f" stopped={c['stopped']}" if c["stopped"] else ""
        lines = [f"DAG {self.name}: {'OK' if self.ok else 'FAILED'} wall={self.wall_ms:.0f}ms ok={c['ok']} skipped={c['skipped']} failed={c['failed']}{stopped} overlap={self.max_overlap()}"]
        for n in sorted(self.nodes.values(), key=lambda n: (n.start_ms, n.name)):
            tail = n.error or n.reason or ""
            lines.append(f"  {n.name:<14}{n.status:<8}{n.duration_ms:>8.0f}ms  [{n.start_ms:>6.0f}..{n.end_ms:>6.0f}]" + (f"  {tail[:110]}" if tail else ""))
        return "\n".join(lines)


class LateInputFailed(RuntimeError):
    """A late input of a node failed, was blocked or was stopped."""


class NodeCtx:
    def __init__(self, name: str, results: dict[str, Any], report: NodeReport,
                 late: Callable[[str], Awaitable[Any]] | None = None) -> None:
        self.name, self._results, self._report, self._late = name, results, report, late

    def late(self, dep: str) -> Awaitable[Any]:
        """An awaitable of a late input's result. Nothing waits until it is awaited (it may never be)."""
        if self._late is None:
            raise KeyError(f"{self.name} has no late input {dep!r}")
        owner = self

        class _Later:
            def __await__(self):  # noqa: ANN204 - awaitable protocol
                return owner._late(dep).__await__()

        return _Later()

    def __getitem__(self, dep: str) -> Any:
        return self._results[dep]

    def note(self, text: str) -> None:
        self._report.notes.append(text)

    def data(self, **kw: Any) -> None:
        self._report.data.update(kw)

    @staticmethod
    def skip(reason: str) -> None:
        raise Skip(reason)


NodeFn = Callable[[NodeCtx], Awaitable[Any] | Any]


class Dag:
    def __init__(self, name: str, *, stop_unusable: bool = False) -> None:
        """``stop_unusable``: stop a running node once none of its consumers can run (see the module docstring)."""
        self.name = name
        self.stop_unusable = stop_unusable
        self._fns: dict[str, NodeFn] = {}
        self._deps: dict[str, tuple[str, ...]] = {}
        self._keep: set[str] = set()
        self._late: dict[str, tuple[str, ...]] = {}

    def node(self, name: str, fn: NodeFn, deps: tuple[str, ...] | list[str] = (), *, keep: bool = False,
             late: tuple[str, ...] | list[str] = ()) -> None:
        """``keep``: the node always finishes, even when nothing downstream can use it (it makes something kept).
        ``late``: inputs the node does not wait for to start; it awaits them with ``ctx.late(name)``."""
        if name in self._fns:
            raise ValueError(f"duplicate node {name!r}")
        if set(late) & set(deps):
            raise ValueError(f"node {name!r}: an input is either a dependency or a late input, not both")
        self._fns[name], self._deps[name] = fn, tuple(deps)
        if late:
            self._late[name] = tuple(late)
        if keep:
            self._keep.add(name)

    def order(self) -> list[str]:
        """Topological order; raises on unknown dependencies and cycles."""
        for n, ds in self._deps.items():
            if missing := [d for d in (*ds, *self._late.get(n, ())) if d not in self._fns]:
                raise ValueError(f"node {n!r} depends on unknown node(s) {missing}")
        seen: dict[str, int] = {}
        out: list[str] = []

        def visit(n: str) -> None:
            if seen.get(n) == 2:
                return
            if seen.get(n) == 1:
                raise ValueError(f"dependency cycle through {n!r}")
            seen[n] = 1
            for d in (*self._deps[n], *self._late.get(n, ())):  # a late input must not depend on its consumer either
                visit(d)
            seen[n] = 2
            out.append(n)

        for n in self._fns:
            visit(n)
        return out

    async def run(self, *, max_parallel: int | None = None) -> DagReport:
        order = self.order()
        t0 = time.perf_counter()
        now = lambda: (time.perf_counter() - t0) * 1e3  # noqa: E731
        reports = {n: NodeReport(n, self._deps[n], late=self._late.get(n, ())) for n in order}
        results: dict[str, Any] = {}
        done = {n: asyncio.Event() for n in order}
        sem = asyncio.Semaphore(max_parallel) if max_parallel else None
        consumers = {n: [m for m in order if n in self._deps[m] or n in self._late.get(m, ())] for n in order}
        running: dict[str, asyncio.Task[Any]] = {}
        why_stopped: dict[str, str] = {}

        def dead(n: str) -> bool:
            r = reports[n]
            return r.status in ("failed", "stopped") or r.blocked

        def unusable(consumer: str) -> str | None:
            """Why ``consumer`` can no longer use a running input: it can never run, or it already ended (only a late
            input's consumer can end before that input is ready), else None."""
            if reports[consumer].status in ("ok", "skipped") and not reports[consumer].blocked:
                return f"{consumer} ended without it"
            return doomed(consumer)

        def doomed(n: str) -> str | None:
            """Why node ``n`` can never run (it or an upstream node failed, was blocked or stopped), else None."""
            if dead(n):
                return f"{n} {reports[n].status}" if not reports[n].blocked else f"{n} blocked"
            for d in self._deps[n]:
                if dead(d):
                    return f"upstream {d} {'blocked' if reports[d].blocked else reports[d].status}"
                if (why := doomed(d)) is not None:
                    return why
            return None

        def stop_unusable() -> None:
            if not self.stop_unusable:
                return
            for n, task in list(running.items()):
                if n in self._keep or not consumers[n] or task.done() or n in why_stopped:
                    continue
                reasons = [(c, unusable(c)) for c in consumers[n]]
                if all(r is not None for _, r in reasons):
                    why_stopped[n] = "stopped: no consumer can run (" + "; ".join(f"{c}: {r}" for c, r in reasons) + ")"
                    task.cancel()

        async def late_input(n: str, dep: str) -> Any:
            if dep not in self._late.get(n, ()):
                raise KeyError(f"{dep!r} is not a late input of {n!r}")
            if not done[dep].is_set():
                if sem is not None:  # do not hold a slot while waiting: the input may need it
                    sem.release()
                try:
                    await done[dep].wait()
                finally:
                    if sem is not None:
                        await sem.acquire()
            d = reports[dep]
            if d.status in ("failed", "stopped") or d.blocked:
                raise LateInputFailed(f"late input {dep} {'blocked' if d.blocked else d.status}: {d.error or d.reason}")
            return results.get(dep)

        async def run_node(n: str) -> None:
            rep = reports[n]
            try:
                for d in self._deps[n]:
                    await done[d].wait()
                bad = [d for d in self._deps[n] if reports[d].status in ("failed", "stopped") or reports[d].blocked]
                if bad:
                    rep.status, rep.blocked, rep.reason = "skipped", True, f"upstream {', '.join(bad)} did not complete"
                    rep.start_ms = rep.end_ms = now()
                    return
                if sem is not None:
                    await sem.acquire()
                try:
                    rep.status, rep.start_ms = "running", now()
                    try:
                        r = self._fns[n](NodeCtx(n, results, rep, (lambda dep, n=n: late_input(n, dep)) if n in self._late else None))
                        if inspect.isawaitable(r) and not self.stop_unusable:
                            r = await r
                        elif inspect.isawaitable(r):  # its own task, so that it alone can be stopped
                            task = asyncio.ensure_future(r)
                            running[n] = task
                            try:
                                r = await task
                            finally:
                                running.pop(n, None)
                        results[n] = r
                        rep.status = "ok"
                    except Skip as s:
                        results[n], rep.status, rep.reason = None, "skipped", s.reason
                    except asyncio.CancelledError:
                        if n not in why_stopped:  # the run itself was cancelled: propagate
                            raise
                        results[n], rep.status, rep.reason = None, "stopped", why_stopped[n]
                    except Exception as e:  # noqa: BLE001 - isolate the failure to this node's subtree
                        results[n], rep.status, rep.error = None, "failed", f"{type(e).__name__}: {e}"
                    rep.end_ms = now()
                finally:
                    if sem is not None:
                        sem.release()
            finally:
                done[n].set()
                stop_unusable()  # a node just ended (or was blocked): some running node may have lost every consumer

        await asyncio.gather(*(run_node(n) for n in order))
        return DagReport(self.name, reports, now(), results)
