"""Texture a design from its geometry alone and package it: the flat package and the smart-surface package side by side.

    python benchmarks/smart_surfaces_demo.py DESIGN.json OUT_DIR [--brief "..."] [--engine godot_4] [--px 2048]
        [--render]   # also render both GLBs with the comparison harness's renderer (needs BLENDER_BIN)

Writes ``OUT_DIR/flat/package`` (what a run ships today without images: flat factors) and ``OUT_DIR/smart/package``
(each opaque material textured by ``kernel.smart_materials`` on the same per-material atlases), plus
``OUT_DIR/smart/record.json`` (class, words, masks, markings and timings per material, bytes per map). No model call,
no image generation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from antigravity3d.ir import IRPatch, SceneIR, UpsertMaterial
from antigravity3d.kernel import mesh_masks, smart_materials
from antigravity3d.kernel.coplanar import opaque_materials
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget


def build(design: Path, out: Path, *, smart: bool, brief: str, engine: str, px: int) -> dict:
    scene = SceneIR.model_validate(json.loads(design.read_text()))
    out.mkdir(parents=True, exist_ok=True)
    sm = StageManager.create(out / f"{scene.asset_name}_stage.usda", scene.asset_name)
    sm.apply_ir(scene)
    budget = EngineBudget(engine)
    meshes = sm.extract_meshes()
    t0 = time.perf_counter()
    bundle = run_export_pipeline(meshes, budget, scene.asset_name, opaque_materials=opaque_materials(scene))
    rec: dict = {"design": str(design), "asset": scene.asset_name, "parts": len(scene.nodes),
                 "triangles": int(sum(len(m.triangles) for m in meshes)), "export_s": round(time.perf_counter() - t0, 2)}
    if smart:
        t1 = time.perf_counter()
        layouts = mesh_masks.layouts_from_uvs(bundle.uvs, px)
        maps = smart_materials.texture_scene(scene.materials, layouts, meshes, brief=brief, asset_name=scene.asset_name)
        rec["texture_s"] = round(time.perf_counter() - t1, 2)
        tdir = (out / "textures").resolve()
        tdir.mkdir(exist_ok=True)
        ops, mats = [], {}
        for mid, mp in maps.items():
            paths = {}
            for kind, img in (("albedo", mp.albedo), ("orm", mp.orm), ("normal", mp.normal)):
                p = tdir / f"{mid}_{kind}.png"
                img.save(p, format="PNG", optimize=False)
                paths[kind] = str(p)
            mats[mid] = {**mp.record, "png_bytes": {k: os.path.getsize(v) for k, v in paths.items()}}
            m = next(x for x in scene.materials if x.id == mid)
            ops.append(UpsertMaterial(material=m.model_copy(update={"albedo_uri": paths["albedo"], "orm_uri": paths["orm"],
                                                                    "normal_uri": paths["normal"]})))
        if ops:
            sm.apply_patch(IRPatch(ops=ops))
        rec["materials"] = mats
    t2 = time.perf_counter()
    manifest = write_engine_package(sm, bundle, budget, out / "package", asset_name=scene.asset_name)
    rec["package_s"] = round(time.perf_counter() - t2, 2)
    rec["package_ok"] = manifest.ok
    rec["critical"] = [f"{i.code}: {i.detail}"[:200] for i in manifest.issues if i.severity == "critical"]
    rec["glb"] = str(out / "package" / f"{scene.asset_name}.glb")
    rec["glb_bytes"] = os.path.getsize(rec["glb"])
    (out / "record.json").write_text(json.dumps(rec, indent=1))
    return rec


async def render(glb: Path, out: Path) -> dict:
    from antigravity3d.adapters.blender_5_3.pool import BlenderPool
    from benchmarks import compare_eval as ce

    pool = BlenderPool(1, os.environ["BLENDER_BIN"], timeout=900)
    await pool.start()
    try:
        r = await ce.render_views(pool, glb, out / "renders")
    finally:
        await pool.stop()
    ce.contact_sheet(r["views"], out / "sheet.png")
    views = {Path(p).stem: p for p in r["views"]}
    ce.small_sheet(views, None, out / "sheet_small.png")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("design", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--brief", default="")
    ap.add_argument("--engine", default="godot_4")
    ap.add_argument("--px", type=int, default=2048)
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--only", choices=("flat", "smart"), default=None)
    a = ap.parse_args()
    for kind in ("flat", "smart"):
        if a.only and a.only != kind:
            continue
        rec = build(a.design, a.out / kind, smart=kind == "smart", brief=a.brief, engine=a.engine, px=a.px)
        print(kind, json.dumps({k: v for k, v in rec.items() if k != "materials"}))
        if a.render:
            t = time.perf_counter()
            asyncio.run(render(Path(rec["glb"]), a.out / kind))
            print(kind, "render_s", round(time.perf_counter() - t, 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
