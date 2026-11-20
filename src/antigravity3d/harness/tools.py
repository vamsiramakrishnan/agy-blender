"""Model-facing harness tools.

Docstrings are prompt text (sent every turn): keep them terse. Every tool returns a short string
(one to three lines); images are written to the session artifacts dir and referenced by URI only.
Expected failures (bad IR, unknown ids, refused stage edits) come back as one ``ERROR ...`` line;
anything unexpected propagates to ``TracebackCompressorHook``.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
from google.antigravity.tools.tool_context import ToolContext
from PIL import Image

from antigravity3d import ir
from antigravity3d.adapters.engines.validators import MIN_SILHOUETTE_IOU, blender_frame_png, silhouette_iou
from antigravity3d.adapters.blender_5_3.linter import UnsafeScriptError, assert_safe
from antigravity3d.decisions.compose import Action, decide_action, engine_parity_reasons, rank, tier15_score
from antigravity3d.genmedia.pbr_deriver import delight, roughness_breakup
from antigravity3d.harness.session import Preview, Session, resolve
from antigravity3d.harness.traceback_compressor import compress_exception
from antigravity3d.ir import IRPatch, MaterialIR, SceneIR, SetTransform, UpsertMaterial
from antigravity3d.kernel.fast_ops import ExportBundle, Tier1Report, run_export_pipeline, tier1_qa
from antigravity3d.kernel.raster import contact_sheet
from antigravity3d.kernel.validator import check_engine_compliance, validate_usd
from antigravity3d.state import EXPORT_MIN_TIER15_SCORE, AssetSessionState, VisualVerdict
from antigravity3d.tactics.cached import cached_derive_pbr

PREVIEW_PX = 256  # per view; the sheet is 2x2
TEXTURE_PX = 2048
MAX_CANDIDATES = 6
MANIFEST_CAP = 1500
TOOLSET_STAGE = {"BLOCKOUT": "BLOCKOUT", "TWEAK": "BLOCKOUT", "VARIANTS": "BLOCKOUT", "LOOKDEV": "LOOKDEV", "EXPORT": "EXPORT"}
QA_STAGE = {"ROUTE": "blockout", "BLOCKOUT": "blockout", "LOOKDEV": "lookdev", "EXPORT": "final", "DONE": "final"}
MAP_KEYS = ("albedo_uri", "orm_uri", "normal_uri")

ToolFn = Callable[..., Awaitable[str]]


def _guard(fn: ToolFn) -> ToolFn:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except (ValueError, LookupError, RuntimeError) as e:  # bad input / unknown id / refused stage edit
            return f"ERROR {compress_exception(e)}"

    return wrapper


def export_block_reason(state: AssetSessionState) -> str | None:
    """None when export is allowed, else one line naming what is missing."""
    if state.export_allowed:
        return None
    missing = []
    if state.validator_ok is False:
        missing.append("passing USD/target checks (run run_usd_validator)")
    if not state.last_tier1_ok:
        missing.append("a passing Tier-1 QA (last edit failed or none yet)")
    if not EXPORT_MIN_TIER15_SCORE <= state.last_tier15_score <= 4.0:
        missing.append(f"tier15>={EXPORT_MIN_TIER15_SCORE} (have {state.last_tier15_score:.2f}; run run_visual_qa after the last edit)")
    if not state.verdict or not state.verdict.qualifies(state.revision, state.budget.target_engine):
        missing.append("current lookdev/final approval from Decisions on an EEVEE/Cycles render")
    return "export blocked: needs " + " and ".join(missing)


# --------------------------------------------------------------------------- shared helpers


async def _tier1(s: Session) -> Tier1Report:
    # Copy mesh arrays under the session lock. A cancelled QA worker cannot touch the live USD stage.
    return await asyncio.to_thread(tier1_qa, s.stage.extract_meshes(), s.state.budget)


async def _commit(s: Session, apply: Callable[[], str]) -> tuple[str | None, Tier1Report]:
    """Runs ``apply`` (opens one tx), Tier-1 checks it, rolls back on critical issues.

    Caller holds the session lock. Returns (tx id or None if rolled back, report).
    """
    cached = s._res("bundle")
    restore_uvs = cached is not None and cached[0] == s.geom_version
    tid = None
    try:
        tid = apply()
        report = await _tier1(s)
        if report.ok:
            s.visuals_changed(tier1_ok=True)
            return tid, report
    except BaseException:
        if tid is not None:
            s.stage.rollback(tid, discard=True)
        if restore_uvs:
            s.stage.author_uvs(cached[1].uvs)
        s.sync_tx()
        raise
    s.stage.rollback(tid, discard=True)
    if restore_uvs:
        s.stage.author_uvs(cached[1].uvs)
    s.sync_tx()
    return None, report


def _stats(r: Tier1Report) -> str:
    return f"tris={r.stats['tris']} draws={r.stats['draw_calls']}"


def _warn(r: Tier1Report) -> str:
    line = r.one_line(160)
    return f" |{line.split('|', 1)[1]}" if "|" in line else ""


def _material(s: Session, mid: str) -> MaterialIR | None:
    return next((m for m in s.stage.base_ir().materials if m.id == mid), None)


async def _render(s: Session, wireframe: bool = True) -> Preview:
    async with s.registry.locked(s.sid):
        meshes = s.stage.extract_meshes()
        version = s.geom_version
    if not meshes:
        raise ValueError("nothing to render: the scene has no meshes")
    sheet = await asyncio.to_thread(contact_sheet, meshes, size=PREVIEW_PX, wireframe_overlay=wireframe)
    path = s.artifacts_dir / f"preview_{uuid.uuid4().hex}.png"
    try:
        sheet.save(path)
        uri = await s.forge.upload(path) if s.forge is not None else path.as_uri()
        async with s.registry.locked(s.sid):
            return s.add_preview(path, uri, version)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _visual_qa(s: Session, *, read_only: bool = False):
    if s.plane is None:
        raise RuntimeError("no decision plane attached to this session")
    qa_stage = QA_STAGE.get(s.state.active_stage, "blockout")
    prev = s.latest_preview()
    if prev is None:
        if read_only:
            raise ValueError("no current preview; the parent agent must render the scene before review")
        prev = await _render(s)
    res = await s.plane.evaluate_visual_qa(
        prev.uri, ortho_uri=s.state.ortho_uris[0] if s.state.ortho_uris else None,
        style_anchor_uri=s.state.style_anchor_uri, stage=qa_stage, governor=s.governor)  # type: ignore[arg-type]
    return prev, res, tier15_score(res, s.thresholds), decide_action(res, s.thresholds, qa_stage)  # type: ignore[arg-type]


def _local(uri: str | None) -> Path | None:
    if not uri:
        return None
    p = Path(uri[7:] if uri.startswith("file://") else uri)
    return p if p.is_file() else None


async def _auto_fix(s: Session, action: Action) -> str:
    """Deterministic fixers runnable from here. Caller holds the session lock."""
    if action.fixer == "reground":
        meshes = s.stage.extract_meshes()
        if not meshes:
            return "reground: no meshes"
        dz = -float(min(m.points[:, 2].min() for m in meshes))
        if abs(dz) < 1e-4:
            return "reground: already grounded"
        roots = [n for n in s.stage.base_ir().nodes if n.parent is None]
        ops = [SetTransform(node_id=n.id, transform=n.transform.model_copy(
            update={"translate": (n.transform.translate[0], n.transform.translate[1], n.transform.translate[2] + dz)}))
            for n in roots]
        tid, rep = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=ops)))
        return f"reground dz={dz:+.3f} tx={tid}; re-run QA" if tid else f"reground rolled back: {rep.one_line(100)}"
    key, fn = {"roughness_breakup": ("orm_uri", roughness_breakup), "delight": ("albedo_uri", delight)}.get(action.fixer or "", (None, None))
    if key is None:
        return f"{action.fixer}: no deterministic fixer here"
    ops = []
    for m in s.stage.base_ir().materials:
        src = _local(getattr(m, key))
        if src is None:
            continue
        out = src.with_name(f"{src.stem}_{action.fixer}.png")
        await asyncio.to_thread(lambda: fn(Image.open(src)).save(out))  # noqa: B023 - awaited immediately
        ops.append(UpsertMaterial(material=m.model_copy(update={key: str(out)})))
    if not ops:
        return f"{action.fixer}: not applicable (no local texture maps); needs agent repair"
    tid, _ = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=ops)))
    return f"{action.fixer} applied to {len(ops)} material(s) tx={tid}; re-run QA"


def _scene_from_json(s: Session, text: str) -> tuple[Callable[[], str], int]:
    payload = json.loads(text)
    if isinstance(payload, dict) and "nodes" in payload:
        scene = SceneIR.model_validate({**payload, "asset_name": s.stage.asset_prim.GetName()})
        return (lambda: s.stage.apply_ir(scene)), len(scene.nodes) + len(scene.materials)
    patch = IRPatch.model_validate(payload if isinstance(payload, dict) else {"ops": payload})
    return (lambda: s.stage.apply_patch(patch)), len(patch.ops)


# --------------------------------------------------------------------------- BLOCKOUT


@_guard
async def apply_scene_ir(ops_json: str, ctx: ToolContext) -> str:
    """Atomically apply an IR edit; auto-rolled back if Tier-1 QA fails.

    ops_json: {"ops":[...]} patch (ops: upsert_node, remove_node, set_transform, set_param,
    upsert_material, assign_material, on_top_of, flush_back) or a full scene {"nodes":[...],"materials":[...]}.
    """
    s = resolve(ctx)
    apply, n = _scene_from_json(s, ops_json)
    async with s.registry.locked(s.sid):
        tid, rep = await _commit(s, apply)
        nodes = len(s.stage.base_ir().nodes)
    if tid is None:
        return f"ROLLED BACK {rep.one_line(200)}"
    return f"OK tx={tid} ops={n} nodes={nodes} {_stats(rep)}{_warn(rep)}"


@_guard
async def inspect_manifest(ctx: ToolContext) -> str:
    """Per-node summary of the composed scene: kind, bbox, tris, material."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        return s.stage.manifest(max_chars=MANIFEST_CAP)


@_guard
async def query_region(bounds_min: list[float], bounds_max: list[float], ctx: ToolContext) -> str:
    """List up to 32 prims intersecting a world-space bounding box, in metres. Each bound is [x, y, z]."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        return s.stage.query_region(bounds_min, bounds_max, limit=32, max_chars=1200)


@_guard
async def undo_last_edit(ctx: ToolContext) -> str:
    """Roll back the most recent edit transaction."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        if not s.stage.tx_stack:
            return "nothing to undo"
        s.revoke_approval()
        dropped = s.stage.rollback()
        s.state.last_tier1_ok = False
        s.visuals_changed()
        rep = await _tier1(s)
        s.state.last_tier1_ok = rep.ok
        s.persist_qa()
    return f"UNDONE tx={dropped[0]} open_tx={len(s.state.tx_stack)} | {rep.one_line(120)}"


@_guard
async def render_preview(ctx: ToolContext, wireframe: bool = True) -> str:
    """Render a 4-view clay contact sheet; returns its handle only."""
    p = await _render(resolve(ctx), wireframe)
    return f"PREVIEW #{p.index} views=front,right,top,persp uri={p.uri}"


@_guard
async def run_visual_qa(ctx: ToolContext) -> str:
    """Visual QA of the current renders; stores tier15, logs the audit delta, applies safe auto-fixes."""
    s = resolve(ctx)
    attempt = uuid.uuid4().hex
    async with s.registry.locked(s.sid):
        s.revoke_approval()
        s._set("visual_qa_attempt", attempt)
        s.state.verdict = None
        s.persist_qa()
        target = s.state.budget.target_engine
    prev, res, t15, action = await _visual_qa(s)
    async with s.registry.locked(s.sid):
        if (s.geom_version != prev.geom_version or s._res("visual_qa_attempt") != attempt
                or target != s.state.budget.target_engine or action.stage != QA_STAGE.get(s.state.active_stage)):
            return "STALE: scene changed during QA; re-run run_visual_qa"
        if prev.kind in {"eevee", "cycles"} and prev.texture_hash != s.texture_digest():
            return "STALE: material files changed since the render; render again"
        source = res.result.source if res.result.calibrated else "uncalibrated"
        s.set_tier15(t15, VisualVerdict(s.state.revision, target, action.stage, prev.kind,
                                      source, action.kind, prev.texture_hash), prev)
        delta = action.audit_delta()
        s.state.add_audit_delta(delta)
        fix = await _auto_fix(s, action) if action.kind == "auto_fix" else ""
    return delta + (f"\nfix: {fix}" if fix else "")


@_guard
async def qa_probe(ctx: ToolContext) -> str:
    """Read-only visual QA: verdict line, stores nothing."""
    s = resolve(ctx)
    prev, _res, _t15, action = await _visual_qa(s, read_only=True)
    if s.geom_version != prev.geom_version:
        return "STALE: scene changed during visual review"
    return "PROBE " + action.audit_delta()


# --------------------------------------------------------------------------- variants


@_guard
async def propose_variants(variants_json: str, ctx: ToolContext) -> str:
    """Add parallel branches: {"set":"lid","variants":{"open":[ops],"closed":[ops]}}; each is a patch on the base."""
    s = resolve(ctx)
    payload = json.loads(variants_json)
    set_name, variants = payload["set"], payload["variants"]
    report = []
    async with s.registry.locked(s.sid):
        first: str | None = None
        try:
            for name, ops in variants.items():
                patch = IRPatch.model_validate(ops if isinstance(ops, dict) else {"ops": ops})
                tid = s.stage.add_variant(set_name, name, patch)
                first = first or tid
                probe = s.stage.select_variant(set_name, name)
                rep = await _tier1(s)
                if not rep.ok:
                    s.stage.rollback(first)
                    s.sync_tx()
                    return f"ROLLED BACK variant {name}: {rep.one_line(120)}"
                s.stage.rollback(probe)
                report.append(f"{name}:ok {rep.stats['tris']}t")
        except BaseException:
            if first is not None:
                s.stage.rollback(first)
            s.sync_tx()
            raise
        if first is not None:
            try:
                s.visuals_changed()
            except BaseException:
                s.stage.rollback(first)
                s.sync_tx()
                raise
    return f"VARIANTS {set_name}: {', '.join(report)}; select_variant to inspect, promote_variant to keep"


async def _variant_op(ctx: ToolContext, op: str, set_name: str, variant: str) -> str:
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        fn = s.stage.select_variant if op == "select" else s.stage.promote_variant
        tid, rep = await _commit(s, lambda: fn(set_name, variant))
        if tid is None:
            return f"ROLLED BACK {set_name}={variant}: {rep.one_line(120)}"
    return f"{op.upper().rstrip('E')}ED {set_name}={variant} tx={tid} | {rep.one_line(120)}"


@_guard
async def select_variant(set_name: str, variant: str, ctx: ToolContext) -> str:
    """Compose a variant for inspection/QA (base edits are refused while selected)."""
    return await _variant_op(ctx, "select", set_name, variant)


@_guard
async def promote_variant(set_name: str, variant: str, ctx: ToolContext) -> str:
    """Make a variant the base scene and drop its variant set."""
    return await _variant_op(ctx, "promote", set_name, variant)


# --------------------------------------------------------------------------- LOOKDEV


@_guard
async def forge_pbr_material(material_id: str, prompt: str, ctx: ToolContext, candidates: int = 1) -> str:
    """Generate a tileable PBR material (albedo + ORM + normal) and bind its maps to material_id."""
    s = resolve(ctx)
    if s.forge is None:
        raise RuntimeError("no image forge attached to this session")
    if s.plane is None:
        return "BLOCKED: no visual policy judge attached"
    async with s.registry.locked(s.sid):
        version = s.geom_version
        current = _material(s, material_id) or MaterialIR(id=material_id)
    n = max(1, min(MAX_CANDIDATES, s.forge.governor.plan.pbr_candidates, int(candidates)))
    results = await asyncio.gather(*(s.forge.seamless_albedo(prompt, size=TEXTURE_PX, variant_hint=f"option {i + 1}" if n > 1 else "")
                                     for i in range(n)), return_exceptions=True)
    gens = [g for g in results if not isinstance(g, BaseException)]
    if not gens:
        raise RuntimeError(f"no candidate generated: {results[0]}")
    uris = await asyncio.gather(*(s.forge.upload(g.png_bytes) for g in gens))
    scores = await s.plane.rank_candidates("pbr", uris, governor=s.governor)
    allowed = [v for v in scores if not v.blocked and not v.error and v.result and v.result.calibrated]
    if not allowed:
        reasons = sorted({r for v in scores if v.policy for r in v.policy.reasons})
        return f"BLOCKED: all candidates failed the policy gate ({', '.join(reasons)[:100] or 'review required'})"
    best = rank(allowed, "pbr", s.thresholds)[0].index
    maps, _ = await asyncio.to_thread(cached_derive_pbr, s.forge.cache, gens[best].png_bytes,
                                      metallic_hint=current.metallic, convention=current.normal_convention, prepare=True)
    mdir = s.artifacts_dir / "materials"
    mdir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=mdir) as temp:
        files = {key: Path(temp) / (key[:-4] + ".png") for key in MAP_KEYS}
        for key, img in zip(MAP_KEYS, (maps.albedo, maps.orm, maps.normal), strict=True):
            await asyncio.to_thread(img.save, files[key])
        async with s.registry.locked(s.sid):
            if version != s.geom_version:
                return "STALE: scene changed during material generation"
            k = 1
            while (destination := mdir / f"{material_id}_v{k}").exists():
                k += 1
            Path(temp).rename(destination)
            try:
                paths = {key: str(destination / file.name) for key, file in files.items()}
                new = MaterialIR.model_validate({**current.model_dump(), **paths})
                tid, rep = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=[UpsertMaterial(material=new)])))
            except BaseException:
                shutil.rmtree(destination)
                raise
            if tid is None:
                shutil.rmtree(destination)
                return f"ROLLED BACK {rep.one_line(160)}"
    return f"MATERIAL {material_id} v{k}: albedo+orm+normal bound (pick {best + 1}/{len(gens)}, {len(allowed)} passed policy) tx={tid}"


@_guard
async def apply_shader_diff(material_id: str, diff_json: str, ctx: ToolContext) -> str:
    """Tweak a material: {"base_color":[r,g,b],"roughness":0.6,"metallic":0,"assign_to":["node_id"]}."""
    s = resolve(ctx)
    diff: dict[str, Any] = json.loads(diff_json)
    assign = diff.pop("assign_to", []) or []
    async with s.registry.locked(s.sid):
        current = _material(s, material_id) or MaterialIR(id=material_id)
        new = MaterialIR.model_validate({**current.model_dump(), **diff, "id": material_id})
        ops: list[Any] = [UpsertMaterial(material=new)]
        ops += [ir.AssignMaterial(node_id=n, material_id=material_id) for n in assign]
        tid, rep = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=ops)))
    if tid is None:
        return f"ROLLED BACK {rep.one_line(160)}"
    keys = ",".join(sorted(diff)) or "-"
    return f"SHADER {material_id} set={keys} assigned={len(assign)} tx={tid} {_stats(rep)}"


@_guard
async def run_paintover_loop(direction: str, ctx: ToolContext) -> str:
    """Paint-over of the latest render following an art direction; stored as the reference image."""
    s = resolve(ctx)
    if s.forge is None:
        raise RuntimeError("no image forge attached to this session")
    if s.plane is None:
        return "BLOCKED: no visual policy judge attached"
    prev = s.latest_preview() or await _render(s, wireframe=False)
    gen = await s.forge.paint_over(await asyncio.to_thread(prev.path.read_bytes), direction)
    uri = gen.uri or await s.forge.upload(gen.png_bytes)
    verdict = await s.plane.policy_gate(uri, governor=s.governor)
    if not verdict.allowed:
        return f"BLOCKED paint-over by policy gate ({', '.join(verdict.reasons)[:120]})"
    async with s.registry.locked(s.sid):
        if prev.geom_version != s.geom_version:
            return "STALE: scene changed during paint-over"
        path = s.artifacts_dir / f"paintover_{prev.index:03d}_{uuid.uuid4().hex}.png"
        await asyncio.to_thread(path.write_bytes, gen.png_bytes)
        s.state.paintover_uri = uri
    return f"PAINTOVER of preview #{prev.index} stored uri={uri}"


@_guard
async def render_lookdev(ctx: ToolContext, engine: str = "eevee") -> str:
    """Lookdev render via the Blender tier (eevee|cycles|workbench); falls back to the preview tier."""
    s = resolve(ctx)
    if s.pool is None:
        p = await _render(s, wireframe=False)
        return f"BLENDER TIER UNAVAILABLE (no pool attached); preview tier used instead: PREVIEW #{p.index} uri={p.uri}"
    if engine not in {"workbench", "eevee", "cycles"}:
        raise ValueError(f"unknown lookdev engine {engine!r}")
    stem = f"lookdev_{engine}_{uuid.uuid4().hex}"
    src = s.artifacts_dir / f"{stem}.usdc"
    try:
        async with s.registry.locked(s.sid):
            version = s.geom_version
            texture_hash = s.texture_digest()
            if any(getattr(m, key) for m in s.stage.base_ir().materials for key in MAP_KEYS) and not s.stage.has_uvs():
                await _bundle(s)
            s.stage.export(src)
        res = await s.pool.call("render_usd", path=str(src), engine=engine, width=512, height=512, transparent=True)
    finally:
        src.unlink(missing_ok=True)
    img = np.asarray(res["image"], dtype=np.float32)
    path = s.artifacts_dir / f"{stem}.png"
    if s.geom_version != version:
        raise RuntimeError("scene changed during render; render again")
    path.write_bytes(blender_frame_png(img))
    try:
        uri = await s.forge.upload(path) if s.forge is not None else path.as_uri()
        async with s.registry.locked(s.sid):
            if s.geom_version != version:
                raise RuntimeError("scene changed during render; render again")
            kind = "clay" if res["fake"] else engine
            if texture_hash != s.texture_digest():
                raise RuntimeError("material files changed during render; render again")
            p = s.add_preview(path, uri, version, kind=kind, texture_hash=texture_hash)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return f"LOOKDEV {res.get('engine', engine)} render #{p.index} uri={uri}"


# --------------------------------------------------------------------------- EXPORT


async def _bundle(s: Session) -> ExportBundle:
    # Caller owns the session lock. Only detached mesh arrays go to the native worker thread.
    cached = s._res("bundle")
    if cached and cached[0] == s.geom_version:
        bundle = cached[1]
    else:
        bundle = await asyncio.to_thread(run_export_pipeline, s.stage.extract_meshes(), s.state.budget,
                                         s.stage.asset_prim.GetName())
        s._set("bundle", (s.geom_version, bundle))
    if not s.stage.has_uvs():
        s.stage.author_uvs(bundle.uvs)
    return bundle


async def _stage_worker(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Drain a native stage/file operation before cancellation can release its lock or files."""
    worker = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        finally:
            raise


@_guard
async def generate_lods_and_colliders(ctx: ToolContext) -> str:
    """Unwrap UVs, build LOD chain and UCX convex colliders for the current scene."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        b = await _bundle(s)
    levels = min((len(v) for v in b.lods.values()), default=0)
    lod_tris = [sum(v[i].tri_count for v in b.lods.values()) for i in range(levels)]
    stretch = max((u.stretch_mean for u in b.uvs), default=0.0)
    srcs = ",".join(sorted({h.source for h in b.colliders}))
    return (f"LODS {len(b.lods)} nodes x {levels} levels tris={'/'.join(map(str, lod_tris))} | "
            f"colliders {len(b.colliders)} hulls ({srcs}) | uv stretch {stretch:.3f} | {b.timings_ms.get('total', 0):.0f}ms")


@_guard
async def run_usd_validator(ctx: ToolContext) -> str:
    """UsdValidation + target-engine compliance of the composed scene, one line."""
    s = resolve(ctx)
    with tempfile.TemporaryDirectory(dir=s.artifacts_dir) as directory:
        return await _validate_snapshot(s, Path(directory) / "validate.usda")


async def _validate_snapshot(s: Session, path: Path) -> str:
    async with s.registry.locked(s.sid):
        revision = s.state.revision
        budget = s.state.budget
        s.stage.export(path)
        meshes = s.stage.extract_meshes()
    from pxr import Usd

    usd = await _stage_worker(validate_usd, path)
    eng = await _stage_worker(check_engine_compliance, Usd.Stage.Open(str(path)), meshes, budget)
    async with s.registry.locked(s.sid):
        if revision != s.state.revision or budget != s.state.budget:
            return "STALE: scene changed during USD validation"
        passed = not any(i.severity == "critical" for i in [*usd, *eng])
        if not passed:
            s.revoke_approval()
        s.state.validator_ok = passed
        s.persist_qa()

    def fmt(tag: str, issues: list) -> str:
        crit = [i for i in issues if i.severity == "critical"]
        head = f"{tag} {len(crit)} crit/{len(issues) - len(crit)} warn"
        top = list(dict.fromkeys(f"{i.code}@{i.node_id}" if i.node_id else i.code for i in crit or issues))[:3]
        return head + (f" ({'; '.join(top)})" if top else "")

    return f"{fmt('USD', usd)} | {fmt(s.state.budget.target_engine, eng)}"[:300]


async def _verify_engine(s: Session, pkg: Any, asset: str) -> str | None:
    from antigravity3d.adapters.engines.validators import validator_for

    preview = s._res("graded_preview")
    if not preview or not preview.path.is_file() or not s.plane or not s.forge:
        return "missing graded DCC frame or calibrated parity backend"
    dcc = preview.path.read_bytes()
    if hashlib.sha256(dcc).hexdigest() != preview.image_hash:
        return "graded DCC frame changed on disk"
    validator = validator_for(s.state.budget.target_engine)
    glb = pkg.out_dir / f"{asset}.glb"
    smoke = await validator.validate(glb)
    if not smoke.ok:
        return f"{smoke.engine} import: {' '.join((smoke.skipped_reason or smoke.log_tail).split())[-160:]}"
    if smoke.metrics is not None and smoke.metrics.get("triangles") != pkg.data["tris"]:
        return "target import triangle count differs from the package"
    image_path = pkg.out_dir / "engine.png"
    capture = await validator.capture(glb, image_path)
    if not capture.ok:
        return f"{capture.engine} capture: {' '.join((capture.skipped_reason or capture.log_tail).split())[-160:]}"
    engine_image = image_path.read_bytes()
    overlap = silhouette_iou(dcc, engine_image)
    if overlap < MIN_SILHOUETTE_IOU:
        return f"target silhouette IoU {overlap:.4f} below {MIN_SILHOUETTE_IOU}"
    parity = await s.plane.evaluate_engine_parity(preview.uri, await s.forge.upload(engine_image), governor=s.governor)
    reasons = engine_parity_reasons(parity, s.thresholds)
    if parity.result.source != "decisions" or not parity.result.calibrated or reasons:
        return "target/DCC parity failed: " + (", ".join(reasons)[:120] or "uncalibrated source")
    (pkg.out_dir / "dcc.png").write_bytes(dcc)
    for name, data in (("dcc.png", dcc), ("engine.png", engine_image)):
        pkg.data["files"].append(name)
        pkg.data["file_hashes"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    pkg.data["validation"]["engine_import"] = {"engine": smoke.engine, "ok": True, "metrics": smoke.metrics}
    pkg.data["validation"]["engine_parity"] = {"ok": True, "silhouette_iou": overlap, "scores": parity.scores}
    return None


@_guard
async def export_engine_package(ctx: ToolContext, out_name: str) -> str:
    """Write the engine package (USD+usdz with UVs, LOD variants, colliders; GLBs; textures; manifest.json). Needs QA approval."""
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.publish import publish_directory

    s = resolve(ctx)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", out_name).strip("_") or "package"
    out = s.export_dir / name
    async with s.registry.locked(s.sid):
        st = s.state
        if why := export_block_reason(st):
            return f"REFUSED {why}"
        asset = s.stage.asset_prim.GetName()
        assert st.verdict is not None
        if st.verdict.texture_hash != s.texture_digest():
            s.revoke_approval()
            return "REFUSED material files changed after visual QA; render and evaluate again"
        b = await _bundle(s)
        qa = {"tier1_ok": st.last_tier1_ok, "tier15": round(st.last_tier15_score, 3),
              "action": st.verdict.action, "revision": st.revision, "target": st.budget.target_engine,
              "renderer": st.verdict.renderer, "backend": st.verdict.backend, "audit": st.audit_deltas[-2:]}
        with tempfile.TemporaryDirectory(dir=s.export_dir, prefix=".package-") as directory:
            candidate = Path(directory) / name
            pkg = await _stage_worker(write_engine_package, s.stage, b, st.budget, candidate, asset_name=asset, qa=qa)
            if not pkg.ok:
                crit = [i.code for i in pkg.issues if i.severity == "critical"]
                return f"REFUSED package validation failed: {', '.join(dict.fromkeys(crit))[:140]}"
            if reason := await _verify_engine(s, pkg, asset):
                return f"REFUSED {reason}"
            (candidate / "manifest.json").write_text(json.dumps(pkg.data, indent=1), encoding="utf-8")
            if st.verdict.texture_hash != s.texture_digest():
                s.revoke_approval()
                return "REFUSED material files changed during package validation"
            publish_directory(candidate, out)
        s.set_last_export(out)
    return (f"EXPORTED {name}: usd+glb, {pkg.n_lod_files} LOD glbs, {len(b.colliders)} UCX hulls, "
            f"{len(pkg.data['textures'])} maps, manifest.json | pkg ok")


@_guard
async def run_bpy_script(source: str, ctx: ToolContext) -> str:
    """Escape hatch: run a linted bpy script on the Blender pool. Prefer IR tools."""
    s = resolve(ctx)
    try:
        assert_safe(source)
    except UnsafeScriptError as e:
        return "REJECTED " + "; ".join(str(i) for i in e.issues[:2])[:200]
    if s.pool is None:
        return "UNAVAILABLE: no Blender pool attached to this session"
    res = await s.pool.call("run_script", source=source)
    return f"RAN {json.dumps(res, default=str)[:200]}"


# --------------------------------------------------------------------------- tool sets

STAGE_TOOLS: dict[str, list[ToolFn]] = {
    "BLOCKOUT": [apply_scene_ir, inspect_manifest, undo_last_edit, render_preview, run_visual_qa],
    "TWEAK": [apply_scene_ir, inspect_manifest, query_region, undo_last_edit],
    "VARIANTS": [propose_variants, select_variant, promote_variant, inspect_manifest, run_visual_qa],
    "LOOKDEV": [forge_pbr_material, apply_shader_diff, run_paintover_loop, render_lookdev, run_visual_qa],
    "EXPORT": [inspect_manifest, generate_lods_and_colliders, run_usd_validator, export_engine_package, run_bpy_script],
}
SUBAGENT_TOOLS: dict[str, list[ToolFn]] = {
    "usd_verifier": [inspect_manifest, run_usd_validator],
    "lookdev_critic": [inspect_manifest, qa_probe],
}
ALL_TOOLS: dict[str, ToolFn] = {f.__name__: f for fs in (*STAGE_TOOLS.values(), *SUBAGENT_TOOLS.values()) for f in fs}
READ_ONLY_TOOL_NAMES = frozenset(f.__name__ for fs in SUBAGENT_TOOLS.values() for f in fs)
