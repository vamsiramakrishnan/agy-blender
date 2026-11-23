"""Geometry of the free-form parts a build script writes (``tube``, ``loft``) and the part file they become.

Only what the kernel has no shape for lives here: the ready shapes (box, cylinder, ...) are the kernel's own
(``kernel.compile_ir.primitive_mesh``), never rebuilt. numpy only, so ``import agy3d`` stays cheap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

import numpy as np

MIN_STEP_M = 1e-4  # two consecutive path points closer than 0.1 mm are a typing mistake, not a shape
MAX_TURN_DEG = 150.0  # a tube that turns back further at one point "doubles back": its mitre ring would be 3.9x stretched
SMOOTH_STEPS = 8  # centripetal Catmull-Rom steps per span between two of the author's points (smooth=True)


class BuildError(ValueError):
    """A call of a build script that cannot be built as written; the message names the part and what was expected."""


def whole(where: str, name: str, v, minimum: int) -> int:
    """``v`` as an int: a whole number, 8.0 included (a script's arithmetic makes floats); anything else is refused."""
    ok = not isinstance(v, bool) and isinstance(v, (int, float, np.integer, np.floating)) and math.isfinite(v) and v == int(v)
    if not ok or int(v) < minimum:
        raise BuildError(f"{where}: {name} must be a whole number of at least {minimum}, got {v!r}")
    return int(v)


# --- the part file ------------------------------------------------------------------------------------------------


def ply_bytes(points: np.ndarray, tris: np.ndarray) -> bytes:
    """Binary little-endian PLY, float32 x y z per vertex and one uchar-counted int list per triangle: the bytes
    ``kernel.mesh_io.triangle_parts`` reads back unchanged (metres, Z-up, winding kept; tests/test_build_asset.py).
    Fixed header, no comment or timestamp, so the same mesh is the same bytes."""
    pts = np.ascontiguousarray(points, dtype="<f4")
    tri = np.ascontiguousarray(tris, dtype="<i4")
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {len(pts)}\nproperty float x\nproperty float y\nproperty float z\n"
              f"element face {len(tri)}\nproperty list uchar int vertex_indices\nend_header\n").encode("ascii")
    faces = np.empty(len(tri), dtype=[("n", "u1"), ("i", "<i4", (3,))])
    faces["n"], faces["i"] = 3, tri
    return header + pts.tobytes() + faces.tobytes()


def signed_volume(points: np.ndarray, tris: np.ndarray) -> float:
    p = np.asarray(points, np.float64)[np.asarray(tris)]
    return float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)


# --- tube ---------------------------------------------------------------------------------------------------------


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _turn(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The rotation matrix of the shortest turn taking unit vector ``a`` onto unit vector ``b`` (never antiparallel
    here: such a turn is refused as doubling back)."""
    v, c = np.cross(a, b), float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-12:
        return np.eye(3)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k / (1.0 + c)


def _about(axis: np.ndarray, angle: float) -> np.ndarray:
    x, y, z = axis
    k = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * k @ k


def catmull_rom(points: np.ndarray, closed: bool, steps: int = SMOOTH_STEPS) -> tuple[np.ndarray, np.ndarray]:
    """Centripetal (alpha 0.5) Catmull-Rom through ``points``: (curve points, the position of each along the author's
    points: span i, step k is i + k / steps, so an author's point j is at j). Open ends get a mirrored phantom point, so the curve starts and ends on the first and last point."""
    p = np.asarray(points, np.float64)
    n = len(p)
    ext = np.vstack([p[-1:], p, p[:2]]) if closed else np.vstack([2 * p[0] - p[1], p, 2 * p[-1] - p[-2]])
    spans = n if closed else n - 1
    out, span = [p[0]], [0]
    for i in range(spans):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        t0 = 0.0
        t1 = t0 + max(np.linalg.norm(p1 - p0), 1e-12) ** 0.5
        t2 = t1 + max(np.linalg.norm(p2 - p1), 1e-12) ** 0.5
        t3 = t2 + max(np.linalg.norm(p3 - p2), 1e-12) ** 0.5
        for k in range(1, steps + 1):
            t = t1 + (t2 - t1) * k / steps
            a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
            a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
            a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
            b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
            b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
            out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
            span.append(i + k / steps)
    c = np.array(out)
    if closed:  # the last step lands on the first point again: the loop joins it
        c, span = c[:-1], span[:-1]
    return c, np.array(span, np.float64)


@dataclass(frozen=True)
class TubeMesh:
    points: np.ndarray  # (N, 3) float64
    tris: np.ndarray  # (M, 3) int32, outward
    centre: np.ndarray  # the centre line as built: ring centres in order (closed: the first again at the end)


def _point_name(i: int, n: int) -> str:
    return f"point {i} {'(the first)' if i == 0 else '(the last)' if i == n - 1 else ''}".rstrip()


def tube(pid: str, path, radius, sides: int, closed: bool, smooth: bool) -> TubeMesh:
    """A rod of ``sides``-gon section along ``path``.

    Frames are rotation-minimising (parallel transport: across a corner the section turns only by the bend itself), so
    a rod does not twist. At a corner the ring lies in the plane bisecting the two legs and is the intersection of both
    legs' prisms with it (a true mitre: stretched along the bend by 1 / cos(turn / 2), so the section stays the same on
    both legs). ``radius``: a number, or values spread evenly along the length (linear in between, a ring at every
    value's position). ``closed``: the last point joins the first, no caps, the leftover twist of the transported frame
    spread evenly along the loop. ``smooth``: the path is first replaced by a centripetal Catmull-Rom curve through its
    points, ``SMOOTH_STEPS`` steps per span. Triangles: 2 * sides per band between two rings, plus 2 * (sides - 2) for
    the two flat caps of an open tube; a smooth open path of k points has 8 (k - 1) bands, a closed one 8 k."""
    where = f"tube {pid}"
    try:
        p = np.asarray(path, np.float64)
    except (TypeError, ValueError):
        raise BuildError(f"{where}: path must be a list of (x, y, z) points") from None
    if p.ndim != 2 or p.shape[1] != 3:
        raise BuildError(f"{where}: path must be a list of (x, y, z) points, got shape {list(p.shape)}")
    if not np.isfinite(p).all():
        raise BuildError(f"{where}: path has a point that is not a finite number")
    n = len(p)
    if n < (3 if closed else 2):
        raise BuildError(f"{where}: path needs at least {3 if closed else 2} points{' for closed=True' if closed else ''}, got {n}")
    sides = whole(where, "sides", sides, 3)
    radii = _radii(where, radius)
    if closed and len(radii) > 1 and radii[0] != radii[-1]:
        raise BuildError(f"{where}: a closed tube's radius list must start and end with the same value (the ends meet), "
                         f"got {radii[0]} and {radii[-1]}")
    steps = np.diff(np.vstack([p, p[:1]]) if closed else p, axis=0)
    for i, d in enumerate(np.linalg.norm(steps, axis=1)):
        if d < MIN_STEP_M:
            j = (i + 1) % n
            hint = " (closed=True joins the ends itself: do not repeat the first point)" if closed and j == 0 else ""
            raise BuildError(f"{where}: {_point_name(i, n)} and point {j} are {d * 1000:.3f} mm apart; consecutive "
                             f"points must be at least 0.1 mm apart{hint}")
    dirs = steps / np.linalg.norm(steps, axis=1)[:, None]
    m = len(dirs)
    for i in range(1 if not closed else 0, n if closed else n - 1):  # each corner of the author's path
        a, b = dirs[i - 1], dirs[i % m]
        turn = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(a, b))))))
        if turn > MAX_TURN_DEG:
            raise BuildError(f"{where}: the path doubles back at {_point_name(i, n)} (it turns by {turn:.0f} degrees, "
                             f"at most {MAX_TURN_DEG:.0f}); add a point to take the bend in two turns")
    if smooth:
        c, span = catmull_rom(p, closed)
    else:
        c, span = p, np.arange(n, dtype=np.float64)
    return _sweep(where, c, span, n, radii, sides, closed, smooth)


def _radii(where: str, radius) -> list[float]:
    vals = list(radius) if isinstance(radius, (list, tuple, np.ndarray)) else [radius]
    if not vals:
        raise BuildError(f"{where}: radius list is empty; give a number or a list of numbers")
    out = []
    for k, r in enumerate(vals):
        if isinstance(r, bool) or not isinstance(r, (int, float, np.floating, np.integer)) or not math.isfinite(r) or r <= 0:
            at = f" (value {k} of the list)" if len(vals) > 1 else ""
            raise BuildError(f"{where}: radius must be a positive number in metres{at}, got {r!r}")
        out.append(float(r))
    return out


def _sweep(where: str, c: np.ndarray, span: np.ndarray, n_author: int, radii: list[float], sides: int,
           closed: bool, smooth: bool) -> TubeMesh:
    k = len(c)
    seg = np.diff(np.vstack([c, c[:1]]) if closed else c, axis=0)
    seg_len = np.linalg.norm(seg, axis=1)
    if (seg_len < 1e-9).any():  # only a smoothed curve can get here (the author's points were checked)
        i = int(np.argmax(seg_len < 1e-9))
        raise BuildError(f"{where}: the smoothed path collapses near point {round(float(span[i])) % n_author}; move the points apart")
    d = seg / seg_len[:, None]
    nseg = len(d)
    arc = np.concatenate([[0.0], np.cumsum(seg_len)])  # arc length at each curve point (closed: and the loop's end)
    total = float(arc[-1])

    def r_at(s: float) -> float:
        if len(radii) == 1:
            return radii[0]
        return float(np.interp(s / total, np.linspace(0.0, 1.0, len(radii)), radii))

    # frames: F[j] = (u, v) of segment j, transported from segment j-1 by the shortest turn
    up = np.eye(3)[int(np.argmin(np.abs(d[0])))]
    u0 = _unit(np.cross(np.cross(d[0], up), d[0]))
    frames = [np.array([u0, np.cross(d[0], u0)])]
    for j in range(1, nseg):
        frames.append(frames[-1] @ _turn(d[j - 1], d[j]).T)
    twist = 0.0
    if closed:  # the frame carried round the loop comes back turned about d[0]; spread that turn along the loop
        back = frames[-1] @ _turn(d[-1], d[0]).T
        twist = math.atan2(float(np.dot(back[0], frames[0][1])), float(np.dot(back[0], frames[0][0])))

    # rings: (centre, leg direction, frame, plane normal, arc position, curve point index)
    rings: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float, int]] = []
    marks = [s * total for s in np.linspace(0.0, 1.0, len(radii))][1:-1] if len(radii) > 2 else []
    for i in range(k):
        if closed and i == 0:  # the loop's first corner, seen from its outgoing leg (the same ring, see the docstring)
            rings.append((c[0], d[0], frames[0], _unit(d[-1] + d[0]), 0.0, 0))
        elif closed or 0 < i < k - 1:  # a corner: the incoming leg's section slid onto the bisecting plane
            rings.append((c[i], d[i - 1], frames[i - 1], _unit(d[i - 1] + d[i]), float(arc[i]), i))
        else:  # an open end: a plain section
            j = 0 if i == 0 else nseg - 1
            rings.append((c[i], d[j], frames[j], d[j], float(arc[i]), i))
        if i < nseg:  # rings at the radius list's positions inside this segment
            for s in marks:
                if arc[i] + 1e-9 < s < arc[i + 1] - 1e-9:
                    rings.append((c[i] + d[i] * (s - arc[i]), d[i], frames[i], d[i], float(s), i))

    ang = np.arange(sides) * (2 * math.pi / sides)
    cs, sn = np.cos(ang), np.sin(ang)
    pts = []
    for centre, leg, frame, normal, s, _ in rings:
        u, v = frame
        if twist:
            rot = _about(leg, -twist * s / total)
            u, v = rot @ u, rot @ v
        sec = r_at(s) * (cs[:, None] * u + sn[:, None] * v)
        lam = -(sec @ normal) / float(np.dot(leg, normal))  # slide along the leg onto the ring's plane (the mitre)
        pts.append(centre + sec + lam[:, None] * leg)
    _check_folds(where, rings, pts, span, n_author, closed, smooth)

    nr = len(rings)
    idx = np.arange(nr * sides).reshape(nr, sides)
    tris = []
    for i in range(nr if closed else nr - 1):
        a, b = idx[i], idx[(i + 1) % nr]
        a1, b1 = np.roll(a, -1), np.roll(b, -1)
        tris += [np.column_stack([a, a1, b]), np.column_stack([a1, b1, b])]
    if not closed:
        fan = np.arange(1, sides - 1)
        tris.append(np.column_stack([np.zeros_like(fan), fan + 1, fan]))  # start cap faces back along the path
        last = idx[-1][0]
        tris.append(np.column_stack([np.full_like(fan, last), last + fan, last + fan + 1]))
    centre_line = np.vstack([c, c[:1]]) if closed else c
    return TubeMesh(np.vstack(pts), np.vstack(tris).astype(np.int32), centre_line)


def _check_folds(where: str, rings, pts, span: np.ndarray, n_author: int, closed: bool, smooth: bool) -> None:
    """A bend tighter than the rod is thick folds the surface inside out: the inner side of a corner ring would lie
    past the next ring along the leg between them. Checked on every generator line between consecutive rings; the
    refusal names the author's corner nearest the fold."""
    nr = len(rings)
    for i in range(nr if closed else nr - 1):
        j = (i + 1) % nr
        leg = rings[j][0] - rings[i][0]
        if ((pts[j] - pts[i]) @ leg <= 0).any():
            a = float(span[rings[i][5]])
            b = float(span[rings[j][5]]) if j else float(n_author if closed else 0)
            corner = round((a + b) / 2)
            corner = corner % n_author if closed else min(max(corner, 1), n_author - 2) if n_author > 2 else 0
            fix = "put the points around it further apart or lower the radius" + ("" if smooth else ", or use smooth=True")
            raise BuildError(f"{where}: the bend at point {corner} is too tight for this radius: the rod's inside folds "
                             f"over itself; {fix}")


def along(centre: np.ndarray, t: float) -> np.ndarray:
    """The point at fraction ``t`` of the length of the polyline ``centre``."""
    seg = np.linalg.norm(np.diff(centre, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    s = t * arc[-1]
    i = min(int(np.searchsorted(arc, s, side="right")) - 1, len(seg) - 1)
    i = max(i, 0)
    f = 0.0 if seg[i] == 0 else (s - arc[i]) / seg[i]
    return centre[i] + f * (centre[i + 1] - centre[i])


# --- loft ---------------------------------------------------------------------------------------------------------


FLAT_TOL = 0.02  # a capped end section may stand off its best-fit plane by 2 % of its size (a cap is a flat polygon)


def _is_point(p) -> bool:
    return isinstance(p, (list, tuple, np.ndarray)) and len(p) == 3 and all(
        isinstance(c, (int, float, np.integer, np.floating)) and not isinstance(c, bool) for c in p)


def _newell(a: np.ndarray) -> np.ndarray:
    """Twice the vector area of the closed polygon ``a`` (its normal by the right-hand rule, length 2 * area); defined
    for a polygon that is not flat too."""
    b = np.roll(a, -1, axis=0)
    return np.array([np.sum((a[:, 1] - b[:, 1]) * (a[:, 2] + b[:, 2])), np.sum((a[:, 2] - b[:, 2]) * (a[:, 0] + b[:, 0])),
                     np.sum((a[:, 0] - b[:, 0]) * (a[:, 1] + b[:, 1]))])


def _cap(where: str, i: int, a: np.ndarray, normal: np.ndarray) -> np.ndarray:
    """Triangles of section ``i`` as the flat polygon it is, wound counter-clockwise about ``normal``: projected onto
    its best-fit plane and triangulated by the kernel's own polygon code (``kernel.shapes.extrude_outline``, which
    ``extrude`` caps with), so a concave section (a U, an L) gets the cap of its own shape."""
    from antigravity3d.kernel import shapes as kshapes

    n = _unit(normal)
    centre = a.mean(axis=0)
    size = float(np.linalg.norm(a - centre, axis=1).max())
    off = float(np.abs((a - centre) @ n).max())
    if off > FLAT_TOL * size:
        raise BuildError(f"{where}: section {i} is an end, capped by a flat face, but a point of it lies {off * 1000:.1f} mm "
                         f"off its plane; make the end section flat (or end on a tip, a section of one point)")
    u = _unit(np.cross(n, np.eye(3)[int(np.argmin(np.abs(n)))]))
    v = np.cross(n, u)
    xy = [(float(p @ u), float(p @ v)) for p in a - centre]
    try:
        ccw, tris = kshapes.extrude_outline(xy, "section")
    except ValueError as e:
        why = str(e).split(": ", 1)[-1].replace("outline", f"section {i}")
        raise BuildError(f"{where}: {why} (seen along its own normal); a capped end section must be a simple polygon") from None
    if not np.allclose(ccw[0], xy[0]):  # the kernel reversed a clockwise outline: its indices count from the end
        raise BuildError(f"{where}: section {i} winds the other way from the skin; list every section in the same direction")
    return np.asarray(tris, np.int64)


def loft(pid: str, sections) -> tuple[np.ndarray, np.ndarray, bool]:
    """(points, outward triangles, flipped) of a skin through ``sections``: consecutive sections joined point to
    point (the author keeps the order consistent; nothing is re-indexed), a one-point section is a tip (only first or
    last), first and last capped when they are not tips, each cap the flat polygon its section is (concave allowed,
    triangulated by the kernel's polygon code). Refused at the call: sections of different counts, a section whose
    points lie on one line, a section that runs the other way round from its neighbour (Newell normals pointing
    apart), a capped end that is not flat or crosses itself, and sections that enclose no volume.
    ``flipped``: every section runs clockwise seen along the skin, so it came out inside out and was turned whole."""
    where = f"loft {pid}"
    if isinstance(sections, (list, tuple)) and sections and all(_is_point(p) for p in sections) and len(sections) >= 3:
        raise BuildError(f"{where}: sections is a list of sections, each a list of (x, y, z) points; this is one list of "
                         f"points (write [section0, section1, ...])")
    if not isinstance(sections, (list, tuple)) or len(sections) < 2:
        raise BuildError(f"{where}: sections must be a list of at least two sections, each a list of (x, y, z) points")
    rings: list[np.ndarray] = []
    count = None
    first = None
    for i, sec in enumerate(sections):
        try:
            a = np.asarray(sec, np.float64)
        except (TypeError, ValueError):
            raise BuildError(f"{where}: section {i} must be a list of (x, y, z) points") from None
        if a.ndim == 1 and a.shape == (3,):
            a = a[None]
        if a.ndim != 2 or a.shape[1] != 3 or not np.isfinite(a).all():
            raise BuildError(f"{where}: section {i} must be a list of finite (x, y, z) points")
        if len(a) == 2:
            raise BuildError(f"{where}: section {i} has 2 points; a section is a ring of at least 3 points, or 1 (a tip)")
        if len(a) == 1:
            if 0 < i < len(sections) - 1:
                raise BuildError(f"{where}: section {i} is a tip (one point); only the first or the last section can be a tip")
        elif count is None:
            count, first = len(a), i
        elif len(a) != count:
            raise BuildError(f"{where}: section {i} has {len(a)} points but section {first} has {count}; every "
                             "section that is not a tip needs the same count")
        rings.append(a)
    if count is None:
        raise BuildError(f"{where}: every section is a tip; at least one needs 3 or more points")
    span = max(float(np.ptp(np.vstack(rings), axis=0).max()), 1e-12)
    normals: dict[int, np.ndarray] = {}
    for i, a in enumerate(rings):
        if len(a) == 1:
            continue
        nv = _newell(a)
        if np.linalg.svd(a - a.mean(axis=0), compute_uv=False)[1] <= 1e-9 * span:
            raise BuildError(f"{where}: section {i}'s points lie on one line (it encloses no area); spread them into a ring")
        if np.linalg.norm(nv) <= 1e-9 * span * span:
            raise BuildError(f"{where}: section {i} crosses itself (its parts wind opposite ways and enclose no area); list "
                             "its points in order around it")
        normals[i] = nv
    # every ring must run the same way round as its neighbour: a reversed one twists the skin into a bow tie of zero
    # volume. Neighbours, not the direction of travel: a skin may turn back on itself (the reference page's hollow
    # tray goes up the outside and down the inside, all four rings counter-clockwise seen from above).
    keys = sorted(normals)
    signs = {keys[0]: 1.0}
    for i, j in pairwise(keys):
        signs[j] = signs[i] * (1.0 if float(normals[i] @ normals[j]) >= 0 else -1.0)
    if len(set(signs.values())) > 1:
        total = sum(signs.values())
        major = 1.0 if total >= 0 else -1.0
        odd = min(i for i, sg in signs.items() if sg != major)
        raise BuildError(f"{where}: section {odd} runs the other way round from section {keys[keys.index(odd) - 1] if odd != keys[0] else keys[1]}; "
                         "list every section's points in the same direction")
    pts, idx = [], []
    off = 0
    for a in rings:
        pts.append(a)
        idx.append(np.arange(off, off + len(a)))
        off += len(a)
    tris: list[np.ndarray] = []
    for a, b in pairwise(idx):
        if len(a) == 1:
            tris.append(np.column_stack([np.full(count, a[0]), np.roll(b, -1), b]))
        elif len(b) == 1:
            tris.append(np.column_stack([a, np.roll(a, -1), np.full(count, b[0])]))
        else:
            a1, b1 = np.roll(a, -1), np.roll(b, -1)
            tris += [np.column_stack([a, a1, b]), np.column_stack([a1, b1, b])]
    for end in (0, len(rings) - 1):
        if len(rings[end]) == 1:
            continue
        cap = idx[end][_cap(where, end, rings[end], normals[end])]  # counter-clockwise about the section's normal
        tris.append(cap[:, ::-1] if end == 0 else cap)  # the first faces back, the last forward, as the sides run
    p = np.vstack(pts)
    t = np.vstack(tris).astype(np.int32)
    vol = signed_volume(p, t)
    if abs(vol) <= 1e-9 * span ** 3:
        raise BuildError(f"{where}: the sections enclose no volume (they lie in one plane); move them apart along the skin")
    flipped = vol < 0
    if flipped:
        t = t[:, ::-1].copy()
    return p, t, flipped


# --- small helpers shared with asset.py ---------------------------------------------------------------------------


def ply_header_size(data: bytes) -> int:
    return data.index(b"end_header\n") + len(b"end_header\n")


def unpack_ply(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of :func:`ply_bytes` (for the builder's own files only)."""
    head = data[: ply_header_size(data)].decode("ascii").split("\n")
    nv = int(next(x for x in head if x.startswith("element vertex")).split()[-1])
    nf = int(next(x for x in head if x.startswith("element face")).split()[-1])
    body = data[ply_header_size(data):]
    pts = np.frombuffer(body[: nv * 12], "<f4").reshape(nv, 3)
    faces = np.frombuffer(body[nv * 12:], dtype=[("n", "u1"), ("i", "<i4", (3,))], count=nf)
    return pts.astype(np.float64), faces["i"].astype(np.int32)

