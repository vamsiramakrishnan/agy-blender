"""Repair of a collider's input mesh: a private copy, the rendered mesh is never touched.

CoACD decomposes a solid. Given a mesh that is not one closed manifold body, its own repair pass rebuilds the surface at a
fixed resolution, which produces a dense double-walled shell (tens to hundreds of thousands of triangles) and can take minutes
to decompose. What an imported mesh usually needs is smaller: weld, drop duplicate faces, fix winding, close small holes, pull
bodies apart where they only touch. This does exactly that, so the result can be decomposed with the repair pass off.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from antigravity3d.kernel.fast_ops import _canon

# A boundary loop is a defect, not a feature, when the patch that closes it is small against the body's own surface. A
# cup's mouth is a tenth to a half of the cup's surface; a hole worth closing is a few triangles of a few thousand.
CAP_AREA_FRACTION = 0.05


@dataclass
class Body:
    points: np.ndarray
    triangles: np.ndarray
    kind: str                        # "closed": closed manifold, consistently wound, outward: fit for the repair-free decomposition
                                     # "pinched": no boundary, but the surface still touches itself at an edge or vertex
                                     # "open": has boundary edges (an open surface)
    volume: float = 0.0              # enclosed volume of a closed body


@dataclass
class Repair:
    bodies: list[Body]
    notes: dict[str, int] = field(default_factory=dict)


def _rotation_sign(f: np.ndarray) -> np.ndarray:
    """+1 where the face's cyclic order matches its sorted vertex order, else -1 (the winding relative to the vertex set)."""
    k = np.argmin(f, axis=1)
    r = np.stack([f[np.arange(len(f)), k], f[np.arange(len(f)), (k + 1) % 3], f[np.arange(len(f)), (k + 2) % 3]], axis=1)
    return np.where(r[:, 1] < r[:, 2], 1, -1)


def _drop_duplicates(f: np.ndarray) -> tuple[np.ndarray, int]:
    """Faces over the same three vertices: back-to-back pairs enclose nothing and both go; of equal copies one stays."""
    key = np.sort(f, axis=1)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    if not (cnt > 1).any():
        return f, 0
    sign = _rotation_sign(f)
    keep = np.ones(len(f), bool)
    for g in np.flatnonzero(cnt > 1):
        idx = np.flatnonzero(inv == g)
        pos, neg = idx[sign[idx] > 0], idx[sign[idx] < 0]
        pair = min(len(pos), len(neg))
        left = np.concatenate([pos[pair:], neg[pair:]])
        keep[idx] = False
        if len(left):
            keep[left[0]] = True
    return f[keep], int((~keep).sum())


def _pair_at_nonmanifold_edges(f: np.ndarray, pts: np.ndarray, edge_faces: dict[tuple[int, int], list[int]]) -> list[tuple[int, int]]:
    """Which faces continue which at an edge shared by four or more faces.

    Seen along the edge, a face leaves the material on one side; sorted by angle around the edge, each face that traverses
    the edge backwards is followed by the face that closes its wedge of material. Faces whose directions disagree stay unpaired
    (the edge is then a boundary of their bodies)."""
    links = []
    for (a, b), fs in edge_faces.items():
        axis = pts[b] - pts[a]
        axis = axis / np.linalg.norm(axis)
        u = np.cross(axis, [1.0, 0.0, 0.0] if abs(axis[0]) < 0.9 else [0.0, 1.0, 0.0])
        u /= np.linalg.norm(u)
        w = np.cross(axis, u)
        rows = []
        for fi in fs:
            t = f[fi].tolist()
            third = t[(t.index(a) + 2) % 3] if t[(t.index(a) + 1) % 3] == b else t[(t.index(b) + 2) % 3]
            forward = t[(t.index(a) + 1) % 3] == b            # the face runs a -> b
            d = pts[third] - pts[a]
            ang = float(np.arctan2(np.dot(d, w), np.dot(d, u)))
            rows.append((ang, forward, fi))
        rows.sort()
        if sum(r[1] for r in rows) * 2 != len(rows):
            continue
        n = len(rows)
        for i in range(n):
            if not rows[i][1] and rows[(i + 1) % n][1]:       # a backwards face, then the forward face closing the wedge
                links.append((rows[i][2], rows[(i + 1) % n][2]))
    return links


def _components(count: int, links: np.ndarray) -> tuple[int, np.ndarray]:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(links):
        return count, np.arange(count)
    g = coo_matrix((np.ones(len(links)), (links[:, 0], links[:, 1])), shape=(count, count))
    return connected_components(g, directed=False)


def _consistent_winding(f: np.ndarray, links: np.ndarray) -> tuple[np.ndarray, int]:
    """Flip faces so that linked faces run their shared edge in opposite directions; each connected set keeps its first face."""
    if not len(links):
        return f, 0
    n = int(f.max()) + 1
    de = np.stack([f[:, 0] * n + f[:, 1], f[:, 1] * n + f[:, 2], f[:, 2] * n + f[:, 0]], axis=1)
    dp, dq = de[links[:, 0]], de[links[:, 1]]
    same = (dp[:, :, None] == dq[:, None, :]).any(axis=(1, 2))      # both run the shared edge the same way: they disagree
    if not same.any():
        return f, 0
    nbr: dict[int, list[tuple[int, bool]]] = {}
    for (p, q), s in zip(links.tolist(), same.tolist()):
        nbr.setdefault(p, []).append((q, s))
        nbr.setdefault(q, []).append((p, s))
    flipped = np.zeros(len(f), bool)
    seen = np.zeros(len(f), bool)
    for start in sorted(nbr):
        if seen[start]:
            continue
        seen[start] = True
        stack = [start]
        while stack:
            p = stack.pop()
            for q, disagree in nbr[p]:
                if not seen[q]:
                    seen[q] = True
                    flipped[q] = flipped[p] != disagree
                    stack.append(q)
    out = f.copy()
    out[flipped] = out[flipped][:, ::-1]
    return out, int(flipped.sum())


def _unfuse(f: np.ndarray, links: np.ndarray, nverts: int) -> tuple[np.ndarray, np.ndarray]:
    """Give every fan of faces around a vertex its own copy of the vertex, where a fan is the faces joined through links.

    That pulls bodies apart at non-manifold edges and vertices. Returns the new faces and, per new vertex, the old one."""
    if not len(links):
        return f, np.arange(nverts)
    p, q = links[:, 0], links[:, 1]
    eq = f[p][:, :, None] == f[q][:, None, :]
    lk, i, j = np.nonzero(eq)
    corner_p, corner_q = 3 * p[lk] + i, 3 * q[lk] + j
    _, label = _components(3 * len(f), np.stack([corner_p, corner_q], axis=1))
    # a corner is its fan's; a fan keeps the vertex it was cut from
    nf = label.reshape(-1, 3)
    origin = np.zeros(int(label.max()) + 1, np.int64)
    origin[label] = f.reshape(-1)
    return nf, origin


def _closed_volume(pts: np.ndarray, f: np.ndarray) -> tuple[bool, float]:
    """Whether the faces, by vertex index, close a consistently wound manifold, and the volume they enclose.

    Unlike a test by position this keeps bodies that only touch (at a vertex or an edge) apart."""
    n = int(f.max()) + 1
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    d, dc = np.unique(a * n + b, return_counts=True)
    closed = bool((dc == 1).all()) and bool(np.isin(b * n + a, d).all())
    p = pts[f]
    return closed, float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)


def _face_bodies(f: np.ndarray, nverts: int) -> tuple[int, np.ndarray]:
    """Connected sets of faces that share a vertex."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    a = np.concatenate([f[:, 0], f[:, 1]])
    b = np.concatenate([f[:, 1], f[:, 2]])
    count, vlabel = connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(nverts, nverts)), directed=False)
    return count, vlabel[f[:, 0]]


def _boundary_loops(f: np.ndarray) -> list[list[int]] | None:
    """Closed chains of the edges that have no partner; None when a vertex starts two of them (the loops are ambiguous)."""
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    n = int(f.max()) + 1
    fwd = a * n + b
    rev = b * n + a
    missing = ~np.isin(fwd, rev)
    nxt: dict[int, int] = {}
    for x, y in zip(a[missing].tolist(), b[missing].tolist()):
        if x in nxt:
            return None
        nxt[x] = y
    loops, seen = [], set()
    for s in sorted(nxt):
        if s in seen:
            continue
        loop, cur = [], s
        while cur not in seen:
            seen.add(cur)
            loop.append(cur)
            cur = nxt.get(cur, -1)
            if cur < 0:
                return None
        if cur != s:
            return None
        loops.append(loop)
    return loops


def _close_small_holes(pts: np.ndarray, f: np.ndarray, fraction: float) -> tuple[np.ndarray, np.ndarray, int]:
    loops = _boundary_loops(f)
    if not loops:
        return pts, f, 0
    area = 0.5 * np.linalg.norm(np.cross(pts[f[:, 1]] - pts[f[:, 0]], pts[f[:, 2]] - pts[f[:, 0]]), axis=1).sum()
    caps, cap_area = [], 0.0
    for lp in loops:
        if len(lp) < 3:
            return pts, f, 0
        p = pts[lp]
        c = p.mean(axis=0)
        cap_area += 0.5 * np.linalg.norm(np.cross(p - c, np.roll(p, -1, axis=0) - c), axis=1).sum()
        caps.append((lp, c))
    if cap_area > fraction * area:
        return pts, f, 0
    new_pts, new_f = [pts], [f]
    base = len(pts)
    for lp, c in caps:
        ci = base
        base += 1
        new_pts.append(c[None, :])
        new_f.append(np.array([(lp[(i + 1) % len(lp)], lp[i], ci) for i in range(len(lp))], np.int64))
    return np.vstack(new_pts), np.vstack(new_f), len(caps)


def repair_collider_mesh(points: np.ndarray, triangles: np.ndarray, cap_fraction: float = CAP_AREA_FRACTION) -> Repair:
    """The collider input as separate bodies, each closed where it can be made so."""
    pts0 = np.asarray(points, np.float64)
    cid = _canon(pts0)
    v = np.zeros((int(cid.max()) + 1, 3))
    v[cid] = pts0
    f = cid[np.asarray(triangles)]
    notes: dict[str, int] = {"welded": int(len(pts0) - len(v))}
    good = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    area2 = np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1)
    good &= area2 > 1e-12
    notes["degenerate"] = int((~good).sum())
    f = f[good]
    f, notes["duplicate"] = _drop_duplicates(f)
    if not len(f):
        return Repair([], notes)
    # edges: two faces link, more than two are paired by angle, one is a boundary
    ea = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    eb = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    fid = np.tile(np.arange(len(f)), 3)
    n = len(v)
    key = np.minimum(ea, eb) * n + np.maximum(ea, eb)
    order = np.argsort(key, kind="stable")
    sk = key[order]
    starts = np.flatnonzero(np.r_[True, sk[1:] != sk[:-1]])
    counts = np.diff(np.r_[starts, len(sk)])
    two = starts[counts == 2]
    links = [np.stack([fid[order[two]], fid[order[two + 1]]], axis=1)]
    many = starts[counts > 2]
    notes["nonmanifold_edges"] = int(len(many))
    if len(many):
        edge_faces = {}
        for s, c in zip(many.tolist(), counts[counts > 2].tolist()):
            e = order[s:s + c]
            edge_faces[(int(min(ea[e[0]], eb[e[0]])), int(max(ea[e[0]], eb[e[0]])))] = fid[e].tolist()
        links.append(np.array(_pair_at_nonmanifold_edges(f, v, edge_faces), np.int64).reshape(-1, 2))
    links = np.vstack(links)
    f, notes["flipped"] = _consistent_winding(f, links)
    used = len(np.unique(f))
    f, origin = _unfuse(f, links, len(v))
    v = v[origin]
    notes["split_vertices"] = int(len(v) - used) if len(links) else 0
    count, label = _face_bodies(f, len(v))
    bodies = []
    for body in range(count):
        bf = f[label == body]
        used = np.unique(bf)
        remap = np.full(len(v), -1, np.int64)
        remap[used] = np.arange(len(used))
        bp, bt = v[used], remap[bf]
        closed, vol = _closed_volume(bp, bt)
        capped = 0
        if not closed:
            bp, bt, capped = _close_small_holes(bp, bt, cap_fraction)
            if capped:
                notes["capped"] = notes.get("capped", 0) + capped
                closed, vol = _closed_volume(bp, bt)
        if closed and vol < 0:
            bt = bt[:, ::-1].copy()
            vol = -vol
        if closed and vol > 0:
            kind = "closed"
        else:
            has_boundary = not bool(np.isin(np.concatenate([bt[:, 1], bt[:, 2], bt[:, 0]]) * (int(bt.max()) + 1)
                                    + np.concatenate([bt[:, 0], bt[:, 1], bt[:, 2]]),
                                    np.concatenate([bt[:, 0], bt[:, 1], bt[:, 2]]) * (int(bt.max()) + 1)
                                    + np.concatenate([bt[:, 1], bt[:, 2], bt[:, 0]])).all())
            kind = "open" if has_boundary else "pinched"
        bodies.append(Body(bp, bt.astype(np.int32), kind, float(vol) if kind == "closed" else 0.0))
    notes["bodies"] = len(bodies)
    return Repair(bodies, notes)
