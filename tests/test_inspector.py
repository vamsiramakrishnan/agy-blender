from __future__ import annotations

import base64
import hashlib
import io
import json
import re

import numpy as np
import pytest
import trimesh
from PIL import Image

from antigravity3d.artifacts.inspector import (
    THREE_JS_URL,
    InspectorQA,
    ReviewNote,
    build_inspector,
    read_notes,
)
from antigravity3d.kernel.gltf_writer import EncodedImage, GltfMaterial, TextureRef
from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget


def _png(rgb: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), rgb).save(buf, "PNG")
    return buf.getvalue()


def _material(mid: str, albedo: tuple[int, int, int] = (200, 10, 10)) -> GltfMaterial:
    """A textured material as the package binds one: white base colour under the albedo map, one ORM texture
    shared by occlusion and metallic-roughness, a normal map."""
    orm = TextureRef(EncodedImage.from_bytes(_png((255, 128, 0)), f"{mid}_orm"))
    return GltfMaterial(name=mid, base_color_texture=TextureRef(EncodedImage.from_bytes(_png(albedo), f"{mid}_albedo")),
                        metallic_roughness_texture=orm, occlusion_texture=orm,
                        normal_texture=TextureRef(EncodedImage.from_bytes(_png((128, 128, 255)), f"{mid}_normal")))


def _materials(meshes, albedo: tuple[int, int, int] = (200, 10, 10)) -> dict[str, GltfMaterial]:
    """``{node_id: GltfMaterial}``, one object per material id (meshes of one material share it)."""
    made = {mid: _material(mid, albedo) for mid in dict.fromkeys(m.material_id for m in meshes)}
    return {m.node_id: made[m.material_id] for m in meshes}


def _mesh(node_id: str, tm: trimesh.Trimesh, mat: str) -> MeshData:
    return MeshData(node_id, f"/Asset/{node_id}", np.asarray(tm.vertices, float), np.asarray(tm.faces, np.int32), mat)


@pytest.fixture
def scene():
    box = trimesh.creation.box((1, 2, 3))  # x=1, y=2, z=3 extents (Z-up: 3 m tall)
    box.apply_translation((0, 0, 1.5))
    cyl = trimesh.creation.cylinder(radius=0.2, height=1.0, sections=12)
    sph = trimesh.creation.icosphere(subdivisions=1, radius=0.3)
    meshes = [_mesh("body", box, "m_a"), _mesh("pole", cyl, "m_a"), _mesh("ball", sph, "m_b")]
    uvs = {m.node_id: np.zeros((len(m.points), 2), np.float32) for m in meshes}
    tex = _materials(meshes)
    lods = [("LOD1", np.asarray(box.vertices), np.asarray(box.faces))]
    cols = [("hull_body", np.asarray(box.vertices), np.asarray(box.faces))]
    return meshes, uvs, tex, lods, cols


def _payload(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="payload">(.*?)</script>', html, re.S)
    assert m, "payload script missing"
    return json.loads(m.group(1))


def _build(tmp_path, scene, **kw):
    meshes, uvs, tex, lods, cols = scene
    args = dict(
        asset_name="crate",
        budget=EngineBudget("unreal_5", max_tris=15_000, max_draw_calls=1, max_collision_hulls=8),
        materials=tex, lods=lods, colliders=cols, uvs=uvs, out_path=tmp_path / "crate.html",
    )
    args.update(kw)
    return build_inspector(meshes, **args)


def _glb(payload: dict):
    """The review GLB embedded in a standalone payload, parsed with pygltflib, plus a reader for accessors."""
    import pygltflib

    data = base64.b64decode(payload["glb_b64"])
    g = pygltflib.GLTF2.load_from_bytes(data)
    blob = g.binary_blob()

    def read(accessor: int) -> np.ndarray:
        acc = g.accessors[accessor]
        view = g.bufferViews[acc.bufferView]
        dtype = {5126: "<f4", 5125: "<u4"}[acc.componentType]
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[acc.type]
        return np.frombuffer(blob, dtype, acc.count * width, view.byteOffset or 0).reshape(acc.count, width)

    return g, read, data


def test_round_trip_and_y_up(tmp_path, scene):
    """The asset travels as the review GLB: Y-up positions, crease normals, glTF UVs, materials, LODs, hulls."""
    meshes = scene[0]
    html = _build(tmp_path, scene).read_text(encoding="utf-8")
    p = _payload(html)
    assert [m["id"] for m in p["nodes"]] == ["body", "pole", "ball"]
    g, read, data = _glb(p)
    assert hashlib.sha256(data).hexdigest() == p["glb"]["sha256"] and len(data) == p["glb"]["bytes"]
    lod0 = p["levels"][0]
    assert [part["node"] for part in lod0["parts"]] == ["body", "pole", "ball"]
    for part, src in zip(lod0["parts"], meshes, strict=True):
        prim = g.meshes[g.nodes[part["glb"]].mesh].primitives[0]
        pos, nrm, idx = read(prim.attributes.POSITION), read(prim.attributes.NORMAL), read(prim.indices).reshape(-1, 3)
        assert part["tris"] == len(src.triangles) == len(idx) and part["uv"] and prim.attributes.TEXCOORD_0 is not None
        # Z-up (x, y, z) -> Y-up (x, z, -y); vertices are only duplicated (crease / seam splits), never moved
        yup = np.stack([src.points[:, 0], src.points[:, 2], -src.points[:, 1]], 1)
        gap = np.abs(pos[:, None, :] - yup[None, :, :]).max(axis=2)
        assert gap.min(axis=1).max() < 1e-6 and gap.min(axis=0).max() < 1e-6  # same point set (float32)
        np.testing.assert_allclose(np.linalg.norm(nrm, axis=1), 1.0, atol=1e-5)
        assert prim.material is not None and set(part["maps"]) == {"albedo", "orm", "normal"}
    body = read(g.meshes[g.nodes[lod0["parts"][0]["glb"]].mesh].primitives[0].attributes.POSITION)
    assert body[:, 1].max() == pytest.approx(3.0)  # 3 m tall box: height is glTF / three.js Y
    assert body[:, 1].min() == pytest.approx(0.0)
    assert len(p["levels"]) == 2 and p["levels"][1]["parts"][0]["node"] is None and len(p["colliders"]) == 1
    hull = g.meshes[g.nodes[p["colliders"][0]["glb"]].mesh].primitives[0]
    assert hull.attributes.NORMAL is None and hull.material is None  # hulls as the package writes them
    assert len(g.materials) == 2 and len(g.images) == 3  # m_a and m_b share three identical PNGs: embedded once


def test_self_contained_urls(tmp_path, scene):
    html = _build(tmp_path, scene).read_text(encoding="utf-8")
    assert set(re.findall(r"https?://[^\s\"'<>\\)]+", html)) == {THREE_JS_URL}
    assert "/three.js/0.160.0/three.module.min.js" in THREE_JS_URL
    offline = _build(tmp_path, scene, three_js_source="export const REVISION='x';", out_path=tmp_path / "o.html")
    text = offline.read_text(encoding="utf-8")
    assert re.findall(r"https?://", text) == []
    assert "export const REVISION='x';" in text
    assert _payload(text)["three_url"] is None


def test_script_breakout_is_escaped(tmp_path, scene):
    evil = "</script><img src=x onerror=alert(1)>"
    qa = InspectorQA(False, evil, 2.0, {evil: 0.9}, [evil, "<!-- x"])
    html = _build(tmp_path, scene, asset_name=evil, qa=qa, three_js_source="/* </script> */ 1;").read_text(encoding="utf-8")
    # exactly the document's own closing tags: payload, three-src, app script
    assert html.count("</script>") == 3
    assert "<img src=x" not in html
    assert _payload(html)["asset_name"] == evil
    assert "<title>&lt;/script&gt;" in html


def test_budget_numbers_are_text(tmp_path, scene):
    html = _build(tmp_path, scene, qa=InspectorQA(True, "ok", 3.7, {"seam": 0.2, "stretch": 0.8}, ["d1"])).read_text()
    assert "Target engine: unreal_5" in html
    assert "460 / 15,000" in html or re.search(r"[\d,]+ / 15,000", html)
    assert "2 / 1" in html and "OVER BUDGET by 1" in html  # 2 materials vs max_draw_calls=1
    assert "1 / 8" in html  # one collider hull
    assert "export threshold 3.4" in html and "Meets export threshold" in html
    assert html.index("stretch") < html.index("seam")  # defects sorted descending
    assert "<button" in html and 'type="button"' in html


def test_missing_textures_and_bad_input(tmp_path, scene):
    meshes = scene[0]
    out = build_inspector(
        meshes, asset_name="bare", budget=EngineBudget("godot_4"), out_path=tmp_path / "sub" / "bare.html"
    )
    p = _payload(out.read_text())
    assert all(not n["maps"] and not n["uv"] for n in p["nodes"]) and len(p["levels"]) == 1 and p["colliders"] == []
    g, _, _ = _glb(p)
    assert not g.images and not g.materials
    with pytest.raises(ValueError):
        build_inspector(meshes, asset_name="x", budget=EngineBudget("godot_4"), uvs={"body": np.zeros((2, 2))}, out_path=tmp_path / "x.html")


def test_read_notes(tmp_path):
    f = tmp_path / "notes.json"
    f.write_text(json.dumps({
        "schema": "antigravity3d.review_notes/1", "asset_name": "crate", "exported_at": "2026-01-01T00:00:00Z",
        "notes": [{"node_id": None, "text": "overall too glossy"}, {"node_id": "pole", "text": "too thin"}, {"node_id": "x", "text": " "}],
    }))
    assert read_notes(f) == [ReviewNote("crate", None, "overall too glossy"), ReviewNote("crate", "pole", "too thin")]
    f.write_text("{}")
    with pytest.raises(ValueError):
        read_notes(f)


@pytest.mark.browser
def test_browser_channels_canvas_and_budget(tmp_path, scene):
    import os
    from pathlib import Path

    source = os.environ.get("AGY3D_THREE_JS")
    if not source:
        pytest.skip("set AGY3D_THREE_JS to a local Three.js 0.160.0 module")
    from playwright.sync_api import sync_playwright

    html = _build(tmp_path, scene, three_js_source=Path(source).read_text())
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_BIN"),
                                     args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(html.as_uri())
            page.wait_for_function("window.__inspectorReady || window.__inspectorError")
            assert page.evaluate("window.__inspectorError") is None
            assert page.evaluate("window.__inspectorState.glOk")
            canvas = page.locator("#gl")
            assert canvas.is_visible()
            assert page.locator(".gauge").count() == 3
            assert page.locator('.gauge[data-state="over"]').count() == 1
            modes = page.locator("button[data-mode]")
            assert modes.count() == 10
            frames = {}
            for button in modes.all():
                mode = button.get_attribute("data-mode")
                button.click()
                page.evaluate("new Promise(ok => requestAnimationFrame(() => requestAnimationFrame(ok)))")
                assert page.locator("body").get_attribute("data-channel") == mode
                assert button.get_attribute("aria-pressed") == "true"
                frames[mode] = canvas.screenshot()
            assert frames["albedo"] != frames["prim"]
            assert not errors, errors
        finally:
            browser.close()


# --------------------------------------------------------------------------- one front end, two hosts

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
APP_DIR = REPO / "apps" / "inspector"


def _build_inputs() -> list[str]:
    """The same input set apps/inspector/build.mjs hashes (keep in step with inputFiles() there)."""
    files = [APP_DIR / "build.mjs", APP_DIR / "package.json", APP_DIR / "package-lock.json",
             *(p for p in (APP_DIR / "src").rglob("*") if p.is_file()), APP_DIR / "test-host" / "host.js",
             REPO / "src/antigravity3d/artifacts/inspector_template.html",
             REPO / "src/antigravity3d/artifacts/vendor/three-r160.module.min.js.gz",
             REPO / "src/antigravity3d/artifacts/vendor/GLTFLoader-r160.js.gz",
             REPO / "src/antigravity3d/artifacts/vendor/BufferGeometryUtils-r160.js.gz",
             REPO / "src/antigravity3d/artifacts/vendor/three-r160-addons.provenance.json"]
    return sorted(p.relative_to(REPO).as_posix() for p in files)


def test_committed_app_build_is_current():
    """Fails when the committed build outputs are stale relative to their sources, or were edited by hand.

    Rebuild with: cd apps/inspector && npm ci && npm run build
    """
    import hashlib

    if not APP_DIR.is_dir():
        pytest.skip("app sources are not part of an installed wheel")
    record = json.loads((REPO / "src/antigravity3d/artifacts/web/build.json").read_text())
    inputs = _build_inputs()
    assert record["inputs"] == inputs, "build inputs changed: rebuild apps/inspector"
    digest = hashlib.sha256()
    for rel in inputs:
        digest.update(f"{rel}\0{hashlib.sha256((REPO / rel).read_bytes()).hexdigest()}\n".encode())
    assert record["inputs_sha256"] == digest.hexdigest(), "app sources changed since the last build: cd apps/inspector && npm ci && npm run build"
    for rel, out in record["outputs"].items():
        data = (REPO / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == out["sha256"], f"{rel} differs from the build output"
    # pinned, third-party code recorded with version, lockfile integrity and licence
    lock = json.loads((APP_DIR / "package-lock.json").read_text())
    pkg = json.loads((APP_DIR / "package.json").read_text())
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in pkg["devDependencies"].values()), "pin exact versions"
    bundled = {b["name"]: b for b in record["bundled"]}
    assert bundled["three"]["version"] == "0.160.0"
    provenance = json.loads((REPO / "src/antigravity3d/artifacts/vendor/three-r160-addons.provenance.json").read_text())
    for name, gz in (("three/examples/jsm/loaders/GLTFLoader.js", "GLTFLoader-r160.js.gz"),
                     ("three/examples/jsm/utils/BufferGeometryUtils.js", "BufferGeometryUtils-r160.js.gz")):
        assert bundled[name]["sha256"] == provenance["files"][gz]["sha256_uncompressed"], name
    assert bundled["@modelcontextprotocol/ext-apps"]["version"] == pkg["devDependencies"]["@modelcontextprotocol/ext-apps"]
    for name, b in bundled.items():
        assert b["license"], name
        if not name.startswith("three"):  # three and its addons come from the vendored files, not npm
            assert b["integrity"] == lock["packages"][f"node_modules/{name}"]["integrity"], name
    notices = (REPO / "src/antigravity3d/artifacts/web/THIRD_PARTY_NOTICES.txt").read_text()
    assert all(name in notices for name in bundled if not name.startswith("three/"))
    assert "GLTFLoader.js" in notices and "three-r160-addons.provenance.json" in notices


def test_mcp_app_document_is_self_contained():
    from antigravity3d.artifacts.inspector import app_html

    html = app_html()
    assert len(html.encode()) < 10 * 1024 * 1024
    assert not re.search(r"<(script|link|iframe|base)\b[^>]*\b(src|href)\s*=", html, re.I)
    assert html.count("<script") == 2  # the (empty) payload slot and the bundled app
    assert "THREE.WebGLRenderer" in html and "bundles three 0.160.0" in html.lower()  # three.js r160 is inside
    assert "localStorage" not in html  # nothing in the app (or the bundled libraries) touches browser storage


def test_payload_revision_is_a_content_hash(scene):
    from antigravity3d.artifacts.inspector import inspector_payload

    meshes, uvs, tex, lods, cols = scene
    budget = EngineBudget("unreal_5")
    a, glb_a = inspector_payload(meshes, asset_name="crate", budget=budget, materials=tex, uvs=uvs, lods=lods, colliders=cols)
    b, glb_b = inspector_payload(meshes, asset_name="crate", budget=budget, materials=tex, uvs=uvs, lods=lods, colliders=cols)
    assert a["revision"] == b["revision"] and len(a["revision"]) == 16 and glb_a == glb_b  # deterministic
    moved = [MeshData(m.node_id, m.prim_path, m.points + 0.01, m.triangles, m.material_id) for m in meshes]
    assert inspector_payload(moved, asset_name="crate", budget=budget)[0]["revision"] != \
        inspector_payload(meshes, asset_name="crate", budget=budget)[0]["revision"]
    retextured = _materials(meshes, albedo=(10, 200, 10))
    assert inspector_payload(meshes, asset_name="crate", budget=budget, materials=retextured, uvs=uvs)[0]["revision"] != \
        inspector_payload(meshes, asset_name="crate", budget=budget, materials=tex, uvs=uvs)[0]["revision"]


def test_welded_box_shades_flat_from_glb_normals():
    """A welded, indexed box (8 shared corners, as the stage extracts it) gets the shipped crease normals:
    every corner of every face carries that face's normal. The page no longer computes normals."""
    from antigravity3d.artifacts.inspector import inspector_payload

    box = trimesh.creation.box((1, 2, 3))
    assert len(box.vertices) == 8
    p, glb = inspector_payload([MeshData("body", "/A/body", np.asarray(box.vertices), np.asarray(box.faces), "m")],
                               asset_name="a", budget=EngineBudget("godot_4"))
    g, read, _ = _glb({**p, "glb_b64": base64.b64encode(glb).decode()})
    prim = g.meshes[0].primitives[0]
    pos, nrm, idx = read(prim.attributes.POSITION), read(prim.attributes.NORMAL), read(prim.indices).reshape(-1, 3)
    assert len(pos) == 24  # split at the 90 degree creases: 3 copies of each corner
    face = np.cross(pos[idx[:, 1]] - pos[idx[:, 0]], pos[idx[:, 2]] - pos[idx[:, 0]])
    face /= np.linalg.norm(face, axis=1, keepdims=True)
    for corner in range(3):
        np.testing.assert_allclose(nrm[idx[:, corner]], face, atol=1e-6)  # flat: vertex normal == face normal


def test_review_mesh_conversion_is_the_package_writers(tmp_path, scene):
    """One conversion, not two copies: the review GLB uses the kernel function fast_ops.write_gltf calls per item,
    and calling it the way the inspector does gives the bytes write_gltf ships for the same items."""
    from types import SimpleNamespace

    from antigravity3d.artifacts import review_glb
    from antigravity3d.kernel.fast_ops import gltf_mesh, write_gltf
    from antigravity3d.kernel.gltf_writer import build_glb

    assert review_glb.gltf_mesh is gltf_mesh
    meshes, uvs, *_ = scene
    items = [SimpleNamespace(points=m.points, triangles=m.triangles, uvs=uvs[m.node_id], node_id=m.node_id) for m in meshes]
    hull = SimpleNamespace(points=meshes[0].points, triangles=meshes[0].triangles, name="hull")
    shipped = write_gltf(tmp_path / "shipped.glb", [*items, hull], [*(i.node_id for i in items), "hull"]).read_bytes()
    review = build_glb([*(gltf_mesh(i.node_id, i.points, i.triangles, i.uvs, None, shade=True) for i in items),
                        gltf_mesh("hull", hull.points, hull.triangles, None, None, shade=False)])
    assert review == shipped


LIGHT_VARS = {
    "--color-background-primary": "#ffffff", "--color-background-secondary": "#f7f7f8", "--color-background-tertiary": "#ececf0",
    "--color-text-primary": "#1f1f1f", "--color-text-secondary": "#5f6368", "--color-border-primary": "#d5d7db",
    "--color-ring-primary": "#1a73e8", "--color-text-success": "#137333", "--color-text-danger": "#c5221f",
    "--font-sans": "Arial, sans-serif",
}
DARK_VARS = {
    "--color-background-primary": "#1e1f22", "--color-background-secondary": "#131416", "--color-background-tertiary": "#2a2c30",
    "--color-text-primary": "#e8eaed", "--color-text-secondary": "#9aa0a6", "--color-border-primary": "#3c4043",
    "--color-ring-primary": "#8ab4f8", "--color-text-success": "#81c995", "--color-text-danger": "#f28b82",
    "--font-sans": "Arial, sans-serif",
}
# The default CSP the Antigravity docs give for MCP App frames: default-src 'none'; inline scripts and styles plus
# data:/blob: images, fonts and media.
FRAME_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
             "font-src data: blob:; media-src data: blob:")


class _CannedServer:
    """Answers the app's tools/call like the studio MCP server would, from canned inspector data."""

    def __init__(self, revisions: dict[str, tuple[dict, bytes]], chunk: int = 1_000):
        self.revisions = revisions
        self.current = next(iter(revisions))
        self.notes: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.chunk = chunk

    def _nodes(self, rev):
        return {m["id"] for m in self.revisions[rev][0]["nodes"]}

    def _listed(self):
        return [{**n, "stale": n["revision"] != self.current, "node_missing": n["node_id"] is not None and n["node_id"] not in self._nodes(self.current)}
                for n in self.notes]

    def __call__(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        payload, glb = self.revisions[self.current]
        if name == "inspector_payload":
            if args.get("revision") and args["revision"] != self.current:
                return {"isError": True, "content": [{"type": "text", "text": "ERROR revision changed"}], "structuredContent": {"current_revision": self.current}}
            text = json.dumps(payload) if args["part"] == "scene" else base64.b64encode(glb).decode()  # part "glb"
            n = max(1, -(-len(text) // self.chunk))
            i = int(args.get("chunk", 0))
            return {"content": [{"type": "text", "text": f"PAYLOAD {i + 1}/{n}"}], "structuredContent": {
                "revision": self.current, "part": args["part"], "chunk": i, "chunks": n, "data": text[i * self.chunk:(i + 1) * self.chunk]}}
        if name == "inspector_list_notes":
            return {"content": [{"type": "text", "text": "NOTES"}], "structuredContent": {"revision": self.current, "notes": self._listed()}}
        if name == "inspector_save_note":
            node = args.get("node_id") or None
            rev = args.get("revision") or self.current
            self.notes = [n for n in self.notes if not (n["node_id"] == node and n["revision"] == rev)]
            if args["text"].strip():
                self.notes.append({"id": str(len(self.notes)), "node_id": node, "text": args["text"], "revision": rev, "updated_at": "2026-11-20T00:00:00+00:00"})
            return {"content": [{"type": "text", "text": "NOTE saved"}], "structuredContent": {"revision": self.current, "notes": self._listed()}}
        if name == "inspector_qa_state":
            return {"content": [{"type": "text", "text": "QA"}], "structuredContent": {"revision": self.current, "qa": {
                "tier1_ok": True, "tier1_line": "", "tier15_score": 3.1, "threshold": 3.4, "export_allowed": False,
                "audit_deltas": ["lookdev: roughness too uniform"], "defects": {}}}}
        return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name}"}]}


def _summary(server: _CannedServer, view: dict | None = None) -> dict:
    payload = server.revisions[server.current][0]
    return {"content": [{"type": "text", "text": "INSPECTOR s1"}], "structuredContent": {
        "schema": "antigravity3d.inspector_view/1", "session_id": payload["session_id"], "asset": payload["asset_name"],
        "revision": server.current, "view": view or {}, "notes": {"current": 0, "stale": 0}}}


def _serve(directory, headers: dict[str, str]):
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class H(SimpleHTTPRequestHandler):
        def end_headers(self):
            for k, v in headers.items():
                self.send_header(k, v)
            super().end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(directory)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.browser
def test_mcp_app_in_a_browser_host(tmp_path, scene):
    """The built MCP App in an iframe under a small host that speaks the host side of the protocol (the SDK's
    AppBridge): opaque-origin sandbox (no storage), the documented default CSP as a response header, no network."""
    import os
    import shutil
    from pathlib import Path

    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    from PIL import Image

    from antigravity3d.artifacts.inspector import app_html, inspector_payload

    meshes, uvs, tex, lods, cols = scene
    budget = EngineBudget("unreal_5", max_tris=15_000, max_draw_calls=1, max_collision_hulls=8)
    def canned(session_id, *args, asset="crate", **kw):
        payload, glb = inspector_payload(*args, asset_name=asset, budget=budget, **kw)
        payload["session_id"] = session_id  # as runtime/inspector_server.py adds it
        return payload, glb

    rev1 = canned("Crate-abc123", meshes, materials=tex, uvs=uvs, lods=lods, colliders=cols)
    moved = [MeshData(m.node_id, m.prim_path, m.points + np.array([0.0, 0.0, 0.25]), m.triangles, m.material_id) for m in meshes[:2]]
    rev2 = canned("Crate-abc123", moved, materials=tex, uvs={k: v for k, v in uvs.items() if k != "ball"}, lods=lods, colliders=cols)
    rev3 = canned("Other-def456", moved, asset="other crate", materials=tex, uvs={k: v for k, v in uvs.items() if k != "ball"})

    app_dir, host_dir = tmp_path / "app", tmp_path / "host"
    app_dir.mkdir(), host_dir.mkdir()
    (app_dir / "index.html").write_text(app_html(), encoding="utf-8")
    shutil.copy(APP_DIR / "test-host" / "host.bundle.js", host_dir / "host.bundle.js")
    (host_dir / "index.html").write_text('<!doctype html><html><head><meta charset="utf-8"><link rel="icon" href="data:,">'
                                         '<style>body{margin:0;background:#888}</style></head><body><div id="slot"></div>'
                                         '<script src="host.bundle.js"></script></body></html>')
    app_srv = _serve(app_dir, {"Content-Security-Policy": FRAME_CSP})
    host_srv = _serve(host_dir, {})
    app_url = f"http://127.0.0.1:{app_srv.server_address[1]}/index.html"
    host_url = f"http://localhost:{host_srv.server_address[1]}/index.html"  # a different origin from the app
    shots = Path(os.environ.get("AGY3D_SCREENSHOT_DIR") or tmp_path / "shots")
    shots.mkdir(parents=True, exist_ok=True)
    caps = {"serverTools": {}, "updateModelContext": {"text": {}}, "message": {"text": {}}}

    def wait_for(pred, what, page, timeout_s=10):
        import time

        end = time.time() + timeout_s
        while time.time() < end:
            if pred():
                return
            page.wait_for_timeout(50)
        raise AssertionError(f"timed out waiting for {what}")

    def open_host(browser, mode, width, height, theme):
        server = _CannedServer({r[0]["revision"]: r for r in (rev1, rev2, rev3)})
        page = browser.new_page(viewport={"width": width, "height": height})
        problems, requests = [], []
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" and not ("probe.png" in m.text or "data:,x" in m.text) else None)  # deliberate CSP probes
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("request", lambda r: requests.append(r.url))
        page.expose_function("serverCall", server)
        page.set_default_timeout(30_000)
        page.goto(host_url)
        page.evaluate("o => Promise.race([startHost(o), new Promise((_, no) => setTimeout(() => no(new Error('no ui/initialize from the app')), 20000))])", {"url": app_url, "mode": mode, "width": width, "height": height, "theme": theme,
                                             "variables": DARK_VARS if theme == "dark" else LIGHT_VARS, "capabilities": caps})
        frame = page.frame_locator("#app")
        app = next(f for f in page.frames if f.url == app_url)
        page.evaluate("r => sendTool({}, r)", _summary(server))
        # (wait_for_function polls with eval inside the frame, which the frame's CSP refuses: poll from here)
        wait_for(lambda: app.evaluate("() => !!(window.__inspectorReady || window.__inspectorError)"), "inspector ready", page, 30)
        return page, frame, app, server, problems, requests

    def settle(app):
        app.evaluate("new Promise(ok => requestAnimationFrame(() => requestAnimationFrame(ok)))")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_BIN"),
                                         args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        except Exception as e:  # same convention as the other browser tests: no browser here, not a failure
            if "Executable doesn't exist" in str(e):
                pytest.skip("no Chromium for this Playwright; set CHROMIUM_BIN")
            raise
        try:
            # ---- interactions: wide inline frame, light theme
            page, frame, app, server, problems, requests = open_host(browser, "inline", 1000, 900, "light")
            assert app.evaluate("window.__inspectorError") is None and app.evaluate("window.__inspectorState.glOk")
            # storage is unavailable in the opaque-origin frame, and the CSP blocks fetch and eval
            assert app.evaluate("(() => { try { localStorage.length; return 'available'; } catch (e) { return 'blocked'; } })()") == "blocked"
            assert app.evaluate("fetch('data:,x').then(() => 'allowed', () => 'blocked')") == "blocked"
            # (DevTools evaluation is exempt from the page's CSP for eval, so probe with a resource load instead)
            assert app.evaluate("""u => new Promise(ok => {
                document.addEventListener('securitypolicyviolation', e => ok(e.effectiveDirective), {once: true});
                const i = new Image(); i.src = u; })""", host_url.replace("index.html", "probe.png")) == "img-src"
            # data arrived through the app-only tool, in chunks: the review metadata, then the review GLB
            scene_calls = [a for n, a in server.calls if n == "inspector_payload" and a["part"] == "scene"]
            glb_calls = [a for n, a in server.calls if n == "inspector_payload" and a["part"] == "glb"]
            assert len(scene_calls) > 1 and {a["revision"] for a in scene_calls} == {rev1[0]["revision"]}
            assert len(glb_calls) > 1 and {a["revision"] for a in glb_calls} == {rev1[0]["revision"]}
            assert not any(a["part"] not in ("scene", "glb") for n, a in server.calls if n == "inspector_payload")
            # the meshes carry the GLB's normals and materials (GLTFLoader), not page-computed ones
            loaded = {m["node"]: m for m in app.evaluate("window.__inspectorMeshes()")}
            assert set(loaded) == {"body", "pole", "ball"}
            assert all(m["normals"] and m["material"]["map"] and m["material"]["normalMap"] and m["material"]["aoMap"]
                       for m in loaded.values())
            assert frame.locator("#nodes tbody tr").count() == 3 and frame.locator(".gauge").count() >= 3
            assert frame.locator("#asset-name").inner_text() == "crate"
            assert "Tier 1.5 score" in frame.locator("#qa").inner_text()
            assert frame.locator("#btn-export").is_hidden() and frame.locator("#btn-ask").is_visible()  # gated on host capabilities
            settle(app)
            canvas_png = tmp_path / "canvas.png"
            canvas_png.write_bytes(frame.locator("#gl").screenshot())
            with Image.open(canvas_png) as im:
                assert len(im.convert("RGB").getcolors(1 << 20)) > 50, "canvas looks empty"
            # inline auto-resize: the frame reported its size and stays within the 800 px cap
            assert page.evaluate("__hostLog.sizes.length") > 0 and 300 <= page.locator("#app").bounding_box()["height"] <= 800

            # the reviewer clicks a node in the viewport -> selection + model context
            pt = app.evaluate("window.__nodeScreenPoint('body')")
            box = frame.locator("#gl").bounding_box()
            frame_box = page.locator("#app").bounding_box()
            page.mouse.click(frame_box["x"] + pt["x"], frame_box["y"] + pt["y"])
            assert frame.locator('#nodes tbody tr[data-node="body"]').get_attribute("aria-selected") == "true", box
            wait_for(lambda: page.evaluate("__hostLog.context.length") >= 1, "updateModelContext after selection", page)
            ctx = page.evaluate("__hostLog.context.at(-1)")
            assert 'Reviewer selected node "body"' in ctx["content"][0]["text"]
            # ... writes a note -> saved through the server tool, then pushed to the model context
            frame.locator("#note-text").fill("chamfer the edges")
            wait_for(lambda: any(n == "inspector_save_note" for n, _ in server.calls), "inspector_save_note", page)
            saved = [a for n, a in server.calls if n == "inspector_save_note"][-1]
            assert saved == {"session_id": "Crate-abc123", "node_id": "body", "text": "chamfer the edges", "revision": rev1[0]["revision"]}
            wait_for(lambda: "chamfer the edges" in page.evaluate("__hostLog.context.at(-1).content[0].text"), "context with the note", page)
            assert "Saved" in frame.locator("#notes-status").inner_text()
            frame.locator("#btn-ask").click()
            wait_for(lambda: page.evaluate("__hostLog.messages.length") == 1, "ui/message", page)

            # a second tool result (the agent calls open_inspector again) drives the open view
            before = len(server.calls)
            page.evaluate("r => sendTool({node: 'ball', channel: 'albedo'}, r)",
                          _summary(server, {"node": "ball", "channel": "albedo", "preset": "top", "wireframe": True}))
            wait_for(lambda: frame.locator("body").get_attribute("data-channel") == "albedo", "channel switch", page)
            assert frame.locator('#nodes tbody tr[data-node="ball"]').get_attribute("aria-selected") == "true"
            assert frame.locator('[data-preset="top"]').get_attribute("aria-pressed") == "true"
            assert frame.locator("#btn-wire").get_attribute("aria-pressed") == "true"
            assert not any(n == "inspector_payload" and a["part"] == "scene" for n, a in server.calls[before:])  # same revision: no refetch
            settle(app)
            page.screenshot(path=str(shots / "inline-1000-light-driven.png"))

            # a geometry edit while a note is still in its debounce window: the pending edit is saved against the
            # revision (and session) it was typed under, never against what is loaded when the timer would fire
            frame.locator('#nodes tbody tr[data-node="body"]').click()
            app.evaluate("() => { window.__inspectorHost.saveDelayMs = 60000; }")
            saves_before = sum(1 for n, _ in server.calls if n == "inspector_save_note")
            frame.locator("#note-text").fill("chamfer the edges, all of them")
            assert sum(1 for n, _ in server.calls if n == "inspector_save_note") == saves_before  # still pending
            server.current = rev2[0]["revision"]
            page.evaluate("r => sendTool({}, r)", _summary(server))
            wait_for(lambda: frame.locator("#nodes tbody tr").count() == 2, "new revision", page)
            wait_for(lambda: sum(1 for n, _ in server.calls if n == "inspector_save_note") == saves_before + 1, "flushed save", page)
            pending = [a for n, a in server.calls if n == "inspector_save_note"][-1]
            assert pending == {"session_id": "Crate-abc123", "node_id": "body", "text": "chamfer the edges, all of them",
                               "revision": rev1[0]["revision"]}, pending
            assert {a["revision"] for n, a in server.calls if n == "inspector_payload" and a["part"] == "scene"} == {rev1[0]["revision"], rev2[0]["revision"]}
            wait_for(lambda: frame.locator('#note-list li[data-stale="true"]').count() == 1, "stale note", page)
            assert "stale" in frame.locator("#note-list").inner_text()

            # the same for a switch to another asset session: the pending edit stays with its own session
            frame.locator('#nodes tbody tr[data-node="pole"]').click()
            frame.locator("#note-text").fill("pole is too thin")
            saves_before = sum(1 for n, _ in server.calls if n == "inspector_save_note")
            server.current = rev3[0]["revision"]
            page.evaluate("r => sendTool({}, r)", _summary(server))
            wait_for(lambda: frame.locator("#asset-name").inner_text() == "other crate", "other session", page)
            wait_for(lambda: sum(1 for n, _ in server.calls if n == "inspector_save_note") == saves_before + 1, "flushed save", page)
            pending = [a for n, a in server.calls if n == "inspector_save_note"][-1]
            assert pending == {"session_id": "Crate-abc123", "node_id": "pole", "text": "pole is too thin",
                               "revision": rev2[0]["revision"]}, pending
            app.evaluate("() => { window.__inspectorHost.saveDelayMs = 400; }")
            settle(app)
            assert not [r for r in requests if not r.startswith(("data:", "blob:")) and r not in (host_url, app_url) and not r.endswith(("/host.bundle.js", "/probe.png"))], requests
            assert not problems, problems
            page.close()

            # ---- screenshots: side-pane width and wide, light and dark; live theme change
            for mode, width, height in (("pane", 380, 820), ("inline", 1000, 900), ("pane", 1280, 800)):
                for theme in ("light", "dark"):
                    page, frame, app, server, problems, requests = open_host(browser, mode, width, height, theme)
                    settle(app)
                    page.wait_for_timeout(300)
                    assert app.evaluate("document.documentElement.dataset.theme") == theme
                    assert app.evaluate("document.documentElement.dataset.layout") == ("inline" if mode == "inline" else "fill")
                    bg = app.evaluate("getComputedStyle(document.body).backgroundColor")
                    assert bg == ("rgb(19, 20, 22)" if theme == "dark" else "rgb(247, 247, 248)"), bg  # host variables applied
                    if width < 720:  # narrow: details become tabs below the viewport
                        assert frame.locator(".tabs").is_visible()
                        frame.locator('.tab[data-tab="nodes"]').click()
                        assert frame.locator("#nodes").is_visible()
                        frame.locator('.tab[data-tab="budget"]').click()
                        scroll_w = app.evaluate("document.documentElement.scrollWidth")
                        assert scroll_w <= width, f"horizontal overflow at {width}px: {scroll_w}"
                    shot = shots / f"canvas-{mode}-{width}-{theme}.png"
                    shot.write_bytes(frame.locator("#gl").screenshot())
                    with Image.open(shot) as im:
                        geo = app.evaluate("(() => { const c = document.getElementById('gl'); return [c.width, c.height, c.clientWidth, c.clientHeight]; })()")
                        assert len(im.convert("RGB").getcolors(1 << 20)) > 50, f"empty viewport at {mode} {width}px {theme}: {geo}"
                    page.screenshot(path=str(shots / f"{mode}-{width}-{theme}.png"))
                    if theme == "light" and mode == "pane" and width == 380:
                        page.evaluate("v => setTheme('dark', v)", DARK_VARS)
                        wait_for(lambda: app.evaluate("() => document.documentElement.dataset.theme") == "dark", "theme change", page)
                        assert app.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(19, 20, 22)"
                    assert not problems, (mode, width, theme, problems)
                    page.close()
        finally:
            browser.close()
            app_srv.shutdown()
            host_srv.shutdown()


@pytest.mark.browser
def test_textured_asset_renders_the_glb_materials(tmp_path, scene):
    """Materials made the way the package makes its GLB materials (texture files embedded as they are, one ORM
    texture for occlusion and metallic-roughness) arrive in the page as GLTFLoader materials with the same maps,
    factors and texels; the albedo channel shows the albedo file's colour."""
    import os

    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from antigravity3d.artifacts.inspector import bundled_three_js
    from antigravity3d.kernel.gltf_writer import EncodedImage, GltfMaterial, TextureRef

    meshes, uvs, *_ = scene
    files = {}
    for name, rgb in (("wood_albedo", (180, 40, 30)), ("wood_orm", (255, 200, 0)), ("wood_normal", (128, 128, 255)),
                      ("iron_albedo", (40, 60, 200))):
        files[name] = tmp_path / f"{name}.png"
        Image.new("RGB", (16, 8), rgb).save(files[name])
    wood = GltfMaterial(name="wood", base_color_factor=(1.0, 1.0, 1.0, 1.0),
                        base_color_texture=TextureRef(EncodedImage.from_file(files["wood_albedo"])),
                        metallic_roughness_texture=TextureRef(EncodedImage.from_file(files["wood_orm"])),
                        occlusion_texture=TextureRef(EncodedImage.from_file(files["wood_orm"])),
                        normal_texture=TextureRef(EncodedImage.from_file(files["wood_normal"])))
    iron = GltfMaterial(name="iron", base_color_factor=(1.0, 1.0, 1.0, 1.0), metallic_factor=0.9, roughness_factor=0.3,
                        base_color_texture=TextureRef(EncodedImage.from_file(files["iron_albedo"])))
    html = build_inspector(meshes, asset_name="crate", budget=EngineBudget("godot_4"), uvs=uvs,
                           materials={"body": wood, "pole": wood, "ball": iron}, three_js_source=bundled_three_js(),
                           out_path=tmp_path / "textured.html")
    assert "wood_albedo" not in html.read_text()[:2000]  # (images live inside the GLB, not as page assets)
    g, _, _ = _glb(_payload(html.read_text()))
    assert len(g.images) == 4 and len(g.materials) == 2  # ORM file embedded once for two slots; wood shared by 2 nodes

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_BIN"),
                                         args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                pytest.skip("no Chromium for this Playwright; set CHROMIUM_BIN")
            raise
        try:
            page = browser.new_context(offline=True).new_page()
            page.set_viewport_size({"width": 1024, "height": 768})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(html.as_uri())
            page.wait_for_function("window.__inspectorReady || window.__inspectorError")
            assert page.evaluate("window.__inspectorError") is None
            page.wait_for_function("window.__inspectorMeshes().every(m => !m.material.map || m.material.mapSize)")
            loaded = {m["node"]: m for m in page.evaluate("window.__inspectorMeshes()")}
            for node, mat in (("body", wood), ("pole", wood), ("ball", iron)):
                got = loaded[node]["material"]
                assert got["type"] == "MeshStandardMaterial" and got["name"] == mat.name
                assert got["map"] and got["mapSize"] == [16, 8]
                assert got["normalMap"] == (mat.normal_texture is not None)
                assert got["aoMap"] == got["roughnessMap"] == (mat.metallic_roughness_texture is not None)
                assert got["metalness"] == pytest.approx(mat.metallic_factor) and got["roughness"] == pytest.approx(mat.roughness_factor)
                assert got["color"] == "ffffff" and loaded[node]["normals"]
            # the albedo channel shows the file's texels: the body's pixels are the wood albedo colour
            page.locator('button[data-mode="albedo"]').click()
            page.locator('[data-preset="front"]').click()
            page.evaluate("new Promise(ok => requestAnimationFrame(() => requestAnimationFrame(ok)))")
            pt = page.evaluate("window.__nodeScreenPoint('body')")
            shot = tmp_path / "albedo.png"
            page.screenshot(path=str(shot), clip={"x": pt["x"] - 2, "y": pt["y"] - 2, "width": 4, "height": 4})
            with Image.open(shot) as im:
                r, g_, b = im.convert("RGB").getpixel((2, 2))
            assert abs(r - 180) <= 3 and abs(g_ - 40) <= 3 and abs(b - 30) <= 3, (r, g_, b)
            assert not errors, errors
        finally:
            browser.close()


def test_a_part_absent_at_a_lod_level_gets_no_node_there(scene):
    """The LOD generator drops parts that cannot be seen at a distance: such a part has an empty mesh at that level.
    The review GLB leaves it out of that level (as the package's LOD GLB does) and keeps it at LOD0."""
    from types import SimpleNamespace

    from antigravity3d.artifacts.inspector import inspector_payload

    meshes, uvs, tex, _lods, cols = scene
    empty = SimpleNamespace(points=np.zeros((0, 3)), triangles=np.zeros((0, 3), np.int32), uvs=np.zeros((0, 2), np.float32))
    chains = {m.node_id: [SimpleNamespace(points=m.points, triangles=m.triangles, uvs=uvs[m.node_id])] * 2 for m in meshes}
    chains["ball"] = [chains["ball"][0], empty]  # the ball is absent at LOD1
    payload, glb = inspector_payload(meshes, asset_name="crate", budget=EngineBudget("unreal_5"), materials=tex, uvs=uvs,
                                     lods=chains, colliders=cols)
    lod0, lod1 = payload["levels"]
    assert [p["node"] for p in lod0["parts"]] == ["body", "pole", "ball"]
    assert [p["node"] for p in lod1["parts"]] == ["body", "pole"]
    assert lod1["tris"] == lod0["tris"] - len(meshes[2].triangles)
    import pygltflib

    g = pygltflib.GLTF2.load_from_bytes(glb)
    names = [n.name for n in g.nodes]
    assert "LOD0:ball" in names and "LOD1:ball" not in names and "LOD1:body" in names
    assert all(n.mesh is not None for n in g.nodes)  # no empty node was written
