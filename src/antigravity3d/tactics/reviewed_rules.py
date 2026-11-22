"""Executable repair allowlist and geometric replay. Registry data never contains code."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator
from pxr import Usd

from antigravity3d.ir import IRPatch, SceneIR, SetTransform, apply_patch
from antigravity3d.kernel.compile_ir import author_scene
from antigravity3d.kernel.fast_ops import tier1_qa
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.runtime.qa_state import _atomic_write_json, state_root
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import world_bounds

RULE = "seat-floating-box-v1"


def scene_hash(scene: SceneIR) -> str:
    if scene.nodes and all(n.kind == "box" and not n.parent and not any(n.transform.rotate_euler_deg)
                           for n in scene.nodes):
        # Renaming, repainting or moving the assembly in XY does not produce a new holdout.
        bounds = list(world_bounds(scene).values())
        origin = np.min([lo for lo, _ in bounds], axis=0)
        origin[2] = 0.0  # absolute height determines ground contact
        data = sorted(tuple(np.round(np.r_[lo - origin, hi - origin], 6)) for lo, hi in bounds)
    else:
        # A material's surface words describe its texture, not the shape a fix applies to (like a repaint).
        data = scene.model_dump(exclude={"asset_name": True, "materials": {"__all__": {"surface"}}})
        for key in ("nodes", "materials"):
            data[key].sort(key=lambda item: item["id"])
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def seat_patch(scene: SceneIR, floating: list[str]) -> IRPatch | None:
    """Lower one floating root box by at most 0.5 m onto a box under its centre."""
    if len(floating) != 1 or not 2 <= len(scene.nodes) <= 64:
        return None
    if any(n.kind != "box" or n.parent or any(n.transform.rotate_euler_deg)
           or min(n.transform.scale) <= 0 for n in scene.nodes):
        return None
    boxes = world_bounds(scene)
    target = floating[0]
    if target not in boxes:
        return None
    low, high = boxes[target]
    centre = (low + high)[:2] / 2
    # A side protrusion in the swept volume could be hit during the drop. Use
    # only the highest intersecting support, then verify it supports the centre.
    supports = [(hi[2], name) for name, (lo, hi) in boxes.items() if name != target
                and hi[2] <= low[2] and np.all(np.minimum(high[:2], hi[:2]) > np.maximum(low[:2], lo[:2]))]
    if not supports:
        return None
    z, support = max(supports)
    gap = low[2] - z
    if not .01 < gap <= .5 or not np.all((boxes[support][0][:2] <= centre) & (boxes[support][1][:2] >= centre)):
        return None
    node = next(n for n in scene.nodes if n.id == target)
    x, y, old_z = node.transform.translate
    transform = node.transform.model_copy(update={"translate": (x, y, float(old_z - gap))})
    return IRPatch(ops=[SetTransform(node_id=target, transform=transform)])


class Fixture(BaseModel):
    """Authored ground truth: expected centre Z in metres for every supported root box."""

    model_config = ConfigDict(extra="forbid")
    scene: SceneIR
    expected_z: dict[str, FiniteFloat]

    @model_validator(mode="after")
    def complete(self):
        if set(self.expected_z) != {n.id for n in self.scene.nodes} or not self.scene.nodes:
            raise ValueError("expected_z must label every node")
        if any(n.parent or n.kind != "box" or any(n.transform.rotate_euler_deg) for n in self.scene.nodes):
            raise ValueError("held-out fixtures must be axis-aligned root boxes")
        return self


class Corpus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixtures: list[Fixture] = Field(min_length=2)

    @model_validator(mode="after")
    def unique(self):
        if len({scene_hash(f.scene) for f in self.fixtures}) != len(self.fixtures):
            raise ValueError("duplicate held-out geometry")
        if len({f.scene.asset_name for f in self.fixtures}) != len(self.fixtures):
            raise ValueError("duplicate held-out asset ID")
        return self


def _qa(sm: StageManager):
    return tier1_qa(sm.extract_meshes(), EngineBudget("unreal_5"))


def replay(fixture: Fixture) -> dict:
    stage = Usd.Stage.CreateInMemory()
    author_scene(stage, fixture.scene)
    sm = StageManager(stage)
    before = _qa(sm)
    flagged = [i.node_id for i in before.issues if i.code == "floating_part"]
    patch = seat_patch(fixture.scene, flagged)
    result = apply_patch(fixture.scene, patch) if patch else fixture.scene
    author_scene(stage, result, prev=fixture.scene)
    after = _qa(sm)

    def errors(scene):
        return np.array([abs(node.transform.translate[2] - fixture.expected_z[node.id])
                         for node in scene.nodes])

    def problems(report):
        return {(i.code, i.node_id, i.severity) for i in report.issues}

    was, now = errors(fixture.scene), errors(result)
    introduced = sorted(str(key) for key in problems(after) - problems(before))
    return {"asset": result.asset_name, "before_mae_m": float(was.mean()), "after_mae_m": float(now.mean()),
            "changed": patch is not None, "regressed": bool((now > was + 1e-6).any() or introduced),
            "new_tier1_issues": introduced}


@lru_cache(maxsize=1)
def implementation_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def registry_path(workspace: Path | None = None) -> Path:
    return state_root(workspace) / "reviewed_rules" / f"{RULE}.json"


def is_enabled(workspace: Path | None = None) -> bool:
    try:
        row = json.loads(registry_path(workspace).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return (isinstance(row, dict) and row.get("rule") == RULE and row.get("implementation") == implementation_hash()
            and bool(row.get("reviewer")) and len(row.get("evidence", "")) == 64)


def record_approval(workspace: Path | None, assessment: dict, reviewer: str) -> None:
    digest = assessment["digest"]
    if not assessment["eligible"] or not reviewer.strip():
        raise ValueError("reviewed promotion needs positive held-out replay")
    _atomic_write_json(state_root(workspace) / "distill" / "evidence" / f"{digest}.json", assessment["evidence"])
    _atomic_write_json(registry_path(workspace), {
        "rule": RULE, "implementation": implementation_hash(), "evidence": digest,
        "reviewer": reviewer, "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
