"""PropToEngineDAG: prompt -> engine-ready, reviewed prop package.

route -> [ortho tournament || style-anchor upload] -> propose -> [geometry tournament || speculative PBR synthesis]
      -> pyramid (A silhouette, B lookdev, C verification) -> package -> inspector -> report

Two independent style inputs compose: ``style_anchor`` is an image (uploaded and attached to the concept sheet, the
author and visual QA as a reference), ``art_direction`` a named style (``antigravity3d.art_direction``): it lowers the
budget where its look calls for it, sets the shipped maps, shading and sampling, and adds its briefs to the concept,
author and texture prompts. Either, both or neither may be given.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from antigravity3d.adapters.blender_5_3 import fbx_package
from antigravity3d.adapters.engines import web_three
from antigravity3d.art_direction import ArtDirection, author_text, concept_prompt, texture_brief
from antigravity3d.kernel.package import drop_fbx
from antigravity3d.kernel.publish import publish_directory
from antigravity3d.kernel.validator import validate_package
from antigravity3d.dags.routing import Route, resolve_route
from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.decisions.compose import Thresholds
from antigravity3d.ir import SceneIR
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import AgentAuthor, IRAuthor, _slug
from antigravity3d.tactics.cached import CachedForge, DesignRecord
from antigravity3d.tactics.common import uploader_for
from antigravity3d.tactics.distill import FixLog
from antigravity3d.tactics.pyramid import PyramidResult, default_texture_px, materials_by_area, refine, synthesize_materials
from antigravity3d.tactics.tournament import geometry_tournament, grade_est, ortho_tournament


@dataclass
class Proposal:
    stage: StageManager
    scenes: list[SceneIR]
    asset: str
    author: IRAuthor


class ImageLimitRefused(RuntimeError):
    """The routed plan cannot run inside the user's image limit; raised before any image is requested."""


class CeilingLeftRefused(RuntimeError):
    """A rerun on an asset whose ledger already holds spend: what is left of the token ceiling cannot admit the largest
    single call the run must make. Raised before any generation call of this run."""


def plan_images(route: Route) -> int:
    """The fewest images the routed plan generates: its ortho best-of-N (when the router asked for a blueprint), its albedo
    best-of-N for at least one material, and the hero plan's paint-over."""
    p = route.plan
    return (p.ortho_candidates if route.needs_ortho else 0) + p.pbr_candidates + (1 if p.paintover_loop else 0)


def preview_ok(res: PyramidResult, preview_judging: bool) -> bool:
    """The preview gates. On a preview plane the review that an uncalibrated judge always requires does not fail them
    (``needs_review`` stays true and is printed); a hard policy flag, an escalation to a person or any failed gate does."""
    if not preview_judging or not res.needs_review:
        return res.ok
    escalated = any(t.action == "escalate_human" for t in res.rounds)
    return not escalated and (res.ok or _ok_but_for_uncalibrated_review(res))


def _ok_but_for_uncalibrated_review(res: PyramidResult) -> bool:
    from dataclasses import replace as _r

    return _r(res, needs_review=False).ok


def preview_record(plane: Any, governor: Any, res: PyramidResult | None) -> dict[str, Any]:
    """What a preview package and its run report say about themselves: never verified, who judged, what that means."""
    from antigravity3d.decisions.engine import UNCALIBRATED_MEANING

    calibrated = bool(getattr(plane, "calibrated", True))
    out: dict[str, Any] = {
        "verified": False, "preview": True,
        "judge": {"label": getattr(plane, "judge_label", "unknown"), "calibrated": calibrated,
                  "meaning": UNCALIBRATED_MEANING if not calibrated else "a preview is never verified, whoever judged it"},
        "needs_review": True}
    if governor is not None:
        out["spend"] = {"unit": governor.unit, "summary": governor.summary()}
    return out


def prior_spend_line(g: Any) -> str:
    """What a rerun finds in the asset's ledger, in plain words with the numbers, said before any call."""
    if g.unit == "tokens":
        used = f"{g.spent:,.0f} of {g.ceiling:,.0f} tokens (no price applied) already used, {g.remaining:,.0f} remaining"
        more = "rerun with a higher --token-ceiling to give it more"
    else:
        used = f"${g.spent:.4f} of the plan's ${g.ceiling:.2f} already used, ${g.remaining:.4f} remaining"
        more = "the plan's ceiling is the most it may use"
    imgs = f"; {g.images_used} of {g.max_images} images used" if g.max_images is not None else ""
    return (f"this asset's ledger already holds spend from earlier runs in this --out: {used}{imgs}. The limits cover "
            f"every run of this asset together, so this run has only what is left; {more}.")


class PropToEngineDAG:
    def __init__(self, *, plane: Any, forge: Any, author: IRAuthor, workdir: Path | str, budget: EngineBudget, pool: Any = None, cache: Any = None,
                 thresholds: Thresholds | None = None, style_anchor: Path | str | bytes | None = None, fixlog: FixLog | None = None,
                 max_rounds: int = 4, max_parallel: int | None = None, single_candidate: bool = False,
                 require_verified: bool = False, art_direction: ArtDirection | None = None,
                 token_ceiling: int | None = None, max_images: int | None = None, new_design: bool = False,
                 announce: Any = None, min_call_tokens: int | None = None) -> None:
        """``new_design``: author a new design instead of continuing from the one recorded beside the image cache by an
        earlier run in this workdir (``DesignRecord``); the image cache is used as always. ``announce``: called with a
        line of text before any call when this asset's ledger already holds spend from earlier runs.
        ``min_call_tokens``: the largest single call this run must be able to make (the start-up check's figure); a
        rerun whose ledger has less than that left is refused before any call (``CeilingLeftRefused``)."""
        if require_verified and getattr(plane, "preview", False):
            raise ValueError("a verified run cannot use a preview decision plane")
        self.art = art_direction
        self.token_ceiling, self.max_images = token_ceiling, max_images
        # A preview plane may let an uncalibrated judge steer (rank, repair, gate); its verdicts never clear anything.
        self.preview_judging = bool(getattr(plane, "preview", False)) and not require_verified
        budget = art_direction.budget(budget, "prop") if art_direction is not None else budget  # only ever lowers it
        self.plane, self.author, self.budget, self.pool, self.cache = plane, author, budget, pool, cache
        self.forge = CachedForge(forge, cache, budget=budget) if cache is not None and forge is not None else forge
        self.record = DesignRecord(cache, fresh=new_design) if cache is not None else None
        self.announce, self.min_call_tokens = announce, min_call_tokens
        self._image_short: tuple[int, int, str] | None = None  # (need, left, refusal) deferred to propose on a rerun
        self._before: tuple[float, int] | None = None  # (spend, images) this asset's ledger held before this run
        self.workdir, self.th, self.style_anchor, self.fixlog = Path(workdir), thresholds or Thresholds(), style_anchor, fixlog
        if self.preview_judging:
            self.th = replace(self.th, trust_uncalibrated=True)
        self.max_rounds, self.max_parallel = max_rounds, max_parallel
        self.require_verified = require_verified
        self.single_candidate = single_candidate
        self.up = uploader_for(self.forge)

    def _texture_px(self) -> int | None:
        """The art direction's map size for this engine; None leaves the pipeline's default (same rule as the pyramid)."""
        if self.art is None or self.art.texture_px_prop is None or self.forge is None:
            return None
        return self.art.texture_px(self.budget.target_engine, "prop", default_texture_px(self.forge))

    def _run_dir(self) -> Path:
        runs = self.workdir / "runs"
        runs.mkdir(exist_ok=True, parents=True)
        return Path(tempfile.mkdtemp(dir=runs))

    def build(self, prompt: str, *, run_dir: Path | None = None) -> Dag:
        wd, asset = run_dir or self._run_dir(), _slug(prompt)
        # Stop a branch nobody can use any more (the speculative materials once geometry has failed): its later paid
        # calls would buy nothing. Every node here feeds the package; none makes something kept on a failed run.
        dag = Dag("prop_to_engine", stop_unusable=True)

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
                **({"art_direction": self.art.id} if self.art is not None else {}),
            })
            rd = await resolve_route(self.workdir, prompt, self.budget, reference, single_candidate=self.single_candidate,
                                     plane=self.plane, token_ceiling=self.token_ceiling, max_images=self.max_images)
            self._governor = rd.governor
            g = rd.governor
            self._before = rd.prior
            if rd.prior[0] > 0 or rd.prior[1] > 0:  # a rerun: say why less is left (before any call when the route is reused)
                ctx.data(prior_spend={g.key("spent"): rd.prior[0], "images_used": rd.prior[1]})
                if self.announce is not None:
                    self.announce(prior_spend_line(g))
                if g.unit == "tokens" and self.min_call_tokens and g.remaining < self.min_call_tokens:
                    raise CeilingLeftRefused(
                        f"--token-ceiling {g.ceiling:,.0f}: earlier runs of this asset in this --out already used {g.spent:,.0f} "
                        f"tokens, so {g.remaining:,.0f} remain, and the largest single call this run must make can bill up to "
                        f"{self.min_call_tokens:,} tokens; the smallest ceiling that lets it continue is "
                        f"{math.ceil(g.spent) + self.min_call_tokens:,} tokens (nothing was generated in this run)")
            if rd.pipeline != "prop_to_engine":
                ctx.note(f"router preferred {rd.pipeline!r}; running prop_to_engine as requested")
            ctx.data(pipeline=rd.pipeline, complexity=rd.complexity, plan=rd.plan.label, reused=rd.reused,
                     **({"ceiling_usd": rd.plan.ceiling_usd} if rd.governor.unit == "usd" else
                        {"ceiling_tokens": rd.governor.token_ceiling, "max_images": rd.governor.max_images}))
            if rd.governor.unit == "tokens":  # the plan's own image need, against the user's image limit, before any image
                need = plan_images(rd)
                ctx.data(images_planned_min=need)
                short = rd.governor.max_images is not None and need > rd.governor.max_images - rd.governor.images_used
                refusal = (
                    f"plan {rd.plan.label!r} needs at least {need} image(s) (ortho best-of-{rd.plan.ortho_candidates if rd.needs_ortho else 0}, "
                    f"albedo best-of-{rd.plan.pbr_candidates} per material" + (", one paint-over" if rd.plan.paintover_loop else "")
                    + f") and {rd.governor.images_summary()} are counted; only the routing call was made. Rerun with "
                    "--single-candidate or ask the user for a higher --max-images")
                if short and self.record is not None and not self.record.fresh and (rd.prior[0] > 0 or rd.prior[1] > 0):
                    # A rerun with the design record on: whether it continues from a recorded design (whose images are
                    # cached and count none) is known once the authoring request is built. The propose step decides
                    # there, before any authoring call, and refuses as here when no design is recorded.
                    self._image_short = (need, rd.governor.max_images - rd.governor.images_used, refusal)
                    ctx.data(image_check="deferred to propose: rerun with a design record")
                elif short:
                    raise ImageLimitRefused(refusal)
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
                t = await ortho_tournament(concept_prompt(prompt, self.art), self.forge, plane=self.plane, governor=r.governor, uploader=self.up,
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
            context = {"asset_name": asset, "ortho_uri": ort.winner.uri if ort else None,
                       "style_uri": ctx["style_anchor"]["uri"] if ctx["style_anchor"] else None}
            if self.art is not None:  # the author brief (metered_author.build_author_request appends it to the request)
                context["art_direction"] = author_text(self.art)
            author = (self.author.bind(r.governor, self.budget, context, record=self.record) if isinstance(self.author, AgentAuthor)
                      else self.author)
            # Size the best-of-N by what a candidate is expected to cost (authoring from the measurements, plus grading);
            # admission then sequences the candidates under the ceiling by their worst case, waiting for settlements.
            n_plan = r.governor.plan.geometry_candidates
            profile = author.cost_profile(asset_name=asset) if hasattr(author, "cost_profile") else None
            g, k = grade_est(r.governor), r.governor.key
            if profile is None:
                n, sizing = r.governor.candidates("geometry", unit_cost=g), {"basis": "grading only (author not metered or no rates)"}
            else:
                expected, worst = profile
                n = r.governor.candidates("geometry", unit_cost=expected + g, admit_cost=worst + g)
                sizing = {"basis": "expected author cost + grading", k("expected_candidate"): expected + g,
                          k("worst_case_candidate"): worst + g, k("remaining"): r.governor.remaining}
            if n < n_plan:
                ctx.note(f"ceiling {r.governor.fmt(r.governor.ceiling)} sizes geometry best-of-{n_plan} down to {n} "
                         f"({r.governor.fmt(r.governor.remaining)} remaining)")
            if self._image_short is not None:  # deferred from the route step: a rerun short of images under --max-images
                need, left, refusal = self._image_short
                recorded = author.recorded_candidates(prompt, n=n, context=context) if hasattr(author, "recorded_candidates") else 0
                if recorded < n:  # a new design would need new images: refused before any authoring call, as on a first run
                    raise ImageLimitRefused(refusal.replace("only the routing call was made", "no authoring call was made")
                                            + f" (no design for this request is recorded in this --out: {recorded} of {n})")
                ctx.note(f"plan {r.plan.label!r} could ask for at least {need} image(s) and {left} remain under --max-images; "
                         f"continuing from the {n} design(s) recorded in this --out: images already generated for them are "
                         "reused and count none, and a material beyond the limit ships untextured and is named in the warnings")
                ctx.data(image_check={"need_at_least": need, "left": left, "recorded_designs": recorded})
            with_outcomes = getattr(author, "propose_with_outcomes", None)
            if with_outcomes is not None:
                result = await with_outcomes(prompt, budget=self.budget, n=n, context=context)
                scenes, outcomes = result.scenes, [o.to_dict() for o in result.outcomes]
            else:  # authors that do not record per-candidate outcomes
                scenes = await author.propose(prompt, budget=self.budget, n=n, context=context)
                outcomes = [{"index": i, "status": "produced"} for i in range(len(scenes))]
            lost = [o for o in outcomes if o["status"] != "produced"]
            if lost:
                ctx.note(f"{len(lost)} of {n} requested geometry candidates produced nothing: "
                         + "; ".join(f"#{o['index']} {o['status']}" + (f" ({o['detail']})" if o.get("detail") else "") for o in lost))
            ctx.data(n_planned=n_plan, n_requested=n, n_proposed=len(scenes), outcomes=outcomes, sizing=sizing,
                     materials=sorted({m.id for s in scenes for m in s.materials}))
            return Proposal(sm, list(scenes), asset, author)

        async def geometry(ctx: NodeCtx) -> Any:
            p: Proposal = ctx["propose"]
            r: Route = ctx["route"]
            ort = ctx["ortho"]
            t = await geometry_tournament(p.author, p.stage, prompt, plane=self.plane, governor=r.governor, budget=self.budget, uploader=self.up,
                                          context_uris=[ort.winner.uri] if ort else (), scenes=p.scenes)
            ctx.data(**t.to_dict())
            return t

        async def pbr(ctx: NodeCtx) -> Any:  # starts once geometry has a winner: textures for the design that ships
            p: Proposal = ctx["propose"]
            r: Route = ctx["route"]
            # The winning scene's materials only (the losers' would never ship), largest surface first, so that when the
            # allowance does not cover every material the visible ones get their textures and best-of-N.
            winner = p.stage.base_ir()
            mats = materials_by_area(p.stage.extract_meshes(), list(winner.materials))
            if not mats:
                ctx.skip("the winning scene has no materials")
            try:
                out = await synthesize_materials(SceneIR(asset_name=asset, materials=mats), self.forge, plane=self.plane, governor=r.governor,
                                                 prompt=prompt, uploader=self.up, cache=self.cache, size=self._texture_px(),
                                                 brief=texture_brief(self.art))
            except Exception as e:  # noqa: BLE001 - the pyramid will retry synthesis itself
                ctx.skip(f"speculative PBR synthesis failed ({type(e).__name__}: {e}); pyramid will retry")
            ctx.data(materials={k: v.tournament.to_dict() for k, v in out.items()},
                     **({"not_textured": nt} if (nt := {k: v.tournament.notes["not_textured"] for k, v in out.items()
                                                        if v.tournament.notes.get("not_textured")}) else {}))
            if nt:
                ctx.note("left untextured: " + "; ".join(f"{k}: {v}" for k, v in nt.items()))
            return out

        async def pyramid(ctx: NodeCtx) -> PyramidResult:
            p: Proposal = ctx["propose"]
            r: Route = ctx["route"]
            ort, st = ctx["ortho"], ctx["style_anchor"]
            res = await refine(p.stage, plane=self.plane, author=p.author, governor=r.governor, budget=self.budget, thresholds=self.th,
                               ortho_uri=ort.winner.uri if ort else None, style_uri=st["uri"] if st else None, max_rounds=self.max_rounds,
                               forge=self.forge, uploader=self.up, pool=self.pool, cache=self.cache, workdir=wd, fixlog=self.fixlog,
                               asset_id=asset, prompt=prompt, textures=ctx.late("pbr"), require_verified=self.require_verified, art=self.art)
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
            # Never verified on a preview plane or an uncalibrated judge, whatever the steps say.
            manifest["verified"] = (res.verified and not getattr(self.forge, "offline", False) and not self.preview_judging
                                    and bool(getattr(self.plane, "calibrated", True)))
            if self.preview_judging:  # what a person could mistake for a verified result says what it is
                manifest.update(preview_record(self.plane, self._governor, res))
            if "engine_import" in res.objects:
                manifest["validation"]["engine_import"] = res.objects["engine_import"]
            if "blender_asset_qa" in res.objects:  # a real worker ran it; a skipped run records nothing here (the step says why)
                manifest["validation"]["blender_asset_qa"] = res.objects["blender_asset_qa"]
            if "fbx_failed" in res.objects:  # the FBX could not be made: no FBX ships, the package stands, the manifest says why
                manifest["fbx"] = res.objects["fbx_failed"]
            if "fbx" in res.objects:  # pass C wrote <asset>.fbx into the package: the manifest that ships lists it with its real hash
                fbx_package.record(manifest, pkg, res.objects["fbx"])
            # What was checked and how it ended ships with the package. How long each step took is a fact about the run,
            # not the asset: it stays in the run report, so two runs of one scene still ship identical bytes.
            manifest["evidence"] = {"render_tier": res.render_tier, "lookdev": res.live_lookdev,
                                    "engine_parity": res.live_parity, "silhouette_iou": res.scores.get("engine_silhouette_iou"),
                                    "steps": [{"name": s.name, "status": s.status, "detail": s.detail} for s in res.steps]}
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
            if "fbx" in res.objects or "fbx_failed" in res.objects:  # the validator re-opens the FBX: binary, its textures relative and resolving, hash as recorded
                broken = [i for i in validate_package(pkg) if i.severity == "critical" and (i.code.startswith("pkg:fbx") or i.node_id == manifest["fbx"].get("file"))]
                if broken and manifest["fbx"].get("status") == "written":  # does not pass the validator: it does not ship, the package does
                    drop_fbx(manifest, pkg, "PackageValidation", f"{broken[0].code}: {broken[0].detail}")
                    for st in res.steps:
                        if st.name == "export_fbx":
                            st.status, st.detail = "output_failed", f"FBX failed package validation (not shipped): {broken[0].code} {broken[0].detail}"[:400]
                    manifest["evidence"]["steps"] = [{"name": x.name, "status": x.status, "detail": x.detail} for x in res.steps]
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
            return {"ok": res.ok, r.governor.key("spent"): r.governor.spent, "plan": r.governor.summary()}

        for name, fn, deps in (("route", route, ()), ("style_anchor", style, ("route",)), ("ortho", ortho, ("route", "style_anchor")),
                               ("propose", propose, ("route", "ortho", "style_anchor")), ("geometry", geometry, ("propose", "route", "ortho")),
                               ("pbr", pbr, ("propose", "route", "style_anchor", "geometry")), ("pyramid", pyramid, ("geometry", "propose", "route", "ortho", "style_anchor")),
                               ("package", package, ("pyramid", "propose")), ("inspector", inspector, ("package", "pyramid", "propose")),
                               ("report", report, ("inspector", "pyramid", "route"))):
            # The materials start once geometry has a winner (never for a run whose geometry failed); the pyramid starts
            # on geometry alone and awaits them only before pass B, so pass A overlaps them instead of waiting.
            dag.node(name, fn, deps, late=("pbr",) if name == "pyramid" else ())
        return dag

    async def run(self, prompt: str) -> DagReport:
        """Runs the DAG; ``report.extras`` carries outputs, gates, costs and every skipped step with its reason."""
        wd = self._run_dir()
        # The web-runtime verifier's browser starts now and comes up while the asset is authored and refined, instead
        # of being waited for after the export. It stays up for the next run only if this one succeeds.
        browser = web_three.prestart()
        ok = False
        try:
            rep = await self.build(prompt, run_dir=wd).run(max_parallel=self.max_parallel)
            ok = rep.ok
        finally:
            await browser.aclose(keep=ok)
        res: PyramidResult | None = rep.results.get("pyramid")
        route: Route | None = rep.results.get("route")
        skipped = [{"step": f"dag:{n.name}", "reason": n.reason} for n in rep.nodes.values() if n.status in ("skipped", "stopped") and n.reason]
        skipped += [{"step": f"pyramid:{s}", "reason": r} for s, r in (res.skipped if res else [])]
        outputs = {**(rep.results.get("package") or {}), **({"inspector": rep.results["inspector"]} if rep.results.get("inspector") else {})}
        cache = None if self.cache is None else asdict(self.cache.stats)
        rep.extras = {
            "prompt": prompt, "engine": self.budget.target_engine, "outputs": outputs, "skipped": skipped,
            "comparison": rep.nodes["route"].data.get("comparison"),
            "published": bool(rep.ok and rep.nodes["package"].data.get("published")),
            "candidate_mode": "single" if self.single_candidate else "governed",
            "geometry_candidates": {k: v for k, v in (rep.nodes["propose"].data if "propose" in rep.nodes else {}).items()
                                    if k in ("n_planned", "n_requested", "n_proposed", "outcomes", "sizing")},
            "budget": asdict(self.budget), "decision_backend": self.plane.backend_kind,
            "preview_gates_ok": bool(res and preview_ok(res, self.preview_judging) and "inspector" in outputs
                                     and not (self.preview_judging and getattr(self.plane, "hard_policy_flags", None))),
            "verified": bool(rep.ok and rep.nodes["package"].data.get("verified")),
            "gates_ok": bool(rep.ok and rep.nodes["package"].data.get("verified")),
            "scores": {"tier15": res.tier15, "tier1_ok": res.tier1_ok, **(res.scores if res else {})} if res else {},
            "cost": {"governor_usd": route.governor.spent if route else 0.0, "by_category": route.governor.by_category if route else {},
                     "receipts": route.governor.receipts() if route else [], "plane": self.plane.stats.summary(), "cache": cache, "plan": route.governor.summary() if route else None},
            "pyramid": res.to_dict() if res else None, "workdir": str(self.workdir)}
        gov = route.governor if route else getattr(self, "_governor", None)
        if self.record is not None:  # designs and repairs reused from the record (no call) and recorded in this run
            rep.extras["cost"]["design_record"] = self.record.summary()
        if gov is not None and self._before is not None and (self._before[0] > 0 or self._before[1] > 0):
            # a rerun on an asset with earlier spend: the ledger is the asset's; this is what this run added to it
            rep.extras["cost"]["this_run"] = {gov.key("spent"): gov.spent - self._before[0],
                                              "images": gov.images_used - self._before[1],
                                              "before": {gov.key("spent"): self._before[0], "images": self._before[1]}}
        if self.token_ceiling is not None:  # token ledger: no dollar figure anywhere, every figure names its unit
            c = rep.extras["cost"]
            del c["governor_usd"]
            c.update(spend_unit="tokens", price_applied=False, governor_tokens=gov.spent if gov else 0,
                     ceiling_tokens=self.token_ceiling, max_images=self.max_images, images=gov.images_used if gov else 0)
            c["plane"] = {**c["plane"], "cost_usd": None}
            if c.get("cache"):
                c["cache"] = {**c["cache"], "saved_cost_usd": None}
        if self.preview_judging:
            rep.extras["preview"] = preview_record(self.plane, gov, res)
            rep.extras["needs_review"] = True
        if self.art is not None:
            rep.extras["art_direction"] = self.art.manifest_record(budget=self.budget, asset_class="prop", texture_px=self._texture_px())
        report_path = wd / "report.json"
        rep.extras["report_path"] = str(report_path)
        data = rep.to_json()
        report_path.write_text(data)
        latest = wd / ".latest.json"
        latest.write_text(data)
        latest.replace(self.workdir / "report.json")
        return rep
