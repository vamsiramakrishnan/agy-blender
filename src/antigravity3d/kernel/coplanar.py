"""Coplanar contact between the parts of one asset: exact coverage of a triangle by the faces of other parts that
lie in its plane.

Two uses share the geometry:

* hidden faces (``hidden_triangles``): a triangle of a closed, outward-oriented part A that is covered completely by
  the union of the *opposite-facing* coplanar faces of other closed parts is not on the boundary of the union of the
  solids: every point of it has A's interior behind it and another solid's interior in front of it. No ray from
  outside the union can reach it before entering a solid, so removing it from what ships changes no picture taken
  from outside (in front of the union boundary, with or without back-face culling), as long as the covering parts
  are opaque and drawn at the same level of detail.
* z-fighting (``same_facing_overlaps``): two parts with *same-facing* coplanar faces whose areas overlap: two
  surfaces at the same depth, which a depth buffer resolves by rounding noise.

Coplanar means: every corner of each triangle lies within ``plane_tol`` of the other triangle's plane (a gap or a
penetration larger than that is not contact: a gap is a slit that can be seen into, and a part sunk into another is
a different case this module does not claim). Coverage is decided exactly in the triangle's plane: the covers'
projections are subtracted from the triangle as convex pieces (Sutherland-Hodgman half-plane splits), and the
triangle counts as covered when no remaining piece is wider than ``plane_tol`` (rounding slivers along the shared
edges of two covers are thinner than that; a real uncovered strip is not). Faces that only touch along an edge
leave the triangle uncovered there and so never hide it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Plane distance and sliver width that count as "the same plane" / "nothing left uncovered", in metres, scaled by the
# asset's size (bounding-box diagonal, at least 1 m). Points are float32 in USD and in the GLB: one unit in the last
# place is 6e-8 m at 1 m and 1e-6 m at 16 m, so authored contact (exact in the IR) lands well inside this, while
# deliberate offsets (the smallest the authoring paths produce is millimetres) stay far outside it.
PLANE_TOL_PER_M = 2e-6


@dataclass(frozen=True)
class Part:
    node_id: str
    points: np.ndarray      # (V, 3) float64, world space
    triangles: np.ndarray   # (M, 3) int


def _frames(p: np.ndarray, t: np.ndarray):
    c = p[t]                                              # (M, 3, 3)
    n = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])
    ln = np.linalg.norm(n, axis=1)
    unit = n / np.maximum(ln, 1e-300)[:, None]
    return c, unit, ln / 2, c.min(axis=1), c.max(axis=1)


def _plane_basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors spanning the plane with unit normal ``n`` (written out: np.cross is slow on 3-vectors)."""
    x, y, z = float(n[0]), float(n[1]), float(n[2])
    u = (0.0, z, -y) if abs(x) < 0.9 else (-z, 0.0, x)  # n x (1,0,0) or n x (0,1,0)
    k = (u[0] * u[0] + u[1] * u[1] + u[2] * u[2]) ** 0.5
    u = (u[0] / k, u[1] / k, u[2] / k)
    v = (y * u[2] - z * u[1], z * u[0] - x * u[2], x * u[1] - y * u[0])
    return np.array(u), np.array(v)


# Small convex polygons (at most nine corners) as lists of (x, y) float pairs: plain Python is several times faster
# than numpy at this size.
Poly = list


def _in_plane(c: np.ndarray, u: np.ndarray, v: np.ndarray) -> Poly:
    """A triangle's corners in the plane basis (u, v), counter-clockwise."""
    q = [(float(x), float(y)) for x, y in zip(c @ u, c @ v)]
    d = (q[1][0] - q[0][0]) * (q[2][1] - q[0][1]) - (q[1][1] - q[0][1]) * (q[2][0] - q[0][0])
    return q if d >= 0 else q[::-1]


def _split(poly: Poly, a, b) -> tuple[Poly, Poly]:
    """(part left of a->b, part right of it) of convex ``poly``."""
    ax, ay, dx, dy = a[0], a[1], b[0] - a[0], b[1] - a[1]
    s = [dx * (y - ay) - dy * (x - ax) for x, y in poly]
    left, right = [], []
    n = len(poly)
    for i in range(n):
        p, q, sp, sq = poly[i], poly[(i + 1) % n], s[i], s[(i + 1) % n]
        if sp >= 0:
            left.append(p)
        if sp <= 0:
            right.append(p)
        if (sp > 0 > sq) or (sp < 0 < sq):
            t = sp / (sp - sq)
            x = (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
            left.append(x)
            right.append(x)
    return left, right


def _area(poly: Poly) -> float:
    n = len(poly)
    if n < 3:
        return 0.0
    return 0.5 * abs(sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n)))


def _width(poly: Poly) -> float:
    """Twice the area over the longest edge: the thickness of a sliver (0 for a degenerate piece)."""
    n = len(poly)
    if n < 3:
        return 0.0
    e = max(((poly[(i + 1) % n][0] - poly[i][0]) ** 2 + (poly[(i + 1) % n][1] - poly[i][1]) ** 2) for i in range(n)) ** 0.5
    return 2.0 * _area(poly) / max(e, 1e-300)


def _subtract(pieces: list[Poly], tri: Poly, tol: float) -> list[Poly]:
    """Convex pieces minus the convex (CCW) triangle ``tri``; pieces thinner than ``tol`` are dropped."""
    out = []
    for poly in pieces:
        rest = poly
        for k in range(3):
            inside, outside = _split(rest, tri[k], tri[(k + 1) % 3])
            if len(outside) >= 3 and _width(outside) > tol:
                out.append(outside)
            rest = inside
            if len(rest) < 3 or _width(rest) <= tol:
                break
    return out


def _overlap(t2: Poly, c2: Poly) -> Poly:
    poly = t2
    for k in range(3):
        poly = _split(poly, c2[k], c2[(k + 1) % 3])[0]
        if len(poly) < 3:
            break
    return poly


def _sat_overlap(ta: np.ndarray, tb: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Per pair of (nearly) coplanar triangles, the smallest overlap of their projections over the six in-plane
    edge normals (negative: separated). Their intersection lies in a slab that thick, and an intersection of two
    triangles (at most six sides, so its longest side is at least a third of its diameter) within a slab of
    thickness h has width (twice the area over the longest side) at most 6 h."""
    edges = np.concatenate([np.roll(ta, -1, axis=1) - ta, np.roll(tb, -1, axis=1) - tb], axis=1)  # (n, 6, 3)
    m = np.broadcast_to(n[:, None, :], edges.shape)
    ax = np.stack([m[..., 1] * edges[..., 2] - m[..., 2] * edges[..., 1], m[..., 2] * edges[..., 0] - m[..., 0] * edges[..., 2],
                   m[..., 0] * edges[..., 1] - m[..., 1] * edges[..., 0]], axis=-1)
    ax /= np.maximum(np.linalg.norm(ax, axis=2, keepdims=True), 1e-300)
    pa = np.einsum("nak,nck->nac", ax, ta)
    pb = np.einsum("nak,nck->nac", ax, tb)
    over = np.minimum(pa.max(axis=2), pb.max(axis=2)) - np.maximum(pa.min(axis=2), pb.min(axis=2))
    return over.min(axis=1)


def asset_tolerance(parts: list[Part]) -> float:
    pts = [np.asarray(p.points, np.float64) for p in parts if len(p.triangles)]
    if not pts:
        return PLANE_TOL_PER_M
    lo = np.min([q.min(axis=0) for q in pts], axis=0)
    hi = np.max([q.max(axis=0) for q in pts], axis=0)
    return PLANE_TOL_PER_M * max(1.0, float(np.linalg.norm(hi - lo)))


@dataclass
class Contacts:
    """Per part i: ``opposite[i][a]`` / ``same[i][a]`` = list of (part j, triangle b) whose triangle is coplanar with
    triangle a of part i, faces the other way / the same way, and overlaps it in more than a sliver."""
    opposite: list[dict[int, list[tuple[int, int]]]]
    same: list[dict[int, list[tuple[int, int]]]]
    tol: float
    frames: list


def coplanar_contacts(parts: list[Part], tol: float | None = None, *, facing: str = "both",
                      eligible: list[bool] | None = None, bounds: tuple[np.ndarray, np.ndarray] | None = None,
                      pairs=None, solid: list[bool] | None = None) -> Contacts:
    """Coplanar overlapping triangle pairs between different parts. ``facing``: "same", "opposite" or "both";
    ``eligible``: parts that take part (others are skipped). Pairs of triangles are narrowed in this order, each
    step cheap before the next: parts whose boxes touch (fast_ops' sweep), triangles near the other part's box,
    nearly parallel planes (|cos| > 0.99, the right sign), overlapping triangle boxes, every corner of each
    within ``tol`` of the other's plane; only then is the in-plane overlap computed exactly. ``bounds`` (per-part
    box corners) and ``pairs`` (part index pairs whose boxes touch within at least ``tol``) can be handed in by a
    caller that has them (tier1_qa), so they are not computed twice. ``solid`` (per part: closed, positive volume; None =
    all are): the same-facing box shortcut below holds for two solids only, so a pair with an open part (a decal, a
    card pressed on a surface) skips it."""
    from antigravity3d.kernel.fast_ops import _candidate_pairs

    tol = asset_tolerance(parts) if tol is None else tol
    eligible = [True] * len(parts) if eligible is None else eligible
    frames: list = [None] * len(parts)  # per-triangle data, built only for parts that meet a candidate partner

    def frame(i: int):
        if frames[i] is None:
            frames[i] = _frames(np.asarray(parts[i].points, np.float64), np.asarray(parts[i].triangles, np.int64))
        return frames[i]

    opposite: list[dict] = [{} for _ in parts]
    same: list[dict] = [{} for _ in parts]
    live = [i for i, p in enumerate(parts) if len(p.triangles) and eligible[i]]
    if len(live) < 2:
        return Contacts(opposite, same, tol, frames)
    if bounds is not None:
        lo, hi = np.asarray(bounds[0])[live], np.asarray(bounds[1])[live]
    else:
        used = [np.asarray(parts[i].points, np.float64)[np.unique(np.asarray(parts[i].triangles))] for i in live]
        lo = np.array([u.min(axis=0) for u in used])
        hi = np.array([u.max(axis=0) for u in used])
    if pairs is None:
        pairs = _candidate_pairs(lo, hi, tol)
    else:  # caller's part indices -> rows of lo/hi
        row = {i: k for k, i in enumerate(live)}
        pairs = [(row[i], row[j]) for i, j in pairs if i in row and j in row]
    # gather candidate triangle pairs of every part pair, then filter them all at once
    PI, PA, PJ, PB = [], [], [], []
    noise = PLANE_TOL_PER_M * max(1.0, float(np.linalg.norm(hi.max(axis=0) - lo.min(axis=0))))
    for a_, b_ in pairs:
        i, j = live[a_], live[b_]
        if ((np.minimum(hi[a_], hi[b_]) - np.maximum(lo[a_], lo[b_])) < -tol).any():
            continue  # a caller's pair from a wider sweep
        if facing == "same" and (solid is None or (solid[i] and solid[j])):
            # Same-facing faces of two solids that overlap in their plane put both interiors on the same side of it,
            # so the solids' boxes overlap by their thickness along the normal. Boxes that only touch on some axis
            # (bricks side by side or stacked: the common harmless case) are rejected here, before any triangle.
            ext = np.minimum(hi[a_], hi[b_]) - np.maximum(lo[a_], lo[b_])
            if (np.abs(ext) <= noise).any():
                continue
        _, ni, ai, li, hi_i = frame(i)
        _, nj, aj, lj, hj = frame(j)
        si = np.flatnonzero(np.all((li <= hi[b_] + tol) & (hi_i >= lo[b_] - tol), axis=1) & (ai > 0))
        sj = np.flatnonzero(np.all((lj <= hi[a_] + tol) & (hj >= lo[a_] - tol), axis=1) & (aj > 0))
        if not len(si) or not len(sj):
            continue
        dot = ni[si] @ nj[sj].T
        par = dot > 0.99 if facing == "same" else dot < -0.99 if facing == "opposite" else np.abs(dot) > 0.99
        x, y = np.nonzero(par)
        if len(x):
            PI.append(np.full(len(x), i)); PA.append(si[x]); PJ.append(np.full(len(x), j)); PB.append(sj[y])
    if not PI:
        return Contacts(opposite, same, tol, frames)
    I, A, J, B = (np.concatenate(v) for v in (PI, PA, PJ, PB))
    base = np.zeros(len(parts) + 1, np.int64)
    base[1:] = np.cumsum([0 if f is None else len(f[0]) for f in frames])  # offsets into the built frames
    corners = np.concatenate([f[0] for f in frames if f is not None])
    normals = np.concatenate([f[1] for f in frames if f is not None])
    tlo = np.concatenate([f[3] for f in frames if f is not None])
    thi = np.concatenate([f[4] for f in frames if f is not None])
    ga, gb = base[I] + A, base[J] + B
    keep = np.all((tlo[ga] <= thi[gb] + tol) & (thi[ga] >= tlo[gb] - tol), axis=1)
    ca, cb, na, nb = corners[ga], corners[gb], normals[ga], normals[gb]
    dij = np.abs(np.einsum("nk,nck->nc", nb, ca - cb[:, :1])).max(axis=1)
    dji = np.abs(np.einsum("nk,nck->nc", na, cb - ca[:, :1])).max(axis=1)
    keep &= (dij <= tol) & (dji <= tol)
    # separating axes (in-plane edge normals of both): a pair within a slab of tol/6 cannot overlap by more than
    # tol in width (see _sat_overlap)
    sel = np.flatnonzero(keep)
    sel = sel[_sat_overlap(ca[sel], cb[sel], na[sel]) > tol / 6]
    for k in sel.tolist():
        i, a1, j, b1 = int(I[k]), int(A[k]), int(J[k]), int(B[k])
        u, v = _plane_basis(na[k])
        poly = _overlap(_in_plane(ca[k], u, v), _in_plane(cb[k], u, v))
        if len(poly) < 3 or _width(poly) <= tol:
            continue  # touching along an edge or a corner, or apart
        book = same if float(na[k] @ nb[k]) > 0 else opposite
        book[i].setdefault(a1, []).append((j, b1))
        book[j].setdefault(b1, []).append((i, a1))
    return Contacts(opposite, same, tol, frames)


def covered(contacts: Contacts, i: int, a: int, allowed) -> bool:
    """Is triangle a of part i covered (up to slivers thinner than tol) by the union of its opposite-facing
    coplanar contacts in parts for which ``allowed(j)`` holds?"""
    covers = [(j, b) for j, b in contacts.opposite[i].get(a, []) if allowed(j)]
    if not covers:
        return False
    ci, ni = contacts.frames[i][0], contacts.frames[i][1]
    u, v = _plane_basis(ni[a])
    pieces = [_in_plane(ci[a], u, v)]
    for j, b in covers:
        cj = contacts.frames[j][0][b]
        pieces = _subtract(pieces, _in_plane(cj, u, v), contacts.tol)
        if not pieces:
            return True
    return False


def hidden_triangles(parts: list[Part], solid: list[bool], opaque: list[bool] | None = None,
                     tol: float | None = None) -> list[np.ndarray]:
    """Per part, a boolean mask of the triangles that are covered completely by opposite-facing coplanar faces of
    other parts. Only a ``solid`` part (closed, consistently wound, positive volume) can lose a triangle, and only
    solid ``opaque`` parts can cover one: an open sheet has nothing behind a removed triangle, and a see-through
    cover would show it."""
    opaque = [True] * len(parts) if opaque is None else opaque
    ok = [s_ and o for s_, o in zip(solid, opaque)]
    c = coplanar_contacts(parts, tol, facing="opposite", eligible=ok)
    out = []
    for i, p in enumerate(parts):
        mask = np.zeros(len(p.triangles), bool)
        if ok[i]:
            for a in c.opposite[i]:
                mask[a] = covered(c, i, a, lambda j: ok[j])
        out.append(mask)
    return out


def same_facing_overlaps(parts: list[Part], tol: float | None = None, *, bounds=None, pairs=None,
                         solid: list[bool] | None = None) -> list[tuple[int, int, float]]:
    """(part i, part j, overlapping area in m^2) for every pair of parts with same-facing coplanar overlap
    (faces within ``tol`` of one plane whose overlap is wider than ``tol``)."""
    c = coplanar_contacts(parts, tol, facing="same", bounds=bounds, pairs=pairs, solid=solid)
    acc: dict[tuple[int, int], float] = {}
    for i in range(len(parts)):
        ci, ni = (c.frames[i][0], c.frames[i][1]) if c.frames[i] is not None else (None, None)
        for a, lst in c.same[i].items():
            for j, b in lst:
                if j <= i:
                    continue
                u, v = _plane_basis(ni[a])
                poly = _overlap(_in_plane(ci[a], u, v), _in_plane(c.frames[j][0][b], u, v))
                acc[(i, j)] = acc.get((i, j), 0.0) + _area(poly)
    return [(i, j, a) for (i, j), a in sorted(acc.items())]


def opaque_materials(scene) -> frozenset[str]:
    """Ids of the scene's materials that nothing can be seen through: alpha mode OPAQUE and no transmission. A
    part bound to any other material (or to none known here) neither loses nor hides a contact face."""
    return frozenset(m.id for m in scene.materials if m.alpha_mode == "OPAQUE" and m.transmission == 0.0)
