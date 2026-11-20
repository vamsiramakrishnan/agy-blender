"""PropToEngineDAG: prompt -> engine-ready, reviewed prop package.

route -> [ortho tournament || style-anchor upload] -> propose -> [geometry tournament || speculative PBR synthesis]
      -> pyramid (A silhouette, B lookdev, C verification) -> package -> inspector -> report
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from antigravity3d.kernel.publish import publish_directory
from antigravity3d.kernel.validator import validate_package
from antigravity3d.dags.routing import Route, resolve_route
from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.decisions.compose import Thresholds
from antigravity3d.ir import SceneIR
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import AgentAuthor, IRAuthor, _slug
from antigravity3d.tactics.cached import CachedForge
from antigravity3d.tactics.common import uploader_for
from antigravity3d.tactics.distill import FixLog
from antigravity3d.tactics.pyramid import PyramidResult, refine, synthesize_materials
from antigravity3d.tactics.tournament import GRADE_USD_EST, geometry_tournament, ortho_tournament


@dataclass
class Proposal:
    stage: StageManager
    scenes: list[SceneIR]
    asset: str
    author: IRAuthor


class PropToEngineDAG:
    def __init__(self, *, plane: Any, forge: Any, author: IRAuthor, workdir: Path | str, budget: EngineBudget, pool: Any = None, cache: Any = None,
                 thresholds: Thresholds | None = None, style_anchor: Path | str | bytes | None = None, fixlog: FixLog | None = None,
                 max_rounds: int = 4, max_parallel: int | None = None, single_candidate: bool = False,
                 require_verified: bool = False) -> None:
        self.plane, self.author, self.budget, self.pool, self.cache = plane, author, budget, pool, cache
        self.forge = CachedForge(forge, cache, budget=budget) if cache is not None and forge is not None else forge
        self.workdir, self.th, self.style_anchor, self.fixlog = Path(workdir), thresholds or Thresholds(), style_anchor, fixlog
        self.max_rounds, self.max_parallel = max_rounds, max_parallel
        self.require_verified = require_verified
        self.single_candidate = single_candidate
        self.up = uploader_for(self.forge)

    def _run_dir(self) -> Path:
        runs = self.workdir / "runs"
        runs.mkdir(exist_ok=True, parents=True)
        return Path(tempfile.mkdtemp(dir=runs))

    def build(self, prompt: str, *, run_dir: Path | None = None) -> Dag:
        wd, asset = run_dir or self._run_dir(), _slug(prompt)
        dag = Dag("prop_to_engine")

        style_read: asyncio.Task[bytes] | None = None

        async def style_bytes() -> bytes | None:
            nonlocal style_read
            if self.style_anchor is None or isinstance(self.style_anchor, bytes):
                return self.style_anchor
            if style_read is None:
                style_read = asyncio.create_task(asyncio.to_thread(Path(self.style_anchor).read_bytes))
            return await style_read

        async def route(ctx: NodeCtx) -> Route:
            reference = await style_bytes()
            ctx.data(comparison={
                "style_sha256": hashlib.sha256(reference).hexdigest() if reference is not None else None,
                "judge": self.plane.model, "image_model": getattr(self.forge, "model", None),
                "author": type(self.author).__name__, "max_rounds": self.max_rounds, "thresholds": asdict(self.th),
                "cache_enabled": self.cache is not None, "strict": self.require_verified,
            })
            rd = await resolve_route(self.workdir, prompt, self.budget, reference,
                                     single_candidate=self.single_candidate, plane=self.plane)
            if rd.pipeline != "prop_to_engine":
                ctx.note(f"router preferred {rd.pipeline!r}; running prop_to_engine as requested")
            ctx.data(pipeline=rd.pipeline, complexity=rd.complexity, plan=rd.plan.label,
                     ceiling_usd=rd.plan.ceiling_usd, reused=rd.reused)
            return rd

        async def style(ctx: NodeCtx) -> dict[str, Any]:
            data = await style_bytes()
            if data is None:
                ctx.skip("no style anchor provided")
            return {"bytes": data, "uri": await self.up(data)}

        async def ortho(ctx: NodeCtx) -> Any:
            r: Route = ctx["route"]
            if not r.needs_ortho:
                ctx.skip(f"router: no orthographic blueprint needed (P={r.needs_ortho_p:.2f})")
            st = ctx["style_anchor"]
            try:
                t = await ortho_tournament(prompt, self.forge, plane=self.plane, governor=r.governor, uploader=self.up,
                                           style_anchor=st["bytes"] if st else None, context_uris=[st["uri"]] if st else ())
                if t.winner is None:
                    raise RuntimeError(f"no ortho candidate survived ({t.failures or t.disqualified or 'all blocked'})")
            except Exception as e:  # noqa: BLE001 - the blueprint is optional context; degrade instead of blocking geometry
                ctx.skip(f"ortho tournament failed, continuing without a blueprint: {type(e).__name__}: {e}")
            wd.mkdir(parents=True, exist_ok=True)
            (wd / "ortho.png").write_bytes(t.winner.payload.png_bytes)
            ctx.data(**t.to_dict())
            return t

        async def propose(ctx: NodeCtx) -> Proposal:
            r: Route = ctx["route"]
            wd.mkdir(parents=True, exist_ok=True)
            sm = StageManager.create(wd / f"{asset}_stage.usda", asset)
            ort = ctx["ortho"]
            n = r.governor.candidates("geometry", unit_cost=GRADE_USD_EST)
            context = {"asset_name": asset, "ortho_uri": ort.winner.uri if ort else None,
                       "style_uri": ctx["style_anchor"]["uri"] if ctx["style_anchor"] else None}
            author = self.author.bind(r.governor, self.budget, context) if isinstance(self.author, AgentAuthor) else self.author
            scenes = await author.propose(prompt, budget=self.budget, n=n, context=context)
            ctx.data(n_requested=n, n_proposed=len(scenes), materials=sorted({m.id for s in scenes for m in s.materials}))
            return Proposal(sm, list(scenes), asset, author)

        async def geometry(ctx: NodeCtx) -> Any:
            p: Proposal = ctx["propose"]
            r: Route = ctx["route"]
            ort = ctx["ortho"]
            t = await geometry_tournament(p.author, p.stage, prompt, plane=self.plane, governor=r.governor, budget=self.budget, uploader=self.up,
                                          context_uris=[ort.winner.uri] if ort else (), scenes=p.scenes)
            ctx.data(**t.to_dict())
            return t

        async def pbr(ctx: NodeCtx) -> Any:  # speculative: starts as soon as the material list is known
            p: Proposal = ctx["propose"]
            r: Route = ctx["route"]
            mats = list({m.id: m for s in p.scenes for m in s.materials}.values())
            if not mats:
                ctx.skip("proposed scenes have no materials")
            try:
                out = await synthesize_materials(SceneIR(asset_name=asset, materials=mats), self.forge, plane=self.plane, governor=r.governor,
                                                 prompt=prompt, uploader=self.up, cache=self.cache)
            except Exception as e:  # noqa: BLE001 - the pyramid will retry synthesis itself
                ctx.skip(f"speculative PBR synthesis failed ({type(e).__name__}: {e}); pyramid will retry")
            ctx.data(materials={k: v.tournament.to_dict() for k, v in out.items()})
            return out

        async def pyramid(ctx: NodeCtx) -> PyramidResult:
            p: Proposal = ctx["propose"]
            r: Route = ctx["route"]
            ort, st = ctx["ortho"], ctx["style_anchor"]
            res = await refine(p.stage, plane=self.plane, author=p.author, governor=r.governor, budget=self.budget, thresholds=self.th,
                               ortho_uri=ort.winner.uri if ort else None, style_uri=st["uri"] if st else None, max_rounds=self.max_rounds,
                               forge=self.forge, uploader=self.up, pool=self.pool, cache=self.cache, workdir=wd, fixlog=self.fixlog,
                               asset_id=asset, prompt=prompt, textures=ctx["pbr"], require_verified=self.require_verified)
            ctx.data(ok=res.ok, tier15=res.tier15, render_tier=res.render_tier, stopped=res.stopped)
            return res

        async def package(ctx: NodeCtx) -> dict[str, str]:
            p: Proposal = ctx["propose"]
            res: PyramidResult = ctx["pyramid"]
            pkg = wd / "package"
            out = dict(res.artifacts)
            if "usd" not in out or "package" not in res.objects:
                raise RuntimeError("pyramid pass C produced no package USD")
            authoring_path = wd / f"{asset}_authoring.usda"
            p.stage.export(authoring_path)
            out["authoring_usd"] = str(authoring_path)
            manifest_path = pkg / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["verified"] = res.verified and not getattr(self.forge, "offline", False)
            if "engine_import" in res.objects:
                manifest["validation"]["engine_import"] = res.objects["engine_import"]
            manifest["evidence"] = {"render_tier": res.render_tier, "lookdev": res.live_lookdev,
                                    "engine_parity": res.live_parity, "silhouette_iou": res.scores.get("engine_silhouette_iou"),
                                    "steps": [vars(s) for s in res.steps]}
            if (wd / "ortho.png").exists():
                shutil.copyfile(wd / "ortho.png", pkg / "ortho.png")
                out["ortho"] = str(pkg / "ortho.png")
            for label in ("ortho", "engine_render", "dcc_render"):
                if label in out:
                    path = Path(out[label])
                    if path.parent != pkg:
                        continue
                    manifest["files"].append(path.name)
                    manifest["file_hashes"][path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                          "bytes": path.stat().st_size}
            manifest_path.write_text(json.dumps(manifest, indent=1))
            out["inspector"] = str(pkg / manifest["inspector"])
            if manifest["verified"]:
                critical = [i for i in validate_package(pkg) if i.severity == "critical"]
                if critical:
                    raise ValueError(f"final package validation failed: {critical[0].detail}")
            if manifest["verified"] and self.require_verified:
                target = self.workdir / "package"
                publish_directory(pkg, target)
                if pkg.exists():
                    shutil.rmtree(pkg)  # the previously published generation after atomic exchange
                def published_path(value: str) -> str:
                    path = Path(value)
                    return str(target / path.relative_to(pkg)) if path.is_relative_to(pkg) else value
                out = {key: published_path(value) for key, value in out.items()}
                res.artifacts = {key: published_path(value) for key, value in res.artifacts.items()}
            ctx.data(verified=manifest["verified"], published=manifest["verified"] and self.require_verified)
            return out

        async def inspector(ctx: NodeCtx) -> str:
            return ctx["package"]["inspector"]

        async def report(ctx: NodeCtx) -> dict[str, Any]:
            res: PyramidResult = ctx["pyramid"]
            r: Route = ctx["route"]
            if self.require_verified and not (res.verified and not getattr(self.forge, "offline", False)):
                raise RuntimeError("publication requires calibrated lookdev, real DCC pixels, target import, and captured engine parity")
            return {"ok": res.ok, "spent_usd": r.governor.spent, "plan": r.governor.summary()}

        for name, fn, deps in (("route", route, ()), ("style_anchor", style, ("route",)), ("ortho", ortho, ("route", "style_anchor")),
                               ("propose", propose, ("route", "ortho", "style_anchor")), ("geometry", geometry, ("propose", "route", "ortho")),
                               ("pbr", pbr, ("propose", "route", "style_anchor")), ("pyramid", pyramid, ("geometry", "pbr", "propose", "route", "ortho", "style_anchor")),
                               ("package", package, ("pyramid", "propose")), ("inspector", inspector, ("package", "pyramid", "propose")),
                               ("report", report, ("inspector", "pyramid", "route"))):
            dag.node(name, fn, deps)
        return dag

    async def run(self, prompt: str) -> DagReport:
        """Runs the DAG; ``report.extras`` carries outputs, gates, costs and every skipped step with its reason."""
        wd = self._run_dir()
        rep = await self.build(prompt, run_dir=wd).run(max_parallel=self.max_parallel)
        res: PyramidResult | None = rep.results.get("pyramid")
        route: Route | None = rep.results.get("route")
        skipped = [{"step": f"dag:{n.name}", "reason": n.reason} for n in rep.nodes.values() if n.status == "skipped" and n.reason]
        skipped += [{"step": f"pyramid:{s}", "reason": r} for s, r in (res.skipped if res else [])]
        outputs = {**(rep.results.get("package") or {}), **({"inspector": rep.results["inspector"]} if rep.results.get("inspector") else {})}
        cache = None if self.cache is None else asdict(self.cache.stats)
        rep.extras = {
            "prompt": prompt, "engine": self.budget.target_engine, "outputs": outputs, "skipped": skipped,
            "comparison": rep.nodes["route"].data.get("comparison"),
            "published": bool(rep.ok and rep.nodes["package"].data.get("published")),
            "candidate_mode": "single" if self.single_candidate else "governed",
            "budget": asdict(self.budget), "decision_backend": self.plane.backend_kind,
            "preview_gates_ok": bool(res and res.ok and "inspector" in outputs),
            "verified": bool(rep.ok and rep.nodes["package"].data.get("verified")),
            "gates_ok": bool(rep.ok and rep.nodes["package"].data.get("verified")),
            "scores": {"tier15": res.tier15, "tier1_ok": res.tier1_ok, **(res.scores if res else {})} if res else {},
            "cost": {"governor_usd": route.governor.spent if route else 0.0, "by_category": route.governor.by_category if route else {},
                     "receipts": route.governor.receipts() if route else [], "plane": self.plane.stats.summary(), "cache": cache, "plan": route.governor.summary() if route else None},
            "pyramid": res.to_dict() if res else None, "workdir": str(self.workdir)}
        report_path = wd / "report.json"
        rep.extras["report_path"] = str(report_path)
        data = rep.to_json()
        report_path.write_text(data)
        latest = wd / ".latest.json"
        latest.write_text(data)
        latest.replace(self.workdir / "report.json")
        return rep
