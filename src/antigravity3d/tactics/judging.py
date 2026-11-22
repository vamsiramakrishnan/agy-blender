"""What the vision judge is shown of a design's shape, in one place.

Every judgement of a design's shape (the geometry ranking, ``tactics.tournament.geometry_tournament``; pass A's blockout
review, ``tactics.pyramid``) shows the judge the same thing: ``JUDGE_VIEWS`` (front, right and top orthographic, a
perspective from the front left and one from the opposite corner), each its own ``JUDGE_VIEW_PX`` image at the high
image setting (``decisions.engine.VIEW_RESOLUTION``), and one design per request. Decided on the live measurement of
2026-11-22 (r5/live/judge_views.py, judge_truth.py; numbers at ``engine.VIEW_RESOLUTION``): against one sheet of four
256 px views at the low setting, separate views doubled the depiction gap between a plain and a detailed crate and
halved its spread, and let the judge report real interpenetrations that it missed on the sheet.

The views show the design as it ships, with the lines a shaded render shows (``JUDGE_VIEW_STYLE``, where the
measurement is): lit with the normals the package writes (``compile_ir.crease_normals``), so a surface that ships smooth
is not judged by facets it will never show, and with its feature edges (part outlines and boundaries, hard creases)
instead of the every-triangle wireframe the sheet always carried. The author's badged views (for an authoring request
and ``locate_defects``) are the same render with the badges drawn over it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Sequence

from PIL import Image, ImageDraw

import numpy as np

from antigravity3d.decisions.models import VisualQAResult
from antigravity3d.kernel.compile_ir import CREASE_DEG, crease_normals
from antigravity3d.kernel.raster import JUDGE_VIEWS, View, render_buffers
from antigravity3d.kernel.som import MarkedSheet, assign_badges, mark_view
from antigravity3d.kernel.types import MeshData
from antigravity3d.tactics.common import Uploader, png_bytes, to_thread_drained

JUDGE_VIEW_PX = 512
# Whether the judge is shown the badged views (then ``views`` and ``author_views`` are the same images). Off: measured
# live on 2026-11-22 (r5/scratch_judge/live_judge_design.py, plain against badged with the legend, same designs), the
# legend of part names raised depiction by up to 0.8 on designs with many well-named parts (director crate 3.55 against
# 2.73): the judge read the names, not the shape.
JUDGE_SEES_BADGES = False

# How a view is drawn. "wire": one normal per triangle and every triangle edge darkened (the contact sheet's look, the
# judge's view until 2026-11-22). "shipped": lit with the shipped normals, no lines. "shipped_edges": the same plus the
# feature edges a shaded render shows (part outlines and boundaries, creases above the shading crease; not the
# triangulation of a smooth surface: ``render_buffers(feature_edges=True)``).
VIEW_STYLES = ("wire", "shipped", "shipped_edges")
# The judge's view, and the author's (the badged views are the same render): "shipped_edges". Measured live on
# 2026-11-22 through judge_design (r5/live/view_style: the four truth-set crates intact / one handle moved 8 cm out /
# handles deleted, two hydrants and two chairs, three repeats, 144 calls; means of three, wire / shipped / shipped_edges):
# - The crate with 26 real interpenetrations, "parts intersect": 0.85 / 0.09 / 0.87 (its two variants 0.72 / 0.14 / 0.82
#   and 0.86 / 0.15 / 0.88). Plain shading was tried ("shipped", no lines) and lost here: parts of one colour that run
#   into each other cannot be seen without a line where they meet.
# - A hydrant that has every part its brief names (chain, caps, 20 bolts), "a named part is missing": 0.13 / 0.87 / 0.10;
#   intact designs flagged missing 0 / 3 / 0 of 24. Plain shading hides small parts that are there (a false alarm, which
#   an earlier run, r5/live/judge_wire.py, had read as the wireframe hiding a missing part).
# - Intact designs called faceted at 0.78 or more: 6 / 5 / 3 of 24 (a hydrant director that ships mostly smooth
#   0.72 / 0.71 / 0.30); the wireframe drew the triangulation of surfaces that ship smooth.
# Planted floating parts found 12 of 12 in every style; planted missing parts 9 / 12 / 9 of 12 (the three misses are one
# design whose remaining stencil plate reads as a handle, in every condition ever measured). Depiction spread (pooled sd)
# 0.081 / 0.101 / 0.101.
JUDGE_VIEW_STYLE = "shipped_edges"


@dataclass(frozen=True)
class JudgedDesign:
    """One design as the judge saw and scored it.

    ``result``: the stage's question set (with the brief's two questions when a brief was given). ``views``: (view
    name, uri), exactly the images the judge was shown, in order. ``author_views``: the same camera views with numbered
    badges, labelled for an authoring request ("front view of the current design (Image 1)"); ``legend``: badge -> node
    id of ``author_views``; ``legend_text``: that legend with the views each badge appears in."""

    result: VisualQAResult
    views: tuple[tuple[str, str], ...]
    author_views: tuple[tuple[str, str], ...]
    legend: dict[int, str]
    legend_text: str


@dataclass(frozen=True)
class DesignRenders:
    """The judge's views of one design as PNG bytes: ``plain`` (view name, png) and, when marked, ``marked`` (the same
    views with badges) with their legend."""

    plain: tuple[tuple[str, bytes], ...]
    marked: tuple[tuple[str, bytes], ...] = ()
    legend: dict[int, str] | None = None
    legend_text: str = ""


def _label(img: Image.Image, view: str) -> Image.Image:
    """The view's name in the top-left corner and a frame, as ``kernel.raster.contact_sheet`` draws each tile."""
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 8 * len(view) + 10, 14], fill=(0, 0, 0))
    d.text((5, 2), view, fill=(255, 255, 255))
    d.rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], outline=(70, 74, 84))
    return img


def _render(meshes: list[MeshData], view: View, size: int, style: str, normals: dict[str, np.ndarray] | None,
            crease_deg: float):
    if style not in VIEW_STYLES:
        raise ValueError(f"unknown view style {style!r}; one of {VIEW_STYLES}")
    if style == "wire":
        return render_buffers(meshes, view, size, wireframe=True)
    return render_buffers(meshes, view, size, normals=normals, feature_edges=style == "shipped_edges", crease_deg=crease_deg)


def shipped_normals(meshes: list[MeshData], crease_deg: float = CREASE_DEG) -> dict[str, np.ndarray]:
    """node id -> the per-corner normals the package writes for that part (``compile_ir.crease_normals``)."""
    return {m.node_id: crease_normals(np.asarray(m.points, np.float64), np.asarray(m.triangles), crease_deg)
            for m in meshes if len(m.triangles)}


def render_design(meshes: list[MeshData], *, views: Sequence[View] = JUDGE_VIEWS, size: int = JUDGE_VIEW_PX,
                  marked: bool = True, style: str | None = None, crease_deg: float = CREASE_DEG) -> DesignRenders:
    """Render each view once in ``style`` (None: ``JUDGE_VIEW_STYLE``) and, when ``marked``, draw its badged view over the
    same render. ``crease_deg``: the shading crease the package is written with (the art direction's). Badge numbers
    follow ``som.assign_badges`` (sorted node ids), so the legend is the same in every view and for every size."""
    style = style or JUDGE_VIEW_STYLE
    ids = [m.node_id for m in meshes if len(m.triangles)]
    # normals are keyed by node id (render_buffers): parts sharing an id are lit one normal per triangle instead
    normals = shipped_normals(meshes, crease_deg) if len(set(ids)) == len(ids) and style != "wire" else None
    legend = assign_badges(meshes) if marked else None
    plain: list[tuple[str, bytes]] = []
    badged: list[tuple[str, bytes]] = []
    visible: dict[str, list[int]] = {}
    seen: set[str] = set()
    for v in views:
        buf = _render(meshes, v, size, style, normals, crease_deg)
        plain.append((v, png_bytes(_label(Image.fromarray(buf.color), v))))
        if legend is not None:
            mv = mark_view(buf, legend, reserved=[(0, 0, 8 * len(v) + 10, 14)])
            badged.append((v, png_bytes(_label(mv.image, v))))
            visible[v] = [b.num for b in mv.badges]
            seen.update(b.node_id for b in mv.badges)
    if legend is None:
        return DesignRenders(tuple(plain))
    sheet = MarkedSheet(Image.new("RGB", (1, 1)), legend, visible, sorted(set(legend.values()) - seen))
    return DesignRenders(tuple(plain), tuple(badged), legend, sheet.legend_text())


async def _upload_all(uploader: Uploader, pngs: Sequence[tuple[str, bytes]]) -> tuple[tuple[str, str], ...]:
    uris = await asyncio.gather(*(uploader(b) for _, b in pngs))
    return tuple((name, uri) for (name, _), uri in zip(pngs, uris))


async def design_views(meshes: list[MeshData], uploader: Uploader, *, size: int = JUDGE_VIEW_PX, style: str | None = None,
                       crease_deg: float = CREASE_DEG) -> tuple[tuple[str, str], ...]:
    """The judge's plain views of a design, uploaded: what a ranking shows of one candidate (no badges: a ranking
    locates nothing)."""
    renders = await to_thread_drained(render_design, meshes, size=size, marked=False, style=style, crease_deg=crease_deg)
    return await _upload_all(uploader, renders.plain)


async def judge_design(plane: Any, meshes: list[MeshData], *, brief: str | None, governor: Any, uploader: Uploader,
                       stage: str = "blockout", ortho_uri: str | None = None, style_uri: str | None = None,
                       badges: bool | None = None, style: str | None = None, crease_deg: float = CREASE_DEG) -> JudgedDesign:
    """Render the design's views (off the event loop), upload them and ask the stage's visual QA about this design alone.

    ``brief=None`` (or empty) drops the brief's two questions, as before. Raises what ``plane.evaluate_visual_qa`` raises;
    a rendering or upload failure propagates. ``badges``: show the judge the badged views (None: ``JUDGE_SEES_BADGES``);
    for the measurement that decides that default. ``style``: how the judge's views are drawn (None: ``JUDGE_VIEW_STYLE``),
    for the measurement that decides that; ``crease_deg``: the shading crease the package is written with."""
    badges = JUDGE_SEES_BADGES if badges is None else badges
    renders = await to_thread_drained(render_design, meshes, style=style, crease_deg=crease_deg)
    uploaded = await _upload_all(uploader, [*renders.plain, *renders.marked])
    plain, badged = uploaded[:len(renders.plain)], uploaded[len(renders.plain):]
    shown = badged if badges else plain
    legend = renders.legend or {}
    result = await plane.evaluate_visual_qa(shown, ortho_uri, style_uri, stage, governor=governor,  # type: ignore[arg-type]
                                            **({"marked_legend": legend} if badges else {}),
                                            **({"brief": brief} if brief else {}))
    author = tuple((f"{name} view of the current design (Image {k})", uri) for k, (name, uri) in enumerate(badged, 1))
    return JudgedDesign(result, tuple(shown), author, dict(legend), renders.legend_text)
