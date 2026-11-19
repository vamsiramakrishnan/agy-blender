from __future__ import annotations

import dataclasses
import json
import time

import numpy as np
import pytest
import trimesh

from antigravity3d.kernel.fast_ops import UVResult
from antigravity3d.kernel.trim_atlas import (
    BYTES_PER_PX_BC7, BYTES_PER_PX_RGBA8, MIP_FACTOR, TrimSheet, TrimStrip, check_no_flips, check_strip_bounds,
    map_to_trim, validate_trim,
)
from antigravity3d.kernel.types import MeshData


def md(tm, nid="m") -> MeshData:
    return MeshData(nid, f"/A/{nid}", np.array(tm.vertices, float), np.array(tm.faces, np.int32), "m")


SHEET = TrimSheet.standard(4096)
TOL = 0.02   # padding shrinks a chart that exactly fills a strip by ~1%


def test_standard_sheet_layout_and_json():
    s = SHEET
    assert abs(sum(x.v1 - x.v0 for x in s.strips) - 1.0) < 1e-9
    top = sorted(s.strips, key=lambda x: -x.v1)
    assert all(abs(a.v0 - b.v1) < 1e-9 for a, b in zip(top[1:], top)) and abs(top[0].v1 - 1) < 1e-9 and top[-1].v0 == 0
    assert TrimSheet.from_json(s.to_json()) == s
    assert json.loads(s.to_json())["resolution"] == 4096
    p = s.layout_prompt()
    assert "y=0..1023" in p and "painted_metal" in p and "rivet_band" in p and "y=1024..1279" in p
    assert p.index("painted_metal") < p.index("rivet_band") < p.index("wood_planks")
    with pytest.raises(ValueError):
        TrimStrip("bad", 0.5, 0.4, world_height_m=1.0)


def test_box_and_cylinder_stay_in_strips_without_flips():
    box = md(trimesh.creation.box((1, 1, 1)), "box")
    r = map_to_trim(box, SHEET, {"+z": "wood_planks", "-z": "worn_edge", "*": "painted_metal"})
    assert isinstance(r, UVResult) and r.uvs.dtype == np.float32 and len(r.report.charts) == 6
    assert validate_trim(r, SHEET) == []
    wood = SHEET.strip("wood_planks")
    sel = r.tri_strip == SHEET.strips.index(wood)
    v = r.uvs[r.triangles[sel]][..., 1]
    assert sel.sum() == 2 and v.min() >= wood.v0 and v.max() <= wood.v1
    assert r.report.mapped_area_fraction == pytest.approx(1.0)
    # worn_edge is 0.125 m tall: a 1 m face is scaled down to fit, and that is reported
    worn = [c for c in r.report.charts if c.strip == "worn_edge"][0]
    assert worn.scale < 0.2 and worn.density_dev == pytest.approx(worn.scale - 1, abs=0.02)

    cyl = md(trimesh.creation.cylinder(0.15, 0.8, sections=48), "cyl")
    rc = map_to_trim(cyl, SHEET, {"side": "painted_metal", "*": "rubber_grip"})
    assert validate_trim(rc, SHEET) == []
    kinds = {c.kind for c in rc.report.charts}
    assert kinds == {"cylinder", "planar"} and len(rc.report.charts) == 3
    side = [c for c in rc.report.charts if c.kind == "cylinder"][0]
    assert side.strip == "painted_metal" and side.scale == 1.0 and abs(side.density_dev) < TOL
    # seam-split: more vertices than the source, vmapping points back at identical positions
    assert len(rc.points) > len(cyl.points) and np.allclose(rc.points, cyl.points[rc.vmapping])
    # circumference 0.94 m at 1024 px/m over a 4 m sheet -> ~0.2356 U
    s = rc.uvs[rc.triangles[rc.tri_strip == SHEET.strips.index(SHEET.strip("painted_metal"))]][..., 0]
    assert s.max() - s.min() == pytest.approx(2 * np.pi * 0.15 / 4.0, rel=0.02)


def test_texel_density_consistent_across_strips():
    box = md(trimesh.creation.box((0.6, 0.4, 0.3)), "b")
    r = map_to_trim(box, SHEET, {"+z": "wood_planks", "-z": "painted_metal", "*": "concrete"})
    for c in r.report.charts:
        assert abs(c.density_dev) < TOL, c
    assert r.report.max_density_dev < TOL
    assert r.texel_density == pytest.approx(1024.0, rel=TOL)
    assert r.stretch_mean < 0.05


def test_u_tiles_for_long_charts_and_tile_flag():
    beam = md(trimesh.creation.box((10, 0.2, 0.2)), "beam")
    r = map_to_trim(beam, SHEET, "painted_metal")
    assert validate_trim(r, SHEET) == []
    assert r.uvs[:, 0].max() == pytest.approx(10 / 4.0, rel=1e-3) and r.uvs[:, 0].max() > 2
    assert r.report.max_density_dev < TOL
    sheet = TrimSheet(1024, [TrimStrip("decal", 0.0, 0.5, tile_u=False, world_height_m=0.5), TrimStrip("rest", 0.5, 1.0, world_height_m=0.5)])
    r2 = map_to_trim(beam, sheet, "decal")
    assert r2.uvs[:, 0].max() <= 1.0 + 1e-6 and check_strip_bounds(r2, sheet) == []
    assert not r2.fitted and any(c.scale < 1 for c in r2.report.charts)   # shrunk to fit, reported


def test_assignment_forms_and_unmapped_fraction():
    box = md(trimesh.creation.box((1, 1, 1)), "box")
    by_fn = map_to_trim(box, SHEET, lambda ch: "wood_planks" if ch.normal[2] > 0.5 else None)
    assert by_fn.report.mapped_area_fraction == pytest.approx(1 / 6)
    assert (by_fn.tri_strip >= 0).sum() == 2 and (by_fn.tri_strip < 0).sum() == 10
    assert validate_trim(by_fn, SHEET) == []
    by_idx = map_to_trim(box, SHEET, {0: "concrete"})
    assert by_idx.report.strip_usage["concrete"]["charts"] == 1
    with pytest.raises(KeyError):
        map_to_trim(box, SHEET, "nope")


def test_validators_catch_violations():
    box = md(trimesh.creation.box((1, 1, 1)), "box")
    r = map_to_trim(box, SHEET, "painted_metal")
    bad = r.uvs.copy()
    bad[:, 1] += 0.5
    assert check_strip_bounds(dataclasses.replace(r, uvs=bad), SHEET)
    tri = r.triangles.copy()
    tri[0] = tri[0][::-1]
    assert check_no_flips(dataclasses.replace(r, triangles=tri))


def test_vram_arithmetic_hand_computed():
    r = map_to_trim(md(trimesh.creation.box((1, 1, 1))), SHEET, "painted_metal")
    assert (BYTES_PER_PX_RGBA8, BYTES_PER_PX_BC7, MIP_FACTOR) == (4.0, 1.0, 4 / 3)
    e = r.report.vram_estimate(100, unique_res=2048, maps=3)
    # unique: 100 props * 3 maps * 2048^2 px * 4 B * 4/3 = 6,710,886,400 ; shared: 3 * 4096^2 * 4 * 4/3 = 268,435,456
    assert e.unique_bytes == pytest.approx(6_710_886_400)
    assert e.shared_bytes == pytest.approx(268_435_456)
    assert e.ratio == pytest.approx(25.0) and e.saved_bytes == pytest.approx(6_442_450_944)
    c = r.report.vram_estimate(100, compressed=True)
    assert c.shared_bytes == pytest.approx(67_108_864) and c.ratio == pytest.approx(25.0)
    assert r.report.vram_estimate(1).ratio == pytest.approx(0.25)   # one prop: the 4K sheet is bigger


def test_timing():
    cyl = md(trimesh.creation.cylinder(0.3, 1.0, sections=128), "c")
    map_to_trim(cyl, SHEET, "painted_metal")
    t = time.perf_counter()
    map_to_trim(cyl, SHEET, "painted_metal")
    ms = (time.perf_counter() - t) * 1e3
    print(f"\ntrim mapping {len(cyl.triangles)} tris: {ms:.1f} ms")
    assert ms < 500
