"""Compiles a :class:`SceneIR` into USD opinions on the current edit target.

Authoring is a *delta* against the scene IR already composed on the root prim
(``customData["agy:sceneIR"]``): only new or changed nodes/materials are
written, and composed prims that the new IR no longer contains are
deactivated in the edit target. Rolling back is therefore just dropping the
layer that holds those opinions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
from pxr import Gf, Kind, Sdf, Usd, UsdGeom, UsdShade, Vt

from antigravity3d.ir import MaterialIR, NodeIR, PlaceRelative, RunIR, SceneIR, Transform
from antigravity3d.kernel import shapes
from antigravity3d.kernel.mesh_io import triangle_parts

IR_KEY = "agy:sceneIR"  # customData key path (USD stores it as dict agy -> sceneIR)
LOOKS = "Looks"
REF_CHILD = "ref"  # child prim of a mesh_ref node that carries the USD reference
# Kinds compiled to a mesh from their parameters (the rest: mesh_ref reads a file, group has no geometry).
PRIMITIVES = frozenset({"box", "cylinder", "cone", "sphere", "capsule", "lathe", "extrude"})
USD_EXTS = frozenset({".usd", ".usda", ".usdc", ".usdz"})
_MESH_ATTRS = ("points", "faceVertexCounts", "faceVertexIndices", "extent")

# --- primitive meshes ---------------------------------------------------------


def _num(node: NodeIR, name: str, default: float | None = None, *, allow_zero: bool = False) -> float:
    v = node.params.get(name, default)
    if v is None:
        raise ValueError(f"{node.id}: {node.kind} requires param '{name}'")
    if isinstance(v, (str, tuple, list, bool)):
        raise ValueError(f"{node.id}: param '{name}' must be a number, got {v!r}")  # noqa: TRY004
    v = float(v)
    if not math.isfinite(v) or v < 0 or (v == 0 and not allow_zero):
        raise ValueError(f"{node.id}: param '{name}' must be {'>= 0' if allow_zero else '> 0'}, got {v}")
    return v


def _int(node: NodeIR, name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    v = node.params.get(name, default)
    if isinstance(v, (str, tuple, list, bool)) or float(v) != int(v) or int(v) < minimum or (maximum is not None and int(v) > maximum):
        within = f"from {minimum} to {maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{node.id}: param '{name}' must be an integer {within}, got {v!r}")
    return int(v)


def _faces(node: NodeIR) -> list[str]:
    v = node.params.get("inset_faces", [])
    if not isinstance(v, (list, tuple)) or not all(isinstance(f, str) for f in v):
        raise ValueError(f"{node.id}: param 'inset_faces' must be a list of face names {list(shapes.BOX_FACES)}, got {v!r}")
    return list(v)


def _box_shape(node: NodeIR) -> tuple | None:
    """(size, bevel, bevel_segments, inset_faces, inset_margin, inset_depth) of a box that is not a plain sharp box."""
    size = _box_size(node)
    b = _num(node, "bevel", 0.0, allow_zero=True)
    segs = _int(node, "bevel_segments", 1, 1, 4)
    faces = _faces(node)
    m = _num(node, "inset_margin", 0.0, allow_zero=True)
    d = _num(node, "inset_depth", 0.0, allow_zero=True)
    if b == 0 and not faces:
        return None
    return size, b, segs, faces, m, d


def _closed(node: NodeIR) -> bool:
    v = node.params.get("closed", False)
    if not isinstance(v, bool):
        raise ValueError(f"{node.id}: param 'closed' must be true or false, got {v!r}")
    return v


def _lathe(profile: list[tuple[float, float]], n: int, closed: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Surface of revolution about Z from an (r, z) profile running clockwise in the (r, z) plane (top to bottom on
    the outside of a solid); ``closed`` also joins the last row to the first (a tube or ring).

    Rows with r == 0 collapse to a single pole vertex, so the result is a
    closed manifold with shared vertices (no seam duplicates).
    """
    ang = np.arange(n) * (2.0 * np.pi / n)
    cos, sin = np.cos(ang), np.sin(ang)
    pts: list[np.ndarray] = []
    rows: list[np.ndarray] = []  # vertex indices per row; length 1 for a pole
    count = 0
    for r, z in profile:
        if r == 0.0:
            pts.append(np.array([[0.0, 0.0, z]]))
            rows.append(np.array([count]))
            count += 1
        else:
            pts.append(np.column_stack([r * cos, r * sin, np.full(n, z)]))
            rows.append(np.arange(count, count + n))
            count += n
    tris: list[np.ndarray] = []
    for a, b in pairwise(rows + rows[:1] if closed else rows):
        a1, b1 = np.roll(a, -1), np.roll(b, -1)
        if len(a) == 1:  # pole above ring: fan
            tris.append(np.column_stack([np.full(n, a[0]), b, b1]))
        elif len(b) == 1:  # ring above pole: fan
            tris.append(np.column_stack([a, np.full(n, b[0]), a1]))
        else:  # ring above ring: quads, CCW seen from outside
            tris.append(np.column_stack([a, b, b1]))
            tris.append(np.column_stack([a, b1, a1]))
    return np.vstack(pts).astype(np.float32), np.vstack(tris).astype(np.int32)


def _box(sx: float, sy: float, sz: float) -> tuple[np.ndarray, np.ndarray]:
    x, y, z = sx / 2, sy / 2, sz / 2
    # fmt: off
    pts = np.array(
        [[-x, -y, -z], [x, -y, -z], [x, y, -z], [-x, y, -z],
         [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]],
        dtype=np.float32,
    )
    tris = np.array(
        [[0, 2, 1], [0, 3, 2],  # -Z
         [4, 5, 6], [4, 6, 7],  # +Z
         [0, 1, 5], [0, 5, 4],  # -Y
         [2, 3, 7], [2, 7, 6],  # +Y
         [1, 2, 6], [1, 6, 5],  # +X
         [3, 0, 4], [3, 4, 7]],  # -X
        dtype=np.int32,
    )
    # fmt: on
    return pts, tris


def _profile(node: NodeIR) -> tuple[list[tuple[float, float]], int]:
    """Top-to-bottom (r, z) lathe profile and segment count of a round primitive."""
    k = node.kind
    r = _num(node, "radius")
    if k == "sphere":
        n, rings = _int(node, "segments", 24, 3), _int(node, "rings", 12, 2)
        phis = np.linspace(0.0, np.pi, rings + 1)
        return [(0.0 if i in (0, rings) else r * math.sin(p), r * math.cos(p)) for i, p in enumerate(phis)], n
    if k == "capsule":
        h = _num(node, "height", allow_zero=True)
        n = _int(node, "segments", 16, 3)
        cap = max(2, n // 4)  # latitude bands per hemisphere
        top = [(r * math.sin(p), h / 2 + r * math.cos(p)) for p in np.linspace(0.0, np.pi / 2, cap + 1)]
        bot = [(r * math.sin(p), -h / 2 + r * math.cos(p)) for p in np.linspace(np.pi / 2, np.pi, cap + 1)]
        top[0], bot[-1] = (0.0, top[0][1]), (0.0, bot[-1][1])
        return top + (bot[1:] if h == 0 else bot), n
    h = _num(node, "height")
    n = _int(node, "segments", 24, 3)
    if k == "cylinder":
        b = _num(node, "bevel", 0.0, allow_zero=True)
        if b == 0:
            return [(0.0, h / 2), (r, h / 2), (r, -h / 2), (0.0, -h / 2)], n
        if b >= min(r, h / 2):
            raise ValueError(f"{node.id}: bevel {b} must be below the radius ({r}) and half the height ({h / 2})")
        return shapes.rounded_cylinder_profile(r, h, b, _int(node, "bevel_segments", 1, 1, 4)), n
    return [(0.0, h / 2), (r, -h / 2), (0.0, -h / 2)], n  # cone: apex up


def _box_size(node: NodeIR) -> tuple[float, float, float]:
    size = node.params.get("size")
    if not isinstance(size, (tuple, list)) or len(size) != 3:
        raise ValueError(f"{node.id}: box requires param 'size' as (x, y, z), got {size!r}")
    if not all(isinstance(s, (int, float)) and math.isfinite(s) and s > 0 for s in size):
        raise ValueError(f"{node.id}: box size components must be > 0, got {tuple(size)}")
    return float(size[0]), float(size[1]), float(size[2])


def _shaped(node: NodeIR) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """(points, triangles, charts) of a node built by kernel/shapes.py, None for the plain primitives."""
    if node.kind == "box":
        if (spec := _box_shape(node)) is None:
            return None
        try:
            return shapes.box(*spec)
        except ValueError as e:
            raise ValueError(f"{node.id}: {e}") from None
    if node.kind == "extrude":
        return shapes.extrude(node.params.get("outline"), _num(node, "depth"), node.id)
    return None


def _lathe_rows(node: NodeIR) -> tuple[list[tuple[float, float]], int, bool]:
    closed = _closed(node)
    return shapes.lathe_rows(node.params.get("profile"), closed, node.id), _int(node, "segments", 16, 3), closed


def primitive_mesh(node: NodeIR) -> tuple[np.ndarray, np.ndarray]:
    """(points float32 Nx3, triangles int32 Mx3) for a primitive node, local space."""
    if (shaped := _shaped(node)) is not None:
        return shaped[0], shaped[1]
    if node.kind == "box":
        return _box(*_box_size(node))
    if node.kind == "lathe":
        return _lathe(*_lathe_rows(node))
    if node.kind not in PRIMITIVES:
        raise ValueError(f"{node.id}: kind '{node.kind}' is not a primitive")
    return _lathe(*_profile(node))


def check_node(node: NodeIR) -> None:
    """Raises the ValueError :func:`primitive_mesh` would raise for ``node``, with the same message, without building
    a mesh (cheap enough for every node of every answer). Nothing is clamped or adjusted anywhere in the kernel: a
    value is either built as given or refused here. mesh_ref (its file is read when authored) and group pass."""
    k = node.kind
    if k in ("group", "mesh_ref"):
        return
    if k == "box":
        if (spec := _box_shape(node)) is not None and (why := shapes.box_problem(*spec)):
            raise ValueError(f"{node.id}: {why}")
    elif k == "lathe":
        _lathe_rows(node)
    elif k == "extrude":
        shapes.extrude_outline(node.params.get("outline"), node.id)
        _num(node, "depth")
    elif k in PRIMITIVES:
        _profile(node)
    else:
        raise ValueError(f"{node.id}: kind '{k}' is not a primitive")


def collider_proxy(node: NodeIR) -> np.ndarray | None:
    """Local points whose convex hull stands in for the render mesh in collision, or None (collision from the mesh).

    A bevelled or inset box collides as its sharp box (8 points, the hull a sharp box gets), a bevelled cylinder as its
    sharp cylinder (2 x segments points). Measured on a 1 x 0.6 x 0.4 box with a 4-segment bevel: the render mesh's
    hull has 168 vertices and 332 triangles against 8 and 12; an inset box is concave, so without the proxy it is
    decomposed into several hulls. A rounded edge or a recessed panel is a visual detail a game collides with as the
    plain shape; the corners of the proxy stand at most bevel * (sqrt(3) - 1) outside the rounded surface."""
    if node.kind == "box" and _box_shape(node) is not None:
        x, y, z = (v / 2 for v in _box_size(node))
        return np.array([[sx * x, sy * y, sz * z] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], np.float32)
    if node.kind == "cylinder" and _num(node, "bevel", 0.0, allow_zero=True) > 0:
        r, h, n = _num(node, "radius"), _num(node, "height"), _int(node, "segments", 24, 3)
        return _lathe([(0.0, h / 2), (r, h / 2), (r, -h / 2), (0.0, -h / 2)], n)[0][1:-1]
    return None


def triangle_count(node: NodeIR) -> int:
    """Triangles :func:`primitive_mesh` makes for ``node``, from its parameters alone (nothing is built or checked).

    The one source of a part's triangle cost (budget estimates, authoring guidance); test_shapes pins it against the
    built meshes. Per kind (s = bevel_segments when bevel > 0, else 0; n = segments):
      box       12 + 16 s^2 + 16 s, plus 16 per inset face
      cylinder  n (4 s + 4)
      cone      2 n;  sphere 2 n (rings - 1);  capsule 4 n c when height > 0, else 4 n c - 2 n, with c = max(2, n // 4)
      lathe     open: n per band touching a pole (the two ends) and 2 n per other band; closed: 2 n per profile point
      extrude   4 k - 4 for k outline points
      group     0
    mesh_ref raises: its cost is in its file.
    """
    k, p = node.kind, node.params
    if k == "group":
        return 0
    if k == "box":
        faces = p.get("inset_faces") or []
        return shapes.box_tris(float(p.get("bevel", 0) or 0), int(p.get("bevel_segments", 1)), len(faces))
    if k == "extrude":
        return shapes.extrude_tris(p["outline"])
    if k == "lathe":
        return shapes.lathe_tris(p["profile"], bool(p.get("closed", False)), int(p.get("segments", 16)))
    if k == "cylinder":
        s = int(p.get("bevel_segments", 1)) if float(p.get("bevel", 0) or 0) > 0 else 0
        return int(p.get("segments", 24)) * (4 * s + 4)
    if k == "cone":
        return 2 * int(p.get("segments", 24))
    if k == "sphere":
        return 2 * int(p.get("segments", 24)) * (int(p.get("rings", 12)) - 1)
    if k == "capsule":
        n = int(p.get("segments", 16))
        cap = max(2, n // 4)
        return 4 * n * cap - (0 if float(p["height"]) > 0 else 2 * n)
    raise ValueError(f"{node.id}: kind '{k}' has no triangle cost from its parameters")


def primitive_charts(node: NodeIR) -> np.ndarray:
    """(M,) int32 UV chart id per triangle of :func:`primitive_mesh` (same triangle order).

    The charts are the primitive's exact developable pieces, which are known here at no cost:
      * box: one chart per face (a rectangle);
      * cylinder / cone: the side (a rectangle / a circular sector, cut along one generator by the
        unwrap) and each cap (a polygonal disc);
      * sphere / capsule: one chart per latitude band. Every band of a lathed mesh is a ring of planar
        trapezoids (a fan of triangles at a pole) whose vertices all lie on its two boundary rings, so it
        unfolds with *zero* area and angle distortion (a polygonal conical frustum is developable), and it
        stays developable under any affine node transform. Two adjacent bands cannot share a chart without
        a non-isometric map, so a seam per latitude ring is the price of zero distortion.
      * lathe / bevelled cylinder: one chart per band, as the sphere (two per band at an odd segment count);
      * bevelled / inset box, extrude: see kernel/shapes.py (open strips and flat pieces, all exact).
    ``fast_ops.unwrap_scene`` unfolds each chart from the *world-space* positions, so a non-uniformly
    scaled node still gets an exact layout at the requested texel density.
    """
    if (shaped := _shaped(node)) is not None:
        return shaped[2]
    if node.kind == "box":
        _box_size(node)
        return (np.arange(12) // 2).astype(np.int32)  # _box lists two triangles per face
    if node.kind not in PRIMITIVES:
        raise ValueError(f"{node.id}: kind '{node.kind}' is not a primitive")
    profile, n, closed = _lathe_rows(node) if node.kind == "lathe" else (*_profile(node), False)
    # _lathe emits one block per consecutive profile-row pair: n triangles for a pole fan, 2n for a quad band
    sizes = [n if 0.0 in (a[0], b[0]) else 2 * n for a, b in pairwise(profile + profile[:1] if closed else profile)]
    band = np.repeat(np.arange(len(sizes), dtype=np.int32), sizes)
    shaped = node.kind == "lathe" or (node.kind == "cylinder" and _num(node, "bevel", 0.0, allow_zero=True) > 0)
    if not (shaped and n % 2):
        return band
    # The unfold cuts a closed band where its two search fronts meet, opposite the first triangle: with an even
    # segment count that is the meridian edge at 180 degrees, with an odd one the diagonal of the quad there (a
    # texture seam across a facet). The shaped kinds split each band of an odd count into two halves at meridians.
    # (The five original primitives keep their charts: existing packages stay byte-identical.)
    j = np.concatenate([np.arange(k) % n for k in sizes])
    return (2 * band + (j >= (n + 1) // 2)).astype(np.int32)


# --- mesh data (UVs / normals) --------------------------------------------------

COLLIDER_PROXY_KEY = "agy:colliderProxy"  # point3f[] local: the convex collision shape of a shaped part (collider_proxy)
UV_WELD_KEY = "agy:uvWeldMap"  # int[] per authored vertex: id of the pre-unwrap vertex it was split from
UV_CHART_KEY = "agy:uvChart"  # int[] per triangle of a primitive: exact developable chart (see primitive_charts)
CREASE_DEG = 40.0
#: On a round part (``is_round``) two faces on its sides smooth with each other when the line where they meet runs
#: along the part (within ROUND_ALONG_DEG of its run there) and they turn by at most ROUND_TURN_DEG: a tube of 5 sides
#: or more (72 degrees per facet) ships round, a 3- or 4-sided one stays the prism it is. A crease ACROSS the run (a
#: turned bead, a kink in a rod, a cap) is a real edge and stays hard whatever its angle. One rule
#: (``round_soft``) for the shipped normals (``crease_normals``), the wear masks (``mesh_masks.mesh_edges``) and the
#: drawn edges of the design views (``raster._feature_edge_list``).
ROUND_TURN_DEG = 75.0
ROUND_ALONG_DEG = 20.0
RUN_POINTS_KEY = "agy:runPoints"  # point3f[] local: the axis or path a part is round about (node_run)
RUN_RADII_KEY = "agy:runRadii"  # float[] per run point (empty: unknown)
RUN_ROUND_KEY = "agy:runRound"  # bool: round about the run (is_round)
RUN_CLOSED_KEY = "agy:runClosed"  # bool: the run's last point joins its first
GRAIN_KEY = "agy:grain"  # vector3f local: the way the wood runs, as the author said it (NodeIR.grain)
#: Ready shapes that are round by construction get the round flag from this many segments. Their segment count is a
#: triangle budget (the authoring pages tell agents to lower it, and tactics.authoring._shrink takes curved parts down
#: to 8), so 7- and 8-segment legs and posts are round parts that shipped as prisms under the 40-degree crease and were
#: painted as edge all round. Five and six segments stay prisms: that is how pentagonal nuts and hex bolts are written
#: (round six's hydrant: cylinder segments=5 and 6). Checked 2026-11-23: every 7- to 9-segment round part in round six's
#: scripts (the crate's latch pivot and handle axle, 8) is a pin or an axle, and the offline templates use 12 to 24.
#: A prism of any side count (an octagonal column) is an extrusion of its outline, which is never round: its corners
#: ship hard and wear (tests/test_surface_review.py). A decision for the owner: one number to change.
ROUND_MIN_SEGMENTS = 7
ROUND_KINDS = frozenset({"cylinder", "cone", "capsule", "lathe"})


def is_round(node: NodeIR) -> bool:
    """Whether the part ships smooth around its run whatever its side count: a script's tube (its run says so), or a
    cylinder, cone, capsule or lathe with at least ``ROUND_MIN_SEGMENTS`` segments. Not a loft (a tray's sections are
    rectangles on purpose), an extrusion (a prism), a box or a sphere (its own crease smooths it from 10 segments up).
    ``grain=`` has no part in it."""
    if node.run is not None and node.run.round:
        return True
    if node.kind in ROUND_KINDS:
        default = 16 if node.kind in ("capsule", "lathe") else 24
        return _int(node, "segments", default, 3) >= ROUND_MIN_SEGMENTS
    return False


def node_run(node: NodeIR) -> RunIR | None:
    """The line a part is round about, in its own frame: a script tube's path (``NodeIR.run``, the author's input), else
    for a cylinder, cone, capsule or lathe its axis (local Z) and radius, derived from its parameters now (so an edit
    of a parameter re-derives it). None for every other part. The way the wood runs is another thing
    (``NodeIR.grain``, the painter's ``surface_frame.part_grain``) and never moves this line."""
    if node.run is not None:
        return node.run
    if node.kind not in ROUND_KINDS:
        return None
    if node.kind == "lathe":
        rows, _, _ = _lathe_rows(node)
        zs = [z for _, z in rows]
        r = max(rr for rr, _ in rows)
        z0, z1 = min(zs), max(zs)
    else:
        r = _num(node, "radius")
        h = _num(node, "height", allow_zero=node.kind == "capsule")
        z0, z1 = (-h / 2 - r, h / 2 + r) if node.kind == "capsule" else (-h / 2, h / 2)
    if z1 - z0 < 1e-9:
        return None
    return RunIR(path=[(0.0, 0.0, z0), (0.0, 0.0, z1)], radius=[r, r], round=is_round(node))


def read_run(prim: Usd.Prim, to_world: np.ndarray | None = None) -> Any:
    """The ``types.Run`` authored on a mesh prim (``RUN_*_KEY``), in the prim's local frame, or through ``to_world``
    (a 4 x 4 row-vector matrix: radii scale by its mean axis scale). None when the prim has none."""
    from antigravity3d.kernel.types import Run

    if not prim.HasAttribute(RUN_POINTS_KEY):
        return None
    pts = prim.GetAttribute(RUN_POINTS_KEY).Get()
    if pts is None or len(pts) < 2:
        return None
    p = np.asarray(pts, np.float64)
    rr = prim.GetAttribute(RUN_RADII_KEY).Get() if prim.HasAttribute(RUN_RADII_KEY) else None
    r = np.asarray(rr, np.float64) if rr is not None and len(rr) == len(p) else np.zeros(len(p))
    rnd = bool(prim.GetAttribute(RUN_ROUND_KEY).Get()) if prim.HasAttribute(RUN_ROUND_KEY) else False
    closed = bool(prim.GetAttribute(RUN_CLOSED_KEY).Get()) if prim.HasAttribute(RUN_CLOSED_KEY) else False
    if to_world is not None:
        m = np.asarray(to_world, np.float64)
        p = p @ m[:3, :3] + m[3, :3]
        r = r * float(np.mean(np.linalg.norm(m[:3, :3], axis=1)))
    return Run(p, r, rnd, closed)


def read_grain(prim: Usd.Prim, to_world: np.ndarray | None = None) -> np.ndarray | None:
    """The author's grain direction on a mesh prim (``GRAIN_KEY``), unit, local or through ``to_world``; else None."""
    if not prim.HasAttribute(GRAIN_KEY):
        return None
    g = prim.GetAttribute(GRAIN_KEY).Get()
    if g is None:
        return None
    v = np.asarray(g, np.float64)
    if to_world is not None:
        v = v @ np.asarray(to_world, np.float64)[:3, :3]
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else None


def _author_run(prim: Usd.Prim, node: NodeIR) -> None:
    run = node_run(node)
    if run is None:
        _block_authored(prim, [RUN_POINTS_KEY, RUN_RADII_KEY, RUN_ROUND_KEY, RUN_CLOSED_KEY])
    else:
        prim.CreateAttribute(RUN_POINTS_KEY, Sdf.ValueTypeNames.Point3fArray, custom=True).Set(
            Vt.Vec3fArray.FromNumpy(np.asarray(run.path, np.float32)))
        prim.CreateAttribute(RUN_RADII_KEY, Sdf.ValueTypeNames.FloatArray, custom=True).Set(
            Vt.FloatArray.FromNumpy(np.asarray(run.radius, np.float32)))
        prim.CreateAttribute(RUN_ROUND_KEY, Sdf.ValueTypeNames.Bool, custom=True).Set(bool(run.round))
        prim.CreateAttribute(RUN_CLOSED_KEY, Sdf.ValueTypeNames.Bool, custom=True).Set(bool(run.closed))
    if node.grain is None:
        _block_authored(prim, [GRAIN_KEY])
    else:
        prim.CreateAttribute(GRAIN_KEY, Sdf.ValueTypeNames.Vector3f, custom=True).Set(Gf.Vec3f(*map(float, node.grain)))


def round_soft(n1: np.ndarray, n2: np.ndarray, d1: np.ndarray, d2: np.ndarray, crease_deg: float = CREASE_DEG) -> np.ndarray:
    """Whether faces with unit normals ``n1``, ``n2`` and round-run directions ``d1``, ``d2`` (``surface_frame.round_dirs``:
    zero where a face is not on a round part's side) smooth with each other although they turn by more than the crease:
    both on a round part's sides, turning by at most ROUND_TURN_DEG, and the line where they meet (n1 x n2) running
    along the run (within ROUND_ALONG_DEG). Never below the project's crease: an art direction that asks for harder
    creases (low-poly, retro: 0 degrees) wants the facets seen, the side count is then the look. Broadcasts."""
    if crease_deg < CREASE_DEG:
        return np.zeros(np.broadcast_shapes(n1.shape[:-1], n2.shape[:-1]), bool)
    dsum = d1 + d2
    has = ((d1 * d1).sum(-1) > 0.25) & ((d2 * d2).sum(-1) > 0.25)
    dots = (n1 * n2).sum(-1)
    c = np.cross(n1, n2)
    cl = np.sqrt((c * c).sum(-1))
    dl = np.sqrt((dsum * dsum).sum(-1))
    along = np.abs((c * dsum).sum(-1)) >= math.cos(math.radians(ROUND_ALONG_DEG)) * cl * dl - 1e-12
    return has & (dots >= math.cos(math.radians(ROUND_TURN_DEG)) - 1e-9) & along


def facing(tris: np.ndarray, linear: np.ndarray) -> np.ndarray:
    """``tris`` with the winding a transform with 3x3 part ``linear`` needs to keep them facing the same way.

    A transform with a negative determinant (a mirror: scale [-1, 1, 1], or under a mirrored parent) turns an outward
    mesh inside out when its points are baked, so baking reverses each triangle (first vertex kept), and writing world
    triangles back onto a mirrored prim's local points reverses them again. USD stores the local winding: renderers
    account for the mirror themselves."""
    t = np.asarray(tris)
    return t[:, [0, 2, 1]] if np.linalg.det(np.asarray(linear, np.float64)[:3, :3]) < 0 else t


def crease_normals(points: np.ndarray, tris: np.ndarray, crease_deg: float = CREASE_DEG, run: Any = None,
                   dirs: np.ndarray | None = None) -> np.ndarray:
    """Per-corner (faceVarying, 3 per triangle) normals, smoothed across faces within ``crease_deg``.

    Smoothing is done over position-welded vertices, so seam-split vertices (UV seams) shade
    continuously while real hard edges (box corners) stay crisp.

    ``run``: the part's ``types.Run`` (same space as ``points``); on a round part the facets along it also smooth
    (``round_soft``). ``dirs``: instead, each face's round-run direction (``surface_frame.round_dirs``), for a batch of
    several parts each with its own run. Every shipped normal comes from here.
    """
    from antigravity3d.kernel.fast_ops import _canon

    p = np.asarray(points, dtype=np.float64)
    t = np.asarray(tris, dtype=np.int64)
    fn = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])  # area-weighted
    ln = np.linalg.norm(fn, axis=1, keepdims=True)
    unit = np.divide(fn, ln, out=np.zeros_like(fn), where=ln > 1e-20)
    if dirs is None and run is not None and getattr(run, "round", False) and len(t):
        from antigravity3d.kernel.surface_frame import round_dirs

        dirs = round_dirs(p, t, run)
    if dirs is not None and (crease_deg < CREASE_DEG or not np.any(dirs)):
        dirs = None
    wid = _canon(p)[t].ravel()
    order = np.argsort(wid, kind="stable")
    cuts = np.flatnonzero(np.diff(wid[order])) + 1
    cos_t = math.cos(math.radians(crease_deg))
    out = np.zeros((len(t) * 3, 3))
    for grp in np.split(order, cuts):
        faces = grp // 3
        dots = unit[faces] @ unit[faces].T
        same = dots >= cos_t - 1e-9
        if dirs is not None:
            d = dirs[faces]
            if d.any():
                u = unit[faces]
                same |= round_soft(u[:, None], u[None, :], d[:, None], d[None, :], crease_deg)
        out[grp] = same.astype(np.float64) @ fn[faces]
    n = np.linalg.norm(out, axis=1, keepdims=True)
    fallback = np.repeat(unit, 3, axis=0)
    out = np.where(n > 1e-20, out / np.maximum(n, 1e-20), fallback)
    return out.astype(np.float32)


def prim_normals(prim: Usd.Prim, pts: np.ndarray, tri: np.ndarray, crease_deg: float) -> np.ndarray:
    """The shipped normals of a mesh prim's local points, decided where the GLB decides them: in world space. Angles
    between faces are the same in both spaces under a rotation, a mirror or a uniform scale, so those stay local; under a
    non-uniform scale they are not (an 8-segment cylinder scaled (3, 1, 1): local 45 degrees per facet, world up to 72),
    and the USD then shaded other edges than the GLB. Measured before this: 12 corners differing by up to 74 degrees."""
    m = np.array(UsdGeom.XformCache().GetLocalToWorldTransform(prim), np.float64)
    L = m[:3, :3]
    sv = np.linalg.svd(L, compute_uv=False)
    if sv.min() <= 0 or sv.max() - sv.min() <= 1e-6 * sv.max():
        return crease_normals(pts, tri, crease_deg, read_run(prim))
    world = np.asarray(pts, np.float64) @ L + m[3, :3]
    flip = np.linalg.det(L) < 0
    wt = tri[:, [0, 2, 1]] if flip else tri
    n = crease_normals(world, wt, crease_deg, read_run(prim, m)).reshape(-1, 3, 3)
    if flip:
        n = n[:, [0, 2, 1]]
    local = n.reshape(-1, 3) @ L.T                       # a normal goes back through the transpose of the map
    local /= np.maximum(np.linalg.norm(local, axis=1, keepdims=True), 1e-20)
    return local.astype(np.float32)


def author_mesh_data(
    prim: Usd.Prim,
    points: np.ndarray,
    tris: np.ndarray,
    uvs: np.ndarray | None,
    *,
    weld_map: np.ndarray | None = None,
    normals: bool = True,
    crease_deg: float = CREASE_DEG,
) -> None:
    """Writes triangle geometry, ``primvars:st`` and normals on a Mesh prim in the edit target.

    Conventions (one place, used by the working stage, the package and its LOD variants):
      * ``primvars:st``  texCoord2f[], *vertex* interpolation: one UV per authored point, i.e. the
        seam-split topology of the unwrap, identical to the GLB. ``uvs`` None blocks any weaker st.
      * ``normals``      normal3f[], *faceVarying* (3 per triangle), crease-smoothed (see
        :func:`crease_normals` at ``crease_deg``, the project's 40 degrees unless an art direction sets
        another); not relying on USD's default because that smooths hard edges of shared-vertex meshes when
        subdivisionScheme=none.
      * the mesh is triangulated and ``subdivisionScheme=none``.
    """
    mesh = UsdGeom.Mesh(prim)
    pts = np.ascontiguousarray(points, dtype=np.float32)
    tri = np.ascontiguousarray(tris, dtype=np.int32).reshape(-1, 3)
    mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(pts))
    mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.full(len(tri), 3, np.int32)))
    mesh.CreateFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(tri.ravel()))
    mesh.CreateExtentAttr().Set(Vt.Vec3fArray.FromNumpy(np.stack([pts.min(0), pts.max(0)])))
    mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    if uvs is not None:
        uv = np.ascontiguousarray(uvs, dtype=np.float32).reshape(-1, 2)
        if len(uv) != len(pts):
            raise ValueError(f"{prim.GetPath()}: {len(uv)} uvs for {len(pts)} points")
        pv = UsdGeom.PrimvarsAPI(prim).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        pv.Set(Vt.Vec2fArray.FromNumpy(uv))
        pv.SetInterpolation(UsdGeom.Tokens.vertex)
        _block_authored(prim, ["primvars:st:indices"])
    else:
        _block_authored(prim, ["primvars:st", "primvars:st:indices"])
    if weld_map is not None:
        prim.CreateAttribute(UV_WELD_KEY, Sdf.ValueTypeNames.IntArray, custom=True).Set(
            Vt.IntArray.FromNumpy(np.ascontiguousarray(weld_map, dtype=np.int32))
        )
    else:
        _block_authored(prim, [UV_WELD_KEY])
    _block_authored(prim, ["primvars:normals", "primvars:normals:indices"])
    if normals:
        mesh.CreateNormalsAttr().Set(Vt.Vec3fArray.FromNumpy(prim_normals(prim, pts, tri, crease_deg)))
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    else:
        _block_authored(prim, ["normals"])


# --- scene authoring ----------------------------------------------------------


def read_ir(prim: Usd.Prim) -> SceneIR | None:
    """The scene IR stored in a prim's composed customData, if any."""
    if not prim or not prim.IsValid():
        return None
    raw = prim.GetCustomDataByKey(IR_KEY)
    return SceneIR.model_validate_json(raw) if raw else None


def node_paths(scene: SceneIR, root: Sdf.Path) -> dict[str, Sdf.Path]:
    by_id = {n.id: n for n in scene.nodes}
    out: dict[str, Sdf.Path] = {}

    for node in scene.nodes:
        nid = node.id
        walked = []
        while nid is not None and nid not in out:
            walked.append(nid)
            if len(walked) > len(by_id):
                raise ValueError(f"{node.id}: parent cycle")
            nid = by_id[nid].parent
        path = root if nid is None else out[nid]
        for child in reversed(walked):
            path = path.AppendChild(child)
            out[child] = path
    return out


def _shader_names(m: MaterialIR) -> list[str]:
    names = ["Surface"]
    if m.albedo_uri or m.orm_uri or m.normal_uri or m.emissive_uri:
        names.append("stReader")
    names += [t for t, uri in (("albedoTex", m.albedo_uri), ("ormTex", m.orm_uri), ("normalTex", m.normal_uri),
                               ("emissiveTex", m.emissive_uri)) if uri]
    return names


def _set_input(shader: UsdShade.Shader, name: str, vtype: Sdf.ValueTypeName, value) -> UsdShade.Input:
    inp = shader.CreateInput(name, vtype)
    inp.Set(value)
    if inp.GetAttr().HasAuthoredConnections():  # a weaker layer wired a texture here
        inp.GetAttr().SetConnections([])  # explicit empty list overrides it
    return inp


def set_normal_encoding(shader: UsdShade.Shader, convention: str) -> None:
    """Decode packed normals to the +Y tangent frame consumed by UsdPreviewSurface."""
    y = -2 if convention == "directx" else 2
    shader.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(2, y, 2, 1))
    shader.CreateInput("bias", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(-1, -y / 2, -1, 0))


# --- material parameters: one authored home each, one reader ---------------------------------------------------
#
# ``UsdPreviewSurface`` input where one exists, otherwise an input on the ``UsdShade.Material`` prim named like the
# MaterialX ``gltf_pbr`` node's input. Double-sidedness is geometry state in USD (``UsdGeom.Gprim.doubleSided`` on the
# bound meshes). An input is written when its value differs from the glTF default or already has an opinion (so
# returning a field to its default overwrites the old value instead of leaving it behind); an absent input reads as
# the glTF default, which is what keeps every pre-existing asset's USD, and therefore its GLB, unchanged.

_F, _C3, _TOK = Sdf.ValueTypeNames.Float, Sdf.ValueTypeNames.Color3f, Sdf.ValueTypeNames.Token
#: (IR field, UsdPreviewSurface input, type, glTF default)
SURFACE_PARAMS = (
    ("opacity", "opacity", _F, 1.0),
    ("emissive_color", "emissiveColor", _C3, (0.0, 0.0, 0.0)),
    ("ior", "ior", _F, 1.5),
    ("clearcoat", "clearcoat", _F, 0.0),
    ("clearcoat_roughness", "clearcoatRoughness", _F, 0.0),
)
#: (IR field, input on the Material prim, type, glTF default); ``alpha_mode`` is a token, the rest numeric
INTERFACE_PARAMS = (
    ("transmission", "transmission", _F, 0.0),
    ("sheen_color", "sheen_color", _C3, (0.0, 0.0, 0.0)),
    ("sheen_roughness", "sheen_roughness", _F, 0.0),
    ("specular", "specular", _F, 1.0),
    ("specular_color", "specular_color", _C3, (1.0, 1.0, 1.0)),
    ("emissive_strength", "emissive_strength", _F, 1.0),
    ("alpha_mode", "alpha_mode", _TOK, "OPAQUE"),
)


def _put(owner, name: str, vtype: Sdf.ValueTypeName, value, default) -> UsdShade.Input | None:
    """Writes ``value`` unless it is the default and nothing was ever authored (see the block comment above)."""
    if value == default and not owner.GetInput(name):
        return None
    if vtype == _C3:
        value = Gf.Vec3f(*value)
    return _set_input(owner, name, vtype, value)


_DS_OWNER = "agy:doubleSidedAuthored"  # customData on a mesh: True while the sync owns its ``doubleSided`` opinion


def _sync_mesh(prim: Usd.Prim, want: bool) -> None:
    """Brings one mesh's ``doubleSided`` to ``want``, touching only what the sync itself authored.

    A mesh that is already double-sided (its source says so, or an earlier sync did) is left alone when ``want``;
    the sync claims a mesh (marker True) only when it is the one that turns the attribute on, and only a claimed
    mesh is turned back off. A source mesh's own ``doubleSided = true`` therefore survives every edit.
    """
    attr = UsdGeom.Gprim(prim).GetDoubleSidedAttr()
    if want:
        if not attr.Get():
            attr.Set(True)
            prim.SetCustomDataByKey(_DS_OWNER, True)
    elif prim.GetCustomDataByKey(_DS_OWNER) is True:
        attr.Set(False)  # an explicit value in this layer overrides the one authored in a weaker layer
        prim.SetCustomDataByKey(_DS_OWNER, False)


def sync_double_sided(stage: Usd.Stage, root: Usd.Prim, sided_ids: set[str]) -> None:
    """Makes ``doubleSided`` follow the binding the renderer resolves, for every mesh under ``root``.

    A mesh gets the opinion when ``ComputeBoundMaterial`` (direct or inherited from a group) lands on a material
    named in ``sided_ids`` (looks of ``root``); one the sync had switched on that no longer resolves to such a
    material is switched off, so re-binding a child or editing a material leaves nothing stale and nothing that
    belongs to a source file is overwritten (see :func:`_sync_mesh`). Walks every mesh under ``root``: callers run
    it only when a double-sided material is, or was, in play.
    """
    looks = root.GetPath().AppendChild(LOOKS)
    for prim in Usd.PrimRange(root, Usd.PrimIsActive):
        if not prim.IsA(UsdGeom.Gprim):
            continue
        mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        _sync_mesh(prim, bool(mat) and mat.GetPath().GetParentPath() == looks and mat.GetPath().name in sided_ids)


def _author_material(stage: Usd.Stage, path: Sdf.Path, m: MaterialIR) -> None:
    mat = UsdShade.Material.Define(stage, path)
    if not mat.GetPrim().IsActive():
        mat.GetPrim().SetActive(True)
    mat.GetPrim().SetCustomDataByKey("agy:normalConvention", m.normal_convention)
    # the material's own words, for what reads a composed material without the scene description (the shared atlas's
    # layout asks each surface source how much detail a material carries: kernel/smart_source.py)
    if m.surface:
        mat.GetPrim().SetCustomDataByKey("agy:surface", m.surface)
    else:
        mat.GetPrim().ClearCustomDataByKey("agy:surface")
    surf = UsdShade.Shader.Define(stage, path.AppendChild("Surface"))
    surf.CreateIdAttr("UsdPreviewSurface")
    mat.CreateSurfaceOutput().ConnectToSource(surf.ConnectableAPI(), "surface")
    color = _set_input(surf, "diffuseColor", Sdf.ValueTypeNames.Color3f, Gf.Vec3f(*m.base_color))
    rough = _set_input(surf, "roughness", Sdf.ValueTypeNames.Float, m.roughness)
    metal = _set_input(surf, "metallic", Sdf.ValueTypeNames.Float, m.metallic)
    for key, name, vtype, default in SURFACE_PARAMS:
        _put(surf, name, vtype, getattr(m, key), default)
    _put(surf, "opacityThreshold", _F, m.alpha_cutoff if m.alpha_mode == "MASK" else 0.0, 0.0)
    for key, name, vtype, default in INTERFACE_PARAMS:
        _put(mat, name, vtype, getattr(m, key), default)
    alpha_from_albedo = m.alpha_mode != "OPAQUE" and bool(m.albedo_uri)
    if alpha_from_albedo:  # the connection below needs the input to exist even at opacity 1
        _set_input(surf, "opacity", _F, m.opacity)
    for name in ("occlusion", "normal"):  # texture-only inputs: unwire stale connections
        inp = surf.GetInput(name)
        if inp and inp.GetAttr().HasAuthoredConnections():
            inp.GetAttr().SetConnections([])
    if not (m.albedo_uri or m.orm_uri or m.normal_uri or m.emissive_uri):
        return
    reader = UsdShade.Shader.Define(stage, path.AppendChild("stReader"))
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")
    st = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    def tex(name: str, uri: str, colorspace: str) -> UsdShade.Shader:
        t = UsdShade.Shader.Define(stage, path.AppendChild(name))
        t.CreateIdAttr("UsdUVTexture")
        t.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(uri))
        t.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st)
        t.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set(colorspace)
        return t

    if m.albedo_uri:
        albedo = tex("albedoTex", m.albedo_uri, "sRGB")
        color.ConnectToSource(albedo.CreateOutput("rgb", Sdf.ValueTypeNames.Float3))
        if alpha_from_albedo:
            surf.GetInput("opacity").ConnectToSource(albedo.CreateOutput("a", Sdf.ValueTypeNames.Float))
    if m.orm_uri:
        orm = tex("ormTex", m.orm_uri, "raw")
        occl = surf.CreateInput("occlusion", Sdf.ValueTypeNames.Float)
        occl.ConnectToSource(orm.CreateOutput("r", Sdf.ValueTypeNames.Float))
        rough.ConnectToSource(orm.CreateOutput("g", Sdf.ValueTypeNames.Float))
        metal.ConnectToSource(orm.CreateOutput("b", Sdf.ValueTypeNames.Float))
    if m.normal_uri:
        nt = tex("normalTex", m.normal_uri, "raw")
        set_normal_encoding(nt, m.normal_convention)
        normal = surf.CreateInput("normal", Sdf.ValueTypeNames.Normal3f)
        normal.ConnectToSource(nt.CreateOutput("rgb", Sdf.ValueTypeNames.Float3))
    if m.emissive_uri:
        et = tex("emissiveTex", m.emissive_uri, "sRGB")
        et.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(*m.emissive_color, 1.0))  # factor x map, as glTF
        surf.GetInput("emissiveColor").ConnectToSource(et.CreateOutput("rgb", Sdf.ValueTypeNames.Float3))


@dataclass(frozen=True)
class MapSource:
    """A texture the material samples: the asset path as authored and the file it resolves to (None if unresolved)."""

    authored: str
    resolved: str | None
    color_space: str | None


@dataclass(frozen=True)
class MaterialParams:
    """The full parameter set of one composed material, in the IR's field names (see :func:`read_material`).

    ``maps`` holds the roles ``albedo``, ``orm``, ``normal`` and ``emissive`` that are wired to a texture.
    ``alpha_from_albedo`` is true when the opacity is the albedo map's alpha channel. Factors of a channel that a
    map drives (``base_color`` under ``albedo``, ``metallic``/``roughness`` under ``orm``) are the authored
    fallback values, as in USD; the glTF derivation decides what to export.
    """

    name: str
    base_color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    metallic: float = 0.0
    roughness: float = 0.5
    alpha_mode: str = "OPAQUE"
    alpha_cutoff: float | None = None
    opacity: float = 1.0
    double_sided: bool = False
    emissive_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    emissive_strength: float = 1.0
    clearcoat: float = 0.0
    clearcoat_roughness: float = 0.0
    transmission: float = 0.0
    ior: float = 1.5
    sheen_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    sheen_roughness: float = 0.0
    specular: float = 1.0
    specular_color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    normal_convention: str = "opengl"
    alpha_from_albedo: bool = False
    maps: Mapping[str, MapSource] = field(default_factory=dict)
    surface: str = ""  # MaterialIR.surface (customData agy:surface): words, not a glTF factor

    def factors(self) -> dict[str, Any]:
        """The scalar/colour parameters by IR field name (everything except ``name``, ``maps`` and map flags)."""
        skip = {"name", "maps", "alpha_from_albedo", "normal_convention", "surface"}
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in skip}


def bound_double_sided(mat: UsdShade.Material) -> bool:
    """True when any mesh that resolves to this material (bound directly or through a parent) is double-sided.

    Includes a source mesh that carries its own ``doubleSided`` (imported foliage, cloth): the geometry bound to the
    material decides, so the GLB material is double-sided for that mesh whatever the IR flag says.
    """
    path = mat.GetPath()
    for prim in mat.GetPrim().GetStage().Traverse():
        if prim.IsA(UsdGeom.Gprim) and UsdGeom.Gprim(prim).GetDoubleSidedAttr().Get():
            bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            if bound and bound.GetPath() == path:
                return True
    return False


def read_material(mat: UsdShade.Material, *, double_sided: bool | None = None) -> MaterialParams:
    """Reads one composed ``UsdShade.Material`` back into the full parameter set. The only reader.

    Everything that needs material values (GLB export, manifest, inspector, checks) calls this, so the USD homes
    chosen in ``_author_material`` are known in exactly one other place. A missing input reads as the glTF
    default. ``double_sided`` is geometry state: pass the answer for the mesh being exported, or leave it None to
    ask the stage (any directly bound double-sided mesh). A material authored by another tool without the
    ``alpha_mode`` input is read through UsdPreviewSurface's own semantics (``opacityThreshold`` > 0 is a cut-out,
    ``opacity`` < 1 or a wired opacity is blended).
    """
    shader = None
    if mat:
        src, _, _ = mat.ComputeSurfaceSource()
        shader = src if src else None

    def get(owner, name: str, default):
        inp = owner.GetInput(name) if owner else None
        v = inp.Get() if inp else None
        if v is None:
            return default
        return tuple(float(c) for c in v) if isinstance(default, tuple) else (str(v) if isinstance(default, str) else float(v))

    def source(owner, name: str) -> tuple[UsdShade.Shader, str] | None:
        inp = owner.GetInput(name) if owner else None
        if not inp or not inp.HasConnectedSource():
            return None
        s = inp.GetConnectedSource()
        tex = UsdShade.Shader(s[0].GetPrim())
        return (tex, str(s[1])) if tex and tex.GetIdAttr().Get() == "UsdUVTexture" else None

    def map_of(owner, name: str) -> MapSource | None:
        hit = source(owner, name)
        if hit is None:
            return None
        f, cs = hit[0].GetInput("file"), hit[0].GetInput("sourceColorSpace")
        v = f.Get() if f else None
        if not isinstance(v, Sdf.AssetPath):
            return None
        return MapSource(v.path, v.resolvedPath or None, str(cs.Get()) if cs and cs.Get() else None)

    maps = {role: ref for role, name in (("albedo", "diffuseColor"), ("emissive", "emissiveColor"), ("normal", "normal"))
            if (ref := map_of(shader, name)) is not None}
    orm = next((r for n in ("occlusion", "roughness", "metallic") if (r := map_of(shader, n)) is not None), None)
    if orm is not None:
        maps["orm"] = orm
    opacity = get(shader, "opacity", 1.0)
    threshold = get(shader, "opacityThreshold", 0.0)
    wired_alpha = (hit := source(shader, "opacity")) is not None and str(hit[1]).split(":")[-1] == "a"
    mode = get(mat, "alpha_mode", "")
    if mode not in ("OPAQUE", "MASK", "BLEND"):
        mode = "MASK" if threshold > 0 else ("BLEND" if opacity < 1.0 or source(shader, "opacity") else "OPAQUE")
    values = {key: get(mat, name, default) for key, name, _, default in INTERFACE_PARAMS if key != "alpha_mode"}
    values.update({key: get(shader, name, default) for key, name, _, default in SURFACE_PARAMS})
    convention = mat.GetPrim().GetCustomDataByKey("agy:normalConvention") if mat else None
    return MaterialParams(
        name=mat.GetPath().name if mat else "",
        surface=str(mat.GetPrim().GetCustomDataByKey("agy:surface") or "") if mat else "",
        base_color=get(shader, "diffuseColor", (0.8, 0.8, 0.8)), metallic=get(shader, "metallic", 0.0),
        roughness=get(shader, "roughness", 0.5), alpha_mode=mode,
        alpha_cutoff=threshold if mode == "MASK" else None,
        double_sided=bound_double_sided(mat) if double_sided is None and mat else bool(double_sided),
        normal_convention=str(convention or "opengl"), alpha_from_albedo=wired_alpha and "albedo" in maps,
        maps=maps, **values)


def _block_authored(prim: Usd.Prim, names) -> None:
    for name in names:
        attr = prim.GetAttribute(name)
        if attr and attr.HasAuthoredValue():
            attr.Block()


def _author_transform(prim: Usd.Prim, t: Transform, prev: Transform | None = None) -> None:
    # Sparse overs avoid duplicating type, geometry and bindings on transform-only edits.
    for name, value_type, vec_type, value, previous in (
        ("translate", Sdf.ValueTypeNames.Double3, Gf.Vec3d, t.translate, prev and prev.translate),
        ("rotateXYZ", Sdf.ValueTypeNames.Float3, Gf.Vec3f, t.rotate_euler_deg, prev and prev.rotate_euler_deg),
        ("scale", Sdf.ValueTypeNames.Float3, Gf.Vec3f, t.scale, prev and prev.scale),
    ):
        if value != previous:
            prim.CreateAttribute(f"xformOp:{name}", value_type, custom=False).Set(vec_type(*value))
    if prev is None:
        UsdGeom.Xformable(prim).CreateXformOpOrderAttr().Set(
            Vt.TokenArray(["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"])
        )


def _author_node(stage: Usd.Stage, path: Sdf.Path, node: NodeIR, root: Sdf.Path) -> None:
    is_prim = node.kind in PRIMITIVES
    ref_path = None
    if node.kind == "mesh_ref":
        uri = node.params.get("path")
        if not isinstance(uri, str) or not uri:
            raise ValueError(f"{node.id}: mesh_ref requires string param 'path'")
        root_file = stage.GetRootLayer().realPath
        ref_path = ((Path(root_file).parent if root_file else Path.cwd()) / uri).resolve()
    imported = ref_path is not None and ref_path.suffix.lower() not in USD_EXTS
    prim = stage.DefinePrim(path, "Mesh" if is_prim or imported else "Xform")
    if not prim.IsActive():
        prim.SetActive(True)
    prim.CreateAttribute("agy:nodeId", Sdf.ValueTypeNames.String, custom=True).Set(node.id)
    prim.CreateAttribute("agy:collider", Sdf.ValueTypeNames.Bool, custom=True).Set(node.collider)

    _author_transform(prim, node.transform)

    if is_prim:
        pts, tris = primitive_mesh(node)
        author_mesh_data(prim, pts, tris, None, normals=False)
        prim.CreateAttribute(UV_CHART_KEY, Sdf.ValueTypeNames.IntArray, custom=True).Set(
            Vt.IntArray.FromNumpy(primitive_charts(node))
        )
        if (proxy := collider_proxy(node)) is not None:
            prim.CreateAttribute(COLLIDER_PROXY_KEY, Sdf.ValueTypeNames.Point3fArray, custom=True).Set(Vt.Vec3fArray.FromNumpy(proxy))
        else:
            _block_authored(prim, [COLLIDER_PROXY_KEY])
    elif imported:
        # One IR node owns one material and one mesh, including transformed instances.
        points, faces, uvs = [], [], []
        offset = 0
        for _, p, f, uv in triangle_parts(ref_path):
            points.append(p)
            faces.append(f + offset)
            uvs.append(uv)
            offset += len(p)
        st = np.concatenate(uvs) if all(uv is not None for uv in uvs) else None
        author_mesh_data(prim, np.concatenate(points), np.concatenate(faces), st, normals=False)
    else:
        _block_authored(prim, _MESH_ATTRS)

    if is_prim or imported:
        _author_run(prim, node)
    if imported:
        prim.CreateAttribute("agy:meshRef", Sdf.ValueTypeNames.String, custom=True).Set(node.params["path"])
    else:
        _block_authored(prim, ["agy:meshRef"])
    if not is_prim:  # imported / referenced geometry: charts are computed by the unwrap stage
        _block_authored(prim, [UV_CHART_KEY, COLLIDER_PROXY_KEY])
    if ref_path is not None and not imported:
        if not ref_path.is_file():
            raise FileNotFoundError(ref_path)
        # Anonymous tx layers have no directory against which to resolve a reference.
        ref = stage.DefinePrim(path.AppendChild(REF_CHILD))
        if not ref.IsActive():
            ref.SetActive(True)
        ref.GetReferences().SetReferences([Sdf.Reference(str(ref_path))])

    _bind_material(stage, prim, root, node.material_id)


def _bind_material(stage: Usd.Stage, prim: Usd.Prim, root: Sdf.Path, material_id: str | None) -> None:
    if material_id is not None:
        api = UsdShade.MaterialBindingAPI.Apply(prim)
        api.Bind(UsdShade.Material(stage.GetPrimAtPath(root.AppendPath(f"{LOOKS}/{material_id}"))))
    else:
        rel = prim.GetRelationship("material:binding")
        if rel and rel.HasAuthoredTargets():
            rel.SetTargets([])  # explicit empty list overrides a weaker binding


def _ensure_root(stage: Usd.Stage, root: Sdf.Path) -> Usd.Prim:
    prim = stage.GetPrimAtPath(root)
    if prim and prim.IsValid():
        return prim
    prim = UsdGeom.Xform.Define(stage, root).GetPrim()
    Usd.ModelAPI(prim).SetKind(Kind.Tokens.component)
    if root.IsRootPrimPath():
        stage.SetDefaultPrim(prim)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    return prim


def author_scene(
    stage: Usd.Stage,
    scene: SceneIR,
    *,
    root: Sdf.Path | str | None = None,
    prev: SceneIR | None = None,
) -> None:
    """Authors ``scene`` under ``root`` (default ``/<asset_name>``) in the edit target.

    ``prev`` defaults to the IR composed on ``root``; only nodes/materials that
    differ from it are written. ``root`` lets the StageManager author into a
    referenced base prim (see usd_stage.py) instead of the default prim.
    """
    root = Sdf.Path(root) if root is not None else Sdf.Path.absoluteRootPath.AppendChild(scene.asset_name)
    root_prim = _ensure_root(stage, root)
    if prev is None:
        prev = read_ir(root_prim)
    prev_nodes = {n.id: n for n in prev.nodes} if prev else {}
    prev_mats = {m.id: m for m in prev.materials} if prev else {}
    prev_paths = node_paths(prev, root) if prev else {}
    paths = node_paths(scene, root)
    expected: set[Sdf.Path] = {root, *paths.values()}
    opaque: set[Sdf.Path] = set()  # subtrees we don't own (referenced mesh files)

    def stale(path: Sdf.Path) -> bool:
        p = stage.GetPrimAtPath(path)
        return not (p and p.IsActive())

    looks = root.AppendChild(LOOKS)
    if scene.materials:
        expected.add(looks)
        if stale(looks):
            scope = UsdGeom.Scope.Define(stage, looks).GetPrim()
            if not scope.IsActive():
                scope.SetActive(True)
    for m in scene.materials:
        mpath = looks.AppendChild(m.id)
        expected.add(mpath)
        expected.update(mpath.AppendChild(s) for s in _shader_names(m))
        if prev_mats.get(m.id) != m or stale(mpath):
            _author_material(stage, mpath, m)

    for node in sorted(scene.nodes, key=lambda n: paths[n.id].pathElementCount):  # parents first
        path = paths[node.id]
        needs_import = False
        if node.kind == "mesh_ref":
            if Path(str(node.params.get("path", ""))).suffix.lower() in USD_EXTS:
                expected.add(path.AppendChild(REF_CHILD))
                opaque.add(path.AppendChild(REF_CHILD))
            else:
                prim = stage.GetPrimAtPath(path)
                needs_import = not prim or not prim.IsA(UsdGeom.Mesh)  # older stages held only a URI

        old = prev_nodes.get(node.id)
        if old is not None and prev_paths[node.id] == path and not needs_import and not stale(path):
            if old == node:
                continue
            if old.kind == node.kind and old.params == node.params and old.run == node.run:
                prim = stage.GetPrimAtPath(path)
                if old.transform != node.transform:
                    _author_transform(prim, node.transform, old.transform)
                if old.collider != node.collider:
                    prim.GetAttribute("agy:collider").Set(node.collider)
                if old.material_id != node.material_id:
                    _bind_material(stage, prim, root, node.material_id)
                continue
        _author_node(stage, path, node, root)

    # Deactivate whatever weaker layers still compose but the new IR dropped.
    # Collect first: changing `active` recomposes and would invalidate the range.
    drop: list[Usd.Prim] = []
    it = iter(Usd.PrimRange(root_prim, Usd.PrimIsActive))
    for p in it:
        path = p.GetPath()
        if path not in expected:
            drop.append(p)
            it.PruneChildren()
        elif path in opaque:
            it.PruneChildren()
    for p in drop:
        p.SetActive(False)
    _order_children(stage, scene, paths, root_prim)

    sided = {m.id for m in scene.materials if m.double_sided}
    if sided or any(m.double_sided for m in (prev.materials if prev else ())):  # nothing to set, nothing to clear
        sync_double_sided(stage, root_prim, sided)
    root_prim.SetCustomDataByKey(IR_KEY, scene.model_dump_json())


def _order_children(stage: Usd.Stage, scene: SceneIR, paths: dict[str, Sdf.Path], root_prim: Usd.Prim) -> None:
    """Puts each parent's nodes in the stage in the order ``scene.nodes`` lists them.

    A prim that an earlier edit authored stays where USD composed it, and a node added in the middle of a later
    design is composed after it, so the stage's order was the session's history: the same script gave the exported
    meshes (hence the GLB's vertex and index order and the manifest's part list) in a different order after another
    build than in a fresh session (measured with the round-six crate). The IR keeps the design's order; this writes
    it into the stage, as a children reorder in the edit target, only where the composed order differs, so an edit
    that appends a node, or changes nothing, authors nothing.
    """
    wanted: dict[Sdf.Path, list[str]] = {}
    for n in scene.nodes:
        path = paths[n.id]
        wanted.setdefault(path.GetParentPath(), []).append(path.name)
    for parent_path, names in wanted.items():
        parent = root_prim if parent_path == root_prim.GetPath() else stage.GetPrimAtPath(parent_path)
        if not parent:
            continue
        # all prims: the base prim is a class (abstract), which the default predicate does not traverse
        wanted_names = set(names)
        have = [c.GetName() for c in parent.GetFilteredChildren(Usd.PrimAllPrimsPredicate) if c.GetName() in wanted_names]
        if have != names:
            parent.SetChildrenReorder(names)


def resolve_anchor(scene: SceneIR, op: PlaceRelative, *, stage: Usd.Stage | None = None) -> Transform:
    """Resolve a one-shot world-space contact against the current composed geometry.

    on_top_of centres X/Y and contacts the upper Z face. flush_back aligns the +Y
    faces (positive gap insets the moving face) and preserves world X/Z.
    """
    if stage is None:
        stage = Usd.Stage.CreateInMemory()
        author_scene(stage, scene)
    paths = node_paths(scene, stage.GetDefaultPrim().GetPath())
    if op.target_id not in paths:
        raise KeyError(f"{op.op}: unknown target {op.target_id}")
    moving, target = paths[op.node_id], paths[op.target_id]
    if moving.HasPrefix(target) or target.HasPrefix(moving):
        raise ValueError("anchor nodes must belong to disjoint subtrees")
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])

    def bounds(path):
        box = cache.ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange()
        if box.IsEmpty():
            raise ValueError(f"{path}: anchor requires a nonempty bound")
        return np.array(box.GetMin()), np.array(box.GetMax())

    lo, hi = bounds(moving)
    other_lo, other_hi = bounds(target)
    delta = np.zeros(3)
    if op.op == "on_top_of":
        delta[:2] = (other_lo[:2] + other_hi[:2] - lo[:2] - hi[:2]) * 0.5
        delta[2] = other_hi[2] + op.gap_m - lo[2]
    else:
        delta[1] = other_hi[1] - op.gap_m - hi[1]
    prim = stage.GetPrimAtPath(moving)
    parent_to_world = np.array(UsdGeom.XformCache().GetParentToWorldTransform(prim))
    try:
        local_delta = np.linalg.solve(parent_to_world[:3, :3].T, delta)
    except np.linalg.LinAlgError as exc:
        raise ValueError(f"{moving}: anchor has singular parent transform") from exc
    transform = next(n.transform for n in scene.nodes if n.id == op.node_id)
    return transform.model_copy(update={"translate": tuple(transform.translate + local_delta)})
