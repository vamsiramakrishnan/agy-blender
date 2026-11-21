"""What relies on authored parts being closed, now that an open mesh is only a warning on three engines: each consumer
of closedness, with an open part. (tier1_qa itself: tests/test_fast_ops.py.)"""

from __future__ import annotations

import numpy as np
import trimesh

from antigravity3d.kernel.fast_ops import (
    _SurfaceIndex, _topology, generate_asset_lods, generate_colliders, hidden_contact_faces, tier1_qa, unwrap_uvs,
)
from antigravity3d.kernel.lod_diff import lod_error_budget_m
from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EngineBudget


def part(tm: trimesh.Trimesh, nid: str, at=(0, 0, 0)) -> MeshData:
    tm = tm.copy()
    tm.apply_translation(at)
    return MeshData(nid, f"/Asset/{nid}", np.array(tm.vertices, np.float64), np.array(tm.faces, np.int32), "m")


def opened(tm: trimesh.Trimesh, drop: int = 0) -> trimesh.Trimesh:
    keep = np.arange(len(tm.faces)) != drop
    return trimesh.Trimesh(tm.vertices, tm.faces[keep], process=False)


def hidden(parts: list[MeshData]) -> list[int]:
    masks = hidden_contact_faces([(p.node_id, p.points, p.triangles, "m") for p in parts], frozenset({"m"}))
    return [int(m.sum()) for m in masks]


def table_top_and_leg(open_leg: bool) -> list[MeshData]:
    top = trimesh.creation.box((1.0, 1.0, 0.1))
    leg = trimesh.creation.box((0.1, 0.1, 0.7))
    if open_leg:  # drop one side triangle of the leg (not the one that rests on the top)
        side = int(np.argmax(np.abs(leg.face_normals[:, 0]) > 0.9))
        leg = opened(leg, side)
    return [part(top, "top", (0, 0, 0.75)), part(leg, "leg", (0.3, 0.3, 0.35))]


def test_hidden_contact_faces_need_both_parts_closed():
    """The leg's top face is pressed against the top's underside. Closed: both faces go (the leg's whole top face: two
    triangles; the top's underside is bigger than the leg and keeps its triangles, a face is dropped only when covered
    completely). Open leg: nothing is removed from either part, the open part neither loses nor hides a face."""
    closed = hidden(table_top_and_leg(False))
    assert closed == [0, 2], closed
    assert hidden(table_top_and_leg(True)) == [0, 0]
    # an open part under a closed one: the closed part's covered face is not removed (the cover is a sheet, nothing behind)
    slab = part(trimesh.creation.box((0.4, 0.4, 0.1)), "slab", (0, 0, 0.05))
    lid = part(opened(trimesh.creation.box((0.4, 0.4, 0.1)), 0), "lid", (0, 0, 0.15))
    assert hidden([slab, lid]) == [0, 0]


def test_open_part_keeps_its_triangles_in_the_shipped_geometry():
    from antigravity3d.kernel.fast_ops import run_export_pipeline

    def removed(parts):
        bundle = run_export_pipeline(parts, EngineBudget("unreal_5"), "Open", opaque_materials=frozenset({"m"}))
        return sum(int(u.hidden.sum()) for u in bundle.uvs if u.hidden is not None), bundle

    assert removed(table_top_and_leg(False))[0] == 2          # the closed pair: the leg's pressed face goes
    n, bundle = removed(table_top_and_leg(True))              # the leg open: nothing goes, every authored triangle ships
    assert n == 0 and {u.node_id: len(u.triangles) for u in bundle.uvs} == {p.node_id: len(p.triangles) for p in table_top_and_leg(True)}


def test_collider_of_an_open_part_is_made_solid_and_covers_it():
    cup = trimesh.creation.icosphere(3, 0.5)
    cup = trimesh.Trimesh(cup.vertices, cup.faces[cup.triangles_center[:, 2] > 0.0], process=True)
    cup.apply_translation([0, 0, 0.5])
    m = part(cup, "cup")
    assert _topology(m.points, m.triangles).boundary > 0
    hulls = generate_colliders([m], 4, asset_name="Cup")
    assert hulls and all("open surface made solid" in h.reason for h in hulls)
    hp = np.vstack([h.points for h in hulls])
    slack = 0.01 * float(np.ptp(m.points, axis=0).max())  # the hulls of a made-solid surface cover it to within a percent of its size
    assert (hp.min(0) <= m.points.min(0) + slack).all() and (hp.max(0) >= m.points.max(0) - slack).all()


def test_lod_levels_of_an_open_part_keep_the_rim_within_the_error_budget():
    """The simplifier does not freeze an open border (the measured error bound protects it): the rim moves, by no more
    than the level's budget."""
    s = trimesh.creation.icosphere(4, 0.5)
    cup = trimesh.Trimesh(s.vertices, s.faces[s.triangles_center[:, 2] > 0.0], process=True)
    cup.apply_translation([0, 0, 0.5])
    u = unwrap_uvs(part(cup, "cup"), 512, 2048)
    lods = generate_asset_lods([u])["cup"]
    assert len(lods) >= 3
    t = np.asarray(u.triangles)
    e = np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
    keys, count = np.unique(e, axis=0, return_counts=True)
    rim = np.unique(keys[count == 1])
    radius = np.linalg.norm(np.ptp(u.points, axis=0)) / 2
    for level in lods[1:]:
        away = _SurfaceIndex(level.points, level.triangles).distances(np.asarray(u.points)[rim], 0.0).max()
        assert away <= lod_error_budget_m(radius, level.screen_fraction) + 1e-9
        assert _topology(level.points, level.triangles).boundary > 0  # still open, not capped or closed


def test_tier1_reports_the_open_part_and_still_sees_what_it_can():
    budget = EngineBudget("godot_4")
    base = part(trimesh.creation.box((1, 1, 1)), "base", (0, 0, 0.5))
    card = part(opened(trimesh.creation.box((0.2, 0.2, 0.2)), 0), "card", (0, 0, 1.1))   # rests on the base, open
    rep = tier1_qa([base, card], budget)
    assert rep.ok and {i.code for i in rep.issues} == {"not_watertight"}       # touching by surface: not floating
    # an open part cannot be judged inside out (it has no inside); it is only probed as the *other* part of a pair, by the
    # winding number of its sheet, so a penetration through a hole-free side is still seen. The warning stays.
    # (a part wholly inside another, open or not, is an enclosed_part: tests/test_fast_ops.py)
    sunk = part(opened(trimesh.creation.box((0.2, 0.2, 0.2)), 0), "card", (0, 0, 0.95))  # through the top face
    got = {i.code for i in tier1_qa([base, sunk], budget).issues}
    assert "not_watertight" in got and not got & {"floating_part", "enclosed_part", "inverted_normals"}
    flipped = opened(trimesh.creation.box((0.2, 0.2, 0.2)), 0); flipped.invert()
    assert "inverted_normals" not in {i.code for i in tier1_qa([base, part(flipped, "card", (0, 0, 1.1))], budget).issues}
    # a sheet pressed flat onto a closed part still z-fights (same-facing faces in one plane)
    sheet = part(trimesh.Trimesh(np.array([[-.3, -.3, 1.0], [.3, -.3, 1.0], [.3, .3, 1.0], [-.3, .3, 1.0]]), np.array([[0, 1, 2], [0, 2, 3]]), process=False), "decal")
    assert "z_fighting" in {i.code for i in tier1_qa([base, sheet], budget).issues}


def test_sanitize_already_reorients_each_body_and_its_report_now_lists_it_as_fixed():
    """`agy3d sanitize` re-orients per connected component (dags/batch_sanitize.orient_faces): the inside-out small body
    of a two-body import is repaired. What changes is the report: tier1_qa now sees it before the repair, so
    `issues_before` carries inverted_normals and `issues_fixed` lists it (it used to pass unnoticed and silently fixed)."""
    from antigravity3d.dags.batch_sanitize import _tokens, repair_mesh

    big = trimesh.creation.box((1, 1, 1))
    small = trimesh.creation.box((0.2, 0.2, 0.2))
    small.apply_translation([2, 0, 0])
    small.invert()
    mesh = MeshData("Two", "", np.vstack([big.vertices, small.vertices]) + [0, 0, 0.5],
                    np.vstack([big.faces, small.faces + len(big.vertices)]).astype(np.int32), "m")
    budget = EngineBudget("unreal_5")
    before = tier1_qa([mesh], budget)
    fixed, counts = repair_mesh(mesh)
    after = tier1_qa([fixed], budget)
    assert "inverted_normals@Two" in _tokens(before) and "inverted_normals@Two" not in _tokens(after)
    assert counts["reoriented_faces"] == 12 and after.ok  # the small body's twelve triangles
    assert "inverted_normals@Two" in (_tokens(before) - _tokens(after))  # what the row's issues_fixed is made of
