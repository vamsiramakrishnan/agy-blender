"""EnvironmentKitDAG: N modular pieces sharing ONE trim sheet, assembled by reference into one USD stage.

trim_sheet || pieces -> map_trim -> kit_check
                                  -> export_pieces -> assemble (settle + spatial index) -> backdrop -> splat_import -> report
                                                   -> snap

Metric. A modular kit only snaps if its pieces share numbers, so the kit decides ONE ``KitMetric`` (grid, module, wall
height and thickness, door opening, pivot convention; ``kernel/kit_metric.py``) before any piece is authored and passes
it to every author in the piece's ``context["kit_metric"]``. The offline templates build to it; a live author is told it
in the request. ``pieces`` checks every piece against it (exact size, pivot, full faces, clear door opening, faces that
fit the trim strip) and a piece that is off-metric is a failed piece, with its numbers. ``kit_check`` is upstream's kit
check (EL ``kit_check.py``) computed on this kit's own meshes, with texel density within 25 % of the kit median as a
gate. ``snap`` places the pieces by the metric alone (a straight run, a doorway in it, floors under it, an L corner, a
closed room, a T junction) and measures every joint: a gap above 1 mm, a slit or an interpenetration fails the kit.

Spend. One ledger (a ``Governor`` journalled in ``spend.sqlite3``) covers the whole kit: the trim sheet image and every
authoring call. A live ``AgentAuthor`` is bound to that ledger and becomes a ``MeteredAuthor``, the same admitted path the
prop pipeline uses: each call reserves its worst case, waits for admission when other calls hold reservations, books the
real usage and leaves a receipt. The pieces are independent, so they are authored concurrently (at most ``author_parallel``
at a time); admission against the ceiling is the back-pressure.

Ceiling. A ``SpendPlan`` ceiling is the allowance of ONE asset. A kit is ``n_pieces`` assets plus one shared trim sheet, so
the kit's ledger gets ``plan.ceiling_usd * (n_pieces + 1)`` (``kit_plan``): one asset allowance per piece and one for the
sheet. Without this a kit of eight pieces would be admitted as if it were one asset and the later pieces would be refused
for a reason that has nothing to do with what they cost. The plan itself is otherwise unchanged and the rule is recorded
in the report under ``spend``. The journal persists in the work directory, so building into a directory that already holds
a kit counts the earlier spend against the new ceiling.

When a piece fails, a failure a second request can fix (an error, a timeout, an empty or invalid answer, or a valid answer
that is off the metric, whose second request states the numbers it missed) is requested once more on the same ledger; a cut-off or not-admitted piece is not, because a repeat would fail the same way. If any piece is
still missing the step fails and names every piece, with each piece's attempts recorded under ``attempts``.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom

from antigravity3d.art_direction import ArtDirection, author_text, png_bytes, texture_brief
from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.ir import MaterialIR, SceneIR
from antigravity3d.kernel.fast_ops import UVResult, Hull, generate_colliders, hull_limit, tier1_qa
from antigravity3d.kernel.kit_metric import KitMetric, PieceCheck, check_piece, kit_report, snap_test
from antigravity3d.kernel.settle import settle
from antigravity3d.kernel.spatial import SpatialIndex
from antigravity3d.kernel.trim_atlas import PAD_PX, TrimResult, TrimSheet, map_to_trim, validate_trim
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import validate_usd
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import AgentAuthor
from antigravity3d.tactics.cached import CachedForge
from antigravity3d.tactics.governor import BudgetExceeded, Governor, SpendPlan
from antigravity3d.tactics.package import write_package_usd

PIECE_CYCLE = ("wall_segment", "floor_tile", "pillar", "doorway")
STRIP_FOR_MATERIAL = {"concrete": "concrete", "stone": "concrete", "wood": "wood_planks", "iron": "painted_metal", "metal": "painted_metal",
                      "glass": "painted_metal", "plastic": "painted_metal"}
SPLAT_EXTS = {".ply", ".spz"}
SETTLE_LIFT_M = 0.35  # pieces are dropped in from this height so settle has real work to do
SHEET_MATERIAL = "kit_trim"
# Failures a second request can plausibly fix. "off_metric" is a complete, valid scene whose size, pivot or faces miss the
# kit's metric: its second request carries the violated numbers (context["kit_feedback"]), which a model can follow; the
# same single extra round bounds it, so a kit still makes at most 2 x n_pieces authoring calls.
RETRYABLE = ("error", "timed_out", "no_answer", "invalid", "off_metric")
COLLIDER_HULLS = 4  # hulls per kit piece (the export_pieces call to generate_colliders)
AUTHOR_PARALLEL = 8  # authoring calls in flight at once; the ceiling's admission is the real limit, this keeps one kit from opening unbounded connections


@dataclass
class Piece:
    name: str
    template: str
    scene: SceneIR
    meshes: list[MeshData]
    trims: list[TrimResult]
    check: PieceCheck


@dataclass
class MappedPiece:
    piece: Piece
    results: list[TrimResult]
    issues: list[str]


def kit_plan(plan: SpendPlan, n_pieces: int) -> SpendPlan:
    """The plan a kit of ``n_pieces`` is admitted under: the per-asset ceiling times (pieces + the shared trim sheet)."""
    if n_pieces < 1:
        raise ValueError(f"a kit needs at least one piece, got {n_pieces}")
    return replace(plan, ceiling_usd=plan.ceiling_usd * (n_pieces + 1))


class PiecesFailed(RuntimeError):
    """One or more pieces were not authored. The message names the outcome of every piece."""

    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = outcomes
        bad = [o for o in outcomes if o["status"] != "produced"]
        lines = "; ".join(f"#{o['index']} {o['piece']} {o['status']}" + (f" ({o['detail']})" if o["detail"] else "") for o in outcomes)
        super().__init__(f"{len(bad)} of {len(outcomes)} pieces were not produced: {lines}")


def _failure_status(exc: BaseException) -> str:
    """The outcome status of an authoring failure, in the vocabulary of ``MeteredAuthor``'s candidate outcomes."""
    if isinstance(exc, BudgetExceeded):
        return "not_admitted"
    try:
        from antigravity3d.tactics.metered_author import AuthorInvalid, AuthorNoAnswer, AuthorTimedOut, AuthorTruncated
    except ImportError:  # the live provider SDK is not installed: nothing here can have raised these
        return "error"
    for cls, status in ((AuthorTruncated, "truncated"), (AuthorTimedOut, "timed_out"), (AuthorNoAnswer, "no_answer"), (AuthorInvalid, "invalid")):
        if isinstance(exc, cls):
            return status
    return "error"


def _convex_groups(meshes: list[MeshData], limit: int = COLLIDER_HULLS) -> list[MeshData]:
    """The piece's parts as at most ``limit`` convex hull meshes for the collider generator: first merged wherever two
    form one convex body exactly (stacked courses, a jamb, a run of floor boards), then, while there are more than
    ``limit``, the touching pair whose hull adds the least empty volume (a relief strip joins the jamb it stands on). The collider
    generator itself merges the two SMALLEST hulls once there are more than it may keep, which joins the two base
    reliefs of a doorway across its opening and closes the door (tests/test_kit_metric.py sweeps a door through)."""
    from scipy.spatial import ConvexHull

    groups = [(np.asarray(m.points, float), m) for m in meshes if m.collider and len(m.points) >= 4]
    vol = [ConvexHull(p).volume for p, _ in groups]
    while len(groups) > 1:
        best = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                pts = np.vstack([groups[i][0], groups[j][0]])
                hv = ConvexHull(pts).volume
                added = hv - vol[i] - vol[j]
                apart = bool(((np.minimum(groups[i][0].max(0), groups[j][0].max(0)) - np.maximum(groups[i][0].min(0), groups[j][0].min(0))) < -1e-6).any())
                key = (apart, added)  # parts that touch first: a hull across a gap can close an opening however little it adds
                if best is None or key < best[0]:
                    best = (key, i, j, pts, hv)
        (_, added), i, j, pts, hv = best
        exact = added <= (vol[i] + vol[j]) * 1e-9 + 1e-12
        if not exact and len(groups) <= limit:
            break
        groups[i], vol[i] = (pts, groups[i][1]), hv
        del groups[j], vol[j]
    out = []
    for pts, m in groups:
        h = ConvexHull(pts)
        tri = h.simplices.copy()
        c = pts[h.vertices].mean(axis=0)
        flip = np.einsum("ij,ij->i", np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]]), pts[tri[:, 0]] - c) < 0
        tri[flip] = tri[flip][:, ::-1]
        out.append(MeshData(m.node_id, m.prim_path, pts, tri.astype(np.int32), m.material_id))
    return out


def ship_masks(meshes: list[MeshData]) -> list[np.ndarray]:
    """Which triangles of each part ship, one boolean mask per part in triangle order. Every triangle today. Parts that
    are stacked full-depth have contact faces between them that nothing can see (the top and bottom of every course);
    the exact test for those (a triangle completely covered by an opposite-facing coplanar face of another closed part)
    belongs to the prop export in ``kernel/fast_ops.py`` and plugs in here: the masks are applied per part BEFORE the
    parts are merged into the piece's one mesh, so a dropped face never needs to be found again in the merged mesh."""
    return [np.ones(len(m.triangles), bool) for m in meshes]


def _one_mesh(name: str, results: list[TrimResult], ship: list[np.ndarray] | None = None) -> UVResult:
    """A piece as ONE render mesh (all its parts share the kit's one trim material): a modular piece is one static mesh,
    one material and one draw call in an engine, and EL kit_check judges each Blender object as a piece. ``ship``: per
    part, the triangles to keep (``ship_masks``); a trim result's triangles are in its source mesh's order."""
    pts, tris, uvs, base = [], [], [], 0
    for k, r in enumerate(results):
        pts.append(r.points)
        keep = np.asarray(r.triangles) if ship is None else np.asarray(r.triangles)[ship[k]]
        tris.append(keep + base)
        uvs.append(r.uvs)
        base += len(r.points)
    first = results[0]
    return replace(UVResult(first.node_id, first.points, first.triangles, first.uvs, first.vmapping, first.texel_density, first.stretch_mean,
                            first.stretch_max, first.resolution, first.atlas_id, first.fitted, first.chart_source, first.angle_distortion),
                   node_id=name, points=np.vstack(pts), triangles=np.vstack(tris).astype(np.int32), uvs=np.vstack(uvs),
                   vmapping=np.arange(base), fitted=all(r.fitted for r in results))


def _merge(name: str, meshes: list[MeshData]) -> MeshData:
    pts, tris, base = [], [], 0
    for m in meshes:
        pts.append(m.points)
        tris.append(np.asarray(m.triangles) + base)
        base += len(m.points)
    return MeshData(name, "", np.vstack(pts), np.vstack(tris).astype(np.int32))


class EnvironmentKitDAG:
    def __init__(self, *, forge: Any, author: Any, workdir: Path | str, budget: EngineBudget, plane: Any = None, pool: Any = None,
                 cache: Any = None, sheet: TrimSheet | None = None, sheet_resolution: int = 2048,
                 max_parallel: int | None = None, author_parallel: int = AUTHOR_PARALLEL, art_direction: ArtDirection | None = None,
                 metric: KitMetric | None = None) -> None:
        """``art_direction``: a named style. It lowers each piece's triangle budget where its look calls for it (never
        raises it), sets the trim sheet's size and palette, the pieces' shading crease, and adds its briefs to the trim
        sheet prompt and the piece author's request. None changes nothing."""
        if author_parallel < 1:
            raise ValueError("author_parallel must be at least 1")
        self.art = art_direction
        if art_direction is not None:
            budget = art_direction.budget(budget, "kit")
            if art_direction.texture_px_kit is not None:
                sheet_resolution = art_direction.texture_px(budget.target_engine, "kit", sheet_resolution)
        self._forge_in, self._cache = forge, cache
        self.forge = forge  # replaced by the kit's own ledger-bound forge in _govern, once the piece count is known
        self.author, self.out, self.budget, self.plane, self.pool = author, Path(workdir), budget, plane, pool
        self.sheet, self.max_parallel, self.author_parallel = sheet or TrimSheet.standard(sheet_resolution), max_parallel, author_parallel
        # The kit's one metric, decided here before any piece exists. Its face limit is the narrowest strip this kit's
        # pieces map to, minus the mapper's padding, at this sheet's resolution: a face that fits is never shrunk.
        strips = [self.sheet.strip(s) for s in sorted(set(STRIP_FOR_MATERIAL.values()))]
        pad_m = [PAD_PX / self.sheet.density_px_per_m(s) for s in strips]
        self.metric = (metric or KitMetric.interior()).fitted_to(s.world_height_m - 2 * p for s, p in zip(strips, pad_m))
        self.governor: Governor | None = None
        self.base_plan: SpendPlan = forge.governor.plan
        self.authoring: list[dict[str, Any]] = []  # one outcome per piece, filled by the pieces step (also when it fails)
        self.checks: dict[str, PieceCheck | None] = {}  # the metric check of every piece's last answer

    def _govern(self, n_pieces: int) -> Any:
        """Bind the forge and a live author to ONE ledger sized for the whole kit. Returns the author to call."""
        plan = kit_plan(self._forge_in.governor.plan, n_pieces)
        self.base_plan = self._forge_in.governor.plan
        self.governor = Governor(plan, journal=self.out / "spend.sqlite3")
        forge = self._forge_in.with_governor(self.governor)
        self.forge = CachedForge(forge, self._cache, budget=self.budget) if self._cache is not None else forge
        # Same rule as the prop pipeline: a live AgentAuthor authors through the metered, admitted path. Other authors
        # (the offline template author, injected test doubles) spend nothing and are used as given.
        context = {"art_direction": author_text(self.art)} if self.art is not None else {}
        return self.author.bind(self.governor, self.budget, context) if isinstance(self.author, AgentAuthor) else self.author

    def build(self, prompt: str, n_pieces: int, splat: Path | str | None = None) -> Dag:
        out, sheet = self.out, self.sheet
        self._author = self._govern(n_pieces)
        dag = Dag("environment_kit")

        async def trim_sheet(ctx: NodeCtx) -> dict[str, Any]:
            brief = texture_brief(self.art)
            g = await self.forge.trim_sheet(f"{prompt}. {sheet.layout_prompt()}" + (f" {brief}" if brief else ""), size=sheet.resolution)
            out.mkdir(parents=True, exist_ok=True)
            data = g.png_bytes
            if self.art is not None:  # the sheet as it ships: the art direction's size and palette
                from PIL import Image
                import io

                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                    done = self.art.finish_map(im, "albedo", sheet.resolution)
                    data = data if done is im else png_bytes(done)
            (out / "trim_sheet.png").write_bytes(data)
            (out / "trim_sheet.json").write_text(sheet.to_json())
            return {"path": str(out / "trim_sheet.png"), "bytes": len(data)}

        metric = self.metric

        async def pieces(ctx: NodeCtx) -> list[Piece]:
            author = self._author
            gate = asyncio.Semaphore(self.author_parallel)
            names = [(PIECE_CYCLE[k % len(PIECE_CYCLE)], f"{PIECE_CYCLE[k % len(PIECE_CYCLE)].title().replace('_', '')}{k:02d}") for k in range(n_pieces)]

            feedback: dict[int, str] = {}  # piece -> what its first answer missed, stated in its second request
            checks: dict[int, PieceCheck] = {}  # piece -> the metric check of its latest answer that built

            async def attempt(k: int) -> tuple[SceneIR | None, dict[str, Any]]:
                tpl, name = names[k]
                outcome: dict[str, Any] = {"index": k, "piece": name, "template": tpl, "status": "produced", "detail": "", "finish_reason": None,
                                           "usage": None, "admission_wait_s": 0.0, "reserved_usd": None}
                try:
                    async with gate:
                        context = {"asset_name": name, "template": tpl, "seed": k, "kit_metric": metric}
                        if k in feedback:
                            context["kit_feedback"] = feedback[k]
                        call = dict(budget=self.budget, n=1, context=context)
                        with_outcomes = getattr(author, "propose_with_outcomes", None)
                        if with_outcomes is not None:  # the metered author: its recorded outcome carries usage, wait and reservation
                            result = await with_outcomes(f"{prompt} {tpl.replace('_', ' ')}", **call)
                            scenes, recorded = result.scenes, result.outcomes[0].to_dict()
                            outcome.update({key: recorded[key] for key in ("status", "detail", "finish_reason", "usage", "admission_wait_s", "reserved_usd")})
                        else:
                            scenes = await author.propose(f"{prompt} {tpl.replace('_', ' ')}", **call)
                except Exception as exc:  # noqa: BLE001 - recorded per piece; the step fails below with all of them listed
                    text = " ".join(str(exc).split())
                    status = _failure_status(exc)
                    outcome.update(status=status, detail=text if status != "error" else f"{type(exc).__name__}: {text}",
                                   finish_reason=getattr(exc, "finish_reason", None), usage=getattr(exc, "usage", None))
                    return None, outcome
                if not scenes:  # an author that returns nothing and raises nothing used to surface as an IndexError
                    outcome.update(status="no_answer", detail="the author returned no scene")
                    return None, outcome
                built = await asyncio.to_thread(build, k, scenes[0])
                if isinstance(built, str):  # a scene that does not build is that piece's outcome
                    outcome.update(status="invalid", detail=built)
                    return None, outcome
                checks[k] = built.check
                if not built.check.ok:
                    outcome.update(status="off_metric", detail=f"{name} ({tpl}): " + "; ".join(built.check.errors))
                    feedback[k] = "; ".join(built.check.errors)
                    return None, outcome
                return built, outcome

            def build(k: int, scene: SceneIR) -> Piece | str:
                """Stage, meshes, trim mapping and the metric check of one answer (deterministic, no model)."""
                tpl, name = names[k]
                try:
                    sm = StageManager.create(out / "src" / f"{name}.usda", name)
                    sm.apply_ir(scene)
                    meshes = sm.extract_meshes()
                    mat_of = {n.id: n.material_id for n in scene.nodes}
                    trims = [map_to_trim(m, sheet, STRIP_FOR_MATERIAL.get(mat_of.get(m.node_id) or "", "concrete")) for m in meshes]
                    return Piece(name, tpl, scene, meshes, trims, check_piece(name, tpl, meshes, metric, trims))
                except Exception as exc:  # noqa: BLE001
                    return f"{type(exc).__name__}: {' '.join(str(exc).split())}"

            (out / "src").mkdir(parents=True, exist_ok=True)
            # Every piece is awaited even when one fails, so the report names what happened to all of them and their
            # receipts are complete. Results are indexed by piece number, never by completion order.
            results = list(await asyncio.gather(*(attempt(k) for k in range(n_pieces))))
            tries: list[list[dict[str, Any]]] = [[o] for _, o in results]
            # One more attempt, once, for the failures a second request can fix (a transport error, an empty or invalid
            # answer, a timeout). A truncated piece is not asked again: the same request would be cut off again and billed
            # again. A not_admitted piece is not asked again either: admission already waited for the ledger, so a second
            # request cannot fit where the first could not. The retry goes through the same admitted path and ledger, so the
            # ceiling can still refuse it, and that refusal is then the piece's final outcome. Never a loop.
            again = [k for k, (scene, o) in enumerate(results) if scene is None and o["status"] in RETRYABLE]
            if again:
                for k, r in zip(again, await asyncio.gather(*(attempt(k) for k in again))):
                    results[k] = r
                    tries[k].append(r[1])
            for k, (scene, o) in enumerate(results):
                o["attempts"] = [{key: t[key] for key in ("status", "detail", "finish_reason", "usage", "admission_wait_s", "reserved_usd")} for t in tries[k]]
            res: list[Piece] = [piece for piece, _ in results if piece is not None]
            self.checks = {names[k][1]: checks.get(k) for k in range(n_pieces)}
            self.authoring = [outcome for _, outcome in results]
            ctx.data(authoring=self.authoring)
            # Every later step (trim mapping, export, assembly, the kit's settle and spatial index) needs all pieces: a kit
            # with a hole in it is a different kit, so one piece that is missing fails the step instead of continuing with fewer.
            if len(res) != n_pieces:
                raise PiecesFailed(self.authoring)
            ctx.data(metric=metric.to_dict(), pieces=[{"name": p.name, "template": p.template, "tris": sum(len(m.triangles) for m in p.meshes),
                                                       "dims_m": p.check.dims_m} for p in res])
            return res

        async def map_trim(ctx: NodeCtx) -> list[MappedPiece]:
            mapped = []
            for p in ctx["pieces"]:  # mapped in `pieces`, where the metric check needs the texel densities
                issues = [f"{p.name}/{r.node_id}: {i}" for r in p.trims for i in validate_trim(r, sheet)]
                mapped.append(MappedPiece(p, list(p.trims), issues))
            ctx.data(trim_issues=sum(len(m.issues) for m in mapped))
            return mapped

        async def export_pieces(ctx: NodeCtx) -> dict[str, str]:
            paths: dict[str, str] = {}
            (out / "pieces").mkdir(parents=True, exist_ok=True)
            mat = MaterialIR(id=SHEET_MATERIAL, albedo_uri="../trim_sheet.png", base_color=(0.8, 0.8, 0.8), roughness=0.8)
            for mp in ctx["map_trim"]:
                p = mp.piece
                hulls: list[Hull] = await asyncio.to_thread(generate_colliders, _convex_groups(p.meshes), COLLIDER_HULLS, asset_name=p.name,
                                                            limit=hull_limit(self.budget.target_engine))
                path = write_package_usd(out / "pieces" / f"{p.name}.usda", p.name, [_one_mesh(p.name, mp.results, ship_masks(p.meshes))], {p.name: SHEET_MATERIAL}, [mat], hulls,
                                         self.budget.target_engine,
                                         crease_deg=self.art.crease_deg() if self.art is not None and self.art.shading_crease_deg is not None else None)
                paths[p.name] = str(path)
            return paths

        async def assemble(ctx: NodeCtx) -> dict[str, Any]:
            mapped: list[MappedPiece] = ctx["map_trim"]
            paths: dict[str, str] = ctx["export_pieces"]
            merged = [_merge(mp.piece.name, mp.piece.meshes) for mp in mapped]
            x, lifted = 0.0, []
            g = metric.grid_m
            for m in merged:  # pieces side by side along +X, one grid cell apart, lifted so settle must bring them down
                lo = m.points.min(0)
                w = float(m.points[:, 0].max() - lo[0])
                off = np.array([x - lo[0], -lo[1], SETTLE_LIFT_M])  # each piece's envelope starts on a grid point
                lifted.append(MeshData(m.node_id, "", m.points + off, m.triangles))
                x += np.ceil(w / g - 1e-9) * g + g
            sr = settle(lifted, {m.node_id for m in lifted})
            placed = sr.apply(lifted)
            stage = Usd.Stage.CreateNew(str(out / "kit.usda"))
            root = UsdGeom.Xform.Define(stage, "/Kit")
            stage.SetDefaultPrim(root.GetPrim())
            UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
            UsdGeom.SetStageMetersPerUnit(stage, 1.0)
            offsets = {}
            for m0, m1 in zip(merged, placed):
                d = (m1.points.mean(axis=0) - m0.points.mean(axis=0))
                prim = UsdGeom.Xform.Define(stage, f"/Kit/{m0.node_id}")
                prim.AddTranslateOp().Set(Gf.Vec3d(*map(float, d)))
                prim.GetPrim().GetReferences().AddReference(f"./pieces/{m0.node_id}.usda")
                offsets[m0.node_id] = [round(float(v), 4) for v in d]
            stage.GetRootLayer().Save()
            idx = SpatialIndex(placed)
            ctx.data(settle_converged=sr.converged, settle_iterations=sr.iterations, offsets=offsets)
            return {"stage": stage, "path": str(out / "kit.usda"), "placed": placed, "overview": idx.overview(), "settle_dz": {k: v.dz for k, v in sr.nodes.items()}}

        async def backdrop(ctx: NodeCtx) -> dict[str, Any]:
            if splat is None:
                ctx.skip("no Gaussian-splat backdrop requested")
            p = Path(splat)
            if p.suffix.lower() not in SPLAT_EXTS or not p.is_file():  # user error: report it, but do not sink the kit
                err = f"unsupported splat format {p.suffix!r} (expected {sorted(SPLAT_EXTS)})" if p.suffix.lower() not in SPLAT_EXTS else f"splat file not found: {p}"
                ctx.note(err)
                return {"error": err}
            stage: Usd.Stage = ctx["assemble"]["stage"]
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            prim = UsdGeom.Xform.Define(stage, "/Kit/Backdrop").GetPrim()
            Usd.ModelAPI(prim).SetAssetIdentifier(Sdf.AssetPath(str(p.resolve())))
            prim.CreateAttribute("agy:splatAsset", Sdf.ValueTypeNames.Asset, custom=True).Set(Sdf.AssetPath(str(p.resolve())))
            meta = {"format": p.suffix.lower().lstrip("."), "bytes": p.stat().st_size, "sha256": digest, "imported": False,
                    "note": "referenced, not imported: splat import/render needs Blender"}
            prim.SetCustomDataByKey("agy:splat", meta)
            stage.GetRootLayer().Save()
            return meta

        async def splat_import(ctx: NodeCtx) -> Any:
            if ctx["backdrop"] is None or "error" in ctx["backdrop"]:
                ctx.skip("no valid backdrop registered")
            if self.pool is None or getattr(self.pool, "fake", False):
                ctx.skip("splat import/render requires a real Blender pool (none provided)" if self.pool is None
                         else "splat import/render requires a real Blender pool (pool is fake)")
            r = await self.pool.call("import_splat", path=str(Path(splat).resolve()))  # type: ignore[arg-type]
            ctx.data(**{k: v for k, v in r.items() if k != "image"})
            return r

        async def report(ctx: NodeCtx) -> dict[str, Any]:
            mapped: list[MappedPiece] = ctx["map_trim"]
            rep = next(r.report for mp in mapped for r in mp.results if r.report is not None)
            v = rep.vram_estimate(n_props=len(mapped))
            vc = rep.vram_estimate(n_props=len(mapped), compressed=True)
            kit = Usd.Stage.Open(ctx["assemble"]["path"])
            crit = [i for i in validate_usd(kit) if i.severity == "critical"]
            cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
            minz = {c.GetName(): round(cache.ComputeWorldBound(c).ComputeAlignedRange().GetMin()[2], 4) for c in kit.GetDefaultPrim().GetChildren() if c.GetName() != "Backdrop"}
            t1 = {mp.piece.name: tier1_qa(mp.piece.meshes, self.budget).ok for mp in mapped}
            return {"vram": {"shared_bytes": v.shared_bytes, "unique_bytes": v.unique_bytes, "ratio": v.ratio, "saved_bytes": v.saved_bytes,
                             "compressed": {"shared_bytes": vc.shared_bytes, "unique_bytes": vc.unique_bytes, "ratio": vc.ratio}},
                    "kit_validate_critical": [f"{i.code}@{i.node_id}" for i in crit], "piece_min_z": minz, "tier1_ok": t1}

        async def kit_check(ctx: NodeCtx) -> dict[str, Any]:
            res = kit_report([mp.piece.check for mp in ctx["map_trim"]], metric)
            ctx.data(kit_check_pass=res["pass"], texel_spread=res["texel_spread_from_median"])
            if not res["pass"]:
                ctx.note("kit check failed: " + "; ".join(f"{n}: {e}" for n, p in res["pieces"].items() for e in p["errors"]))
            return res

        async def snap(ctx: NodeCtx) -> dict[str, Any]:
            res = await asyncio.to_thread(snap_test, metric, {p.name: (p.template, p.meshes) for p in ctx["pieces"]})
            # the assemblies as instanced references to the exported pieces, to look at
            stage = Usd.Stage.CreateNew(str(out / "snap_test.usda"))
            root = UsdGeom.Xform.Define(stage, "/SnapTest")
            stage.SetDefaultPrim(root.GetPrim())
            UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
            UsdGeom.SetStageMetersPerUnit(stage, 1.0)
            for lname, lay in res["layouts"].items():
                UsdGeom.Xform.Define(stage, f"/SnapTest/{lname}")
                for k, pl in enumerate(lay["placements"]):
                    x = UsdGeom.Xform.Define(stage, f"/SnapTest/{lname}/{pl['piece']}_{k:02d}")
                    x.AddTranslateOp().Set(Gf.Vec3d(*pl["translate"]))
                    x.AddRotateZOp().Set(float(pl["rot"]))
                    x.GetPrim().GetReferences().AddReference(f"./pieces/{pl['piece']}.usda")
                    x.GetPrim().SetInstanceable(True)
            stage.GetRootLayer().Save()
            res["path"] = str(out / "snap_test.usda")
            ctx.data(snap_pass=res["pass"], **{f"{k}_max_gap_m": v["max_gap_m"] for k, v in res["layouts"].items()})
            if not res["pass"]:
                ctx.note("snap test failed: " + "; ".join(p for v in res["layouts"].values() for p in v["problems"]))
            return res

        for name, fn, deps in (("trim_sheet", trim_sheet, ()), ("pieces", pieces, ()), ("map_trim", map_trim, ("trim_sheet", "pieces")),
                               ("kit_check", kit_check, ("map_trim",)), ("snap", snap, ("pieces", "export_pieces")),
                               ("export_pieces", export_pieces, ("map_trim",)), ("assemble", assemble, ("export_pieces", "map_trim")),
                               ("backdrop", backdrop, ("assemble",)), ("splat_import", splat_import, ("backdrop",)),
                               ("report", report, ("assemble", "map_trim", "backdrop", "splat_import"))):
            dag.node(name, fn, deps)
        return dag

    async def run(self, prompt: str, n_pieces: int = 4, splat: Path | str | None = None) -> DagReport:
        dag = self.build(prompt, n_pieces, splat)
        profile = getattr(self._author, "cost_profile", None)
        if profile is not None and profile(asset_name="Piece00") is None:  # refuse before the first image or piece is paid for
            from antigravity3d.models import tier

            role = self.governor.plan.builder_role
            raise BudgetExceeded(f"live kit authoring is metered and no text prices are configured for {tier(role).model!r} (the {role!r} role that authors the pieces): "
                                 f"set AGY3D_TEXT_RATES_JSON to a JSON object mapping {tier(role).model!r} to [USD per million input tokens, USD per million output tokens]; "
                                 f"nothing was requested or spent")
        rep = await dag.run(max_parallel=self.max_parallel)
        r = rep.results.get("report") or {}
        asm = rep.results.get("assemble") or {}
        mapped: list[MappedPiece] = rep.results.get("map_trim") or []
        receipts = self.governor.receipts()
        skipped = [{"step": f"dag:{n.name}", "reason": n.reason} for n in rep.nodes.values() if n.status == "skipped" and n.reason and not n.blocked]
        rep.extras = {
            "prompt": prompt, "n_pieces": n_pieces, "kit_usd": asm.get("path"), "genmedia_receipts": [r for r in receipts if r["category"].startswith("image.")],
            "authoring": {"pieces": self.authoring, "receipts": [r for r in receipts if r["category"].startswith("author.")], "parallel": self.author_parallel,
                          "metered": hasattr(self._author, "cost_profile")},
            "spend": {"rule": "ceiling = per-asset plan ceiling x (n_pieces + 1 shared trim sheet), one ledger for images and authoring",
                      "plan": self.base_plan.label, "per_asset_ceiling_usd": self.base_plan.ceiling_usd, "ceiling_usd": self.governor.plan.ceiling_usd,
                      "spent_usd": self.governor.spent, "by_category": self.governor.by_category, "summary": self.governor.summary()},
            "trim_sheet": (rep.results.get("trim_sheet") or {}).get("path"),
            "pieces": rep.results.get("export_pieces") or {}, "vram": r.get("vram"), "settle_dz": asm.get("settle_dz"), "piece_min_z": r.get("piece_min_z"),
            "spatial_overview": asm.get("overview"), "backdrop": rep.results.get("backdrop"), "skipped": skipped,
            "trim_issues": [i for mp in mapped for i in mp.issues],
            "metric": self.metric.to_dict(), "metric_checks": {n: (c.to_dict() if c else None) for n, c in self.checks.items()},
            "kit_check": rep.results.get("kit_check"), "snap": rep.results.get("snap"),
            "trim_reports": {mp.piece.name: [{"node": rr.node_id, "mapped_area_fraction": rr.report.mapped_area_fraction, "max_density_dev": rr.report.max_density_dev}
                                              for rr in mp.results if rr.report] for mp in mapped},
            "gates_ok": bool(rep.ok and asm and "error" not in (rep.results.get("backdrop") or {}) and not r.get("kit_validate_critical") and all(r.get("tier1_ok", {}).values()) and not any(mp.issues for mp in mapped)
                            and bool((rep.results.get("kit_check") or {}).get("pass")) and bool((rep.results.get("snap") or {}).get("pass")))}
        if self.art is not None:
            record = self.art.manifest_record(budget=self.budget, asset_class="kit", texture_px=self.sheet.resolution)
            if self.art.texture_filter is not None:  # a kit ships USD only, and UsdUVTexture has no filter input
                record["effective"]["texture_filter"] = None
                record["not_applied"] = [f"texture_filter {self.art.texture_filter}: kit pieces are USD, which cannot carry a sampler"]
            rep.extras["art_direction"] = record
        (self.out / "report.json").write_text(rep.to_json())
        return rep
