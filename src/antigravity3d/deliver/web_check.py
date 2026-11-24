"""The in-project check of a web install: the installed loader, run in a headless browser with the project's own three.js.

It reuses the package verifier's machinery (``adapters/engines/web_three.py``: the page server, the browser lease, the
facts read back from the scene, ``evaluate`` and its pass rule, the silhouette comparison, the camera and the lights) and
changes what it loads: the page imports the developer's own ``node_modules/three`` through an import map (so no bundler
is needed) and calls the installed ``index.js`` exactly as the developer would, with no base URL, so the loader's
default (the site root) is what is exercised. The project is reachable by the page only through mounts: the installed
folders and ``node_modules/three``, nothing else.

Checks, in the shape of the Godot install's (``install.godot_checks``): import, scene, scale, levels, materials,
collision, level_switching, shared_textures, outline, and compression (or loader_vs_package without compression: the
same frame through the loader and straight from the package, which must be the same picture).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from antigravity3d.adapters.engines import web_three as w3
from antigravity3d.deliver import glb as glbtool
from antigravity3d.deliver.godot import HYSTERESIS, switch_probes
from antigravity3d.deliver.paths import asset_name
from antigravity3d.deliver.web import ChildError, pascal, run_child, search_dirs

# The compression tolerance, fixed in the contract before the first compressed file existed (CONTRACT_WEB.md section 4):
# on the web check's 512 px beauty frame, same camera and light, compressed against uncompressed.
TOL_MEAN_ABS = 0.015          # mean absolute difference, share of full scale
TOL_PIXEL_SHARE = 0.01        # share of the asset's pixels that differ by more than TOL_PIXEL_DELTA
TOL_PIXEL_DELTA = 0.08        # share of full scale
TOL_OUTLINE = 0.999           # silhouette agreement
# A second field of view for the level-switching probes: the loader follows the live camera, so the same levels must be
# drawn at the distances that field of view needs.
PROBE_FOV_DEG = 30.0

# The page. Imports the project's three through the import map; the helpers (SLOT_KEYS, content, describe) and the
# environment code are the package verifier's own (web_three._PAGE).
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>agy3d install check</title>
<script type="importmap">{"imports":{"three":"./_m/__three/build/three.module.js","three/examples/jsm/":"./_m/__three/examples/jsm/","three/addons/":"./_m/__three/examples/jsm/"}}</script>
<style>html,body{margin:0;background:transparent}canvas{display:block}</style></head>
<body><canvas id="c" width="__SIZE__" height="__SIZE__"></canvas>
<script type="application/json" id="job">__JOB__</script>
<script type="module">
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
const job = JSON.parse(document.getElementById('job').textContent);
const out = { done: false, errors: [], shader_errors: [], programs: [], main: null, lods: [], render: null, png: null, mask_png: null };
window.__agy = out;
__HELPERS__

function describeObject(group) {
  const meshes = [], mats = new Map(); let tris = 0;
  group.traverse((o) => {
    if (!o.isMesh) return;
    const g = o.geometry, n = (g.index ? g.index.count : g.attributes.position.count) / 3;
    tris += n;
    const m = Array.isArray(o.material) ? o.material[0] : o.material;
    meshes.push({ name: o.name, tris: n, material: m.name, hasNormals: !!g.attributes.normal, hasUV: !!g.attributes.uv, hasTangents: !!g.attributes.tangent });
    mats.set(m.uuid, m);
  });
  const materials = [...mats.values()].map((m) => {
    const slots = {};
    for (const slot of Object.keys(SLOT_KEYS)) {
      const t = m[slot];
      if (t) {
        const [w, h] = imgSize(t);
        slots[slot] = { width: w, height: h, colorSpace: t.colorSpace, content: content(t), uri: null, compressed: !!t.isCompressedTexture, format: t.format };
      }
    }
    return { name: m.name, type: m.type, fallback_default: false, declared: Object.keys(slots), slots, missing_slots: [],
             color: m.color ? m.color.getHexString() : null, metalness: m.metalness, roughness: m.roughness,
             flatShading: !!m.flatShading, doubleSided: m.side === THREE.DoubleSide, transparent: !!m.transparent,
             json_alpha_mode: null, json_extensions: [],
             params: {
               color: c3(m.color), opacity: m.opacity, alphaTest: m.alphaTest, emissive: c3(m.emissive), emissiveIntensity: m.emissiveIntensity,
               physical: !!m.isMeshPhysicalMaterial, clearcoat: num(m.clearcoat), clearcoatRoughness: num(m.clearcoatRoughness),
               transmission: num(m.transmission), ior: num(m.ior), sheen: num(m.sheen), sheenColor: c3(m.sheenColor),
               sheenRoughness: num(m.sheenRoughness), specularIntensity: num(m.specularIntensity), specularColor: c3(m.specularColor) } };
  });
  const box = new THREE.Box3().setFromObject(group);
  return { meshes, mesh_count: meshes.length, triangles: tris, non_triangle_objects: 0, materials,
           texture_count: materials.reduce((s, m) => s + Object.keys(m.slots).length, 0),
           bounds_m: box.isEmpty() ? null : [box.min.toArray(), box.max.toArray()], json_materials: materials.length, json_textures: 0, extensions_used: [] };
}

__ENV_JS__
let envTarget = null;
async function environment(renderer) {
  if (job.env.mode === 'shared') return sharedEnvironment('./' + job.env.file);
  envTarget = gradientEnvironment(renderer, job.rig);
  return envTarget.texture;
}
const shot = (renderer, scene, camera) => { renderer.render(scene, camera); renderer.getContext().finish(); return document.getElementById('c').toDataURL('image/png'); };

async function main() {
  const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('c'), antialias: false, alpha: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1); renderer.setSize(job.camera.size_px, job.camera.size_px, false);
  renderer.setClearColor(0x000000, 0); renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.toneMapping = THREE.NoToneMapping;
  renderer.debug.checkShaderErrors = true;
  renderer.debug.onShaderError = (gl, program, vs, fs) => out.shader_errors.push({
    program_log: gl.getProgramInfoLog(program), vertex_log: gl.getShaderInfoLog(vs), fragment_log: gl.getShaderInfoLog(fs) });
  window.__agy_export_env = () => exportEnvironment(renderer, envTarget);
  const env = environment(renderer);
  const scene = new THREE.Scene();
  scene.environment = await env;
  for (const [pos, inten] of job.rig.lights) { const d = new THREE.DirectionalLight(0xffffff, inten); d.position.set(...pos); scene.add(d); }
  const cam = job.camera, w = cam.ortho_width / 2;
  const camera = new THREE.OrthographicCamera(-w, w, w, -w, cam.diameter * 0.01, cam.diameter * 5);
  const dir = new THREE.Vector3(...cam.view_dir).normalize(), center = new THREE.Vector3(...cam.center);
  camera.position.copy(center).addScaledVector(dir, cam.distance); camera.up.set(0, 1, 0); camera.lookAt(center);

  // 1. the developer's call: the installed module, no base URL. The environment map is uploaded to the GPU by the first
  // draw of a lit material, so one is drawn now: the texture count after the asset is then the asset's own.
  const probe = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshStandardMaterial());
  scene.add(probe); renderer.render(scene, camera); scene.remove(probe); probe.geometry.dispose(); probe.material.dispose();
  const baseline = renderer.info.memory.textures;
  const t0 = performance.now();
  let root;
  try {
    const mod = await import(job.loader_url);
    root = await mod[job.export](Object.assign({ renderer }, job.options || {}));
  } catch (e) { out.errors.push('loader: ' + String((e && e.stack) || e)); out.done = true; return; }
  out.load_ms = performance.now() - t0;
  const agy = root.userData.agy3d;
  root.updateMatrixWorld(true);
  const lod = agy.lod, holders = lod.levels.map((l) => l.object);
  out.agy = { asset: agy.asset, bounds: agy.bounds, levels: agy.levels, hulls: agy.hulls, is_lod: !!lod.isLOD, group: !!root.isGroup,
              lod_levels: lod.levels.map((l) => ({ distance: l.distance, hysteresis: l.hysteresis })) };
  out.main = describeObject(holders[0]);
  out.lods = holders.slice(1).map((h, i) => ({ name: 'LOD' + (i + 1), ...describeObject(h) }));
  const lod0 = new Set(); holders[0].traverse((o) => { if (o.isMesh) lod0.add(o.material); });
  const every = []; holders.forEach((h) => h.traverse((o) => { if (o.isMesh) every.push(o.material); }));
  const maps = new Set(); lod0.forEach((m) => Object.keys(SLOT_KEYS).forEach((k) => { if (m[k]) maps.add(m[k]); }));
  out.shared = { materials_from_lod0: every.every((m) => lod0.has(m)), distinct_maps: maps.size, baseline_textures: baseline };

  // 2. the highest level, as the package verifier frames it
  scene.add(root);
  lod.autoUpdate = false;
  holders.forEach((h, i) => { h.visible = i === 0; });
  renderer.info.reset(); renderer.info.autoReset = false;
  renderer.render(scene, camera);
  const gl = renderer.getContext(); gl.finish();
  out.render = { draw_calls: renderer.info.render.calls, triangles: renderer.info.render.triangles, gl_error: gl.getError(), context_lost: gl.isContextLost() };
  out.programs = (renderer.info.programs || []).map((p) => ({ name: p.name, diagnostics: p.diagnostics || null }));
  out.png = document.getElementById('c').toDataURL('image/png');
  scene.overrideMaterial = new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide });
  renderer.info.reset();
  out.mask_png = shot(renderer, scene, camera);
  out.mask_draw_calls = renderer.info.render.calls;
  scene.overrideMaterial = null;

  // 3. the levels switching: a perspective camera moved to each probe, in order, the LOD updating itself
  lod.autoUpdate = true;
  const pc = new THREE.PerspectiveCamera(60, 1, 0.01, 1000), cw = new THREE.Vector3(...agy.bounds.center), r = agy.bounds.radius;
  out.probes = [];
  for (const p of job.probes) {
    pc.fov = p.fov; pc.near = Math.max(p.distance - 2 * r, 0.001); pc.far = p.distance + 2 * r; pc.updateProjectionMatrix();
    pc.position.copy(cw).addScaledVector(dir, p.distance); pc.up.set(0, 1, 0); pc.lookAt(cw); pc.updateMatrixWorld(true);
    renderer.info.reset();
    renderer.render(scene, pc);
    out.probes.push({ distance: p.distance, fov: p.fov, triangles: renderer.info.render.triangles, visible: holders.map((h) => h.visible) });
  }
  out.shared.textures_after = renderer.info.memory.textures;

  // 4. the package as it is (embedded textures, no compression), through the project's own GLTFLoader, same frame
  if (job.ref_url) {
    scene.remove(root);
    try {
      const ref = await new Promise(async (ok, bad) => new GLTFLoader().parse(await new THREE.FileLoader().setResponseType('arraybuffer').loadAsync(job.ref_url), '', ok, bad));
      out.ref = describe(ref);
      scene.add(ref.scene);
      out.ref_png = shot(renderer, scene, camera);
      scene.overrideMaterial = new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide });
      out.ref_mask_png = shot(renderer, scene, camera);
    } catch (e) { out.errors.push('package GLB: ' + String((e && e.message) || e)); }
  }
  out.three_revision = THREE.REVISION;
  out.done = true;
}
main().catch((e) => { out.errors.push('page: ' + String((e && e.stack) || e)); out.done = true; });
</script></body></html>
"""


def _helpers_js() -> str:
    s = w3._PAGE
    a, b = s.index("// @helpers-begin"), s.index("// @helpers-end")
    return s[a:b]


def build_page(out_dir: Path, job: dict[str, Any], env: bytes | None, ref_glb: bytes | None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if env is not None:
        (out_dir / w3.ENV_FILE).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / w3.ENV_FILE).write_bytes(env)
    job = {**job, "env": {"mode": "page"} if env is None else {"mode": "shared", "file": w3.ENV_FILE}}
    if ref_glb is not None:
        (out_dir / "ref.glb").write_bytes(ref_glb)
        job["ref_url"] = "./ref.glb"
    html = (_PAGE.replace("__HELPERS__", _helpers_js()).replace("__ENV_JS__", w3._ENV_JS).replace("__SIZE__", str(w3.FRAME_PX))
            .replace("__JOB__", json.dumps(job).replace("</", "<\\/")))
    page = out_dir / "page.html"
    page.write_text(html, "utf-8")
    return page


# --------------------------------------------------------------------------- frames


def compare_frames(installed: bytes, mask_installed: bytes, package: bytes, mask_package: bytes) -> dict[str, float]:
    """The compression tolerance's three numbers. The mean and the share are taken over the asset's pixels (the package
    frame's silhouette), which is stricter than over the whole frame (the empty background is identical in both)."""
    from PIL import Image

    def rgba(b: bytes) -> np.ndarray:
        with Image.open(io.BytesIO(b)) as im:
            return np.asarray(im.convert("RGBA")).astype(np.float64)

    a, b = rgba(installed), rgba(package)
    mask = rgba(mask_package)[..., 3] >= 128
    if not mask.any():
        raise ValueError("the package frame has no foreground")
    d = np.abs(a[..., :3] - b[..., :3]) / 255.0
    asset = d[mask]
    return {"mean_abs": float(asset.mean()), "pixel_share": float((asset.max(axis=1) > TOL_PIXEL_DELTA).mean()),
            "mean_abs_whole_frame": float(d.mean()), "outline": w3.silhouette_iou(mask_package, mask_installed),
            "asset_pixels": int(mask.sum())}


# --------------------------------------------------------------------------- checks


def _group(name: str) -> str:
    n = name.lower()
    if n.startswith(("no loader", "no console", "shaders", "no blocked", "webgl")):
        return "import"
    if "bounds" in n:
        return "scale"
    if "material" in n or "texture" in n:
        return "materials"
    if "triangles" in n or "mesh count" in n or "draw calls" in n or n.endswith(" loads"):
        return "levels"
    if "frame" in n or "silhouette" in n or "framed" in n or "lit" in n or "reference" in n:
        return "outline"
    return "scene"


def grouped(checks: list[w3.Check]) -> list[tuple[str, str, str]]:
    """web_three's fine-grained checks folded into the install's named ones: (name, status, detail)."""
    order = ["import", "scene", "scale", "levels", "materials", "outline"]
    by: dict[str, list[w3.Check]] = {k: [] for k in order}
    for c in checks:
        by.setdefault(_group(c.name), []).append(c)
    out = []
    for g in order:
        cs = by.get(g) or []
        if not cs:
            continue
        fails = [c for c in cs if c.status == "fail"]
        skips = [c for c in cs if c.status == "skipped"]
        if fails:
            out.append((g, "failed", "; ".join(f"{c.name}: {c.detail}" for c in fails[:3])))
        elif len(skips) == len(cs):
            out.append((g, "skipped", "; ".join(f"{c.name}: {c.detail}" for c in skips[:2])))
        else:
            ok = [c for c in cs if c.status == "pass"]
            out.append((g, "passed", f"{len(ok)} check(s): " + "; ".join(c.name for c in ok[:6]) + ("; ..." if len(ok) > 6 else "")
                        + (f" ({len(skips)} skipped: {skips[0].name}: {skips[0].detail})" if skips else "")))
    return out


def probe_plan(record: dict[str, Any]) -> tuple[list[dict[str, float]], list[int]]:
    """Probe distances in the order visited, and the level that must be drawn at each: the Godot check's stops (out past
    every switch and back in, the middle of each hysteresis band from both sides) at the recorded field of view, then
    the same stops at PROBE_FOV_DEG with the distances that field of view needs."""
    base = record["switch_m"]
    probes, want = [], []
    scale = math.tan(math.radians(record["fov_deg"]) / 2) / math.tan(math.radians(PROBE_FOV_DEG) / 2)
    for fov, k in ((record["fov_deg"], 1.0), (PROBE_FOV_DEG, scale)):
        for d, level in switch_probes(base):
            probes.append({"distance": d * k, "fov": fov})
            want.append(level)
    return probes, want


def expected_hulls(project: Path, record: dict[str, Any], manifest: dict[str, Any], pkg: Path | None
                   ) -> tuple[dict[str, dict[str, list]], str, list[str]]:
    """(the hull data the loader must hand back, what it was taken from, problems found on the way).

    The package's colliders file when the package is where the record says (the record holds its absolute path). When it
    is not, the collision.json this install wrote, which is then held to the hash the record stored at install time: the
    check can say the loader passes the installed data on unchanged and that the data is what was installed, but not that
    it matches the colliders file, and it says so."""
    import re

    names = list(manifest.get("collider_glb_nodes") or [])
    colliders = pkg / f"{record['asset']}_colliders.glb" if pkg is not None else None
    if names and colliders is not None and colliders.is_file():
        data = {re.sub(r"-(conv)?col(only)?$", "", n): {"points": [float(v) for v in pts.reshape(-1)], "index": [int(i) for i in faces.reshape(-1)]}
                for n, (pts, faces) in glbtool.hull_meshes(colliders, names).items()}
        return data, "the package's colliders file", []
    f = project / record["assets"] / "collision.json"
    problems = []
    if f.is_file():
        import hashlib

        want = (record.get("files") or {}).get(f"{record['assets']}/collision.json", {}).get("sha256")
        if want and hashlib.sha256(f.read_bytes()).hexdigest() != want:
            problems.append("collision.json is not what was installed (its hash differs from the record's)")
        return json.loads(f.read_text()), "the collision.json recorded at install (the package's colliders file was not found)", problems
    return {}, "nothing (no collision.json and no package)", problems


def hull_issues(got: dict[str, Any], want: dict[str, Any], atol: float = 1e-5) -> list[str]:
    issues = []
    if sorted(got) != sorted(want):
        issues.append(f"hulls {sorted(got)}, expected {sorted(want)}")
    for name in sorted(set(got) & set(want)):
        g, w = got[name], want[name]
        gp, wp = np.asarray(g["points"], np.float64), np.asarray(w["points"], np.float64)
        if gp.shape != wp.shape or not np.allclose(gp, wp, atol=atol, rtol=0):
            issues.append(f"{name}: the points differ from the colliders file (largest difference "
                          f"{float(np.abs(gp - wp).max()) if gp.shape == wp.shape else float('nan'):.6f} m)")
        elif list(g["index"]) != list(w["index"]):
            issues.append(f"{name}: the triangle indices differ from the colliders file")
    return issues


_HOMES: list[str] = []


def private_home() -> str:
    """A new private folder (mkdtemp: unpredictable name, mode 700, ours) for one run's browser HOME. It is removed after
    the run and, because Chromium's crash handler outlives the browser and may write its settings into it a moment later,
    again when this process exits."""
    import atexit

    home = tempfile.mkdtemp(prefix="agy3d-check-home-")
    if not _HOMES:
        atexit.register(lambda: [shutil.rmtree(h, ignore_errors=True) for h in _HOMES])
    _HOMES.append(home)
    return home


def browser_env(home: Path) -> dict[str, str]:
    """The environment of the browser that runs the project's code: PATH, ``home`` (a private folder the caller makes and
    removes), LANG and what a display needs; no key of this process's environment."""
    env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"}
    for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "LD_LIBRARY_PATH", "SYSTEMROOT"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def install_checks(raw: dict[str, Any], run: dict[str, Any], record: dict[str, Any], manifest: dict[str, Any],
                   reference: bytes | None, ref_note: str, ref_issue: str | None, compressed: bool,
                   Check: type, want_hulls: tuple[dict[str, Any], str, list[str]] | None = None) -> tuple[list[Any], dict[str, Any]]:
    """The checks and the measurements (wire bytes, frame differences) from one page run."""
    shot = base64.b64decode(raw["png"].split(",", 1)[1]) if raw.get("png") else None
    mask = base64.b64decode(raw["mask_png"].split(",", 1)[1]) if raw.get("mask_png") else None
    fine, iou = w3.evaluate(raw, run["console"], run["page_errors"], run["blocked"], manifest, shot, reference, w3.PASS_RULE,
                            reference_note=ref_note, reference_issue=ref_issue, mask_png=mask)
    checks = [Check(n, {"fail": "failed"}.get(s, "passed" if s == "pass" else s), d) for n, s, d in grouped(fine)]
    measured: dict[str, Any] = {"silhouette_iou": iou, "three_revision": raw.get("three_revision"), "fine_checks": [
        {"name": c.name, "status": c.status, "detail": c.detail} for c in fine]}
    if raw.get("main") is None:
        return checks, measured
    agy = raw.get("agy") or {}
    # scene: a Group holding one LOD with the package's levels
    want_levels = record["levels"]
    problems = []
    if not agy.get("group") or not agy.get("is_lod"):
        problems.append("the loader did not return a Group holding a THREE.LOD")
    if len(agy.get("levels") or []) != len(want_levels):
        problems.append(f"{len(agy.get('levels') or [])} level(s), the package has {len(want_levels)}")
    checks.insert(1, Check("loader", "failed" if problems else "passed", "; ".join(problems) or
                           f"{record['asset']}: a Group with one THREE.LOD of {len(want_levels)} level(s) in three r{raw.get('three_revision')}",
                           {"load_ms": raw.get("load_ms")}))
    # level switch distances as recorded
    dist_problems = []
    for lv, got in zip(want_levels, agy.get("lod_levels") or []):
        want_d = 0.0 if not lv["index"] else lv["distance"]
        if abs(got["distance"] - want_d) > 1e-4 * max(1.0, want_d) or abs(got["hysteresis"] - lv["hysteresis"]) > 1e-4:
            dist_problems.append(f"LOD{lv['index']}: distance {got['distance']:.3f} / hysteresis {got['hysteresis']:.4f}, "
                                 f"the install recorded {want_d:.3f} / {lv['hysteresis']:.4f}")
    # collision
    hulls = agy.get("hulls") or {}
    recorded = record["hulls"]
    if not recorded:
        checks.append(Check("collision", "skipped", "the package has no collision hulls (manifest collider_glb_nodes is empty)"))
    else:
        data, source, pre = want_hulls if want_hulls is not None else ({}, "nothing", [])
        issues = [*pre, *hull_issues(hulls, data)]
        for name, n in recorded.items():
            h = hulls.get(name)
            if h and len(h["points"]) != 3 * n:
                issues.append(f"{name}: {len(h['points']) // 3} points, the package hull has {n}")
        checks.append(Check("collision", "failed" if issues else "passed", "; ".join(issues[:4]) or
                            f"{len(hulls)} hull(s) as plain arrays: every point within 1e-5 m of {source}, and the same triangle indices", {"hulls": {k: len(v['points']) // 3 for k, v in hulls.items()}}))
    # switching
    probes, want = probe_plan(record)
    got = raw.get("probes") or []
    if len(want_levels) < 2:
        checks.append(Check("level_switching", "skipped", "the package has one level: nothing switches"))
    else:
        sw_problems = list(dist_problems)
        lod_tris = manifest.get("lod_tris") or [manifest["tris"]]
        seen = []
        if len(got) != len(want):
            sw_problems.append(f"{len(got)} probe frames, expected {len(want)}")
        for p, level in zip(got, want):
            shown = [i for i, v in enumerate(p["visible"]) if v]
            seen.append({"distance_m": round(p["distance"], 3), "fov": p["fov"], "expected": f"LOD{level}", "visible": shown,
                         "triangles": p["triangles"]})
            if shown != [level] or int(p["triangles"]) != int(lod_tris[level]):
                sw_problems.append(f"at {p['distance']:.2f} m with a {p['fov']:g} degree lens LOD{level} ({lod_tris[level]} triangles) "
                                   f"expected, drawn {shown} with {p['triangles']:.0f} triangles")
        d = ", ".join(f"{x:.2f}" for x in record["switch_m"])
        checks.append(Check("level_switching", "failed" if sw_problems else "passed", "; ".join(sw_problems[:4]) or
                            f"rendered: the expected level alone (its triangle count in renderer.info) at every stop, moving out past "
                            f"each switch and back in, including the middle of each hysteresis band from both sides (switch at {d} m, "
                            f"hysteresis {HYSTERESIS:.1%}), at the recorded {record['fov_deg']:g} degree lens and again at {PROBE_FOV_DEG:g} "
                            "degrees with the distances that lens needs", {"probes": seen}))
    # one set of textures
    sh = raw.get("shared") or {}
    extra = (sh.get("textures_after") or 0) - (sh.get("baseline_textures") or 0)
    problems = []
    if not sh.get("materials_from_lod0"):
        problems.append("a far level has its own material objects")
    if extra != sh.get("distinct_maps"):
        problems.append(f"{extra} texture(s) uploaded to the GPU for {sh.get('distinct_maps')} distinct map(s)")
    served = [p for p, _n in run.get("served", [])]
    texture_requests = {p: served.count(p) for p in set(served) if "/textures/" in p}
    twice = sorted(p for p, n in texture_requests.items() if n > 1)
    if twice:
        problems.append(f"fetched more than once: {twice[:3]}")
    checks.append(Check("shared_textures", "failed" if problems else "passed", "; ".join(problems) or
                        f"every level draws the highest level's materials; {sh.get('distinct_maps')} map(s) fetched once each "
                        f"and {extra} texture(s) on the GPU after all levels were drawn",
                        {"texture_requests": texture_requests}))
    # the picture against the package's own
    if raw.get("ref_png") and raw.get("ref_mask_png") and shot and mask:
        ref_shot = base64.b64decode(raw["ref_png"].split(",", 1)[1])
        ref_mask = base64.b64decode(raw["ref_mask_png"].split(",", 1)[1])
        m = compare_frames(shot, mask, ref_shot, ref_mask)
        measured["frame_difference"] = m
        measured["frames"] = {"installed": shot, "package": ref_shot}
        bad = []
        if m["mean_abs"] > TOL_MEAN_ABS:
            bad.append(f"mean difference {m['mean_abs']:.2%} > {TOL_MEAN_ABS:.1%}")
        if m["pixel_share"] > TOL_PIXEL_SHARE:
            bad.append(f"{m['pixel_share']:.2%} of the asset's pixels differ by more than {TOL_PIXEL_DELTA:.0%} (> {TOL_PIXEL_SHARE:.0%})")
        if m["outline"] < TOL_OUTLINE:
            bad.append(f"outline agreement {m['outline']:.4f} < {TOL_OUTLINE}")
        name = "compression" if compressed else "loader_vs_package"
        checks.append(Check(name, "failed" if bad else "passed", "; ".join(bad) or
                            f"{'compressed files' if compressed else 'the loader'} against the package's own GLB, same camera and "
                            f"light: mean difference {m['mean_abs']:.3%} (limit {TOL_MEAN_ABS:.1%}), {m['pixel_share']:.3%} of "
                            f"{m['asset_pixels']} asset pixels over {TOL_PIXEL_DELTA:.0%} (limit {TOL_PIXEL_SHARE:.0%}), outline "
                            f"{m['outline']:.5f} (limit {TOL_OUTLINE})", m))
    elif compressed:
        checks.append(Check("compression", "skipped", "the package's GLB is not available to render the uncompressed frame"))
    return checks, measured


def wire_bytes(served: list[tuple[str, int]], record: dict[str, Any]) -> dict[str, int]:
    """Bytes the page received, from what its server sent: the asset's own files, and the transcoder."""
    assets, basis = "/" + record["assets_url"].strip("/") + "/", "/" + record["basis_url"].strip("/") + "/"
    a = sum(n for p, n in served if p.startswith(assets))
    b = sum(n for p, n in served if p.startswith(basis))
    return {"asset_bytes": a, "transcoder_bytes": b, "total_bytes": a + b, "requests": len(served)}


async def run(project: Path, record: dict[str, Any], manifest: dict[str, Any], pkg: Path | None, three_path: str | None,
              Check: type, *, timeout: float = 120.0, out_dir: Path | None = None) -> tuple[list[Any], dict[str, Any]]:
    """Run the installed loader in a browser and judge it. Skips (with the reason) where the check cannot run."""
    asset_name(record.get("asset"))  # a name pasted into code below
    project = project.resolve()  # the paths below are joined to it: a relative --project must not become a bare module name
    if pkg is not None:
        pkg = pkg.resolve()
    v = w3.WebThreeValidator()
    if three_path is None:
        return [Check("in_project", "skipped", "node_modules/three was not found: run npm install, then agy3d install again")], {}
    if (why := v._unavailable()) is not None:
        return [Check("in_project", "skipped", why)], {}
    exe = v.binary()
    layout_assets = project / record["assets"]
    mounts = {"/" + record["assets_url"].strip("/") + "/": layout_assets, "__code": project / record["dest"],
              "__three": Path(three_path)}
    if record.get("basis"):
        mounts["/" + record["basis_url"].strip("/") + "/"] = project / record["basis"]
    bounds = manifest["glb_bounds_m"]
    ref_glb = None
    reference, ref_note, ref_issue = None, "no package folder to derive a reference from", None
    if pkg is not None and (pkg / f"{record['asset']}.glb").is_file():
        ref_glb = (pkg / f"{record['asset']}.glb").read_bytes()
        if (record.get("compression") or {}).get("state") in ("compressed", "kept"):
            # KTX2 is encoded from the PNG maps (web.lossless_level0), so its tolerance is measured against them, not
            # against the package's WebP copy of them
            from antigravity3d.deliver.web import lossless_level0

            maps = [m for m in (manifest.get("web_textures") or {}).get("maps") or [] if m.get("webp")]
            texs = {m[k]: (pkg / m[k]).read_bytes() for m in maps for k in ("webp", "texture") if (pkg / m[k]).is_file()}
            with contextlib.suppress(ValueError):  # a package without the PNGs: the frame against what it ships
                ref_glb = lossless_level0(ref_glb, texs, manifest)
        usd = next(iter(sorted(pkg.glob("*.usd"))), None)
        reference, ref_note, ref_issue = await asyncio.to_thread(w3._usd_reference, usd, bounds)
    probes, _ = probe_plan(record)
    job = {"loader_url": "./_m/__code/index.js", "export": f"load{pascal(record['asset'])}", "probes": probes,
           "camera": w3.web_camera(bounds),
           "rig": {"lights": [[list(p), i] for p, i in w3.LIGHTS], "env_sky": w3.ENV_SKY, "env_ground": w3.ENV_GROUND}}
    own = out_dir is None
    work = Path(tempfile.mkdtemp(prefix="agy3d-web-check-")) if own else out_dir
    home_dir = private_home()  # private to this run, removed afterwards
    try:
        env, export_env = w3._environment_plan(exe)
        build_page(work, job, env, ref_glb)
        try:
            run_ = await asyncio.wait_for(w3.run_page(work, exe, timeout, export_env=export_env, mounts=mounts, env=browser_env(Path(home_dir)),
                                                  keep_browser=False), timeout + 15)
        except w3.PageServerUnavailable as e:
            return [Check("in_project", "skipped", f"cannot serve the page on this machine: {e}")], {}
        except Exception as e:  # noqa: BLE001  browser launch / navigation / timeout
            return [Check("in_project", "failed", f"browser run failed: {type(e).__name__}: {e}"[:600])], {}
        compressed = bool((record.get("compression") or {}).get("state") in ("compressed", "kept"))
        want = await asyncio.to_thread(expected_hulls, project, record, manifest, pkg)
        checks, measured = install_checks(run_["raw"], run_, record, manifest, reference, ref_note, ref_issue, compressed, Check, want)
        measured["wire"] = wire_bytes(run_["served"], record)
        measured["browser"] = run_["browser"]
        measured["load_ms"] = run_["raw"].get("load_ms")
        measured["console"] = run_["console"]
        measured["served"] = run_["served"]
        raw = run_["raw"]
        if raw.get("png"):
            measured["screenshot"] = base64.b64decode(raw["png"].split(",", 1)[1])
        return checks, measured
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- the React component

_REACT_ENTRY = """
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { Canvas } from "@react-three/fiber";
import { %(comp)s } from %(module)s;
const out = { done: false, errors: [] };
window.__agy_react = out;
const job = %(job)s;
const finish = (state, group) => {
  let frames = 0;
  const tick = () => {
    if (++frames < 6) return requestAnimationFrame(tick);
    const gl = state.gl;
    let lod = null; state.scene.traverse((o) => { if (o.isLOD) lod = o; });
    out.found_lod = !!lod; out.levels = lod ? lod.levels.length : 0;
    out.triangles = gl.info.render.triangles; out.draw_calls = gl.info.render.calls;
    const c = document.querySelector("canvas"), g = document.createElement("canvas");
    g.width = c.width; g.height = c.height; const x = g.getContext("2d"); x.drawImage(c, 0, 0);
    const px = x.getImageData(0, 0, g.width, g.height).data; let drawn = 0;
    for (let i = 3; i < px.length; i += 4) if (px[i] > 8) drawn++;
    out.drawn_share = drawn / (g.width * g.height);
    out.asset = group.userData.agy3d.asset; out.done = true;
  };
  requestAnimationFrame(tick);
};
let state = null, group = null;
const maybe = () => { if (state && group) finish(state, group); };
createRoot(document.getElementById("root")).render(
  createElement(Canvas, { gl: { preserveDrawingBuffer: true, antialias: false, alpha: true }, dpr: 1,
      camera: { position: job.camera, fov: 50, near: 0.01, far: 1000 },
      onCreated: (s) => { s.camera.lookAt(...job.target); state = s; maybe(); } },
    createElement(%(comp)s, { onLoad: (g) => { group = g; maybe(); }, onError: (e) => { out.errors.push(String((e && e.stack) || e)); out.done = true; } })));
setTimeout(() => { if (!out.done) { out.errors.push("the component did not load within 60 s"); out.done = true; } }, 60000);
"""


#: the bundlers the component check can use, in the order they are looked for
BUNDLERS = ("esbuild", "rolldown")


def find_bundler(project: Path) -> tuple[str, Path] | None:
    """(name, executable) of the project's own bundler, searched where the project's packages are searched: its own
    folder and, above it, only up to its workspace or repository root. esbuild is what Vite ships up to version 7;
    Vite 8 ships Rolldown instead. Runs 2 and 3 of the website task (Vite 8.3.8) each installed three props whose
    component check read "skipped no esbuild in the project's node_modules/.bin (Vite ships one)"."""
    for name in BUNDLERS:
        for d in search_dirs(project):
            p = d / "node_modules" / ".bin" / name
            if p.exists():
                return name, p
    return None


def _bundle(bundler: str, exe: Path, project: Path, entry: str, out: Path) -> tuple[int, str]:
    """Bundle ``entry`` (module source that imports from the project's packages) to ``out``. Returns (exit code, the
    bundler's error text); raises ChildError when it does not start or finish."""
    define = 'process.env.NODE_ENV'
    if bundler == "esbuild":
        code, _out, err = run_child(
            [str(exe), "--bundle", "--format=esm", "--platform=browser", "--target=es2020", f'--define:{define}="production"',
             f"--outfile={out}", "--sourcefile=entry.jsx", "--loader=jsx"],
            cwd=project, timeout=120, input=entry)  # stdin is resolved from the cwd; a bare environment, its own session
        return code, err
    # Rolldown reads no entry from stdin, and a bare import ("react") is looked for from the folder of the file that
    # imports it. So the entry is a file under the project's own node_modules (where the installed component finds its
    # react; in a workspace the bundler may sit in the root's node_modules, which can hold another react or none), in a
    # folder of its own under the conventional .cache, removed afterwards. A project with no node_modules of its own
    # has its packages where the bundler is. An empty .cache that this made is removed too; one install may remove it
    # between another's two steps, so the folder is made in two tries.
    own = project / "node_modules"
    cache = (own if own.is_dir() else exe.parent.parent) / ".cache"
    made_cache = not cache.exists()
    folder: Path | None = None
    try:
        for attempt in (1, 2):
            cache.mkdir(exist_ok=True)
            try:
                folder = Path(tempfile.mkdtemp(prefix="agy3d-react-check-", dir=cache))
                break
            except FileNotFoundError:
                if attempt == 2:
                    raise
        assert folder is not None
        (folder / "entry.js").write_text(entry, encoding="utf-8")
        code, _out, err = run_child(
            [str(exe), str(folder / "entry.js"), "--file", str(out), "--format", "esm", "--platform", "browser",
             "--transform.define", f'{define}:"production"'], cwd=project, timeout=120)
        return code, err
    finally:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        if made_cache:
            with contextlib.suppress(OSError):
                cache.rmdir()


# Warnings the project's own libraries print whatever the installed component does: not the install's to fail on. React
# Three Fiber 9.8.2 (the newest on 2026-11-24) and 8.17.10 make a THREE.Clock in every Canvas, and three.js r183 and later
# warn "THREE.Clock: This module has been deprecated. Please use THREE.Timer instead." (three 0.187.0, build/three.core.js).
# The component check then failed every React project on current versions. Each one is named in the check's detail.
LIBRARY_NOTICES = ("THREE.Clock: This module has been deprecated",)


async def run_react(project: Path, record: dict[str, Any], manifest: dict[str, Any], three_path: str | None, Check: type,
                    *, timeout: float = 120.0) -> list[Any]:
    """Mount the installed component in a React Three Fiber Canvas (bundled with the project's own bundler, the one its
    Vite ships: ``find_bundler``) and look at what it drew. Skipped, with the reason, where it cannot run."""
    v = w3.WebThreeValidator()
    if (why := v._unavailable()) is not None:
        return [Check("react_component", "skipped", why)]
    project = project.resolve()  # the generated import of react.js is an absolute path, whatever --project was
    found = find_bundler(project)
    if found is None:
        return [Check("react_component", "skipped", "no esbuild and no rolldown in the project's node_modules/.bin (Vite ships "
                                                      "one of them): the component needs a bundler to be checked")]
    bundler, exe = found
    if three_path is None:
        return [Check("react_component", "skipped", "node_modules/three was not found")]
    comp = pascal(asset_name(record["asset"]))
    b = manifest["glb_bounds_m"]
    centre = [(x + y) / 2 for x, y in zip(*b)]
    radius = float(np.linalg.norm(np.asarray(b[1]) - np.asarray(b[0])) / 2)
    job = {"camera": [centre[0] + radius * 2.2, centre[1] + radius * 1.6, centre[2] + radius * 3.2], "target": centre}
    entry = _REACT_ENTRY % {"comp": comp, "module": json.dumps(str(project / record["dest"] / "react.js")), "job": json.dumps(job)}
    work = Path(tempfile.mkdtemp(prefix="agy3d-react-check-"))
    home_dir = private_home()
    try:
        try:
            code, err = await asyncio.to_thread(_bundle, bundler, exe, project, entry, work / "bundle.js")
        except ChildError as e:
            return [Check("react_component", "failed", f"the project's {bundler} did not finish: {e}")]
        except OSError as e:  # the entry file could not be written beside the project's packages
            return [Check("react_component", "skipped", f"the project's {bundler} needs its entry inside node_modules/.cache, "
                                                          f"which could not be written ({type(e).__name__})")]
        if code != 0:
            return [Check("react_component", "failed", f"the project's {bundler} could not bundle the component: {err[-500:]}")]
        (work / "react.html").write_text('<!doctype html><meta charset="utf-8"><div id="root" style="width:512px;height:512px"></div>'
                                         '<script type="module" src="./bundle.js"></script>')
        mounts = {"/" + record["assets_url"].strip("/") + "/": project / record["assets"]}
        if record.get("basis"):
            mounts["/" + record["basis_url"].strip("/") + "/"] = project / record["basis"]
        try:
            run_ = await asyncio.wait_for(
                w3.run_page(work, v.binary(), timeout, mounts=mounts, page_name="react.html", env=browser_env(Path(home_dir)), keep_browser=False,
                            ready="window.__agy_react && window.__agy_react.done", result="window.__agy_react"), timeout + 15)
        except Exception as e:  # noqa: BLE001
            return [Check("react_component", "failed", f"browser run failed: {type(e).__name__}: {e}"[:500])]
        raw = run_["raw"]
        errs = list(raw.get("errors", [])) + [f"pageerror: {e}" for e in run_["page_errors"]] + \
            [f"console.error: {c['text'][:200]}" for c in run_["console"] if c["type"] == "error"]
        library = [c["text"][:200] for c in run_["console"] if c["type"] == "warning" and any(i in c["text"] for i in LIBRARY_NOTICES)]
        warns = [c["text"][:200] for c in run_["console"] if c["type"] == "warning"
                 and not any(i in c["text"] for i in (*w3.PASS_RULE.ignored_console, *LIBRARY_NOTICES))]
        problems = errs[:3] + warns[:2] + [f"blocked: {x}" for x in run_["blocked"][:2]]
        if not raw.get("found_lod"):
            problems.append("no THREE.LOD in the Canvas's scene")
        want_levels = len(record["levels"])
        if raw.get("levels") != want_levels:
            problems.append(f"{raw.get('levels')} level(s) in the LOD, the package has {want_levels}")
        if (raw.get("drawn_share") or 0) < 0.01:
            problems.append(f"the Canvas drew almost nothing ({(raw.get('drawn_share') or 0):.3%} of its pixels)")
        return [Check("react_component", "failed" if problems else "passed", "; ".join(problems) or
                      f"<{comp} /> mounted in a React Three Fiber Canvas bundled with the project's {bundler}: a THREE.LOD of "
                      f"{want_levels} level(s), {raw.get('triangles'):.0f} triangles in {raw.get('draw_calls')} draw call(s), "
                      f"{raw.get('drawn_share'):.1%} of the frame drawn, no console error or warning"
                      + (f" from it (the project's own libraries said: {'; '.join(dict.fromkeys(library))})" if library else ""),
                      {"triangles": raw.get("triangles")})]
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(home_dir, ignore_errors=True)


__all__ = ["run", "run_react", "install_checks", "compare_frames", "probe_plan", "wire_bytes"]
