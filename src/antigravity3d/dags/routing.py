"""Asset-scoped routing checkpoint and spend journal for resumable prop builds."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import Governor, SpendPlan, plan_for


@dataclass
class Route:
    pipeline: str
    needs_ortho: bool
    needs_ortho_p: float
    complexity: float
    plan: SpendPlan
    governor: Governor
    cost_usd: float
    reused: bool


async def resolve_route(workdir: Path, prompt: str, budget: EngineBudget, style: bytes | None, *,
                        single_candidate: bool, plane: DecisionControlPlane, token_ceiling: int | None = None,
                        max_images: int | None = None) -> Route:
    """``token_ceiling`` / ``max_images``: the user's limits of a token ledger (see ``Governor``). A preview plane
    (``plane.preview``) may route on an uncalibrated decision; nothing else may."""
    identity = {"prompt": prompt, "budget": asdict(budget), "single_candidate": single_candidate,
                "style_sha256": hashlib.sha256(style).hexdigest() if style is not None else None,
                "backend": plane.backend_kind, "model": plane.model}
    if token_ceiling is not None or getattr(plane, "preview", False):  # never shares a ledger or route with a money run
        identity.update(spend_unit="tokens" if token_ceiling is not None else "usd", preview=bool(getattr(plane, "preview", False)))
    ledger = dict(token_ceiling=token_ceiling, max_images=max_images)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    folder = workdir / "ledgers" / key
    folder.mkdir(parents=True, exist_ok=True)
    spend = folder / "spend.sqlite3"
    # Lock routing separately: the provider call settles receipts in the spend database before it returns.
    with closing(sqlite3.connect(folder / "route.sqlite3", timeout=0)) as db, db:
        while True:
            try:
                db.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                await asyncio.sleep(0.05)
        db.execute("CREATE TABLE IF NOT EXISTS route (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        row = db.execute("SELECT payload FROM route WHERE id=1").fetchone()
        if row is not None:
            data = json.loads(row[0])
            plan = SpendPlan(**data.pop("plan"))
            return Route(**data, plan=plan, governor=Governor(plan, journal=spend, **ledger), cost_usd=0.0, reused=True)
        bootstrap = Governor(plan_for(0), journal=spend, **ledger)
        decision = await plane.route_pipeline(prompt, "# empty project: new asset", governor=bootstrap)
        if not decision.result.calibrated and not getattr(plane, "preview", False):
            raise ValueError("the asset spend plan requires a calibrated routing decision")
        plan = plan_for(decision.complexity)
        if single_candidate:
            plan = replace(plan, ortho_candidates=1, geometry_candidates=1, pbr_candidates=1)
        data = {"pipeline": decision.pipeline, "needs_ortho": decision.needs_ortho,
                "needs_ortho_p": decision.needs_ortho_p, "complexity": decision.complexity}
        db.execute("INSERT INTO route VALUES(1, ?)", (json.dumps({**data, "plan": asdict(plan)}),))
        return Route(**data, plan=plan, governor=Governor(plan, journal=spend, **ledger), cost_usd=decision.result.booked_usd, reused=False)
