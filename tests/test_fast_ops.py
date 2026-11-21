from __future__ import annotations

import time

import numpy as np
import pytest
import trimesh
from scipy.spatial import ConvexHull

from antigravity3d.kernel.fast_ops import (
    UV_CHART_PASS_PX,
    UV_PADDING_PX,
    ExportBundle,
    compress_texture_ktx2,
    generate_colliders,
    generate_lods,
    run_export_pipeline,
    tier1_qa,
    unwrap_scene,
    unwrap_uvs,
    uv_overlap_fraction,
    write_gltf,
    _topology,
    _canon,
    _decomposition_input,
    _merge_planar,
    _point_triangle_distance,
)
from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget

BUDGET = EngineBudget("unreal_5")


def md(tm: trimesh.Trimesh, nid: str, mat: str | None = "m", z: float | None = 0.0, xy=(0, 0), **kw) -> MeshData:
    tm = tm.copy()
    if z is not None:  # rest the mesh on z=z
        tm.apply_translation([xy[0], xy[1], z - tm.bounds[0][2]])
    return MeshData(nid, f"/Asset/{nid}", np.array(tm.vertices, np.float64), np.array(tm.faces, np.int32), mat, **kw)


def codes(rep, sev=None) -> set[str]:
    return {i.code for i in rep.issues if sev is None or i.severity == sev}


def l_shape(size: float = 0.1) -> MeshData:  # single closed concave L-prism
    v = np.array([(0, 0), (1, 0), (1, 0.3), (0.3, 0.3), (0.3, 1), (0, 1)], float)
    tm = trimesh.creation.extrude_triangulation(v, np.array([(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 5)]), 0.3)
    return md(tm.subdivide_to_size(size), "ell")


def l_kitbash(size: float = 0.12) -> MeshData:  # two overlapping closed bodies in one mesh
    a = trimesh.creation.box((1.0, 0.3, 0.3))
    b = trimesh.creation.box((0.3, 0.3, 1.0))
    b.apply_translation([-0.35, 0, 0.6])
    return md(trimesh.util.concatenate([a.subdivide_to_size(size), b.subdivide_to_size(size)]), "kit")


# ------------------------------------------------------------------ QA


def test_clean_props_pass():
    ms = [md(trimesh.creation.box((1, 1, 1)), "box"), md(trimesh.creation.cylinder(0.3, 1), "cyl", xy=(2, 0)),
          md(trimesh.creation.icosphere(3, 0.4), "sph", xy=(0, 2))]
    rep = tier1_qa(ms, BUDGET)
    assert rep.ok and not rep.issues, rep.one_line()
    assert rep.one_line() == "TIER1 OK"
    assert rep.stats["draw_calls"] == 1 and rep.stats["tris"] == sum(len(m.triangles) for m in ms)


def test_qa_speed_typical_prop():
    ms = [md(trimesh.creation.icosphere(3, 0.4), "sph"), md(trimesh.creation.box((1, 1, 1)), "box", xy=(2, 0))]
    tier1_qa(ms, BUDGET)
    t = time.perf_counter()
    for _ in range(10):
        rep = tier1_qa(ms, BUDGET)
    ms_each = (time.perf_counter() - t) * 100
    print(f"\ntier1_qa ~1.3k tris: {ms_each:.2f} ms (internal {rep.stats['ms']} ms)")
    assert ms_each < 20


def test_broken_open_mesh():
    br = trimesh.creation.box((1, 1, 1))
    br.update_faces(np.arange(len(br.faces)) != 0)
    rep = tier1_qa([md(br, "lid")], BUDGET)
    assert not rep.ok and "not_watertight" in codes(rep, "critical")
    assert "not_watertight@lid" in rep.one_line() and rep.one_line().startswith("TIER1 FAIL")
    relaxed = tier1_qa([md(br, "lid")], EngineBudget("unreal_5", require_manifold=False))
    assert relaxed.ok and "not_watertight" in codes(relaxed, "warning")


def test_inverted_and_inconsistent_winding():
    inv = trimesh.creation.box((1, 1, 1))
    inv.invert()
    assert "inverted_normals" in codes(tier1_qa([md(inv, "inv")], BUDGET), "critical")
    mixed = trimesh.creation.box((1, 1, 1))
    f = mixed.faces.copy()
    f[3] = f[3][::-1]
    mixed = trimesh.Trimesh(mixed.vertices, f, process=False)
    assert "inconsistent_winding" in codes(tier1_qa([md(mixed, "mix")], BUDGET), "critical")


def test_degenerate_nonfinite_duplicate_nonmanifold():
    b = trimesh.creation.box((1, 1, 1))
    v, f = b.vertices.copy(), b.faces.copy()
    deg = trimesh.Trimesh(v, np.vstack([f, [[0, 0, 1]]]), process=False)
    assert "degenerate_tris" in codes(tier1_qa([md(deg, "d")], BUDGET), "critical")
    dup = trimesh.Trimesh(v, np.vstack([f, f[:1]]), process=False)
    rep = tier1_qa([md(dup, "dup")], BUDGET)
    assert "duplicate_faces" in codes(rep, "warning")
    nan = md(b, "nan")
    nan.points[0, 0] = np.nan
    assert "non_finite" in codes(tier1_qa([nan], BUDGET), "critical")
    e = (f[0][0], f[0][1])  # an edge already shared by two faces + a third -> non-manifold
    nm = trimesh.Trimesh(np.vstack([v, [[0, 0, 5.0]]]), np.vstack([f, [[e[0], e[1], 8]]]), process=False)
    assert "non_manifold" in codes(tier1_qa([md(nm, "nm", z=None)], BUDGET), "critical")


def test_budgets():
    ms = [md(trimesh.creation.icosphere(4, 0.4), "a", mat="m1", xy=(0, 0)), md(trimesh.creation.icosphere(3, 0.4), "b", mat=None, xy=(2, 0))]
    rep = tier1_qa(ms, EngineBudget("unreal_5", max_tris=5000, max_draw_calls=1))
    assert {"tri_budget", "draw_call_budget"} <= codes(rep, "critical")
    assert "tris 6400>5000" in rep.one_line() and "draws 2>1" in rep.one_line()
    assert len(rep.one_line()) <= 200


def test_grounding_floating_and_interpenetration():
    box = md(trimesh.creation.box((1, 1, 1)), "box")
    assert "ungrounded" in codes(tier1_qa([md(trimesh.creation.box((1, 1, 1)), "hi", z=0.5)], BUDGET), "warning")
    assert "ungrounded" in codes(tier1_qa([md(trimesh.creation.box((1, 1, 1)), "lo", z=-0.3)], BUDGET), "warning")
    assert "ungrounded" not in codes(tier1_qa([md(trimesh.creation.box((1, 1, 1)), "ok", z=0.005)], BUDGET))
    fl = md(trimesh.creation.box((0.3, 0.3, 0.3)), "fl", z=2.0, xy=(4, 4))
    rep = tier1_qa([box, fl], BUDGET)
    assert rep.ok and any(i.code == "floating_part" and i.node_id == "fl" for i in rep.issues)
    # stacked part touching the base is not floating and not interpenetrating
    cap = md(trimesh.creation.box((0.5, 0.5, 0.5)), "cap", z=1.0)
    assert not codes(tier1_qa([box, cap], BUDGET))
    # overlapping kitbash part -> warning only
    ov = md(trimesh.creation.box((1, 1, 1)), "ov", z=0.0, xy=(0.4, 0.4))
    rep = tier1_qa([box, ov], BUDGET)
    assert rep.ok and "interpenetration" in codes(rep, "warning")


# ------------------------------------------------------------------ UVs


def test_unwrap_sphere_uvs():
    m = md(trimesh.creation.icosphere(3, 0.5), "sph")
    u = unwrap_uvs(m, 256.0, 2048)
    assert u.uvs.shape == (len(u.points), 2) and u.uvs.min() >= 0 and u.uvs.max() <= 1
    assert len(u.triangles) == len(m.triangles) and len(u.vmapping) == len(u.points)
    assert np.allclose(u.points, m.points[u.vmapping])
    assert abs(u.texel_density - 256) / 256 < 0.25, u.texel_density
    assert uv_overlap_fraction([u]) < 0.01
    assert 0 <= u.stretch_mean <= u.stretch_max
    print(f"\nUV sphere: density {u.texel_density:.1f} px/m, stretch mean {u.stretch_mean:.3f} max {u.stretch_max:.3f}")


def test_unwrap_scene_shared_atlas():
    a = md(trimesh.creation.box((1, 1, 1)), "a", mat="wood")
    b = md(trimesh.creation.cylinder(0.3, 1), "b", mat="wood", xy=(2, 0))
    c = md(trimesh.creation.box((1, 1, 1)), "c", mat="metal", xy=(4, 0))
    res = unwrap_scene([a, b, c], EngineBudget("unreal_5", texel_density_px_per_m=200), 2048)
    assert [r.node_id for r in res] == ["a", "b", "c"]
    assert uv_overlap_fraction(res[:2]) < 0.01  # a, b share one atlas -> must not overlap each other
    assert res[0].atlas_id == "wood" and res[2].atlas_id == "metal"
    for r in res:
        assert abs(r.texel_density - 200) / 200 < 0.25 and r.uvs.max() <= 1


def _prim(kind: str, params: dict, scale=(1.0, 1.0, 1.0), mat: str = "m", nid: str | None = None) -> MeshData:
    from antigravity3d.ir import NodeIR
    from antigravity3d.kernel.compile_ir import primitive_charts, primitive_mesh

    node = NodeIR(id=kind, kind=kind, params=params)
    pts, tris = primitive_mesh(node)
    nid = nid or kind
    return MeshData(nid, f"/Asset/{nid}", np.asarray(pts, np.float64) * scale, tris, mat, uv_charts=primitive_charts(node))


PRIMS = [("box", {"size": (0.6, 0.4, 0.3)}), ("cylinder", {"radius": 0.2, "height": 0.5}), ("cone", {"radius": 0.2, "height": 0.4}),
         ("sphere", {"radius": 0.3}), ("capsule", {"radius": 0.15, "height": 0.3}), ("capsule", {"radius": 0.15, "height": 0.0})]


@pytest.mark.parametrize("kind,params", PRIMS)
def test_primitive_charts_unfold_exactly(kind, params):
    # non-uniform scale: the charts stay developable, so the layout is still exact at the requested density
    m = _prim(kind, params, scale=(1.0, 1.5, 0.8))
    u = unwrap_uvs(m, 1024.0, 2048)
    assert u.chart_source == "analytic" and u.fitted
    assert len(u.triangles) == len(m.triangles) and np.allclose(u.points, m.points[u.vmapping])
    assert u.stretch_mean < 1e-4 and u.stretch_max < 1e-3 and u.angle_distortion < 1e-4
    assert u.texel_density == pytest.approx(1024.0, rel=1e-3)
    assert uv_overlap_fraction([u], 1024) == 0.0 and u.uvs.min() >= 0 and u.uvs.max() <= 1
    a = u.uvs[u.triangles].astype(np.float64)
    signed = (a[:, 1, 0] - a[:, 0, 0]) * (a[:, 2, 1] - a[:, 0, 1]) - (a[:, 1, 1] - a[:, 0, 1]) * (a[:, 2, 0] - a[:, 0, 0])
    assert (signed > 0).all()  # no mirrored triangles


def test_overflow_fits_one_square_atlas_without_distortion():
    # 2 x 3 x 0.5 m box at 1024 px/m needs ~2.5x the 2048^2 atlas: density drops uniformly, texels stay square
    for charts in (True, False):
        m = _prim("box", {"size": (2.0, 3.0, 0.5)})
        if not charts:
            m = MeshData(m.node_id, m.prim_path, m.points, m.triangles, "m")
        u = unwrap_uvs(m, 1024.0, 2048)
        assert not u.fitted and 300 < u.texel_density < 1024 and u.uvs.max() <= 1
        # before: xatlas's fit mode grew the atlas past 2048 and the UVs were squeezed into the square (angle 0.28)
        assert u.stretch_mean < 1e-2 and u.angle_distortion < 1e-2, (charts, u.stretch_mean, u.angle_distortion)
        assert uv_overlap_fraction([u]) < 0.01


def test_computed_charts_meet_the_stretch_budget_on_curved_meshes():
    m = md(trimesh.creation.uv_sphere(0.5, count=[32, 16]), "ball")
    u = unwrap_uvs(m, 1024.0, 2048)
    assert u.chart_source == "computed" and u.stretch_mean <= BUDGET.max_uv_stretch, u.stretch_mean


def _uv_charts_of(r) -> np.ndarray:
    """Chart id per vertex of a UVResult: its uv-connected components (charts never share a vertex)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    t = np.asarray(r.triangles, np.int64)
    g = coo_matrix((np.ones(2 * len(t)), (np.r_[t[:, 0], t[:, 1]], np.r_[t[:, 1], t[:, 2]])), shape=(len(r.uvs),) * 2)
    return connected_components(g, directed=False)[1]


def min_chart_gutter_px(results) -> float:
    """Smallest distance, in atlas texels, between the borders of two different charts of one atlas (exact:
    point-to-segment distances between the border edges of every two charts whose padded boxes meet)."""
    segs, lab, base = [], [], 0
    for r in results:
        cid = _uv_charts_of(r) + base
        base = int(cid.max()) + 1
        t = np.asarray(r.triangles, np.int64)
        e = np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
        u, n = np.unique(e, axis=0, return_counts=True)
        bnd = u[n == 1]  # border edges (seams are borders: charts never share vertices)
        uv = r.uvs.astype(np.float64) * r.resolution
        segs.append(np.stack([uv[bnd[:, 0]], uv[bnd[:, 1]]], 1))
        lab.append(cid[bnd[:, 0]])
    seg, lab = np.concatenate(segs), np.concatenate(lab)
    charts = np.unique(lab)
    by = {c: seg[lab == c] for c in charts}
    box = {c: (by[c].reshape(-1, 2).min(0), by[c].reshape(-1, 2).max(0)) for c in charts}

    def pt_seg(p, a, b):  # (n,2) points to (m,2,2) segments -> (n, m)
        ab = b - a
        tt = np.clip(((p[:, None] - a[None]) * ab[None]).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-30)[None], 0, 1)
        return np.linalg.norm(p[:, None] - (a[None] + tt[..., None] * ab[None]), axis=-1)

    best, reach = np.inf, 4.0 * UV_PADDING_PX
    for i, c in enumerate(charts):
        for d in charts[i + 1:]:
            (lo1, hi1), (lo2, hi2) = box[c], box[d]
            if (lo1 > hi2 + reach).any() or (lo2 > hi1 + reach).any():
                continue
            s1, s2 = by[c], by[d]
            dist = min(pt_seg(s1[:, 0], s2[:, 0], s2[:, 1]).min(), pt_seg(s1[:, 1], s2[:, 0], s2[:, 1]).min(),
                       pt_seg(s2[:, 0], s1[:, 0], s1[:, 1]).min(), pt_seg(s2[:, 1], s1[:, 0], s1[:, 1]).min())
            best = min(best, float(dist))
    return best


def _meshes_for_gutter():
    torus = md(trimesh.creation.torus(0.5, 0.15, major_sections=48, minor_sections=16), "torus")
    return [
        ("computed sphere, overflowing", [md(trimesh.creation.uv_sphere(0.5, count=[32, 16]), "ball")], 1024.0),
        ("computed torus, charts cut into slabs", [torus], 1024.0),
        ("exact charts", [_prim("cylinder", {"radius": 0.2, "height": 0.5}, nid="a"), _prim("sphere", {"radius": 0.3}, nid="b")], 1024.0),
        ("mixed group", [_prim("box", {"size": (0.6, 0.4, 0.3)}, nid="box"), md(trimesh.creation.icosphere(2, 0.4), "ico", mat="m")], 1024.0),
    ]


@pytest.mark.parametrize("case", range(4))
def test_charts_never_overlap_and_keep_the_gutter(case):
    from antigravity3d.kernel.fast_ops import _unwrap_group

    name, group, density = _meshes_for_gutter()[case]
    res = _unwrap_group(group, density, 2048, "m")
    assert uv_overlap_fraction(res, 2048) == 0.0, name
    for r in res:
        assert r.uvs.min() >= 0 and r.uvs.max() <= 1, name
    # every chart sits in its own rectangle with UV_PADDING_PX texels of gutter on each side: charts are at
    # least 2 * UV_PADDING_PX apart (the guarantee is UV_PADDING_PX); 0.01 texel covers float32 uvs
    gutter = min_chart_gutter_px(res)
    assert gutter >= 2 * UV_PADDING_PX - 0.01, (name, gutter)


def test_computed_density_is_uniform_and_reported_exactly():
    # every chart is scaled to its 3D area, so each chart has the reported density; fitted is exact
    m = md(trimesh.creation.uv_sphere(0.5, count=[32, 16]), "ball")
    for density, res, fits in ((512.0, 2048, True), (1024.0, 2048, False), (1024.0, 4096, True)):
        u = unwrap_uvs(m, density, res)
        assert u.fitted is fits and (u.texel_density == pytest.approx(density, rel=1e-4)) is fits, (density, res, u.texel_density)
        cid = _uv_charts_of(u)[u.triangles[:, 0]]
        from antigravity3d.kernel.fast_ops import _tri_areas

        a3 = np.bincount(cid, _tri_areas(u.points, u.triangles, 3))
        a2 = np.bincount(cid, _tri_areas(u.uvs.astype(np.float64), u.triangles, 2))
        per_chart = np.sqrt(a2 / a3) * res
        assert np.allclose(per_chart, u.texel_density, rtol=2e-3), (per_chart.min(), per_chart.max(), u.texel_density)


def test_computed_charts_pack_densely_within_the_distortion_budget():
    # charts packed by xatlas reached 892 px/m on this sphere and 720 on this torus (1024 requested, 2048 atlas):
    # xatlas' random placement filled ~60 % of the square and its packing was not square. Packed as rectangles
    # (torus strips cut into slabs) they reach 920+ with the same charts and no more distortion.
    ball = md(trimesh.creation.uv_sphere(0.5, count=[40, 20]), "ball")
    torus = md(trimesh.creation.torus(0.5, 0.15, major_sections=64, minor_sections=24), "torus")
    for m, floor in ((ball, 905.0), (torus, 880.0)):
        u = unwrap_uvs(m, 1024.0, 2048)
        assert u.chart_source == "computed" and not u.fitted and u.texel_density >= floor, (m.node_id, u.texel_density)
        assert u.stretch_mean <= BUDGET.max_uv_stretch, (m.node_id, u.stretch_mean)


def _bumpy_blob() -> MeshData:
    s = trimesh.creation.icosphere(4, 0.5)
    d = s.vertices / np.linalg.norm(s.vertices, axis=1, keepdims=True)
    r = 0.5 * (1 + 0.18 * np.sin(5 * d[:, 0]) * np.sin(4 * d[:, 1] + 1) + 0.12 * np.cos(6 * d[:, 2] + 0.5 * d[:, 0]))
    return md(trimesh.Trimesh(d * r[:, None], s.faces, process=False), "blob")


def _wavy_sheet() -> MeshData:
    x, y = np.meshgrid(np.linspace(-0.8, 0.8, 30), np.linspace(-0.6, 0.6, 30))
    v = np.stack([x.ravel(), y.ravel(), 0.15 * np.sin(3 * x.ravel()) * np.cos(4 * y.ravel())], 1)
    f = [(a, a + 1, a + 31) for a in range(29 * 30) if a % 30 < 29] + [(a, a + 31, a + 30) for a in range(29 * 30) if a % 30 < 29]
    return MeshData("sheet", "/Asset/sheet", v, np.asarray(f, np.int32), "m")


def _shells() -> MeshData:
    parts = [trimesh.creation.icosphere(2, r).apply_translation([i * 0.7, 0, 0]) for i, r in enumerate((0.3, 0.15, 0.25))]
    parts.append(trimesh.creation.torus(0.3, 0.08, major_sections=32, minor_sections=12).apply_translation([0, 1, 0]))
    return md(trimesh.util.concatenate(parts), "shells")


FLATTEN_SUBJECTS = {
    "uv sphere": lambda: md(trimesh.creation.uv_sphere(0.5, count=[32, 16]), "ball"),
    "torus": lambda: md(trimesh.creation.torus(0.5, 0.15, major_sections=48, minor_sections=16), "torus"),
    "bumpy blob": _bumpy_blob,
    "open sheet": _wavy_sheet,
    "long tube": lambda: md(trimesh.creation.cylinder(0.03, 2.5, sections=24).subdivide().subdivide(), "tube"),
    "several shells": _shells,
    "concave prism": l_shape,
}


@pytest.mark.parametrize("name", sorted(FLATTEN_SUBJECTS))
def test_computed_charts_are_never_flipped_and_never_overlap_themselves(name):
    from antigravity3d.kernel.fast_ops import _valid_charts

    u = unwrap_uvs(FLATTEN_SUBJECTS[name](), 1024.0, 2048)
    q = u.uvs.astype(np.float64)[u.triangles]
    signed = (q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1]) - (q[:, 1, 1] - q[:, 0, 1]) * (q[:, 2, 0] - q[:, 0, 0])
    assert (signed > 0).all(), (name, int((signed <= 0).sum()))  # every chart counter-clockwise, none folded
    chart = _uv_charts_of(u)[u.triangles[:, 0]]
    assert _valid_charts(u.uvs.astype(np.float64), u.triangles.astype(np.int64), chart).all(), name  # exact border test
    assert uv_overlap_fraction([u], 2048) == 0.0, name
    assert u.stretch_mean <= 0.01 and u.angle_distortion <= 0.01, (name, u.stretch_mean, u.angle_distortion)


def test_developable_charts_flatten_without_distortion():
    # xatlas projected the tube's quarter-cylinder charts orthographically: area distortion 0.080, over the
    # 0.05 budget. Unfolded by the conformal map they are isometric.
    u = unwrap_uvs(FLATTEN_SUBJECTS["long tube"](), 1024.0, 2048)
    assert u.within(BUDGET) and u.stretch_mean < 1e-4 and u.angle_distortion < 1e-4, (u.stretch_mean, u.angle_distortion)


def test_chart_validity_detects_flips_and_self_overlap():
    from antigravity3d.kernel.fast_ops import _valid_charts

    # a strip of 8 quads; chart 0 lies flat, chart 1 is bent round until its end crosses its start
    n = 8
    top = np.arange(n + 1)
    tris = np.array([(i, i + 1, i + n + 2) for i in range(n)] + [(i, i + n + 2, i + n + 1) for i in range(n)], np.int64)
    flat = np.array([(i, 0.0) for i in range(n + 1)] + [(i, 1.0) for i in range(n + 1)], np.float64)
    ang = np.linspace(0, 2.4 * np.pi, n + 1)
    curl = np.concatenate([np.stack([3 * np.cos(ang), 3 * np.sin(ang)], 1), np.stack([2 * np.cos(ang), 2 * np.sin(ang)], 1)])
    uv = np.concatenate([flat, curl])
    q = curl[tris]
    assert ((q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1]) - (q[:, 1, 1] - q[:, 0, 1]) * (q[:, 2, 0] - q[:, 0, 0]) > 0).all()  # no flip
    t = np.concatenate([tris, tris + 2 * (n + 1)])
    chart = np.repeat([0, 1], len(tris))
    assert _valid_charts(uv, t, chart).tolist() == [True, False]
    folded = flat.copy()
    folded[top[3], 1] = 1.5  # pushes a lower vertex above the upper row: two triangles turn over
    assert _valid_charts(folded, tris, np.zeros(len(tris), np.int64)).tolist() == [False]


def test_flattening_keeps_the_projection_where_the_solve_is_invalid(monkeypatch):
    from antigravity3d.kernel import fast_ops

    m = FLATTEN_SUBJECTS["torus"]()
    stats = []
    real_flatten, real_arap = fast_ops._flatten, fast_ops._arap

    def bad_arap(p3, t, uv, chart, solve, iters=3):  # chart 0 comes out mirrored (all its triangles flipped)
        out = real_arap(p3, t, uv, chart, solve, iters)
        v = np.unique(t[chart == 0])
        out[v, 0] *= -1
        return out

    def spy(*a):
        r = real_flatten(*a)
        stats.append(r[1])
        return r

    monkeypatch.setattr(fast_ops, "_arap", bad_arap)
    monkeypatch.setattr(fast_ops, "_flatten", spy)
    u = unwrap_uvs(m, 1024.0, 2048)
    assert stats[0]["projection_kept_invalid"] == 1
    assert uv_overlap_fraction([u], 2048) == 0.0 and u.stretch_mean <= BUDGET.max_uv_stretch


def test_computed_charts_are_packed_once_not_at_full_density(monkeypatch):
    # xatlas only computes the charts, in a tiny atlas; its packing at the requested density (an atlas of
    # 2300 x 2300 texels for this sphere, then again at the fitted density) is not run at all
    import xatlas

    real = xatlas.Atlas
    sizes = []

    class Spy:
        def __init__(self):
            self._a = real()

        def __getattr__(self, k):
            return getattr(self._a, k)

        def generate(self, *a, **kw):
            self._a.generate(*a, **kw)
            sizes.append((self._a.width, self._a.height))

    monkeypatch.setattr(xatlas, "Atlas", Spy)
    u = unwrap_uvs(md(trimesh.creation.uv_sphere(0.5, count=[32, 16]), "ball"), 1024.0, 2048)
    assert len(sizes) == 1 and max(sizes[0]) <= 4 * UV_CHART_PASS_PX, sizes
    assert 880 < u.texel_density < 1024 and not u.fitted


def test_mixed_group_keeps_exact_charts_exact():
    from antigravity3d.kernel.fast_ops import _unwrap_group

    box = _prim("box", {"size": (0.6, 0.4, 0.3)}, nid="box")
    ball = md(trimesh.creation.uv_sphere(0.4, count=[32, 16]), "ball", mat="m")
    a, b = _unwrap_group([box, ball], 1024.0, 2048, "m")
    assert (a.chart_source, b.chart_source) == ("analytic", "computed")
    assert a.stretch_mean < 1e-4 and a.angle_distortion < 1e-4  # still the exact unfolding
    assert a.fitted == b.fitted and a.texel_density == pytest.approx(b.texel_density, rel=2e-3)
    assert uv_overlap_fraction([a, b], 2048) == 0.0


def test_skyline_packer_matches_its_reference():
    # the packer was rewritten for speed: same placements as the straightforward implementation
    from antigravity3d.kernel.fast_ops import _skyline_pack

    def reference(sizes, width, height):
        order = sorted(range(len(sizes)), key=lambda i: (-max(sizes[i]), -sizes[i][0] * sizes[i][1], i))
        sky = [(0.0, 0.0, float(width))]
        out = np.zeros((len(sizes), 3))
        for i in order:
            best = None
            for turned in (0, 1):
                w, h = (sizes[i][1], sizes[i][0]) if turned else (sizes[i][0], sizes[i][1])
                if w > width or h > height:
                    continue
                for j, (x, _, _) in enumerate(sky):
                    if x + w > width + 1e-9:
                        break
                    y, k, right = 0.0, j, x + w
                    while k < len(sky) and sky[k][0] < right - 1e-9:
                        y = max(y, sky[k][1])
                        k += 1
                    if y + h <= height + 1e-9 and (best is None or (y + h, x) < (best[0], best[1])):
                        best = (y + h, x, y, w, h, turned)
            if best is None:
                return None
            _, x, y, w, h, turned = best
            out[i] = (x, y, turned)
            new, right = [], x + w
            for sx, sy, sw in sky:
                if sx + sw <= x + 1e-9 or sx >= right - 1e-9:
                    new.append((sx, sy, sw))
                    continue
                if sx < x:
                    new.append((sx, sy, x - sx))
                if sx + sw > right:
                    new.append((right, sy, sx + sw - right))
            new.append((x, y + h, w))
            new = sorted(seg for seg in new if seg[2] > 1e-9)
            merged = [new[0]]
            for seg in new[1:]:
                px, py, pw = merged[-1]
                if abs(py - seg[1]) < 1e-9 and abs(px + pw - seg[0]) < 1e-9:
                    merged[-1] = (px, py, pw + seg[2])
                else:
                    merged.append(seg)
            sky = merged
        return out

    rng = np.random.default_rng(7)
    for trial in range(120):
        n = int(rng.integers(1, 150))
        s = rng.random((n, 2)) * rng.choice([5.0, 50.0, 300.0]) + (4.0 if trial % 2 else 0.0)
        if trial % 4 == 0:
            s = np.round(s)  # ties
        side = float(rng.choice([256.0, 1024.0, 2047.0]))
        a, b = _skyline_pack(s, side, side), reference(s.tolist(), side, side)
        assert (a is None) == (b is None) and (a is None or np.array_equal(a, b)), trial


def test_unwrap_is_reproducible_across_processes():
    # same input, same bytes: independent of thread counts and hash seeds
    import subprocess
    import sys

    code = ("import hashlib, numpy as np, trimesh\n"
            "from antigravity3d.kernel.fast_ops import _unwrap_group\n"
            "from antigravity3d.kernel.types import MeshData\n"
            "t = trimesh.creation.torus(0.5, 0.15, major_sections=48, minor_sections=16)\n"
            "s = trimesh.creation.uv_sphere(0.4, count=[24, 12])\n"
            "ms = [MeshData(n, '/' + n, np.asarray(x.vertices) + o, np.asarray(x.faces, np.int32), 'm') for n, x, o in (('t', t, 0), ('s', s, 2))]\n"
            "h = hashlib.sha256()\n"
            "for r in _unwrap_group(ms, 1024.0, 2048, 'm'):\n"
            "    h.update(r.uvs.tobytes() + r.triangles.tobytes() + np.asarray(r.vmapping, np.int64).tobytes())\n"
            "print(h.hexdigest())\n")
    import os

    outs = set()
    for env in ({"OMP_NUM_THREADS": "1", "PYTHONHASHSEED": "1"}, {"OMP_NUM_THREADS": "4", "PYTHONHASHSEED": "2"}):
        p = subprocess.run([sys.executable, "-c", code], env={**os.environ, **env}, capture_output=True, text=True, check=True)
        outs.add(p.stdout.strip())
    assert len(outs) == 1, outs


# ------------------------------------------------------------------ LODs


@pytest.mark.parametrize("with_uv", [False, True])
def test_lods_monotonic_and_closed(with_uv):
    m = md(trimesh.creation.icosphere(4, 0.5), "sph")
    uv = None
    pts, tri = m.points, m.triangles
    if with_uv:
        u = unwrap_uvs(m, 200, 2048)
        pts, tri, uv = u.points, u.triangles, u.uvs
    lods = generate_lods(pts, tri, uvs=uv)
    counts = [l.tri_count for l in lods]
    print(f"\nLOD tris (uv={with_uv}):", counts, [round(l.error, 4) for l in lods])
    assert counts[0] == len(tri) and all(a > b for a, b in zip(counts, counts[1:]))
    for l in lods:
        assert (uv is None) == (l.uvs is None)
        if with_uv:
            assert l.uvs.shape == (len(l.points), 2)
        assert l.triangles.max() < len(l.points)
        if not with_uv:  # UV seams legitimately open the mesh: positions are split there
            assert _topology(l.points, l.triangles).closed
        else:
            assert _topology(l.points, l.triangles).closed  # position-welded topology stays closed
    assert lods[-1].error > 0 and lods[-1].tri_count <= 0.15 * len(tri)


def test_lods_follow_the_screen_space_budget_and_never_duplicate():
    from antigravity3d.kernel.fast_ops import generate_asset_lods
    from antigravity3d.kernel.lod_diff import LOD_MIN_REDUCTION, lod_error_budget_m

    boxes = [_prim("box", {"size": (0.5, 0.5, 0.5)}, nid="a"), _prim("box", {"size": (0.1, 0.1, 0.6)}, nid="b")]
    chains = generate_asset_lods(unwrap_scene(boxes, BUDGET))
    assert [len(c) for c in chains.values()] == [1, 1]  # nothing to remove from boxes: LOD0 only, no copies

    m = md(trimesh.creation.icosphere(4, 0.5), "sph")
    u = unwrap_uvs(m, 512, 2048)
    lods = generate_asset_lods([u])["sph"]
    radius = np.linalg.norm(np.ptp(u.points, axis=0)) / 2
    tris = [l.tri_count for l in lods]
    assert len(lods) >= 3 and all(b <= (1 - LOD_MIN_REDUCTION) * a for a, b in zip(tris, tris[1:]))
    fr = [l.screen_fraction for l in lods]
    assert fr[0] == 1.0 and all(b < a for a, b in zip(fr, fr[1:]))
    for l in lods[1:]:
        assert 0 < l.error_m <= lod_error_budget_m(radius, l.screen_fraction) + 1e-9
        assert l.uvs.shape == (len(l.points), 2) and _topology(l.points, l.triangles).closed


def test_seam_split_cylinder_simplifies_along_its_seams():
    # xatlas charts split every rim vertex; meshoptimizer re-welds by position and collapses along seams
    m = md(trimesh.creation.cylinder(0.3, 1.0, sections=32), "cyl")
    b = run_export_pipeline([m], BUDGET, "Cyl")
    tris = [l.tri_count for l in b.lods["cyl"]]
    assert len(tris) >= 2 and tris[-1] < 0.6 * tris[0], tris
    for l in b.lods["cyl"][1:]:
        assert l.uvs is not None and l.uvs.min() >= 0 and l.uvs.max() <= 1


def test_surface_deviation_is_exact():
    from antigravity3d.kernel.fast_ops import _point_triangle_distance, _Simplifier, _surface_deviation

    m = md(trimesh.creation.cylinder(0.3, 1.0, sections=24), "c")
    s = _Simplifier(m.points, m.triangles, None)
    p, t = s.ref
    t1 = s._run(0.03).reshape(-1, 3).astype(np.int64)
    a, b, c = p[t1[:, 0]], p[t1[:, 1]], p[t1[:, 2]]
    samples = np.vstack([(a + b + c) / 3, (a + b) / 2, (b + c) / 2, (c + a) / 2])
    brute = max(min(_point_triangle_distance(np.repeat(v[None], len(t1), 0), a, b, c)) for v in p[np.unique(t)])
    brute = max(brute, max(min(_point_triangle_distance(np.repeat(s[None], len(t), 0), p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]))
                           for s in samples))
    assert _surface_deviation(p, t, p, t1, 0.0) == pytest.approx(brute, abs=1e-12) and brute > 0


def test_surface_index_is_exact_on_dense_meshes():
    # > 2048 triangles takes the box-tree pruning path; it must agree with brute force exactly
    from antigravity3d.kernel.fast_ops import _point_triangle_distance, _SurfaceIndex

    tm = trimesh.creation.icosphere(4, 0.5)
    p, t = np.asarray(tm.vertices, float), np.asarray(tm.faces, np.int64)
    rng = np.random.default_rng(0)
    q = rng.normal(size=(300, 3)) * 0.4
    brute = np.array([_point_triangle_distance(np.repeat(x[None], len(t), 0), p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]).min() for x in q])
    index = _SurfaceIndex(p, t)
    assert np.allclose(index.distances(q, 0.0), brute, atol=1e-12)
    d = index.distances(q, 0.05)
    assert np.allclose(d[brute > 0.05], brute[brute > 0.05], atol=1e-12) and (d[brute <= 0.05] <= 0.05).all()
    assert index.exceeds(q, brute.max() - 1e-9) and not index.exceeds(q, brute.max() + 1e-9)


def test_convex_upper_bounds_the_true_distance():
    # the part-removal bracket: lo (exact point distances) <= D <= hi (convex bound per triangle, coplanar quads)
    from antigravity3d.kernel.fast_ops import _convex_upper, _SurfaceIndex

    box = trimesh.creation.box((1.0, 1.0, 0.8))
    index = _SurfaceIndex(np.asarray(box.vertices, float), np.asarray(box.faces, np.int64))
    strap = trimesh.creation.box((0.04, 0.06, 0.7))
    strap.apply_translation((0.52, 0.1, 0.0))
    tri = np.asarray(strap.vertices, float)[np.asarray(strap.faces)]
    up = _convex_upper(index, tri)
    w = np.random.default_rng(1).dirichlet((1, 1, 1), 400)
    dense = np.einsum("sk,nkd->nsd", w, tri)  # 400 random points in every strap triangle
    exact = index.distances(dense.reshape(-1, 3), 0.0).reshape(len(tri), -1).max(axis=1)
    assert (up >= exact - 1e-12).all()
    assert up.max() == pytest.approx(0.04, abs=1e-9)  # the strap's outer face is 4 cm off the side: tight bound


def _plate_with_pebbles():
    """One large two-triangle plate under 60 small spheres: a mesh whose triangle sizes differ by 1000x."""
    plate = trimesh.Trimesh([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [(0, 1, 2), (0, 2, 3)], process=False)
    rng = np.random.default_rng(3)
    pebbles = []
    for _ in range(60):
        s = trimesh.creation.icosphere(2, 0.01)
        s.apply_translation((*rng.uniform(-0.9, 0.9, 2), 0.02))
        pebbles.append(s)
    tm = trimesh.util.concatenate([plate, *pebbles])
    return np.asarray(tm.vertices, float), np.asarray(tm.faces, np.int64)


def test_surface_index_exact_search_does_not_scan_everything_for_one_large_triangle(monkeypatch):
    # The exhaustive pass may only visit triangles whose box is closer than the bound found. With one search
    # margin for the whole mesh (its largest half-diagonal, the plate's 1.4 m), every query would visit all
    # 19,202 triangles: an assembly of small parts on a table top would take a minute to get its LODs.
    import scipy.spatial

    from antigravity3d.kernel import fast_ops as F

    p, t = _plate_with_pebbles()
    q = np.random.default_rng(4).uniform((-1, -1, 0.0), (1, 1, 0.06), (400, 3))
    a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    brute = np.array([F._point_triangle_distance(np.repeat(x[None], len(t), 0), a, b, c).min() for x in q])
    visited = []

    class Tree(scipy.spatial.cKDTree):  # counts the boxes the exhaustive pass is handed
        def query_ball_point(self, x, r, *args, **kw):
            hits = super().query_ball_point(x, r, *args, **kw)
            visited.append(sum(map(len, hits)))
            return hits

    monkeypatch.setattr(scipy.spatial, "cKDTree", Tree)
    index = F._SurfaceIndex(p, t)
    assert np.array_equal(index.distances(q, 0.0), brute)  # exact: the same minimum of the same values
    assert index.max_distance(q) == brute.max()
    assert sum(visited) < 0.01 * len(q) * len(t), sum(visited)


@pytest.mark.parametrize("limit", [0.0, 0.004, 0.01, 0.02, 0.05, 1.0])
def test_max_over_and_exceeds_agree_with_the_full_distances(limit):
    # max_over settles only what can change the maximum; it must return exactly distances(...).max()
    from antigravity3d.kernel.fast_ops import _SurfaceIndex

    p, t = _plate_with_pebbles()
    index = _SurfaceIndex(p, t)
    q = np.random.default_rng(5).uniform((-1, -1, -0.02), (1, 1, 0.08), (3000, 3))
    full = index.distances(q, limit)
    assert index.max_over(q, limit) == full.max()
    assert index.exceeds(q, limit) == bool(full.max() > limit)


def test_coarse_samples_and_projected_areas_keep_their_values():
    # shared edge midpoints are taken once and the silhouette product runs in row blocks: same maximum, same bits
    from antigravity3d.kernel.fast_ops import _VIEW_DIRS, _coarse_samples, _projected_areas, _SurfaceIndex

    tm = trimesh.creation.icosphere(5, 0.5)  # 20,480 triangles: more than one block
    p, t = np.asarray(tm.vertices, float), np.asarray(tm.faces, np.int64)
    a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    per_triangle = np.vstack([(a + b + c) / 3, (a + b) / 2, (b + c) / 2, (c + a) / 2])
    shared = _coarse_samples(p, t)
    assert len(shared) == len(t) + len(t) * 3 // 2  # closed mesh: every edge shared by two triangles
    assert {r.tobytes() for r in shared} == {r.tobytes() for r in per_triangle}
    index = _SurfaceIndex(p * 1.01, t)
    assert index.max_over(shared, 0.0) == index.max_over(per_triangle, 0.0)
    n = np.cross(b - a, c - a) / 2
    assert np.array_equal(_projected_areas(p, t), 0.5 * np.abs(n @ _VIEW_DIRS.T).sum(axis=0))


def test_removal_bounds_from_per_part_results_match_the_union():
    # lo and hi are assembled from per-part results cached across remaining sets: lo must equal the exact
    # largest probe distance to the union of the remaining parts, and hi must bound every point of the part
    from antigravity3d.kernel.fast_ops import _Removal, _Simplifier, _SurfaceIndex, _gather

    rng = np.random.default_rng(6)
    parts = {"base": trimesh.creation.box((1.0, 1.0, 0.1))}
    for i in range(6):
        m = trimesh.creation.icosphere(1, 0.05) if i % 2 else trimesh.creation.box((0.08, 0.05, 0.04))
        m.apply_translation((*rng.uniform(-0.4, 0.4, 2), 0.08))
        parts[f"p{i}"] = m
    simp = {n: _Simplifier(np.asarray(m.vertices, float), np.asarray(m.faces), None) for n, m in parts.items()}
    removal = _Removal(simp, 0.9)
    for nid in ("p0", "p1", "base"):
        for rest in (frozenset(simp) - {nid}, frozenset({"base", "p2", "p3"}) - {nid}):
            union = _SurfaceIndex(*_gather([simp[n].ref for n in sorted(rest)]))
            assert removal._lo(nid, rest) == union.max_distance(removal._probes(nid))
            s = simp[nid]
            w = rng.dirichlet((1, 1, 1), 50)
            dense = np.einsum("sk,nkd->nsd", w, s.ref[0][s.ref[1]]).reshape(-1, 3)
            assert removal._hi(nid, rest) >= union.distances(dense, 0.0).max() - 1e-12


def test_removal_search_stops_when_the_level_cannot_pay():
    # a level is emitted only if it saves `needed` triangles; once the parts not yet decided cannot make that
    # up, nothing more is measured (the result is discarded either way)
    from antigravity3d.kernel.fast_ops import _Removal, _Simplifier

    parts = {f"b{i}": trimesh.creation.box((0.1, 0.1, 0.1)) for i in range(6)}
    for i, m in enumerate(parts.values()):
        m.apply_translation((i * 0.5, 0, 0))  # far apart: none can go at a small budget
    simp = {n: _Simplifier(np.asarray(m.vertices, float), np.asarray(m.faces), None) for n, m in parts.items()}
    removal = _Removal(simp, 0.9)
    decided = []
    real = removal._decide
    removal._decide = lambda nid, *a: (decided.append(nid), real(nid, *a))[1]
    picks = {n: (s.idx0, 0.0) for n, s in simp.items()}
    assert removal.droppable(picks, set(), 0.001, 540.0, needed=36) == set()  # 3 of 6 boxes would have to go
    assert len(decided) == 4  # after 4 refusals the last 2 boxes (24 triangles) cannot save 36


def test_assembly_drops_only_parts_whose_removal_is_invisible():
    from antigravity3d.kernel.fast_ops import generate_asset_lods
    from antigravity3d.kernel.lod_diff import lod_pop_report
    from antigravity3d.tactics.package import merged_lods

    body = _prim("box", {"size": (1.0, 1.0, 0.8)}, nid="body")
    plate = _prim("box", {"size": (0.3, 0.05, 0.01)}, nid="plate")  # 1 cm thick, lying on the top face
    plate = MeshData("plate", "/Asset/plate", plate.points + [0, 0, 0.405], plate.triangles, "m", uv_charts=plate.uv_charts)
    knob = _prim("box", {"size": (0.03, 0.03, 0.03)}, nid="knob")  # small, but floating 40 cm above the body
    knob = MeshData("knob", "/Asset/knob", knob.points + [0, 0, 0.8], knob.triangles, "m", uv_charts=knob.uv_charts)
    wing = _prim("box", {"size": (0.6, 0.6, 0.6)}, nid="wing")  # large part beside the body
    wing = MeshData("wing", "/Asset/wing", wing.points + [0.8, 0, 0], wing.triangles, "m", uv_charts=wing.uv_charts)
    uvs = unwrap_scene([body, plate, knob, wing], BUDGET)
    chains = generate_asset_lods(uvs, min_reduction=0.05)
    n = len(chains["body"])
    assert n >= 2 and all(len(c) == n for c in chains.values())
    assert chains["plate"][-1].tri_count == 0 and chains["plate"][-1].points.shape == (0, 3)  # absent: no geometry
    for nid in ("body", "knob", "wing"):
        assert all(lod.tri_count == 12 for lod in chains[nid]), nid  # never dropped (and boxes do not simplify)
    absent = [lod.tri_count == 0 for lod in chains["plate"]]
    assert absent == sorted(absent)  # once absent, absent at every farther level
    bundle = ExportBundle("A", uvs, chains, [])
    rep = lod_pop_report(merged_lods(bundle))
    assert rep.ok(), rep.one_line()


# ------------------------------------------------------------------ colliders


def _covers(hulls, pts, tol=0.02):
    ok = np.zeros(len(pts), bool)
    for h in hulls:
        eq = ConvexHull(h.points).equations
        ok |= (pts @ eq[:, :3].T + eq[:, 3] <= tol).all(axis=1)
    return ok.mean()


def test_colliders_fast_path_and_ucx_names():
    ms = [md(trimesh.creation.box((1, 1, 1)), "box"), md(trimesh.creation.cylinder(0.3, 1), "cyl", xy=(2, 0)),
          md(trimesh.creation.box((1, 1, 1)), "skip", xy=(4, 0), collider=False)]
    hulls = generate_colliders(ms, 8, asset_name="Crate")
    assert [h.source for h in hulls] == ["fast_path"] * 2
    assert [h.name for h in hulls] == ["UCX_Crate_00", "UCX_Crate_01"]


def test_colliders_concave_convex_cover_and_cap():
    m = l_shape()
    vol = trimesh.Trimesh(m.points, m.triangles).volume
    for cap in (8, 2):
        t = time.perf_counter()
        hulls = generate_colliders([m], cap, asset_name="L")
        dt = (time.perf_counter() - t) * 1e3
        print(f"\nconcave L ({len(m.triangles)} tris) cap={cap}: {len(hulls)} hulls in {dt:.0f} ms")
        assert 1 <= len(hulls) <= cap and len(hulls) >= 1
        for h in hulls:
            assert trimesh.Trimesh(h.points, h.triangles).is_convex
        assert _covers(hulls, m.points) > 0.99
        assert 0.9 * vol <= sum(h.volume for h in hulls) <= 2.5 * vol
    assert len(generate_colliders([m], 8)) >= 2  # concave must be split


def test_colliders_multibody_kitbash_does_not_crash():
    m = l_kitbash()
    hulls = generate_colliders([m], 4, asset_name="K")
    assert 2 <= len(hulls) <= 4 and _covers(hulls, m.points) > 0.99


def test_hull_merge_enforces_cap():
    ms = [md(trimesh.creation.box((0.5, 0.5, 0.5)), f"b{i}", xy=(i, 0)) for i in range(6)]
    hulls = generate_colliders(ms, 3)
    assert len(hulls) == 3 and all(h.source in ("fast_path", "merged") for h in hulls)
    assert _covers(hulls, np.vstack([m.points for m in ms])) == 1.0


def _voxel_solid(filled, size: float = 0.05):
    """Closed, welded, outward-wound mesh of a voxel set: every planar face is cut into unit squares."""
    faces = {(1, 0, 0): [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)], (-1, 0, 0): [(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)],
             (0, 1, 0): [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)], (0, -1, 0): [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)],
             (0, 0, 1): [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], (0, 0, -1): [(0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)]}
    filled = set(filled)
    vid: dict = {}
    tris = []
    for (i, j, k) in sorted(filled):
        for d, corners in faces.items():
            if (i + d[0], j + d[1], k + d[2]) in filled:
                continue
            q = [vid.setdefault((i + a, j + b, k + c), len(vid)) for a, b, c in corners]
            tris += [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
    pts = np.array(sorted(vid, key=vid.get), float) * size
    return md(trimesh.Trimesh(pts, np.array(tris), process=False), "vox")


def _u_prism() -> MeshData:  # concave U, 12 x 8 x 10 voxels: about 2,000 triangles for 30 planar faces
    return _voxel_solid([(i, j, k) for i in range(12) for j in range(8) for k in range(10) if j < 3 or i < 3 or i >= 9])


def _table() -> MeshData:  # a slab on four legs: the slab's underside is one face with four holes
    return _voxel_solid([(i, j, k) for i in range(16) for j in range(16) for k in range(16) if k >= 13 or ((i < 3 or i >= 13) and (j < 3 or j >= 13))])


def _welded(m: MeshData):
    c = _canon(m.points)
    p = np.zeros((int(c.max()) + 1, 3))
    p[c] = m.points
    return p, c[m.triangles]


@pytest.mark.parametrize("make, max_tris", [(_u_prism, 40), (_table, 80)])
def test_planar_merge_keeps_the_solid_exactly(make, max_tris):
    m = make()
    p, t = _welded(m)
    before = _topology(p, t)
    out = _merge_planar(p, t)
    assert out is not None and len(t) > 1000 and len(out[1]) <= max_tris
    after = _topology(*out)
    assert after.closed and not after.degenerate and not after.duplicates
    assert after.volume == pytest.approx(before.volume, rel=1e-12)
    assert np.allclose(ConvexHull(out[0]).volume, ConvexHull(p).volume, rtol=1e-12)
    # every original vertex still lies on the new surface
    tp = out[0][out[1]]
    near = np.min([_point_triangle_distance(p, *(np.broadcast_to(c, p.shape) for c in tri)) for tri in tp], axis=0)
    assert near.max() < 1e-9
    again = _merge_planar(p, t)
    assert np.array_equal(again[0], out[0]) and np.array_equal(again[1], out[1])  # deterministic


def test_planar_merge_leaves_curved_and_open_meshes_alone():
    tor = md(trimesh.creation.torus(0.5, 0.18, major_sections=40, minor_sections=20), "tor")
    assert _merge_planar(*_welded(tor)) is None
    assert _decomposition_input(tor.points, tor.triangles)[1] is tor.triangles
    u = _u_prism()
    cut = np.asarray(u.triangles)[:-2]  # open mesh
    pts, tri = _decomposition_input(u.points, cut)
    assert tri is cut and pts is u.points


def test_decomposition_gets_the_merged_solid_and_hulls_still_cover(monkeypatch):
    import coacd

    seen = []
    real = coacd.run_coacd

    def spy(mesh, **kw):
        seen.append(len(mesh.indices))
        return real(mesh, **kw)

    monkeypatch.setattr(coacd, "run_coacd", spy)
    m = _u_prism()
    vol = trimesh.Trimesh(m.points, m.triangles).volume
    hulls = generate_colliders([m], 8, asset_name="U")
    assert seen and max(seen) <= 40 < len(m.triangles)
    assert 2 <= len(hulls) <= 8 and _covers(hulls, m.points) == 1.0
    assert vol <= sum(h.volume for h in hulls) <= 1.05 * vol
    for h in hulls:  # no hull leaves the mesh's own convex hull
        assert ConvexHull(np.vstack([m.points, h.points])).volume <= ConvexHull(m.points).volume * (1 + 1e-6)  # CoACD returns float32 vertices


def test_closed_bodies_in_one_mesh_are_decomposed_one_by_one(monkeypatch):
    import coacd

    modes = []
    real = coacd.run_coacd

    def spy(mesh, **kw):
        modes.append(kw["preprocess_mode"])
        return real(mesh, **kw)

    monkeypatch.setattr(coacd, "run_coacd", spy)
    u, v = _u_prism(), _table()
    pts = np.vstack([u.points, v.points + [2.0, 0, 0], trimesh.creation.box((0.3, 0.3, 0.3)).vertices + [0, 2.0, 0]])
    box_tris = trimesh.creation.box((0.3, 0.3, 0.3)).faces
    tri = np.vstack([u.triangles, v.triangles + len(u.points), box_tris + len(u.points) + len(v.points)])
    m = MeshData("bodies", "/Asset/bodies", pts, tri.astype(np.int32), "m")
    assert _topology(m.points, m.triangles).closed
    t = time.perf_counter()
    hulls = generate_colliders([m], 12, asset_name="B")
    dt = time.perf_counter() - t
    # the manifold repair pass costs seconds and would also re-mesh the bodies; closed bodies need neither
    assert modes == ["off", "off"] and dt < 5.0
    assert [h.source for h in hulls].count("fast_path") == 1  # the cube
    assert _covers(hulls, m.points) == 1.0
    assert sum(h.volume for h in hulls) <= 1.05 * (_topology(u.points, u.triangles).volume + _topology(v.points, v.triangles).volume + 0.027)


# ------------------------------------------------------------------ export + timing


def test_gltf_and_ktx(tmp_path):
    m = md(trimesh.creation.icosphere(2, 0.5), "sph")
    u = unwrap_uvs(m, 200, 1024)
    lods = generate_lods(u.points, u.triangles, uvs=u.uvs)
    out = write_gltf(tmp_path / "a.glb", lods, names=[f"sph_LOD{i}" for i in range(len(lods))])
    sc = trimesh.load(out)
    assert set(sc.geometry) == {f"sph_LOD{i}" for i in range(len(lods))}
    g = sc.geometry["sph_LOD0"]
    assert abs(g.bounds[:, 1].max() - 1.0) < 1e-3  # sphere spans z 0..1 -> Y 0..1 (Z-up -> Y-up)
    png = tmp_path / "t.png"
    from PIL import Image
    Image.new("RGB", (16, 16), (200, 10, 10)).save(png)
    import shutil
    r = compress_texture_ktx2(png, tmp_path / "t.ktx2")
    assert (r is None) == (shutil.which("ktx") is None and shutil.which("toktx") is None)


def _prop_convex() -> list[MeshData]:  # barrel: 2000 tris, convex
    return [md(trimesh.creation.cylinder(0.4, 1.0, sections=500), "barrel")]


def test_pipeline_timing():
    b = EngineBudget("unreal_5", texel_density_px_per_m=256)
    convex = _prop_convex()
    assert len(convex[0].triangles) == 2000
    run_export_pipeline(convex, b, "Barrel")  # warm
    best = min((run_export_pipeline(convex, b, "Barrel") for _ in range(3)), key=lambda x: x.timings_ms["total"])
    print("\nconvex 2k-tri prop pipeline ms:", best.timings_ms)
    assert best.timings_ms["total"] < 250
    assert best.colliders[0].source == "fast_path" and best.colliders[0].name == "UCX_Barrel_00"

    concave = [l_shape(0.1)]
    run_export_pipeline(concave, b, "L")
    r = run_export_pipeline(concave, b, "L")
    print(f"concave {len(concave[0].triangles)}-tri prop pipeline ms:", r.timings_ms)
    assert len(r.colliders) <= b.max_collision_hulls


def test_seam_copies_that_differ_by_rounding_noise_do_not_open_cracks():
    # a tube whose UV seam is closed with cos/sin of 2 pi: the two copies of each seam vertex differ by 1e-16, and
    # were simplified separately, which opened the seam in the far levels
    seg, rings = 48, 20
    ang, zz = np.linspace(0, 2 * np.pi, seg + 1), np.linspace(0, 1.0, rings + 1)
    A, Z = np.meshgrid(ang, zz)
    bulge = 0.3 * (1 + 0.3 * np.sin(np.pi * Z.ravel()))
    pts = np.stack([bulge * np.cos(A).ravel(), bulge * np.sin(A).ravel(), Z.ravel()], 1)
    uvs = np.stack([A.ravel() / (2 * np.pi), Z.ravel()], 1).astype(np.float32)
    tris = np.array([t for j in range(rings) for i in range(seg) for a in [j * (seg + 1) + i]
                     for t in ((a, a + 1, a + seg + 2), (a, a + seg + 2, a + seg + 1))], np.int32)
    assert not np.array_equal(pts[0], pts[seg]) and np.allclose(pts[0], pts[seg], atol=1e-12)  # the noise is there
    chain = generate_lods(pts, tris, uvs=uvs)
    assert len(chain) >= 3
    for lod in chain[1:]:
        cid = _canon(lod.points)[lod.triangles]
        e = np.sort(np.concatenate([cid[:, [0, 1]], cid[:, [1, 2]], cid[:, [2, 0]]]), axis=1)
        edges, count = np.unique(e, axis=0, return_counts=True)
        z = np.zeros(int(cid.max()) + 1)
        z[cid.ravel()] = lod.points[lod.triangles.ravel(), 2]
        open_edges = edges[count == 1]
        on_a_rim = (np.abs(z[open_edges]) < 1e-9).all(axis=1) | (np.abs(z[open_edges] - 1.0) < 1e-9).all(axis=1)
        assert on_a_rim.all(), f"level with {lod.tri_count} triangles has {int((~on_a_rim).sum())} open edges along the seam"


# --------------------------------------------------------------------------- engine hull limits


def _dense(kind: str, **params) -> MeshData:
    from antigravity3d.ir import NodeIR
    from antigravity3d.kernel.compile_ir import primitive_mesh

    p, t = primitive_mesh(NodeIR(id=kind, kind=kind, params=params))
    return MeshData(kind, f"/Asset/{kind}", np.asarray(p, np.float64), np.asarray(t, np.int32), "m")


@pytest.mark.parametrize("engine", ["unreal_5", "unity_urp", "godot_4"])
def test_hulls_over_an_engine_limit_are_replaced_by_enclosing_hulls_within_it(engine):
    """A dense convex part takes the fast path; its own hull (1,986 vertices for a 64x32 sphere) is over every PhysX and
    Jolt limit. The replacement is within the engine's limit as the validator counts it, contains the original hull,
    is no smaller, says why it differs, and is the same bit for bit on a second run."""
    from antigravity3d.kernel.fast_ops import CAPPED_MARK, hull_limit
    from antigravity3d.kernel.validator import hull_limit_issue

    for m in (_dense("sphere", radius=0.5, segments=64, rings=32), _dense("cylinder", radius=0.3, height=2.0, segments=256),
              _dense("capsule", radius=0.25, height=1.5, segments=48)):
        (orig,) = generate_colliders([m], 8, asset_name="P")
        assert hull_limit_issue(engine, orig.points, orig.triangles)  # the subject is over the limit
        (h,) = generate_colliders([m], 8, asset_name="P", limit=hull_limit(engine))
        again = generate_colliders([m], 8, asset_name="P", limit=hull_limit(engine))[0]
        assert np.array_equal(h.points, again.points) and np.array_equal(h.triangles, again.triangles) and h.reason == again.reason
        assert hull_limit_issue(engine, h.points, h.triangles) == "", m.node_id
        assert h.source == "fast_path" and h.reason.startswith(CAPPED_MARK)
        eq = ConvexHull(h.points).equations
        diag = float(np.linalg.norm(np.ptp(orig.points, axis=0)))
        assert (orig.points @ eq[:, :3].T + eq[:, 3]).max() <= 2e-6 * diag  # encloses (weld tolerance 1e-6 of the diagonal)
        assert orig.volume <= h.volume <= 1.08 * orig.volume
        assert trimesh.Trimesh(h.points, h.triangles).is_convex


def test_hulls_within_the_limit_and_engines_without_one_are_left_as_computed():
    from antigravity3d.kernel.fast_ops import hull_limit

    small = md(trimesh.creation.cylinder(0.3, 1, sections=32), "cyl")      # 64 vertices, 124 triangles
    dense = _dense("sphere", radius=0.5, segments=64, rings=32)
    assert hull_limit("roblox_r15") is None and hull_limit(None) is None
    for m, engine in ((small, "unity_urp"), (small, "godot_4"), (dense, "roblox_r15")):
        (a,) = generate_colliders([m], 8, limit=hull_limit(engine))
        (b,) = generate_colliders([m], 8)
        assert np.array_equal(a.points, b.points) and np.array_equal(a.triangles, b.triangles) and a.reason == b.reason == ""


def test_export_pipeline_caps_hulls_for_its_target_engine():
    from antigravity3d.kernel.fast_ops import CAPPED_MARK, describe_collider_sources

    m = _dense("sphere", radius=0.5, segments=64, rings=32)
    for engine, vertices in (("godot_4", 256), ("unity_urp", 129)):
        (h,) = run_export_pipeline([m], EngineBudget(engine), "Ball").colliders
        assert len(h.points) <= vertices and CAPPED_MARK in h.reason
        assert "capped to the engine's hull limit" in describe_collider_sources([h])
    assert len(run_export_pipeline([m], EngineBudget("roblox_r15"), "Ball").colliders[0].points) == 1986
