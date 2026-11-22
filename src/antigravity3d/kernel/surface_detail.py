"""Where the texels of a shared atlas come from: the two questions the layout and the bake ask of a material.

The opaque materials of a prop share one UV atlas (``fast_ops.unwrap_scene``) and their maps are baked into one texture
set (``package._compose_atlas``). Two things about each material decide that work, and both are answered here by
*surface sources*, so a new kind of surface plugs in without changing the layout or the bake:

* ``detail_px_per_m(material, target_px_per_m)``: how much detail the material carries, in texels per metre. The
  shared atlas is laid out no denser than the most detailed material needs (``fast_ops.run_export_pipeline``): a 256 px
  tileable map holds an eighth of what a 2048 px one does, and a flat colour needs almost none (``FLAT_DETAIL_PX``).
* ``texels(material, role, raster)``: the material's texels for one map role (``albedo``, ``orm``, ``normal``,
  ``emissive``) over the whole atlas, as (height, width, 3) 8-bit RGB, or None when this source has nothing for it.
  The bake keeps each material's texels on its charts and its gutters; ``raster`` says which material and which
  triangle every texel belongs to, so a source can paint in atlas space (edge wear, cavity dirt, grain) from the
  surface position under each texel.

Sources are asked in registration order; the built-in map source (the material's own image files) is first. A
material no source answers for is a flat patch of its factors (``package._flat_texel``). Register a source with
``register_source``; ``sources()`` lists them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import numpy as np
from PIL import Image

from antigravity3d.kernel.compile_ir import MaterialParams
from antigravity3d.kernel.fast_ops import FLAT_DETAIL_PX, SWATCH_SPAN_PX

ROLES = ("albedo", "orm", "normal", "emissive")


@dataclass(frozen=True)
class AtlasPart:
    """One mesh in the atlas: its world points, uvs and shipped triangles; its triangles are numbered from
    ``tri_offset`` in ``AtlasRaster.triangle``."""
    node_id: str
    material: str
    points: np.ndarray
    uvs: np.ndarray
    triangles: np.ndarray
    tri_offset: int


@dataclass(frozen=True)
class AtlasRaster:
    """Who owns every texel of an atlas of ``size`` (width, height) px; arrays are rows x columns, row 0 at v = 1.

    ``drawn``: label of the chart drawn on the texel, 0 for a gutter texel. ``owner``: the label of the nearest chart
    texel (gutters belong to their nearest chart, so a gap between two materials is split down its middle).
    ``distance``: px to that chart texel (0 on a chart). ``triangle``: global id of the triangle under the texel, or
    of the nearest chart texel's triangle in a gutter (-1 when nothing is drawn); ``parts`` map ids back to meshes.
    ``labels``: material name -> label. ``repeat_px``: atlas px per repeat of a tileable map; ``density_px_per_m``:
    the layout's texel density."""
    size: tuple[int, int]
    drawn: np.ndarray
    owner: np.ndarray
    distance: np.ndarray
    triangle: np.ndarray
    parts: tuple[AtlasPart, ...] = ()
    labels: Mapping[str, int] = field(default_factory=dict)
    repeat_px: int = SWATCH_SPAN_PX
    density_px_per_m: float = 0.0

    def part_of(self, tri: int) -> tuple[AtlasPart, int]:
        """The part a global triangle id belongs to, and its index in that part's triangles."""
        for p in reversed(self.parts):
            if tri >= p.tri_offset:
                return p, tri - p.tri_offset
        raise IndexError(tri)

    def region(self, material: str) -> np.ndarray:
        """Texels the material owns (its charts and its share of the gutters)."""
        return self.owner == self.labels[material]


def rasterise(size: tuple[int, int], parts: Sequence[tuple[int, np.ndarray, np.ndarray]]) -> tuple[np.ndarray, ...]:
    """(owner, drawn, distance, triangle) of an atlas of ``size`` (width, height) px; ``parts``: (label >= 1, uvs,
    triangles). Triangles are drawn in glTF rows (v = 1 at the top, as the GLB writer flips v); triangles with a uv
    outside the atlas (charts of hidden contact faces lie right of it, ``fast_ops._pack_analytic``) are skipped. The
    gutter texels take the label and triangle of the nearest chart texel (a Euclidean distance transform). Triangle
    ids count across ``parts`` in order."""
    from PIL import ImageDraw
    from scipy.ndimage import distance_transform_edt

    W, H = size
    lab_img, tri_img = Image.new("I", (W, H), 0), Image.new("I", (W, H), 0)
    dl, dt = ImageDraw.Draw(lab_img), ImageDraw.Draw(tri_img)
    base = 0
    for label, uv, tri in parts:
        uv = np.asarray(uv, np.float64)
        t = np.asarray(tri, np.int64)
        if len(t):
            q = uv[t]
            keep = np.flatnonzero((q <= 1.0 + 1e-6).all(axis=(1, 2)) & (q >= -1e-6).all(axis=(1, 2)))
            px = np.stack([q[keep, :, 0] * W - 0.5, (1.0 - q[keep, :, 1]) * H - 0.5], axis=-1)
            for k, p in zip(keep, px):
                poly = [tuple(v) for v in p]
                dl.polygon(poly, fill=int(label), outline=int(label))
                dt.polygon(poly, fill=int(base + k + 1), outline=int(base + k + 1))
        base += len(t)
    drawn = np.asarray(lab_img, np.int32)
    tri_id = np.asarray(tri_img, np.int64) - 1
    if not drawn.any():
        return drawn, drawn, np.zeros(drawn.shape), tri_id
    dist, idx = distance_transform_edt(drawn == 0, return_distances=True, return_indices=True)
    return drawn[idx[0], idx[1]], drawn, dist, tri_id[idx[0], idx[1]]


class SurfaceSource(Protocol):
    """Answers the layout's and the bake's two questions for the materials it knows (None: not mine)."""
    name: str

    def detail_px_per_m(self, material: MaterialParams, target_px_per_m: float) -> float | None: ...

    def texels(self, material: MaterialParams, role: str, raster: AtlasRaster) -> np.ndarray | None: ...

    # optional: ``roles(material) -> set[str]``, the roles it paints (a role no source paints is not baked when every
    # member's flat value for it is the same: a grain that paints base colour only adds no normal map)


def _map_file(material: MaterialParams, role: str) -> Path | None:
    ref = material.maps.get(role)
    if ref is None:
        return None
    raw = ref.resolved or ref.authored
    p = Path(raw[7:] if raw.startswith("file://") else raw)
    return p if p.is_file() else None


def tile_map(path: Path, role: str, size: tuple[int, int], period: int, convention: str) -> tuple[np.ndarray, int]:
    """A tileable map over an atlas of ``size`` (width, height) px, one repeat every ``period`` atlas px from uv (0, 0)
    (the bottom left: rows run top down), as 8-bit RGB; and the map's own side. Resampled once to ``period`` px
    (Lanczos, which filters when shrinking) and repeated, never stretched. A normal map is decoded to the glTF
    (OpenGL) convention and its vectors renormalised after a resize. No rotation: a tangent frame stays the uv frame."""
    with Image.open(path) as im:
        im.load()
        src = im.convert("RGB")
    own = max(src.size)
    if src.size != (period, period):
        src = src.resize((period, period), Image.Resampling.LANCZOS)
    a = np.asarray(src, np.uint8)
    if role == "normal":
        a = a.copy()
        if convention == "directx":
            a[..., 1] = 255 - a[..., 1]
        if own != period:
            n = a.astype(np.float32) / 127.5 - 1.0
            n /= np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)
            a = np.clip(np.rint((n + 1.0) * 127.5), 0, 255).astype(np.uint8)
    W, H = size
    tiled = np.tile(a, (-(-H // period), -(-W // period), 1))
    return np.ascontiguousarray(tiled[tiled.shape[0] - H:, :W]), own


class MapSource:
    """The material's own tileable image files (the stage's ``UsdUVTexture`` maps). A map spans ``SWATCH_SPAN_PX``
    atlas px at the target density, so it carries target x (its side / SWATCH_SPAN_PX) texels per metre. An emissive
    map is multiplied by the emissive colour (glTF: emissive = map x emissiveFactor; the baked set carries the product)."""
    name = "map"

    def __init__(self) -> None:
        self.last_source_px: dict[tuple[str, str], int] = {}  # (material, role) -> the map's side, for the manifest

    def detail_px_per_m(self, material: MaterialParams, target_px_per_m: float) -> float | None:
        sides = []
        for role in material.maps:
            p = _map_file(material, role)
            if p is not None:
                try:
                    with Image.open(p) as im:
                        sides.append(max(im.size))
                except (OSError, ValueError):
                    continue
        return target_px_per_m * max(sides) / SWATCH_SPAN_PX if sides else None

    def texels(self, material: MaterialParams, role: str, raster: AtlasRaster) -> np.ndarray | None:
        p = _map_file(material, role)
        if p is None:
            return None
        pix, own = tile_map(p, role, raster.size, raster.repeat_px, material.normal_convention)
        self.last_source_px[(material.name, role)] = own
        if role == "emissive":
            lin = pix.astype(np.float64) / 255.0
            lin = np.where(lin <= 0.04045, lin / 12.92, ((lin + 0.055) / 1.055) ** 2.4) * np.asarray(material.emissive_color)
            lin = np.clip(lin, 0.0, 1.0)
            pix = np.rint(np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(lin, 1 / 2.4) - 0.055) * 255.0).astype(np.uint8)
        return pix


MAPS = MapSource()
_SOURCES: list[Any] = [MAPS]


def register_source(source: Any) -> None:
    """Adds a surface source after those already registered (the map source stays first: a material's own image files
    win). Registering the same object twice is a no-op."""
    if source not in _SOURCES:
        _SOURCES.append(source)


def unregister_source(source: Any) -> None:
    if source is not MAPS and source in _SOURCES:
        _SOURCES.remove(source)


def sources() -> tuple[Any, ...]:
    return tuple(_SOURCES)


def detail_px_per_m(material: MaterialParams | None, target_px_per_m: float) -> float | None:
    """The most detail any source says the material carries (texels per metre), or None: a flat colour."""
    if material is None:
        return None
    got = [d for s in _SOURCES if (d := s.detail_px_per_m(material, target_px_per_m))]
    return max(got) if got else None


def flat_detail_px_per_m(target_px_per_m: float) -> float:
    """What a shared atlas of flat colours only is laid out for (``fast_ops.FLAT_DETAIL_PX``)."""
    return target_px_per_m * FLAT_DETAIL_PX / SWATCH_SPAN_PX


def texels(material: MaterialParams, role: str, raster: AtlasRaster) -> tuple[np.ndarray, str] | None:
    """The first source's texels for the material's role and that source's name, or None (a flat patch)."""
    for s in _SOURCES:
        got = s.texels(material, role, raster)
        if got is not None:
            return got, s.name
    return None


def has_texels(material: MaterialParams, role: str) -> bool:
    """Whether a source other than the flat factors could answer for this role (for deciding which roles to bake):
    the material has a map for it, or a registered non-map source claims detail for the material."""
    if _map_file(material, role) is not None:
        return True
    for s in _SOURCES:
        if s is MAPS or not s.detail_px_per_m(material, 1.0):
            continue
        roles = getattr(s, "roles", None)  # optional: the roles the source paints (default: all of them)
        if roles is None or role in roles(material):
            return True
    return False
