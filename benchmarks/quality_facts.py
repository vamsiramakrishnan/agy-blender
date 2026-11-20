#!/usr/bin/env python3
"""Quality facts behind the report's UV and LOD tables: what the export pipeline produces, not how long it takes.

    python benchmarks/quality_facts.py > benchmarks/results/quality.jsonl

One JSON line per subject: the five offline template props and the benchmark's computed UV spheres (radius 0.5 m).
For each: UV area and angle distortion of the worst part, the lowest achieved texel density, where the charts
came from, and the asset's LOD chain (triangles per level, the largest screen fraction each level is shown at,
and the measured deviation of the worst part).

It runs against whichever tree is first on ``PYTHONPATH``, so the same script measures an older commit:
``git worktree add <dir> <commit>``, then ``PYTHONPATH=<dir>/src python benchmarks/quality_facts.py``. Fields a
tree does not have (angle distortion, chart source and screen fractions before the fixes) are null, and before
the fixes the deviation is the simplifier's own estimate, not a measured distance.
No network, no model calls; a few seconds.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import numpy as np
import trimesh

from antigravity3d.kernel import fast_ops
from antigravity3d.kernel.types import MeshData
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import TemplateAuthor

PROMPTS = ("wooden crate with iron brackets", "rusty metal barrel", "oak dining table", "iron street lamp post", "wooden storage shelf")
SPHERES = (500, 2000, 10000)


def facts(subject: str, meshes: list, budget: EngineBudget) -> dict:
    bundle = fast_ops.run_export_pipeline(meshes, budget, "Asset")
    lods = bundle.lods
    levels = max(len(v) for v in lods.values())
    at = lambda i: [v[i] for v in lods.values() if i < len(v)]  # noqa: E731 - the parts present at level i
    angle = [getattr(u, "angle_distortion", None) for u in bundle.uvs]
    fractions = [max((getattr(lod, "screen_fraction", None) for lod in at(i)), key=lambda f: -1.0 if f is None else f) for i in range(levels)]
    return {
        "subject": subject, "parts": len(meshes), "triangles": int(sum(len(m.triangles) for m in meshes)),
        "uv_area_distortion_worst_part": round(max(u.stretch_mean for u in bundle.uvs), 4),
        "uv_angle_distortion_worst_part": None if None in angle else round(max(angle), 4),
        "texel_density_px_per_m_lowest_part": round(min(u.texel_density for u in bundle.uvs), 1),
        "requested_density_reached": all(u.fitted for u in bundle.uvs),
        "chart_source": sorted({getattr(u, "chart_source", None) or "not recorded" for u in bundle.uvs}),
        "lod_triangles": [int(sum(lod.tri_count for lod in at(i))) for i in range(levels)],
        "lod_parts_present": [sum(1 for lod in at(i) if lod.tri_count) for i in range(levels)],
        "lod_shown_at_or_below_screen_fraction": [None if f is None else round(float(f), 4) for f in fractions],
        "lod_deviation_m_worst_part": [round(max(lod.error_m for lod in at(i)), 5) for i in range(levels)],
    }


def sphere(target_triangles: int) -> MeshData:
    """The offline benchmark's UV sphere: radius 0.5 m, resting on z = 0, closest triangle count to the target."""
    def make(bands: int):
        return trimesh.creation.uv_sphere(radius=0.5, count=[2 * bands, bands])
    best = make(min(range(3, 120), key=lambda b: abs(len(make(b).faces) - target_triangles)))
    return MeshData("ball", "/ball", np.asarray(best.vertices + np.array([0, 0, 0.5]), np.float64), np.asarray(best.faces, np.int32), "m0", True)


async def main() -> None:
    budget = EngineBudget("unreal_5")
    for prompt in PROMPTS:
        scene = (await TemplateAuthor().propose(prompt, budget=budget, n=1, context={"asset_name": "Asset"}))[0]
        with tempfile.TemporaryDirectory() as tmp:
            stage = StageManager.create(Path(tmp) / "asset.usda", scene.asset_name)
            stage.apply_ir(scene)
            meshes = stage.extract_meshes()
        print(json.dumps(facts(prompt, meshes, budget)), flush=True)
    for target in SPHERES:
        print(json.dumps(facts(f"uv sphere, about {target} triangles (computed charts)", [sphere(target)], budget)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
