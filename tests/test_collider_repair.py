"""Collider input that is not one closed manifold body: repaired privately, decomposed where that is bounded, and stopped by a
deadline where it is not (CoACD's own repair pass took minutes on a sphere missing four faces)."""
from __future__ import annotations

import os
import sys
import textwrap
import time
import uuid

import numpy as np
import pytest
import trimesh
from scipy.spatial import ConvexHull

from antigravity3d.kernel import collider_worker, fast_ops
from antigravity3d.kernel.collider_repair import repair_collider_mesh
from antigravity3d.kernel.fast_ops import describe_collider_sources, generate_colliders
from antigravity3d.kernel.types import MeshData

FACES = {(1, 0, 0): [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)], (-1, 0, 0): [(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)],
         (0, 1, 0): [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)], (0, -1, 0): [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)],
         (0, 0, 1): [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], (0, 0, -1): [(0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)]}


def voxels(filled, size=1.0):
    filled = set(filled)
    vid: dict = {}
    tris = []
    for (i, j, k) in sorted(filled):
        for d, cs in FACES.items():
            if (i + d[0], j + d[1], k + d[2]) in filled:
                continue
            q = [vid.setdefault((i + a, j + b, k + c), len(vid)) for a, b, c in cs]
            tris += [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
    return np.array(sorted(vid, key=vid.get), float) * size, np.array(tris)


def mesh(name, pts, tris) -> MeshData:
    return MeshData(name, f"/A/{name}", np.asarray(pts, np.float64), np.asarray(tris, np.int32), "m")


def sphere(count, faces_removed):
    s = trimesh.creation.uv_sphere(0.4, count=list(count))
    return np.asarray(s.vertices), np.asarray(s.faces)[:len(s.faces) - faces_removed]


def open_cup():
    c = trimesh.creation.cylinder(0.3, 0.8, sections=24)
    v, f = np.asarray(c.vertices), np.asarray(c.faces)
    return v, f[~(v[f][:, :, 2] > 0.39).all(axis=1)]


def covered(hulls, pts, tol=1e-3):
    pts = np.asarray(pts)
    ok = np.zeros(len(pts), bool)
    for h in hulls:
        eq = ConvexHull(h.points).equations
        ok |= (pts @ eq[:, :3].T + eq[:, 3] <= tol).all(axis=1)
    return ok.all()


def _tagged(tag: str) -> list[int]:
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/environ", "rb") as f:
                env = f.read()
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue
        if f"AGY3D_PROC_TAG={tag}".encode() in env.split(b"\0") and stat[stat.rindex(b")") + 2:][:1] != b"Z":
            out.append(int(name))
    return out


@pytest.fixture
def tag(monkeypatch):
    t = uuid.uuid4().hex
    monkeypatch.setenv("AGY3D_PROC_TAG", t)
    yield t
    assert _tagged(t) == []  # no child outlives its call, whatever happened to it


# ------------------------------------------------------------------ repair rules


def test_bodies_that_touch_are_pulled_apart():
    for cells in ([(0, 0, 0), (1, 1, 0)], [(0, 0, 0), (1, 1, 1)]):  # along an edge, at a vertex
        p, t = voxels(cells)
        rep = repair_collider_mesh(p, t)
        assert [b.kind for b in rep.bodies] == ["closed", "closed"]
        assert sum(len(b.triangles) for b in rep.bodies) == len(t)


def test_duplicate_flipped_and_unwelded_faces_are_repaired():
    p, t = voxels([(i, j, k) for i in range(4) for j in range(4) for k in range(2) if i < 2 or j < 2])
    flipped = t.copy()
    flipped[[3, 20]] = flipped[[3, 20]][:, ::-1]
    messy = np.vstack([flipped, t[[5, 6]]])
    rep = repair_collider_mesh(p, messy)
    assert rep.notes["duplicate"] == 2 and rep.notes["flipped"] == 2
    assert [b.kind for b in rep.bodies] == ["closed"] and len(rep.bodies[0].triangles) == len(t)
    soup = repair_collider_mesh(p[t.reshape(-1)], np.arange(len(t) * 3).reshape(-1, 3))
    assert [b.kind for b in soup.bodies] == ["closed"] and soup.notes["welded"] > 0
    assert rep.bodies[0].volume == pytest.approx(soup.bodies[0].volume)


def test_a_small_hole_is_closed_and_a_mouth_is_not():
    small = repair_collider_mesh(*sphere((24, 12), 4))
    assert [b.kind for b in small.bodies] == ["closed"] and small.notes["capped"] == 1
    large = repair_collider_mesh(*sphere((24, 12), 200))
    assert [b.kind for b in large.bodies] == ["open"]
    cup = repair_collider_mesh(*open_cup())
    assert [b.kind for b in cup.bodies] == ["open"]


def test_the_rendered_mesh_is_not_changed():
    p, t = sphere((10, 6), 6)
    p0, t0 = p.copy(), t.copy()
    repair_collider_mesh(p, t)
    assert np.array_equal(p, p0) and np.array_equal(t, t0)


# ------------------------------------------------------------------ the inputs that ran away


@pytest.mark.parametrize("make, expect", [
    (lambda: voxels([(0, 0, 0), (1, 1, 0)]), {"fast_path"}),               # two cubes sharing an edge: 5.9 s through the repair pass
    (lambda: voxels([(0, 0, 0), (1, 1, 1)]), {"fast_path"}),
    (lambda: sphere((10, 6), 6), {"fast_path"}),                           # 186 triangles, 103 s through the repair pass
    (lambda: sphere((24, 12), 4), {"fast_path"}),
])
def test_closed_by_repair_inputs_finish_at_once_and_cover(make, expect):
    p, t = make()
    t0 = time.perf_counter()
    hulls = generate_colliders([mesh("m", p, t)], 8)
    assert time.perf_counter() - t0 < 5.0
    assert {h.source for h in hulls} == expect and covered(hulls, p)
    assert all(h.reason.startswith("repaired input") for h in hulls) or len(hulls) == 2


def test_a_surface_that_still_touches_itself_gets_the_hull_of_its_body_and_says_so():
    # a blob of voxels whose bodies meet along edges in a ring: pulling them apart at an edge leaves them joined at another
    cells = [(0, 0, 0), (0, 0, 2), (0, 1, 2), (0, 2, 0), (0, 2, 1), (0, 2, 2), (1, 0, 1), (1, 0, 2), (1, 1, 1), (1, 2, 0), (1, 2, 1),
             (1, 2, 2), (2, 1, 0), (2, 1, 1)]
    p, t = voxels(cells)
    assert "pinched" in [b.kind for b in repair_collider_mesh(p, t).bodies]
    t0 = time.perf_counter()
    hulls = generate_colliders([mesh("m", p, t)], 8)
    assert time.perf_counter() - t0 < 5.0
    pinched = [h for h in hulls if h.source == "body_hull"]
    assert pinched and all("touches itself" in h.reason for h in pinched)
    assert covered(hulls, p)


def test_an_open_surface_is_made_solid_decomposed_under_a_deadline_and_covered(tag):
    p, t = open_cup()
    t0 = time.perf_counter()
    hulls = generate_colliders([mesh("cup", p, t)], 8)
    assert time.perf_counter() - t0 < fast_ops.decomposition_budget_s(len(t))
    assert len(hulls) >= 2 and all(h.source == "coacd" for h in hulls) and all("made solid" in h.reason for h in hulls)
    assert covered(hulls, p[np.unique(t)], tol=0.01)  # CoACD's hulls of the solid give up up to 1% of the extent at the surface
    again = generate_colliders([mesh("cup", p, t)], 8)
    assert all(np.array_equal(a.points, b.points) for a, b in zip(hulls, again)) and len(hulls) == len(again)


def test_a_flat_sheet_gets_a_hull_with_thickness():
    n = 6
    xs = np.linspace(0, 1, n)
    X, Y = np.meshgrid(xs, xs)
    p = np.c_[X.ravel(), Y.ravel(), np.zeros(n * n)]
    t = np.array([f for i in range(n - 1) for j in range(n - 1) for a in [i * n + j] for f in ((a, a + 1, a + n), (a + 1, a + n + 1, a + n))])
    hulls = generate_colliders([mesh("sheet", p, t)], 8)
    assert hulls and covered(hulls, p) and all(h.volume > 0 for h in hulls)


# ------------------------------------------------------------------ the deadline


def _script(tmp_path, body: str):
    f = tmp_path / "child.py"
    f.write_text(textwrap.dedent(body))
    return f


def test_a_child_that_never_returns_is_killed_and_a_hull_stands_in(tmp_path, monkeypatch, tag):
    monkeypatch.setattr(collider_worker, "_SCRIPT", _script(tmp_path, "import time\ntime.sleep(3600)\n"))
    monkeypatch.setattr(fast_ops, "decomposition_budget_s", lambda n: 1.0)
    p, t = open_cup()
    t0 = time.perf_counter()
    hulls = generate_colliders([mesh("cup", p, t)], 8)
    assert time.perf_counter() - t0 < 10
    assert [h.source for h in hulls] == ["body_hull"]
    assert "decomposition stopped: timeout after 1 s" in hulls[0].reason
    assert covered(hulls, p)
    assert "body_hull" in describe_collider_sources(hulls) and "timeout" in describe_collider_sources(hulls)


def test_a_child_that_crashes_is_a_result_not_a_crash(tmp_path, monkeypatch, tag):
    monkeypatch.setattr(collider_worker, "_SCRIPT", _script(tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)\n"))
    p, t = open_cup()
    hulls = generate_colliders([mesh("cup", p, t)], 8)
    assert [h.source for h in hulls] == ["body_hull"] and "crash (signal 11)" in hulls[0].reason


def test_a_child_that_has_children_leaves_none_behind(tmp_path, monkeypatch, tag):
    body = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'])\ntime.sleep(3600)\n"
    monkeypatch.setattr(collider_worker, "_SCRIPT", _script(tmp_path, body))
    out = collider_worker.run_in_child(np.zeros((4, 3)), np.zeros((1, 3), np.int32), {}, 1.0)
    assert out.parts is None and out.reason.startswith("timeout")


def test_a_clean_closed_body_does_not_start_a_process(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a child process was started")

    monkeypatch.setattr(collider_worker, "run_in_child", refuse)
    p, t = voxels([(i, j, k) for i in range(8) for j in range(6) for k in range(4) if j < 2 or i < 2 or i >= 6])
    hulls = generate_colliders([mesh("u", p, t)], 8)
    assert {h.source for h in hulls} == {"coacd"} and all(h.reason == "" for h in hulls)


def test_a_very_large_clean_body_runs_under_a_deadline(monkeypatch, tag):
    p, t = voxels([(i, j, k) for i in range(8) for j in range(6) for k in range(4) if j < 2 or i < 2 or i >= 6])
    seen = []
    real = collider_worker.run_in_child

    def spy(points, triangles, params, timeout_s):
        seen.append((len(triangles), timeout_s))
        return real(points, triangles, params, timeout_s)

    monkeypatch.setattr(collider_worker, "run_in_child", spy)
    monkeypatch.setattr(fast_ops, "IN_PROCESS_MAX_TRIANGLES", 10)
    hulls = generate_colliders([mesh("u", p, t)], 8)
    assert len(seen) == 1 and seen[0][1] == fast_ops.decomposition_budget_s(seen[0][0])
    assert {h.source for h in hulls} == {"coacd"}


def test_the_child_matches_the_process_for_the_same_mesh(tag):
    p, t = voxels([(i, j, k) for i in range(6) for j in range(5) for k in range(3) if j < 2 or i < 2])
    import coacd

    prm = fast_ops._coacd_params(0.05, 4, True)
    inproc = coacd.run_coacd(coacd.Mesh(p, t.astype(np.int32)), **prm)
    out = collider_worker.run_in_child(p, t, prm, 60)
    assert out.parts is not None and len(out.parts) == len(inproc)
    assert all(np.array_equal(a[0], b[0]) for a, b in zip(out.parts, inproc))


def test_fallback_reasons_survive_the_export_cache():
    from antigravity3d.tactics import cached

    h = fast_ops.Hull("UCX_A_00", np.eye(4, 3), np.array([[0, 1, 2]], np.int32), 1.0, "body_hull", "decomposition stopped: timeout after 30 s")
    b = fast_ops.ExportBundle("A", [], {}, [h], {})
    back = cached._bundle_from_bytes(cached._bundle_to_bytes(b))
    assert back.colliders[0].source == "body_hull" and back.colliders[0].reason == h.reason
