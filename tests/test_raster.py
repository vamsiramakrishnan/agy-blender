from __future__ import annotations

import time

import numpy as np
import trimesh

from antigravity3d.kernel.raster import BG, contact_sheet, render_clay
from antigravity3d.kernel.types import MeshData


def md(tm, nid, t=(0, 0, 0)) -> MeshData:
    tm = tm.copy()
    tm.apply_translation(t)
    return MeshData(nid, f"/A/{nid}", np.array(tm.vertices, float), np.array(tm.faces, np.int32), "m")


def scene_5k() -> list[MeshData]:
    return [
        md(trimesh.creation.box((1, 1, 1)), "box", (0, 0, 0.5)),
        md(trimesh.creation.icosphere(4, 0.5), "sph", (1.5, 0, 0.5)),
        md(trimesh.creation.cylinder(0.3, 1.5, sections=64), "cyl", (-1.5, 0, 0.75)),
    ]


def covered(img):
    return (img != BG).any(axis=2)


def test_views_shapes_and_content():
    ms = scene_5k()
    for v in ("front", "back", "left", "right", "top", "persp"):
        img = render_clay(ms, v, 128)
        assert img.shape == (128, 128, 3) and img.dtype == np.uint8
        frac = covered(img).mean()
        assert 0.05 < frac < 0.95, (v, frac)


def test_orientation_and_depth_order():
    ms = scene_5k()
    front = covered(render_clay(ms, "front", 128))
    cols = np.nonzero(front.any(axis=0))[0]
    assert cols.min() < 40 and cols.max() > 88  # cylinder left (-x), sphere right (+x)
    # right view (camera at +X): the sphere at x=1.5 must occlude the box: centre pixel is sphere-shaded, not flat
    img = render_clay(ms, "right", 128)
    patch = img[50:78, 50:78].reshape(-1, 3).astype(int)
    assert patch.std(axis=0).max() > 3  # curved shading varies, a flat box face would not
    top = render_clay([md(trimesh.creation.box((1, 1, 1)), "b", (0, 0, 0.5)), md(trimesh.creation.box((0.5, 0.5, 0.5)), "c", (0, 0, 1.25))], "top", 64)
    assert covered(top).any()


def test_wireframe_changes_pixels_and_sheet():
    ms = scene_5k()[:1]
    a, b = render_clay(ms, "persp", 128), render_clay(ms, "persp", 128, wireframe=True)
    assert (a != b).any() and covered(b).sum() == covered(a).sum()
    sheet = contact_sheet(scene_5k(), size=128)
    assert sheet.size == (256, 256) and sheet.mode == "RGB"
    arr = np.asarray(sheet)
    for q in ((0, 0), (0, 128), (128, 0), (128, 128)):
        assert covered(arr[q[0]:q[0] + 128, q[1]:q[1] + 128]).mean() > 0.05
    assert contact_sheet(scene_5k(), views=("front", "back", "left"), size=64).size == (128, 128)


def test_empty_scene():
    assert (render_clay([], "front", 32) == BG).all()


def test_timing():
    ms = scene_5k()
    n = sum(len(m.triangles) for m in ms)
    contact_sheet(ms)
    t = time.perf_counter()
    contact_sheet(ms, size=256)
    sheet_ms = (time.perf_counter() - t) * 1e3
    prop = [md(trimesh.creation.icosphere(2, 0.5), "p")]
    render_clay(prop, "persp", 256)
    t = time.perf_counter()
    render_clay(prop, "persp", 256)
    prop_ms = (time.perf_counter() - t) * 1e3
    print(f"\nraster: {n}-tri scene 4-view sheet {sheet_ms:.0f} ms; {len(prop[0].triangles)}-tri prop single view {prop_ms:.1f} ms")
    assert sheet_ms < 1000 and prop_ms < 100
