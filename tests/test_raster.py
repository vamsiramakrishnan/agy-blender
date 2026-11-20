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
# recorded from the pre-refactor implementation, then re-recorded once when coverage became exact: 26 pixels of the
# 12 renders without wireframe changed triangle (49 colour pixels over all 24 renders). In
# every one the new triangle covers the pixel centre by exact evaluation and is the nearest that does; the old
# one either did not cover the centre (20 pixels, admitted by the old tolerance or seen through a hole) or was a
# farther triangle shown because the near one had been rejected (4 pixels); 2 pixels were holes.
CLAY_BASELINE = "70739816d6d78b5503db1db0aaf6d4d9b0f478ea2e37e55bc3a6e82be40192ef"


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


# --------------------------------------------------------------------------- watertight coverage
#
# The coverage rule (raster._rasterise): a pixel centre is covered iff it lies strictly inside the triangle, or on
# its boundary with the tie rule giving it to that triangle; edge signs are exact. The references below are written
# independently of the implementation: Fraction arithmetic for arbitrary float coordinates, int64 arithmetic for
# coordinates on a dyadic grid.

def _sheet(n: int, wavy: bool, rot: np.ndarray) -> MeshData:
    """(n x n quads, 2 n**2 triangles) unit sheet, flat or wavy, rotated by ``rot``."""
    xs, ys = np.meshgrid(np.linspace(0, 1, n + 1), np.linspace(0, 1, n + 1))
    zs = 0.05 * np.sin(xs * 6) * np.cos(ys * 4) if wavy else np.zeros_like(xs)
    p = np.stack([xs.ravel(), ys.ravel(), zs.ravel()], 1) @ rot.T
    a = (np.arange(n)[None, :] + (n + 1) * np.arange(n)[:, None]).ravel()
    t = np.concatenate([np.stack([a, a + 1, a + n + 2], 1), np.stack([a, a + n + 2, a + n + 1], 1)])
    return MeshData("sheet", "/sheet", p, t.astype(np.int32), "m")


def _holes(mask: np.ndarray) -> int:
    from scipy import ndimage

    return int((ndimage.binary_fill_holes(mask) & ~mask).sum())


def _one(tri_xy: np.ndarray, size: int) -> np.ndarray:
    """Coverage mask of one triangle alone."""
    from antigravity3d.kernel.raster import _rasterise

    return _rasterise(np.asarray(tri_xy, np.float64), np.zeros(3, np.float32), np.array([[0, 1, 2]]), size)[0] >= 0


def _fraction_classes(tri_xy: np.ndarray, size: int) -> np.ndarray:
    """Per pixel centre: 1 strictly inside the triangle, 0 strictly outside, -1 exactly on an edge line segment
    of it (Fraction arithmetic on the exact float values)."""
    from fractions import Fraction

    v = [(Fraction(float(x)), Fraction(float(y))) for x, y in tri_xy]
    out = np.zeros((size, size), np.int8)
    xs, ys = [p[0] for p in v], [p[1] for p in v]
    if (v[1][0] - v[0][0]) * (v[2][1] - v[0][1]) - (v[1][1] - v[0][1]) * (v[2][0] - v[0][0]) == 0:
        return out
    lo_x, hi_x = max(int(min(xs)) - 1, 0), min(int(max(xs)) + 1, size - 1)
    lo_y, hi_y = max(int(min(ys)) - 1, 0), min(int(max(ys)) + 1, size - 1)
    for j in range(lo_y, hi_y + 1):
        for i in range(lo_x, hi_x + 1):
            px, py = Fraction(2 * i + 1, 2), Fraction(2 * j + 1, 2)
            e = [(v[(k + 1) % 3][0] - v[k][0]) * (py - v[k][1]) - (v[(k + 1) % 3][1] - v[k][1]) * (px - v[k][0]) for k in range(3)]
            if all(x > 0 for x in e) or all(x < 0 for x in e):
                out[j, i] = 1
            elif all(x >= 0 for x in e) or all(x <= 0 for x in e):
                out[j, i] = -1
    return out


def test_coverage_equals_exact_evaluation_on_random_triangles():
    """Arbitrary float64 vertices, including slivers, vertices far outside the frame and small triangles far from
    the frame origin (where absolute-coordinate float32 evaluation loses most of its precision): coverage is the
    exact point-in-triangle answer at every pixel centre not exactly on an edge."""
    rng = np.random.default_rng(20261120)
    checked = 0
    for k in range(90):
        size = int(rng.choice([24, 48, 1024]))
        if size == 1024:  # a small triangle near the far corner of a large frame
            c = rng.uniform(900, 1010, 2)
            tri = c + rng.normal(0, rng.choice([0.7, 2.0, 6.0]), (3, 2))
        elif k % 9 == 0:  # one vertex far outside the frame
            tri = rng.uniform(-4, size + 4, (3, 2))
            tri[0] = rng.choice([-1, 1], 2) * rng.uniform(1e3, 1e5, 2)
        elif k % 9 == 1:  # sliver: third vertex very close to the line through the other two
            a, b = rng.uniform(0, size, (2, 2))
            tri = np.stack([a, b, a + rng.uniform(0.2, 0.8) * (b - a) + rng.normal(0, 1e-3, 2)])
        else:
            tri = rng.uniform(-4, size + 4, (3, 2))
        want = _fraction_classes(tri, size)
        got = _one(tri, size)
        decided = want >= 0
        assert np.array_equal(got[decided], want[decided] == 1), (k, size, tri.tolist())
        checked += int(decided.sum())
    assert checked > 20000


def _grid_triangulation(rng, size: int, n: int, step: float):
    """Delaunay triangulation of random points with coordinates on a grid of ``step`` px (a dyadic step puts many
    pixel centres exactly on edges and vertices), plus the exact hull test of every pixel centre."""
    from scipy.spatial import Delaunay

    pts = np.unique(np.round(rng.uniform(-3, size + 3, (n, 2)) / step) * step, axis=0)
    d = Delaunay(pts)
    tris = d.simplices
    # pixel centres strictly inside / on the boundary of the convex hull, exactly (int64 on the 1/(2 step) grid)
    q = int(round(1 / step)) * 2
    hp = np.round(pts[d.convex_hull] * q).astype(np.int64)                  # hull edges (H, 2, 2), unordered
    c = np.round(pts.mean(axis=0) * q).astype(np.int64)
    gy, gx = np.mgrid[0:size, 0:size]
    P = np.stack([(2 * gx + 1) * q // 2, (2 * gy + 1) * q // 2], -1).reshape(-1, 1, 2).astype(np.int64)
    u, v = hp[:, 0][None], hp[:, 1][None]
    side = (v[..., 0] - u[..., 0]) * (P[..., 1] - u[..., 1]) - (v[..., 1] - u[..., 1]) * (P[..., 0] - u[..., 0])
    inner = (v[..., 0] - u[..., 0]) * (c[1] - u[..., 1]) - (v[..., 1] - u[..., 1]) * (c[0] - u[..., 0])
    s = side * np.sign(inner)                                               # > 0: on the hull's inner side
    strictly_in = (s > 0).all(axis=1).reshape(size, size)
    strictly_out = (s < 0).any(axis=1).reshape(size, size)
    return pts, tris, strictly_in, strictly_out


def test_triangulation_covers_every_pixel_centre_exactly_once():
    """A triangulation tiles its convex hull: each pixel centre strictly inside the hull is covered by exactly one
    triangle, including centres exactly on shared edges and on vertices where a fan meets (coarse grids put many
    there), and no centre strictly outside is covered. Each triangle is rasterised alone so double cover shows."""
    rng = np.random.default_rng(11)
    for size, n, step in ((32, 60, 0.5), (48, 200, 0.25), (64, 400, 1.0), (512, 3000, 2.0 ** -20)):
        pts, tris, strictly_in, strictly_out = _grid_triangulation(rng, size, n, step)
        count = np.zeros((size, size), np.int32)
        for t in tris:
            count += _one(pts[t], size)
        assert (count[strictly_in] == 1).all(), (size, int((count[strictly_in] == 0).sum()), int((count[strictly_in] > 1).sum()))
        assert (count[strictly_out] == 0).all() and (count <= 1).all()
        # the same triangulation in one call: the union is exactly the hull interior plus boundary centres it owns
        from antigravity3d.kernel.raster import _rasterise

        img = _rasterise(pts, np.zeros(len(pts), np.float32), tris, size)[0]
        assert np.array_equal(img >= 0, count > 0)


def test_dense_open_sheets_have_no_holes():
    """Flat and wavy open sheets (1k to 50k triangles), random rotations, every view, 64 to 1024 px: the silhouette
    mask equals its hole-filled self. Single-pixel holes between adjacent triangles used to appear by the hundred
    at 702 and 1024 px."""
    from scipy.spatial.transform import Rotation

    rng = np.random.default_rng(5)
    views = ("front", "back", "left", "right", "top", "persp")
    for n, sizes, vs in ((23, (64, 256), views), (50, (702,), views), (100, (1024,), views), (158, (1024,), ("top", "persp"))):
        for wavy in (False, True):
            m = _sheet(n, wavy, Rotation.random(random_state=rng).as_matrix())
            for view in vs:
                for size in sizes:
                    mask = render_buffers([m], view, size).prim_id >= 0
                    assert mask.any() and _holes(mask) == 0, (n, wavy, view, size, _holes(mask))


def test_pop_gate_sees_identical_silhouettes_for_identical_geometry():
    """The LOD pop gate's own cameras (a 702 px window of the 1080 px frame at 50 %, persp and ortho): a dense flat
    sheet and the two triangles covering the same rectangle render the same mask, so the silhouette distance is 0
    (it used to read single-pixel holes as a ~95 px silhouette jump)."""
    from types import SimpleNamespace

    from antigravity3d.kernel.lod_diff import render_lod_pair

    dense = _sheet(40, False, np.eye(3))
    quad = SimpleNamespace(points=np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float), triangles=np.array([[0, 1, 2], [0, 2, 3]]))
    for view in ("persp", "top", "front"):
        p = render_lod_pair(dense, quad, screen_fraction=0.5, view=view, frame_px=1080, render_px=702)
        if view == "front":  # edge-on: both are a line
            assert p.boundary_px <= 1.0
            continue
        assert p.mask_a.sum() > 10000 and _holes(p.mask_a) == 0
        assert np.array_equal(p.mask_a, p.mask_b) and p.boundary_px == 0.0 and p.iou == 1.0


def test_closed_meshes_never_show_back_faces():
    """On a closed, outward-oriented mesh every covered pixel centre is covered by a front-facing triangle, and the
    nearest surface there is front-facing: the triangle-id buffer must never show a back face (a hole between two
    front triangles used to show the far side of the mesh)."""
    import trimesh

    meshes = [md(trimesh.creation.icosphere(5, 0.5), "sphere"), md(trimesh.creation.torus(0.6, 0.15, major_sections=128, minor_sections=48), "torus")]
    for m in meshes:
        p, t = m.points, m.triangles
        for view in ("front", "top", "persp"):
            for size in (256, 702, 1024):
                rb = render_buffers([m], view, size)
                sxy, _ = rb.camera.project(p)
                a, b, c = sxy[t[:, 0]], sxy[t[:, 1]], sxy[t[:, 2]]
                orient = np.sign((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))
                n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
                eye = rb.camera.center - rb.camera.basis[2] * rb.camera.dist if rb.camera.persp else None
                toward = (eye - p[t].mean(axis=1)) if rb.camera.persp else -rb.camera.basis[2][None]
                facing = (n * toward).sum(axis=1) / np.linalg.norm(n, axis=1) / np.linalg.norm(toward, axis=1)
                front = np.sign(np.median(orient[facing > 0.5]))
                assert (orient[facing > 0.5] == front).all() and (orient[facing < -0.5] == -front).all()
                shown = rb.tri_id[rb.tri_id >= 0]
                assert (orient[shown] == front).all(), (m.node_id, view, size, int((orient[shown] != front).sum()))


def test_identical_input_gives_identical_buffers():
    import trimesh

    ms = scene_5k() + [md(trimesh.creation.torus(0.6, 0.15), "t", (0, -2, 0.5))]
    for v in ("persp", "top"):
        a, b = render_buffers(ms, v, 300, wireframe=True), render_buffers(ms, v, 300, wireframe=True)
        assert np.array_equal(a.color, b.color) and np.array_equal(a.tri_id, b.tri_id) and np.array_equal(a.prim_id, b.prim_id)
        assert np.array_equal(a.depth, b.depth)


# --------------------------------------------------------------------------- shading with normals


def _corner(normals_per_triangle: np.ndarray) -> np.ndarray:
    return np.repeat(normals_per_triangle[:, None, :], 3, axis=1)


def test_corner_normals_equal_to_the_face_normal_give_the_flat_picture():
    # the per-pixel path with every corner at its face normal must be the flat picture, pixel for pixel
    tm = trimesh.creation.icosphere(subdivisions=2, radius=0.5)
    m = MeshData("ball", "/ball", np.asarray(tm.vertices, np.float64), np.asarray(tm.faces, np.int32))
    p = m.points[m.triangles]
    fn = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    fn /= np.linalg.norm(fn, axis=1, keepdims=True)
    for view in ("front", "persp"):
        flat = render_buffers([m], view, 192)
        lit = render_buffers([m], view, 192, normals={"ball": _corner(fn)})
        assert np.array_equal(flat.color, lit.color) and np.array_equal(flat.tri_id, lit.tri_id), view


def test_shading_normals_light_a_coarse_sphere_like_a_fine_one():
    # with the surface's own normals at the corners, a 320-triangle sphere and a 5,120-triangle sphere are lit almost
    # alike; with one normal per triangle the coarse one shows its facets
    def ball(sub: int):
        tm = trimesh.creation.icosphere(subdivisions=sub, radius=0.5)
        m = MeshData("ball", "/ball", np.asarray(tm.vertices, np.float64), np.asarray(tm.faces, np.int32))
        radial = m.points / np.linalg.norm(m.points, axis=1, keepdims=True)
        return m, radial[m.triangles]

    (coarse, cn), (fine, fn) = ball(2), ball(4)
    cam = render_buffers([fine], "persp", 256).camera

    def diff(a, b):
        both = (a.prim_id >= 0) & (b.prim_id >= 0)
        return float(np.abs(a.color.astype(np.float32) - b.color.astype(np.float32)).mean(axis=2)[both].mean())

    flat = diff(render_buffers([coarse], "persp", 256, camera=cam), render_buffers([fine], "persp", 256, camera=cam))
    smooth = diff(render_buffers([coarse], "persp", 256, camera=cam, normals={"ball": cn}),
                  render_buffers([fine], "persp", 256, camera=cam, normals={"ball": fn}))
    assert smooth < 0.5 * flat, (smooth, flat)


def test_only_the_meshes_that_come_with_normals_are_lit_per_pixel():
    a = MeshData("a", "/a", *(lambda t: (np.asarray(t.vertices, np.float64), np.asarray(t.faces, np.int32)))(trimesh.creation.icosphere(subdivisions=2, radius=0.4)))
    t = trimesh.creation.icosphere(subdivisions=2, radius=0.4)
    b = MeshData("b", "/b", np.asarray(t.vertices, np.float64) + [1.2, 0, 0], np.asarray(t.faces, np.int32))
    radial = a.points / np.linalg.norm(a.points, axis=1, keepdims=True)
    flat = render_buffers([a, b], "front", 256)
    mixed = render_buffers([a, b], "front", 256, normals={"a": radial[a.triangles]})
    on_a, on_b = flat.prim_id == 0, flat.prim_id == 1
    assert np.array_equal(flat.color[on_b], mixed.color[on_b])        # b has no normals: untouched
    assert not np.array_equal(flat.color[on_a], mixed.color[on_a])    # a is lit with its own
