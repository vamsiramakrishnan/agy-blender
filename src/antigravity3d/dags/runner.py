"""A tiny async DAG runner: named nodes, dependencies, concurrent execution of independent nodes.

* every node runs as soon as all its dependencies finished (so independent branches overlap);
* a node that raises is ``failed``; everything downstream of it is ``skipped`` (blocked) with the reason;
* a node may call ``ctx.skip(reason)``: it is ``skipped`` but its dependents still run (they get ``None``).
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
    status: str = "pending"  # pending | running | ok | failed | skipped
    start_ms: float = 0.0
    end_ms: float = 0.0
    error: str | None = None
    reason: str | None = None
    blocked: bool = False  # skipped because an upstream node failed
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "deps": list(self.deps), "status": self.status, "start_ms": round(self.start_ms, 2),
                "end_ms": round(self.end_ms, 2), "duration_ms": round(self.duration_ms, 2), "error": self.error,
                "reason": self.reason, "blocked": self.blocked, "notes": self.notes, "data": self.data}


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
        ev = sorted([(n.start_ms, 1) for n in self.nodes.values() if n.status in ("ok", "failed")]
                    + [(n.end_ms, -1) for n in self.nodes.values() if n.status in ("ok", "failed")], key=lambda e: (e[0], e[1]))
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
        c = {s: len(self.by_status(s)) for s in ("ok", "failed", "skipped")}
        lines = [f"DAG {self.name}: {'OK' if self.ok else 'FAILED'} wall={self.wall_ms:.0f}ms ok={c['ok']} skipped={c['skipped']} failed={c['failed']} overlap={self.max_overlap()}"]
        for n in sorted(self.nodes.values(), key=lambda n: (n.start_ms, n.name)):
            tail = n.error or n.reason or ""
            lines.append(f"  {n.name:<14}{n.status:<8}{n.duration_ms:>8.0f}ms  [{n.start_ms:>6.0f}..{n.end_ms:>6.0f}]" + (f"  {tail[:110]}" if tail else ""))
        return "\n".join(lines)


class NodeCtx:
    def __init__(self, name: str, results: dict[str, Any], report: NodeReport) -> None:
        self.name, self._results, self._report = name, results, report

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
    def __init__(self, name: str) -> None:
        self.name = name
        self._fns: dict[str, NodeFn] = {}
        self._deps: dict[str, tuple[str, ...]] = {}

    def node(self, name: str, fn: NodeFn, deps: tuple[str, ...] | list[str] = ()) -> None:
        if name in self._fns:
            raise ValueError(f"duplicate node {name!r}")
        self._fns[name], self._deps[name] = fn, tuple(deps)

    def order(self) -> list[str]:
        """Topological order; raises on unknown dependencies and cycles."""
        for n, ds in self._deps.items():
            if missing := [d for d in ds if d not in self._fns]:
                raise ValueError(f"node {n!r} depends on unknown node(s) {missing}")
        seen: dict[str, int] = {}
        out: list[str] = []

        def visit(n: str) -> None:
            if seen.get(n) == 2:
                return
            if seen.get(n) == 1:
                raise ValueError(f"dependency cycle through {n!r}")
            seen[n] = 1
            for d in self._deps[n]:
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
        reports = {n: NodeReport(n, self._deps[n]) for n in order}
        results: dict[str, Any] = {}
        done = {n: asyncio.Event() for n in order}
        sem = asyncio.Semaphore(max_parallel) if max_parallel else None

        async def run_node(n: str) -> None:
            rep = reports[n]
            try:
                for d in self._deps[n]:
                    await done[d].wait()
                bad = [d for d in self._deps[n] if reports[d].status == "failed" or reports[d].blocked]
                if bad:
                    rep.status, rep.blocked, rep.reason = "skipped", True, f"upstream {', '.join(bad)} did not complete"
                    rep.start_ms = rep.end_ms = now()
                    return
                if sem is not None:
                    await sem.acquire()
                try:
                    rep.status, rep.start_ms = "running", now()
                    try:
                        r = self._fns[n](NodeCtx(n, results, rep))
                        results[n] = await r if inspect.isawaitable(r) else r
                        rep.status = "ok"
                    except Skip as s:
                        results[n], rep.status, rep.reason = None, "skipped", s.reason
                    except Exception as e:  # noqa: BLE001 - isolate the failure to this node's subtree
                        results[n], rep.status, rep.error = None, "failed", f"{type(e).__name__}: {e}"
                    rep.end_ms = now()
                finally:
                    if sem is not None:
                        sem.release()
            finally:
                done[n].set()

        await asyncio.gather(*(run_node(n) for n in order))
        return DagReport(self.name, reports, now(), results)
