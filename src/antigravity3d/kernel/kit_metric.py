"""One metric per modular kit, the check that every piece obeys it, and the snap test that proves the pieces assemble.

A modular kit only works if its pieces share numbers: the grid they snap to, the module length of a wall run, one wall
height, one wall thickness, one door opening and one pivot convention. ``KitMetric`` holds those numbers. The kit
pipeline (``dags/environment_kit.py``) decides it once per kit and hands it to every author in the piece's ``context``
(``context["kit_metric"]``): the offline templates build to it, a live model is told it (:meth:`KitMetric.brief`).

Rules, and where they come from (both upstream skill packs were read, nothing is imported from them):

* EL = EL4CTEO/blender-skills ``blender-environment-kits`` (``SKILL.md``, ``scripts/kit_check.py``). ``check_piece`` and
  ``kit_report`` re-implement ``kit_check.run`` on this project's meshes, one piece = one object: footprint lengths of
  at least one grid cell are multiples of the grid (thinner dimensions are exempt), height on the grid, the origin at the
  pivot convention (``corner`` = bounding-box min, the default; ``bottom-center``; ``center``), open edges only on outer
  faces, texel density within 25 % of the kit median.
* AR = arjun988/blender-skills ``environment-artist`` (``SKILL.md``, ``references/modular-kits.md``): 2 m or 4 m modules
  on a 0.5 m sub-grid inside, 4 m on a 1 m sub-grid outside; origin at the grid corner at floor level; the kit is
  validated by assembling a closed room, an L corridor and a T junction with no gap above 1 mm.

Added here, because the snap test needs them and neither upstream says them: the pieces that must share a height (wall,
doorway, pillar) are held to the metric's wall height exactly, not only "on the grid"; and no face of a part may be
larger than the shared trim sheet's strip on its shorter side (``max_face_m``), because the trim mapper shrinks such a
face to fit and its texel density drops with it (audit row K6).

Module volume and relief. A wall-type piece is a module volume (length x thickness x height) plus optional relief
(trims, mouldings) standing proud of its +Y face by at most ``relief_m``. Only MATING faces must be flat and full: the
two ends (x), which meet the next piece of a run, and the -Y face, which is the face a perpendicular wall butts against
in every corner and T junction these four piece types can make (worked through in ``layouts``: a relief on that face
would leave a slit at the joint, or interpenetrate the butting wall). The +Y face is never mated, so relief there is
free; AR calls +Y the forward face. The pivot is defined on the module volume. With relief on the +Y side only, the
bounding-box minimum IS the module corner, so EL's corner test (bounding-box min) still passes; EL's bottom-center and
center tests measure the bounding box and would report the relief's half-depth as a pivot offset.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Sequence

import numpy as np
from scipy.spatial import ConvexHull, QhullError

from antigravity3d.kernel.types import MeshData

PIVOTS = ("corner", "bottom-center", "center")
KIT_TEMPLATES = ("wall_segment", "floor_tile", "pillar", "doorway")
WALL_LIKE = ("wall_segment", "doorway")
SHARED_HEIGHT = ("wall_segment", "doorway", "pillar")  # pieces that stand side by side under one ceiling line
DIM_TOL_M = 0.001        # EL kit_check --tol default; AR's 1 mm gap tolerance
GAP_TOL_M = 0.001        # AR: "no gaps at 0.001 m"
OVERLAP_TOL_M = 1e-5     # interpenetration deeper than this fails (float32 points carry ~1e-7 m of noise)
FULL_FACE = 0.999        # a face that must be full may miss at most 0.1 % of its area
TEXEL_DEV_MAX = 0.25     # EL: texel density within 25 % of the kit median (here also: of the strip's nominal density)
PLANE_TOL_M = 1e-5
DOOR_MIN_M = (1.0, 2.2)  # EL environment-kits SKILL.md l.37: doors >= 1 x 2.2 m (AR's 0.9 x 2.1 is below it)


def _on_grid(v: float, grid: float, tol: float = DIM_TOL_M) -> bool:
    """EL kit_check._on_grid."""
    return abs(v / grid - round(v / grid)) * grid <= tol


def _g(v: float) -> str:
    return f"{round(float(v), 4) + 0.0:g}"  # + 0.0: no "-0"


@dataclass(frozen=True)
class KitMetric:
    """The numbers every piece of one kit shares, in metres, Z-up. Walls run along +X, their thickness along +Y.

    ``max_face_m`` is not a design choice: it is the shorter side the shared trim sheet's narrowest used strip can hold
    at full density, set by :meth:`fitted_to` from the kit's sheet.
    """

    name: str = "interior"
    grid_m: float = 0.5
    module_m: float = 2.0
    wall_height_m: float = 3.0
    wall_thickness_m: float = 0.25
    floor_thickness_m: float = 0.1
    door_width_m: float = 1.0
    door_height_m: float = 2.5
    pillar_m: float = 0.5
    pivot: str = "corner"
    relief_m: float = 0.04
    max_face_m: float = 0.75

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise ValueError(f"kit metric {self.name!r}: " + "; ".join(problems))

    def problems(self) -> list[str]:
        out = []
        for key in ("grid_m", "module_m", "wall_height_m", "wall_thickness_m", "floor_thickness_m", "door_width_m",
                    "door_height_m", "pillar_m", "max_face_m"):
            v = getattr(self, key)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                return [f"{key} must be a positive number, got {v!r}"]
        g = self.grid_m
        if not (isinstance(self.relief_m, (int, float)) and 0 <= self.relief_m < self.wall_thickness_m):
            out.append(f"relief_m must be >= 0 and less than the wall thickness, got {self.relief_m!r}")
        elif self.wall_thickness_m < g and self.wall_thickness_m + self.relief_m >= g - DIM_TOL_M:
            out.append(f"wall_thickness_m + relief_m = {_g(self.wall_thickness_m + self.relief_m)} m reaches the {_g(g)} m grid "
                       f"(a piece's depth must stay thinner than one cell to be exempt from the footprint rule)")
        if self.pivot not in PIVOTS:
            out.append(f"pivot must be one of {PIVOTS}, got {self.pivot!r}")
        # EL: footprint lengths of at least one cell must tile; a dimension thinner than a cell is a thickness
        for key in ("module_m", "pillar_m", "wall_thickness_m"):
            v = getattr(self, key)
            if v >= g - DIM_TOL_M and not _on_grid(v, g):
                out.append(f"{key} {_g(v)} is not a multiple of the {_g(g)} m grid")
        # EL warns on an off-grid height ("fine for props, not for walls"); walls are what this height is for
        if not _on_grid(self.wall_height_m, g):
            out.append(f"wall_height_m {_g(self.wall_height_m)} is not on the {_g(g)} m grid")
        jamb = (self.module_m - self.door_width_m) / 2
        if jamb < min(g, self.module_m / 8) - DIM_TOL_M:
            out.append(f"door_width_m {_g(self.door_width_m)} leaves jambs of {_g(jamb)} m in a {_g(self.module_m)} m module")
        if not _on_grid(jamb, g) and jamb >= g:
            out.append(f"door jambs of {_g(jamb)} m are off the {_g(g)} m grid")
        if self.door_height_m > self.wall_height_m - 0.1 + 1e-9:
            out.append(f"door_height_m {_g(self.door_height_m)} leaves no lintel under a {_g(self.wall_height_m)} m wall")
        if self.door_width_m < DOOR_MIN_M[0] - 1e-9 or self.door_height_m < DOOR_MIN_M[1] - 1e-9:
            out.append(f"door opening {_g(self.door_width_m)} x {_g(self.door_height_m)} m is below {DOOR_MIN_M[0]:g} x {DOOR_MIN_M[1]:g} m")
        if self.pillar_m > self.module_m + 1e-9:
            out.append(f"pillar_m {_g(self.pillar_m)} is larger than the module")
        return out

    # ------------------------------------------------------------------ presets and parsing

    @classmethod
    def interior(cls, **kw: Any) -> "KitMetric":
        """AR interior: 2 m modules on a 0.5 m sub-grid; walls 3 m (EL 3-4 m, AR ceilings 2.4-3.5 m); door 1 x 2.5 m."""
        return cls(**{"name": "interior", **kw})

    @classmethod
    def exterior(cls, **kw: Any) -> "KitMetric":
        """AR exterior facades: 4 m modules on a 1 m sub-grid; 4 m storeys (EL's upper interior bound); door 2 x 3 m."""
        return cls(**{"name": "exterior", "grid_m": 1.0, "module_m": 4.0, "wall_height_m": 4.0, "wall_thickness_m": 0.5,
                      "floor_thickness_m": 0.2, "door_width_m": 2.0, "door_height_m": 3.0, "pillar_m": 1.0, "relief_m": 0.08, **kw})

    PRESETS = ("interior", "exterior")
    KEYS = {"grid": "grid_m", "module": "module_m", "wall_height": "wall_height_m", "wall_thickness": "wall_thickness_m",
            "floor_thickness": "floor_thickness_m", "door_width": "door_width_m", "door_height": "door_height_m",
            "pillar": "pillar_m", "relief": "relief_m", "pivot": "pivot"}

    @classmethod
    def parse(cls, spec: str | None) -> "KitMetric":
        """``interior`` (default), ``exterior``, or a preset followed by overrides: ``interior,module=4,wall_height=3.5``."""
        parts = [p.strip() for p in (spec or "interior").split(",") if p.strip()]
        base, overrides = "interior", {}
        for k, part in enumerate(parts):
            if "=" not in part:
                if k or part not in cls.PRESETS:
                    raise ValueError(f"unknown kit metric preset {part!r} (presets: {', '.join(cls.PRESETS)})")
                base = part
                continue
            key, value = (s.strip() for s in part.split("=", 1))
            if key not in cls.KEYS:
                raise ValueError(f"unknown kit metric key {key!r} (keys: {', '.join(cls.KEYS)})")
            overrides[cls.KEYS[key]] = value if key == "pivot" else float(value)
        return getattr(cls, base)(**overrides)

    def fitted_to(self, strip_heights_m: Iterable[float]) -> "KitMetric":
        """This metric with ``max_face_m`` = the smallest usable strip height, rounded down to 5 cm."""
        usable = min(strip_heights_m)
        return replace(self, max_face_m=max(0.05, math.floor(usable * 20 + 1e-9) / 20))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------ what each piece must be

    def dims(self, template: str) -> tuple[float, float, float]:
        """The module volume of a piece of ``template`` (its bounding box, except a wall-type piece's +Y relief)."""
        L, D, H = self.module_m, self.wall_thickness_m, self.wall_height_m
        if template in WALL_LIKE:
            return (L, D, H)
        if template == "floor_tile":
            return (L, L, self.floor_thickness_m)
        if template == "pillar":
            return (self.pillar_m, self.pillar_m, H)
        raise KeyError(f"{template!r} is not a kit piece type ({', '.join(KIT_TEMPLATES)})")

    def origin(self, dims: Sequence[float]) -> np.ndarray:
        """Where the bounding-box minimum sits relative to the piece origin under this pivot convention (EL _pivot_target)."""
        d = np.asarray(dims, float)
        if self.pivot == "corner":
            return np.zeros(3)
        if self.pivot == "bottom-center":
            return np.array([-d[0] / 2, -d[1] / 2, 0.0])
        return -d / 2

    def envelope(self, template: str) -> tuple[np.ndarray, np.ndarray]:
        d = np.asarray(self.dims(template), float)
        lo = self.origin(d)
        return lo, lo + d

    def opening(self) -> tuple[np.ndarray, np.ndarray]:
        """The doorway's clear opening (through the whole thickness), in piece coordinates."""
        lo, hi = self.envelope("doorway")
        j = (self.module_m - self.door_width_m) / 2
        return lo + np.array([j, 0, 0]), np.array([hi[0] - j, hi[1], lo[2] + self.door_height_m])

    def brief(self, template: str) -> str:
        """What a model must build for one piece: numbers it can copy, in the order it needs them (it is sent with every
        call, so it is as short as exactness allows)."""
        lo, hi = self.envelope(template)
        box = ", ".join(f"{a} {_g(lo[i])}..{_g(hi[i])}" for i, a in enumerate("xyz"))
        where = {"corner": "min corner", "bottom-center": "bottom-face centre", "center": "centre"}[self.pivot]
        text = f"Kit metric, exact, metres: box {box}; origin at its {where}."
        if template in WALL_LIKE:
            text += f" Faces x={_g(lo[0])}, x={_g(hi[0])}, y={_g(lo[1])} flat and full."
            if template == "doorway":
                olo, ohi = self.opening()
                text += f" Opening x {_g(olo[0])}..{_g(ohi[0])}, z {_g(olo[2])}..{_g(ohi[2])} empty through y."
            if self.relief_m > 0:
                text += f" Relief only on +Y, out to y {_g(hi[1] + self.relief_m)}."
        elif template == "floor_tile":
            text += f" Top z={_g(hi[2])} and sides flat and full."
        return text + f" Part faces at most {_g(self.max_face_m)} m on the shorter side. Parts touch, never overlap."


# ---------------------------------------------------------------------------------------------- geometry helpers


def _tri_normals(p: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
    a = np.linalg.norm(n, axis=1)
    return n / np.maximum(a, 1e-30)[:, None], a / 2


def plane_faces(meshes: Iterable[MeshData], axis: int, value: float, sign: int) -> np.ndarray:
    """(K, 3, 2) triangles lying in the plane ``x[axis] = value`` whose outward normal is ``sign`` along ``axis``,
    projected onto the other two axes. Faces of one closed body never overlap there, nor do faces of disjoint parts."""
    keep = [i for i in range(3) if i != axis]
    out = []
    for m in meshes:
        p, t = np.asarray(m.points, float), np.asarray(m.triangles)
        if not len(t):
            continue
        n, a = _tri_normals(p, t)
        on = (np.abs(p[t][:, :, axis] - value) <= PLANE_TOL_M).all(axis=1)
        sel = on & (n[:, axis] * sign > 1 - 1e-6) & (a > 1e-14)
        if sel.any():
            out.append(p[t[sel]][:, :, keep])
    return np.concatenate(out) if out else np.zeros((0, 3, 2))


def _area2(poly: np.ndarray) -> float:
    if len(poly) < 3:
        return 0.0
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _ccw(tri: np.ndarray) -> np.ndarray:
    return tri if _area2(tri) >= 0 else tri[::-1]


def _clip(subject: np.ndarray, clipper: np.ndarray) -> np.ndarray:
    """Sutherland-Hodgman: ``subject`` (convex polygon) clipped by convex CCW ``clipper``."""
    out = subject
    for i in range(len(clipper)):
        if not len(out):
            break
        a, b = clipper[i], clipper[(i + 1) % len(clipper)]
        e = b - a
        side = e[0] * (out[:, 1] - a[1]) - e[1] * (out[:, 0] - a[0])
        nxt = []
        for k in range(len(out)):
            p, q, sp, sq = out[k], out[(k + 1) % len(out)], side[k], side[(k + 1) % len(out)]
            if sp >= 0:
                nxt.append(p)
            if (sp >= 0) != (sq >= 0):
                nxt.append(p + (q - p) * (sp / (sp - sq)))
        out = np.asarray(nxt) if nxt else np.zeros((0, 2))
    return out


def faces_area(tris: np.ndarray) -> float:
    return float(sum(abs(_area2(t)) for t in tris))


def overlap_area(a: np.ndarray, b: np.ndarray) -> float:
    """Exact area shared by two sets of non-overlapping triangles (per set) in one plane."""
    if not len(a) or not len(b):
        return 0.0
    alo, ahi, blo, bhi = a.min(axis=1), a.max(axis=1), b.min(axis=1), b.max(axis=1)
    total = 0.0
    for i in range(len(a)):
        near = np.flatnonzero(((blo < ahi[i]) & (bhi > alo[i])).all(axis=1))
        ta = _ccw(a[i])
        for j in near:
            total += abs(_area2(_clip(ta, _ccw(b[j]))))
    return total


@dataclass(frozen=True)
class _Convex:
    """A part as a convex polytope for exact separating-axis tests (the hull of a non-convex part: conservative)."""

    pts: np.ndarray
    normals: np.ndarray
    edges: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    name: str = ""

    @classmethod
    def of(cls, pts: np.ndarray, name: str = "") -> "_Convex":
        pts = np.asarray(pts, float)
        try:
            h = ConvexHull(pts)
            v = pts[h.vertices]
            normals = np.unique(np.round(h.equations[:, :3], 9), axis=0)
            e = pts[h.simplices[:, [1, 2, 0]]] - pts[h.simplices]
            e = e.reshape(-1, 3)
        except (QhullError, ValueError):  # flat or degenerate: fall back to its box
            v = pts
            normals, e = np.eye(3), np.eye(3)
        e = e / np.maximum(np.linalg.norm(e, axis=1), 1e-30)[:, None]
        e = np.where((e[:, :1] < 0) | ((e[:, :1] == 0) & (e[:, 1:2] < 0)), -e, e)
        return cls(v, normals, np.unique(np.round(e, 9), axis=0), pts.min(0), pts.max(0), name)


def penetration(a: _Convex, b: _Convex) -> float:
    """Exact penetration depth of two convex polytopes (SAT over face normals and edge cross products); <= 0 when
    they are apart or only touch."""
    if ((np.minimum(a.hi, b.hi) - np.maximum(a.lo, b.lo)) <= OVERLAP_TOL_M).any():
        return float((np.minimum(a.hi, b.hi) - np.maximum(a.lo, b.lo)).min())
    cross = np.cross(a.edges[:, None, :], b.edges[None, :, :]).reshape(-1, 3)
    norm = np.linalg.norm(cross, axis=1)
    axes = np.concatenate([a.normals, b.normals, cross[norm > 1e-9] / norm[norm > 1e-9, None]])
    pa, pb = a.pts @ axes.T, b.pts @ axes.T
    ov = np.minimum(pa.max(0), pb.max(0)) - np.maximum(pa.min(0), pb.min(0))
    return float(ov.min())


def parts_of(meshes: Iterable[MeshData]) -> list[_Convex]:
    return [_Convex.of(m.points, m.node_id) for m in meshes if len(m.points) >= 4]


# ---------------------------------------------------------------------------------------------- the per-piece check


@dataclass
class PieceCheck:
    """The result of checking one piece against the metric. ``errors`` fail the piece; ``warnings`` do not."""

    piece: str
    template: str
    dims_m: list[float]
    expected_dims_m: list[float]
    pivot_offset_m: list[float]           # bounding-box minimum minus where the pivot convention puts it
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    faces: dict[str, float] = field(default_factory=dict)  # coverage of each face that must be full
    texel: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def check_piece(name: str, template: str, meshes: Sequence[MeshData], metric: KitMetric, trims: Sequence[Any] | None = None) -> PieceCheck:
    """Whether one piece obeys the metric, with every failing number named.

    Footprint and height on the grid and the pivot are EL kit_check's rules; exact dimensions (so the pieces that must
    share a height do), full faces, a clear door opening and trim-fitting faces are this kit's (module docstring)."""
    pts = np.vstack([np.asarray(m.points, float) for m in meshes])
    lo, hi = pts.min(0), pts.max(0)
    dims = hi - lo
    want = np.asarray(metric.dims(template), float)
    target = metric.origin(want)  # the pivot is defined on the module volume (relief does not move it)
    relief = metric.relief_m if template in WALL_LIKE else 0.0
    pc = PieceCheck(name, template, [round(float(v), 4) for v in dims], [round(float(v), 4) for v in want],
                    [round(float(v), 4) for v in lo - target])
    g = metric.grid_m
    # EL kit_check: footprint lengths >= one cell must be grid multiples; height on the grid is a warning
    bad = [_g(float(d)) for d in dims[:2] if d >= g - DIM_TOL_M and not _on_grid(float(d), g)]
    if bad:
        pc.errors.append(f"footprint {_g(dims[0])} x {_g(dims[1])} m is not a multiple of the {_g(g)} m grid")
    if not _on_grid(float(dims[2]), g):
        pc.warnings.append(f"height {_g(dims[2])} m is off the {_g(g)} m grid")
    # the metric: exact size (one shared height for walls, doorways and pillars; one module; one thickness)
    for i, axis in enumerate(("length x", "depth y", "height z")):
        if i == 1 and relief > 0:
            if not (want[1] - DIM_TOL_M <= dims[1] <= want[1] + relief + DIM_TOL_M):
                pc.errors.append(f"depth y {_g(dims[1])} m, kit {_g(want[1])} m plus at most {_g(relief)} m of relief on +Y")
        elif abs(dims[i] - want[i]) > DIM_TOL_M:
            pc.errors.append(f"{axis} {_g(dims[i])} m, kit {_g(want[i])} m")
    if np.linalg.norm(lo - target) > DIM_TOL_M:
        pc.errors.append(f"pivot is not at the {metric.pivot}: bounding-box min at ({', '.join(_g(v) for v in lo)}), "
                         f"expected ({', '.join(_g(v) for v in target)})")
    # MATING faces must be flat and full over the module volume's face, from the actual geometry; the +Y face of a
    # wall-type piece is not mated and may carry relief
    mlo, mhi = lo, lo + want  # the module volume where this piece actually is
    need: list[tuple[str, int, float, int, float]] = []  # label, axis, plane, outward sign, required coverage
    if template in WALL_LIKE:
        side = FULL_FACE
        if template == "doorway":
            side = FULL_FACE - (metric.door_width_m * metric.door_height_m) / (want[0] * want[2])
        need += [("end x-", 0, mlo[0], -1, FULL_FACE), ("end x+", 0, mhi[0], 1, FULL_FACE), ("face y-", 1, mlo[1], -1, side)]
    elif template == "floor_tile":
        need += [("top", 2, mhi[2], 1, FULL_FACE), ("side x-", 0, mlo[0], -1, FULL_FACE), ("side x+", 0, mhi[0], 1, FULL_FACE),
                 ("side y-", 1, mlo[1], -1, FULL_FACE), ("side y+", 1, mhi[1], 1, FULL_FACE)]
    for label, axis, plane, sign, required in need:
        others = [i for i in range(3) if i != axis]
        a, b = mlo[others], mhi[others]
        rect = np.array([[[a[0], a[1]], [b[0], a[1]], [b[0], b[1]]], [[a[0], a[1]], [b[0], b[1]], [a[0], b[1]]]])
        full = float(np.prod(want[others]))
        cov = overlap_area(plane_faces(meshes, axis, float(plane), sign), rect) / full if full > 0 else 0.0
        pc.faces[label] = round(cov, 4)
        if cov < required:
            pc.errors.append(f"{label} face covers {cov:.1%} of its {_g(want[others[0]])} x {_g(want[others[1]])} m "
                             f"module face (needs {required:.1%}: a neighbour would meet a slit or a relief)")
    if template == "doorway":
        olo, ohi = metric.opening()
        olo, ohi = olo - target + lo, ohi - target + lo  # the opening where this piece actually is
        hole = _Convex.of(np.array([[x, y, z] for x in (olo[0], ohi[0]) for y in (olo[1], ohi[1]) for z in (olo[2], ohi[2])]), "opening")
        for part in parts_of(meshes):
            d = penetration(part, hole)
            if d > DIM_TOL_M:
                pc.errors.append(f"part {part.name} reaches {_g(d)} m into the {_g(metric.door_width_m)} x {_g(metric.door_height_m)} m opening")
    if trims is not None:
        pc.texel = texel_of(trims)
        for node, chart, dev, k in pc.texel["shrunk"]:
            pc.errors.append(f"part {node} face (chart {chart}) is shrunk to {k:.0%} to fit its trim strip: texel density "
                             f"{dev:+.0%} of the sheet's (limit {TEXEL_DEV_MAX:.0%}); keep part faces under {_g(metric.max_face_m)} m on the shorter side")
    return pc


def texel_of(trims: Sequence[Any]) -> dict[str, Any]:
    """Texel density of one piece as EL kit_check measures an object (sqrt(UV area / 3D area) x texture, all its
    triangles), at the sheet's resolution, plus the charts the trim mapper shrank by more than ``TEXEL_DEV_MAX``."""
    a2 = a3 = 0.0
    shrunk, worst = [], 0.0
    res = None
    for r in trims:
        p, t, uv = np.asarray(r.points, float), np.asarray(r.triangles), np.asarray(r.uvs, float)
        a3 += float(np.linalg.norm(np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]]), axis=1).sum() / 2)
        q = uv[t]
        a2 += float(np.abs((q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1]) - (q[:, 1, 1] - q[:, 0, 1]) * (q[:, 2, 0] - q[:, 0, 0])).sum() / 2)
        res = math.sqrt(r.size[0] * r.size[1])  # a uv area of 1 is width x height texels (a trim sheet is square: its side)
        if r.report is not None:
            for c in r.report.charts:
                if c.strip:
                    worst = max(worst, abs(c.density_dev))
                    if abs(c.density_dev) > TEXEL_DEV_MAX:
                        shrunk.append((r.node_id, c.index, c.density_dev, c.scale))
    td = math.sqrt(a2 / a3) * res if a3 > 0 and a2 > 0 and res else None
    return {"px_per_m": round(td, 1) if td else None, "max_chart_dev": round(worst, 4), "shrunk": shrunk}


# ---------------------------------------------------------------------------------------------- the kit report


def kit_report(checks: Sequence[PieceCheck], metric: KitMetric) -> dict[str, Any]:
    """EL kit_check's whole-kit result for this kit's pieces, with this kit's gates added: the texel density of every
    piece within ``TEXEL_DEV_MAX`` of the kit median (EL warns; here it fails, because one trim sheet at one nominal
    density means any spread is a mapping defect, see ``check_piece``) and one shared height."""
    pieces: dict[str, dict[str, Any]] = {}
    for c in checks:
        pieces[c.piece] = {"template": c.template, "dims_m": c.dims_m, "texel_px_per_m": c.texel.get("px_per_m"),
                           "texel_max_chart_dev": c.texel.get("max_chart_dev"), "faces": c.faces,
                           "errors": list(c.errors), "warnings": list(c.warnings)}
    tds = {c.piece: c.texel["px_per_m"] for c in checks if c.texel.get("px_per_m")}
    med = float(np.median(list(tds.values()))) if tds else None
    spread = None
    if med and len(tds) > 1:
        devs = {n: td / med - 1 for n, td in tds.items()}
        spread = round(max(abs(d) for d in devs.values()), 4)
        for n, d in devs.items():
            pieces[n]["texel_dev_from_median"] = round(d, 4)
            if abs(d) > TEXEL_DEV_MAX:
                pieces[n]["errors"].append(f"texel density {tds[n]:.0f} px/m vs kit median {med:.0f} ({d:+.0%}, limit {TEXEL_DEV_MAX:.0%})")
    heights = {c.piece: c.dims_m[2] for c in checks if c.template in SHARED_HEIGHT}
    if len(set(heights.values())) > 1:
        for n, h in heights.items():
            if abs(h - metric.wall_height_m) > DIM_TOL_M:
                pieces[n]["errors"].append(f"height {_g(h)} m differs from the kit's shared height {_g(metric.wall_height_m)} m")
    for n, p in pieces.items():
        p["errors"] = list(dict.fromkeys(p["errors"]))
    return {"pass": not any(p["errors"] for p in pieces.values()), "grid_m": metric.grid_m, "pivot": metric.pivot,
            "texel_median_px_per_m": round(med, 1) if med else None, "texel_spread_from_median": spread, "pieces": pieces}


# ---------------------------------------------------------------------------------------------- the snap test

_ROT = {0: np.eye(3), 90: np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]]), 180: np.diag([-1.0, -1, 1]),
        270: np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1.0]])}


@dataclass(frozen=True)
class Placement:
    """One piece placed in a test layout: rotated about Z by ``rot`` degrees, then translated by ``translate``."""

    piece: str
    template: str
    rot: int
    translate: tuple[float, float, float]


def place(meshes: Sequence[MeshData], rot: int, t: Sequence[float]) -> list[MeshData]:
    R = _ROT[rot]
    move = lambda p: np.asarray(p, float) @ R.T + np.asarray(t, float)  # noqa: E731
    return [replace(m, points=move(m.points), collider_points=None if m.collider_points is None else move(m.collider_points),
                    run=None if m.run is None else m.run.moved(move), grain=None if m.grain is None else m.grain @ R.T)
            for m in meshes]


def _target(metric: KitMetric, template: str, rot: int, env_min: Sequence[float]) -> tuple[float, float, float]:
    """The translation that puts the rotated envelope's minimum at ``env_min`` (a position derived from the metric)."""
    lo, hi = metric.envelope(template)
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]) @ _ROT[rot].T
    return tuple(float(v) for v in np.asarray(env_min, float) - corners.min(0))


def layouts(metric: KitMetric, by_template: dict[str, list[str]]) -> dict[str, tuple[list[Placement], list[tuple[int, int]]]]:
    """The assemblies the four piece types allow, placed by the metric alone. Each layout lists its placements and the
    pairs (by index) that must meet. Walls stand on floor tiles where a floor is part of the layout.

    * ``run``: every wall and doorway variant in one straight line on a row of floor tiles (a wall-wall joint is always
      included by repeating the first wall), with a pillar standing on the first tile.
    * ``corner``: an L corner; both walls' pivots on the same grid point, one turned 90 degrees.
    * ``room``: a closed room of four walls (a doorway in one of them when the kit has one) on floor tiles.
    * ``storey``: two walls on floor tiles with a second storey of floor tiles resting on their tops.
    * ``t_junction``: a wall butting into the -Y face of a two-wall run (the face that carries no relief).
    """
    L, D, Tf, p = metric.module_m, metric.wall_thickness_m, metric.floor_thickness_m, metric.pillar_m
    walls, doors = by_template.get("wall_segment", []), by_template.get("doorway", [])
    floors, pillars = by_template.get("floor_tile", []), by_template.get("pillar", [])
    tpl = {n: t for t, ns in by_template.items() for n in ns}
    out: dict[str, tuple[list[Placement], list[tuple[int, int]]]] = {}

    def put(name: str, rot: int, env_min: Sequence[float]) -> Placement:
        return Placement(name, tpl[name], rot, _target(metric, tpl[name], rot, env_min))

    run = ([walls[0], walls[0], *walls[1:]] if walls else []) + [x for d in doors for x in (d, walls[0] if walls else d)]
    if len(run) >= 2:
        z = Tf if floors else 0.0
        pl = [put(n, 0, (k * L, 0, z)) for k, n in enumerate(run)]
        pairs = [(k, k + 1) for k in range(len(run) - 1)]
        if floors:
            base = len(pl)
            pl += [put(floors[k % len(floors)], 0, (k * L, 0, 0)) for k in range(len(run))]
            pairs += [(base + k, k) for k in range(len(run))] + [(base + k, base + k + 1) for k in range(len(run) - 1)]
            if pillars:
                pl.append(put(pillars[0], 0, (0, L - p, Tf)))
                pairs.append((base, len(pl) - 1))
        out["run"] = (pl, pairs)
    if walls:
        w = walls[0]
        out["corner"] = ([put(w, 0, (0, 0, 0)), put(w, 90, (-D, 0, 0))], [(1, 0)])
        front = doors[0] if doors else w
        z = Tf if floors else 0.0
        pl = [put(front, 0, (0, 0, z)), put(w, 90, (-D, 0, z)), put(w, 180, (0, L - D, z)), put(w, 270, (L, 0, z))]
        pairs = [(1, 0), (0, 3), (1, 2), (2, 3)]
        if floors:
            pl += [put(floors[0], 0, (x, 0, 0)) for x in (-L, 0, L)]
            pairs += [(5, 0), (4, 1), (5, 2), (6, 3), (4, 5), (5, 6)]
        out["room"] = (pl, pairs)
        if floors:  # a second storey: a floor tile resting on the wall tops (stacking, and the storey pitch it implies)
            pl = [put(w, 0, (0, 0, Tf)), put(walls[-1], 0, (L, 0, Tf)), put(floors[0], 0, (0, 0, 0)), put(floors[-1], 0, (L, 0, 0)),
                  put(floors[0], 0, (0, 0, Tf + metric.wall_height_m)), put(floors[-1], 0, (L, 0, Tf + metric.wall_height_m))]
            out["storey"] = (pl, [(0, 1), (2, 0), (3, 1), (2, 3), (0, 4), (1, 5), (4, 5)])
        stem = math.floor((L - D) / 2 / metric.grid_m + 1e-9) * metric.grid_m  # a grid point along the first wall
        out["t_junction"] = ([put(w, 0, (0, 0, 0)), put(walls[-1], 0, (L, 0, 0)), put(w, 270, (stem, -L, 0))], [(0, 1), (2, 0)])
    return out


def _joint(a: list[MeshData], b: list[MeshData]) -> dict[str, Any]:
    """Measure where two placed pieces meet: the axis they meet along, the gap (or overlap) between their envelopes
    along it, and how much of each contact face the other covers (exact polygon areas)."""
    pa, pb = np.vstack([m.points for m in a]), np.vstack([m.points for m in b])
    alo, ahi, blo, bhi = pa.min(0), pa.max(0), pb.min(0), pb.max(0)
    seps = [max(blo[i] - ahi[i], alo[i] - bhi[i]) for i in range(3)]
    axis = int(np.argmax(seps))  # the axis the envelopes are separated (or touching) along; the other two overlap
    first, second = (a, b) if blo[axis] - ahi[axis] >= alo[axis] - bhi[axis] else (b, a)
    f_hi = np.vstack([m.points for m in first]).max(0)[axis]
    s_lo = np.vstack([m.points for m in second]).min(0)[axis]
    gap = float(s_lo - f_hi)
    fa = plane_faces(first, axis, float(f_hi), 1)
    fb = plane_faces(second, axis, float(s_lo), -1)
    shared = overlap_area(fa, fb) if abs(gap) <= PLANE_TOL_M else 0.0
    area_a, area_b = faces_area(fa), faces_area(fb)
    cover = max(shared / area_a if area_a else 0.0, shared / area_b if area_b else 0.0)
    return {"axis": "xyz"[axis], "gap_m": round(gap, 6), "contact_m2": round(shared, 6), "smaller_face_covered": round(cover, 4)}


def snap_test(metric: KitMetric, pieces: dict[str, tuple[str, list[MeshData]]]) -> dict[str, Any]:
    """Assemble the layouts the kit's piece types allow and measure every intended joint and every pair of parts.

    A joint fails with a gap above ``GAP_TOL_M``, or when the smaller of its two contact faces is not covered by the
    other (a slit or a missed contact). Any two parts of different placed pieces that interpenetrate by more than
    ``OVERLAP_TOL_M`` fail (exact separating-axis depth on convex parts). ``missing`` names the arrangements that the
    piece types cannot express (AR's minimum kit, audit row K9)."""
    by_template: dict[str, list[str]] = {}
    for name, (tpl, _) in pieces.items():
        by_template.setdefault(tpl, []).append(name)
    res: dict[str, Any] = {"layouts": {}, "pass": True}
    for lname, (pl, pairs) in layouts(metric, by_template).items():
        placed = [place(pieces[p.piece][1], p.rot, p.translate) for p in pl]
        joints, problems = [], []
        for i, j in pairs:
            jm = {"a": f"{pl[i].piece}#{i}", "b": f"{pl[j].piece}#{j}", **_joint(placed[i], placed[j])}
            joints.append(jm)
            if jm["gap_m"] > GAP_TOL_M:
                problems.append(f"{jm['a']}/{jm['b']}: gap {jm['gap_m'] * 1000:.1f} mm along {jm['axis']}")
            elif jm["smaller_face_covered"] < FULL_FACE:
                problems.append(f"{jm['a']}/{jm['b']}: contact covers {jm['smaller_face_covered']:.1%} of the smaller face along {jm['axis']}")
        convex = [parts_of(ms) for ms in placed]
        worst, worst_at = 0.0, ""
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                for ca in convex[i]:
                    for cb in convex[j]:
                        d = penetration(ca, cb)
                        if d > worst:
                            worst, worst_at = d, f"{pl[i].piece}#{i}/{ca.name} into {pl[j].piece}#{j}/{cb.name}"
        if worst > OVERLAP_TOL_M:
            problems.append(f"interpenetration {worst * 1000:.2f} mm: {worst_at}")
        pivots = [p.translate for p in pl]
        off_grid = [f"{pl[k].piece}#{k}" for k, t in enumerate(pivots) if not all(_on_grid(v, metric.grid_m) for v in t[:2])]
        res["layouts"][lname] = {
            "placements": [asdict(p) for p in pl], "joints": joints,
            "max_gap_m": max((j["gap_m"] for j in joints), default=0.0), "max_interpenetration_m": round(worst, 6),
            "min_face_covered": min((j["smaller_face_covered"] for j in joints), default=1.0),
            "pivots_off_grid": off_grid, "problems": problems, "pass": not problems}
        res["pass"] = res["pass"] and not problems
    have = set(by_template)
    res["missing"] = [m for m, needs in (
        ("straight run (wall_segment x2)", {"wall_segment"}), ("doorway in a run", {"wall_segment", "doorway"}),
        ("floor under walls", {"wall_segment", "floor_tile"})) if not needs <= have]
    res["not_expressible"] = [
        "outside corner with symmetric interior: needs a corner piece (AR minimum kit: corner x2); the L corner here is a "
        "butt joint, so a room's interior is offset by one wall thickness on two sides",
        "T junction against a wall's inner face on the grid: needs a T or corner piece; only the outer-face T is on the grid",
        "window frame and ceiling tile (AR minimum kit)"]
    if not res["layouts"]:
        res["pass"] = False
        res["missing"].append("no layout: the kit has no wall_segment or doorway")
    return res


def context_brief(context: dict[str, Any]) -> str:
    """The metric text for one authoring call, or "" when the call is not for a kit piece. A second request for a piece
    that came back off-metric also carries what was wrong (``context["kit_feedback"]``)."""
    metric, template = context.get("kit_metric"), context.get("template")
    if not isinstance(metric, KitMetric) or template not in KIT_TEMPLATES:
        return ""
    text = metric.brief(template)
    if context.get("kit_feedback"):
        text += f" Previous answer rejected: {context['kit_feedback']}"
    return text
