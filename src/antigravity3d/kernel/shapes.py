"""Geometry of the shaped primitives: bevelled / inset boxes, bevelled cylinders, lathes and extrusions.

Every builder returns ``(points float32 (N, 3), triangles int32 (M, 3), charts int32 (M,))``: a closed, outward-wound
mesh without degenerate triangles, in a vertex and triangle order that depends only on the parameters (packages are
byte-reproducible), and the exact developable UV chart of every triangle (see ``compile_ir.primitive_charts``).

Charts. A chart is unfolded isometrically, triangle by triangle, along a breadth-first tree (``fast_ops._unfold_charts``),
so a chart made of developable pieces joined along straight edges, with no interior vertex of non-zero angle defect,
unfolds with zero area and angle distortion. A closed ring of such pieces is cut wherever the two fronts of the search
meet, which can be the diagonal of a large flat face; every ring here is therefore split into two open halves at
chosen edges. The rounded corners of a box are the only non-developable surface: they are cut along latitude rings
about local Z (the vertices are a product of a latitude and a longitude profile, so each band between two rings is a
ring of planar trapezoids, which is developable) and each band joins the bevel strips of the same latitude.

Triangle cost (:func:`triangle_count` in ``compile_ir``) is pinned against these builders by the tests.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

# The crease angle normals are smoothed within (compile_ir.CREASE_DEG; duplicated here to avoid an import cycle,
# test_shapes pins them equal). A 2-segment quarter arc sampled uniformly turns 22.5 / 45 / 22.5 degrees, and its
# middle 45 creases: the edge would shade as two flat facets. Moving the middle vertex inward along the diagonal until
# the turns are 26 / 38 / 26 stops the crease on the edge itself, but crease_normals then still split the normals at
# each corner's three face-corner vertices by up to 32 degrees (a strip and the corner quad beside it differ by more
# than 40), against 6.8 at 30 / 30 / 30 (measured on a 1 x 0.6 x 0.4 box, bevel 0.05). So the two segments turn 30
# degrees each, like the middle turn of three; the edge's middle lies 10 % of the radius inside the true arc
# (1 mm on a 1 cm bevel) and the extents are unchanged. One segment (45) is a flat chamfer by design.
CREASE_DEG = 40.0
_S2_END_TURN_DEG = 30.0

BOX_FACES = ("+x", "-x", "+y", "-y", "+z", "-z")


def quarter(s: int) -> list[tuple[float, float]]:
    """``s + 1`` points of a unit quarter profile from (1, 0) to (0, 1), convex, every turn below CREASE_DEG for s >= 2."""
    if s == 2:
        m = 1.0 / (1.0 + math.tan(math.radians(_S2_END_TURN_DEG)))
        return [(1.0, 0.0), (m, m), (0.0, 1.0)]
    return [(math.cos(k * math.pi / (2 * s)), math.sin(k * math.pi / (2 * s))) if 0 < k < s else ((1.0, 0.0) if k == 0 else (0.0, 1.0))
            for k in range(s + 1)]


# --------------------------------------------------------------------------- 2D polygon checks


def _seg_hit(p1, p2, q1, q2, tol: float) -> bool:
    """True when closed segments p1p2 and q1q2 share a point (touching counts)."""
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on(a, b, c):  # c on segment ab, given collinear
        return min(a[0], b[0]) - tol <= c[0] <= max(a[0], b[0]) + tol and min(a[1], b[1]) - tol <= c[1] <= max(a[1], b[1]) + tol

    d1, d2, d3, d4 = orient(q1, q2, p1), orient(q1, q2, p2), orient(p1, p2, q1), orient(p1, p2, q2)
    if ((d1 > tol and d2 < -tol) or (d1 < -tol and d2 > tol)) and ((d3 > tol and d4 < -tol) or (d3 < -tol and d4 > tol)):
        return True
    return ((abs(d1) <= tol and on(q1, q2, p1)) or (abs(d2) <= tol and on(q1, q2, p2))
            or (abs(d3) <= tol and on(p1, p2, q1)) or (abs(d4) <= tol and on(p1, p2, q2)))


def loop_problem(pts: list[tuple[float, float]], what: str) -> str | None:
    """Why the closed polygon ``pts`` is not simple, or None. Indices in the message are the author's (0-based)."""
    n = len(pts)
    span = max(max(abs(c) for p in pts for c in p), 1e-12)
    tol = 1e-12 * span * span
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            c, d = pts[j], pts[(j + 1) % n]
            if j == i + 1 or (i == 0 and j == n - 1):  # neighbours share a vertex: only a fold back onto itself is wrong
                shared, x, y = (b, a, d) if j == i + 1 else (a, b, c)
                ux, uy, vx, vy = x[0] - shared[0], x[1] - shared[1], y[0] - shared[0], y[1] - shared[1]
                if abs(ux * vy - uy * vx) <= tol and ux * vx + uy * vy > 0:
                    return f"{what} folds back on itself at point {(i + 1) % n if j == i + 1 else 0}"
                continue
            if _seg_hit(a, b, c, d, tol):
                return f"{what} crosses or touches itself (segment {i}-{(i + 1) % n} meets segment {j}-{(j + 1) % n})"
    return None


def signed_area(pts: list[tuple[float, float]]) -> float:
    n = len(pts)
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))


def clean_points(raw, nid: str, name: str, *, min_len: int, closed: bool) -> list[tuple[float, float]]:
    """Validated 2D points: finite pairs, no zero-length segment. A closed list may repeat its first point at the end
    (dropped: authors often close a loop explicitly)."""
    if not isinstance(raw, (list, tuple)) or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in raw):
        raise ValueError(f"{nid}: param '{name}' must be a list of (a, b) pairs, got {raw!r}")
    pts = []
    for p in raw:
        if not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) for c in p):
            raise ValueError(f"{nid}: param '{name}' must hold finite numbers, got {tuple(p)!r}")
        pts.append((float(p[0]), float(p[1])))
    span = max([abs(c) for p in pts for c in p] + [1e-12])
    eps = 1e-9 * span
    same = lambda a, b: abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps  # noqa: E731
    if closed and len(pts) > 1 and same(pts[0], pts[-1]):
        pts = pts[:-1]
    for i in range(len(pts) - (0 if closed else 1)):
        if same(pts[i], pts[(i + 1) % len(pts)]):
            raise ValueError(f"{nid}: {name} points {i} and {(i + 1) % len(pts)} coincide (a zero-length segment); remove one")
    if len(pts) < min_len:
        raise ValueError(f"{nid}: {name} needs at least {min_len} distinct points, got {len(pts)}")
    return pts


# --------------------------------------------------------------------------- box


class _Builder:
    def __init__(self) -> None:
        self.index: dict[tuple, int] = {}
        self.pts: list[tuple[float, float, float]] = []
        self.tris: list[tuple[int, int, int]] = []
        self.refs: list[tuple[float, float, float] | None] = []  # outward reference direction, None = the centroid
        self.labels: list[tuple] = []

    def v(self, key: tuple, pos) -> int:
        i = self.index.get(key)
        if i is None:
            i = self.index[key] = len(self.pts)
            self.pts.append(tuple(float(c) for c in pos))
        return i

    def quad(self, a: int, b: int, c: int, d: int, label: tuple, ref=None) -> None:
        self.tri(a, b, c, label, ref)
        self.tri(a, c, d, label, ref)

    def tri(self, a: int, b: int, c: int, label: tuple, ref=None) -> None:
        self.tris.append((a, b, c))
        self.refs.append(ref)
        self.labels.append(label)

    def finish(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p = np.asarray(self.pts, np.float64)
        t = np.asarray(self.tris, np.int64)
        n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
        ref = np.array([r if r is not None else p[tri].mean(axis=0) for r, tri in zip(self.refs, t)])
        flip = (n * ref).sum(axis=1) < 0
        t[flip] = t[flip][:, [0, 2, 1]]
        ids: dict[tuple, int] = {}
        chart = np.array([ids.setdefault(lb, len(ids)) for lb in self.labels], np.int32)
        return p.astype(np.float32), t.astype(np.int32), chart


def box_problem(size, bevel: float, segments: int, inset_faces, margin: float, depth: float) -> str | None:
    """Why these box parameters cannot be built, or None."""
    h = [s / 2 for s in size]
    if bevel >= min(h):
        return f"bevel {bevel} must be below half the smallest extent ({min(h)})"
    if not inset_faces:
        return None
    bad = [f for f in inset_faces if f not in BOX_FACES]
    if bad:
        return f"inset_faces {bad} are not face names; use {list(BOX_FACES)}"
    if len(set(inset_faces)) != len(inset_faces):
        return f"inset_faces {list(inset_faces)} names a face twice"
    if depth <= 0:
        return "inset_depth must be > 0 when inset_faces is set (a panel with no depth is no recess)"
    if margin <= 0:
        return "inset_margin must be > 0 when inset_faces is set (without a frame the recess cuts the edge away)"
    axis_of = {"x": 0, "y": 1, "z": 2}
    for f in inset_faces:
        ax = axis_of[f[1]]
        for o in (k for k in range(3) if k != ax):
            if h[o] - bevel - margin <= 0:
                return (f"inset_margin {margin} leaves no panel on face {f}: the flat part of the face is "
                        f"{2 * (h[o] - bevel):.6g} wide along {'xyz'[o]} inside the bevel, and the frame takes {2 * margin:.6g}")
        both = sum(1 for g in inset_faces if g[1] == f[1])
        if depth * both >= 2 * h[ax]:
            other = "-" if f[0] == "+" else "+"
            reach = (f"into the inset from the opposite face {other}{f[1]}" if both == 2 else "through the box")
            return f"inset_depth {depth} on face {f} reaches {reach} (extent along {f[1]} is {2 * h[ax]:.6g})"
    for f in inset_faces:
        for g in inset_faces:
            if f[1] < g[1] and depth >= bevel + margin:
                return (f"inset_depth {depth} on adjacent faces {f} and {g} makes the two recesses cut into each other: "
                        f"keep inset_depth below bevel + inset_margin ({bevel + margin:.6g})")
    return None


@lru_cache(maxsize=512)
def _box(size: tuple[float, float, float], b: float, s: int, insets: tuple[str, ...], m: float, d: float):
    h = np.asarray(size, np.float64) / 2
    a = h - b
    q = quarter(s) if b > 0 else [(1.0, 0.0)]
    S = s if b > 0 else 0
    B = _Builder()

    def cv(sx: int, sy: int, sz: int, i: int, j: int) -> int:
        """Vertex of the octant (sx, sy, sz): latitude i from the pole (0) to the equator (S), longitude j from +x (0)
        to +y (S). The pole is one vertex."""
        if b == 0:
            return B.v(("c", sx, sy, sz), (sx * h[0], sy * h[1], sz * h[2]))
        if i == 0:
            j = 0
        r, z = q[i][1], q[i][0]
        return B.v(("c", sx, sy, sz, i, j), (sx * (a[0] + b * r * q[j][0]), sy * (a[1] + b * r * q[j][1]), sz * (a[2] + b * z)))

    def face_corner(axis: int, sg: int, su: int, sv: int) -> int:
        sgn = [0, 0, 0]
        u, v = [k for k in range(3) if k != axis]
        sgn[axis], sgn[u], sgn[v] = sg, su, sv
        i, j = {2: (0, 0), 0: (S, 0), 1: (S, S)}[axis]
        return cv(*sgn, i, j)

    def label_of_face(axis: int, sg: int) -> tuple:
        if b == 0:
            return ("face", axis, sg)
        if axis == 2:
            return ("cap", sg)
        half = "A" if (axis, sg) in ((1, 1), (0, -1)) else "B"  # +y and -x in one half of the side band, -y and +x in the other
        return ("side", half)

    cyc = ((-1, -1), (1, -1), (1, 1), (-1, 1))
    for name in ("-z", "+z", "-y", "+y", "-x", "+x"):
        axis, sg = "xyz".index(name[1]), (1 if name[0] == "+" else -1)
        u, v = [k for k in range(3) if k != axis]
        normal = tuple(float(sg) if k == axis else 0.0 for k in range(3))
        lab = label_of_face(axis, sg)
        outer = [face_corner(axis, sg, su, sv) for su, sv in cyc]
        if name not in insets:
            B.quad(*outer, lab, normal)
            continue

        def at(su: int, sv: int, depth: float) -> tuple:
            p = [0.0, 0.0, 0.0]
            p[axis] = sg * (h[axis] - depth)
            p[u], p[v] = su * (a[u] - m), sv * (a[v] - m)
            return tuple(p)

        inner = [B.v(("i", name, su, sv), at(su, sv, 0.0)) for su, sv in cyc]
        panel = [B.v(("p", name, su, sv), at(su, sv, d)) for su, sv in cyc]
        for k in range(4):  # frame side k and the wall below it: one chart, a trapezoid folded onto a rectangle
            B.quad(outer[k], outer[(k + 1) % 4], inner[(k + 1) % 4], inner[k], ("rim", name, k), normal)
            mid = np.mean([B.pts[x] for x in (inner[k], inner[(k + 1) % 4])], axis=0)
            ref = tuple(0.0 if x == axis else -float(mid[x]) for x in range(3))  # a wall faces the recess's axis
            B.quad(inner[k], inner[(k + 1) % 4], panel[(k + 1) % 4], panel[k], ("rim", name, k), ref)
        B.quad(*panel, ("panel", name), normal)
    if S:
        for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):  # vertical edges: the side band
            for j in range(S):
                B.quad(cv(sx, sy, 1, S, j), cv(sx, sy, 1, S, j + 1), cv(sx, sy, -1, S, j + 1), cv(sx, sy, -1, S, j),
                       ("side", "A" if sy > 0 else "B"))
        for sz in (1, -1):
            for sy in (1, -1):  # edges along x, on the +y / -y side
                for i in range(S):
                    B.quad(cv(1, sy, sz, i, S), cv(1, sy, sz, i + 1, S), cv(-1, sy, sz, i + 1, S), cv(-1, sy, sz, i, S),
                           ("band", sz, i, "y", sy))
            for sx in (1, -1):  # edges along y, on the +x / -x side
                for i in range(S):
                    B.quad(cv(sx, 1, sz, i, 0), cv(sx, 1, sz, i + 1, 0), cv(sx, -1, sz, i + 1, 0), cv(sx, -1, sz, i, 0),
                           ("band", sz, i, "x", sx))
            for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):  # corners
                # columns nearer +-x (j < S / 2) go with the edge along y, the others with the edge along x
                lab = lambda i, j, sx=sx, sy=sy: ("band", sz, i, "x", sx) if 2 * j + 1 < S + (S % 2) else ("band", sz, i, "y", sy)  # noqa: E731
                for j in range(S):
                    B.tri(cv(sx, sy, sz, 0, 0), cv(sx, sy, sz, 1, j), cv(sx, sy, sz, 1, j + 1), lab(0, j))
                for i in range(1, S):
                    for j in range(S):
                        B.quad(cv(sx, sy, sz, i, j), cv(sx, sy, sz, i, j + 1), cv(sx, sy, sz, i + 1, j + 1), cv(sx, sy, sz, i + 1, j), lab(i, j))
    return B.finish()


def box(size, bevel: float, segments: int, inset_faces, margin: float, depth: float):
    """Bevelled and / or inset box (see module docstring). Raises ValueError (without the node id) on bad input."""
    if why := box_problem(size, bevel, segments, inset_faces, margin, depth):
        raise ValueError(why)
    p, t, c = _box(tuple(float(x) for x in size), float(bevel), int(segments), tuple(f for f in BOX_FACES if f in inset_faces),
                   float(margin), float(depth))
    return p.copy(), t.copy(), c.copy()


def box_tris(bevel: float, segments: int, n_insets: int) -> int:
    s = segments if bevel > 0 else 0
    return 12 + 16 * s * s + 16 * s + 16 * n_insets


# --------------------------------------------------------------------------- lathe


def rounded_cylinder_profile(r: float, h: float, b: float, s: int) -> list[tuple[float, float]]:
    """Top-to-bottom (r, z) profile of a cylinder whose cap rims are rounded with radius ``b`` in ``s`` segments."""
    q = quarter(s)
    top = [(r - b + b * qy, h / 2 - b + b * qx) for qx, qy in q]
    return [(0.0, h / 2)] + top + [(x, -z) for x, z in reversed(top)] + [(0.0, -h / 2)]


def lathe_rows(profile: list[tuple[float, float]], closed: bool, nid: str = "lathe") -> list[tuple[float, float]]:
    """The rows ``compile_ir._lathe`` turns: the profile oriented so its normals face out, open ends off the axis capped
    with a pole at the same height. Raises ValueError with the reason a profile cannot be turned."""
    pts = clean_points(profile, nid, "profile", min_len=3 if closed else 2, closed=closed)
    if any(r < 0 for r, _ in pts):
        k = next(i for i, (r, _) in enumerate(pts) if r < 0)
        raise ValueError(f"{nid}: profile point {k} has negative radius {pts[k][0]}; radius is the distance from the axis, >= 0")
    if closed:
        if any(r == 0 for r, _ in pts):
            raise ValueError(f"{nid}: a closed profile must not touch the axis (point "
                             f"{next(i for i, (r, _) in enumerate(pts) if r == 0)} has radius 0); use closed=false for a solid of revolution")
        loop = list(pts)
    else:
        if any(r == 0 for r, _ in pts[1:-1]):
            k = next(i for i, (r, _) in enumerate(pts) if r == 0 and 0 < i < len(pts) - 1)
            raise ValueError(f"{nid}: profile point {k} lies on the axis between the ends: the solid would pinch to a point there; "
                             "split it into two lathes")
        loop = ([(0.0, pts[0][1])] if pts[0][0] > 0 else []) + pts + ([(0.0, pts[-1][1])] if pts[-1][0] > 0 else [])
        if len(loop) < 3:
            raise ValueError(f"{nid}: profile {pts} encloses nothing (both points on the axis)")
    if why := loop_problem(loop, "profile" if closed else "profile (with its caps)"):
        raise ValueError(f"{nid}: {why}")
    area = signed_area(loop)
    span = max(max(abs(c) for p in loop for c in p), 1e-12)
    if abs(area) <= 1e-12 * span * span:
        raise ValueError(f"{nid}: profile encloses no area")
    return loop if area < 0 else loop[::-1]  # clockwise in (r, z): normals to the left of travel face out


def lathe_tris(profile, closed: bool, segments: int) -> int:
    """Triangles of a lathe from its parameters, without checking them (the cost a design pays, see triangle_count)."""
    pts = [(float(r), float(z)) for r, z in profile]
    if closed:
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        return 2 * segments * len(pts)
    rows = len(pts) + (pts[0][0] > 0) + (pts[-1][0] > 0)
    poles = 2  # every open lathe ends in a pole at each end, given or added as a cap
    return segments * (2 * (rows - 1) - poles)


# --------------------------------------------------------------------------- extrude


def extrude_outline(outline, nid: str = "extrude") -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """The outline counter-clockwise (a clockwise one reversed) and its cap triangulation; raises ValueError with the
    reason an outline cannot be extruded. Cheap: no mesh."""
    from antigravity3d.kernel.fast_ops import _ear_clip

    pts = clean_points(outline, nid, "outline", min_len=3, closed=True)
    if why := loop_problem(pts, "outline"):
        raise ValueError(f"{nid}: {why}")
    area = signed_area(pts)
    span = max(max(abs(c) for p in pts for c in p), 1e-12)
    if abs(area) <= 1e-12 * span * span:
        raise ValueError(f"{nid}: outline encloses no area")
    if area < 0:
        pts = pts[::-1]
    xy = np.asarray(pts, np.float64)
    cap = _ear_clip(list(range(len(pts))), [tuple(p) for p in xy], 1e-14 * span * span)
    if cap is None:
        raise ValueError(f"{nid}: outline could not be triangulated without zero-area triangles (remove points that lie on a straight edge)")
    return xy, cap


def extrude(outline, depth: float, nid: str = "extrude"):
    """Prism of a simple polygon in local XY, ``depth`` along Z, centred. CW outlines are reversed."""
    xy, cap = extrude_outline(outline, nid)
    k = len(xy)
    z = depth / 2
    p = np.concatenate([np.c_[xy, np.full(k, -z)], np.c_[xy, np.full(k, z)]])
    tris, charts = [], []
    # side runs: cut at sharp corners (they are seams anyway), and at least twice so that no run closes into a ring
    turn = []
    for i in range(k):
        e0, e1 = xy[i] - xy[i - 1], xy[(i + 1) % k] - xy[i]
        ang = math.degrees(abs(math.atan2(e0[0] * e1[1] - e0[1] * e1[0], float(e0 @ e1))))
        turn.append(ang)
    cuts = [i for i in range(k) if turn[i] > CREASE_DEG]
    if len(cuts) == 0:
        cuts = [0, k // 2]
    elif len(cuts) == 1:
        cuts = sorted({cuts[0], (cuts[0] + k // 2) % k})
    run_of = [0] * k
    for i in range(k):  # side quad i spans outline points i -> i + 1; its run starts at the last cut at or before i
        before = [c for c in cuts if c <= i]
        run_of[i] = cuts.index(before[-1]) if before else len(cuts) - 1
    for a, b_, c in cap:
        tris.append((a + k, b_ + k, c + k))
        charts.append(0)
    for a, b_, c in cap:
        tris.append((a, c, b_))
        charts.append(1)
    for i in range(k):
        j = (i + 1) % k
        tris += [(i, j, j + k), (i, j + k, i + k)]
        charts += [2 + run_of[i]] * 2
    return p.astype(np.float32), np.asarray(tris, np.int32), np.asarray(charts, np.int32)


def extrude_tris(outline) -> int:
    pts = [tuple(p) for p in outline]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return 4 * len(pts) - 4
