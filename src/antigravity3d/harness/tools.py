"""Model-facing harness tools.

Docstrings are prompt text (sent every turn): keep them terse. Every tool returns a short string
(one to three lines); images are written to the session artifacts dir and referenced by URI only.
Expected failures (bad IR, unknown ids, refused stage edits) come back as one ``ERROR ...`` line;
anything unexpected propagates to ``TracebackCompressorHook``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
from google.antigravity.tools.tool_context import ToolContext
from PIL import Image

from antigravity3d import ir
from antigravity3d.adapters.blender_5_3.linter import UnsafeScriptError, assert_safe
from antigravity3d.decisions.compose import Action, decide_action, rank, tier15_score
from antigravity3d.genmedia.pbr_deriver import delight, derive_pbr, roughness_breakup
from antigravity3d.harness.session import Preview, Session, resolve
from antigravity3d.harness.traceback_compressor import compress_exception
from antigravity3d.ir import IRPatch, MaterialIR, SceneIR, SetTransform, UpsertMaterial
from antigravity3d.kernel.fast_ops import ExportBundle, Tier1Report, run_export_pipeline, tier1_qa, write_gltf
from antigravity3d.kernel.raster import contact_sheet
from antigravity3d.kernel.validator import check_engine_compliance, validate_usd
from antigravity3d.state import EXPORT_MIN_TIER15_SCORE, AssetSessionState

PREVIEW_PX = 256  # per view; the sheet is 2x2
TEXTURE_PX = 1024
MAX_CANDIDATES = 4
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
    if not state.last_tier1_ok:
        missing.append("a passing Tier-1 QA (last edit failed or none yet)")
    if state.last_tier15_score < EXPORT_MIN_TIER15_SCORE:
        missing.append(f"tier15>={EXPORT_MIN_TIER15_SCORE} (have {state.last_tier15_score:.2f}; run run_visual_qa after the last edit)")
    return "export blocked: needs " + " and ".join(missing)


# --------------------------------------------------------------------------- shared helpers


def _tier1(s: Session) -> Tier1Report:
    return tier1_qa(s.stage.extract_meshes(), s.state.budget)


async def _commit(s: Session, apply: Callable[[], str]) -> tuple[str | None, Tier1Report]:
    """Runs ``apply`` (opens one tx), Tier-1 checks it, rolls back on critical issues.

    Caller holds the session lock. Returns (tx id or None if rolled back, report).
    """
    tid = apply()
    report = await asyncio.to_thread(_tier1, s)
    if not report.ok:
        s.stage.rollback(tid)
        s.sync_tx()
        return None, report
    s.state.last_tier1_ok = True
    s.visuals_changed()
    return tid, report


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
    path = s.artifacts_dir / f"preview_{len(s.previews) + 1:03d}.png"
    await asyncio.to_thread(sheet.save, path)
    uri = await s.forge.upload(path) if s.forge is not None else path.as_uri()
    p = s.add_preview(path, uri, version)
    if version != s.geom_version:  # geometry changed while rendering: recorded as stale
        raise RuntimeError("geometry changed during render; re-run")
    return p


async def _visual_qa(s: Session):
    if s.plane is None:
        raise RuntimeError("no decision plane attached to this session")
    prev = s.latest_preview() or await _render(s)
    qa_stage = QA_STAGE.get(s.state.active_stage, "blockout")
    res = await s.plane.evaluate_visual_qa(
        prev.uri, ortho_uri=s.state.ortho_uris[0] if s.state.ortho_uris else None,
        style_anchor_uri=s.state.style_anchor_uri, stage=qa_stage)  # type: ignore[arg-type]
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
    upsert_material, assign_material) or a full scene {"nodes":[...],"materials":[...]}.
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
    text = s.stage.manifest()
    if len(text) <= MANIFEST_CAP:
        return text
    lines, out = text.splitlines(), []
    for ln in lines:
        if sum(map(len, out)) + len(ln) > MANIFEST_CAP - 40:
            break
        out.append(ln)
    return "\n".join(out + [f"... +{len(lines) - len(out)} nodes"])


@_guard
async def undo_last_edit(ctx: ToolContext) -> str:
    """Roll back the most recent edit transaction."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        if not s.stage.tx_stack:
            return "nothing to undo"
        dropped = s.stage.rollback()
        rep = await asyncio.to_thread(_tier1, s)
        s.state.last_tier1_ok = rep.ok
        s.visuals_changed()
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
    prev, _res, t15, action = await _visual_qa(s)
    async with s.registry.locked(s.sid):
        if s.geom_version != prev.geom_version:
            return "STALE: scene changed during QA; re-run run_visual_qa"
        s.set_tier15(t15)
        delta = action.audit_delta()
        s.state.add_audit_delta(delta)
        fix = await _auto_fix(s, action) if action.kind == "auto_fix" else ""
    return delta + (f"\nfix: {fix}" if fix else "")


@_guard
async def qa_probe(ctx: ToolContext) -> str:
    """Read-only visual QA: verdict line, stores nothing."""
    _prev, _res, _t15, action = await _visual_qa(resolve(ctx))
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
        base = s.stage.base_ir()
        for name, ops in variants.items():
            patch = IRPatch.model_validate(ops if isinstance(ops, dict) else {"ops": ops})
            s.stage.add_variant(set_name, name, ir.apply_patch(base, patch))
            probe = s.stage.select_variant(set_name, name)
            rep = await asyncio.to_thread(_tier1, s)
            s.stage.rollback(probe)
            report.append(f"{name}:{'ok' if rep.ok else 'FAIL'} {rep.stats['tris']}t")
        s.sync_tx()
    return f"VARIANTS {set_name}: {', '.join(report)}; select_variant to inspect, promote_variant to keep"


async def _variant_op(ctx: ToolContext, op: str, set_name: str, variant: str) -> str:
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        fn = s.stage.select_variant if op == "select" else s.stage.promote_variant
        tid = fn(set_name, variant)
        rep = await asyncio.to_thread(_tier1, s)
        s.state.last_tier1_ok = rep.ok
        s.visuals_changed()
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
    n = max(1, min(MAX_CANDIDATES, int(candidates)))
    gens = await asyncio.gather(*(s.forge.seamless_albedo(prompt, size=TEXTURE_PX, variant_hint=f"option {i + 1}" if n > 1 else "")
                                  for i in range(n)))
    uris = [g.uri or await s.forge.upload(g.png_bytes) for g in gens]
    allowed = list(range(n))
    if s.plane is not None:
        verdicts = await asyncio.gather(*(s.plane.policy_gate(u) for u in uris))
        allowed = [i for i, v in enumerate(verdicts) if v.allowed]
        if not allowed:
            why = sorted({r for v in verdicts for r in v.reasons})
            return f"BLOCKED: all {n} candidate(s) failed the policy gate ({', '.join(why)[:120]})"
    best = allowed[0]
    if len(allowed) > 1 and s.plane is not None:
        ranked = rank(await s.plane.rank_candidates("pbr", [uris[i] for i in allowed]), "pbr", s.thresholds)
        best = allowed[ranked[0].index]
    current = _material(s, material_id) or MaterialIR(id=material_id)
    maps = await asyncio.to_thread(lambda: derive_pbr(delight(gens[best].image().convert("RGB")), metallic_hint=current.metallic))
    mdir = s.artifacts_dir / "materials"
    mdir.mkdir(exist_ok=True)
    k = len(list(mdir.glob(f"{material_id}_v*_albedo.png"))) + 1
    paths = {key: mdir / f"{material_id}_v{k}_{key[:-4]}.png" for key in MAP_KEYS}
    for key, img in zip(MAP_KEYS, (maps.albedo, maps.orm, maps.normal), strict=True):
        await asyncio.to_thread(img.save, paths[key])
    new = MaterialIR.model_validate({**current.model_dump(), **{key: str(p) for key, p in paths.items()}})
    async with s.registry.locked(s.sid):
        tid, rep = await _commit(s, lambda: s.stage.apply_patch(IRPatch(ops=[UpsertMaterial(material=new)])))
    if tid is None:
        return f"ROLLED BACK {rep.one_line(160)}"
    return f"MATERIAL {material_id} v{k}: albedo+orm+normal bound (pick {best + 1}/{n}, {len(allowed)} passed policy) tx={tid}"


@_guard
async def apply_shader_diff(material_id: str, diff_json: str, ctx: ToolContext) -> str:
    """Tweak a material: {"base_color":[r,g,b],"roughness":0.6,"metallic":0,"assign_to":["node_id"]}."""
    s = resolve(ctx)
    diff: dict[str, Any] = json.loads(diff_json)
    assign = diff.pop("assign_to", []) or []
    current = _material(s, material_id) or MaterialIR(id=material_id)
    new = MaterialIR.model_validate({**current.model_dump(), **diff, "id": material_id})
    ops: list[Any] = [UpsertMaterial(material=new)]
    ops += [ir.AssignMaterial(node_id=n, material_id=material_id) for n in assign]
    async with s.registry.locked(s.sid):
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
    prev = s.latest_preview() or await _render(s, wireframe=False)
    gen = await s.forge.paint_over(prev.path.read_bytes(), direction)
    path = s.artifacts_dir / f"paintover_{prev.index:03d}.png"
    path.write_bytes(gen.png_bytes)
    uri = gen.uri or await s.forge.upload(path)
    if s.plane is not None and not (v := await s.plane.policy_gate(uri)).allowed:
        return f"BLOCKED paint-over by policy gate ({', '.join(v.reasons)[:120]})"
    s.state.paintover_uri = uri
    return f"PAINTOVER of preview #{prev.index} stored uri={uri}"


@_guard
async def render_lookdev(ctx: ToolContext, engine: str = "eevee") -> str:
    """Lookdev render via the Blender tier (eevee|cycles|workbench); falls back to the preview tier."""
    s = resolve(ctx)
    if s.pool is None:
        p = await _render(s, wireframe=False)
        return f"BLENDER TIER UNAVAILABLE (no pool attached); preview tier used instead: PREVIEW #{p.index} uri={p.uri}"
    src = s.artifacts_dir / "lookdev_src.usdc"
    async with s.registry.locked(s.sid):
        s.stage.export(src)
    await s.pool.call("reset_scene")
    await s.pool.call("import_usd", path=str(src))
    res = await s.pool.call("render", engine=engine, width=512, height=512)
    img = np.asarray(res["image"], dtype=np.float32)
    path = s.artifacts_dir / f"lookdev_{engine}_{len(s.previews) + 1:03d}.png"
    Image.fromarray((np.clip(img[..., :3], 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)).save(path)
    uri = await s.forge.upload(path) if s.forge is not None else path.as_uri()
    p = s.add_preview(path, uri)
    return f"LOOKDEV {res.get('engine', engine)} render #{p.index} uri={uri}"


# --------------------------------------------------------------------------- EXPORT


def _bundle(s: Session) -> ExportBundle:
    cached = s._res("bundle")
    if cached and cached[0] == s.geom_version:
        return cached[1]
    b = run_export_pipeline(s.stage.extract_meshes(), s.state.budget, s.stage.asset_prim.GetName())
    s.stage.author_uvs(b.uvs)  # UVs live in the working stage too (overlay layer: no tx, no undo entry)
    s._set("bundle", (s.geom_version, b))
    return b


@_guard
async def generate_lods_and_colliders(ctx: ToolContext) -> str:
    """Unwrap UVs, build LOD chain and UCX convex colliders for the current scene."""
    s = resolve(ctx)
    async with s.registry.locked(s.sid):
        b = await asyncio.to_thread(_bundle, s)
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
    path = s.artifacts_dir / "validate.usda"
    async with s.registry.locked(s.sid):
        s.stage.export(path)
        meshes = s.stage.extract_meshes()
    from pxr import Usd

    usd = await asyncio.to_thread(validate_usd, path)
    eng = await asyncio.to_thread(check_engine_compliance, Usd.Stage.Open(str(path)), meshes, s.state.budget)

    def fmt(tag: str, issues: list) -> str:
        crit = [i for i in issues if i.severity == "critical"]
        head = f"{tag} {len(crit)} crit/{len(issues) - len(crit)} warn"
        top = list(dict.fromkeys(f"{i.code}@{i.node_id}" if i.node_id else i.code for i in crit or issues))[:3]
        return head + (f" ({'; '.join(top)})" if top else "")

    return f"{fmt('USD', usd)} | {fmt(s.state.budget.target_engine, eng)}"[:300]


@_guard
async def export_engine_package(ctx: ToolContext, out_name: str) -> str:
    """Write the engine package (USD+usdz with UVs, LOD variants, colliders; GLBs; textures; manifest.json). Needs QA approval."""
    s = resolve(ctx)
    if (why := export_block_reason(s.state)) is not None:
        return f"REFUSED {why}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", out_name).strip("_") or "package"
    out = s.export_dir / name
    asset = s.stage.asset_prim.GetName()
    st = s.state
    qa = {"tier1_ok": st.last_tier1_ok, "tier15": round(st.last_tier15_score, 3), "audit": st.audit_deltas[-2:]}
    from antigravity3d.kernel.package import write_engine_package

    async with s.registry.locked(s.sid):
        b = await asyncio.to_thread(_bundle, s)
        pkg = await asyncio.to_thread(
            write_engine_package, s.stage, b, st.budget, out, asset_name=asset, qa=qa
        )
    s.set_last_export(out)
    crit = [i for i in pkg.issues if i.severity == "critical"]
    verdict = "pkg ok" if not crit else f"PKG {len(crit)} crit ({'; '.join(dict.fromkeys(i.code for i in crit))[:100]})"
    return (f"EXPORTED {name}: usd+glb, {pkg.n_lod_files} LOD glbs, {len(b.colliders)} UCX hulls, "
            f"{len(pkg.data['textures'])} maps, manifest.json | {verdict}")


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
    "TWEAK": [apply_scene_ir, inspect_manifest, undo_last_edit],
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
