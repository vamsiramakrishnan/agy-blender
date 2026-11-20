from __future__ import annotations

import time

import numpy as np
import trimesh

from antigravity3d.kernel.raster import BG, contact_sheet, render_buffers, render_clay
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


# --------------------------------------------------------------------------- render_buffers

# sha256 over render_clay of scene_5k()+prop for all views x wireframe x sizes (128, 256),
# recorded from the pre-refactor implementation
CLAY_BASELINE = "467283949de696c9f801ce45ed31cac4ebed2bac81970328ef4c86a3d9f72396"


def _baseline_scene():
    return scene_5k() + [md(trimesh.creation.icosphere(2, 0.5), "p", (0, 2, 0.5))]


def test_render_clay_byte_identical_to_pre_refactor():
    import hashlib

    h = hashlib.sha256()
    ms = _baseline_scene()
    for v in ("front", "back", "left", "right", "top", "persp"):
        for wf in (False, True):
            for s in (128, 256):
                h.update(render_clay(ms, v, s, wf).tobytes())
                assert (render_buffers(ms, v, s, wireframe=wf).color == render_clay(ms, v, s, wf)).all()
    assert h.hexdigest() == CLAY_BASELINE


def test_render_buffers_ids_depth_camera():
    ms = scene_5k()
    for v in ("front", "top", "persp"):
        rb = render_buffers(ms, v, 128)
        assert rb.prim_id.dtype == np.int32 and rb.depth.dtype == np.float32 and rb.prim_id.shape == (128, 128)
        bg = rb.prim_id < 0
        assert (bg == (rb.color == BG).all(axis=2)).all() and np.isinf(rb.depth[bg]).all() and np.isfinite(rb.depth[~bg]).all()
        assert set(np.unique(rb.prim_id[~bg])) == {0, 1, 2}
        # projecting each mesh's centre lands on a pixel of that mesh (front/top: nothing occludes the centres)
        if v != "persp":
            for i, m in enumerate(ms):
                x, y = rb.camera.project(m.points.mean(axis=0, keepdims=True))[0][0]
                assert rb.prim_id[int(y), int(x)] == i
        assert (rb.tri_id[~bg] >= 0).all() and (rb.tri_id[~bg] < max(len(m.triangles) for m in ms)).all()
    f = render_buffers(ms, "front", 128)
    assert f.camera.metres_per_pixel(0.0) == f.camera.metres_per_pixel(5.0) > 0
    p = render_buffers(ms, "persp", 128).camera
    assert p.metres_per_pixel(10.0) == 2 * p.metres_per_pixel(5.0)
    # ortho scale is exact: box is 1 m wide in the front view
    cols = np.nonzero((f.prim_id == 0).any(axis=0))[0]
    assert abs((cols.max() - cols.min() + 1) * f.camera.metres_per_pixel(0) - 1.0) < 2 * f.camera.metres_per_pixel(0)


def test_render_buffers_empty_and_explicit_camera_and_uvs():
    rb = render_buffers([], "front", 32)
    assert (rb.prim_id == -1).all() and np.isinf(rb.depth).all() and rb.camera is None
    ms = scene_5k()[:1]
    a = render_buffers(ms, "persp", 64)
    assert (render_buffers(ms, "persp", 64, camera=a.camera).color == a.color).all()
    uv = {"box": np.random.default_rng(0).random((8, 2))}
    c = render_buffers(ms, "persp", 64, uvs=uv).color
    assert (c != a.color).any() and ((c != BG).any(axis=2) == (a.color != BG).any(axis=2)).all()


def test_id_buffer_overhead():
    ms = scene_5k()
    render_clay(ms, "persp", 256)
    t = time.perf_counter()
    for _ in range(5):
        render_clay(ms, "persp", 256)
    a = time.perf_counter() - t
    t = time.perf_counter()
    for _ in range(5):
        render_buffers(ms, "persp", 256)
    b = time.perf_counter() - t
    print(f"\nrender_clay {a * 200:.1f} ms, render_buffers {b * 200:.1f} ms")
    assert b - a < 0.02 * 5 or b < a * 1.5


def test_frames_larger_than_256_px_hold_frame_spanning_triangles():
    """A box face fills most of the frame, so its two triangles span far more than 256 px at 512 and 1024.

    The tile table used to stop at 256 px and such a triangle raised IndexError. Coverage must be resolution
    independent: the same framing gives the same covered fraction at every size, to within edge pixels.
    """
    box = [md(trimesh.creation.box((1, 1, 1)), "box", (0, 0, 0.5))]
    base = render_buffers(box, "front", 256)
    want = (base.prim_id >= 0).mean()
    assert 0.5 < want < 0.9
    for size in (384, 512, 1024):
        r = render_buffers(box, "front", size)
        got = (r.prim_id >= 0).mean()
        assert abs(got - want) < 4.0 / size, (size, got, want)  # a few edge pixels per row at most
        ys, xs = np.nonzero(r.prim_id >= 0)
        assert xs.max() - xs.min() > 256 and ys.max() - ys.min() > 256  # really wider than the old limit
        assert np.isfinite(r.depth[r.prim_id >= 0]).all()
    # a mixed scene (thousands of small triangles and large ones) renders at 512 and agrees with 256 on coverage
    ms = scene_5k()
    a, b = render_buffers(ms, "persp", 256), render_buffers(ms, "persp", 512)
    assert abs((a.prim_id >= 0).mean() - (b.prim_id >= 0).mean()) < 0.01


def test_tile_batches_do_not_change_the_image(monkeypatch):
    """Splitting a tile size's triangles into batches is only a memory bound: the image is identical."""
    from antigravity3d.kernel import raster

    ms = scene_5k()
    whole = render_buffers(ms, "persp", 256)
    monkeypatch.setattr(raster, "_TILE_BATCH_ELEMENTS", 4096)  # forces many batches per tile size
    split = render_buffers(ms, "persp", 256)
    assert np.array_equal(whole.color, split.color) and np.array_equal(whole.prim_id, split.prim_id)
    assert np.array_equal(whole.depth, split.depth)
