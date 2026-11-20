"""EnvironmentKitDAG: N modular pieces sharing ONE trim sheet, assembled by reference into one USD stage.

trim_sheet || pieces -> map_trim -> export_pieces -> assemble (settle + spatial index) -> backdrop -> splat_import -> report
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom

from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.ir import MaterialIR, SceneIR
from antigravity3d.kernel.fast_ops import Hull, generate_colliders, tier1_qa
from antigravity3d.kernel.settle import settle
from antigravity3d.kernel.spatial import SpatialIndex
from antigravity3d.kernel.trim_atlas import TrimResult, TrimSheet, map_to_trim, validate_trim
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import validate_usd
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.governor import Governor
from antigravity3d.tactics.cached import CachedForge
from antigravity3d.tactics.package import write_package_usd

PIECE_CYCLE = ("wall_segment", "floor_tile", "pillar", "doorway")
STRIP_FOR_MATERIAL = {"concrete": "concrete", "stone": "concrete", "wood": "wood_planks", "iron": "painted_metal", "metal": "painted_metal",
                      "glass": "painted_metal", "plastic": "painted_metal"}
SPLAT_EXTS = {".ply", ".spz"}
SETTLE_LIFT_M = 0.35  # pieces are dropped in from this height so settle has real work to do
SHEET_MATERIAL = "kit_trim"


@dataclass
class Piece:
    name: str
    template: str
    scene: SceneIR
    meshes: list[MeshData]


@dataclass
class MappedPiece:
    piece: Piece
    results: list[TrimResult]
    issues: list[str]


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
                 max_parallel: int | None = None) -> None:
        forge = forge.with_governor(Governor(forge.governor.plan, journal=Path(workdir) / "spend.sqlite3"))
        self.forge = CachedForge(forge, cache, budget=budget) if cache is not None else forge
        self.author, self.out, self.budget, self.plane, self.pool = author, Path(workdir), budget, plane, pool
        self.sheet, self.max_parallel = sheet or TrimSheet.standard(sheet_resolution), max_parallel

    def build(self, prompt: str, n_pieces: int, splat: Path | str | None = None) -> Dag:
        out, sheet = self.out, self.sheet
        dag = Dag("environment_kit")

        async def trim_sheet(ctx: NodeCtx) -> dict[str, Any]:
            g = await self.forge.trim_sheet(f"{prompt}. {sheet.layout_prompt()}", size=sheet.resolution)
            out.mkdir(parents=True, exist_ok=True)
            (out / "trim_sheet.png").write_bytes(g.png_bytes)
            (out / "trim_sheet.json").write_text(sheet.to_json())
            return {"path": str(out / "trim_sheet.png"), "bytes": len(g.png_bytes)}

        async def pieces(ctx: NodeCtx) -> list[Piece]:
            res: list[Piece] = []
            for k in range(n_pieces):
                tpl = PIECE_CYCLE[k % len(PIECE_CYCLE)]
                name = f"{tpl.title().replace('_', '')}{k:02d}"
                scene = (await self.author.propose(f"{prompt} {tpl.replace('_', ' ')}", budget=self.budget, n=1,
                                                   context={"asset_name": name, "template": tpl, "seed": k}))[0]
                (out / "src").mkdir(parents=True, exist_ok=True)
                sm = StageManager.create(out / "src" / f"{name}.usda", name)
                sm.apply_ir(scene)
                res.append(Piece(name, tpl, scene, sm.extract_meshes()))
            ctx.data(pieces=[{"name": p.name, "template": p.template, "tris": sum(len(m.triangles) for m in p.meshes)} for p in res])
            return res

        async def map_trim(ctx: NodeCtx) -> list[MappedPiece]:
            mapped = []
            for p in ctx["pieces"]:
                mat_of = {n.id: n.material_id for n in p.scene.nodes}
                results, issues = [], []
                for m in p.meshes:
                    strip = STRIP_FOR_MATERIAL.get(mat_of.get(m.node_id) or "", "concrete")
                    r = await asyncio.to_thread(map_to_trim, m, sheet, strip)
                    results.append(r)
                    issues += [f"{p.name}/{m.node_id}: {i}" for i in validate_trim(r, sheet)]
                mapped.append(MappedPiece(p, results, issues))
            ctx.data(trim_issues=sum(len(m.issues) for m in mapped))
            return mapped

        async def export_pieces(ctx: NodeCtx) -> dict[str, str]:
            paths: dict[str, str] = {}
            (out / "pieces").mkdir(parents=True, exist_ok=True)
            mat = MaterialIR(id=SHEET_MATERIAL, albedo_uri="../trim_sheet.png", base_color=(0.8, 0.8, 0.8), roughness=0.8)
            for mp in ctx["map_trim"]:
                p = mp.piece
                hulls: list[Hull] = await asyncio.to_thread(generate_colliders, p.meshes, 4, asset_name=p.name)
                path = write_package_usd(out / "pieces" / f"{p.name}.usda", p.name, mp.results, {m.node_id: SHEET_MATERIAL for m in p.meshes}, [mat], hulls,
                                         self.budget.target_engine)
                paths[p.name] = str(path)
            return paths

        async def assemble(ctx: NodeCtx) -> dict[str, Any]:
            mapped: list[MappedPiece] = ctx["map_trim"]
            paths: dict[str, str] = ctx["export_pieces"]
            merged = [_merge(mp.piece.name, mp.piece.meshes) for mp in mapped]
            x, lifted = 0.0, []
            for m in merged:  # pieces side by side along +X, lifted so settle must bring them down
                w = float(m.points[:, 0].max() - m.points[:, 0].min())
                off = np.array([x - m.points[:, 0].min(), -float((m.points[:, 1].min() + m.points[:, 1].max()) / 2), SETTLE_LIFT_M])
                lifted.append(MeshData(m.node_id, "", m.points + off, m.triangles))
                x += w + 0.5
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

        for name, fn, deps in (("trim_sheet", trim_sheet, ()), ("pieces", pieces, ()), ("map_trim", map_trim, ("trim_sheet", "pieces")),
                               ("export_pieces", export_pieces, ("map_trim",)), ("assemble", assemble, ("export_pieces", "map_trim")),
                               ("backdrop", backdrop, ("assemble",)), ("splat_import", splat_import, ("backdrop",)),
                               ("report", report, ("assemble", "map_trim", "backdrop", "splat_import"))):
            dag.node(name, fn, deps)
        return dag

    async def run(self, prompt: str, n_pieces: int = 4, splat: Path | str | None = None) -> DagReport:
        rep = await self.build(prompt, n_pieces, splat).run(max_parallel=self.max_parallel)
        r = rep.results.get("report") or {}
        asm = rep.results.get("assemble") or {}
        mapped: list[MappedPiece] = rep.results.get("map_trim") or []
        skipped = [{"step": f"dag:{n.name}", "reason": n.reason} for n in rep.nodes.values() if n.status == "skipped" and n.reason and not n.blocked]
        rep.extras = {
            "prompt": prompt, "n_pieces": n_pieces, "kit_usd": asm.get("path"), "genmedia_receipts": self.forge.governor.receipts(), "trim_sheet": (rep.results.get("trim_sheet") or {}).get("path"),
            "pieces": rep.results.get("export_pieces") or {}, "vram": r.get("vram"), "settle_dz": asm.get("settle_dz"), "piece_min_z": r.get("piece_min_z"),
            "spatial_overview": asm.get("overview"), "backdrop": rep.results.get("backdrop"), "skipped": skipped,
            "trim_issues": [i for mp in mapped for i in mp.issues],
            "trim_reports": {mp.piece.name: [{"node": rr.node_id, "mapped_area_fraction": rr.report.mapped_area_fraction, "max_density_dev": rr.report.max_density_dev}
                                              for rr in mp.results if rr.report] for mp in mapped},
            "gates_ok": bool(rep.ok and asm and "error" not in (rep.results.get("backdrop") or {}) and not r.get("kit_validate_critical") and all(r.get("tier1_ok", {}).values()) and not any(mp.issues for mp in mapped))}
        (self.out / "report.json").write_text(rep.to_json())
        return rep
