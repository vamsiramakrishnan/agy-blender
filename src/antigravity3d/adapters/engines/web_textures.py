"""The textures of a web_three package's GLBs in a web format, when that is shown not to change the picture.

Why: run 1 of the website task (benchmarks/results/site_run1) shipped containment_gate.glb at 17,369,108 bytes, four
4096 px PNG maps embedded, and the agent had no lever but to shrink the object. The same GLB's PNG maps re-encoded as
WebP at the same pixel size were 2,450,008 bytes instead of 9,593,560, at a PSNR of 36 to 40 dB against the PNG
render. PNG is lossless and a page pays for that in every byte it downloads; a browser decodes both formats to the same
RGBA8 on the graphics card, so the download is all that changes.

What it does (``web_format``), for a ``web_three`` package only:

1. Encodes every PNG map LOD0 embeds as WebP (Pillow, libwebp; ``WEBP_QUALITY`` per role, ``WEBP_METHOD``) and keeps an
   encoding only where it is smaller than the PNG (a near-black emissive map is often smaller as PNG).
2. Renders the PNG GLB and the WebP GLB in the same headless three.js page (the package check's vendored three.js,
   camera, lights and environment): the check's own 512 px frame, and close-ups where one texel is about one pixel, at
   points of the surface found by casting rays through the check's frame. Every frame of the WebP GLB is compared with
   the PNG one by the tolerance the installer's KTX2 compression is held to (``deliver/web_check.compare_frames`` and
   its ``TOL_*``), over the asset's pixels.
3. Keeps WebP only when every frame is inside the tolerance; otherwise, or when the frames could not be rendered, the
   package keeps PNG. The manifest's ``web_textures`` record says which, the bytes before and after, the measured
   differences and the reason.

With WebP, LOD0 embeds the WebP maps (``EXT_texture_webp``, required, no PNG fallback: a fallback would be the PNG
again in the same file); the far levels reference ``textures/<map>.webp``, so every level binds the same image content
(the package check compares it); the PNG maps stay in ``textures/``: the USD uses them, and the installer's KTX2
compression encodes from them (``deliver/web.py``), never from a lossy copy.

A decision is kept in this process for the bytes it was made for (``_DECIDED``): the package written for deliver_package's
checks and the one written by the export are the same bytes, so the export applies the same files without encoding or
rendering again.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import shutil
import tempfile
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from antigravity3d.adapters.engines import web_three as w3
from antigravity3d.deliver import glb as glbtool

ENGINE = "web_three"
# Quality per map role. 90 for colour and data maps; 95 for normal maps, whose error is an angle (my probe on run 1's
# containment_gate: the ORM map is the largest after encoding at 90, 1,073,782 bytes; PSNR 38.5 to 40.2 dB over three
# orbit views and 35.7 dB on an extreme close-up). The frame comparison below decides whether they ship.
WEBP_QUALITY = {"albedo": 90, "orm": 90, "emissive": 90, "normal": 95}
# libwebp's effort, 0 (fast) to 6. Measured on run 1's containment_gate (four 4096 x 2048 maps, four threads): method 4
# 3,519,038 bytes in 2.0 s, method 6 3,441,008 bytes in 3.3 s. 2 % of the bytes is not worth 1.3 s inside a tool call
# the host abandons after 3 minutes.
WEBP_METHOD = 4
WEBP_MIME = "image/webp"
# Close-ups taken beside the check's frame, each one texel to about one pixel (where texture compression shows first).
CLOSE_UPS = 3
# decisions kept in this process (``web_format``): the checks' package and the export's are the same bytes
_DECIDED: OrderedDict[str, tuple[dict[str, Any], Candidate | None]] = OrderedDict()
_DECIDED_MAX = 4


@dataclass
class Candidate:
    """The WebP version of a package's GLBs, not yet written."""
    glbs: dict[str, bytes]                    # package-relative GLB name -> new bytes
    files: dict[str, bytes]                   # textures/<map>.webp -> bytes
    images: list[dict[str, Any]] = field(default_factory=list)


def encoder() -> str:
    from PIL import Image, features

    return f"Pillow {getattr(Image, '__version__', '?')}, libwebp {features.version('webp')}"


def _encode_one(png: bytes, quality: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        im.load()
        buf = io.BytesIO()
        # exact: the colour under a fully transparent pixel is kept (a cut-out's edge is filtered from it)
        im.save(buf, "WEBP", quality=quality, method=WEBP_METHOD, exact=True)
    return buf.getvalue()


def encode(pkg: Path, manifest: dict[str, Any]) -> tuple[Candidate | None, list[dict[str, Any]], str]:
    """(the WebP candidate or None, a record per map, why there is no candidate)."""
    asset = manifest["asset"]
    lod0 = f"{asset}.glb"
    records = (manifest.get("glb_images") or {}).get(lod0) or []
    role_of = {t["path"]: t["role"] for t in manifest.get("textures") or []}
    data = (pkg / lod0).read_bytes()
    embedded = {hashlib.sha256(b).hexdigest(): (b, mime) for b, mime in glbtool.embedded_images(data)}
    todo: list[tuple[str, str, str, bytes]] = []   # (sha, png rel, role, bytes)
    maps: list[dict[str, Any]] = []
    for rec in records:
        rel = rec.get("texture")
        if rec.get("storage") != "embedded" or rec.get("sha256") not in embedded or not rel:
            continue
        png, mime = embedded[rec["sha256"]]
        if mime != "image/png" or any(t[0] == rec["sha256"] for t in todo):
            continue  # a JPEG base colour is already lossy: it is not encoded a second time
        todo.append((rec["sha256"], rel, role_of.get(rel, "albedo"), png))
    if not todo:
        return None, maps, "LOD0 embeds no PNG map"
    with ThreadPoolExecutor(min(4, len(todo))) as pool:  # libwebp releases the GIL: four 4096 px maps in 2.0 s, not 5.0
        webps = list(pool.map(lambda t: _encode_one(t[3], WEBP_QUALITY.get(t[2], 90)), todo))
    swap: dict[str, tuple[bytes, str]] = {}
    files: dict[str, bytes] = {}
    names = set(manifest.get("files") or [])
    for (sha, rel, role, png), webp in zip(todo, webps):
        target = str(Path(rel).with_suffix(".webp").as_posix())
        rec = {"texture": rel, "role": role, "quality": WEBP_QUALITY.get(role, 90), "png_bytes": len(png),
               "webp_bytes": len(webp), "webp": None}
        if len(webp) >= len(png):
            rec["kept_png"] = "the WebP encoding is not smaller"
        elif target in names or target in files:
            rec["kept_png"] = f"{target} is already a file of the package"
        else:
            rec["webp"] = target
            swap[sha] = (webp, WEBP_MIME)
            files[target] = webp
        maps.append(rec)
    if not swap:
        return None, maps, "no map is smaller as WebP"
    uris = {quote(r["texture"]): (quote(r["webp"]), WEBP_MIME) for r in maps if r["webp"]}
    glbs = {lod0: glbtool.swap_images(data, embedded=swap)}
    for name, recs in (manifest.get("glb_images") or {}).items():
        if name != lod0 and any(r.get("storage") == "uri" and r.get("uri") in uris for r in recs):
            glbs[name] = glbtool.swap_images((pkg / name).read_bytes(), uris=uris)
    return Candidate(glbs, files, maps), maps, ""


# --------------------------------------------------------------------------- the comparison page

_COMPARE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>agy3d web texture comparison</title>
<!-- Self-contained like the package check's page (web_three._PAGE): the vendored three.js under ./vendor, the two GLBs
     under ./glb. Renders the PNG GLB (a) and the WebP GLB (b) at the same cameras. -->
<script type="importmap">{"imports":{"three":"./vendor/three.module.min.js"}}</script>
<style>html,body{margin:0;background:transparent}canvas{display:block}</style></head>
<body><canvas id="c" width="__SIZE__" height="__SIZE__"></canvas>
<script type="application/json" id="job">__JOB__</script>
<script type="module">
import * as THREE from 'three';
import { GLTFLoader } from './vendor/examples/jsm/loaders/GLTFLoader.js';
const job = JSON.parse(document.getElementById('job').textContent);
const out = { done: false, errors: [], frames: [], points: [] };
window.__agy = out;
const load = (url) => new THREE.FileLoader().setResponseType('arraybuffer').loadAsync(url);
const parse = (buf) => new Promise((ok, bad) => new GLTFLoader().parse(buf, '', ok, (e) => bad(e)));
__ENV_JS__
async function environment(renderer) {
  if (job.env.mode === 'shared') return sharedEnvironment('./' + job.env.file);
  return gradientEnvironment(renderer, job.rig).texture;
}
const SLOTS = ['map', 'normalMap', 'aoMap', 'roughnessMap', 'metalnessMap', 'emissiveMap'];
function dispose(root) {
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.geometry.dispose();
    for (const m of [].concat(o.material)) {
      for (const k of SLOTS) if (m[k]) { if (m[k].image && m[k].image.close) m[k].image.close(); m[k].dispose(); }
      m.dispose();
    }
  });
}
async function main() {
  const canvas = document.getElementById('c');
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1); renderer.setSize(job.camera.size_px, job.camera.size_px, false);
  renderer.setClearColor(0x000000, 0); renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.toneMapping = THREE.NoToneMapping;
  const scene = new THREE.Scene();
  scene.environment = await environment(renderer);
  for (const [pos, inten] of job.rig.lights) { const d = new THREE.DirectionalLight(0xffffff, inten); d.position.set(...pos); scene.add(d); }
  const cam = job.camera, w = cam.ortho_width / 2;
  const dir = new THREE.Vector3(...cam.view_dir).normalize(), center = new THREE.Vector3(...cam.center);
  const beauty = new THREE.OrthographicCamera(-w, w, w, -w, cam.diameter * 0.01, cam.diameter * 5);
  beauty.position.copy(center).addScaledVector(dir, cam.distance); beauty.up.set(0, 1, 0); beauty.lookAt(center); beauty.updateMatrixWorld(true);
  const white = new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide });
  const shot = (camera) => {
    renderer.render(scene, camera); renderer.getContext().finish();
    const png = canvas.toDataURL('image/png');
    scene.overrideMaterial = white; renderer.render(scene, camera); renderer.getContext().finish();
    const mask = canvas.toDataURL('image/png'); scene.overrideMaterial = null;
    return { png, mask };
  };
  let cameras = null;
  for (const which of ['a', 'b']) {
    let gltf; const t0 = performance.now();
    try { gltf = await parse(await load(job.glb[which])); } catch (e) { out.errors.push(which + ': ' + String((e && e.message) || e)); out.done = true; return; }
    scene.add(gltf.scene); gltf.scene.updateMatrixWorld(true);
    if (cameras === null) {  // the close-ups: points of the surface seen in the check's frame, spread apart
      const ray = new THREE.Raycaster(), hits = [];
      for (let i = 0; i < 9; i++) for (let j = 0; j < 9; j++) {
        ray.setFromCamera(new THREE.Vector2(-0.8 + 0.2 * i, -0.8 + 0.2 * j), beauty);
        const h = ray.intersectObject(gltf.scene, true)[0];
        if (h) hits.push(h.point.clone());
      }
      const pick = [];
      if (hits.length) {
        hits.sort((p, q) => p.distanceTo(center) - q.distanceTo(center)); pick.push(hits[0]);
        while (pick.length < job.close_ups && pick.length < hits.length) {
          let best = null, far = -1;
          for (const p of hits) { const d = Math.min(...pick.map((q) => q.distanceTo(p))); if (d > far) { far = d; best = p; } }
          if (far <= 0) break; pick.push(best);
        }
      }
      const cw = job.close_up_width / 2;
      cameras = [['frame', beauty]].concat(pick.map((p, k) => {
        const c = new THREE.OrthographicCamera(-cw, cw, cw, -cw, cam.diameter * 0.01, cam.diameter * 5);
        c.position.copy(p).addScaledVector(dir, cam.distance); c.up.set(0, 1, 0); c.lookAt(p); c.updateMatrixWorld(true);
        out.points.push(p.toArray()); return ['close_up_' + (k + 1), c];
      }));
    }
    const t1 = performance.now();
    for (const [name, c] of cameras) out.frames.push({ glb: which, view: name, ...shot(c) });
    out.ms = (out.ms || []).concat([[which, Math.round(t1 - t0), Math.round(performance.now() - t1)]]);
    scene.remove(gltf.scene); dispose(gltf.scene);
  }
  out.done = true;
}
main().catch((e) => { out.errors.push('page: ' + String((e && e.stack) || e)); out.done = true; });
</script></body></html>
"""


def _close_up_width(manifest: dict[str, Any], diameter: float) -> float:
    """Frame width in metres at which one texel of the shipped maps covers about one pixel of the 512 px frame."""
    atlas = manifest.get("texture_atlas") or {}
    density = atlas.get("texel_density_px_per_m") or (manifest.get("texel_density") or {}).get("shipped_mean_px_per_m")
    # never wider than half the object: a coarse atlas (128 px/m: four metres a frame) would otherwise frame less
    # closely than the check's own frame
    return min(w3.FRAME_PX / float(density), diameter / 2) if density else diameter / 4


async def measure(manifest: dict[str, Any], png_glb: bytes, webp_glb: bytes, timeout: float = 90.0) -> dict[str, Any]:
    """Render both GLBs at the same cameras and compare each WebP frame with its PNG frame. ``{"passed": True | False |
    None, "frames": [...], ...}``; None: the frames could not be rendered here (``why``)."""
    from antigravity3d.deliver.web_check import (
        TOL_MEAN_ABS,
        TOL_OUTLINE,
        TOL_PIXEL_DELTA,
        TOL_PIXEL_SHARE,
        compare_frames,
    )

    tolerance = {"mean_abs": TOL_MEAN_ABS, "pixel_share": TOL_PIXEL_SHARE, "pixel_delta": TOL_PIXEL_DELTA, "outline": TOL_OUTLINE}
    v = w3.WebThreeValidator()
    if (why := v._unavailable()) is not None:
        return {"passed": None, "why": why, "tolerance": tolerance}
    bounds = manifest.get("glb_bounds_m")
    if not bounds:
        return {"passed": None, "why": "the manifest has no glb_bounds_m to place the camera", "tolerance": tolerance}
    exe = v.binary()
    env, _ = w3._environment_plan(exe)
    camera = w3.web_camera(bounds)
    t0 = time.perf_counter()
    work = Path(tempfile.mkdtemp(prefix="agy3d-webp-"))
    try:
        w3.build_page(work, png_glb, bounds, env=env)  # the vendored three.js, the environment file, glb/main.glb
        (work / w3.GLB_DIR / "webp.glb").write_bytes(webp_glb)
        job = {"glb": {"a": f"./{w3.GLB_DIR}/main.glb", "b": f"./{w3.GLB_DIR}/webp.glb"}, "camera": camera,
               "close_ups": CLOSE_UPS, "close_up_width": _close_up_width(manifest, camera["diameter"]),
               "env": {"mode": "page"} if env is None else {"mode": "shared", "file": w3.ENV_FILE},
               "rig": {"lights": [[list(p), i] for p, i in w3.LIGHTS], "env_sky": w3.ENV_SKY, "env_ground": w3.ENV_GROUND}}
        html = (_COMPARE_PAGE.replace("__SIZE__", str(w3.FRAME_PX)).replace("__ENV_JS__", w3._ENV_JS)
                .replace("__JOB__", json.dumps(job).replace("</", "<\\/")))
        (work / "compare.html").write_text(html, "utf-8")
        try:
            run = await asyncio.wait_for(w3.run_page(work, exe, timeout, page_name="compare.html"), timeout + 15)
        except Exception as e:  # noqa: BLE001 - no browser run is no measurement, never a verdict on the maps
            return {"passed": None, "why": f"browser run failed: {type(e).__name__}: {e}"[:300], "tolerance": tolerance}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    raw = run["raw"]
    errors = [*raw.get("errors", []), *run["page_errors"],
              *(f"console {c['type']}: {c['text']}" for c in run["console"] if c["type"] == "error"), *run["blocked"]]
    if errors or not raw.get("frames"):
        return {"passed": None, "why": "; ".join(errors)[:300] or "no frames were rendered", "tolerance": tolerance}
    import base64

    def png(url: str) -> bytes:
        return base64.b64decode(url.split(",", 1)[1])

    frames = {(f["glb"], f["view"]): f for f in raw["frames"]}
    out = []
    for (which, view), b in frames.items():
        if which != "b" or ("a", view) not in frames:
            continue
        a = frames[("a", view)]
        try:
            d = compare_frames(png(b["png"]), png(b["mask"]), png(a["png"]), png(a["mask"]))
        except ValueError as e:  # a close-up with nothing of the asset in it
            out.append({"view": view, "skipped": str(e)})
            continue
        ok = d["mean_abs"] <= TOL_MEAN_ABS and d["pixel_share"] <= TOL_PIXEL_SHARE and d["outline"] >= TOL_OUTLINE
        out.append({"view": view, "passed": ok, **{k: round(v, 6) if isinstance(v, float) else v for k, v in d.items()}})
    measured = [f for f in out if "passed" in f]
    return {"passed": bool(measured) and all(f["passed"] for f in measured), "frames": out, "tolerance": tolerance,
            "close_up_width_m": round(job["close_up_width"], 4), "browser": run.get("browser"),
            "seconds": round(time.perf_counter() - t0, 2), "page_ms": raw.get("ms")}


# --------------------------------------------------------------------------- the package


RULE = ("the GLBs carry WebP maps (EXT_texture_webp) only when every frame of the WebP GLB (the three.js check's frame "
        "and close-ups at about one texel per pixel) is inside the installer's compression tolerance against the PNG "
        "GLB; otherwise PNG. The PNG maps in textures/ stay: the USD uses them and the installer's KTX2 compression "
        "encodes from them")


def _key(pkg: Path, manifest: dict[str, Any]) -> str:
    h = hashlib.sha256(json.dumps([WEBP_QUALITY, WEBP_METHOD, encoder(), sorted(manifest.get("glb_images") or {})]).encode())
    for name in sorted(manifest.get("glb_images") or {}):
        h.update(name.encode() + hashlib.sha256((pkg / name).read_bytes()).digest())
    return h.hexdigest()


async def web_format(pkg: Path, data: dict[str, Any], timeout: float = 90.0) -> dict[str, Any] | None:
    """Give a just-written ``web_three`` package (``pkg``, its manifest ``data``) WebP maps when that does not change the
    picture; ``data`` and ``pkg/manifest.json`` are updated either way with the ``web_textures`` record, which is returned
    with its ``timings`` (seconds; not written to the manifest).
    None (nothing done) for another target. Never raises for a map or a browser: the package then keeps PNG."""
    if data.get("target_engine") != ENGINE:
        return None
    pkg = Path(pkg)
    t0 = time.perf_counter()
    key = _key(pkg, data)
    lod0 = f"{data['asset']}.glb"
    before = (pkg / lod0).stat().st_size
    if key in _DECIDED:
        _DECIDED.move_to_end(key)
        record, cand = copy.deepcopy(_DECIDED[key][0]), _DECIDED[key][1]
        timings: dict[str, Any] = {"reused": True}
    else:
        try:
            cand, maps, why = await asyncio.to_thread(encode, pkg, data)
        except Exception as e:  # noqa: BLE001 - an image Pillow cannot read: the package keeps what it has
            cand, maps, why = None, [], f"encoding failed: {type(e).__name__}: {e}"[:300]
        timings = {"reused": False, "encode_s": round(time.perf_counter() - t0, 2)}
        record = {"format": "png", "encoder": encoder(), "method": WEBP_METHOD, "maps": maps, "rule": RULE}
        if cand is None:
            record["reason"] = why
        else:
            m = await measure(data, (pkg / lod0).read_bytes(), cand.glbs[lod0], timeout)
            timings.update(compare_s=m.pop("seconds", None), page_ms=m.pop("page_ms", None))
            record["measured"] = m
            if m["passed"] is True:
                record["format"] = "webp"
            elif m["passed"] is None:
                record["reason"] = f"the frames could not be compared, so nothing lossy ships: {m.get('why')}"
            else:
                worst = [f for f in m["frames"] if f.get("passed") is False]
                record["reason"] = ("outside the tolerance in " + ", ".join(
                    f"{f['view']} (mean {f['mean_abs']:.4f}, {f['pixel_share']:.2%} of the asset's pixels over "
                    f"{m['tolerance']['pixel_delta']:.0%})" for f in worst[:3]))
        if record["format"] != "webp":
            cand = None
        if (record.get("measured") or {}).get("passed", False) is not None:  # "could not compare" is asked again next time
            _DECIDED[key] = (copy.deepcopy(record), cand)
            while len(_DECIDED) > _DECIDED_MAX:
                _DECIDED.popitem(last=False)
    record["glb_bytes"] = {"before": before, "after": before}
    if cand is not None:
        problem = await asyncio.to_thread(_apply, pkg, data, cand)
        if problem:
            record.update(format="png", reason=f"the package with WebP maps did not validate, so it keeps PNG: {problem}")
        else:
            record["glb_bytes"]["after"] = (pkg / lod0).stat().st_size
    data["web_textures"] = record
    (pkg / "manifest.json").write_text(json.dumps(data, indent=1))
    # how long it took is run telemetry, not package content (the manifest is a function of the scene and the budget)
    timings["total_s"] = round(time.perf_counter() - t0, 2)
    return {**record, "timings": timings}


def _apply(pkg: Path, data: dict[str, Any], cand: Candidate) -> str:
    """Write the candidate into the package and bring the manifest along; '' or the validation problem (everything is then
    put back as it was)."""
    from antigravity3d.kernel import provenance
    from antigravity3d.kernel.package import _glb_image_records, web_download_record
    from antigravity3d.kernel.validator import validate_package

    saved = copy.deepcopy(data)
    old = {name: (pkg / name).read_bytes() for name in cand.glbs}
    for rel, b in {**cand.files, **cand.glbs}.items():
        (pkg / rel).write_bytes(b)
    data["files"] = [*data["files"], *sorted(cand.files)]
    sha = {rel: hashlib.sha256(b).hexdigest() for rel, b in {**cand.files, **cand.glbs}.items()}
    for rel, b in {**cand.files, **cand.glbs}.items():
        data["file_hashes"][rel] = {"sha256": sha[rel], "bytes": len(b)}
    hashes = {rel: meta["sha256"] for rel, meta in data["file_hashes"].items() if rel.startswith("textures/")}
    texture_of: dict[str, str] = {}
    for rel, digest in hashes.items():
        texture_of.setdefault(digest, rel)
    data["glb_images"] = {f: _glb_image_records(pkg / f, texture_of, hashes) for f in data["glb_images"]}
    data["web_download"] = web_download_record(pkg, f"{data['asset']}.glb", data["glb_images"][f"{data['asset']}.glb"])
    prov = data.get("provenance")
    if isinstance(prov, dict):
        for m in [r for r in (cand.images or []) if r.get("webp")]:
            provenance.add_file(prov, m["webp"], [m["texture"]], ["webp_encode"])
        for name in cand.glbs:
            entry = prov["files"].get(name)
            if entry is not None:
                entry["derived_from"] = list(dict.fromkeys([*entry["derived_from"], *sorted(cand.files)]))
                entry["steps"] = list(dict.fromkeys([*entry["steps"], "webp_encode"]))
        provenance.finalize(prov, data)
    (pkg / "manifest.json").write_text(json.dumps(data, indent=1))
    issues = validate_package(pkg)
    crit = [i for i in issues if i.severity == "critical"]
    if crit:
        for name, b in old.items():
            (pkg / name).write_bytes(b)
        for rel in cand.files:
            (pkg / rel).unlink(missing_ok=True)
        data.clear()
        data.update(saved)
        return "; ".join(f"{i.code} {i.detail}" for i in crit[:3])[:300]
    validation = data.get("validation") or {}
    validation["package"] = [{"code": i.code, "severity": i.severity, "node": i.node_id, "detail": i.detail} for i in issues]
    rest = [*validation.get("usd", []), *validation.get("engine", []), *validation["package"]]
    validation["package_ok"] = not any(i.get("severity") == "critical" for i in rest)
    data["validation"] = validation
    return ""
