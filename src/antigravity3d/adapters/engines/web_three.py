"""Web-runtime verifier: load the *shipped* GLB through three.js's own GLTFLoader and render it.

The review inspector draws its own copy of the geometry with its own materials, so it cannot
attest that the exported ``<asset>.glb`` loads in a web runtime. This module does: it embeds the
package's GLB bytes in a self-contained page, parses them with the unmodified three r160
``GLTFLoader`` (vendored under ``artifacts/vendor``), renders the scene exactly as the loader
returned it (no material substitution) and reports

* every loader / console / page / shader error and warning,
* facts read back from the loaded scene (meshes, triangles, materials, bound texture slots and
  their pixel sizes, bounds, draw calls),
* a transparent-background silhouette frame (material-free second render) compared with an independent reference
  render of the package's USD (``kernel.raster.render_buffers`` with an explicit matching camera)
  through the existing ``silhouette_iou``.

It implements the engine-validator interface of ``validators.py`` (``available()``,
``async validate()``, skipped-with-reason when the runtime is missing) and returns a
``WebThreeReport``, a subclass of ``EngineReport``.

What is and is not verified is stated in ``PassRule`` and ``WebThreeValidator.__doc__``.

Command line::

    python -m antigravity3d.adapters.engines.web_three PACKAGE_DIR [--reference PNG] [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import glob
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from urllib.parse import unquote

import numpy as np

from antigravity3d.adapters.engines import web_browser
from antigravity3d.adapters.engines.validators import (
    DEFAULT_TIMEOUT,
    MIN_SILHOUETTE_IOU,
    TAIL_CHARS,
    EngineReport,
    EngineValidator,
    silhouette_iou,
)
from antigravity3d.kernel.types import MeshData, bounds_issue

ENGINE = "web_three"

# --------------------------------------------------------------------------- vendored runtime

#: published (relative) path in the generated page -> file name inside ``artifacts/vendor``
VENDOR_FILES = {
    "vendor/three.module.min.js": "three-r160.module.min.js.gz",
    "vendor/examples/jsm/loaders/GLTFLoader.js": "GLTFLoader-r160.js.gz",
    "vendor/examples/jsm/utils/BufferGeometryUtils.js": "BufferGeometryUtils-r160.js.gz",
}
PROVENANCE_FILE = "three-r160-addons.provenance.json"
_VENDOR_DIR = ("antigravity3d.artifacts", "vendor")


def _vendor_dir():
    return resources.files(_VENDOR_DIR[0]).joinpath(_VENDOR_DIR[1])


def provenance() -> dict:
    return json.loads(_vendor_dir().joinpath(PROVENANCE_FILE).read_text("utf-8"))


def vendor_source(gz_name: str) -> bytes:
    """Decompressed vendored file, checked against the recorded sha256 (raises on mismatch)."""
    raw = gzip.decompress(_vendor_dir().joinpath(gz_name).read_bytes())
    want = provenance()["files"][gz_name]["sha256_uncompressed"]
    if hashlib.sha256(raw).hexdigest() != want:
        raise RuntimeError(f"vendored {gz_name} does not match its recorded sha256 {want}")
    return raw


_VENDORED: dict[str, bytes] = {}


def _vendored(rel: str) -> bytes:
    """``vendor_source`` of a page path, decompressed and hash-checked once per process."""
    if rel not in _VENDORED:
        _VENDORED[rel] = vendor_source(VENDOR_FILES[rel])
    return _VENDORED[rel]


# --------------------------------------------------------------------------- camera / rig

FRAME_PX = 512                          # same frame size as the Godot capture
VIEW_DIR_YUP = (1.4, 1.2, 2.0)          # camera offset direction, glTF Y-up; copied from the Godot rig
ORTHO_FILL = 1.1                        # ortho frame width = ORTHO_FILL * bounds diagonal (Godot rig)
CAMERA_DISTANCE = 2.0                   # in bounds diagonals from the bounds centre (Godot rig)
#: directional lights as (position, intensity), aimed at the origin; the Godot rig's pair
LIGHTS = (((2.0, 4.0, 3.0), 3.0), ((-2.0, 2.0, -1.0), 1.0))
#: sky / ground colours of the fixed gradient environment used for image-based lighting
ENV_SKY, ENV_GROUND = 0xCFE2F3, 0x4A3F35


def web_camera(bounds_m, view_dir=VIEW_DIR_YUP) -> dict:
    """The fixed orthographic camera, from the package's glTF-space (Y-up) AABB. ``view_dir``: the camera's offset
    direction from the centre (glTF Y-up); the verification's is ``VIEW_DIR_YUP``, the look round adds the opposite
    corner (``tactics.look_round``)."""
    lo, hi = (np.asarray(b, np.float64) for b in bounds_m)
    center = (lo + hi) / 2
    diameter = max(float(np.linalg.norm(hi - lo)), 1e-3)
    return {"center": center.tolist(), "diameter": diameter, "view_dir": [float(x) for x in view_dir],
            "ortho_width": diameter * ORTHO_FILL, "distance": diameter * CAMERA_DISTANCE, "size_px": FRAME_PX}


def reference_png(meshes: list[MeshData], bounds_m) -> bytes:
    """Reference silhouette: the kernel rasteriser with the matching camera, alpha = covered pixels.

    ``meshes`` are in glTF Y-up metres (the camera is axis-agnostic; only the basis matters).
    """
    from PIL import Image

    from antigravity3d.kernel.raster import Camera, render_buffers

    cam = web_camera(bounds_m)
    f = -np.asarray(cam["view_dir"], np.float64)
    f /= np.linalg.norm(f)
    r = np.cross(f, [0.0, 1.0, 0.0])
    r /= np.linalg.norm(r)
    basis = np.stack([r, np.cross(r, f), f])
    camera = Camera("web_three", basis, np.asarray(cam["center"]), False, 0.0,
                    FRAME_PX / cam["ortho_width"], np.zeros(2), FRAME_PX)
    rb = render_buffers(meshes, camera=camera, size=FRAME_PX)
    rgba = np.zeros((FRAME_PX, FRAME_PX, 4), np.uint8)
    rgba[..., :3] = rb.color
    rgba[..., 3] = np.where(rb.prim_id >= 0, 255, 0)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def usd_meshes(usd_path: Path) -> list[MeshData]:
    """Visible (non-guide) meshes of the package USD at its default LOD, in glTF world Y-up metres.

    Derived from the stage itself: composed world transforms (root rotation included), then the stage's
    ``upAxis`` (Z-up stages, i.e. Unreal packages, map (x, y, z) -> (x, z, -y) like the GLB writer) and
    ``metersPerUnit``."""
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(str(usd_path))
    if stage is None or not stage.GetDefaultPrim():
        raise ValueError(f"{usd_path}: cannot open or no default prim")
    cache = UsdGeom.XformCache()
    z_up = UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
    unit = UsdGeom.GetStageMetersPerUnit(stage) or 1.0
    out = []
    for prim in Usd.PrimRange(stage.GetDefaultPrim()):
        if not prim.IsA(UsdGeom.Mesh) or UsdGeom.Imageable(prim).ComputePurpose() != UsdGeom.Tokens.default_:
            continue
        mesh = UsdGeom.Mesh(prim)
        counts, idx, pts = (mesh.GetFaceVertexCountsAttr().Get(), mesh.GetFaceVertexIndicesAttr().Get(),
                            mesh.GetPointsAttr().Get())
        if not counts or not pts:
            continue
        m = np.array(cache.GetLocalToWorldTransform(prim)).T  # pxr is row-vector; transpose to column form
        p = np.asarray(pts, np.float64)
        p = (p @ m[:3, :3].T + m[:3, 3]) * unit
        if z_up:
            p = np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1)
        tris, i = [], 0
        for c in counts:
            tris.extend((idx[i], idx[i + k], idx[i + k + 1]) for k in range(1, c - 1))
            i += c
        out.append(MeshData(prim.GetName(), str(prim.GetPath()), p, np.asarray(tris, np.int32)))
    return out


# --------------------------------------------------------------------------- pass rule


@dataclass(frozen=True)
class PassRule:
    """The one place that defines pass / fail. Every threshold names its origin.

    A report passes iff no check below has status ``fail``. Checks may be ``skipped`` only when
    their evidence does not exist (no reference, no manifest); skipped checks are listed in the
    report and printed, they never count as passes.
    """

    #: silhouette IoU against the reference render: the Godot DCC-vs-engine gate, reused unchanged
    min_silhouette_iou: float = MIN_SILHOUETTE_IOU
    #: console/page/loader errors and shader compile or link failures tolerated: none
    max_errors: int = 0
    #: loader / console warnings tolerated (three logs recoverable glTF problems as warnings): none
    max_warnings: int = 0
    #: bounds drift is judged by kernel.types.bounds_issue (0.5% of each axis span, 1 um floor), the
    #: same function the Godot import check uses; triangle counts and texture sizes must match exactly
    #: authored here (not from another gate): a frame with less foreground than this is "nothing drawn"
    min_foreground_fraction: float = 0.02
    #: authored here: the object must not touch the frame edge (framing sanity; the rig leaves ~9% margin)
    border_margin_px: int = 1
    #: authored here: mean sRGB luminance (0-255) of foreground pixels; below this the asset rendered black
    min_mean_luminance: float = 8.0
    #: console text fragments that are browser performance hints, not asset problems. Chromium prints
    #: "GL Driver Message (OpenGL, Performance, ...): GPU stall due to ReadPixels" when this page reads
    #: its own canvas back to take the frame; anything else (including any three.js message) still counts.
    ignored_console: tuple[str, ...] = ("GL Driver Message (OpenGL, Performance,",)


PASS_RULE = PassRule()

#: manifest texture role -> three.js material slots that must be bound for it
ROLE_SLOTS = {"albedo": ("map",), "normal": ("normalMap",), "orm": ("aoMap", "roughnessMap", "metalnessMap"),
              "emissive": ("emissiveMap",)}
#: glTF texture slot (manifest ``materials[].texture_slots``) -> three.js material slots bound for it
GLTF_SLOTS = {"baseColorTexture": ("map",), "normalTexture": ("normalMap",), "occlusionTexture": ("aoMap",),
              "metallicRoughnessTexture": ("roughnessMap", "metalnessMap"), "emissiveTexture": ("emissiveMap",)}


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "pass" | "fail" | "skipped"
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.status.upper():7}] {self.name}: {self.detail}"


@dataclass(frozen=True)
class WebThreeReport(EngineReport):
    """``EngineReport`` plus the web verifier's evidence. ``metrics``/``bounds_m`` carry the loaded scene's
    triangle count and world AABB so ``geometry_issue`` works as for the Godot report."""

    screenshot: str | None = None
    page: str | None = None
    diagnostics: dict | None = None
    facts: dict | None = None
    checks: tuple[Check, ...] = ()
    silhouette_iou: float | None = None
    timings_s: dict | None = None

    def geometry_issue(self, manifest: dict) -> str | None:
        return super().geometry_issue(manifest) or bounds_issue(manifest.get("glb_bounds_m"), self.bounds_m)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    def summary(self) -> str:
        head = "SKIPPED" if self.skipped else ("PASS" if self.ok else "FAIL")
        lines = [f"{ENGINE}: {head}" + (f" ({self.skipped_reason})" if self.skipped else "")]
        if self.silhouette_iou is not None:
            lines.append(f"silhouette IoU {self.silhouette_iou:.4f}")
        lines += [str(c) for c in self.checks]
        if self.screenshot:
            lines.append(f"screenshot: {self.screenshot}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- page

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>agy3d web_three verification page</title>
<!-- Self-contained: three r160 and its own GLTFLoader (unmodified) under ./vendor, the package's GLBs under ./glb,
     their referenced textures under ./textures, the #job block below. No network. Camera and lighting are documented in antigravity3d/adapters/engines/web_three.py. -->
<script type="importmap">{"imports":{"three":"./vendor/three.module.min.js"}}</script>
<style>html,body{margin:0;background:transparent}canvas{display:block}</style></head>
<body><canvas id="c" width="__SIZE__" height="__SIZE__"></canvas>
<script type="application/json" id="job">__JOB__</script>
<script type="module">
import * as THREE from 'three';
import { GLTFLoader } from './vendor/examples/jsm/loaders/GLTFLoader.js';
const job = JSON.parse(document.getElementById('job').textContent);
const out = { done: false, errors: [], shader_errors: [], programs: [], main: null, lods: [], render: null, png: null, mask_png: null };
window.__agy = out;
// The GLBs are files beside the page, fetched from the page's server like everything else; they are parsed with an empty
// path, so a URI inside one resolves against the page directory, where the package's textures/ is staged.
// Images are decoded once per distinct content. GLTFLoader hands every image, embedded (a blob: URL of the GLB's bytes)
// or referenced (a textures/ file), to ImageBitmapLoader; this load() fetches the bytes once per URL, hashes them
// (SHA-256) and decodes each distinct byte string once, with the loader's own options. A far LOD whose file holds the
// same bytes as the image LOD0 embeds therefore gets the very image LOD0 decoded, and content() below hashes its
// pixels once; a file that differs in a single byte is decoded and compared on its own.
const fetched = new Map(), decodedImages = new Map();
const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, '0')).join('');
THREE.ImageBitmapLoader.prototype.load = function (url, onLoad, onProgress, onError) {
  if (url === undefined) url = '';
  if (this.path !== undefined) url = this.path + url;
  url = this.manager.resolveURL(url);
  const options = Object.assign({}, this.options, { colorSpaceConversion: 'none' }), scope = this;
  if (!fetched.has(url)) fetched.set(url, fetch(url, { credentials: 'same-origin', headers: this.requestHeader }).then((r) => r.arrayBuffer()));
  scope.manager.itemStart(url);
  fetched.get(url).then(async (bytes) => {
    const key = hex(await crypto.subtle.digest('SHA-256', bytes)) + JSON.stringify(options);
    if (!decodedImages.has(key)) decodedImages.set(key, createImageBitmap(new Blob([bytes]), options));
    return decodedImages.get(key);
  }).then((image) => { if (onLoad) onLoad(image); scope.manager.itemEnd(url); })
    .catch((e) => { if (onError) onError(e); scope.manager.itemError(url); scope.manager.itemEnd(url); });
};
const load = (url) => new THREE.FileLoader().setResponseType('arraybuffer').loadAsync(url);
const parse = (buf) => new Promise((ok, bad) => new GLTFLoader().parse(buf, '', ok, (e) => bad(e)));
const SLOT_KEYS = { map: ['pbr', 'baseColorTexture'], metalnessMap: ['pbr', 'metallicRoughnessTexture'], roughnessMap: ['pbr', 'metallicRoughnessTexture'],
                    normalMap: [null, 'normalTexture'], aoMap: [null, 'occlusionTexture'], emissiveMap: [null, 'emissiveTexture'] };
const c3 = (c) => (c ? [c.r, c.g, c.b] : null), num = (v) => (typeof v === 'number' ? v : null);
const imgSize = (t) => { const i = t && t.image; return i ? [i.width || i.naturalWidth || 0, i.height || i.naturalHeight || 0] : [0, 0]; };
// What a decoded image holds: a 64-bit hash of its RGBA pixels drawn unscaled. Two files whose images decode to the
// same pixels get the same value, whichever GLB (embedded bytes or a referenced file) they came from.
// Hashed once per decoded image: identical bytes share one decoded image (ImageBitmapLoader above), and a map bound to
// several slots (ORM) is one image.
const fpCanvas = document.createElement('canvas'), fpCtx = fpCanvas.getContext('2d', { willReadFrequently: true }), fpCache = new WeakMap();
function content(t) {
  const [w, h] = imgSize(t);
  if (!w || !h) return null;
  const key = t.image && typeof t.image === 'object' ? t.image : null;
  if (key && fpCache.has(key)) return fpCache.get(key);
  fpCanvas.width = w; fpCanvas.height = h; fpCtx.clearRect(0, 0, w, h); fpCtx.drawImage(t.image, 0, 0);
  const px = new Uint32Array(fpCtx.getImageData(0, 0, w, h).data.buffer);
  let a = 0x811c9dc5 | 0, b = 0x5bd1e995 | 0;
  for (let i = 0; i < px.length; i++) { a = Math.imul(a ^ px[i], 0x01000193); b = (Math.imul(b ^ px[i], 0x5bd1e995) + i) | 0; }
  const fp = (a >>> 0).toString(16).padStart(8, '0') + (b >>> 0).toString(16).padStart(8, '0');
  if (key) fpCache.set(key, fp);
  return fp;
}

function describe(gltf) {
  const json = gltf.parser.json, assoc = gltf.parser.associations;
  gltf.scene.updateMatrixWorld(true);
  const meshes = [], mats = new Map(); let tris = 0, nonTri = 0;
  gltf.scene.traverse((o) => {
    if (o.isPoints || o.isLine) nonTri++;
    if (!o.isMesh) return;
    const g = o.geometry, n = (g.index ? g.index.count : g.attributes.position.count) / 3;
    tris += n;
    const m = Array.isArray(o.material) ? o.material[0] : o.material;
    meshes.push({ name: o.name, tris: n, material: m.name, hasNormals: !!g.attributes.normal, hasUV: !!g.attributes.uv, hasTangents: !!g.attributes.tangent });
    mats.set(m.uuid, m);
  });
  const materials = [...mats.values()].map((m) => {
    const a = assoc.get(m), idx = a && a.materials, jm = idx !== undefined ? json.materials[idx] : null;
    const slots = {}, declared = [];
    for (const [slot, [grp, key]] of Object.entries(SLOT_KEYS)) {
      const src = jm ? (grp === 'pbr' ? (jm.pbrMetallicRoughness || {}) : jm) : {};
      if (src[key] !== undefined) declared.push(slot);
      const t = m[slot];
      if (t) {
        const [w, h] = imgSize(t), ta = assoc.get(t), ti = ta && ta.textures !== undefined ? json.textures[ta.textures] : null;
        const img = ti ? (json.images || [])[ti.source] : null;
        slots[slot] = { width: w, height: h, colorSpace: t.colorSpace, content: content(t), uri: img && img.uri ? img.uri : null };
      }
    }
    const missing = declared.filter((s) => !slots[s] || !slots[s].width || !slots[s].height);
    return { name: m.name, type: m.type, fallback_default: idx === undefined, declared, slots, missing_slots: missing,
             color: m.color ? m.color.getHexString() : null, metalness: m.metalness, roughness: m.roughness,
             flatShading: !!m.flatShading, doubleSided: m.side === THREE.DoubleSide, transparent: !!m.transparent,
             json_alpha_mode: jm ? (jm.alphaMode || 'OPAQUE') : null, json_extensions: jm ? Object.keys(jm.extensions || {}) : [],
             params: {
               color: c3(m.color), opacity: m.opacity, alphaTest: m.alphaTest, emissive: c3(m.emissive), emissiveIntensity: m.emissiveIntensity,
               physical: !!m.isMeshPhysicalMaterial, clearcoat: num(m.clearcoat), clearcoatRoughness: num(m.clearcoatRoughness),
               transmission: num(m.transmission), ior: num(m.ior), sheen: num(m.sheen), sheenColor: c3(m.sheenColor),
               sheenRoughness: num(m.sheenRoughness), specularIntensity: num(m.specularIntensity), specularColor: c3(m.specularColor) } };
  });
  const box = new THREE.Box3().setFromObject(gltf.scene);
  return { meshes, mesh_count: meshes.length, triangles: tris, non_triangle_objects: nonTri, materials,
           texture_count: materials.reduce((s, m) => s + Object.keys(m.slots).length, 0),
           bounds_m: box.isEmpty() ? null : [box.min.toArray(), box.max.toArray()],
           json_materials: (json.materials || []).length, json_textures: (json.textures || []).length,
           extensions_used: json.extensionsUsed || [] };
}

__ENV_JS__
// job.env.mode 'page': PMREMGenerator renders the environment here. 'shared': it was rendered once in this process and
// is loaded from ENV_FILE beside the page into the same texture (identical half floats).
let envTarget = null;
async function environment(renderer) {
  if (job.env.mode === 'shared') return sharedEnvironment('./' + job.env.file);
  envTarget = gradientEnvironment(renderer, job.rig);
  return envTarget.texture;
}

async function main() {
  const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('c'), antialias: false, alpha: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1); renderer.setSize(job.camera.size_px, job.camera.size_px, false);
  renderer.setClearColor(0x000000, 0); renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.toneMapping = THREE.NoToneMapping;
  renderer.debug.checkShaderErrors = true;
  renderer.debug.onShaderError = (gl, program, vs, fs) => out.shader_errors.push({
    program_log: gl.getProgramInfoLog(program), vertex_log: gl.getShaderInfoLog(vs), fragment_log: gl.getShaderInfoLog(fs) });
  // asked for by the verifier after the frames, never before: hands this page's environment to later verifications
  window.__agy_export_env = () => exportEnvironment(renderer, envTarget);
  // Started first: the GPU process prefilters the environment while this thread fetches, parses and decodes the GLBs.
  const env = environment(renderer);
  for (const l of job.lods) {
    try { const g = await parse(await load(l.url)); out.lods.push({ name: l.name, ...describe(g) }); }
    catch (e) { out.lods.push({ name: l.name, error: String((e && e.message) || e) }); }
  }
  let gltf;
  try { gltf = await parse(await load(job.glb_url)); } catch (e) { out.errors.push('GLTFLoader: ' + String((e && e.message) || e)); out.done = true; return; }
  out.main = describe(gltf);
  const scene = new THREE.Scene();
  scene.add(gltf.scene);
  scene.environment = await env;
  for (const [pos, inten] of job.rig.lights) { const d = new THREE.DirectionalLight(0xffffff, inten); d.position.set(...pos); scene.add(d); }
  const cam = job.camera, w = cam.ortho_width / 2;
  const camera = new THREE.OrthographicCamera(-w, w, w, -w, cam.diameter * 0.01, cam.diameter * 5);
  const dir = new THREE.Vector3(...cam.view_dir).normalize(), center = new THREE.Vector3(...cam.center);
  camera.position.copy(center).addScaledVector(dir, cam.distance); camera.up.set(0, 1, 0); camera.lookAt(center);
  renderer.info.reset(); renderer.info.autoReset = false;
  renderer.render(scene, camera);
  const gl = renderer.getContext(); gl.finish();
  out.render = { draw_calls: renderer.info.render.calls, triangles: renderer.info.render.triangles, gl_error: gl.getError(), context_lost: gl.isContextLost() };
  out.programs = (renderer.info.programs || []).map((p) => ({ name: p.name, diagnostics: p.diagnostics || null }));
  out.png = document.getElementById('c').toDataURL('image/png');
  // The silhouette must be geometry, not material: a second frame of the same scene with every material replaced by
  // an opaque, double-sided white one. Glass, cut-outs and blended materials change the beauty frame's alpha;
  // they cannot change this one.
  scene.overrideMaterial = new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide });
  renderer.info.reset();
  renderer.render(scene, camera);
  gl.finish();
  out.mask_draw_calls = renderer.info.render.calls;
  out.mask_png = document.getElementById('c').toDataURL('image/png');
  out.done = true;
}
main().catch((e) => { out.errors.push('page: ' + String((e && e.stack) || e)); out.done = true; });
</script></body></html>
"""


#: The rig's environment, shared by the verification page and the environment export. ``gradientEnvironment`` is what
#: the page has always rendered (gradient sphere -> ``PMREMGenerator.fromScene``); ``exportEnvironment`` reads that
#: prefiltered map back as the half floats it holds (``ENV_FILE`` format, zlib, base64); ``sharedEnvironment`` loads
#: such a file into the texture ``fromScene`` returns.
_ENV_JS = """
function gradientEnvironment(renderer, rig) {
  const s = new THREE.Scene(), g = new THREE.SphereGeometry(10, 32, 16), c = new THREE.Color(), sky = new THREE.Color(rig.env_sky), gnd = new THREE.Color(rig.env_ground);
  const col = []; const p = g.attributes.position;
  for (let i = 0; i < p.count; i++) { c.copy(gnd).lerp(sky, (p.getY(i) / 10 + 1) / 2); col.push(c.r, c.g, c.b); }
  g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  s.add(new THREE.Mesh(g, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide })));
  const pm = new THREE.PMREMGenerator(renderer), rt = pm.fromScene(s); pm.dispose(); return rt;
}
async function exportEnvironment(renderer, rt) {
  const t = rt.texture;
  if (t.type !== THREE.HalfFloatType || t.format !== THREE.RGBAFormat || t.mapping !== THREE.CubeUVReflectionMapping ||
      t.magFilter !== THREE.LinearFilter || t.minFilter !== THREE.LinearFilter || t.generateMipmaps || t.colorSpace !== THREE.LinearSRGBColorSpace)
    throw new Error('PMREMGenerator target is not the half-float CubeUV texture sharedEnvironment rebuilds');
  const gl = renderer.getContext(), w = rt.width, h = rt.height;
  gl.bindFramebuffer(gl.FRAMEBUFFER, renderer.properties.get(rt).__webglFramebuffer);
  const buf = new ArrayBuffer(8 + w * h * 8), u = new Uint16Array(buf, 8);
  new Uint32Array(buf, 0, 2).set([w, h]);
  if (gl.getParameter(gl.IMPLEMENTATION_COLOR_READ_FORMAT) === gl.RGBA && gl.getParameter(gl.IMPLEMENTATION_COLOR_READ_TYPE) === gl.HALF_FLOAT) {
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.HALF_FLOAT, u);  // the stored half floats themselves
  } else {
    const f = new Float32Array(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.FLOAT, f);
    for (let i = 0; i < f.length; i++) {
      u[i] = THREE.DataUtils.toHalfFloat(f[i]);
      if (THREE.DataUtils.fromHalfFloat(u[i]) !== f[i]) throw new Error('environment texel ' + i + ' is not a half float: ' + f[i]);
    }
  }
  const err = gl.getError();
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  if (err) throw new Error('readPixels of the environment map: GL error ' + err);
  const z = new Uint8Array(await new Response(new Blob([buf]).stream().pipeThrough(new CompressionStream('deflate'))).arrayBuffer());
  let b = ''; for (let i = 0; i < z.length; i += 0x8000) b += String.fromCharCode.apply(null, z.subarray(i, i + 0x8000));
  return { width: w, height: h, zlib_b64: btoa(b) };
}
async function sharedEnvironment(url) {
  const z = await new THREE.FileLoader().setResponseType('arraybuffer').loadAsync(url);
  const buf = await new Response(new Blob([z]).stream().pipeThrough(new DecompressionStream('deflate'))).arrayBuffer();
  const [w, h] = new Uint32Array(buf, 0, 2);
  const t = new THREE.DataTexture(new Uint16Array(buf, 8, w * h * 4), w, h, THREE.RGBAFormat, THREE.HalfFloatType);
  t.mapping = THREE.CubeUVReflectionMapping; t.magFilter = t.minFilter = THREE.LinearFilter; t.generateMipmaps = false;
  t.colorSpace = THREE.LinearSRGBColorSpace; t.needsUpdate = true; return t;
}
"""

#: the prefiltered environment map, written beside a page that loads it (zlib: uint32 width, height, RGBA half floats)
ENV_FILE = "env/pmrem.bin"
#: where the package's GLBs are written beside the page
GLB_DIR = "glb"

#: the only page-relative directory package files are staged into (the package's own textures/)
STAGED_DIR = "textures"


def build_page(out_dir: Path, glb: bytes, bounds_m, lods: dict[str, bytes] | None = None,
               files: dict[str, bytes] | None = None, env: bytes | None = None, view_dir=VIEW_DIR_YUP) -> Path:
    """Write ``page.html`` and everything it loads into ``out_dir`` and return the page path.

    The vendored three.js files are written byte-for-byte (hash-verified) at the paths the page imports, the GLBs
    under ``glb/``. ``files`` (``{path under textures/: bytes}``, see :func:`referenced_textures`) are written at
    those paths beside the page: a far LOD GLB references its maps as ``textures/<file>`` relative to itself, and
    GLTFLoader resolves that against the page (the GLBs are parsed with an empty path), so the page directory mirrors
    the package for them. ``env`` (``ENV_FILE`` bytes) makes the page load the environment map instead of rendering
    it. ``view_dir``: the camera direction (``web_camera``). The page needs no network.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for rel in VENDOR_FILES:
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_vendored(rel))
    (out_dir / GLB_DIR).mkdir(exist_ok=True)
    glbs = {"main": glb, **(lods or {})}
    for name, data in glbs.items():
        (out_dir / GLB_DIR / f"{name}.glb").write_bytes(data)
    if env is not None:
        (out_dir / ENV_FILE).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / ENV_FILE).write_bytes(env)
    for rel, data in (files or {}).items():
        dest = out_dir / rel
        if rel.split("/", 1)[0] != STAGED_DIR or ".." in rel.split("/") or not dest.resolve().is_relative_to((out_dir / STAGED_DIR).resolve()):
            raise ValueError(f"refusing to stage {rel!r} outside {STAGED_DIR}/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    job = {"glb_url": f"./{GLB_DIR}/main.glb", "camera": web_camera(bounds_m, view_dir),
           "lods": [{"name": n, "url": f"./{GLB_DIR}/{n}.glb"} for n in (lods or {})],
           "env": {"mode": "page"} if env is None else {"mode": "shared", "file": ENV_FILE},
           "rig": {"lights": [[list(p), i] for p, i in LIGHTS], "env_sky": ENV_SKY, "env_ground": ENV_GROUND}}
    html = _PAGE.replace("__SIZE__", str(FRAME_PX)).replace("__ENV_JS__", _ENV_JS).replace("__JOB__", json.dumps(job).replace("</", "<\\/"))
    page = out_dir / "page.html"
    page.write_text(html, "utf-8")
    return page


def _glb_doc(data: bytes) -> dict:
    magic, version, _size, jlen, jtype = struct.unpack_from("<4sIII4s", data, 0)
    if (magic, version, jtype) != (b"glTF", 2, b"JSON"):
        raise ValueError("not a GLB")
    return json.loads(data[20:20 + jlen])


def referenced_textures(pkg_dir: Path, glbs) -> dict[str, bytes]:
    """The package files the GLBs reference by relative URI, keyed by the path the page will request.

    Only files inside the package's ``textures/`` that exist are returned. Anything else a GLB references (a
    missing file, a path outside ``textures/``, a scheme) is not staged, so the browser's request for it is
    blocked and recorded, and the check fails naming it. The key is the file's path relative to the package (the
    URI percent-decoded, as the page's server decodes the request it answers).
    """
    root = (pkg_dir / STAGED_DIR).resolve()
    out: dict[str, bytes] = {}
    for data in glbs:
        try:
            doc = _glb_doc(data)
        except (ValueError, struct.error):
            continue  # an unreadable GLB is the loader's to report
        for item in [*(doc.get("images") or []), *(doc.get("buffers") or [])]:
            uri = item.get("uri") if isinstance(item, dict) else None
            if not isinstance(uri, str) or not uri or ":" in uri.split("/", 1)[0] or uri.startswith("/") or any(c in uri for c in "?#\\"):
                continue
            rel = unquote(uri)
            target = (pkg_dir / rel).resolve()
            if ".." in rel.split("/") or not target.is_relative_to(root) or not target.is_file():
                continue
            out[rel] = target.read_bytes()
    return out


# --------------------------------------------------------------------------- evaluation (pure)


def evaluate(raw: dict, console: list[dict], page_errors: list[str], blocked: list[str], manifest: dict | None,
             shot_png: bytes | None, reference: bytes | None, rule: PassRule = PASS_RULE,
             *, reference_note: str = "", reference_issue: str | None = None,
             mask_png: bytes | None = None) -> tuple[list[Check], float | None]:
    """Turn browser evidence into checks. Pure: canned data in, verdict out.

    ``raw`` is the page's ``window.__agy`` object, ``console`` the captured console messages
    (``{"type", "text"}``), ``manifest`` the package manifest (or None). ``shot_png`` is the frame as the loader's
    materials render it; ``mask_png`` is the same view with every material replaced by opaque white, and is what
    framing and the silhouette are judged on (geometry only). Without it the beauty frame's alpha is used.
    """
    checks: list[Check] = []

    def add(name: str, ok: bool | None, detail: str = "") -> None:
        checks.append(Check(name, "skipped" if ok is None else ("pass" if ok else "fail"), detail))

    errs = list(raw.get("errors", [])) + [f"pageerror: {e}" for e in page_errors] + \
        [f"console.error: {c['text'][:300]}" for c in console if c["type"] == "error"]
    add("no loader/console/page errors", len(errs) <= rule.max_errors, "; ".join(errs[:4]) if errs else "none")
    warns = [c["text"][:300] for c in console if c["type"] == "warning" and not any(i in c["text"] for i in rule.ignored_console)]
    ignored = sum(1 for c in console if c["type"] == "warning" and any(i in c["text"] for i in rule.ignored_console))
    add("no console warnings", len(warns) <= rule.max_warnings,
        "; ".join(warns[:4]) if warns else f"none ({ignored} browser perf hint(s) ignored)" if ignored else "none")
    sh = raw.get("shader_errors", [])
    diag = [p for p in raw.get("programs", []) if p.get("diagnostics")]
    add("shaders compile and link", not sh and not diag,
        "; ".join(str(x)[:300] for x in (sh + diag)[:2]) if sh or diag else f"{len(raw.get('programs', []))} program(s) ok")
    add("no blocked/failed network requests", not blocked, ", ".join(blocked[:4]) if blocked else "none")
    for lod in raw.get("lods", []):
        if "error" in lod:
            add(f"{lod['name']} loads", False, lod["error"][:300])

    main = raw.get("main")
    if main is None:
        add("main GLB loaded", False, "GLTFLoader produced no scene")
        return checks, None
    _lod_material_checks(add, main, raw.get("lods", []))
    r = raw.get("render") or {}
    add("WebGL context healthy", not r.get("context_lost") and not r.get("gl_error"),
        f"gl_error={r.get('gl_error')} context_lost={r.get('context_lost')}")
    add("scene has triangle geometry", main["triangles"] > 0 and main["mesh_count"] > 0,
        f"{main['mesh_count']} mesh(es), {main['triangles']:.0f} tris")
    fall = [m["name"] or "<unnamed>" for m in main["materials"] if m["fallback_default"]]
    add("no material fell back to a loader default", not fall, ", ".join(fall) if fall else "none")
    miss = [f"{m['name']}:{','.join(m['missing_slots'])}" for m in main["materials"] if m["missing_slots"]]
    add("every declared texture slot is bound to a decoded image", not miss, "; ".join(miss) if miss else "all bound")

    if manifest is None:
        add("manifest cross-check", None, "no manifest.json next to the package")
    else:
        _manifest_checks(add, main, raw.get("lods", []), r, manifest, raw.get("mask_draw_calls"))

    iou = None
    if reference_issue:
        add("reference usable", False, f"reference unusable: {reference_issue}")
        reference = None
    if shot_png is None:
        add("rendered frame", False, "no frame captured")
    else:
        iou = _frame_checks(add, shot_png, mask_png, reference, rule, reference_note if not reference_issue else "reference unusable")
    return checks, iou


def _manifest_checks(add, main: dict, lods: list[dict], render: dict, manifest: dict,
                     mask_draw_calls: int | None = None) -> None:
    if "tris" in manifest:
        add("triangles == manifest tris", int(main["triangles"]) == manifest["tris"],
            f"loaded {main['triangles']:.0f}, manifest {manifest['tris']}")
    for i, want in enumerate(manifest.get("lod_tris") or []):
        if i == 0:
            add("LOD0 triangles == manifest lod_tris[0]", int(main["triangles"]) == want,
                f"loaded {main['triangles']:.0f}, manifest {want}")
            continue
        got = next((x for x in lods if x["name"] == f"LOD{i}"), None)
        if got is None:
            add(f"LOD{i} triangles == manifest lod_tris[{i}]", None, "LOD GLB not present")
        elif "error" not in got:
            add(f"LOD{i} triangles == manifest lod_tris[{i}]", int(got["triangles"]) == want,
                f"loaded {got['triangles']:.0f}, manifest {want}")
    batches = manifest.get("glb_batches")
    if batches is not None:
        add("mesh count == manifest glb_batches", main["mesh_count"] == len(batches),
            f"loaded {main['mesh_count']}, manifest {len(batches)}")
    if "draw_calls" in manifest and render:
        transmissive = [m["name"] for m in main["materials"] if (m.get("params") or {}).get("transmission")]
        if transmissive and mask_draw_calls is not None:
            # three.js draws the opaque objects (and double-sided transmissive ones) a second time into the buffer
            # a transmissive material samples, so the beauty frame legitimately has more draws than the manifest.
            # The package's own draws are then counted on the material-free silhouette pass: one per mesh.
            add("rendered draw calls == manifest draw_calls",
                mask_draw_calls == manifest["draw_calls"] and render.get("draw_calls", 0) >= manifest["draw_calls"],
                f"silhouette pass {mask_draw_calls}, manifest {manifest['draw_calls']}; beauty frame {render.get('draw_calls')} "
                f"(transmission pre-pass of {transmissive})")
        else:
            add("rendered draw calls == manifest draw_calls", render.get("draw_calls") == manifest["draw_calls"],
                f"rendered {render.get('draw_calls')}, manifest {manifest['draw_calls']}")
    if manifest.get("glb_bounds_m") is not None:
        issue = bounds_issue(manifest["glb_bounds_m"], main["bounds_m"])
        add("loaded bounds == manifest glb_bounds_m (Y-up)", issue is None, issue or "within tolerance")
    declared_normals = (manifest.get("uv") or {}).get("normals")
    if declared_normals:
        flat = [m["name"] for m in main["meshes"] if not m.get("hasNormals")]
        add("meshes carry NORMAL when the manifest declares vertex normals", not flat,
            f"no NORMAL on {flat} (three falls back to flat shading); manifest declares: {declared_normals}" if flat
            else "all meshes have NORMAL")
    if manifest.get("materials") is not None:
        _inventory_checks(add, main, manifest["materials"])
    if "textures" in manifest:
        # The manifest's texture records are the complete list of texture files the package ships. A material
        # may legitimately have none (factor-only), so loaded material names are not compared for equality:
        # every material that has records must be present, and a material without records must bind no texture.
        textures = manifest["textures"] or []
        # Materials baked into one texture set ship as one glTF material (``texture_atlas.gltf_materials``): their
        # texture records (one per USD material) are checked against the material they ship as.
        alias = {src: rec["material"] for rec in (manifest.get("texture_atlas") or {}).get("gltf_materials") or []
                 for src in rec.get("from") or []}
        if alias:
            textures = [{**t, "material": alias.get(t["material"], t["material"])} for t in textures]
        by_name = {m["name"]: m for m in main["materials"]}
        want_mats = {t["material"] for t in textures}
        missing = sorted(want_mats - set(by_name))
        stray = sorted(f"{name}.{slot}" for name, m in by_name.items() if name not in want_mats
                       for slot, bound in (m.get("slots") or {}).items() if bound is not None)
        add("textured materials present; other materials bind no texture", not missing and not stray,
            "; ".join(filter(None, [f"missing {missing}" if missing else "",
                                    f"texture bound with no manifest record: {stray[:6]}" if stray else ""]))
            or f"{len(want_mats)} textured, {len(set(by_name) - want_mats)} factor-only")
        bad = []
        for t in textures:
            mat = by_name.get(t["material"])
            for slot in ROLE_SLOTS.get(t["role"], ()):
                got = (mat or {}).get("slots", {}).get(slot)
                if got is None:
                    bad.append(f"{t['material']}.{slot} unbound")
                elif t.get("width") and (got["width"], got["height"]) != (t["width"], t["height"]):
                    bad.append(f"{t['material']}.{slot} {got['width']}x{got['height']} != {t['width']}x{t['height']}")
        add("manifest textures bound per role with matching pixel size", not bad,
            "; ".join(bad[:6]) if bad else f"{len(textures)} texture(s) match")


def _slot_key(v: dict | None):
    return None if v is None else (v.get("width"), v.get("height"), v.get("content"))


def _lod_material_checks(add, main: dict, lods: list[dict]) -> None:
    """Each LOD file must load with the materials of LOD0: every material it binds is one LOD0 binds (by name),
    with the same parameters, the same texture slots, and in each slot an image of the same size and the same
    decoded pixels (the page's content hash), whether LOD0 embeds it and the LOD file references a file or not.
    A texture that failed to load leaves its declared slot unbound, which fails here too."""
    base: dict[str, dict] = {}
    for m in main["materials"]:
        base.setdefault(m["name"], m)
    for lod in lods:
        if "error" in lod:
            continue  # reported as "<LOD> loads"
        name = f"{lod['name']} materials == LOD0 materials (parameters and texture content)"
        if "materials" not in lod:
            add(name, None, "the page reported no materials for this level")
            continue
        bad, slots = [], 0
        for m in lod["materials"]:
            ref = base.get(m["name"])
            label = m["name"] or "<unnamed>"
            if ref is None:
                bad.append(f"{label}: not a LOD0 material")
                continue
            if m.get("missing_slots"):
                bad.append(f"{label}: declared {','.join(m['missing_slots'])} not bound to a decoded image")
            got, want = m.get("slots") or {}, ref.get("slots") or {}
            for slot in sorted(set(got) | set(want)):
                slots += 1
                if _slot_key(got.get(slot)) != _slot_key(want.get(slot)):
                    bad.append(f"{label}.{slot} {_slot_key(got.get(slot))} != LOD0 {_slot_key(want.get(slot))}")
            for key in ("type", "metalness", "roughness", "doubleSided", "transparent", "fallback_default"):
                if m.get(key) != ref.get(key) and not (isinstance(m.get(key), (int, float)) and _near(m.get(key), ref.get(key))):
                    bad.append(f"{label}: {key} {m.get(key)} != LOD0 {ref.get(key)}")
            pa, pb = m.get("params") or {}, ref.get("params") or {}
            for key in sorted(set(pa) | set(pb)):
                a, b = pa.get(key), pb.get(key)
                if a != b and not (a is not None and b is not None and not isinstance(a, bool) and _near(a, b)):
                    bad.append(f"{label}: {key} {a} != LOD0 {b}")
        add(name, not bad, "; ".join(bad[:4]) if bad else f"{len(lod['materials'])} material(s), {slots} texture slot(s) match LOD0")


_TOL = 1e-5


def _near(a, b) -> bool:
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        return a is not None and b is not None and len(a) == len(b) and all(_near(x, y) for x, y in zip(a, b))
    return a is not None and b is not None and abs(a - b) <= _TOL


def expected_runtime_params(rec: dict) -> dict:
    """What three.js's GLTFLoader must make of one manifest ``materials[]`` record: the loaded material's value for
    every parameter, with the defaults of ``MeshPhysicalMaterial`` standing in where no extension is declared.
    The mapping is the loader's own (glTF factor -> material property); it is stated here, not assumed."""
    ext = rec.get("extensions") or {}
    color, mode = rec["base_color_factor"], rec["alpha_mode"]
    out = {"color": color[:3], "opacity": color[3], "transparent": mode == "BLEND",
           "alphaTest": rec["alpha_cutoff"] if mode == "MASK" else 0.0, "doubleSided": rec["double_sided"],
           "emissive": rec["emissive_factor"], "metalness": rec["metallic_factor"], "roughness": rec["roughness_factor"],
           "emissiveIntensity": ext.get("KHR_materials_emissive_strength", {}).get("emissiveStrength", 1.0),
           "clearcoat": ext.get("KHR_materials_clearcoat", {}).get("clearcoatFactor", 0.0),
           "clearcoatRoughness": ext.get("KHR_materials_clearcoat", {}).get("clearcoatRoughnessFactor", 0.0),
           "transmission": ext.get("KHR_materials_transmission", {}).get("transmissionFactor", 0.0),
           "ior": ext.get("KHR_materials_ior", {}).get("ior", 1.5),
           "specularIntensity": ext.get("KHR_materials_specular", {}).get("specularFactor", 1.0),
           "specularColor": ext.get("KHR_materials_specular", {}).get("specularColorFactor", [1.0, 1.0, 1.0])}
    sheen = ext.get("KHR_materials_sheen")
    out["sheen"] = 1.0 if sheen else 0.0
    if sheen:
        out["sheenColor"], out["sheenRoughness"] = sheen["sheenColorFactor"], sheen["sheenRoughnessFactor"]
    return out


def material_mismatches(loaded: dict, rec: dict) -> list[str]:
    """Differences between a loaded three.js material (``describe``'s record) and its manifest record."""
    p = loaded.get("params") or {}
    have = {"color": p.get("color"), "opacity": p.get("opacity"), "transparent": bool(loaded.get("transparent")),
            "alphaTest": p.get("alphaTest") or 0.0, "doubleSided": bool(loaded.get("doubleSided")),
            "emissive": p.get("emissive"), "metalness": loaded.get("metalness"), "roughness": loaded.get("roughness"),
            "emissiveIntensity": p.get("emissiveIntensity"), "clearcoat": p.get("clearcoat") or 0.0,
            "clearcoatRoughness": p.get("clearcoatRoughness") or 0.0, "transmission": p.get("transmission") or 0.0,
            "ior": p.get("ior") if p.get("ior") is not None else 1.5,
            "specularIntensity": p.get("specularIntensity") if p.get("specularIntensity") is not None else 1.0,
            "specularColor": p.get("specularColor") or [1.0, 1.0, 1.0], "sheen": p.get("sheen") or 0.0,
            "sheenColor": p.get("sheenColor"), "sheenRoughness": p.get("sheenRoughness")}
    want = expected_runtime_params(rec)
    bad = []
    for key, w in want.items():
        h = have[key]
        ok = (h == w) if isinstance(w, bool) else _near(h, w)
        if not ok:
            bad.append(f"{key} loaded {h} expected {w}")
    return bad


def _inventory_checks(add, main: dict, inventory: list[dict]) -> None:
    """The loaded materials against the manifest's material inventory: names, every parameter, bound texture slots."""
    loaded = list(main["materials"])
    problems = []
    for rec in inventory:
        candidates = [m for m in loaded if m["name"] == rec["name"]]
        if not candidates:
            problems.append(f"{rec['name']}: not loaded")
            continue
        best = None
        for cand in candidates:
            bad = material_mismatches(cand, rec)
            slots = {s for gl_slot in rec.get("texture_slots", ()) for s in GLTF_SLOTS.get(gl_slot, ())}
            bound = {s for s, v in (cand.get("slots") or {}).items() if v is not None}
            if bound != slots:
                bad.append(f"texture slots bound {sorted(bound)} expected {sorted(slots)}")
            if not bad:
                loaded.remove(cand)
                best = None
                break
            best = best or (cand, bad)
        if best is not None:
            problems.append(f"{rec['name']}: " + "; ".join(best[1][:4]))
    problems += [f"{m['name'] or '<unnamed>'}: loaded but not in the manifest" for m in loaded
                 if not any(r["name"] == m["name"] for r in inventory)]
    add("loaded materials == manifest material inventory (parameters and bound slots)", not problems,
        "; ".join(problems[:4]) if problems else f"{len(inventory)} material(s) match")


def _frame_checks(add, shot_png: bytes, mask_png: bytes | None, reference: bytes | None, rule: PassRule,
                  ref_note: str) -> float | None:
    from PIL import Image

    with Image.open(io.BytesIO(shot_png)) as im:
        beauty = np.asarray(im.convert("RGBA"))
    if mask_png is not None:
        with Image.open(io.BytesIO(mask_png)) as im:
            silhouette = np.asarray(im.convert("RGBA"))
    else:
        silhouette = beauty
    mask = silhouette[..., 3] >= 128
    frac = float(mask.mean())
    add("frame has foreground", frac >= rule.min_foreground_fraction,
        f"foreground {frac:.3f} of frame (floor {rule.min_foreground_fraction})")
    if not mask.any():
        return None
    m = rule.border_margin_px
    touching = bool(mask[:m].any() or mask[-m:].any() or mask[:, :m].any() or mask[:, -m:].any())
    add("asset framed (does not touch frame edge)", not touching, "touches edge" if touching else "inside frame")
    seen = beauty[..., 3] >= 128  # pixels the materials actually draw: a cut-out or glass may leave some undrawn
    if not seen.any():
        add("asset is lit (not black)", None, "no pixel of the beauty frame is drawn (fully transparent materials)")
    else:
        lum = float((0.2126 * beauty[..., 0] + 0.7152 * beauty[..., 1] + 0.0722 * beauty[..., 2])[seen].mean())
        add("asset is lit (not black)", lum >= rule.min_mean_luminance,
            f"mean luminance {lum:.1f}/255 (floor {rule.min_mean_luminance})")
    if reference is None:
        add("silhouette IoU vs reference", None, ref_note or "no reference render available")
        return None
    try:
        iou = silhouette_iou(reference, mask_png if mask_png is not None else shot_png)
    except ValueError as e:
        add("silhouette IoU vs reference", False, str(e))
        return None
    add("silhouette IoU vs reference", iou >= rule.min_silhouette_iou,
        f"IoU {iou:.4f} (min {rule.min_silhouette_iou}, the Godot DCC-vs-engine gate)")
    return iou


# --------------------------------------------------------------------------- driving the browser

CHROMIUM_NAMES = ("google-chrome", "chromium", "chromium-browser", "chrome")
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
_MIME = {".html": "text/html", ".js": "text/javascript", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".glb": "model/gltf-binary"}


# The environment map is the same for every package. The first verification of a process renders it in its page, as
# the page always has, so a process that verifies one package does nothing extra. A later verification uses the copy
# held here; until one exists, the verification after the first hands its page's map back once its frames are taken.
_env_lock = threading.Lock()
_env_cache: dict[tuple, bytes] = {}
_pages_started = 0


def _env_key(exe: str | None) -> tuple:
    return (exe, _launch_args(), ENV_SKY, ENV_GROUND)


def _environment_plan(exe: str | None) -> tuple[bytes | None, bool]:
    """(the shared map for this page, or None to render its own; whether to hand the page's own map back)."""
    global _pages_started
    with _env_lock:
        first = _pages_started == 0
        _pages_started += 1
        env = _env_cache.get(_env_key(exe))
    return env, env is None and not first


def _check_env_export(out: dict) -> bytes:
    data = base64.b64decode(out["zlib_b64"])
    raw = zlib.decompress(data)
    w, h = np.frombuffer(raw[:8], "<u4")
    if (int(w), int(h)) != (out["width"], out["height"]) or len(raw) != 8 + int(w) * int(h) * 8:
        raise RuntimeError(f"environment map has {len(raw)} bytes, not {w}x{h} RGBA half floats")
    return data


def _launch_args() -> tuple[str, ...]:
    return tuple(CHROMIUM_ARGS) + (("--no-sandbox",) if hasattr(os, "geteuid") and os.geteuid() == 0 else ())


def prestart() -> web_browser.Lease:
    """Start the shared browser in the background and hold it for the caller.

    A pipeline run calls this when it starts, so the start-up overlaps the run's own work instead of being waited for
    when the verification is reached. Release the lease when the run ends (``web_browser.Lease``): after a failure or
    a cancellation that closes the browser. When the verifier cannot run here, nothing is started."""
    v = WebThreeValidator()
    if not v.available():
        return web_browser.Lease(None)  # the step will be skipped with its reason; taking a real lease imports Playwright

    async def started(_session) -> None:
        return None
    return web_browser.SHARED.lease(started, exe=v.binary(), args=_launch_args())


def page_file(page_dir: Path, request_path: str) -> Path | None:
    """The file of ``page_dir`` a page-relative request path names, or None. The path is percent-decoded (glTF URIs are
    written encoded, so ``textures/a%20b.png`` is the file ``textures/a b.png``); nothing outside ``page_dir`` is a
    file of the page, however the path is spelled."""
    root = page_dir.resolve()
    f = (root / unquote(request_path.split("?")[0].split("#")[0])).resolve()
    return f if f.is_file() and f.is_relative_to(root) else None


class PageServerUnavailable(RuntimeError):
    """This machine cannot open a local port for the page, so the verifier cannot run here."""


class _PageServer:
    """Serves one page directory to the browser for one verification: on 127.0.0.1, under a random path, GET only.

    The page's requests used to be answered through the browser's request interception, which passes every body
    through the DevTools pipe as base64; for a package with 2K textures that was most of the verification (a 40 MB
    GLB took 17 to 22 s). Interception still sees every request: one for this server is let through to it, anything
    else is aborted and recorded as blocked, and the server records any request it cannot answer from the page
    directory the same way."""

    def __init__(self, page_dir: Path, blocked: list[str]) -> None:
        import http.server
        import secrets

        token = secrets.token_hex(16)
        server_self = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # the method name http.server dispatches a GET to
                prefix = f"/{token}/"
                f = page_file(page_dir, self.path[len(prefix):]) if self.path.startswith(prefix) else None
                if f is None:
                    if self.path.startswith(prefix):
                        blocked.append(server_self.base + self.path[len(prefix):][:200])
                    self.send_error(404)
                    return
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", _MIME.get(f.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with contextlib.suppress(ConnectionError):
                    self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}/{token}/"
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="agy3d-web-page", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


async def run_page(page_dir: Path, exe: str | None, timeout: float, *, export_env: bool = False) -> dict:
    """Open ``page_dir/page.html`` in a fresh context of the process's headless Chromium and collect everything.
    Requests outside the page directory are aborted and reported, so a page that tried to reach the network cannot
    pass silently. The context is the verification's own: its routes, console, page errors and blocked requests.

    ``export_env``: after the frames, read the page's own environment map back for later verifications in this process
    (it does not change this verification's result)."""
    console: list[dict] = []
    page_errors: list[str] = []
    blocked: list[str] = []

    async def verify(session, ctx) -> dict:
        try:
            server = await asyncio.to_thread(_PageServer, page_dir, blocked)
        except OSError as e:
            raise PageServerUnavailable(f"cannot serve the page on 127.0.0.1 ({e})") from e
        try:
            async def gate(route):
                url = route.request.url
                if url.startswith(server.base):
                    await route.continue_()  # the body goes from the server to the browser, not through this process
                else:
                    blocked.append(url[:200])
                    await route.abort()

            await ctx.route("**/*", gate)
            page = await ctx.new_page()
            page.on("console", lambda m: console.append({"type": m.type, "text": m.text}))
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            await page.goto(server.base + "page.html", wait_until="load", timeout=timeout * 1000)
            await page.wait_for_function("window.__agy && window.__agy.done", timeout=timeout * 1000)
            raw = await page.evaluate("window.__agy")
            # what the verdict is made of is taken now: nothing the export below logs can reach it
            result = {"raw": raw, "console": list(console), "page_errors": list(page_errors), "blocked": list(blocked),
                      "browser": session.browser.version}
            if export_env and raw.get("render") and not raw.get("errors") and not raw.get("shader_errors") and not page_errors:
                seen = len(console)
                with contextlib.suppress(Exception):  # on any trouble a later verification renders its own map instead
                    data = _check_env_export(await page.evaluate("window.__agy_export_env()"))
                    if not [c for c in console[seen:] if c["type"] in ("error", "warning")
                            and not any(i in c["text"] for i in PASS_RULE.ignored_console)] and not page_errors:
                        with _env_lock:
                            _env_cache.setdefault(_env_key(exe), data)
            return result
        finally:
            await asyncio.to_thread(server.close)

    return await web_browser.SHARED.run(verify, exe=exe, args=_launch_args(),
                                        context_options={"viewport": {"width": FRAME_PX, "height": FRAME_PX}})


# --------------------------------------------------------------------------- the validator


def _reference_meshes(usd: Path | None) -> tuple[list[MeshData] | None, str]:
    """(meshes, why there are none) for the package USD."""
    if usd is None:
        return None, "no .usd in the package to derive a reference from"
    try:
        return usd_meshes(usd), ""
    except Exception as e:  # noqa: BLE001  pxr missing or unreadable USD
        return None, f"cannot derive reference from {usd.name}: {e}"


def _usd_reference(usd: Path | None, bounds, meshes: tuple[list[MeshData] | None, str] | None = None
                   ) -> tuple[bytes | None, str, str | None]:
    """(reference PNG or None, note, issue): the USD rendered with the camera for ``bounds``, unless the USD's own bounds
    disagree with them (then the reference is unusable and says why)."""
    ref_meshes, note = meshes or _reference_meshes(usd)
    issue = None
    if ref_meshes and meshes is None:
        pts = np.vstack([m.points for m in ref_meshes])
        issue = bounds_issue(bounds, [pts.min(axis=0).tolist(), pts.max(axis=0).tolist()])
        if issue:
            ref_meshes = None
    return (reference_png(ref_meshes, bounds) if ref_meshes else None), note, issue


def _find_package(package_path: Path) -> tuple[Path, Path | None, Path | None]:
    """(glb, manifest.json or None, directory)  for a package directory or a .glb path."""
    p = package_path.resolve()
    if p.is_dir():
        man = p / "manifest.json"
        glbs = sorted(g for g in p.glob("*.glb")
                      if "_LOD" not in g.stem and not g.stem.endswith("_colliders"))
        if man.is_file():
            name = json.loads(man.read_text("utf-8")).get("asset")
            if name and (p / f"{name}.glb").is_file():
                glbs = [p / f"{name}.glb"]
        if not glbs:
            raise FileNotFoundError(f"no .glb in {p}")
        return glbs[0], man if man.is_file() else None, p
    man = p.parent / "manifest.json"
    return p, man if man.is_file() else None, p.parent


class WebThreeValidator(EngineValidator):
    """Loads the shipped GLB in three.js (headless Chromium via Playwright) and judges it with ``PASS_RULE``.

    Verified: the GLB parses with the stock GLTFLoader without errors/warnings; shaders compile; every
    texture slot the glTF declares is bound to a decoded image of the manifest's size; triangle counts
    (main + each LOD GLB), mesh count, draw calls, bounds and material/texture lists agree with the
    manifest; every loaded material's parameters (base colour and alpha, alpha mode and cut-off, double-sided,
    emissive colour and strength, clearcoat, transmission, IOR, sheen, specular, metallic, roughness) and bound
    texture slots equal the manifest's ``materials`` inventory, with no material loaded that the manifest does
    not list; each LOD file loads with LOD0's materials: same parameters, same slots, and in every slot an image
    of the same size and decoded pixels (a far LOD references the package's ``textures/`` files, which are staged
    beside the page; a reference that cannot be served is a blocked request and an unbound slot, and fails);
    the frame is framed, lit, and its silhouette overlaps an independent reference (kernel rasteriser
    of the package USD) at the Godot gate. The silhouette comes from a second render of the loaded scene with
    every material replaced by opaque double-sided white, so glass, cut-outs and blended materials cannot change it.
    Not verified: colour/PBR look (no photometric reference; the loaded values are compared, not the pixels
    they produce), normal-map orientation, KHR extensions beyond what three reads, other web runtimes (Babylon,
    model-viewer), GPUs (software GL only here).
    """

    engine, env_var, binary_names = ENGINE, "CHROMIUM_BIN", CHROMIUM_NAMES

    def binary(self) -> str | None:
        override = os.environ.get(self.env_var)
        if override:
            return override if os.access(override, os.X_OK) else shutil.which(override)
        for n in self.binary_names:
            if p := shutil.which(n):
                return p
        for p in sorted(glob.glob(os.path.join(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "chromium"))):
            if os.access(p, os.X_OK):
                return p
        return None

    @staticmethod
    def _playwright_managed() -> bool:
        """A browser fetched by ``playwright install`` (the CI fallback when no system Chrome exists)."""
        root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
        return bool(glob.glob(os.path.join(root, "chromium-*")))

    def _unavailable(self) -> str | None:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return f"{ENGINE} needs the Python 'playwright' package (pip install 'antigravity-3d[web]')"
        if self.binary() is None and (os.environ.get(self.env_var) or not self._playwright_managed()):
            return f"{ENGINE} browser not found (set ${self.env_var} to a Chromium/Chrome binary or add {self.binary_names[0]} to PATH)"
        return None

    def available(self) -> bool:
        return self._unavailable() is None

    def command(self, exe, package, workdir):  # the browser is driven through Playwright, not a command line
        raise NotImplementedError

    async def validate(self, package_path: str | Path, timeout: float = DEFAULT_TIMEOUT, *,
                       reference: bytes | Path | None = None, out_dir: Path | None = None,
                       rule: PassRule = PASS_RULE) -> WebThreeReport:
        """``package_path``: a package directory or its ``.glb``. ``reference``: PNG (bytes or path) rendered
        with ``web_camera`` / ``reference_png``; default is derived from the package USD. ``out_dir`` keeps the
        page, vendored files and ``web_three.png``; by default they live in a temp dir that is removed."""
        if (why := self._unavailable()) is not None:
            return self._skipped_report(why)
        try:
            glb_path, man_path, pkg_dir = _find_package(Path(package_path))
        except (OSError, ValueError) as e:
            return WebThreeReport(False, ENGINE, f"package not found: {e}")
        t0 = time.perf_counter()
        manifest = None
        if man_path:
            try:
                manifest = json.loads(man_path.read_text("utf-8"))
            except ValueError as e:
                return WebThreeReport(False, ENGINE, f"manifest.json unreadable: {e}")
        glb = glb_path.read_bytes()
        lods = {f"LOD{i}": f.read_bytes() for i in range(1, 8) if (f := glb_path.with_name(f"{glb_path.stem}_LOD{i}.glb")).is_file()}
        # far LOD files reference the package's textures/ (LOD0 embeds its images; a reference there is staged too)
        textures = referenced_textures(glb_path.parent, [glb, *lods.values()])

        bounds = (manifest or {}).get("glb_bounds_m")
        if isinstance(reference, Path):
            reference = reference.read_bytes()
        ref_task: asyncio.Future | None = None
        if reference is not None:
            if bounds is None:
                return WebThreeReport(False, ENGINE, "an explicit reference needs manifest glb_bounds_m to fix the camera")
            ref: tuple[bytes | None, str, str | None] = (reference, "", None)
        else:
            # The reference is rendered on a worker thread while the browser renders the package: once the camera is
            # fixed the two are independent. Without manifest bounds the USD itself fixes the camera, so it is read first.
            usd = next(iter(sorted(pkg_dir.glob("*.usd"))), None)
            with contextlib.suppress(ImportError):  # imported here, not first on the worker thread (see web_browser)
                from pxr import Usd, UsdGeom  # noqa: F401

                from antigravity3d.kernel import raster  # noqa: F401
            if bounds is None:
                ref_meshes, ref_note = await asyncio.to_thread(_reference_meshes, usd)
                if not ref_meshes:
                    return WebThreeReport(False, ENGINE, "cannot fix the camera: manifest has no glb_bounds_m and no USD reference")
                pts = np.vstack([m.points for m in ref_meshes])
                bounds = [pts.min(axis=0).tolist(), pts.max(axis=0).tolist()]
                ref_task = asyncio.ensure_future(asyncio.to_thread(_usd_reference, usd, bounds, (ref_meshes, ref_note)))
            else:
                ref_task = asyncio.ensure_future(asyncio.to_thread(_usd_reference, usd, bounds))

        owned = out_dir is None
        work = Path(tempfile.mkdtemp(prefix="agy3d-web-three-")) if owned else Path(out_dir)
        try:
            exe = self.binary()
            env, export_env = _environment_plan(exe)
            page = build_page(work, glb, bounds, lods, textures, env)
            t_page = time.perf_counter()
            try:
                run = await asyncio.wait_for(run_page(work, exe, timeout, export_env=export_env), timeout + 15)
            except PageServerUnavailable as e:  # the machine cannot run the check: a skip with its reason, not a failed package
                return self._skipped_report(f"{ENGINE}: {e}")
            except Exception as e:  # noqa: BLE001  browser launch / navigation / timeout
                return WebThreeReport(False, ENGINE, f"browser run failed: {type(e).__name__}: {e}"[:TAIL_CHARS])
            t_ran = time.perf_counter()
            ref_png, ref_note, ref_issue = await ref_task if ref_task is not None else ref
            t_run = time.perf_counter()
            raw = run["raw"]
            shot = None
            if raw.get("png"):
                shot = base64.b64decode(raw["png"].split(",", 1)[1])
            mask = base64.b64decode(raw["mask_png"].split(",", 1)[1]) if raw.get("mask_png") else None
            checks, iou = evaluate(raw, run["console"], run["page_errors"], run["blocked"], manifest, shot, ref_png, rule,
                                   reference_note=ref_note, reference_issue=ref_issue, mask_png=mask)
            shot_path = None
            if out_dir is not None:
                work.mkdir(parents=True, exist_ok=True)
                if shot:
                    (work / "web_three.png").write_bytes(shot)
                    shot_path = str(work / "web_three.png")
                if mask:
                    (work / "silhouette.png").write_bytes(mask)
                if ref_png:
                    (work / "reference.png").write_bytes(ref_png)
            main = raw.get("main")
            ok = not any(c.status == "fail" for c in checks)
            tail = "\n".join(str(c) for c in checks if c.status != "pass")
            tail += "\n--- console ---\n" + "\n".join(f"{c['type']}: {c['text']}" for c in run["console"])
            return WebThreeReport(
                ok, ENGINE, tail[-TAIL_CHARS:],
                metrics={"triangles": int(main["triangles"]), "meshes": main["mesh_count"],
                         "materials": len(main["materials"]), "textures": main["texture_count"]} if main else None,
                bounds_m=main["bounds_m"] if main else None,
                screenshot=shot_path, page=str(page) if out_dir is not None else None,
                diagnostics={"errors": raw.get("errors"), "shader_errors": raw.get("shader_errors"), "programs": raw.get("programs"),
                             "console": run["console"], "page_errors": run["page_errors"], "blocked_requests": run["blocked"],
                             "render": raw.get("render"), "browser": run["browser"]},
                facts={"main": main, "lods": raw.get("lods")}, checks=tuple(checks), silhouette_iou=iou,
                timings_s={"prepare": round(t_page - t0, 3), "browser": round(t_ran - t_page, 3),
                           "reference_wait": round(t_run - t_ran, 3), "total": round(time.perf_counter() - t0, 3)})
        finally:
            if ref_task is not None and not ref_task.done():  # the browser failed first: the reference is not needed
                ref_task.add_done_callback(lambda t: t.cancelled() or t.exception())
            if owned:
                shutil.rmtree(work, ignore_errors=True)

    def _skipped_report(self, reason: str) -> WebThreeReport:
        return WebThreeReport(False, ENGINE, "", reason)

    async def capture(self, package_path: str | Path, png: Path, timeout: float = DEFAULT_TIMEOUT) -> EngineReport:
        """Interface parity with ``GodotValidator.capture``: run the verification and copy the frame to ``png``."""
        with tempfile.TemporaryDirectory(prefix="agy3d-web-three-out-") as td:
            rep = await self.validate(package_path, timeout, out_dir=Path(td))
            if rep.screenshot:
                png.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(rep.screenshot, png)
            return rep


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m antigravity3d.adapters.engines.web_three",
                                 description="Load a package's GLB in three.js (headless Chromium) and verify it.")
    ap.add_argument("package", type=Path, help="package directory (or its .glb)")
    ap.add_argument("--reference", type=Path, help="reference PNG rendered with the web_three camera (default: from the package USD)")
    ap.add_argument("--out", type=Path, help="keep page, vendored files, screenshot and reference.png here")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", action="store_true", help="print the facts and diagnostics as JSON after the summary")
    a = ap.parse_args(argv)
    rep = asyncio.run(WebThreeValidator().validate(a.package, a.timeout, reference=a.reference, out_dir=a.out))
    print(rep.summary())
    if rep.skipped:
        return 2
    if not rep.checks:
        print(rep.log_tail)
    if a.json:
        print(json.dumps({"facts": rep.facts, "diagnostics": rep.diagnostics}, indent=1, default=str))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
