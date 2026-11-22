"""What the vision judge is shown of a design's shape, in one place.

Every judgement of a design's shape (the geometry ranking, ``tactics.tournament.geometry_tournament``; pass A's blockout
review, ``tactics.pyramid``) shows the judge the same thing: ``JUDGE_VIEWS`` (front, right and top orthographic, a
perspective from the front left and one from the opposite corner), each its own ``JUDGE_VIEW_PX`` image at the high
image setting (``decisions.engine.VIEW_RESOLUTION``), and one design per request. Decided on the live measurement of
2026-11-22 (r5/live/judge_views.py, judge_truth.py; numbers at ``engine.VIEW_RESOLUTION``): against one sheet of four
256 px views at the low setting, separate views doubled the depiction gap between a plain and a detailed crate and
halved its spread, and let the judge report real interpenetrations that it missed on the sheet.

Each view is rendered once (``render_buffers``, with the wireframe overlay the sheet always carried); its badged image
(numbered part badges, ``kernel.som``, for an authoring request and for ``locate_defects``) is that image with the badges
drawn over it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Sequence

from PIL import Image, ImageDraw

from antigravity3d.decisions.models import VisualQAResult
from antigravity3d.kernel.raster import JUDGE_VIEWS, View, render_buffers
from antigravity3d.kernel.som import MarkedSheet, assign_badges, mark_view
from antigravity3d.kernel.types import MeshData
from antigravity3d.tactics.common import Uploader, png_bytes, to_thread_drained

JUDGE_VIEW_PX = 512
# Whether the judge is shown the badged views (then ``views`` and ``author_views`` are the same images). Off: every
# condition measured on 2026-11-22 was unmarked, and numbers drawn over the parts may cost the judge on depiction.
# The live measurement that settles it is r5/scratch_judge/live_judge_design.py (plain against badged, same designs).
JUDGE_SEES_BADGES = False


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


def render_design(meshes: list[MeshData], *, views: Sequence[View] = JUDGE_VIEWS, size: int = JUDGE_VIEW_PX,
                  marked: bool = True, wireframe: bool = True) -> DesignRenders:
    """Render each view once (clay with the wireframe overlay the sheet always carried) and, when ``marked``, draw its
    badged image from the same buffers. Badge numbers follow ``som.assign_badges`` (sorted node ids), so
    the legend is the same in every view and for every size."""
    legend = assign_badges(meshes) if marked else None
    plain: list[tuple[str, bytes]] = []
    badged: list[tuple[str, bytes]] = []
    visible: dict[str, list[int]] = {}
    seen: set[str] = set()
    for v in views:
        buf = render_buffers(meshes, v, size, wireframe=wireframe)
        plain.append((v, png_bytes(_label(Image.fromarray(buf.color), v))))
        if legend is not None:
            # the plain image with badges drawn over it: the same render, so the badged view differs from what the judge
            # is shown by the badges alone (kernel.som.marked_contact_sheet's views have no wireframe overlay)
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


async def design_views(meshes: list[MeshData], uploader: Uploader, *, size: int = JUDGE_VIEW_PX) -> tuple[tuple[str, str], ...]:
    """The judge's plain views of a design, uploaded: what a ranking shows of one candidate (no badges: a ranking
    locates nothing)."""
    renders = await to_thread_drained(render_design, meshes, size=size, marked=False)
    return await _upload_all(uploader, renders.plain)


async def judge_design(plane: Any, meshes: list[MeshData], *, brief: str | None, governor: Any, uploader: Uploader,
                       stage: str = "blockout", ortho_uri: str | None = None, style_uri: str | None = None,
                       badges: bool | None = None) -> JudgedDesign:
    """Render the design's views (off the event loop), upload them and ask the stage's visual QA about this design alone.

    ``brief=None`` (or empty) drops the brief's two questions, as before. Raises what ``plane.evaluate_visual_qa`` raises;
    a rendering or upload failure propagates. ``badges``: show the judge the badged views (None: ``JUDGE_SEES_BADGES``);
    for the measurement that decides that default."""
    badges = JUDGE_SEES_BADGES if badges is None else badges
    renders = await to_thread_drained(render_design, meshes)
    uploaded = await _upload_all(uploader, [*renders.plain, *renders.marked])
    plain, badged = uploaded[:len(renders.plain)], uploaded[len(renders.plain):]
    shown = badged if badges else plain
    legend = renders.legend or {}
    result = await plane.evaluate_visual_qa(shown, ortho_uri, style_uri, stage, governor=governor,  # type: ignore[arg-type]
                                            **({"marked_legend": legend} if badges else {}),
                                            **({"brief": brief} if brief else {}))
    author = tuple((f"{name} view of the current design (Image {k})", uri) for k, (name, uri) in enumerate(badged, 1))
    return JudgedDesign(result, tuple(shown), author, dict(legend), renders.legend_text)
