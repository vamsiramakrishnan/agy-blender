"""PropToEngineDAG: prompt -> engine-ready, reviewed prop package.

route -> [ortho tournament || style-anchor upload] -> propose -> [geometry tournament || speculative PBR synthesis]
      -> pyramid (A silhouette, B lookdev, C verification) -> package -> inspector -> report
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from antigravity3d.artifacts.inspector import InspectorCollider, InspectorLod, InspectorQA, InspectorTextures, build_inspector
from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.decisions.compose import Thresholds
from antigravity3d.ir import SceneIR
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import IRAuthor, _slug
from antigravity3d.tactics.cached import CachedForge
from antigravity3d.tactics.common import png_bytes, uploader_for
from antigravity3d.tactics.distill import FixLog
from antigravity3d.tactics.governor import Governor, SpendPlan, plan_for
from antigravity3d.tactics.package import merged_lods
from antigravity3d.tactics.pyramid import PyramidResult, refine, synthesize_materials
from antigravity3d.tactics.tournament import GRADE_USD_EST, geometry_tournament, ortho_tournament


@dataclass
class Route:
    pipeline: str
    needs_ortho: bool
    needs_ortho_p: float
    complexity: float
    plan: SpendPlan
    governor: Governor
    cost_usd: float


@dataclass
class Proposal:
    stage: StageManager
    scenes: list[SceneIR]
    asset: str


class PropToEngineDAG:
    def __init__(self, *, plane: Any, forge: Any, author: IRAuthor, workdir: Path | str, budget: EngineBudget, pool: Any = None, cache: Any = None,
                 thresholds: Thresholds | None = None, style_anchor: Path | str | bytes | None = None, fixlog: FixLog | None = None,
                 max_rounds: int = 4, max_parallel: int | None = None) -> None:
        self.plane, self.author, self.budget, self.pool, self.cache = plane, author, budget, pool, cache
        self.forge = CachedForge(forge, cache) if cache is not None and forge is not None else forge
        self.workdir, self.th, self.style_anchor, self.fixlog = Path(workdir), thresholds or Thresholds(), style_anchor, fixlog
        self.max_rounds, self.max_parallel = max_rounds, max_parallel
        self.up = uploader_for(self.forge)

    def build(self, prompt: str) -> Dag:
        wd, asset = self.workdir, _slug(prompt)
        dag = Dag("prop_to_engine")

        async def route(ctx: NodeCtx) -> Route:
            rd = await self.plane.route_pipeline(prompt, "# empty project: new asset")
            plan = plan_for(rd.complexity)
            gov = Governor(plan)
            gov.charge("route", rd.result.est_cost_usd)
            if rd.pipeline != "prop_to_engine":
                ctx.note(f"router preferred {rd.pipeline!r}; running prop_to_engine as requested")
            ctx.data(pipeline=rd.pipeline, complexity=rd.complexity, plan=plan.label, ceiling_usd=plan.ceiling_usd)
            return Route(rd.pipeline, rd.needs_ortho, rd.needs_ortho_p, rd.complexity, plan, gov, rd.result.est_cost_usd)

        async def style(ctx: NodeCtx) -> dict[str, Any]:
            if self.style_anchor is None:
                ctx.skip("no style anchor provided")
            data = self.style_anchor if isinstance(self.style_anchor, bytes) else Path(self.style_anchor).read_bytes()  # type: ignore[arg-type]
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
            scenes = await self.author.propose(prompt, budget=self.budget, n=n, context={
                "asset_name": asset, "ortho_uri": ort.winner.uri if ort else None, "style_uri": ctx["style_anchor"]["uri"] if ctx["style_anchor"] else None})
            ctx.data(n_requested=n, n_proposed=len(scenes), materials=sorted({m.id for s in scenes for m in s.materials}))
            return Proposal(sm, list(scenes), asset)

        async def geometry(ctx: NodeCtx) -> Any:
            p: Proposal = ctx["propose"]
            r: Route = ctx["route"]
            ort = ctx["ortho"]
            t = await geometry_tournament(self.author, p.stage, prompt, plane=self.plane, governor=r.governor, budget=self.budget, uploader=self.up,
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
            res = await refine(p.stage, plane=self.plane, author=self.author, governor=r.governor, budget=self.budget, thresholds=self.th,
                               ortho_uri=ort.winner.uri if ort else None, style_uri=st["uri"] if st else None, max_rounds=self.max_rounds,
                               forge=self.forge, uploader=self.up, pool=self.pool, cache=self.cache, workdir=wd, fixlog=self.fixlog,
                               asset_id=asset, prompt=prompt, textures=ctx["pbr"])
            ctx.data(ok=res.ok, tier15=res.tier15, render_tier=res.render_tier, stopped=res.stopped)
            return res

        async def package(ctx: NodeCtx) -> dict[str, str]:
            p: Proposal = ctx["propose"]
            res: PyramidResult = ctx["pyramid"]
            pkg = wd / "package"
            pkg.mkdir(parents=True, exist_ok=True)
            out = dict(res.artifacts)
            if "usd" not in out:
                raise RuntimeError("pyramid pass C produced no package USD")
            p.stage.export(pkg / f"{asset}_authoring.usda")  # note: write_engine_package already wrote manifest.json
            out["authoring_usd"] = str(pkg / f"{asset}_authoring.usda")
            if (wd / "ortho.png").exists():
                shutil.copy2(wd / "ortho.png", pkg / "ortho.png")
                out["ortho"] = str(pkg / "ortho.png")
            if Path(out["manifest"]).exists():
                return {**out, "authoring_usd": out["authoring_usd"]}
            manifest = {"asset": asset, "engine": self.budget.target_engine, "budget": asdict(self.budget), "prompt": prompt,
                        "files": sorted(str(x.relative_to(pkg)) for x in pkg.rglob("*") if x.is_file()), "tier15": res.tier15, "tier1_ok": res.tier1_ok}
            (pkg / "manifest.json").write_text(json.dumps(manifest, indent=2))
            out["manifest"] = str(pkg / "manifest.json")
            return out

        async def inspector(ctx: NodeCtx) -> str:
            res: PyramidResult = ctx["pyramid"]
            b = res.objects.get("bundle")
            if b is None:
                ctx.skip("no export bundle (pass C failed)")
            scene = ctx["propose"].stage.base_ir()
            mat_of = {n.id: n.material_id for n in scene.nodes}
            meshes = [MeshData(u.node_id, "", u.points, u.triangles, mat_of.get(u.node_id)) for u in b.uvs]
            maps = res.objects.get("maps", {})
            tex = {k: InspectorTextures(png_bytes(m.albedo), png_bytes(m.orm), png_bytes(m.normal)) for k, m in maps.items()}
            lods = [InspectorLod(f"LOD{k}", m.points, m.triangles) for k, m in enumerate(merged_lods(b)) if k > 0]
            path = build_inspector(meshes, asset_name=asset, budget=self.budget, textures=tex, lods=lods,
                                   colliders=[InspectorCollider(h.name, h.points, h.triangles) for h in b.colliders],
                                   qa=InspectorQA(res.tier1_ok, "", res.tier15, res.defects, res.audit[-8:]),
                                   uvs={u.node_id: u.uvs for u in b.uvs}, out_path=wd / "package" / "inspector.html")
            return str(path)

        async def report(ctx: NodeCtx) -> dict[str, Any]:
            res: PyramidResult = ctx["pyramid"]
            r: Route = ctx["route"]
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
        rep = await self.build(prompt).run(max_parallel=self.max_parallel)
        res: PyramidResult | None = rep.results.get("pyramid")
        route: Route | None = rep.results.get("route")
        skipped = [{"step": f"dag:{n.name}", "reason": n.reason} for n in rep.nodes.values() if n.status == "skipped" and n.reason]
        skipped += [{"step": f"pyramid:{s}", "reason": r} for s, r in (res.skipped if res else [])]
        outputs = {**(rep.results.get("package") or {}), **({"inspector": rep.results["inspector"]} if rep.results.get("inspector") else {})}
        cache = None if self.cache is None else asdict(self.cache.stats)
        rep.extras = {
            "prompt": prompt, "engine": self.budget.target_engine, "outputs": outputs, "skipped": skipped,
            "gates_ok": bool(rep.ok and res and res.ok and "inspector" in outputs),
            "scores": {"tier15": res.tier15, "tier1_ok": res.tier1_ok, **(res.scores if res else {})} if res else {},
            "cost": {"governor_usd": route.governor.spent if route else 0.0, "by_category": route.governor.by_category if route else {},
                     "plane": self.plane.stats.summary(), "cache": cache, "plan": route.governor.summary() if route else None},
            "pyramid": res.to_dict() if res else None, "workdir": str(self.workdir)}
        rep.extras["report_path"] = str(self.workdir / "report.json")
        (self.workdir / "report.json").write_text(rep.to_json())
        return rep
