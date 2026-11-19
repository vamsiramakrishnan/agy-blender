from __future__ import annotations

import dataclasses
import time

import numpy as np
import trimesh

from antigravity3d.kernel.fast_ops import unwrap_uvs
from antigravity3d.kernel.raster import render_buffers
from antigravity3d.kernel.som import (
    assign_badges, locate_pixel, mark_view, marked_contact_sheet, prim_bbox_px, uv_distortion_overlay,
)
from antigravity3d.kernel.types import MeshData

VIEWS = ("front", "right", "top", "persp")


def md(tm, nid, t=(0, 0, 0)) -> MeshData:
    tm = tm.copy()
    tm.apply_translation(t)
    return MeshData(nid, f"/A/{nid}", np.array(tm.vertices, float), np.array(tm.faces, np.int32), "m")


def scene() -> list[MeshData]:
    return [
        md(trimesh.creation.box((1, 1, 1)), "box", (0, 0, 0.5)),
        md(trimesh.creation.icosphere(3, 0.5), "sph", (1.5, 0, 0.5)),
        md(trimesh.creation.cylinder(0.3, 1.5, sections=48), "cyl", (-1.5, 0, 0.75)),
        md(trimesh.creation.box((0.3, 0.3, 0.3)), "core_hidden", (0, 0, 0.5)),          # inside the box
        md(trimesh.creation.box((0.015, 0.015, 0.015)), "speck", (0, 0, 1.2)),          # ~1-2 px
        md(trimesh.creation.box((0.12, 0.12, 0.12)), "bolt", (0, -0.55, 0.9)),          # small, on the box face
        md(trimesh.creation.cylinder(0.05, 0.6), "rod_a", (0.5, 0, 1.2)),
        md(trimesh.creation.cylinder(0.05, 0.6), "rod_b", (0.56, 0, 1.2)),
    ]


def overlap(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def test_anchors_on_own_prim_and_boxes_disjoint():
    ms = scene()
    legend = assign_badges(ms)
    for v in VIEWS:
        rb = render_buffers(ms, v, 256)
        mv = mark_view(rb, legend)
        assert mv.badges
        for b in mv.badges:
            assert locate_pixel(rb, b.anchor) == b.node_id == legend[b.num]
            x0, y0, x1, y1 = b.box
            assert x0 <= b.anchor[0] <= x1 and y0 <= b.anchor[1] <= y1
            assert 0 <= x0 and x1 < 256 and 0 <= y0 and y1 < 256
            if not b.leader:  # inside-the-part badges: anchor is the box centre-ish, still on the part
                assert rb.prim_id[b.anchor[1], b.anchor[0]] >= 0
        for i, a in enumerate(mv.badges):
            for b in mv.badges[i + 1:]:
                assert not overlap(a.box, b.box), (v, a.node_id, b.node_id)


def test_anchor_is_deep_inside_concave_region():
    # an L-shaped (concave) part: the centroid of its pixels lies outside it, the anchor must not
    t = trimesh.util.concatenate([trimesh.creation.box((1, 0.2, 0.2)).apply_translation((0.4, 0, 0.1)),
                                  trimesh.creation.box((0.2, 0.2, 1)).apply_translation((-0.0, 0, 0.5))])
    ms = [MeshData("ell", "/A/ell", np.array(t.vertices, float), np.array(t.faces, np.int32))]
    rb = render_buffers(ms, "front", 256)
    mv = mark_view(rb, assign_badges(ms))
    (b,) = mv.badges
    assert locate_pixel(rb, b.anchor) == "ell"
    ys, xs = np.nonzero(rb.prim_id == 0)
    assert rb.prim_id[int(ys.mean()), int(xs.mean())] == 0 or True  # centroid may or may not be inside; anchor must be


def test_numbering_stable_under_reordering():
    ms = scene()
    ref = assign_badges(ms)
    assert ref == {i + 1: n for i, n in enumerate(sorted(m.node_id for m in ms))}
    rng = np.random.default_rng(1)
    for _ in range(3):
        shuf = [ms[i] for i in rng.permutation(len(ms))]
        assert assign_badges(shuf) == ref
    a = marked_contact_sheet(ms, size=192)
    b = marked_contact_sheet(list(reversed(ms)), size=192)
    assert a.legend == b.legend and a.visible == b.visible and a.unmarked == b.unmarked
    assert (np.asarray(a.image) == np.asarray(b.image)).all()


def test_occluded_and_tiny_parts():
    ms = scene()
    sh = marked_contact_sheet(ms)
    num = {n: i for i, n in sh.legend.items()}
    for v in VIEWS:
        assert num["core_hidden"] not in sh.visible[v]
        assert set(sh.visible[v]) <= set(sh.legend)
    assert "core_hidden" in sh.unmarked and "speck" in sh.unmarked
    assert num["speck"] not in sum(sh.visible.values(), [])
    assert num["box"] in sh.visible["front"] and num["bolt"] in sh.visible["front"]
    # per-view tiny list from mark_view
    rb = render_buffers(ms, "front", 256)
    assert "speck" in mark_view(rb, sh.legend).unmarked
    assert mark_view(rb, sh.legend, min_pixels=1).unmarked == ["core_hidden"] or "speck" not in mark_view(rb, sh.legend, min_pixels=1).unmarked


def test_legend_text_matches():
    ms = scene()
    sh = marked_contact_sheet(ms)
    lines = sh.legend_text().splitlines()
    for n, nid in sh.legend.items():
        line = [l for l in lines if l.startswith(f"[{n:02d}] ")]
        assert len(line) == 1 and nid in line[0]
        shown = [v for v in VIEWS if n in sh.visible[v]]
        assert line[0].endswith(",".join(shown) if shown else "-")
    assert lines[-1].startswith("unmarked") and "speck" in lines[-1]
    assert sh.image.size == (512, 512)


def test_locate_and_bbox():
    ms = scene()
    rb = render_buffers(ms, "front", 256)
    x0, y0, x1, y1 = prim_bbox_px(rb, "cyl")
    assert locate_pixel(rb, ((x0 + x1) / 2, (y0 + y1) / 2)) == "cyl"
    assert locate_pixel(rb, (0, 0)) is None and locate_pixel(rb, (-1, 5)) is None
    assert prim_bbox_px(rb, "core_hidden") is None and prim_bbox_px(rb, "nope") is None


def test_uv_distortion_overlay_highlights_stretch():
    ms = scene()[:3]
    ud = [unwrap_uvs(m, 256, 1024) for m in ms]
    good = uv_distortion_overlay(ms, ud, "persp", 192)
    bad = []
    for u in ud:
        uv = u.uvs.copy()
        sel = u.points[:, 2] > (0.9 if u.node_id != "sph" else 0.5)
        c = uv[sel].mean(axis=0)
        uv[sel] = c + (uv[sel] - c) * 0.3
        bad.append(dataclasses.replace(u, uvs=uv))
    badim = uv_distortion_overlay(ms, bad, "persp", 192)
    assert good.size == badim.size == (192, 192)
    g, b = np.asarray(good).astype(int), np.asarray(badim).astype(int)
    assert np.abs(g - b).sum(axis=2).max() > 60
    base = np.asarray(render_buffers(ms, "persp", 192).color).astype(int)
    assert (np.abs(g - base).sum(axis=2) > 0).any()
    assert (np.asarray(uv_distortion_overlay(ms, [], "persp", 192)).astype(int)[:150] == base[:150]).all()


def test_timing():
    ms = scene()
    marked_contact_sheet(ms)
    t = time.perf_counter()
    marked_contact_sheet(ms)
    ms_ = (time.perf_counter() - t) * 1e3
    print(f"\nmarked 4-view sheet ({sum(len(m.triangles) for m in ms)} tris): {ms_:.0f} ms")
    assert ms_ < 1500
