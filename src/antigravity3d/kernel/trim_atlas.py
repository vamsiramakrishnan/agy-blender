"""Trim-sheet-first UVs: one shared texture per kit, props map their faces onto tiling strips.

Conventions
- V is up (v=1 is the top image row); a strip covers v in [v0, v1]; image row y = (1 - v) * resolution.
- Texel density of a strip is (v1 - v0) * resolution / world_height_m px/m, same along U and V
  (square texels), so U in metres is u = x_m * (v1 - v0) / world_height_m and runs past 1.0 on
  strips with tile_u (that is the point: the strip repeats).
- A chart whose short extent does not fit its strip (minus padding), or, for tile_u=False strips,
  whose long extent does not fit [0, 1], is scaled DOWN uniformly (never split); the density
  deviation is reported per chart in TrimReport.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable, Union

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.spatial import ConvexHull, QhullError

from antigravity3d.kernel.fast_ops import UVResult, _canon, _stretch, _tri_areas
from antigravity3d.kernel.types import MeshData

BYTES_PER_PX_RGBA8 = 4.0      # uncompressed RGBA8
BYTES_PER_PX_BC7 = 1.0        # BC7: 16 bytes per 4x4 block
MIP_FACTOR = 4.0 / 3.0        # full mip chain
PAD_PX = 4.0                  # padding to strip edges, in sheet pixels
SMOOTH_ANGLE_DEG = 30.0       # adjacent faces closer than this belong to the same chart
PLANAR_SPREAD_DEG = 25.0      # chart normals within this of the mean -> planar projection
_EPS = 1e-6


@dataclass(frozen=True)
class TrimStrip:
    name: str
    v0: float
    v1: float
    tile_u: bool = True
    world_height_m: float = 0.0
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not (0.0 <= self.v0 < self.v1 <= 1.0 + _EPS) or self.world_height_m <= 0:
            raise ValueError(f"bad strip {self.name}: v=[{self.v0},{self.v1}] world_height_m={self.world_height_m}")


@dataclass(frozen=True)
class TrimSheet:
    resolution: int
    strips: list[TrimStrip]

    # (name, pixel rows at 4096, tile_u, tags): listed top to bottom; 1024 px per metre at 4096 (sheet spans 4 m)
    _STANDARD = (
        ("painted_metal", 1024, True, ("metal", "paint")),
        ("rivet_band", 256, True, ("metal", "detail")),
        ("wood_planks", 1024, True, ("wood",)),
        ("worn_edge", 128, True, ("edge", "wear")),
        ("cloth_weave", 512, True, ("fabric",)),
        ("rubber_grip", 256, True, ("rubber",)),
        ("concrete", 896, True, ("stone",)),
    )

    @classmethod
    def standard(cls, resolution: int = 4096) -> "TrimSheet":
        strips, y = [], 0
        for name, px, tile, tags in cls._STANDARD:
            v1, v0 = 1.0 - y / 4096, 1.0 - (y + px) / 4096
            strips.append(TrimStrip(name, v0, v1, tile, px / 1024.0, tags))
            y += px
        return cls(resolution, strips)

    def strip(self, name: str) -> TrimStrip:
        for s in self.strips:
            if s.name == name:
                return s
        raise KeyError(f"no strip {name!r}; have {[s.name for s in self.strips]}")

    def density_px_per_m(self, s: TrimStrip) -> float:
        return (s.v1 - s.v0) * self.resolution / s.world_height_m

    def to_json(self) -> str:
        return json.dumps({"resolution": self.resolution, "strips": [asdict(s) for s in self.strips]}, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "TrimSheet":
        d = json.loads(text)
        return cls(int(d["resolution"]), [TrimStrip(**{**s, "tags": tuple(s.get("tags", ()))}) for s in d["strips"]])

    def layout_prompt(self) -> str:
        r = self.resolution
        lines = [
            f"Generate one {r}x{r} px trim sheet texture, split into {len(self.strips)} full-width horizontal strips, listed top to bottom.",
            "Strips marked TILES must repeat seamlessly left-to-right (left edge matches right edge). Keep each strip's content inside its rows; leave no gaps between strips.",
        ]
        for i, s in enumerate(sorted(self.strips, key=lambda s: -s.v1), 1):
            y0, y1 = round((1 - s.v1) * r), round((1 - s.v0) * r)
            tag = f" [{', '.join(s.tags)}]" if s.tags else ""
            lines.append(
                f"{i}. {s.name}{tag}: rows y={y0}..{y1 - 1} ({y1 - y0} px tall = {s.world_height_m:g} m of surface), "
                + ("TILES horizontally." if s.tile_u else "does NOT tile; one image across the full width.")
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class Chart:
    index: int
    faces: np.ndarray        # indices into mesh.triangles
    normal: np.ndarray       # area-weighted unit normal
    area: float              # m^2
    kind: str                # "planar" | "cylinder"
    label: str               # planar: dominant world axis "+z"...; cylinder: "side"
    extent: tuple[float, float]   # (long, short) in metres


Assignment = Union[dict, Callable[[Chart], "str | None"], str]


@dataclass(frozen=True)
class ChartInfo:
    index: int
    strip: str | None
    kind: str
    label: str
    area: float
    scale: float             # uniform shrink applied to fit (1.0 = none)
    density_dev: float       # measured density / strip nominal - 1
    u_tiles: float           # U extent in tiles (1.0 = one full repeat)


@dataclass(frozen=True)
class VramEstimate:
    shared_bytes: float
    unique_bytes: float

    @property
    def ratio(self) -> float:
        return self.unique_bytes / self.shared_bytes

    @property
    def saved_bytes(self) -> float:
        return self.unique_bytes - self.shared_bytes


@dataclass(frozen=True)
class TrimReport:
    sheet_resolution: int
    charts: list[ChartInfo]
    strip_usage: dict[str, dict[str, float]]   # strip -> {"charts": n, "area_m2": a}
    mapped_area_fraction: float

    @property
    def max_density_dev(self) -> float:
        return max((abs(c.density_dev) for c in self.charts if c.strip), default=0.0)

    def vram_estimate(self, n_props: int, unique_res: int = 2048, maps: int = 3, *, compressed: bool = False) -> VramEstimate:
        """Shared: `maps` sheets of sheet_resolution^2. Unique: n_props * maps textures of unique_res^2.
        Per pixel BYTES_PER_PX_RGBA8 x MIP_FACTOR, or BYTES_PER_PX_BC7 x MIP_FACTOR if compressed."""
        bpp = (BYTES_PER_PX_BC7 if compressed else BYTES_PER_PX_RGBA8) * MIP_FACTOR
        return VramEstimate(maps * self.sheet_resolution**2 * bpp, n_props * maps * unique_res**2 * bpp)


@dataclass(frozen=True)
class TrimResult(UVResult):
    tri_chart: np.ndarray | None = None     # (M,) chart index per triangle (same order as input)
    tri_strip: np.ndarray | None = None     # (M,) index into sheet.strips, -1 = unmapped
    charts: list[Chart] = field(default_factory=list)
    report: TrimReport | None = None


# --------------------------------------------------------------------------- segmentation

def _segment(mesh: MeshData, angle_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pts, tris = np.asarray(mesh.points, np.float64), np.asarray(mesh.triangles, np.int64)
    n = np.cross(pts[tris[:, 1]] - pts[tris[:, 0]], pts[tris[:, 2]] - pts[tris[:, 0]])
    area = 0.5 * np.linalg.norm(n, axis=1)
    n /= np.maximum(2 * area, 1e-18)[:, None]
    cid = _canon(pts)[tris]
    e = np.concatenate([cid[:, [0, 1]], cid[:, [1, 2]], cid[:, [2, 0]]])
    f = np.tile(np.arange(len(tris)), 3)
    key = np.sort(e, axis=1)
    order = np.lexsort((key[:, 1], key[:, 0]))
    ks, fs = key[order], f[order]
    same = (ks[1:] == ks[:-1]).all(axis=1)
    a, b = fs[:-1][same], fs[1:][same]
    smooth = (n[a] * n[b]).sum(axis=1) >= np.cos(np.radians(angle_deg))
    a, b = a[smooth], b[smooth]
    g = sparse.coo_matrix((np.ones(len(a)), (a, b)), shape=(len(tris), len(tris)))
    _, lab = connected_components(g, directed=False)
    return lab, n, area


def _label(normal: np.ndarray) -> str:
    ax = int(np.argmax(np.abs(normal)))
    return ("+" if normal[ax] >= 0 else "-") + "xyz"[ax]


def _min_area_rect(p2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Rotation (2,2: rows = new axes) of the min-area bbox, and its (w, h) along those axes."""
    try:
        hull = p2[ConvexHull(p2).vertices]
        d = np.roll(hull, -1, axis=0) - hull
        ang = np.unique(np.round(np.arctan2(d[:, 1], d[:, 0]) % (np.pi / 2), 9))
    except (QhullError, ValueError):
        ang = np.array([0.0])
    c, s = np.cos(ang), np.sin(ang)
    R = np.stack([np.stack([c, s], -1), np.stack([-s, c], -1)], axis=1)   # (A,2,2)
    q = np.einsum("aij,nj->ani", R, p2)
    wh = q.max(axis=1) - q.min(axis=1)
    k = int(np.argmin(wh[:, 0] * wh[:, 1]))
    return R[k], wh[k]


def _planar(pts: np.ndarray, normal: np.ndarray) -> np.ndarray:
    t1 = np.cross(normal, [0, 0, 1.0] if abs(normal[2]) < 0.9 else [1.0, 0, 0])
    t1 /= np.linalg.norm(t1)
    t2 = np.cross(normal, t1)   # t1 x t2 = normal: CCW seen from outside = positive area
    p2 = np.stack([pts @ t1, pts @ t2], axis=1)
    R, _ = _min_area_rect(p2)
    return p2 @ R.T


def _cylinder(pts: np.ndarray, tris_local: np.ndarray, normals: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Per-corner (M,3,2) arc-length unwrap of a chart around its fitted axis; u=arc, v=along axis."""
    cov = (normals * weights[:, None]).T @ normals
    axis = np.linalg.eigh(cov)[1][:, 0]                      # normals are ~perpendicular to the axis
    c = pts.mean(axis=0)
    rel = pts - c
    h = rel @ axis
    rad = rel - h[:, None] * axis
    R = float(np.linalg.norm(rad, axis=1).mean())
    e1 = np.cross(axis, [0, 0, 1.0] if abs(axis[2]) < 0.9 else [1.0, 0, 0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(axis, e1)
    ang = np.arctan2(rad @ e2, rad @ e1)
    s = np.sort(ang)
    gaps = np.diff(np.concatenate([s, [s[0] + 2 * np.pi]]))
    k = int(np.argmax(gaps))
    mid = s[k] + gaps[k] / 2                                  # put the seam in the widest empty gap
    ang = (ang - mid + np.pi) % (2 * np.pi) - np.pi
    ca = ang[tris_local]
    lo = ca.max(axis=1, keepdims=True) - ca > np.pi
    ca = np.where(lo, ca + 2 * np.pi, ca)
    # outward-normal orientation: u increases CCW about the axis, u x v = outward when axis is "up"
    # (normals flip sign if the mean normal opposes e1-rotation; fixed by the sign check below)
    uv = np.stack([ca * R, np.broadcast_to(h[tris_local], ca.shape)], axis=-1)
    a = _signed_area(uv)
    if np.median(a) < 0:
        uv[..., 0] *= -1
    return uv


def _signed_area(uv: np.ndarray) -> np.ndarray:
    a, b, c = uv[:, 0], uv[:, 1], uv[:, 2]
    return 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))


def _resolve(assignment: Assignment, ch: Chart) -> str | None:
    if isinstance(assignment, str):
        return assignment
    if callable(assignment):
        return assignment(ch)
    for k in (ch.index, ch.label, ch.kind, "*"):
        if k in assignment:
            return assignment[k]
    return None


def map_to_trim(
    mesh: MeshData,
    sheet: TrimSheet,
    assignment: Assignment,
    *,
    angle_deg: float = SMOOTH_ANGLE_DEG,
    pad_px: float = PAD_PX,
) -> TrimResult:
    """Chart the mesh and map every assigned chart onto its strip.

    assignment: a strip name (everything), a callable(Chart) -> strip name | None, or a dict
    keyed by chart index, chart label ("+z", "side"...), kind ("planar"/"cylinder") or "*".
    Unassigned charts keep their triangles with UV (0,0) and strip index -1.
    """
    pts, tris = np.asarray(mesh.points, np.float64), np.asarray(mesh.triangles, np.int64)
    lab, fn, fa = _segment(mesh, angle_deg)
    res = sheet.resolution
    names = [s.name for s in sheet.strips]
    corner_uv = np.zeros((len(tris), 3, 2))
    tri_strip = np.full(len(tris), -1, np.int32)
    charts: list[Chart] = []
    infos: list[ChartInfo] = []

    for ci in range(lab.max() + 1 if len(lab) else 0):
        faces = np.nonzero(lab == ci)[0]
        nrm = (fn[faces] * fa[faces, None]).sum(axis=0)
        nrm /= max(np.linalg.norm(nrm), 1e-18)
        spread = np.degrees(np.arccos(np.clip(fn[faces] @ nrm, -1, 1))).max()
        verts = np.unique(tris[faces])
        local = np.searchsorted(verts, tris[faces])
        cp = pts[verts]
        cyl = spread > PLANAR_SPREAD_DEG
        if cyl:
            uv = _cylinder(cp, local, fn[faces], fa[faces])
        else:
            uv = _planar(cp, nrm)[local]
            if np.median(_signed_area(uv)) < 0:   # degenerate hull fallback guard
                uv[..., 1] *= -1
        flat = uv.reshape(-1, 2)
        lo, hi = flat.min(axis=0), flat.max(axis=0)
        ext = hi - lo
        if ext[1] > ext[0]:                       # long axis along U: rotate -90 deg (keeps orientation)
            uv = np.stack([uv[..., 1], -uv[..., 0]], axis=-1)
            flat = uv.reshape(-1, 2)
            lo, hi = flat.min(axis=0), flat.max(axis=0)
            ext = hi - lo
        ch = Chart(ci, faces, nrm, float(fa[faces].sum()), "cylinder" if cyl else "planar", "side" if cyl else _label(nrm), (float(ext[0]), float(ext[1])))
        charts.append(ch)
        sname = _resolve(assignment, ch)
        if sname is None:
            infos.append(ChartInfo(ci, None, ch.kind, ch.label, ch.area, 1.0, 0.0, 0.0))
            continue
        st = sheet.strip(sname)
        dens = sheet.density_px_per_m(st)
        k = 1.0                                    # px/m -> uv: s = k * (v1-v0)/world_height_m per metre
        per_m = (st.v1 - st.v0) / st.world_height_m
        avail_v = (st.v1 - st.v0) - 2 * pad_px / res
        k = min(1.0, avail_v / max(ext[1] * per_m, _EPS))
        if not st.tile_u:
            k = min(k, (1.0 - 2 * pad_px / res) / max(ext[0] * per_m, _EPS))
        u = (uv[..., 0] - lo[0]) * per_m * k
        v = (uv[..., 1] - lo[1] - ext[1] / 2) * per_m * k + (st.v0 + st.v1) / 2   # centred in strip
        if not st.tile_u:
            u = u + (1.0 - ext[0] * per_m * k) / 2
        corner_uv[faces] = np.stack([u, v], axis=-1)
        tri_strip[faces] = names.index(sname)
        a3 = fa[faces].sum()
        a2 = _tri_areas(corner_uv[faces].reshape(-1, 2), np.arange(len(faces) * 3).reshape(-1, 3), 2).sum()
        meas = np.sqrt(a2 / a3) * res if a3 > 0 else 0.0
        infos.append(ChartInfo(ci, sname, ch.kind, ch.label, ch.area, float(k), float(meas / dens - 1.0), float((u.max() - u.min()))))

    # weld corners into vertices: same source vertex, chart and uv
    chart_of_tri = lab.astype(np.int64)
    rows = np.concatenate([tris.reshape(-1, 1), np.repeat(chart_of_tri, 3)[:, None], corner_uv.reshape(-1, 2)], axis=1)
    uniq, first, inv = np.unique(np.round(rows, 7), axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    vmap = tris.reshape(-1)[first]
    out_pts = pts[vmap]
    out_tris = inv.reshape(-1, 3).astype(np.int32)
    out_uv = corner_uv.reshape(-1, 2)[first].astype(np.float32)

    mapped = tri_strip >= 0
    a3 = _tri_areas(pts, tris, 3)
    sm, sx = _stretch(out_pts, out_tris, out_uv) if mapped.all() else _stretch(out_pts, out_tris[mapped], out_uv) if mapped.any() else (0.0, 0.0)
    a2 = _tri_areas(out_uv.astype(np.float64), out_tris, 2)
    dens = float(np.sqrt(a2[mapped].sum() / a3[mapped].sum()) * res) if mapped.any() and a3[mapped].sum() > 0 else 0.0
    usage: dict[str, dict[str, float]] = {s.name: {"charts": 0.0, "area_m2": 0.0} for s in sheet.strips}
    for inf in infos:
        if inf.strip:
            usage[inf.strip]["charts"] += 1
            usage[inf.strip]["area_m2"] += inf.area
    total = float(a3.sum())
    report = TrimReport(res, infos, usage, float(a3[mapped].sum() / total) if total > 0 else 0.0)
    return TrimResult(
        mesh.node_id, out_pts, out_tris, out_uv, vmap, dens, sm, sx, res, "trim", all(i.scale >= 1.0 - 1e-9 for i in infos),
        tri_chart=lab.astype(np.int32), tri_strip=tri_strip, charts=charts, report=report,
    )


# --------------------------------------------------------------------------- validation

def check_strip_bounds(r: TrimResult, sheet: TrimSheet, tol: float = 1e-5) -> list[str]:
    """Every mapped triangle's V range must stay within its strip's [v0, v1] (and U in [0,1] when not tiling)."""
    issues = []
    for si, s in enumerate(sheet.strips):
        sel = r.tri_strip == si
        if not sel.any():
            continue
        uv = r.uvs[r.triangles[sel]]
        if uv[..., 1].min() < s.v0 - tol or uv[..., 1].max() > s.v1 + tol:
            issues.append(f"strip {s.name}: V [{uv[..., 1].min():.5f},{uv[..., 1].max():.5f}] outside [{s.v0:.5f},{s.v1:.5f}]")
        if not s.tile_u and (uv[..., 0].min() < -tol or uv[..., 0].max() > 1 + tol):
            issues.append(f"strip {s.name}: U leaves [0,1] on a non-tiling strip")
    return issues


def check_no_flips(r: TrimResult, min_area: float = 1e-12) -> list[str]:
    """Within each mapped chart all non-degenerate UV triangles must share one winding sign."""
    issues = []
    sa = _signed_area(r.uvs[r.triangles].astype(np.float64))
    for ci in np.unique(r.tri_chart[r.tri_strip >= 0]):
        a = sa[(r.tri_chart == ci) & (np.abs(sa) > min_area)]
        if len(a) and (a > 0).any() and (a < 0).any():
            issues.append(f"chart {ci}: {int((a < 0).sum())} of {len(a)} triangles flipped")
    return issues


def validate_trim(r: TrimResult, sheet: TrimSheet) -> list[str]:
    return check_strip_bounds(r, sheet) + check_no_flips(r)
