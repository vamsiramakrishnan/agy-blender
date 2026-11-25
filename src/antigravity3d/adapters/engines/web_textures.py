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
3. Ships that encoding when every frame is inside the tolerance. When one is not, the next encoding of ``LADDER`` is
   made and measured the same way (the roughness/metal map lossless, then every map lossless: see there for what a
   lossy roughness/metal map loses); the first inside the tolerance ships. The package keeps PNG only when none is, or
   when the frames could not be rendered. The manifest's ``web_textures`` record says what shipped, the bytes before
   and after, the measured differences, and what was tried before it and why it did not ship.

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
import os
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
from antigravity3d.tactics.common import to_thread_drained

ENGINE = "web_three"
# Quality per map role. 90 for colour and data maps; 95 for normal maps, whose error is an angle (my probe on run 1's
# containment_gate: the ORM map is the largest after encoding at 90, 1,073,782 bytes; PSNR 38.5 to 40.2 dB over three
# orbit views and 35.7 dB on an extreme close-up). The frame comparison below decides whether they ship.
WEBP_QUALITY = {"albedo": 90, "orm": 90, "emissive": 90, "normal": 95}
# libwebp's effort, 0 (fast) to 6. Measured on run 1's containment_gate (four 4096 x 2048 maps, four threads): method 4
# 3,519,038 bytes in 2.0 s, method 6 3,441,008 bytes in 3.3 s. 2 % of the bytes is not worth 1.3 s inside a tool call
# the host abandons after 3 minutes.
WEBP_METHOD = 4
# libwebp's effort for a lossless map (Pillow passes ``quality`` as that, 0 to 100). Measured on capture_press's
# 1024 px roughness/metal map (684,895 bytes as PNG): 25 gives 534,414 bytes in 0.34 s, 50 gives 490,634 in 0.46 s,
# 75 gives 490,046 in 0.56 s, 100 gives 487,960 in 1.18 s; the same map tiled to 4096 x 2048 takes 2.7 s at 50.
WEBP_LOSSLESS_EFFORT = 50
WEBP_MIME = "image/webp"
# The encodings tried, smallest first; the first whose frames are inside the tolerance ships. Each names the map roles
# stored lossless (None: every map); the other maps are lossy at WEBP_QUALITY. WebP's lossless mode gives back the PNG's
# pixels exactly.
#
# Why there is more than one: run 5 of the website task (benchmarks/results/site_run5_plugin). Two of its three props
# shipped PNG, 1.65 and 1.48 MB of maps where the lossy WebP maps were 0.30 and 0.22 MB, because 1.07 % and 2.02 % of a
# close-up's pixels differed by more than 8 % (the limit is 1 %). The cause was one map in both, the roughness/metal
# map: alone as lossy WebP it gave 1.00 % and 1.83 %, the albedo alone 0.02 % and 0.07 %, the normal map alone 0.03 %
# and 0.01 %. Lossy WebP stores colour at half the resolution of brightness (YUV 4:2:0) at every quality, and in this
# map the three channels are three unrelated quantities, so the difference between them (a metal mask against the
# roughness beside it) is "colour": 14.4 % and 10.2 % of the map's texels were off by more than 8 % at quality 90 and
# 14.3 % and 10.2 % at quality 100, with a mean error of 80 of 255 at the texels beside a step in metalness and 4
# elsewhere. So a higher quality cannot help (quality 100: 1.06 % and 2.07 % of the close-up), and the same map
# lossless can: 0.000 % in every frame, 478 KB against the PNG's 669 KB. With that one map lossless the two props'
# maps are 0.65 and 0.54 MB. The last step is the same reasoning for a miss with another cause (the offline crate's
# noise maps, test_browser_noise_maps...): every map lossless was 1.22 and 1.07 MB on those two props, still under
# the PNG.
LADDER: tuple[tuple[str, frozenset[str] | None], ...] = (
    ("lossy", frozenset()),
    ("roughness/metal map lossless", frozenset({"orm"})),
    ("lossless", None),
)
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


def _encode_one(png: bytes, quality: int | None) -> bytes:
    """``png`` as WebP: lossy at ``quality``, or lossless (the same pixels back) when ``quality`` is None."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        im.load()
        buf = io.BytesIO()
        # exact: the colour under a fully transparent pixel is kept (a cut-out's edge is filtered from it)
        if quality is None:
            im.save(buf, "WEBP", lossless=True, quality=WEBP_LOSSLESS_EFFORT, method=WEBP_METHOD, exact=True)
        else:
            im.save(buf, "WEBP", quality=quality, method=WEBP_METHOD, exact=True)
    return buf.getvalue()


def encode(pkg: Path, manifest: dict[str, Any], lossless: frozenset[str] | None = frozenset(),
           made: dict[tuple[str, int | None], bytes] | None = None) -> tuple[Candidate | None, list[dict[str, Any]], str]:
    """(the WebP candidate or None, a record per map, why there is no candidate). ``lossless``: the map roles stored
    lossless (None: every map), the others lossy at ``WEBP_QUALITY``. ``made`` keeps the encodings between the
    candidates of one package, so a map is encoded once per form."""
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
    made = {} if made is None else made
    quality = {sha: None if lossless is None or role in lossless else WEBP_QUALITY.get(role, 90) for sha, _rel, role, _png in todo}
    new = [t for t in todo if (t[0], quality[t[0]]) not in made]
    if new:
        with ThreadPoolExecutor(min(4, len(new))) as pool:  # libwebp releases the GIL: four 4096 px maps in 2.0 s, not 5.0
            for t, webp in zip(new, pool.map(lambda t: _encode_one(t[3], quality[t[0]]), new)):
                made[(t[0], quality[t[0]])] = webp
    swap: dict[str, tuple[bytes, str]] = {}
    files: dict[str, bytes] = {}
    names = set(manifest.get("files") or [])
    for sha, rel, role, png in todo:
        webp = made[(sha, quality[sha])]
        target = str(Path(rel).with_suffix(".webp").as_posix())
        rec = {"texture": rel, "role": role, **({"lossless": True} if quality[sha] is None else {"quality": quality[sha]}),
               "png_bytes": len(png), "webp_bytes": len(webp), "webp": None}
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
        "GLB. The encodings are tried smallest first (every map lossy; the roughness/metal map lossless; every map "
        "lossless) and the first inside the tolerance ships; otherwise PNG. The PNG maps in textures/ stay: the USD uses "
        "them and the installer's KTX2 compression encodes from them")


def _key(pkg: Path, manifest: dict[str, Any]) -> str:
    rule = [WEBP_QUALITY, WEBP_METHOD, WEBP_LOSSLESS_EFFORT, [[n, r if r is None else sorted(r)] for n, r in LADDER], encoder()]
    h = hashlib.sha256(json.dumps([rule, sorted(manifest.get("glb_images") or {})]).encode())
    for name in sorted(manifest.get("glb_images") or {}):
        h.update(name.encode() + hashlib.sha256((pkg / name).read_bytes()).digest())
    return h.hexdigest()


def _outside(m: dict[str, Any]) -> str:
    """One measurement that was outside the tolerance, in a clause: its three worst frames."""
    worst = [f for f in m["frames"] if f.get("passed") is False]
    return "outside the tolerance in " + ", ".join(
        f"{f['view']} (mean {f['mean_abs']:.4f}, {f['pixel_share']:.2%} of the asset's pixels over "
        f"{m['tolerance']['pixel_delta']:.0%})" for f in worst[:3])


async def _decide(pkg: Path, data: dict[str, Any], timeout: float) -> tuple[dict[str, Any], Candidate | None, dict[str, Any]]:
    """(the ``web_textures`` record, the candidate that ships or None for PNG, timings): the encodings of ``LADDER`` in
    order, each rendered and compared, until one is inside the tolerance.

    Ends early, with PNG, when no encoding can change the answer: LOD0 embeds no PNG map, a map cannot be read, or the
    frames cannot be rendered here (a browser that fails once is not asked again inside a call the host abandons
    after 3 minutes; nothing ships that was not measured, a lossless map included)."""
    lod0 = f"{data['asset']}.glb"
    png_glb = (pkg / lod0).read_bytes()
    record: dict[str, Any] = {"format": "png", "encoder": encoder(), "method": WEBP_METHOD, "maps": [], "rule": RULE}
    timings: dict[str, Any] = {"reused": False, "encode_s": 0.0}
    made: dict[tuple[str, int | None], bytes] = {}
    tried: list[dict[str, Any]] = []   # the encodings that did not ship, in order, each with why
    measured: set[bytes] = set()       # the candidates compared so far (LOD0's digest)
    for i, (name, lossless) in enumerate(LADDER):
        t0 = time.perf_counter()
        try:
            cand, maps, why = await to_thread_drained(encode, pkg, data, lossless, made)
        except Exception as e:  # noqa: BLE001 - an image Pillow cannot read: the package keeps what it has
            cand, maps, why = None, [], f"encoding failed: {type(e).__name__}: {e}"[:300]
        timings["encode_s"] = round(timings["encode_s"] + time.perf_counter() - t0, 2)
        if i == 0:
            record["maps"] = maps  # what the smallest encoding weighs; replaced by the maps that ship
        if cand is None:
            if not maps:  # nothing to encode, or unreadable: the same for every encoding
                record["reason"] = why
                return record, None, timings
            tried.append({"encoding": name, "reason": why})
            continue
        digest = hashlib.sha256(cand.glbs[lod0]).digest()
        if digest in measured:
            continue  # the same files as an encoding already compared (no roughness/metal map, or it stayed PNG)
        measured.add(digest)
        m = await measure(data, png_glb, cand.glbs[lod0], timeout)
        timings["compare_s"] = round((timings.get("compare_s") or 0.0) + (m.pop("seconds", None) or 0.0), 2)
        timings["page_ms"] = m.pop("page_ms", None)
        record["measured"] = m
        if m["passed"] is True:
            record.update(format="webp", encoding=name, maps=maps)
            if tried:
                record["tried"] = tried
            return record, cand, timings
        if m["passed"] is None:
            tried.append({"encoding": name, "reason": f"the frames could not be compared: {m.get('why')}"})
            record["reason"] = (f"the frames could not be compared, so nothing lossy ships: {m.get('why')}" if i == 0
                                else "; ".join(f"{t['encoding']}: {t['reason']}" for t in tried))
            if i:
                record["tried"] = tried
            return record, None, timings
        tried.append({"encoding": name, "reason": _outside(m)})
    record["reason"] = "; ".join(f"{t['encoding']}: {t['reason']}" for t in tried)
    record["tried"] = tried
    return record, None, timings


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
        record, cand, timings = await _decide(pkg, data, timeout)
        if (record.get("measured") or {}).get("passed", False) is not None:  # "could not compare" is asked again next time
            _DECIDED[key] = (copy.deepcopy(record), cand)
            while len(_DECIDED) > _DECIDED_MAX:
                _DECIDED.popitem(last=False)
    record["glb_bytes"] = {"before": before, "after": before}
    if cand is not None:
        # drained: a cancelled call (an edit stops a running deliver_package routinely) returns only once the thread has
        # finished writing, so the package is whole (all WebP with its manifest, or put back to PNG), never half of each
        problem = await to_thread_drained(_apply, pkg, data, cand)
        if problem:
            record.update(format="png", reason=f"the package with WebP maps did not validate, so it keeps PNG: {problem}")
        else:
            record["glb_bytes"]["after"] = (pkg / lod0).stat().st_size
    data["web_textures"] = record
    _write(pkg / "manifest.json", json.dumps(data, indent=1).encode())
    # how long it took is run telemetry, not package content (the manifest is a function of the scene and the budget)
    timings["total_s"] = round(time.perf_counter() - t0, 2)
    return {**record, "timings": timings}


def _write(path: Path, data: bytes) -> None:
    """Write ``path`` whole or not at all (a temporary name beside it, then a rename): a GLB or manifest of the package is
    never seen half-written, whatever stops the process or removes the folder meanwhile."""
    tmp = path.with_name(f".{path.name}.agy3d-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _apply(pkg: Path, data: dict[str, Any], cand: Candidate) -> str:
    """Write the candidate into the package and bring the manifest along; '' or the validation problem (everything is then
    put back as it was)."""
    from antigravity3d.kernel import provenance
    from antigravity3d.kernel.package import _glb_image_records, web_download_record
    from antigravity3d.kernel.validator import validate_package

    saved = copy.deepcopy(data)
    old = {name: (pkg / name).read_bytes() for name in cand.glbs}
    for rel, b in {**cand.files, **cand.glbs}.items():
        _write(pkg / rel, b)
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
    _write(pkg / "manifest.json", json.dumps(data, indent=1).encode())  # last: it names the files written above
    issues = validate_package(pkg)
    crit = [i for i in issues if i.severity == "critical"]
    if crit:
        for name, b in old.items():
            _write(pkg / name, b)
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
