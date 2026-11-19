from __future__ import annotations

import time

import numpy as np
import pytest
import trimesh

from antigravity3d.kernel.spatial import SpatialIndex
from antigravity3d.kernel.types import MeshData


def box_mesh(nid: str, lo, hi, material: str | None = None) -> MeshData:
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    m = trimesh.creation.box(extents=hi - lo)
    m.apply_translation((lo + hi) / 2)
    return MeshData(nid, f"/{nid}", np.asarray(m.vertices, float), np.asarray(m.faces, np.int32), material)


def fast_boxes(n: int, seed: int = 0, extent: float = 100.0):
    """Cheap MeshData (12-tri boxes built with numpy) for large scenes."""
    rng = np.random.default_rng(seed)
    lo = rng.random((n, 3)) * extent
    size = rng.random((n, 3)) * 3 + 0.1
    tri = trimesh.creation.box().faces.astype(np.int32)
    base = trimesh.creation.box(extents=(1, 1, 1)).vertices + 0.5
    meshes = [
        MeshData(f"p{i:05d}", f"/p{i}", base * size[i] + lo[i], tri) for i in range(n)
    ]
    return meshes, lo, lo + size


def brute_region(lo, hi, bmin, bmax):
    return {i for i in range(len(lo)) if np.all(lo[i] <= bmax) and np.all(hi[i] >= bmin)}


def test_region_matches_brute_force():
    meshes, lo, hi = fast_boxes(600, seed=1, extent=40)
    idx = SpatialIndex(meshes, leaf_capacity=4)
    rng = np.random.default_rng(2)
    for _ in range(60):
        a = rng.random(3) * 40
        b = a + rng.random(3) * 15
        r = idx.query_region(a, b, limit=10_000)
        assert r.kind == "prims"
        assert {p.node_id for p in r.prims} == {f"p{i:05d}" for i in brute_region(lo, hi, a, b)}
        assert r.total_prims == len(r.prims)


def test_cells_cover_all_hits_and_drill_down():
    meshes, lo, hi = fast_boxes(800, seed=3, extent=50)
    sem = {m.node_id: ("north/a" if i % 3 else "south/b") for i, m in enumerate(meshes)}
    idx = SpatialIndex(meshes, sem, leaf_capacity=4)
    a, b = np.array([5.0, 5, 5]), np.array([30.0, 30, 30])
    expect = brute_region(lo, hi, a, b)
    for depth in (1, 2, 3):
        r = idx.query_region(a, b, depth=depth)
        assert r.kind == "cells" and r.total_prims == len(expect)
        assert sum(c.n_prims for c in r.cells) == len(expect)  # cells partition the hits
        assert all(c.top_tags for c in r.cells)
    r = idx.query_region(a, b, limit=10)  # too many -> auto cell summaries within limit
    assert r.kind == "cells" and 1 <= len(r.cells) <= 10 and sum(c.n_prims for c in r.cells) == len(expect)
    assert sum(c.tris for c in idx.query_region(a, b, depth=2).cells) == 12 * len(expect)


def test_text_cap_holds_for_10k_and_build_is_fast():
    meshes, lo, hi = fast_boxes(10_000, seed=4)
    sem = {m.node_id: f"zone{i % 7}/room{i % 13}" for i, m in enumerate(meshes)}
    t = time.perf_counter()
    idx = SpatialIndex(meshes, sem)
    build = time.perf_counter() - t
    # bounding-box-only build (the index itself, excluding MeshData construction) must be fast
    assert build < 1.0, build
    everything = (-np.inf,) * 3, (np.inf,) * 3
    for cap in (1200, 300, 80, 10):
        for depth in (None, 1, 4):
            txt = idx.query_region(*everything, depth=depth, limit=10**6 if depth is None and cap == 80 else 50).to_text(cap)
            assert len(txt) <= cap
        assert len(idx.overview(1)) <= 1200
    small = idx.query_region([0, 0, 0], [3, 3, 3], limit=1000).to_text(1200)
    assert len(small) <= 1200 and small == idx.query_region([0, 0, 0], [3, 3, 3], limit=1000).to_text(1200)
    ov = idx.overview(1)
    assert ov.startswith("scene ") and "10000 prims" in ov


def test_text_deterministic_and_informative():
    meshes = [box_mesh("a", (0, 0, 0), (1, 1, 1), "m1"), box_mesh("b", (5, 5, 0), (6, 6, 2))]
    idx = SpatialIndex(meshes, {"a": "house/kitchen"})
    t = idx.query_region((-1, -1, -1), (2, 2, 2)).to_text()
    assert t == idx.query_region((-1, -1, -1), (2, 2, 2)).to_text()
    assert "a " in t and "12t" in t and "m=m1" in t and "house/kitchen" in t and "b " not in t.split("\n", 1)[1]
    assert SpatialIndex([]).overview() == "empty scene"


def test_neighbors_match_brute_force_and_sorted():
    meshes, lo, hi = fast_boxes(500, seed=5, extent=30)
    idx = SpatialIndex(meshes, leaf_capacity=4)
    for q in (0, 17, 250):
        r = idx.query_neighbors(meshes[q].node_id, 4.0, limit=10_000)
        gaps = np.maximum(0, np.maximum(lo - hi[q], lo[q] - hi))
        d = np.sqrt((gaps**2).sum(1))
        exp = sorted((d[i], meshes[i].node_id) for i in range(500) if i != q and d[i] <= 4.0)
        assert [n.node_id for n in r] == [e[1] for e in exp]
        assert [n.distance_m for n in r] == pytest.approx([e[0] for e in exp])
        assert [n.distance_m for n in r] == sorted(n.distance_m for n in r)
    assert len(idx.query_neighbors(meshes[0].node_id, 100.0, limit=3)) == 3


def test_neighbors_use_box_distance_not_centroid():
    big = box_mesh("big", (0, 0, 0), (10, 1, 1))
    near = box_mesh("near", (10.5, 0, 0), (11, 1, 1))  # 0.5 m from big's edge, centroid 5.75 m away
    far = box_mesh("far", (0, 3, 0), (1, 4, 1))  # 2 m gap
    r = SpatialIndex([big, near, far]).query_neighbors("big", 1.0)
    assert [n.node_id for n in r] == ["near"] and r[0].distance_m == pytest.approx(0.5)
    assert SpatialIndex([big, near, far]).query_neighbors("big", 5.0)[1].node_id == "far"
    with pytest.raises(KeyError):
        SpatialIndex([big]).query_neighbors("zzz", 1.0)


def test_raycast_down_known_shape():
    floor = box_mesh("floor", (-5, -5, -0.1), (5, 5, 0))
    table = box_mesh("table", (0, 0, 0), (2, 2, 0.8))
    shelf = box_mesh("shelf", (0, 0, 2.0), (1, 1, 2.1))
    sph = trimesh.creation.icosphere(subdivisions=4, radius=1.0)
    sph.apply_translation((3, 3, 1))
    sphere = MeshData("ball", "/ball", np.asarray(sph.vertices, float), np.asarray(sph.faces, np.int32))
    idx = SpatialIndex([floor, table, shelf, sphere])
    assert idx.raycast_down((-3, -3)) == pytest.approx(0.0)
    assert idx.raycast_down((1.5, 1.5)) == pytest.approx(0.8)
    assert idx.raycast_down((0.5, 0.5)) == pytest.approx(2.1)  # highest surface
    assert idx.raycast_down((0.5, 0.5), z_start=1.5) == pytest.approx(0.8)  # shelf above start ignored
    assert idx.raycast_down((2.0, 2.0)) == pytest.approx(0.8)  # exactly on box edge
    assert idx.raycast_down((3, 3)) == pytest.approx(2.0, abs=0.02)  # sphere top
    assert idx.raycast_down((50, 50)) is None
    assert idx.raycast_down((-3, -3), z_start=-1.0) is None


def test_raycast_only_tests_candidate_prims(monkeypatch):
    import antigravity3d.kernel.spatial as sp

    meshes, _, _ = fast_boxes(300, seed=9, extent=60)
    idx = SpatialIndex(meshes, leaf_capacity=4)
    seen = []
    orig = sp.vertical_hits
    monkeypatch.setattr(sp, "vertical_hits", lambda tri, xy, *a: seen.append(len(tri)) or orig(tri, xy, *a))
    idx.raycast_down((30.0, 30.0))
    assert len(seen) < 10  # candidates by index, not all 300 prims
