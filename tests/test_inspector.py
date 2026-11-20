from __future__ import annotations

import base64
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
    InspectorTextures,
    ReviewNote,
    build_inspector,
    read_notes,
)
from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget


def _png(rgb: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), rgb).save(buf, "PNG")
    return buf.getvalue()


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
    tex = {k: InspectorTextures(_png((200, 10, 10)), _png((255, 128, 0)), _png((128, 128, 255))) for k in ("m_a", "m_b")}
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
        textures=tex, lods=lods, colliders=cols, uvs=uvs, out_path=tmp_path / "crate.html",
    )
    args.update(kw)
    return build_inspector(meshes, **args)


def test_round_trip_and_y_up(tmp_path, scene):
    meshes = scene[0]
    html = _build(tmp_path, scene).read_text(encoding="utf-8")
    p = _payload(html)
    assert [m["id"] for m in p["meshes"]] == ["body", "pole", "ball"]
    for m, src in zip(p["meshes"], meshes, strict=True):
        pos = np.frombuffer(base64.b64decode(m["pos"]), "<f4").reshape(-1, 3)
        idx = np.frombuffer(base64.b64decode(m["idx"]), "<u4").reshape(-1, 3)
        assert pos.shape == src.points.shape and idx.shape == src.triangles.shape
        assert m["tris"] == len(src.triangles)
        # Z-up (x, y, z) -> Y-up (x, z, -y)
        np.testing.assert_allclose(pos, np.stack([src.points[:, 0], src.points[:, 2], -src.points[:, 1]], 1), atol=1e-6)
        assert m["uv"] is not None
    body = np.frombuffer(base64.b64decode(p["meshes"][0]["pos"]), "<f4").reshape(-1, 3)
    assert body[:, 1].max() == pytest.approx(3.0)  # 3 m tall box: height is three.js Y
    assert body[:, 1].min() == pytest.approx(0.0)
    assert len(p["lods"]) == 1 and len(p["colliders"]) == 1
    assert set(p["textures"]) == {"m_a", "m_b"}
    assert p["textures"]["m_a"]["albedo"].startswith("data:image/png;base64,")


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
    assert p["textures"] == {} and p["lods"] == [] and p["colliders"] == []
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
