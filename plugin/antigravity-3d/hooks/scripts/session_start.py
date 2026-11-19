#!/usr/bin/env python3
"""SessionStart context for antigravity-3d (wired in ../../hooks.json).

Injects one short system message (docs: ``{"injectSteps": [{"systemMessage":
{"systemMessage": "..."}}]}``) with the active engine budget, pipeline stage
and the last QA verdict, so the agent starts with the gate state in view
instead of re-deriving it with tool calls.

Budget source: ``<state>/budget.json`` (``{"target_engine": ..., "max_tris": ...,
"max_draw_calls": ..., "texel_density_px_per_m": ..., "stage": ...}``) if the
project wrote one, else the studio defaults below. Fails open: on any error it
injects nothing.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402

# Mirrors antigravity3d.state.EngineBudget defaults + kernel/validator.py ENGINE_RULES.
ENGINE_NOTES = {
    "unreal_5": "UE5: Z-up cm on import, UCX_<Mesh>_NN hulls, pow2 textures, DirectX normals on export",
    "unity_urp": "Unity URP: Y-up m, colliders as components, <=65535 verts per 16-bit mesh",
    "godot_4": "Godot 4: Y-up m, -col/-convcol hull suffixes, node names without . : @ / \" %",
    "roblox_r15": "Roblox R15: Y-up, <=10k tris per MeshPart, <=1024 px textures, 4 SurfaceAppearance maps",
}
DEFAULT_BUDGET = {"max_tris": 15000, "max_draw_calls": 4, "texel_density_px_per_m": 1024, "max_collision_hulls": 8}


def build_message(payload: dict[str, Any]) -> str:
    root = c.state_root(payload)
    cfg = c.read_json(root / "budget.json")
    cfg = cfg if isinstance(cfg, dict) else {}
    engine = str(cfg.get("target_engine", "")) or None
    b = {**DEFAULT_BUDGET, **{k: v for k, v in cfg.items() if k in DEFAULT_BUDGET}}
    lines = ["[antigravity-3d] "
             + (f"target {engine}: " if engine else "no target engine set (budget.json); defaults: ")
             + f"<= {b['max_tris']} tris, <= {b['max_draw_calls']} draw calls, "
               f"{b['texel_density_px_per_m']} px/m, <= {b['max_collision_hulls']} hulls."]
    if engine in ENGINE_NOTES:
        lines.append(ENGINE_NOTES[engine] + ".")
    if cfg.get("stage"):
        lines.append(f"Stage: {cfg['stage']}.")
    qa = c.read_qa(root, c.conv_id(payload))
    if qa:
        age_h = (time.time() - float(qa.get("ts", 0))) / 3600
        lines.append(f"Last QA ({qa.get('asset') or 'asset'}, {age_h:.1f} h ago): Tier-1 "
                     f"{'ok' if qa.get('tier1_ok') else 'FAILED'}, Tier-1.5 {float(qa.get('tier15_score', 0)):.2f}.")
    else:
        lines.append("No QA verdict recorded yet.")
    lines.append("Edit via Scene IR tools; renders go through run_visual_qa/render_preview; "
                 "export needs Tier-1 ok and Tier-1.5 >= 3.4 (hook-enforced).")
    return " ".join(lines)


def main() -> None:
    payload = c.read_payload()
    try:
        out = {"injectSteps": [{"systemMessage": {"systemMessage": build_message(payload)}}]}
    except Exception as e:  # noqa: BLE001
        c.warn(payload, f"session_start failed: {e!r}")
        out = {"injectSteps": []}
    c.emit(out)


if __name__ == "__main__":
    main()
