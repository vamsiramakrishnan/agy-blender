from __future__ import annotations

import time

import numpy as np
import pytest
import trimesh
from scipy.spatial import ConvexHull

from antigravity3d.kernel.fast_ops import (
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
