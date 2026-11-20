"""web_three: the three.js GLB verifier. Unit tests need no browser; ``@pytest.mark.browser`` tests drive Chromium."""

# ruff: noqa: C408
from __future__ import annotations

import asyncio
import base64
import copy
import gzip
import hashlib
import io
import json
import os
import re
import struct
import time

import numpy as np
import pytest
import trimesh
from PIL import Image

from antigravity3d.adapters.engines import web_browser as B
from antigravity3d.adapters.engines import web_three as W
from antigravity3d.adapters.engines.validators import (
    MIN_SILHOUETTE_IOU,
    EngineReport,
    EngineValidator,
    silhouette_iou,
)
from antigravity3d.kernel.types import MeshData

# --------------------------------------------------------------------------- helpers


def _noise(seed: int, size=64) -> bytes:
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(60, 255, (size, size, 3), dtype=np.uint8), "RGB").save(buf, "PNG")
    return buf.getvalue()


def _png_image(seed: int) -> Image.Image:
    return Image.open(io.BytesIO(_noise(seed)))


def _glb_of(tm: trimesh.Trimesh, textured=True) -> bytes:
    """Y-up crate-like GLB with an embedded metallic-roughness material 'crate' (albedo, ORM, normal, AO)."""
    tm = tm.copy()
    uv = (tm.vertices[:, [0, 2]] - tm.vertices[:, [0, 2]].min(axis=0)) / np.maximum(np.ptp(tm.vertices[:, [0, 2]], axis=0), 1e-9)
    mat = trimesh.visual.material.PBRMaterial(
        name="crate", baseColorTexture=_png_image(1), metallicRoughnessTexture=_png_image(2),
        normalTexture=_png_image(3), occlusionTexture=_png_image(2)) if textured else trimesh.visual.material.PBRMaterial(name="crate")
    tm.visual = trimesh.visual.TextureVisuals(uv=uv, material=mat)
    scene = trimesh.Scene({"Batch_0": tm})
    return bytes(scene.export(file_type="glb"))


def _box():
    return trimesh.creation.box((1.0, 0.8, 0.6), transform=trimesh.transformations.translation_matrix((0, 0.4, 0)))


def _manifest(tm: trimesh.Trimesh, **over) -> dict:
    lo, hi = tm.bounds
    man = {"tris": len(tm.faces), "lod_tris": [len(tm.faces)], "draw_calls": 1, "glb_batches": {"Batch_0": ["body"]},
           "glb_bounds_m": [lo.tolist(), hi.tolist()],
           "textures": [{"material": "crate", "role": r, "width": 64, "height": 64} for r in ("albedo", "orm", "normal")]}
    man.update(over)
    return man


def _reference(tm: trimesh.Trimesh, man: dict) -> bytes:
    return W.reference_png([MeshData("m", "/m", np.asarray(tm.vertices, float), np.asarray(tm.faces, np.int32))], man["glb_bounds_m"])


def _edit_glb(data: bytes, fn) -> bytes:
    """Apply ``fn(json_dict, bin_bytearray)`` to a GLB and repack it."""
    jlen, _ = struct.unpack("<II", data[12:20])
    doc = json.loads(data[20:20 + jlen])
    off = 20 + jlen
    blen, _ = struct.unpack("<II", data[off:off + 8])
    blob = bytearray(data[off + 8:off + 8 + blen])
    fn(doc, blob)
    j = json.dumps(doc).encode()
    j += b" " * (-len(j) % 4)
    blob += b"\0" * (-len(blob) % 4)
    total = 12 + 8 + len(j) + 8 + len(blob)
    return b"glTF" + struct.pack("<II", 2, total) + struct.pack("<II", len(j), 0x4E4F534A) + j + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob)


def _write_pkg(tmp_path, glb: bytes, man: dict, name="Crate"):
    d = tmp_path / "pkg"
    d.mkdir(exist_ok=True)
    (d / f"{name}.glb").write_bytes(glb)
    (d / "manifest.json").write_text(json.dumps({"asset": name, **man}))
    return d


def _verify(pkg, reference=None, **kw):
    return asyncio.run(W.WebThreeValidator().validate(pkg, 60, reference=reference, **kw))


def _failed(rep) -> dict[str, str]:
    return {c.name: c.detail for c in rep.checks if c.status == "fail"}


# --------------------------------------------------------------------------- unit tests (no browser)


def test_vendored_files_match_recorded_hashes_and_provenance():
    prov = W.provenance()
    assert prov["package"] == "three@0.160.0" and prov["tarball_integrity"].startswith("sha512-")
    assert set(prov["files"]) == set(W.VENDOR_FILES.values())
    vendor = W._vendor_dir()
    for gz, rec in prov["files"].items():
        blob = vendor.joinpath(gz).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == rec["sha256_gz"], gz
        raw = gzip.decompress(blob)
        assert hashlib.sha256(raw).hexdigest() == rec["sha256_uncompressed"] and len(raw) == rec["bytes_uncompressed"], gz
        assert W.vendor_source(gz) == raw
    lic = vendor.joinpath(prov["license"]["file"]).read_bytes()
    assert hashlib.sha256(lic).hexdigest() == prov["license"]["sha256"] and b"MIT License" in lic
    # the CI workflow pins the same three.js build
    assert prov["files"]["three-r160.module.min.js.gz"]["sha256_uncompressed"] == "3e690ac7d180b0aadf0891bea39eec643e29e2d3e75c99b18689518665f69ba6"


def test_vendored_loader_is_the_stock_loader_importing_the_vendored_three():
    src = W.vendor_source("GLTFLoader-r160.js.gz").decode()
    assert "from 'three'" in src and "../utils/BufferGeometryUtils.js" in src  # relative imports resolved by the page layout
    assert "class GLTFLoader" in src


def test_vendor_hash_mismatch_is_rejected(monkeypatch):
    prov = copy.deepcopy(W.provenance())
    prov["files"]["GLTFLoader-r160.js.gz"]["sha256_uncompressed"] = "0" * 64
    monkeypatch.setattr(W, "provenance", lambda: prov)
    with pytest.raises(RuntimeError, match="does not match"):
        W.vendor_source("GLTFLoader-r160.js.gz")


def test_page_is_self_contained_and_has_no_external_urls(tmp_path):
    tm = _box()
    man = _manifest(tm)
    page = W.build_page(tmp_path, _glb_of(tm), man["glb_bounds_m"], {"LOD1": _glb_of(tm)})
    html = page.read_text()
    without_data = re.sub(r'"(glb_)?b64": ?"[A-Za-z0-9+/=]+"', '"b64":""', html)
    assert not re.search(r"https?://|//cdn|@import|<link ", without_data)
    # every module reference is a relative path to a file that exists beside the page
    refs = re.findall(r"""(?:from|import\()\s*['"](\./[^'"]+)['"]|"three":"(\./[^"]+)"|\./vendor/[\w./-]+""", html)
    for path in {m for tup in refs for m in tup if m} | set(re.findall(r"\./vendor/[\w./-]+", html)):
        assert (tmp_path / path).is_file(), path
    for rel, gz in W.VENDOR_FILES.items():  # vendored files land byte-for-byte
        assert (tmp_path / rel).read_bytes() == W.vendor_source(gz)
    job = json.loads(re.search(r'id="job">(.*?)</script>', html, re.DOTALL).group(1))
    assert base64.b64decode(job["glb_b64"]) == _glb_of(tm) and job["camera"]["size_px"] == W.FRAME_PX
    assert not re.search(r"\bfetch\(|XMLHttpRequest|WebSocket", html)  # nothing in our page code reaches out


def test_camera_is_documented_rig_and_reference_matches_geometry():
    cam = W.web_camera([[-1, 0, -1], [1, 2, 1]])
    assert cam["center"] == [0, 1, 0] and cam["ortho_width"] == pytest.approx(cam["diameter"] * 1.1)
    tm = _box()
    ref = _reference(tm, _manifest(tm))
    with Image.open(io.BytesIO(ref)) as im:
        a = np.asarray(im)[..., 3]
    assert im.size == (W.FRAME_PX, W.FRAME_PX) and 0.02 < (a > 127).mean() < 0.9
    assert silhouette_iou(ref, ref) == 1.0
    ys, xs = np.nonzero(a > 127)
    assert min(xs.min(), ys.min()) > 5 and max(xs.max(), ys.max()) < W.FRAME_PX - 6  # framed with margin


def test_pass_rule_reuses_the_godot_threshold():
    assert W.PASS_RULE.min_silhouette_iou == MIN_SILHOUETTE_IOU == 0.98
    assert W.PASS_RULE.max_errors == 0 and W.PASS_RULE.max_warnings == 0


def test_report_extends_engine_report_and_validator_implements_interface():
    assert issubclass(W.WebThreeReport, EngineReport) and issubclass(W.WebThreeValidator, EngineValidator)
    rep = W.WebThreeReport(True, "web_three", metrics={"triangles": 12}, bounds_m=[[0, 0, 0], [1, 1, 1]])
    man = {"tris": 12, "glb_bounds_m": [[0, 0, 0], [1, 1, 1]]}
    assert rep.geometry_issue(man) is None
    assert "triangle" in rep.geometry_issue({**man, "tris": 13})
    assert "bounds drift" in rep.geometry_issue({**man, "glb_bounds_m": [[0, 0, 0], [2, 1, 1]]})


async def test_skipped_with_reason_when_no_browser(monkeypatch, tmp_path):
    import sys
    import types

    # Hold the other precondition constant: this case is "package present, browser absent",
    # whether or not the Python playwright package happens to be installed on this machine.
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.delenv("CHROMIUM_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    v = W.WebThreeValidator()
    assert not v.available()
    rep = await v.validate(tmp_path / "x.glb")
    assert isinstance(rep, EngineReport) and rep.skipped and not rep.ok and "CHROMIUM_BIN" in rep.skipped_reason


async def test_prestart_without_playwright_starts_nothing_and_releases_cleanly(monkeypatch):
    """A machine without the Python playwright package runs the pipeline with the web check skipped. The pipeline
    takes its browser lease before any pass runs, so that lease must not need Playwright either."""
    import sys

    from antigravity3d.adapters.engines import web_browser

    monkeypatch.setitem(sys.modules, "playwright", None)  # makes ``import playwright`` raise ImportError
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    leases, thread = web_browser.SHARED._leases, web_browser.SHARED._thread
    lease = W.prestart()
    assert web_browser.SHARED._leases == leases and web_browser.SHARED._thread is thread
    assert lease.release(keep=True) is None
    await lease.aclose(keep=False)
    assert web_browser.SHARED._leases == leases


async def test_skipped_with_reason_when_playwright_missing(monkeypatch, tmp_path):
    import sys

    monkeypatch.setitem(sys.modules, "playwright", None)  # makes ``import playwright`` raise ImportError
    rep = await W.WebThreeValidator().validate(tmp_path / "x.glb")
    assert rep.skipped and "playwright" in rep.skipped_reason


def _canned_raw(**over):
    mat = {"name": "wood", "type": "MeshStandardMaterial", "fallback_default": False, "missing_slots": [],
           "declared": ["map", "normalMap", "aoMap", "roughnessMap", "metalnessMap"],
           "slots": {s: {"width": 256, "height": 256, "colorSpace": "srgb", "content": f"{s:0>16}"[-16:]}
                     for s in ("map", "normalMap", "aoMap", "roughnessMap", "metalnessMap")}}
    raw = {"errors": [], "shader_errors": [], "programs": [{"name": "MeshStandardMaterial", "diagnostics": None}],
           "lods": [{"name": "LOD1", "triangles": 60, "mesh_count": 1, "materials": [copy.deepcopy(mat)]}],
           "main": {"mesh_count": 1, "triangles": 120, "meshes": [{"name": "Batch_0", "tris": 120, "hasNormals": True}],
                    "materials": [mat], "texture_count": 5, "bounds_m": [[0, 0, 0], [1, 1, 1]]},
           "render": {"draw_calls": 1, "triangles": 120, "gl_error": 0, "context_lost": False}}
    raw.update(over)
    return raw


def _canned_manifest(**over):
    man = {"tris": 120, "lod_tris": [120, 60], "glb_batches": {"Batch_0": []}, "draw_calls": 1, "glb_bounds_m": [[0, 0, 0], [1, 1, 1]],
           "uv": {"normals": "faceVarying"},
           "textures": [{"material": "wood", "role": r, "width": 256, "height": 256} for r in ("albedo", "orm", "normal")]}
    man.update(over)
    return man


def _frame(fill=(200, 120, 60), box=(100, 100, 400, 400)):
    a = np.zeros((512, 512, 4), np.uint8)
    x0, y0, x1, y1 = box
    a[y0:y1, x0:x1, :3] = fill
    a[y0:y1, x0:x1, 3] = 255
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _run_eval(raw=None, console=(), page_errors=(), blocked=(), manifest="canned", shot="canned", ref="canned"):
    frame = _frame()
    checks, iou = W.evaluate(raw or _canned_raw(), list(console), list(page_errors), list(blocked),
                             _canned_manifest() if manifest == "canned" else manifest,
                             frame if shot == "canned" else shot, frame if ref == "canned" else ref)
    return {c.name: c for c in checks}, iou


def test_evaluate_passes_clean_canned_data():
    checks, iou = _run_eval()
    assert iou == 1.0 and all(c.status == "pass" for c in checks.values()), [str(c) for c in checks.values() if c.status != "pass"]


@pytest.mark.parametrize("kwargs,needle", [
    (dict(console=[{"type": "error", "text": "THREE.WebGLProgram: Shader Error 0 - VALIDATE_STATUS false"}]), "no loader/console/page errors"),
    (dict(console=[{"type": "warning", "text": "THREE.GLTFLoader: Unknown extension \"X\"."}]), "no console warnings"),
    (dict(page_errors=["ReferenceError: x"]), "no loader/console/page errors"),
    (dict(blocked=["https://cdn.example/three.js"]), "no blocked/failed network requests"),
    (dict(raw=_canned_raw(shader_errors=[{"program_log": "link failed"}])), "shaders compile and link"),
    (dict(raw=_canned_raw(programs=[{"name": "x", "diagnostics": {"runnable": False}}])), "shaders compile and link"),
    (dict(raw=_canned_raw(errors=["GLTFLoader: Unexpected end of JSON input"])), "no loader/console/page errors"),
    (dict(manifest=_canned_manifest(tris=121)), "triangles == manifest tris"),
    (dict(manifest=_canned_manifest(lod_tris=[120, 61])), "LOD1 triangles == manifest lod_tris[1]"),
    (dict(manifest=_canned_manifest(glb_bounds_m=[[0, 0, 0], [1, 2, 1]])), "loaded bounds == manifest glb_bounds_m (Y-up)"),
    (dict(manifest=_canned_manifest(glb_batches={"a": [], "b": []})), "mesh count == manifest glb_batches"),
    (dict(manifest=_canned_manifest(draw_calls=3)), "rendered draw calls == manifest draw_calls"),
    (dict(manifest=_canned_manifest(textures=[{"material": "stone", "role": "albedo", "width": 256, "height": 256}])),
     "textured materials present; other materials bind no texture"),
    (dict(manifest=_canned_manifest(textures=[])), "textured materials present; other materials bind no texture"),
    (dict(manifest=_canned_manifest(textures=[{"material": "wood", "role": "albedo", "width": 512, "height": 512}])),
     "manifest textures bound per role with matching pixel size"),
])
def test_evaluate_fails_naming_the_cause(kwargs, needle):
    checks, _ = _run_eval(**kwargs)
    assert checks[needle].status == "fail", [str(c) for c in checks.values()]


def _factor_only(name="iron"):
    return {"name": name, "type": "MeshStandardMaterial", "fallback_default": False, "missing_slots": [], "declared": [], "slots": {}}


def _lod1_check(raw) -> W.Check:
    return _run_eval(raw=raw)[0]["LOD1 materials == LOD0 materials (parameters and texture content)"]


def test_evaluate_lod_materials_must_bind_lod0_textures():
    assert _lod1_check(_canned_raw()).status == "pass"
    raw = _canned_raw()  # same size, other pixels: a different map bound at the far level
    raw["lods"][0]["materials"][0]["slots"]["map"]["content"] = "ffffffffffffffff"
    check = _lod1_check(raw)
    assert check.status == "fail" and "wood.map" in check.detail
    raw = _canned_raw()  # the referenced file did not load: the declared slot stays unbound
    lod_mat = raw["lods"][0]["materials"][0]
    del lod_mat["slots"]["normalMap"]
    lod_mat["missing_slots"] = ["normalMap"]
    check = _lod1_check(raw)
    assert check.status == "fail" and "normalMap not bound" in check.detail
    raw = _canned_raw()
    raw["lods"][0]["materials"][0]["name"] = "stranger"
    assert "not a LOD0 material" in _lod1_check(raw).detail
    raw = _canned_raw()
    raw["main"]["materials"][0]["roughness"], raw["lods"][0]["materials"][0]["roughness"] = 0.5, 0.9
    assert "roughness" in _lod1_check(raw).detail


def test_referenced_textures_stages_only_existing_files_inside_textures(tmp_path):
    from antigravity3d.kernel.gltf_writer import EncodedImage, GltfMaterial, GltfMesh, TextureRef, build_glb

    pkg = tmp_path / "pkg"
    (pkg / "textures").mkdir(parents=True)
    a, b = _noise(1, 8), _noise(2, 8)
    (pkg / "textures" / "a map.png").write_bytes(a)
    (pkg / "b.png").write_bytes(b)
    ia, ib, ic = EncodedImage.from_bytes(a), EncodedImage.from_bytes(b), EncodedImage.from_bytes(_noise(3, 8))
    mat = GltfMaterial(name="m", base_color_texture=TextureRef(ia), normal_texture=TextureRef(ib), emissive_texture=TextureRef(ic))
    mesh = GltfMesh("t", np.eye(3), np.array([[0, 1, 2]]), np.zeros((3, 2)), mat)
    glb = build_glb([mesh], image_uris={ia.digest: "textures/a%20map.png", ib.digest: "b.png", ic.digest: "textures/missing.png"})
    staged = W.referenced_textures(pkg, [glb, b"not a glb"])
    assert staged == {"textures/a%20map.png": a}  # outside textures/ and missing files are left for the browser to fail on
    page = W.build_page(tmp_path / "page", glb, [[0, 0, 0], [1, 1, 1]], {"LOD1": glb}, staged)
    assert (page.parent / "textures" / "a%20map.png").read_bytes() == a
    with pytest.raises(ValueError, match="outside"):
        W.build_page(tmp_path / "page", glb, [[0, 0, 0], [1, 1, 1]], None, {"../x.png": a})


def test_evaluate_accepts_a_factor_only_material_beside_a_textured_one():
    """A package may mix a textured material with a factor-only one: the manifest lists texture files, not materials."""
    raw = _canned_raw()
    raw["main"]["materials"].append(_factor_only())
    checks, _ = _run_eval(raw=raw)
    assert all(c.status == "pass" for c in checks.values()), [str(c) for c in checks.values() if c.status != "pass"]
    assert "1 textured, 1 factor-only" in checks["textured materials present; other materials bind no texture"].detail


def test_evaluate_rejects_a_texture_the_manifest_does_not_record():
    raw = _canned_raw()
    stray = _factor_only()
    stray["slots"] = {"map": {"width": 64, "height": 64, "colorSpace": "srgb"}}
    raw["main"]["materials"].append(stray)
    check = _run_eval(raw=raw)[0]["textured materials present; other materials bind no texture"]
    assert check.status == "fail" and "iron.map" in check.detail
    raw = _canned_raw()
    raw["main"]["materials"] = [_factor_only("wood")]  # the textured material lost every binding: caught per role
    assert _run_eval(raw=raw)[0]["manifest textures bound per role with matching pixel size"].status == "fail"


def test_evaluate_fails_unbound_texture_and_fallback_material_and_flat_normals():
    raw = _canned_raw()
    mat = raw["main"]["materials"][0]
    del mat["slots"]["map"]
    mat["missing_slots"] = ["map"]
    checks, _ = _run_eval(raw=raw)
    assert checks["every declared texture slot is bound to a decoded image"].status == "fail"
    assert "wood.map unbound" in checks["manifest textures bound per role with matching pixel size"].detail
    raw = _canned_raw()
    raw["main"]["materials"][0]["fallback_default"] = True
    assert _run_eval(raw=raw)[0]["no material fell back to a loader default"].status == "fail"
    raw = _canned_raw()
    raw["main"]["meshes"][0]["hasNormals"] = False
    assert _run_eval(raw=raw)[0]["meshes carry NORMAL when the manifest declares vertex normals"].status == "fail"


def test_evaluate_frame_failures():
    checks, _ = _run_eval(shot=_frame(box=(0, 0, 300, 300)))
    assert checks["asset framed (does not touch frame edge)"].status == "fail"
    checks, _ = _run_eval(shot=_frame(fill=(0, 0, 0)))
    assert checks["asset is lit (not black)"].status == "fail"
    checks, _ = _run_eval(shot=_frame(box=(10, 10, 20, 20)), ref=_frame(box=(10, 10, 20, 20)))
    assert checks["frame has foreground"].status == "fail"
    checks, iou = _run_eval(shot=_frame(box=(150, 100, 450, 400)))  # shifted 50 px: IoU 250/350
    assert iou == pytest.approx(250 / 350) and checks["silhouette IoU vs reference"].status == "fail"
    checks, _ = _run_eval(shot=None)
    assert checks["rendered frame"].status == "fail"
    checks, _ = _run_eval(raw=_canned_raw(main=None))
    assert checks["main GLB loaded"].status == "fail"


def test_missing_evidence_is_skipped_not_passed():
    checks, iou = _run_eval(ref=None, manifest=None)
    assert iou is None
    assert checks["silhouette IoU vs reference"].status == "skipped" and checks["manifest cross-check"].status == "skipped"


def test_browser_perf_hints_are_ignored_but_three_warnings_are_not():
    hint = {"type": "warning", "text": "[.WebGL-0x1]GL Driver Message (OpenGL, Performance, GL_CLOSE_PATH_NV, High): GPU stall due to ReadPixels"}
    assert _run_eval(console=[hint])[0]["no console warnings"].status == "pass"
    assert _run_eval(console=[hint, {"type": "warning", "text": "THREE.GLTFLoader: Missing texture"}])[0]["no console warnings"].status == "fail"


def test_find_package_accepts_directory_or_glb(tmp_path):
    d = _write_pkg(tmp_path, b"glTF", {"tris": 1})
    (d / "Crate_LOD1.glb").write_bytes(b"x")
    (d / "Crate_colliders.glb").write_bytes(b"x")
    glb, man, root = W._find_package(d)
    assert glb.name == "Crate.glb" and man.name == "manifest.json" and root == d.resolve()
    assert W._find_package(d / "Crate.glb")[0] == glb
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        W._find_package(empty)


# --------------------------------------------------------------------------- browser tests

@pytest.fixture(scope="module")
def runtime():
    v = W.WebThreeValidator()
    if not v.available():
        pytest.skip("needs the Python 'playwright' package and CHROMIUM_BIN (or a Chrome/Chromium on PATH)")
    return v


@pytest.fixture(scope="module")
def good(tmp_path_factory, runtime):
    tm = _box()
    man = _manifest(tm)
    d = _write_pkg(tmp_path_factory.mktemp("good"), _glb_of(tm), man)
    return tm, man, d


@pytest.mark.browser
def test_browser_good_package_passes_and_frame_is_upright_framed_textured(good, tmp_path):
    tm, man, d = good
    t0 = time.perf_counter()
    rep = _verify(d, _reference(tm, man), out_dir=tmp_path)
    elapsed = time.perf_counter() - t0
    assert rep.ok, rep.summary()
    assert not rep.failures and rep.silhouette_iou >= MIN_SILHOUETTE_IOU
    assert rep.metrics["triangles"] == 12 and rep.geometry_issue(man) is None
    main = rep.facts["main"]
    assert main["mesh_count"] == 1 and main["materials"][0]["name"] == "crate"
    assert {"map", "normalMap", "aoMap", "roughnessMap", "metalnessMap"} <= set(main["materials"][0]["slots"])
    assert main["materials"][0]["slots"]["map"]["width"] == 64
    img = np.asarray(Image.open(rep.screenshot).convert("RGBA"))
    ys, xs = np.nonzero(img[..., 3] >= 128)
    # Y-up glTF: the 0.8 m tall box sits on y=0; on screen its bounding rows must be taller than wide-per-foot sanity and
    # the frame must carry real texture variation (random-noise textures => many distinct colours), not a flat fill
    assert len(np.unique(img[ys, xs, :3].reshape(-1, 3), axis=0)) > 200
    assert elapsed < 30
    print(f"web_three verify (small package, incl. browser launch): {elapsed:.2f}s timings={rep.timings_s}")


@pytest.mark.browser
def test_browser_textured_and_factor_only_materials_in_one_package_pass(runtime, tmp_path):
    """Real GLB from the package's own writer: 'wood' carries three maps, 'iron' is factors only."""
    from antigravity3d.kernel.gltf_writer import EncodedImage, GltfMaterial, GltfMesh, TextureRef, build_glb

    def part(name, extents, at, material):
        tm = trimesh.creation.box(extents, transform=trimesh.transformations.translation_matrix(at))
        pos = np.asarray(tm.vertices[tm.faces].reshape(-1, 3), np.float32)  # unwelded: flat normals, one UV per corner
        tri = np.arange(len(pos), dtype=np.uint32).reshape(-1, 3)
        nrm = np.repeat(np.asarray(tm.face_normals, np.float32), 3, axis=0)
        uv = np.tile(np.array([[0, 0], [1, 0], [1, 1]], np.float32), (len(tri), 1))
        return GltfMesh(name, pos, tri, uvs=uv, material=material, normals=nrm), tm

    orm = TextureRef(EncodedImage.from_bytes(_noise(2)))
    wood = GltfMaterial(name="wood", base_color_texture=TextureRef(EncodedImage.from_bytes(_noise(1))),
                        metallic_roughness_texture=orm, occlusion_texture=orm,
                        normal_texture=TextureRef(EncodedImage.from_bytes(_noise(3))))
    iron = GltfMaterial(name="iron", base_color_factor=(0.55, 0.56, 0.6, 1.0), metallic_factor=1.0, roughness_factor=0.4)
    (body, tm_a), (strap, tm_b) = part("body", (1.0, 0.8, 0.6), (0, 0.4, 0), wood), part("strap", (1.04, 0.1, 0.64), (0, 0.4, 0), iron)
    both = trimesh.util.concatenate([tm_a, tm_b])
    man = _manifest(both, draw_calls=2, glb_batches={"body": ["body"], "strap": ["strap"]},
                    textures=[{"material": "wood", "role": r, "width": 64, "height": 64} for r in ("albedo", "orm", "normal")])
    d = _write_pkg(tmp_path, build_glb([body, strap]), man)
    rep = _verify(d, _reference(both, man))
    assert rep.ok, _failed(rep)
    names = {m["name"]: m for m in rep.facts["main"]["materials"]}
    assert set(names) == {"wood", "iron"} and not any(names["iron"]["slots"].values())

    # the same package with a texture bound on the factor-only material is rejected
    def bind(doc, _blob):
        iron_json = next(m for m in doc["materials"] if m["name"] == "iron")
        iron_json["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    (d / "Crate.glb").write_bytes(_edit_glb(build_glb([body, strap]), bind))
    assert "textured materials present; other materials bind no texture" in _failed(_verify(d, _reference(both, man)))


@pytest.mark.browser
def test_browser_is_deterministic(good):
    tm, man, d = good
    a, b = _verify(d, _reference(tm, man)), _verify(d, _reference(tm, man))
    assert a.silhouette_iou == pytest.approx(b.silhouette_iou, abs=1e-9) and a.ok and b.ok


@pytest.mark.browser
def test_browser_truncated_glb_fails_with_loader_error(good, tmp_path):
    tm, man, _ = good
    glb = _glb_of(tm)
    d = _write_pkg(tmp_path, glb[: len(glb) // 2], man)
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok
    assert "no loader/console/page errors" in _failed(rep) and "GLTFLoader" in _failed(rep)["no loader/console/page errors"]
    assert "main GLB loaded" in _failed(rep)


@pytest.mark.browser
def test_browser_corrupted_texture_bytes_fail_naming_the_loader(good, tmp_path):
    tm, man, _ = good

    def corrupt(doc, blob):
        view = doc["bufferViews"][doc["images"][0]["bufferView"]]
        blob[view["byteOffset"]:view["byteOffset"] + view["byteLength"]] = b"\x00" * view["byteLength"]

    d = _write_pkg(tmp_path, _edit_glb(_glb_of(tm), corrupt), man)
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok
    failed = _failed(rep)
    assert any(k in failed for k in ("no loader/console/page errors", "every declared texture slot is bound to a decoded image")), failed


@pytest.mark.browser
def test_browser_dangling_texture_reference_fails(good, tmp_path):
    tm, man, _ = good

    def dangle(doc, blob):
        doc["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]["index"] = 99

    d = _write_pkg(tmp_path, _edit_glb(_glb_of(tm), dangle), man)
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok and any(k in _failed(rep) for k in ("no loader/console/page errors", "no console warnings",
                                                          "every declared texture slot is bound to a decoded image"))


@pytest.mark.browser
def test_browser_removed_texture_reference_fails_against_manifest(good, tmp_path):
    tm, man, _ = good

    def strip(doc, blob):
        del doc["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]

    d = _write_pkg(tmp_path, _edit_glb(_glb_of(tm), strip), man)
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok
    failed = _failed(rep)
    assert "crate.map unbound" in failed["manifest textures bound per role with matching pixel size"]


@pytest.mark.browser
def test_browser_texture_of_wrong_size_fails(good, tmp_path):
    tm, man, _ = good
    bad = _manifest(tm, textures=[{"material": "crate", "role": "albedo", "width": 128, "height": 128}])
    rep = _verify(_write_pkg(tmp_path, _glb_of(tm), bad), _reference(tm, man))
    assert not rep.ok and "crate.map 64x64 != 128x128" in _failed(rep)["manifest textures bound per role with matching pixel size"]


@pytest.mark.browser
def test_browser_replaced_mesh_fails_on_silhouette_alone(good, tmp_path):
    """A cylinder inside the same bounding box, with a manifest that agrees with the GLB on every count:
    only the independent reference render can tell, and it must."""
    tm, man, _ = good
    cyl = trimesh.creation.cylinder(radius=0.3, height=0.8, sections=16, transform=trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    cyl.apply_translation((0, 0.4, 0))
    cyl_man = _manifest(cyl, glb_bounds_m=man["glb_bounds_m"])  # same camera, honest tri count
    d = _write_pkg(tmp_path, _glb_of(cyl), cyl_man)
    rep = _verify(d, _reference(tm, man))
    failed = _failed(rep)
    assert not rep.ok and rep.silhouette_iou < MIN_SILHOUETTE_IOU
    assert "silhouette IoU vs reference" in failed and "triangles == manifest tris" not in failed


@pytest.mark.browser
def test_browser_manifest_triangle_count_disagreement_fails(good, tmp_path):
    tm, man, _ = good
    d = _write_pkg(tmp_path, _glb_of(tm), {**man, "tris": 13})
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok and "loaded 12, manifest 13" in _failed(rep)["triangles == manifest tris"]
    assert "triangle" in rep.geometry_issue({**man, "tris": 13})


@pytest.mark.browser
def test_browser_lod_triangle_disagreement_fails(good, tmp_path):
    tm, man, _ = good
    d = _write_pkg(tmp_path, _glb_of(tm), {**man, "lod_tris": [12, 12]})
    (d / "Crate_LOD1.glb").write_bytes(_glb_of(trimesh.creation.icosphere(subdivisions=1).copy()))
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok and "LOD1 triangles == manifest lod_tris[1]" in _failed(rep)


@pytest.mark.browser
def test_browser_wrong_up_axis_fails(good, tmp_path):
    """A Z-up export (tall axis on Z): bounds and silhouette must both reject it."""
    tm, man, _ = good
    zup = tm.copy()
    zup.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    d = _write_pkg(tmp_path, _glb_of(zup), man)
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok and "loaded bounds == manifest glb_bounds_m (Y-up)" in _failed(rep)


@pytest.mark.browser
def test_browser_shader_failure_is_caught_when_the_runtime_reports_one(good, tmp_path, monkeypatch):
    """Inject a broken fragment shader into the page and confirm the diagnostics path reports it."""
    tm, man, d = good
    orig = W._PAGE
    broken = orig.replace("renderer.render(scene, camera);",
                          "gltf.scene.traverse((o) => { if (o.isMesh) o.material.onBeforeCompile = (s) => { s.fragmentShader = s.fragmentShader.replace('void main() {', 'void main() { this_is_not_glsl;'); }; });\n  renderer.render(scene, camera);")
    assert broken != orig
    monkeypatch.setattr(W, "_PAGE", broken)
    rep = _verify(d, _reference(tm, man))
    assert not rep.ok and "shaders compile and link" in _failed(rep)


@pytest.mark.browser
def test_browser_capture_interface_writes_png(good, tmp_path, runtime):
    _, _, d = good
    png = tmp_path / "sub" / "engine.png"
    rep = asyncio.run(runtime.capture(d, png))
    assert png.is_file() and isinstance(rep, EngineReport)


# --------------------------------------------------------------------------- every target engine (real exporter)

ENGINES = ["unreal_5", "unity_urp", "godot_4", "roblox_r15"]


def _export_l_block(tmp, engine: str):
    """Asymmetric L-shaped block with a post, written by the real kernel export (``write_engine_package``).

    Unreal packages come out Z-up in USD, the others Y-up with a root rotation; every GLB is Y-up."""
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel.fast_ops import run_export_pipeline
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget

    maps = {}
    for k, seed in (("albedo", 1), ("orm", 2), ("normal", 3)):
        (tmp / f"{k}.png").write_bytes(_noise(seed, 32))
        maps[f"{k}_uri"] = str(tmp / f"{k}.png")

    def box(i, size, at):
        return NodeIR(id=i, kind="box", params={"size": size}, transform=Transform(translate=at), material_id="stone")

    scene = SceneIR(asset_name="Lblock", materials=[MaterialIR(id="stone", **maps)], nodes=[
        box("arm_x", (1.2, 0.4, 0.4), (0.6, 0.2, 0.2)), box("arm_y", (0.4, 0.6, 0.4), (0.2, 0.7, 0.2)),
        box("post", (0.3, 0.3, 0.8), (1.05, 0.2, 0.8))])
    sm = StageManager.create(tmp / "work.usda", "Lblock")
    sm.apply_ir(scene)
    budget = EngineBudget(engine)
    bundle = run_export_pipeline(sm.extract_meshes(), budget, "Lblock")
    pm = write_engine_package(sm, bundle, budget, tmp / engine, asset_name="Lblock", qa={"tier1_ok": True, "tier15": 3.9})
    assert pm.ok
    return tmp / engine


@pytest.fixture(scope="module")
def l_packages(tmp_path_factory, runtime):
    out = {}
    for e in ENGINES:
        out[e] = _export_l_block(tmp_path_factory.mktemp(f"l_{e}"), e)
    return out


@pytest.mark.browser
@pytest.mark.parametrize("engine", ENGINES)
def test_browser_asymmetric_block_passes_for_every_target_engine(engine, l_packages, tmp_path):
    from antigravity3d.adapters.engines.web_three import usd_meshes

    pkg = l_packages[engine]
    man = json.loads((pkg / "manifest.json").read_text())
    rep = _verify(pkg, out_dir=tmp_path)
    assert rep.ok, rep.summary()
    assert rep.silhouette_iou >= MIN_SILHOUETTE_IOU
    # the reference was derived from the stage and lands in the GLB's frame by construction
    pts = np.vstack([m.points for m in usd_meshes(next(pkg.glob("*.usd")))])
    np.testing.assert_allclose([pts.min(0), pts.max(0)], man["glb_bounds_m"], atol=1e-4)


@pytest.fixture(scope="module")
def lod_package(tmp_path_factory, runtime):
    """A textured package with far LOD files (a smooth cylinder and sphere simplify; boxes do not), real exporter."""
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
    from antigravity3d.kernel.fast_ops import run_export_pipeline
    from antigravity3d.kernel.package import write_engine_package
    from antigravity3d.kernel.usd_stage import StageManager
    from antigravity3d.state import EngineBudget

    tmp = tmp_path_factory.mktemp("lod_pkg")
    maps = {}
    for k, seed in (("albedo", 1), ("orm", 2), ("normal", 3)):
        (tmp / f"{k}.png").write_bytes(_noise(seed, 32))
        maps[f"{k}_uri"] = str(tmp / f"{k}.png")
    scene = SceneIR(asset_name="Post", materials=[MaterialIR(id="stone", **maps)], nodes=[
        NodeIR(id="shaft", kind="cylinder", params={"radius": 0.3, "height": 1.2}, transform=Transform(translate=(0, 0, 0.6)), material_id="stone"),
        NodeIR(id="cap", kind="sphere", params={"radius": 0.35}, transform=Transform(translate=(0, 0, 1.3)), material_id="stone")])
    sm = StageManager.create(tmp / "work.usda", "Post")
    sm.apply_ir(scene)
    budget = EngineBudget("godot_4")
    pm = write_engine_package(sm, run_export_pipeline(sm.extract_meshes(), budget, "Post"), budget, tmp / "pkg", asset_name="Post")
    assert pm.ok and pm.n_lod_files >= 1
    assert all(r["storage"] == "uri" for f, recs in pm.data["glb_images"].items() if "_LOD" in f for r in recs)
    return pm.out_dir


@pytest.mark.browser
def test_browser_far_lods_load_their_referenced_textures_and_match_lod0(lod_package, tmp_path):
    rep = _verify(lod_package)
    assert rep.ok, rep.summary()
    man = json.loads((lod_package / "manifest.json").read_text())
    lods = {lod["name"]: lod for lod in rep.facts["lods"]}
    assert sorted(lods) == [f"LOD{i}" for i in range(1, man["lod_levels"])]
    lod0 = {m["name"]: m["slots"] for m in rep.facts["main"]["materials"]}
    for name, lod in lods.items():
        check = next(c for c in rep.checks if c.name.startswith(f"{name} materials == LOD0"))
        assert check.status == "pass", check
        for m in lod["materials"]:
            assert {"map", "normalMap", "aoMap", "roughnessMap", "metalnessMap"} <= set(m["slots"])
            for slot, bound in m["slots"].items():
                assert bound["uri"].startswith("textures/") and bound["content"] == lod0[m["name"]][slot]["content"]
    # LOD0 embeds: its slots name no URI
    assert all(b["uri"] is None for slots in lod0.values() for b in slots.values())


@pytest.mark.browser
def test_browser_far_lod_with_a_missing_texture_fails(lod_package, tmp_path):
    import shutil

    pkg = tmp_path / "pkg"
    shutil.copytree(lod_package, pkg)
    man = json.loads((pkg / "manifest.json").read_text())
    normal = next(t["usd_path"] for t in man["textures"] if t["role"] == "normal")
    (pkg / normal).unlink()  # LOD0 still carries it; every far LOD references it
    rep = _verify(pkg)
    failed = _failed(rep)
    assert not rep.ok, rep.summary()
    assert normal in failed["no blocked/failed network requests"]
    assert "normalMap" in failed["LOD1 materials == LOD0 materials (parameters and texture content)"]
    assert "main GLB loaded" not in failed and "every declared texture slot is bound to a decoded image" not in failed


@pytest.mark.browser
def test_browser_reference_bounds_mismatch_is_reported_as_unusable(l_packages, tmp_path):
    """If the reference's own bounds disagree with glb_bounds_m the report says so instead of scoring a silhouette."""
    import shutil

    pkg = tmp_path / "pkg"
    shutil.copytree(l_packages["unreal_5"], pkg)
    man = json.loads((pkg / "manifest.json").read_text())
    (pkg / "manifest.json").write_text(json.dumps({**man, "glb_bounds_m": [[0, 0, 0], [5, 5, 5]]}))
    rep = _verify(pkg)
    failed = _failed(rep)
    assert not rep.ok and "reference unusable" in failed["reference usable"]
    assert rep.silhouette_iou is None


def _rotate_nodes(doc, blob, quat):
    for n in doc["nodes"]:
        n["rotation"] = quat


@pytest.mark.browser
@pytest.mark.parametrize("name,quat", [("z_up_export", [-0.70710678, 0, 0, 0.70710678]), ("yaw_90", [0, 0.70710678, 0, 0.70710678])])
def test_browser_axis_error_in_the_glb_is_still_caught(name, quat, l_packages, tmp_path):
    import shutil

    pkg = tmp_path / "pkg"
    shutil.copytree(l_packages["godot_4"], pkg)
    glb = next(p for p in pkg.glob("*.glb") if "_LOD" not in p.stem and "colliders" not in p.stem)
    glb.write_bytes(_edit_glb(glb.read_bytes(), lambda d, b: _rotate_nodes(d, b, quat)))
    rep = _verify(pkg)
    failed = _failed(rep)
    assert not rep.ok and rep.silhouette_iou < MIN_SILHOUETTE_IOU, rep.summary()
    assert "silhouette IoU vs reference" in failed and "loaded bounds == manifest glb_bounds_m (Y-up)" in failed


# --------------------------------------------------------------------------- material inventory and silhouette (glTF PBR parameters)


def _rec(name="m", **over):
    rec = {"name": name, "base_color_factor": [0.5, 0.5, 0.5, 1.0], "metallic_factor": 0.0, "roughness_factor": 0.5,
           "emissive_factor": [0.0, 0.0, 0.0], "alpha_mode": "OPAQUE", "alpha_cutoff": None, "double_sided": False,
           "extensions": {}, "texture_slots": []}
    rec.update(over)
    return rec


def _loaded(rec, **over):
    """What the three.js page reports for ``rec`` when the loader did exactly what glTF says."""
    exp = W.expected_runtime_params(rec)
    ext = rec["extensions"]
    params = {"color": exp["color"], "opacity": exp["opacity"], "alphaTest": exp["alphaTest"], "emissive": exp["emissive"],
              "emissiveIntensity": exp["emissiveIntensity"], "physical": bool(ext), "clearcoat": exp["clearcoat"] if ext else None,
              "clearcoatRoughness": exp["clearcoatRoughness"] if ext else None, "transmission": exp["transmission"] if ext else None,
              "ior": exp["ior"] if ext else None, "sheen": exp["sheen"] if ext else None,
              "sheenColor": exp.get("sheenColor"), "sheenRoughness": exp.get("sheenRoughness"),
              "specularIntensity": exp["specularIntensity"] if ext else None, "specularColor": exp["specularColor"] if ext else None}
    m = {"name": rec["name"], "type": "MeshStandardMaterial", "fallback_default": False, "missing_slots": [], "declared": [], "slots": {},
         "metalness": rec["metallic_factor"], "roughness": rec["roughness_factor"], "doubleSided": rec["double_sided"],
         "transparent": rec["alpha_mode"] == "BLEND", "params": params}
    m.update(over)
    return m


def _inv_check(recs, loaded):
    raw = _canned_raw()
    raw["main"]["materials"] = loaded
    man = _canned_manifest(textures=[], materials=recs)
    return _run_eval(raw=raw, manifest=man)[0]["loaded materials == manifest material inventory (parameters and bound slots)"]


def test_inventory_check_passes_when_the_loader_did_what_gltf_says() -> None:
    glass = _rec("glass", extensions={"KHR_materials_transmission": {"transmissionFactor": 0.9}, "KHR_materials_ior": {"ior": 1.45}},
                 double_sided=True)
    leaf = _rec("leaf", alpha_mode="MASK", alpha_cutoff=0.4)
    lamp = _rec("lamp", emissive_factor=[1, 0.5, 0], extensions={"KHR_materials_emissive_strength": {"emissiveStrength": 4.0}})
    plain = _rec("plain")
    recs = [glass, leaf, lamp, plain]
    assert _inv_check(recs, [_loaded(r) for r in recs]).status == "pass"


@pytest.mark.parametrize("rec,over,needle", [
    (_rec("g", extensions={"KHR_materials_transmission": {"transmissionFactor": 0.9}}), {"params_edit": ("transmission", 0.1)}, "transmission"),
    (_rec("l", alpha_mode="MASK", alpha_cutoff=0.4), {"params_edit": ("alphaTest", 0.5)}, "alphaTest"),
    (_rec("e", emissive_factor=[1, 0, 0]), {"params_edit": ("emissive", [0, 0, 0])}, "emissive"),
    (_rec("d", double_sided=True), {"doubleSided": False}, "doubleSided"),
    (_rec("b", alpha_mode="BLEND"), {"transparent": False}, "transparent"),
])
def test_inventory_check_fails_naming_the_parameter(rec, over, needle) -> None:
    over = dict(over)
    loaded = _loaded(rec)
    if "params_edit" in over:
        k, v = over.pop("params_edit")
        loaded["params"] = {**loaded["params"], k: v}
    loaded.update(over)
    check = _inv_check([rec], [loaded])
    assert check.status == "fail" and needle in check.detail, check


def test_inventory_check_fails_for_missing_extra_and_unbound_slots() -> None:
    assert "not loaded" in _inv_check([_rec("a")], [_loaded(_rec("b"))]).detail
    assert "not in the manifest" in _inv_check([], [_loaded(_rec("b"))]).detail
    rec = _rec("t", texture_slots=["emissiveTexture"])
    check = _inv_check([rec], [_loaded(rec)])  # the loader bound no emissiveMap
    assert check.status == "fail" and "emissiveMap" in check.detail


def test_transmission_draw_calls_are_counted_on_the_material_free_pass() -> None:
    rec = _rec("g", extensions={"KHR_materials_transmission": {"transmissionFactor": 0.9}})
    raw = _canned_raw(mask_draw_calls=1)
    raw["main"]["materials"] = [_loaded(rec)]
    raw["render"]["draw_calls"] = 3  # three.js drew the opaque objects again into its transmission buffer
    checks, _ = _run_eval(raw=raw, manifest=_canned_manifest(textures=[], materials=[rec]))
    assert checks["rendered draw calls == manifest draw_calls"].status == "pass"
    raw["mask_draw_calls"] = 2
    checks, _ = _run_eval(raw=raw, manifest=_canned_manifest(textures=[], materials=[rec]))
    assert checks["rendered draw calls == manifest draw_calls"].status == "fail"


def test_silhouette_and_framing_are_judged_on_the_geometry_pass_not_on_alpha() -> None:
    """A glass or cut-out asset leaves holes in the beauty frame's alpha; the geometry pass decides the silhouette."""
    holes = np.asarray(Image.open(io.BytesIO(_frame()))).copy()
    holes[150:350, 150:350, 3] = 0  # the material discards the middle of the object
    buf = io.BytesIO()
    Image.fromarray(holes, "RGBA").save(buf, "PNG")
    checks, iou = W.evaluate(_canned_raw(), [], [], [], _canned_manifest(), buf.getvalue(), _frame(), mask_png=_frame())
    assert iou == 1.0 and all(c.status == "pass" for c in checks), [str(c) for c in checks if c.status != "pass"]
    _, iou_alpha = W.evaluate(_canned_raw(), [], [], [], _canned_manifest(), buf.getvalue(), _frame())  # legacy: alpha only
    assert iou_alpha < MIN_SILHOUETTE_IOU


def _pbr_package(tmp_path, materials, name="Mat"):
    """A one-box GLB from the package's writer with ``materials`` ({name: GltfMaterial}), its inventory and reference."""
    from antigravity3d.kernel.gltf_writer import GltfMesh, build_glb
    from antigravity3d.kernel.package import material_inventory

    tm = trimesh.creation.box((1.0, 0.8, 0.6), transform=trimesh.transformations.translation_matrix((0, 0.4, 0)))
    pos = np.asarray(tm.vertices[tm.faces].reshape(-1, 3), np.float32)
    tri = np.arange(len(pos), dtype=np.uint32).reshape(-1, 3)
    nrm = np.repeat(np.asarray(tm.face_normals, np.float32), 3, axis=0)
    uv = np.tile(np.array([[0, 0], [1, 0], [1, 1]], np.float32), (len(tri), 1))
    material = materials
    mesh = GltfMesh("body", pos, tri, uvs=uv, material=material, normals=nrm)
    inv = material_inventory({"body": material})
    roles = {"baseColorTexture": "albedo", "normalTexture": "normal", "metallicRoughnessTexture": "orm", "emissiveTexture": "emissive"}
    records = [{"material": rec["name"], "role": roles[slot], "width": 32, "height": 32}
               for rec in inv for slot in rec["texture_slots"] if slot in roles]
    man = _manifest(tm, draw_calls=1, glb_batches={"body": ["body"]}, textures=records, materials=inv)
    return _write_pkg(tmp_path, build_glb([mesh]), man, name), tm, man


def _alpha_png(seed=9) -> bytes:
    rng = np.random.default_rng(seed)
    a = rng.integers(40, 255, (32, 32, 4), dtype=np.uint8)
    a[..., 3] = np.where((np.arange(32)[:, None] // 4 + np.arange(32)[None] // 4) % 2 == 0, 255, 0)
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _material_cases():
    from antigravity3d.kernel.gltf_writer import EncodedImage, GltfMaterial, TextureRef

    return {
        "opaque": GltfMaterial(name="m", base_color_factor=(0.6, 0.5, 0.4, 1.0)),
        "glass": GltfMaterial(name="m", base_color_factor=(0.9, 0.95, 1.0, 1.0), metallic_factor=0.0, roughness_factor=0.05, double_sided=True,
                              extensions={"KHR_materials_transmission": {"transmissionFactor": 0.95}, "KHR_materials_ior": {"ior": 1.45}}),
        "cutout": GltfMaterial(name="m", base_color_factor=(1, 1, 1, 1.0), metallic_factor=0.0, alpha_mode="MASK", alpha_cutoff=0.5,
                               base_color_texture=TextureRef(EncodedImage.from_bytes(_alpha_png())), double_sided=True),
        "blend": GltfMaterial(name="m", base_color_factor=(0.2, 0.6, 0.9, 0.3), metallic_factor=0.0, alpha_mode="BLEND"),
        "emissive": GltfMaterial(name="m", base_color_factor=(0.05, 0.05, 0.05, 1.0), metallic_factor=0.0, emissive_factor=(1.0, 0.7, 0.2),
                                 extensions={"KHR_materials_emissive_strength": {"emissiveStrength": 4.0}}),
        "everything": GltfMaterial(name="m", base_color_factor=(0.5, 0.1, 0.1, 1.0), metallic_factor=0.0, roughness_factor=0.4,
                                   extensions={"KHR_materials_clearcoat": {"clearcoatFactor": 1.0, "clearcoatRoughnessFactor": 0.1},
                                               "KHR_materials_sheen": {"sheenColorFactor": [0.8, 0.7, 0.9], "sheenRoughnessFactor": 0.4},
                                               "KHR_materials_specular": {"specularFactor": 0.5, "specularColorFactor": [0.9, 0.9, 1.0]}}),
    }


@pytest.mark.browser
@pytest.mark.parametrize("case", ["opaque", "glass", "cutout", "blend", "emissive", "everything"])
def test_browser_runtime_received_what_was_authored_and_silhouette_stays_geometric(case, runtime, tmp_path):
    material = _material_cases()[case]
    d, tm, man = _pbr_package(tmp_path, material)
    rep = _verify(d, _reference(tm, man))
    assert rep.ok, rep.summary()
    inventory = rep.checks[[c.name for c in rep.checks].index("loaded materials == manifest material inventory (parameters and bound slots)")]
    assert inventory.status == "pass", inventory
    assert rep.silhouette_iou >= MIN_SILHOUETTE_IOU


@pytest.mark.browser
def test_browser_silhouette_iou_does_not_depend_on_the_material(runtime, tmp_path):
    ious = {}
    for case in ("opaque", "glass", "cutout", "blend"):
        (tmp_path / case).mkdir()
        d, tm, man = _pbr_package(tmp_path / case, _material_cases()[case])
        ious[case] = _verify(d, _reference(tm, man)).silhouette_iou
    assert max(ious.values()) - min(ious.values()) < 1e-6, ious  # identical geometry, identical mask


@pytest.mark.browser
@pytest.mark.parametrize("case,edit,needle", [
    ("glass", lambda m: m["extensions"]["KHR_materials_transmission"].update(transmissionFactor=0.2), "transmission"),
    ("cutout", lambda m: m.update(alpha_cutoff=0.9), "alphaTest"),
    ("emissive", lambda m: m.update(emissive_factor=[0.0, 0.0, 0.0]), "emissive"),
    ("blend", lambda m: m.update(alpha_mode="OPAQUE"), "transparent"),
    ("opaque", lambda m: m.update(double_sided=True), "doubleSided"),
])
def test_browser_a_manifest_that_misstates_the_material_fails(case, edit, needle, runtime, tmp_path):
    d, tm, man = _pbr_package(tmp_path, _material_cases()[case])
    on_disk = json.loads((d / "manifest.json").read_text())
    edit(on_disk["materials"][0])
    (d / "manifest.json").write_text(json.dumps(on_disk))
    failed = _failed(_verify(d, _reference(tm, man)))
    key = "loaded materials == manifest material inventory (parameters and bound slots)"
    assert key in failed and needle in failed[key], failed


# --------------------------------------------------------------------------- the process's shared browser


#: the verification page's environment as it was rendered on every run before it was shared: PMREMGenerator in the page
_IN_PAGE_ENVIRONMENT = """async function environment() {
  const renderer = window.__renderer;
  const s = new THREE.Scene(), g = new THREE.SphereGeometry(10, 32, 16), c = new THREE.Color(), sky = new THREE.Color(job.rig.env_sky), gnd = new THREE.Color(job.rig.env_ground);
  const col = []; const p = g.attributes.position;
  for (let i = 0; i < p.count; i++) { c.copy(gnd).lerp(sky, (p.getY(i) / 10 + 1) / 2); col.push(c.r, c.g, c.b); }
  g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  s.add(new THREE.Mesh(g, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide })));
  const pm = new THREE.PMREMGenerator(renderer), t = pm.fromScene(s).texture; pm.dispose(); return t;
}
"""


def _in_page_environment_page() -> str:
    page = W._PAGE
    start, end = page.index("async function environment() {"), page.index("async function main() {")
    page = page[:start] + _IN_PAGE_ENVIRONMENT + page[end:]
    return page.replace("renderer.debug.checkShaderErrors = true;", "renderer.debug.checkShaderErrors = true; window.__renderer = renderer;")


def _tagged(tag: str) -> list[int]:
    """Live processes whose environment carries ``tag``: a driver, a Chromium or one of its helpers started under it."""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/environ", "rb") as f:
                env = f.read()
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue
        if f"AGY3D_PROC_TAG={tag}".encode() in env.split(b"\0") and stat[stat.rindex(b")") + 2:][:1] != b"Z":
            out.append(int(name))  # the scenario's own interpreter has exited by the time this is asked
    return out


def _gone(tag: str, within: float = 15.0) -> list[int]:
    deadline = time.monotonic() + within
    while (left := _tagged(tag)) and time.monotonic() < deadline:
        time.sleep(0.1)
    return left


def _bytes_of(data_url: str) -> bytes:
    return base64.b64decode(data_url.split(",", 1)[1])


@pytest.mark.browser
def test_browser_shared_environment_renders_what_the_page_rendered_itself(good, tmp_path, monkeypatch):
    """The prefiltered environment loaded from the shared bytes lights the package exactly as PMREMGenerator in the page
    did: the silhouette frame is identical and the beauty frame differs at most in the odd edge pixel that SwiftShader's
    threads already decide differently from run to run with either environment."""
    _, man, d = good

    def frames(sub):
        page_dir = tmp_path / sub
        W.build_page(page_dir, (d / "Crate.glb").read_bytes(), man["glb_bounds_m"])
        run = asyncio.run(W.run_page(page_dir, W.WebThreeValidator().binary(), 60))
        assert not run["raw"]["errors"] and not run["blocked"], run
        return _bytes_of(run["raw"]["png"]), _bytes_of(run["raw"]["mask_png"]), page_dir

    shared = [frames(f"shared{i}") for i in range(2)]
    assert (shared[0][2] / W.ENV_FILE).read_bytes() == (shared[1][2] / W.ENV_FILE).read_bytes()  # kept beside the page
    monkeypatch.setattr(W, "_PAGE", _in_page_environment_page())
    own = [frames(f"own{i}") for i in range(2)]
    assert {m for _, m, _ in shared} == {m for _, m, _ in own} and len({m for _, m, _ in own}) == 1
    for a in (s for s, _, _ in shared):
        for b in (o for o, _, _ in own):
            diff = np.abs(np.asarray(Image.open(io.BytesIO(a)), np.int16) - np.asarray(Image.open(io.BytesIO(b)), np.int16)).max(axis=2)
            assert (diff > 0).sum() <= 2, (diff > 0).sum()


@pytest.mark.browser
def test_browser_is_reused_across_event_loops_and_threads(good, runtime, tmp_path):
    """One ``asyncio.run`` per package, and a worker thread: later verifications reuse the running browser."""
    import threading

    tm, man, d = good
    ref = _reference(tm, man)
    assert _verify(d, ref).ok
    launches = B.SHARED.launches
    reps = [_verify(d, ref) for _ in range(2)]
    box = {}
    t = threading.Thread(target=lambda: box.update(rep=_verify(d, ref)))
    t.start()
    t.join(120)
    reps.append(box["rep"])
    assert all(r.ok for r in reps), [r.summary() for r in reps]
    assert B.SHARED.launches == launches, "a later verification started its own browser"
    assert len({r.diagnostics["browser"] for r in reps}) == 1


@pytest.mark.browser
def test_browser_concurrent_verifications_do_not_see_each_other(good, runtime, tmp_path):
    """Two packages at once on one browser: each gets its own context, so the broken one's loader errors, console
    warnings and requests stay in its own report."""
    tm, man, d = good
    ref = _reference(tm, man)

    def dangle(doc, blob):
        doc["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]["index"] = 99

    bad = _write_pkg(tmp_path, _edit_glb(_glb_of(tm), dangle), man)

    async def both():
        return await asyncio.gather(*(W.WebThreeValidator().validate(p, 60, reference=ref) for p in (d, bad, d)))

    good_a, broken, good_b = asyncio.run(both())
    assert not broken.ok and broken.failures
    for rep in (good_a, good_b):
        assert rep.ok, rep.summary()
        assert not rep.diagnostics["errors"] and not rep.diagnostics["blocked_requests"] and not rep.diagnostics["page_errors"]
        assert not [c for c in rep.diagnostics["console"] if c["type"] in ("error", "warning") and "GPU stall" not in c["text"]]


def _session() -> B.Session:
    s = B.SHARED._session
    assert s is not None and s.alive()
    return s


def _browser_pid(s: B.Session) -> int:
    kids = B._children().get(s.driver.pid, [])
    assert kids, "the driver has no browser process"
    return kids[0]


@pytest.mark.browser
@pytest.mark.parametrize("how", ["crashed", "closed", "hung"])
def test_browser_a_dead_or_hung_browser_is_replaced(how, good, runtime, monkeypatch):
    import signal

    tm, man, d = good
    ref = _reference(tm, man)
    assert _verify(d, ref).ok
    s = _session()
    if how == "crashed":
        os.kill(_browser_pid(s), signal.SIGKILL)
    elif how == "closed":
        B.SHARED.submit(s.browser.close()).result(30)
    else:
        monkeypatch.setattr(B, "CONTEXT_TIMEOUT_S", 3.0)
        monkeypatch.setattr(B, "CLOSE_TIMEOUT_S", 2.0)
        os.kill(_browser_pid(s), signal.SIGSTOP)
    t0 = time.monotonic()
    rep = _verify(d, ref)
    assert rep.ok, rep.summary()
    assert _session().generation > s.generation
    assert time.monotonic() - t0 < 40
    if how == "hung":
        assert not B._children().get(s.driver.pid) and s.driver.returncode is not None, "the hung browser was not killed"


@pytest.mark.browser
def test_browser_verification_timeout_is_honoured_and_the_browser_stays_usable(good, runtime, tmp_path, monkeypatch):
    tm, man, d = good
    ref = _reference(tm, man)
    hung = W._PAGE.replace("const job = JSON.parse", "for (;;) {}\nconst job = JSON.parse")  # the page's script never returns
    assert hung != W._PAGE
    monkeypatch.setattr(W, "_PAGE", hung)
    t0 = time.monotonic()
    rep = asyncio.run(W.WebThreeValidator().validate(d, 3, reference=ref))
    assert not rep.ok and "browser run failed" in rep.log_tail and "Timeout" in rep.log_tail
    assert time.monotonic() - t0 < 3 + 15 + 2 * B.CLOSE_TIMEOUT_S
    monkeypatch.undo()
    assert _verify(d, ref).ok


_SCENARIO = r'''
import asyncio, json, os, sys, time
from pathlib import Path

from antigravity3d.adapters.engines import web_browser as B, web_three as W



def tagged():
    """Processes started under this run's tag other than this interpreter (and its multiprocessing workers): the
    Playwright driver, Chromium and Chromium's helpers."""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            env = open(f"/proc/{name}/environ", "rb").read().split(b"\0")
            stat = open(f"/proc/{name}/stat", "rb").read()
            cmd = open(f"/proc/{name}/cmdline", "rb").read().split(b"\0")[0]
        except (OSError, ValueError):
            continue
        if f"AGY3D_PROC_TAG={tag}".encode() in env and stat[stat.rindex(b")") + 2:][:1] != b"Z" and os.path.basename(cmd) != os.path.basename(sys.executable.encode()):
            out.append(int(name))
    return out


def gone(within=15.0):
    deadline = time.monotonic() + within
    while (left := tagged()) and time.monotonic() < deadline:
        time.sleep(0.1)
    return left


async def until_running():
    while not B.SHARED.running:
        await asyncio.sleep(0.05)


def dag():
    from antigravity3d.dags.prop_to_engine import PropToEngineDAG
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient
    from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.authoring import TemplateAuthor

    plane = DecisionControlPlane(FakeDecisionsClient(answers={"pipeline_dag": "prop_to_engine", "needs_ortho_blueprint": 0.9, "complexity_tier": 2.0}))
    return PropToEngineDAG(plane=plane, forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=work, budget=EngineBudget("unreal_5"))



if __name__ == "__main__":  # spawned multiprocessing workers import this file again
    tag, scenario, pkg = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    work = Path(sys.argv[4])
    report = {}
    if scenario == "exit":  # verified, browser kept for the next package, then the interpreter ends
        rep = asyncio.run(W.WebThreeValidator().validate(pkg, 60))
        report = {"ok": rep.ok, "running": len(tagged())}
    elif scenario == "verification_cancelled":
        async def main():
            task = asyncio.ensure_future(W.WebThreeValidator().validate(pkg, 60))
            await until_running()
            await asyncio.sleep(0.3)
            report["running"] = len(tagged())
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                report["cancelled"] = True
        asyncio.run(main())
        report["left"] = gone()
    elif scenario in ("run_cancelled", "run_failed"):
        from antigravity3d.dags import prop_to_engine

        async def refine(*a, **k):  # the run gets as far as the pyramid, with the browser starting in the background
            await until_running()
            report["running"] = len(tagged())
            if scenario == "run_failed":
                raise RuntimeError("the pyramid failed")
            await asyncio.sleep(3600)
        prop_to_engine.refine = refine

        async def main():
            task = asyncio.ensure_future(dag().run("oak wine barrel"))
            if scenario == "run_cancelled":
                while "running" not in report:
                    await asyncio.sleep(0.05)
                task.cancel()
            try:
                rep = await task
                report["dag_ok"] = rep.ok
            except asyncio.CancelledError:
                report["cancelled"] = True
        asyncio.run(main())
        report["left"] = gone()
    elif scenario == "run_succeeded":  # a whole offline run: the browser was started early and is kept afterwards
        rep = asyncio.run(dag().run("oak wine barrel"))
        steps = {s["name"]: s["status"] for s in rep.extras["pyramid"]["steps"]}
        report = {"dag_ok": rep.ok, "web_runtime": steps.get("web_runtime"), "running": len(tagged()), "launches": B.SHARED.launches}
    elif scenario == "spawned_worker":  # multiprocessing workers leave through os._exit, which skips atexit
        import concurrent.futures as cf
        import multiprocessing as mp

        import tests_web_worker
        with cf.ProcessPoolExecutor(1, mp_context=mp.get_context("spawn")) as ex:
            report["ok"] = ex.submit(tests_web_worker.verify, str(pkg)).result()
            report["running"] = len(tagged())
        report["left"] = gone()
    print(json.dumps(report))
'''

_WORKER = '''
import asyncio
from antigravity3d.adapters.engines.web_three import WebThreeValidator


def verify(pkg):
    return asyncio.run(WebThreeValidator().validate(pkg, 60)).ok
'''


@pytest.mark.browser
@pytest.mark.parametrize("scenario", ["exit", "verification_cancelled", "run_cancelled", "run_failed", "run_succeeded", "spawned_worker"])
def test_browser_no_driver_or_browser_outlives_the_interpreter_or_a_failed_or_cancelled_run(scenario, good, runtime, tmp_path):
    import subprocess
    import sys
    import uuid

    _, _, d = good
    tag = uuid.uuid4().hex
    (tmp_path / "scenario.py").write_text(_SCENARIO)
    (tmp_path / "tests_web_worker.py").write_text(_WORKER)
    env = dict(os.environ, AGY3D_PROC_TAG=tag, PYTHONPATH=os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, str(tmp_path / "scenario.py"), tag, scenario, str(d), str(tmp_path / "work")],
                       env=env, capture_output=True, text=True, timeout=300, cwd=tmp_path, check=False)
    assert p.returncode == 0, p.stderr[-3000:]
    report = json.loads(p.stdout.strip().splitlines()[-1])
    assert report["running"] > 0, report  # the browser really was up at the checkpoint
    if scenario in ("verification_cancelled", "run_cancelled"):
        assert report["cancelled"]
    if scenario == "run_failed":
        assert report["dag_ok"] is False
    if scenario == "run_succeeded":
        assert report["dag_ok"] and report["web_runtime"] == "passed" and report["launches"] == 1
    if scenario in ("exit", "spawned_worker"):
        assert report["ok"]
    assert report.get("left", []) == [], report  # gone while the interpreter was still running
    assert _gone(tag) == [], "a driver or browser outlived the interpreter"


@pytest.mark.browser
def test_browser_command_line_in_fresh_interpreters(l_packages, tmp_path):
    """``python -m ...web_three PACKAGE`` still works, and does not hang: in a fresh interpreter the browser thread imports
    nothing that could deadlock with the reference thread's first import of pxr (it did about every other run)."""
    import subprocess
    import sys

    d = l_packages["godot_4"]  # a real package: its USD reference is rendered on a worker thread beside the browser
    for i in range(4):
        p = subprocess.run([sys.executable, "-m", "antigravity3d.adapters.engines.web_three", str(d), "--out", str(tmp_path / f"o{i}")],
                           capture_output=True, text=True, timeout=120, check=False)
        assert p.returncode == 0 and p.stdout.startswith("web_three: PASS"), p.stdout[-2000:] + p.stderr[-2000:]
    assert {f.name for f in (tmp_path / "o0").iterdir()} == {"page.html", "vendor", "env", "web_three.png", "silhouette.png", "reference.png"}
