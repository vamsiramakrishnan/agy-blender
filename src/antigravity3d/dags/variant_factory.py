"""VariantFactoryDAG: K look variants per asset as a USD variant set of material overrides.

Each look is a deterministic transform of the asset's base maps (mud, snow, scars, clean-up) and, when a forge
is supplied, a forge-generated decal overlay composited on the albedo. Every variant is validated and exported.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from pxr import Usd
from scipy import ndimage as ndi

from antigravity3d.dags.runner import Dag, DagReport, NodeCtx
from antigravity3d.genmedia.pbr_deriver import PBRMaps, delight, derive_pbr, edge_wear, height_from_albedo
from antigravity3d.ir import MaterialIR, SceneIR
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import validate_usd
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.cached import CachedForge
from antigravity3d.tactics.common import load_png, png_bytes

LOOKS = ("pristine", "muddy", "snow_dusted", "battle_scarred")
SET_NAME = "look"
BASE_RES = 256
OVERLAY_PROMPT = {"muddy": "mud splatter and dirt streaks", "snow_dusted": "patches of fresh snow", "battle_scarred": "scorch marks and deep scratches"}


def _seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:4], "big")


def _noise(shape: tuple[int, int], seed: int, sigma: float) -> np.ndarray:
    n = ndi.gaussian_filter(np.random.default_rng(seed).standard_normal(shape), sigma, mode="wrap")
    lo, hi = np.percentile(n, 5), np.percentile(n, 95)
    return np.clip((n - lo) / max(hi - lo, 1e-9), 0, 1)


def _f(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB"), np.float32) / 255.0


def _u(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(np.rint(a * 255.0), 0, 255).astype(np.uint8))


def _mix(a: np.ndarray, color: tuple[float, float, float], mask: np.ndarray, k: float) -> np.ndarray:
    return a * (1 - mask[..., None] * k) + np.array(color, np.float32) * (mask[..., None] * k)


def apply_look(look: str, maps: PBRMaps, seed: int = 0) -> PBRMaps:
    """Deterministic base-map transform for one look. Identical inputs give identical outputs."""
    alb, orm, nrm = _f(maps.albedo), _f(maps.orm), maps.normal
    h, w = alb.shape[:2]
    s = _seed(look, seed)
    if look == "pristine":
        alb = np.clip(_f(delight(maps.albedo)) * 1.06, 0, 1)
        orm[..., 1] = np.clip(orm[..., 1] * 0.8, 0.04, 1)
    elif look == "muddy":
        mask = np.clip((_noise((h, w), s, h / 12) - 0.35) * 2.2, 0, 1) * (0.5 + 0.5 * _noise((h, w), s + 1, h / 40))
        alb = _mix(alb, (0.30, 0.21, 0.13), mask, 0.8)
        orm[..., 1] = np.clip(orm[..., 1] + mask * 0.45, 0.04, 1)
        orm[..., 0] *= 1 - 0.2 * mask
    elif look == "snow_dusted":
        top = np.clip(height_from_albedo(maps.albedo) * 1.4 - 0.2, 0, 1)  # raised parts catch snow
        mask = np.clip((_noise((h, w), s, h / 16) - 0.3) * 2.0, 0, 1) * (0.4 + 0.6 * top)
        alb = _mix(alb, (0.93, 0.95, 0.98), mask, 0.9)
        orm[..., 1] = np.clip(orm[..., 1] * (1 - 0.5 * mask) + 0.55 * mask, 0.04, 1)
        orm[..., 2] *= 1 - mask
    elif look == "battle_scarred":
        alb = _f(edge_wear(maps.albedo, maps.normal, 0.9))
        img = Image.new("L", (w, h), 0)
        d, rng = ImageDraw.Draw(img), np.random.default_rng(s)
        for _ in range(max(6, h // 12)):
            x0, y0 = rng.integers(0, w), rng.integers(0, h)
            ang, ln = rng.uniform(0, np.pi), rng.uniform(h * 0.1, h * 0.35)
            d.line([x0, y0, x0 + np.cos(ang) * ln, y0 + np.sin(ang) * ln], fill=255, width=max(1, h // 128))
        scratch = ndi.gaussian_filter(np.asarray(img, np.float32) / 255.0, 0.8)
        soot = np.clip((_noise((h, w), s + 2, h / 10) - 0.5) * 2, 0, 1)
        alb = _mix(alb * (1 - 0.35 * soot[..., None]), (0.75, 0.74, 0.72), np.clip(scratch * 1.5, 0, 1), 0.7)
        orm[..., 1] = np.clip(orm[..., 1] + 0.3 * soot + 0.2 * scratch, 0.04, 1)
    else:
        raise ValueError(f"unknown look {look!r}; known: {LOOKS}")
    return PBRMaps(_u(alb), _u(orm), nrm)


def overlay_decal(albedo: Image.Image, decal_rgba: Image.Image, seed: int, count: int = 5) -> Image.Image:
    """Composite a forge decal (RGBA) at seeded positions / scales over the albedo."""
    base = albedo.convert("RGBA")
    rng = np.random.default_rng(seed)
    for _ in range(count):
        side = int(base.width * rng.uniform(0.15, 0.35))
        d = decal_rgba.resize((side, side)).rotate(float(rng.uniform(0, 360)), expand=False)
        a = d.getchannel("A").point(lambda v: int(v * 0.75))
        d.putalpha(a)
        base.alpha_composite(d, (int(rng.integers(0, base.width - side)), int(rng.integers(0, base.height - side))))
    return base.convert("RGB")


def _resolve(uri: str | None, root: Path) -> Path | None:
    if not uri:
        return None
    p = Path(uri)
    p = p if p.is_absolute() else root / p
    return p if p.is_file() else None


def base_maps(mat: MaterialIR, root: Path) -> PBRMaps:
    """The material's own maps when they exist on disk, otherwise a flat base colour with fine deterministic grain."""
    alb_p = _resolve(mat.albedo_uri, root)
    if alb_p is not None:
        albedo = Image.open(alb_p).convert("RGB").resize((BASE_RES, BASE_RES))
    else:
        grain = _noise((BASE_RES, BASE_RES), _seed(mat.id), 2.0)[..., None] * 0.12 - 0.06
        albedo = _u(np.clip(np.array(mat.base_color, np.float32)[None, None] + grain, 0, 1))
    derived = derive_pbr(albedo, metallic_hint=mat.metallic)
    orm_p, nrm_p = _resolve(mat.orm_uri, root), _resolve(mat.normal_uri, root)
    return PBRMaps(albedo, Image.open(orm_p).convert("RGB").resize(albedo.size) if orm_p else derived.orm,
                   Image.open(nrm_p).convert("RGB").resize(albedo.size) if nrm_p else derived.normal)


def _mean(img: Image.Image) -> list[float]:
    return [round(float(x), 4) for x in _f(img).reshape(-1, 3).mean(0)]


class VariantFactoryDAG:
    def __init__(self, *, workdir: Path | str, budget: EngineBudget, forge: Any = None, plane: Any = None, looks: tuple[str, ...] = LOOKS,
                 cache: Any = None, pool: Any = None, max_parallel: int = 2) -> None:
        self.out, self.budget, self.looks, self.plane, self.pool = Path(workdir), budget, tuple(looks), plane, pool
        self.forge = CachedForge(forge, cache) if (forge is not None and cache is not None) else forge
        self.max_parallel = max_parallel

    async def variants_for(self, asset: Path, out_dir: Path) -> dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        sm = await asyncio.to_thread(StageManager.open, asset)
        scene = sm.base_ir()
        name = scene.asset_name
        row: dict[str, Any] = {"asset": str(asset), "asset_name": name, "set": SET_NAME, "variants": [], "overlays": "forge" if self.forge else "skipped: no forge provided"}
        if not scene.materials:
            raise ValueError(f"{asset.name}: scene has no materials to vary")
        bases = {m.id: await asyncio.to_thread(base_maps, m, asset.parent) for m in scene.materials}
        decals: dict[str, Image.Image] = {}
        if self.forge is not None:
            for look, prompt in OVERLAY_PROMPT.items():
                if look in self.looks:
                    decals[look] = load_png((await self.forge.decal(prompt, size=256)).png_bytes).convert("RGBA")
        scenes: dict[str, SceneIR] = {}
        for look in self.looks:
            mats, stats = [], {}
            for m in scene.materials:
                maps = await asyncio.to_thread(apply_look, look, bases[m.id], _seed(name, m.id))
                if look in decals:
                    maps = PBRMaps(overlay_decal(maps.albedo, decals[look], _seed(look, m.id)), maps.orm, maps.normal)
                d = (out_dir / "textures" / look).resolve()
                d.mkdir(parents=True, exist_ok=True)
                paths = {}
                for kind, img in (("albedo", maps.albedo), ("orm", maps.orm), ("normal", maps.normal)):
                    (d / f"{m.id}_{kind}.png").write_bytes(png_bytes(img))
                    paths[kind] = str(d / f"{m.id}_{kind}.png")
                mats.append(m.model_copy(update={"albedo_uri": paths["albedo"], "orm_uri": paths["orm"], "normal_uri": paths["normal"]}))
                stats[m.id] = _mean(maps.albedo)
            scenes[look] = SceneIR(asset_name=name, nodes=scene.nodes, materials=mats)
            row["variants"].append({"look": look, "albedo_mean": stats})
        for look in self.looks:  # author all, then validate/export one by one (never leaving a selection behind)
            sm.add_variant(SET_NAME, look, scenes[look])
        for v in row["variants"]:
            tx = sm.select_variant(SET_NAME, v["look"])
            try:
                path = out_dir / f"{name}_{v['look']}.usda"
                sm.export(path)
                crit = [i for i in validate_usd(path) if i.severity == "critical"]
                comp = {m.id: m.albedo_uri for m in sm.scene_ir().materials}
            finally:
                sm.rollback(tx)
            v.update(export=str(path), validate_critical=[f"{i.code}@{i.node_id}" for i in crit],
                     composed_albedo=comp, status="ok" if not crit else "needs_attention")
        looks_path = out_dir / f"{name}_looks.usda"
        sm.flatten_to(looks_path)
        stage = Usd.Stage.Open(str(looks_path))
        row.update(looks_usd=str(looks_path), variant_names=stage.GetDefaultPrim().GetVariantSet(SET_NAME).GetVariantNames(),
                   status="ok" if all(v["status"] == "ok" for v in row["variants"]) else "needs_attention")
        return row

    def build(self, assets: list[Path | str]) -> Dag:
        dag = Dag("variant_factory")
        for i, a in enumerate(assets):
            p = Path(a)

            async def run(ctx: NodeCtx, p: Path = p, i: int = i) -> dict[str, Any]:
                row = await self.variants_for(p, self.out / f"{i:02d}_{p.stem}")
                ctx.data(variants=[v["look"] for v in row["variants"]], status=row["status"])
                return row

            dag.node(f"asset:{i:02d}_{p.stem}", run)
        return dag

    async def run(self, assets: list[Path | str]) -> DagReport:
        rep = await self.build(assets).run(max_parallel=self.max_parallel)
        rows = [rep.results.get(n.name) or {"asset": n.name, "status": "failed", "error": n.error or n.reason} for n in rep.nodes.values()]
        rep.extras = {"rows": rows, "looks": list(self.looks), "gates_ok": bool(rows) and all(r["status"] == "ok" for r in rows),
                      "skipped": [{"step": "overlays", "reason": r["overlays"]} for r in rows if str(r.get("overlays", "")).startswith("skipped")]}
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.json").write_text(rep.to_json())
        return rep
