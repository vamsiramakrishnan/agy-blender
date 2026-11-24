"""check_site: open a built website the way a visitor does, take its pictures and measure it.

Run 1 (benchmarks/results/site_run1) showed why this exists: the agent wrote four throw-away Playwright scripts to see
its own site, looked only at six chapter stops, and never saw what lay between them (at 60 % of the scroll the canvas
showed an empty grid). Nothing measured the page's weight, its errors, the phone layout, reduced motion or the page
without WebGL. This module is those eyes, shared by the management tool ``check_site`` and ``agy3d check-site``.

Given a folder that holds a built site (``index.html``) or a localhost URL it:

1. serves the folder under a sub-path (a site that only works at the server root fails here as it would on a host that
   publishes it under ``/project/``), and opens it at a desktop and a phone size in the process's shared headless
   Chromium (``adapters/engines/web_browser.py``), each in a context of its own;
2. scrolls through it in even steps, waits at each step until the picture stops changing and keeps
   the frame; writes a labelled contact sheet per size;
3. at each step takes the WebGL canvases alone (the page's HTML hidden) and says where they show nothing but their
   background, and where the picture did not change from the step before;
4. records uncaught errors, console errors, failed requests, requests to other hosts, sideways overflow at phone width
   and the bytes downloaded by type;
5. reads what the scene costs from WebGL calls wrapped before the page's scripts run (``site_probe.js`` says how);
6. loads the page once with "reduce motion" set and once with WebGL unavailable;
7. checks every element that names its source with ``data-src`` against that source, and lists numbers and code shown
   with no source.

Everything is bounded: the whole check answers within ``ANSWER_S`` (the host ends a tool call at 180 s), with what it
has when time runs out, and says which parts are missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import re
import shutil
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import numpy as np

#: the whole check answers within this many seconds. Antigravity ends a tool call after 180 s (run 1, transcript rows 12
#: and 32); 150 s is the bound every long tool of this plugin answers within (``DELIVER_ANSWER_S``).
ANSWER_S = 150.0
#: the visits stop taking new pictures this long before ``ANSWER_S``: closing the contexts, the contact sheets and the
#: facts need the rest.
WRAP_UP_S = 12.0

#: The two sizes: a common laptop viewport, and a common phone's CSS viewport (390 x 844, iPhone 12 to 15). Both at a
#: device pixel ratio of 1: software WebGL draws every pixel on the CPU, and three times the pixels would cost three
#: times the wait without changing what the frames show about layout and composition.
SIZES: dict[str, dict[str, Any]] = {
    "desktop": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1},
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 1, "is_mobile": True, "has_touch": True},
}

#: A step's picture is "settled" when two frames 150 ms apart differ by less than this (mean absolute difference of the
#: 1/8-size grey image, share of full scale; JPEG noise of an unchanged picture is 0, a blinking caret is about 0.0002).
STILL = 0.0015
#: how long one step may take to settle; a page still moving after it is photographed as it is and marked "still moving"
SETTLE_CAP_S = 4.0
#: The longest time the page is shown to pass between two of its frames (``site_probe.js``, "The page's clock"): below
#: GSAP's 500 ms lag threshold, so scrubbed animations advance by real time instead of 33 ms a frame on a slow frame.
FRAME_CAP_MS = 250
#: Frame-to-frame change below which a picture that keeps changing at the same rate counts as the page's own continuous
#: animation (a turning prop, drifting particles) rather than the scroll still arriving; a camera cut is far above it.
AMBIENT = 0.03
#: Two steps "did not change at all" below this mean difference (1/4-size grey image): the scroll moved and nothing on
#: screen did, which a visitor reads as a frozen page.
UNCHANGED = 0.002
#: The 3D canvas "shows nothing but its background" when less than this share of it differs from its most common colour
#: by more than 8 % of full scale, measured on the 1/4-size frame (thin lines of a helper grid average out at that size,
#: an object does not). 1 % of a 1440 x 900 canvas is a 120 x 108 px patch.
EMPTY_CONTENT = 0.01
#: Download weight is reported as the wait on a 10 Mbit/s connection (a fair mobile link); it is called out when that
#: wait passes 5 s, the point where the page has had the visitor's whole attention span for a first impression.
LINK_BPS = 10e6 / 8
SLOW_WAIT_S = 5.0
#: Visits run at once. Software WebGL is bound by the CPU (4 cores here): the two sizes and the short no-WebGL visit side
#: by side, the reduced-motion visit when a lane frees. Measured on the film: 1 lane 67 s, 2 lanes 54 s, 4 lanes 40 s.
LANES = 3
VISIT_LABEL = {"reduced": "the visit with reduce motion set", "nowebgl": "the visit without WebGL"}
#: how many unsourced numbers and code specimens are listed (the total is always given)
UNSOURCED_LISTED = 6

_HERE = Path(__file__).resolve().parent
_log = logging.getLogger(__name__)
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}

_MIME = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
         ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".webp": "image/webp", ".avif": "image/avif", ".gif": "image/gif", ".ico": "image/x-icon",
         ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".bin": "application/octet-stream", ".ktx2": "image/ktx2",
         ".wasm": "application/wasm", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf",
         ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".txt": "text/plain; charset=utf-8",
         ".map": "application/json", ".hdr": "application/octet-stream", ".exr": "application/octet-stream",
         ".xml": "application/xml", ".webmanifest": "application/manifest+json"}

_KINDS = (("models", (".glb", ".gltf", ".bin", ".drc", ".usdz", ".fbx", ".obj")),
          ("textures", (".ktx2", ".hdr", ".exr", ".basis", ".dds")),
          ("images", (".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".svg", ".ico")),
          ("scripts", (".js", ".mjs", ".wasm")),
          ("styles", (".css",)),
          ("fonts", (".woff2", ".woff", ".ttf", ".otf")),
          ("media", (".mp4", ".webm", ".mp3", ".ogg", ".wav")),
          ("documents", (".html", ".htm")),
          ("data", (".json", ".txt", ".csv", ".xml", ".map")))
_TYPE_KIND = {"script": "scripts", "stylesheet": "styles", "font": "fonts", "image": "images", "media": "media",
              "document": "documents"}


def _kind(url: str, resource_type: str) -> str:
    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    for kind, exts in _KINDS:
        if ext in exts:
            return kind
    return _TYPE_KIND.get(resource_type, "other")


# --------------------------------------------------------------------------- the result


@dataclass
class Step:
    index: int
    share: float            # scroll position asked for, 0..1
    y: int                  # where the page actually was
    frame: str = ""         # path of the frame
    canvas_frame: str = ""  # path of the canvas-only frame ("" when the page has no WebGL canvas in view)
    settle_s: float = 0.0
    motion: str = "still"   # how the picture settled: still | ambient | moving (see _Visitor.settle)
    canvas_content: float | None = None  # share of the canvas showing something other than its background
    unchanged: bool = False
    draws: int = 0
    triangles: int = 0
    sideways_px: int = 0
    reached_by: str = "scroll"


@dataclass
class Visit:
    name: str
    load_s: float | None = None
    loaded: bool = False
    load_error: str = ""
    steps: list[Step] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)          # own-origin requests that failed (status or network)
    other_hosts: dict[str, int] = field(default_factory=dict)
    other_failed: dict[str, str] = field(default_factory=dict)  # host -> why a tried request to it failed
    requests: dict[str, dict] = field(default_factory=dict)  # url -> {kind, bytes, status}
    idle: dict | None = None
    memory: dict | None = None
    probe: dict | None = None
    overflow: list[str] = field(default_factory=list)
    facts_raw: dict | None = None
    text_chars: int | None = None
    frame: str = ""
    notes: list[str] = field(default_factory=list)
    reached_steps: int = 0


@dataclass
class SiteReport:
    target: str
    out_dir: Path
    seconds: float = 0.0
    visits: dict[str, Visit] = field(default_factory=dict)
    sheets: dict[str, str] = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    unfinished: list[str] = field(default_factory=list)
    error: str = ""
    steps: int = 9

    # ------------------------------------------------------------------ verdicts

    def broken(self) -> list[str]:
        out: list[str] = []
        d = self.visits.get("desktop")
        if self.error:
            return [self.error]
        for v in self.visits.values():
            if v.name in ("desktop", "phone") and not v.loaded:
                out.append(f"The page did not load at {v.name} size: {v.load_error or 'no load event'}.")
        errs = _merged(v.page_errors for k, v in self.visits.items() if k in ("desktop", "phone"))
        if errs:
            out.append(f"{len(errs)} uncaught error{_s(errs)} in the page: " + "; ".join(_q(e) for e in errs[:3]) + ".")
        cons = _merged(v.console_errors for k, v in self.visits.items() if k in ("desktop", "phone"))
        if cons:
            out.append(f"{len(cons)} console error{_s(cons)}: " + "; ".join(_q(e) for e in cons[:3]) + ".")
        failed = _merged(v.failed for k, v in self.visits.items() if k in ("desktop", "phone"))
        if failed:
            root = [f for f in failed if "at the server root" in f]
            out.append(f"{len(failed)} request{_s(failed)} for the site's own files failed: " + "; ".join(failed[:4]) + "."
                       + (" A path that starts with / asks the server root; served under a sub-path (as on most hosts "
                          "that publish a project page) it is missing. Use relative paths (Vite: base: './')." if root else ""))
        if d and d.loaded and d.probe is not None and not d.probe.get("contexts") and not d.probe.get("webgpu") and not d.probe.get("offscreen"):
            out.append("The page never created a WebGL context: there is no 3D on it.")
        for m in self.facts.get("mismatches", []):
            out.append(m)
        return out

    def noticed(self) -> list[str]:
        out: list[str] = []
        for name in ("desktop", "phone"):
            v = self.visits.get(name)
            if not v or not v.steps:
                continue
            empty = [s for s in v.steps if s.canvas_content is not None and s.canvas_content < EMPTY_CONTENT]
            if empty:
                out.append(f"{name.capitalize()}: the 3D canvas shows nothing but its background at "
                           + _steps_list(empty, self.steps) + ".")
            same = [s for s in v.steps if s.unchanged]
            if same:
                out.append(f"{name.capitalize()}: the picture did not change at all from the step before at "
                           + _steps_list(same, self.steps) + ".")
            moving = [s for s in v.steps if s.motion == "moving"]
            if moving:
                out.append(f"{name.capitalize()}: still moving {SETTLE_CAP_S:.0f} s after the scroll stopped at "
                           + _steps_list(moving, self.steps) + " (photographed as it was).")
        p = self.visits.get("phone")
        if p and p.steps:
            wide = [s for s in p.steps if s.sideways_px > 1]
            if wide:
                worst = max(s.sideways_px for s in wide)
                out.append(f"Phone ({SIZES['phone']['viewport']['width']} px): the page scrolls sideways by up to {worst} px"
                           + (f"; widest: {', '.join(p.overflow[:3])}" if p.overflow else "") + ".")
        total = self.total_bytes()
        if total and total / LINK_BPS > SLOW_WAIT_S:
            out.append(f"The page downloads {_mb(total)}: about {total / LINK_BPS:.0f} s on a 10 Mbit/s connection "
                       f"before everything has arrived.")
        hosts = self.other_hosts()
        if hosts:
            out.append(f"It asks {len(hosts)} other host{_s(hosts)} ({', '.join(sorted(hosts))}): " + self._other_host_note() + ".")
        r = self.reduced_line()
        if r and not r[1]:
            out.append(r[0])
        w = self.nowebgl_line()
        if w and not w[1]:
            out.append(w[0])
        return out

    def _other_host_note(self) -> str:
        tried = {}
        for v in self.visits.values():
            tried.update(v.other_failed)
        if self.facts.get("other_hosts_mode") == "try":
            if tried:
                return ("tried, and from this machine " + "; ".join(f"{h}: {why}" for h, why in list(tried.items())[:2])
                        + " (a request this machine cannot make is not a fault of the site)")
            return "a visitor's browser depends on them"
        return ("not requested by this check (other_hosts='try' makes them); the page was seen as a visitor sees it when "
                "those hosts are unreachable")

    # ------------------------------------------------------------------ measures

    def other_hosts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.visits.values():
            if v.name in ("desktop", "phone"):
                for h, n in v.other_hosts.items():
                    out[h] = out.get(h, 0) + n
        return out

    def total_bytes(self) -> int:
        d = self.visits.get("desktop")
        return sum(r["bytes"] for r in d.requests.values()) if d else 0

    def bytes_line(self) -> str:
        d = self.visits.get("desktop")
        if not d or not d.requests:
            return ""
        by: dict[str, int] = {}
        for r in d.requests.values():
            by[r["kind"]] = by.get(r["kind"], 0) + r["bytes"]
        kinds = ", ".join(f"{k} {_mb(b)}" for k, b in sorted(by.items(), key=lambda kv: -kv[1]) if b)
        top = sorted(d.requests.items(), key=lambda kv: -kv[1]["bytes"])[:5]
        tops = ", ".join(f"{_short(u, d)} {_mb(r['bytes'])}" for u, r in top)
        return (f"Downloaded (desktop, whole scroll): {_mb(self.total_bytes())} in {len(d.requests)} files: {kinds}. "
                f"Largest: {tops}.")

    def scene_line(self) -> list[str]:
        d = self.visits.get("desktop")
        if not d or d.probe is None:
            return []
        p = d.probe
        if not p.get("contexts"):
            extra = " It asked for WebGPU, which this check cannot measure." if p.get("webgpu") else ""
            extra += " It draws in an OffscreenCanvas (a worker), which this check cannot measure." if p.get("offscreen") else ""
            return [f"Scene: no WebGL context.{extra}"]
        steps = [s for s in d.steps]
        md = max((s.draws for s in steps), default=0)
        mt = max((s.triangles for s in steps), default=0)
        at = max(steps, key=lambda s: s.triangles) if steps else None
        m = d.memory or {}
        lines = [f"Scene (desktop, the largest over the steps): {md:,} draw calls and {mt:,} triangles in one frame"
                 + (f" (most triangles at {at.share:.0%})" if at and mt else "")
                 + f"; textures {_mb(m.get('texture_bytes', 0))} on the graphics card in {m.get('textures', 0)} textures"
                 + (f" ({_mb(m.get('texture_bytes', 0) - m.get('uploaded_bytes', 0))} of it render targets)"
                    if m.get('texture_bytes', 0) - m.get('uploaded_bytes', 0) > 0 else "")
                 + (f", plus {_mb(m['renderbuffer_bytes'])} of render buffers" if m.get("renderbuffer_bytes") else "")
                 + ". Counted by wrapping the page's WebGL draw and texture calls before its scripts ran."]
        idle = d.idle
        if idle:
            if idle["frames"] == 0:
                lines.append("While nothing moves: the page ran no animation frames.")
            else:
                lines.append(f"While nothing moves (top of the page, {idle['seconds']:.1f} s, no input): it drew in "
                             f"{idle['drawn']} of {idle['frames']} frames, and the picture "
                             + ("changed" if idle["changed"] else "stayed the same")
                             + (" (it keeps drawing an unchanging picture: that is battery and fan spent on nothing; "
                                "render on change, or stop the loop when the scene is still)"
                                if idle["drawn"] >= 0.8 * idle["frames"] and not idle["changed"] else "") + ".")
        return lines

    def reduced_line(self) -> tuple[str, bool] | None:
        """(the line, whether the page handles it)."""
        r, d = self.visits.get("reduced"), self.visits.get("desktop")
        if not r:
            return None
        if not r.loaded:
            return (f"With reduce motion set: the page did not load ({r.load_error}).", False)
        asked = bool(r.probe and (r.probe.get("reducedMotionQueried") or r.probe.get("reducedMotionCss")))
        moving = bool(r.idle and r.idle.get("changed"))
        normal_moving = bool(d and d.idle and d.idle.get("changed"))
        r_step = r.steps[0] if r.steps else None
        d_step = None
        if r_step and d and d.steps:
            d_step = min(d.steps, key=lambda s: abs(s.share - r_step.share))
        glide = ""
        if r_step and d_step:
            glide = (f"; after a scroll the picture settles in {r_step.settle_s:.1f} s (without the setting "
                     f"{d_step.settle_s:.1f} s)")
        how = []
        if r.probe and r.probe.get("reducedMotionQueried"):
            how.append("asks matchMedia")
        if r.probe and r.probe.get("reducedMotionCss"):
            how.append("has a CSS rule")
        if not asked:
            return (f"With reduce motion set the page never asks for it (no matchMedia query, no CSS rule for "
                    f"prefers-reduced-motion): a visitor who asked for less motion gets all of it"
                    + (", including movement while nothing is touched" if moving else "") + glide + ".", False)
        return (f"With reduce motion set the page {' and '.join(how)}; while nothing is touched the picture "
                + ("still changes" if moving else "stays still")
                + (" (without the setting it changes)" if normal_moving and not moving else "") + glide
                + f". Picture: {_rel(r.frame, self.out_dir)}.", not moving)

    def nowebgl_line(self) -> tuple[str, bool] | None:
        w, d = self.visits.get("nowebgl"), self.visits.get("desktop")
        if not w:
            return None
        if not w.loaded:
            return (f"Without WebGL: the page did not load ({w.load_error}).", False)
        errs = _merged([w.page_errors])
        said = bool(w.probe and w.probe.get("fallbackMessage"))
        chars = w.text_chars or 0
        dchars = (d.text_chars if d else None) or 0
        text = (f"shows {chars:,} characters of text" + (f" (with WebGL {dchars:,})" if dchars else ""))
        ok = said and not errs and chars > 0.5 * max(dchars, 1)
        return (f"Without WebGL: {len(errs)} uncaught error{_s(errs)}" + (f" ({_q(errs[0])})" if errs else "")
                + f"; the page {text}, and "
                + (f"a visible message says so ({_q(w.probe['fallbackMessage'])})" if said else
                   "nothing visible tells the visitor the 3D is missing")
                + f". Picture: {_rel(w.frame, self.out_dir)}.", ok)

    def facts_lines(self) -> list[str]:
        f = self.facts
        if not f:
            return []
        lines = []
        n, bad = f.get("checked", 0), len(f.get("mismatches", []))
        where = f" against {f['sources']}" if f.get("sources") else ""
        if n:
            lines.append(f"Facts: {n} element{_s(n)} name{'' if n != 1 else 's'} a source (data-src){where}; "
                         + (f"{bad} do{'es' if bad == 1 else ''} not match it (listed above)." if bad else "all match."))
        else:
            lines.append("Facts: no element names its source (data-src=\"file#/json/pointer\", \"file#L10-L14\" or \"file\").")
        un_n, un_c = f.get("unsourced_numbers", []), f.get("unsourced_code", [])
        if un_n or un_c:
            parts = []
            if un_c:
                parts.append(f"{len(un_c)} code specimen{_s(un_c)} (" + "; ".join(_q(c, 60) for c in un_c[:3]) + ")")
            if un_n:
                parts.append(f"{len(un_n)} number{_s(un_n)} (" + "; ".join(_q(c, 50) for c in un_n[:UNSOURCED_LISTED]) + ")")
            lines.append("Shown without a source: " + " and ".join(parts)
                         + ". Mark each with data-src, or take it out if no file holds it.")
        return lines

    # ------------------------------------------------------------------ the answer

    def text(self) -> str:
        broken, noticed = self.broken(), self.noticed()
        head = (f"SITE CHECKED in {self.seconds:.0f} s: "
                + (f"{len(broken)} thing{_s(broken)} broken" if broken else "nothing broken")
                + f", {len(noticed)} thing{_s(noticed)} a visitor would notice. "
                + ("Open the contact sheets below with your file viewer and look at every frame before you change anything."
                   if self.sheets else ""))
        lines = [head]
        if self.unfinished:
            lines.append("Not finished within the time limit (call again to see the rest; nothing needs fixing because of "
                         "it): " + "; ".join(self.unfinished) + ".")
        if broken:
            lines.append("")
            lines.append("Broken:")
            lines += [f"- {b}" for b in broken]
        if noticed:
            lines.append("")
            lines.append("A visitor would notice:")
            lines += [f"- {b}" for b in noticed]
        lines.append("")
        lines.append("Measures:")
        for name in ("desktop", "phone"):
            v = self.visits.get(name)
            if v and v.loaded:
                vp = SIZES[name]["viewport"]
                lines.append(f"- {name.capitalize()} {vp['width']}x{vp['height']}: settled {v.load_s:.1f} s after the page was "
                             f"asked for; {len(v.steps)} of {self.steps} steps photographed; 3D content per step "
                             + " ".join("-" if s.canvas_content is None else f"{s.canvas_content:.0%}" for s in v.steps) + ".")
        for line in [self.bytes_line(), *self.scene_line()]:
            if line:
                lines.append(f"- {line}")
        for r in (self.reduced_line(), self.nowebgl_line()):
            if r:
                lines.append(f"- {r[0]}")
        lines += [f"- {x}" for x in self.facts_lines()]
        lines.append("")
        lines.append("Pictures:")
        for name, sheet in self.sheets.items():
            v = self.visits[name]
            frames = [s.frame for s in v.steps if s.frame]
            lines.append(f"- {name} contact sheet: {sheet}" + (f" (frames {_rel(frames[0], self.out_dir)} to "
                                                             f"{_rel(frames[-1], self.out_dir)})" if frames else ""))
        lines.append(f"- all frames, the canvas-only frames and the full record (report.json) are in {self.out_dir}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        def visit(v: Visit) -> dict:
            out = {k: getattr(v, k) for k in ("name", "load_s", "loaded", "load_error", "page_errors", "console_errors", "failed",
                                              "other_hosts", "other_failed", "idle", "memory", "overflow", "text_chars", "frame",
                                              "notes")}
            out["steps"] = [s.__dict__ for s in v.steps]
            out["requests"] = v.requests
            out["probe"] = v.probe
            return out

        return {"target": self.target, "seconds": round(self.seconds, 1), "broken": self.broken(), "noticed": self.noticed(),
                "unfinished": self.unfinished, "sheets": self.sheets, "facts": self.facts,
                "visits": {k: visit(v) for k, v in self.visits.items()}, "total_bytes": self.total_bytes()}


def _s(n) -> str:
    n = len(n) if not isinstance(n, int) else n
    return "" if n == 1 else "s"


def _q(text: str, n: int = 120) -> str:
    t = " ".join(str(text).split())
    return f"'{t[:n]}{'...' if len(t) > n else ''}'"


def _mb(b: float) -> str:
    return f"{b / 1e6:.1f} MB" if b >= 1e5 else f"{b / 1e3:.0f} kB"


def _merged(lists) -> list[str]:
    seen: list[str] = []
    for lst in lists:
        for x in lst:
            if x not in seen:
                seen.append(x)
    return seen


def _steps_list(steps: list[Step], n: int) -> str:
    return ", ".join(f"step {s.index + 1} of {n} ({s.share:.0%})" for s in steps)


def _rel(p: str, base: Path) -> str:
    if not p:
        return "(none)"
    with contextlib.suppress(ValueError):
        return str(Path(p).relative_to(base))
    return p


def _short(url: str, v: Visit) -> str:
    base = (v.probe or {}).get("base") or ""
    return url[len(base):] if base and url.startswith(base) else urlsplit(url).path.rsplit("/", 1)[-1] or url


# --------------------------------------------------------------------------- serving a folder


class _SiteServer:
    """Serves one site folder on 127.0.0.1 under a random sub-path, GET and HEAD only.

    A request outside the sub-path is answered 404 and recorded as "at the server root": that is the request a site
    built for ``/`` makes when it is published at ``/project/`` (GitHub Pages, most static hosts' preview links)."""

    def __init__(self, root: Path) -> None:
        import http.server
        import secrets

        self.root = root.resolve()
        self.prefix = f"/site-{secrets.token_hex(6)}/"
        self.missing: list[str] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:  # the method name http.server dispatches a HEAD to
                self._answer(body=False)

            def do_GET(self) -> None:  # the method name http.server dispatches a GET to
                self._answer(body=True)

            def _answer(self, body: bool) -> None:
                path = self.path.split("?")[0].split("#")[0]
                if not path.startswith(server.prefix):
                    server.missing.append(f"{unquote(path)} (asked at the server root)")
                    self.send_error(404)
                    return
                rel = unquote(path[len(server.prefix):])
                f = (server.root / rel).resolve()
                if f.is_dir():
                    f = f / "index.html"
                if not f.is_file() or not f.is_relative_to(server.root):
                    server.missing.append(f"{rel or '/'} (not in the folder)")
                    self.send_error(404)
                    return
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", _MIME.get(f.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if body:
                    with contextlib.suppress(ConnectionError):
                        self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self.url = self.origin + self.prefix
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="agy3d-site", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


# --------------------------------------------------------------------------- pictures


def _grey(jpeg: bytes, factor: int) -> np.ndarray:
    from PIL import Image

    im = Image.open(io.BytesIO(jpeg)).convert("L")
    im = im.reduce(factor) if factor > 1 else im
    return np.asarray(im, dtype=np.float32) / 255.0


def _diff(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 1.0
    return float(np.abs(a - b).mean())


def canvas_content(jpeg: bytes) -> float:
    """Share of a canvas-only frame that shows something other than its background: pixels of the 1/4-size image that
    differ from the frame's most common colour (quantised to 16 levels per channel) by more than 8 % of full scale in
    any channel. Averaging to 1/4 size first lets a 1 px helper grid fade into the background; an object stays."""
    from PIL import Image

    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im = im.reduce(4) if min(im.size) >= 64 else im
    a = np.asarray(im, dtype=np.int16)
    q = (a // 16).reshape(-1, 3)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    mode = np.bincount(keys).argmax()
    bg = a.reshape(-1, 3)[keys == mode].mean(axis=0)
    far = (np.abs(a.reshape(-1, 3) - bg) > 0.08 * 255).any(axis=1)
    return float(far.mean())


def contact_sheet(frames: list[tuple[str, str, bool]], out: Path, thumb_w: int, cols: int) -> None:
    """One labelled sheet: (path, label, flagged) per frame; flagged labels are drawn in red."""
    from PIL import Image, ImageDraw, ImageFont

    ims = [Image.open(p).convert("RGB") for p, _, _ in frames]
    if not ims:
        return
    w0, h0 = ims[0].size
    th = round(h0 * thumb_w / w0)
    band, pad = 22, 6
    rows = math.ceil(len(ims) / cols)
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, rows * (th + band + pad) + pad), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:  # Pillow before 10.1 has one bitmap size
        font = ImageFont.load_default()
    for i, (im, (_, label, flagged)) in enumerate(zip(ims, frames)):
        x, y = pad + (i % cols) * (thumb_w + pad), pad + (i // cols) * (th + band + pad)
        sheet.paste(im.resize((thumb_w, th), Image.LANCZOS), (x, y))
        draw.text((x + 2, y + th + 3), label, fill=(255, 110, 100) if flagged else (230, 230, 230), font=font)
    sheet.save(out, quality=85)


# --------------------------------------------------------------------------- facts


_UNIT = (r"%|ms|s|sec|min|h|hrs?|kB|KB|MB|GB|TB|B|px|fps|k|K|M|×|x|tokens?|lines?|bytes?|files?|calls?|tris|triangles|"
         r"draws?|steps?|runs?|turns?")
_NUM = re.compile(r"(?<![\w.,/#-])(\d{1,3}(?:[,   ]\d{3})+|\d+)(\.\d+)?(?:\s?(" + _UNIT + r"))?(?![\w])")


def _numbers(text: str) -> list[tuple[float, int, str, str]]:
    """(value, decimals, unit, as written) of each number in ``text``."""
    out = []
    for m in _NUM.finditer(text):
        whole = re.sub(r"[,   ]", "", m.group(1))
        frac = m.group(2) or ""
        out.append((float(whole + frac), max(len(frac) - 1, 0), m.group(3) or "", m.group(0)))
    return out


_MULT = {"k": 1e3, "K": 1e3, "M": 1e6, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def _number_matches(value: float, shown: list[tuple[float, int, str, str]]) -> bool:
    """A shown number matches a source value when it is that value written at the shown precision, with a k/M/kB/MB
    multiplier or as a percentage of a fraction."""
    for v, dec, unit, _ in shown:
        tol = 0.5 * 10 ** -dec + 1e-9
        for scale in (1.0, _MULT.get(unit, 1.0)):
            if abs(v * scale - value) <= tol * scale:
                return True
        if unit == "%" and abs(v - value * 100) <= tol:
            return True
    return False


def _norm(text: str) -> str:
    return " ".join(text.split())


def _json_pointer(doc: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        key = unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            cur = cur[key]
        else:
            raise KeyError(key)
    return cur


def check_fact(src: str, shown: str, sources: Path | None, is_code: bool = False) -> str | None:
    """None when the element agrees with its source, else why not (one sentence)."""
    if sources is None:
        return f"data-src=\"{src}\" cannot be checked: no sources folder was given"
    path, _, frag = src.partition("#")
    if re.match(r"^[a-z][a-z0-9+.-]*:", path, re.I) or path.startswith("/") or not path:
        return f"data-src=\"{src}\" names no file inside the sources folder (give a path relative to it)"
    f = (sources / unquote(path)).resolve()
    if not f.is_relative_to(sources.resolve()):
        return f"data-src=\"{src}\" points outside the sources folder"
    if not f.is_file():
        return f"data-src=\"{src}\": there is no {path} in {sources}"
    try:
        body = f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        return f"data-src=\"{src}\": {path} cannot be read as text ({type(e).__name__})"
    shown_n = _norm(shown)
    if frag.startswith("/") or frag == "":
        if frag.startswith("/"):
            try:
                value = _json_pointer(json.loads(body), frag)
            except (ValueError, KeyError, IndexError) as e:
                return f"data-src=\"{src}\": {path} has nothing at {frag} ({type(e).__name__})"
            if isinstance(value, bool) or value is None:
                ok = json.dumps(value) in shown_n.lower() or str(value) in shown_n
            elif isinstance(value, (int, float)):
                ok = _number_matches(float(value), _numbers(shown))
            elif isinstance(value, str):
                ok = _norm(value) in shown_n or shown_n in _norm(value) and len(shown_n) >= 0.5 * len(_norm(value))
            else:
                return f"data-src=\"{src}\" points at a {type(value).__name__} in {path}; point at the one value the element shows"
            if not ok:
                return f"the page shows {_q(shown, 60)} where {path}#{frag} holds {_q(json.dumps(value), 60)}"
            return None
        # a quote: it must occur in the file
        if shown_n and shown_n in _norm(body):
            return None
        nums = _numbers(shown)
        if nums and all(any(_number_matches(v, [(n, d, u, w)]) for v in (float(x) for x in re.findall(r"\d+(?:\.\d+)?", body.replace(",", ""))))
                        for n, d, u, w in nums[:5]) and len(shown_n) < 40:
            return None
        return f"the page shows {_q(shown, 60)}, which does not occur in {path}"
    m = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", frag)
    if not m:
        return (f"data-src=\"{src}\": the part after # must be a JSON pointer (#/key/0) or lines (#L10 or #L10-L14)")
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    lines = body.splitlines()
    if a < 1 or b < a or b > len(lines):
        return f"data-src=\"{src}\": {path} has {len(lines)} lines"
    want = textwrap.dedent("\n".join(lines[a - 1:b])).strip("\n").splitlines()
    got = textwrap.dedent(shown.strip("\n")).splitlines()
    want = [x.rstrip() for x in want]
    got = [x.rstrip() for x in got]
    if got == want:
        return None
    if len(got) == 1 and _norm(got[0]) and _norm(got[0]) in _norm("\n".join(want)):
        return None
    i = next((i for i, (x, y) in enumerate(zip(got, want)) if _norm(x) != _norm(y)), min(len(got), len(want)))
    if i == min(len(got), len(want)) and len(got) == len(want):
        return None  # only indentation differs
    return (f"the specimen at {path}#{frag} differs from the file at line {a + i}: the page shows "
            f"{_q(got[i] if i < len(got) else '<nothing>', 60)}, the file has {_q(want[i] if i < len(want) else '<nothing>', 60)}")


def _years(text: str) -> bool:
    return bool(re.fullmatch(r"(19|20)\d\d", text.strip()))


def judge_facts(raw: dict | None, sources: Path | None) -> dict:
    """Mismatches of the sourced elements and the unsourced numbers and code, from what the page extraction returned."""
    if not raw:
        return {}
    mismatches = []
    seen: set[tuple[str, str]] = set()
    for el in raw.get("sourced", []):
        key = (el["src"], el["text"])
        if key in seen:
            continue
        seen.add(key)
        why = check_fact(el["src"], el["text"], sources, el.get("code", False))
        if why:
            mismatches.append(f"Fact {el['sel']}: {why}.")
    numbers: list[str] = []
    for node in raw.get("texts", []):
        for v, dec, unit, written in _numbers(node["text"]):
            digits = len(re.sub(r"\D", "", written.split(".")[0]))
            if (digits >= 3 or unit) and not (not unit and _years(written)):
                ctx = _norm(node["text"])
                numbers.append(f"{written.strip()} in {ctx[:50]}" if len(ctx) > len(written.strip()) + 2 else written.strip())
    numbers = list(dict.fromkeys(numbers))
    code = [_norm(c) for c in raw.get("code", [])]
    return {"checked": len(seen), "mismatches": mismatches, "unsourced_numbers": numbers, "unsourced_code": code,
            "sources": str(sources) if sources else ""}


_FACTS_JS = r"""() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.01 || el.closest('[data-src]'); };
  const sel = (el) => { const p = []; for (let e = el; e && e !== document.body && p.length < 3; e = e.parentElement) {
    let s = e.tagName.toLowerCase(); if (e.id) { p.unshift(s + '#' + e.id); break; }
    const c = [...e.classList].slice(0, 2).join('.'); if (c) s += '.' + c; p.unshift(s); } return p.join(' > '); };
  const sourced = [...document.querySelectorAll('[data-src]')].slice(0, 500).map((el) => ({
    src: el.getAttribute('data-src'), text: el.textContent, sel: sel(el),
    code: /^(PRE|CODE)$/.test(el.tagName) || !!el.closest('pre') || !!el.querySelector('pre') }));
  const skip = 'script,style,noscript,template,head,title,[data-src],[aria-hidden="true"] svg';
  const code = [...document.querySelectorAll('pre, code')].filter((el) => !el.parentElement.closest('pre, code')
      && !el.closest('[data-src]') && !el.querySelector('[data-src]') && visible(el)
      && (el.tagName === 'PRE' || el.textContent.includes('\n') || el.textContent.trim().length > 60))
    .slice(0, 50).map((el) => el.textContent.trim().split('\n').slice(0, 2).join(' / '));
  const texts = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n && texts.length < 3000; n = w.nextNode()) {
    const el = n.parentElement; if (!el || !/\d/.test(n.textContent)) continue;
    if (el.closest(skip) || el.closest('pre, code, time') || !visible(el)) continue;
    texts.push({ text: n.textContent, sel: sel(el) });
  }
  return { sourced, code, texts };
}"""


# --------------------------------------------------------------------------- driving the browser

_PAGE_INFO_JS = r"""() => {
  const S = window.__agySite || {};
  let css = false;
  for (const sh of document.styleSheets) { try { for (const r of sh.cssRules) { if (/prefers-reduced-motion/.test(r.conditionText || (r.media && r.media.mediaText) || '')) { css = true; break; } } } catch (e) {} if (css) break; }
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.05; };
  let fallback = null;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length || !vis(el)) continue;
    const t = el.textContent.trim();
    if (t.length < 200 && /webgl|3d|graphics|hardware acceleration|your browser/i.test(t)) { fallback = t; break; }
  }
  return { contexts: (S.contexts || []).length, webgl2: (S.contexts || []).some((c) => window.WebGL2RenderingContext && c instanceof WebGL2RenderingContext),
           webgpu: !!S.webgpu, offscreen: !!S.offscreen, reducedMotionQueried: !!S.reducedMotionQueried, reducedMotionCss: css,
           memory: S.memory ? S.memory() : null, textChars: (document.body && document.body.innerText || '').length,
           fallbackMessage: fallback, base: location.href.replace(/[^/]*([?#].*)?$/, '') };
}"""








@dataclass
class _Clock:
    t0: float
    budget: float

    def left(self) -> float:
        return self.budget - (time.monotonic() - self.t0)


class _Visitor:
    """One visit: one browser context at one size, its own listeners and its own record."""

    def __init__(self, name: str, url: str, own_origin: str, out_dir: Path, clock: _Clock, other_hosts: str) -> None:
        self.v = Visit(name)
        self.url, self.own, self.out, self.clock, self.mode = url, own_origin, out_dir, clock, other_hosts
        self.inflight = 0
        self.last_net = time.monotonic()
        self._sizes: list[asyncio.Future] = []
        self.cdp = None

    def log(self, what: str) -> None:
        _log.debug("[%6.1f] %s: %s", time.monotonic() - self.clock.t0, self.v.name, what)

    # ---- listeners
    async def gate(self, route) -> None:
        url = route.request.url
        if url.startswith(self.own + "/") or url == self.own:
            await route.continue_()
            return
        host = urlsplit(url).hostname or url[:40]
        self.v.other_hosts[host] = self.v.other_hosts.get(host, 0) + 1
        if self.mode == "try":
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def attach(self, ctx, page) -> None:
        v = self

        def on_request(_r):
            v.inflight += 1
            v.last_net = time.monotonic()

        def on_done(r, failed: bool):
            v.inflight = max(0, v.inflight - 1)
            v.last_net = time.monotonic()
            url = r.url
            if url.startswith("data:") or url.startswith("blob:"):
                return
            own = url.startswith(v.own)
            if failed:
                why = r.failure or "failed"
                if own:
                    v.v.failed.append(f"{_short_url(url, v.url)} ({why})")
                elif "BLOCKED_BY_CLIENT" not in why:
                    v.v.other_failed.setdefault(urlsplit(url).hostname or url[:40], why)
                return
            v._sizes.append(asyncio.ensure_future(_record(v.v, r, own, v.url)))

        page.on("request", on_request)
        page.on("requestfinished", lambda r: on_done(r, False))
        page.on("requestfailed", lambda r: on_done(r, True))
        page.on("pageerror", lambda e: v.v.page_errors.append(str(e).split("\n")[0][:300]))
        page.on("console", lambda m: m.type == "error" and not _is_request_echo(m.text)
                and v.v.console_errors.append(m.text.split("\n")[0][:300]))
        # Frames are taken through DevTools directly: Playwright's screenshot waits for extra frames and restyles the page
        # (it hides the caret), which on run 1's page cost 2.5 s a frame against 0.9 s for this.
        self.cdp = await ctx.new_cdp_session(page)

    async def flush(self) -> None:
        if self._sizes:
            await asyncio.gather(*self._sizes, return_exceptions=True)

    async def shot(self, clip: dict | None = None) -> bytes:
        """The viewport as JPEG; ``clip`` (viewport coordinates) crops it afterwards (DevTools' own clip is in page
        coordinates and would need the scroll offset of the moment)."""
        import base64

        params: dict[str, Any] = {"format": "jpeg", "quality": 80, "optimizeForSpeed": True}
        data = base64.b64decode((await self.cdp.send("Page.captureScreenshot", params))["data"])
        if not clip:
            return data
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        box = (round(clip["x"]), round(clip["y"]), round(clip["x"] + clip["width"]), round(clip["y"] + clip["height"]))
        buf = io.BytesIO()
        im.crop(box).save(buf, "JPEG", quality=80)
        return buf.getvalue()

    # ---- waiting
    async def settle(self, cap: float) -> tuple[bytes, float, str]:
        """Take frames until the picture stops changing. Returns (last frame, seconds, how): 'still' (two frames agree),
        'ambient' (after a second the change from frame to frame stopped shrinking: what the scroll set in motion is
        over and what remains is the page's own continuous animation), or 'moving' (still changing at the cap)."""
        t0 = time.monotonic()
        cap = max(0.5, min(cap, self.clock.left() - WRAP_UP_S))
        prev, last_d, shot = None, None, b""
        while True:
            shot = await self.shot()
            small = _grey(shot, 8)
            el = time.monotonic() - t0
            if prev is not None:
                d = _diff(prev, small)
                self.log(f"  frame change {d:.4f} at {el:.1f} s")
                if d < STILL:
                    return shot, el, "still"
                if last_d is not None and el >= 1.0 and d >= 0.7 * last_d and d < AMBIENT:
                    return shot, el, "ambient"
                last_d = d
            if el > cap:
                return shot, el, "moving"
            prev = small
            await asyncio.sleep(0.1)

    async def network_quiet(self, cap: float) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < cap and self.clock.left() > WRAP_UP_S:
            if self.inflight == 0 and time.monotonic() - self.last_net > 0.5:
                return
            await asyncio.sleep(0.1)

    async def load(self, page) -> bool:
        t0 = time.monotonic()
        try:
            await page.goto(self.url, wait_until="load", timeout=max(5.0, min(60.0, self.clock.left() - WRAP_UP_S)) * 1000)
        except Exception as e:  # noqa: BLE001  the reason goes in the answer
            self.v.load_error = str(e).split("\n")[0][:200]
            return False
        self.log("load event")
        await self.network_quiet(30.0)
        with contextlib.suppress(Exception):
            await page.evaluate("document.fonts && document.fonts.ready")
        await self.settle(4.0)
        self.v.load_s = round(time.monotonic() - t0, 2)
        self.v.loaded = True
        self.log("settled after loading")
        return True

    async def idle(self, page, seconds: float = 1.5) -> dict:
        a = await self.shot()
        await page.evaluate("window.__agySite && window.__agySite.mark()")
        await asyncio.sleep(seconds)
        w = await page.evaluate("window.__agySite ? window.__agySite.mark() : null") or {"frames": 0, "drawn": 0}
        b = await self.shot()
        return {"seconds": seconds, "frames": w["frames"], "drawn": w["drawn"], "changed": _diff(_grey(a, 8), _grey(b, 8)) >= STILL}

    async def info(self, page) -> dict:
        return await page.evaluate(_PAGE_INFO_JS)

    # ---- steps
    async def scroll_to(self, page, target: int) -> str:
        """Put the page at ``target`` px and say how it got there.

        The position is set as a scrollbar drag or a link sets it (one native scroll event; scroll libraries such as
        Lenis and ScrollTrigger follow native scroll events), not by a train of wheel events: on software WebGL every
        wheel event of a heavy page waits for a frame, and the film took 5 to 20 s per step that way. A page that undoes
        a native scroll (it moves only on wheel input) gets the wheel instead."""
        y = await page.evaluate("(t) => window.__agySite.scrollTo(t)", target)
        if abs(y - target) <= 4:
            return "scroll"
        await asyncio.sleep(0.1)
        y = await page.evaluate("window.__agySite.y()")
        if abs(y - target) <= 4:
            return "scroll"
        vp = page.viewport_size or {"width": 800, "height": 600}
        await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(page.mouse.wheel(0, target - y), 5.0)
        return "wheel"

    async def steps(self, page, n: int, canvas: bool, sideways: bool) -> None:
        geo = await page.evaluate("window.__agySite.geometry()")
        if geo["inner"]:
            self.v.notes.append("the page scrolls inside an element, not the window")
        for i in step_order(n):
            if self.clock.left() < WRAP_UP_S + 2.0:
                break
            share = i / max(n - 1, 1)
            how = await self.scroll_to(page, round(geo["max"] * share))
            shot, secs, motion = await self.settle(SETTLE_CAP_S)
            end = await page.evaluate("(hide) => window.__agySite.stepEnd(hide)", canvas)
            w = end["win"]
            step = Step(i, share, round(end["y"]), settle_s=round(secs, 2), motion=motion, reached_by=how,
                        draws=int(w.get("maxDraws", 0)), triangles=int(w.get("maxTris", 0)))
            f = self.out / f"{self.v.name}_{i:02d}.jpg"
            f.write_bytes(shot)
            step.frame = str(f)
            if canvas and end["rect"]:
                try:
                    cshot = await self.shot(end["rect"])
                finally:
                    await page.evaluate("window.__agySite.unhide()")
                cf = self.out / f"{self.v.name}_{i:02d}_canvas.jpg"
                cf.write_bytes(cshot)
                step.canvas_frame = str(cf)
                step.canvas_content = round(canvas_content(cshot), 4)
            if sideways:
                step.sideways_px = int(end["sideways"]["over"])
                for c in end["sideways"]["culprits"]:
                    if c not in self.v.overflow:
                        self.v.overflow.append(c)
            self.v.steps.append(step)
            self.log(f"step {i}: {how}, {motion} after {secs:.1f} s")
        self.v.steps.sort(key=lambda st: st.index)
        for a_, b_ in zip(self.v.steps, self.v.steps[1:]):
            if b_.index == a_.index + 1:
                b_.unchanged = _diff(_grey(Path(a_.frame).read_bytes(), 4), _grey(Path(b_.frame).read_bytes(), 4)) < UNCHANGED


def step_order(n: int) -> list[int]:
    """The steps coarse to fine (0 %, 100 %, 50 %, 25 %, 75 %, ...): a heavy page on software WebGL may not leave time
    for every step, and then the frames that were taken still cover the whole scroll evenly."""
    order, seen = [], set()
    parts = 1
    while len(order) < n:
        for k in range(parts + 1):
            i = round(k * (n - 1) / parts)
            if i not in seen:
                seen.add(i)
                order.append(i)
        parts *= 2
        if parts > 2 * n:
            order += [i for i in range(n) if i not in seen]
            break
    return order


def _short_url(url: str, base: str) -> str:
    return url[len(base):] if url.startswith(base) else urlsplit(url).path


def _is_request_echo(text: str) -> bool:
    """Chromium logs every failed resource load as a console error too; those are reported once, as failed requests."""
    return text.startswith("Failed to load resource")


async def _record(v: Visit, r, own: bool, base: str) -> None:
    try:
        resp = await r.response()
        sizes = await r.sizes()
    except Exception:  # noqa: BLE001  a page that navigated away: the request's size is unknown, nothing else is
        return
    status = resp.status if resp else 0
    if own and status >= 400:
        v.failed.append(f"{_short_url(r.url, base)} (HTTP {status})")
    if r.url not in v.requests:
        v.requests[r.url] = {"kind": _kind(r.url, r.resource_type), "bytes": int(sizes.get("responseBodySize") or 0),
                             "status": status, "own": own}


def _probe_script(no_webgl: bool) -> str:
    cfg = json.dumps({"noWebGL": no_webgl, "frameCapMs": FRAME_CAP_MS})
    return f"window.__agySiteConfig = {cfg};\n" + (_HERE / "site_probe.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the check


def _resolve_target(target: str) -> tuple[Path | None, str]:
    """(site folder or None, URL or '')."""
    if re.match(r"^https?://", target, re.I):
        host = urlsplit(target).hostname or ""
        if host not in _LOOPBACK:
            raise ValueError(f"{target} is not a localhost URL. check_site opens a built folder or a local dev server "
                             f"(http://localhost:5173/); to check a published page, check the folder it was built from.")
        return None, target
    p = Path(target).expanduser().resolve()
    if p.is_file() and p.suffix.lower() in (".html", ".htm"):
        p = p.parent
    if not (p / "index.html").is_file():
        raise ValueError(f"{p} holds no index.html. Give the folder of the built site (for Vite: dist/ after npm run build), "
                         f"or the URL of a running dev server.")
    return p, ""


async def acheck_site(target: str, *, sources: str | Path | None = None, steps: int = 9, out_dir: str | Path | None = None,
                      sizes: tuple[str, ...] = ("desktop", "phone"), other_hosts: str = "block",
                      budget_s: float = ANSWER_S, exe: str | None = None) -> SiteReport:
    """Check a built site; see the module docstring. ``other_hosts``: 'block' (default: requests to other hosts are
    recorded and not made, so the check sees the page as a visitor does when they are unreachable) or 'try'."""
    from antigravity3d.adapters.engines import web_browser
    from antigravity3d.adapters.engines.web_three import WebThreeValidator, _launch_args

    t_start = time.monotonic()
    clock = _Clock(t_start, budget_s)
    steps = max(2, min(int(steps), 25))
    folder, url = _resolve_target(str(target))
    out = Path(out_dir) if out_dir else Path.cwd() / "agy3d_site_check" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()
    src_dir = Path(sources).expanduser().resolve() if sources else folder
    rep = SiteReport(str(target), out, steps=steps)
    rep.facts["other_hosts_mode"] = other_hosts

    validator = WebThreeValidator()
    why = validator._unavailable()
    if why:
        rep.error = f"No browser to look with: {why}."
        rep.seconds = time.monotonic() - t_start
        return rep
    exe = exe or validator.binary()

    server = await asyncio.to_thread(_SiteServer, folder) if folder else None
    page_url = server.url if server else url
    own = server.origin if server else f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"

    async def visit(name: str, ctx_opts: dict, *, no_webgl: bool = False, do_steps: int = 0, canvas: bool = False,
                    sideways: bool = False, facts: bool = False, idle: bool = False, one_step: float | None = None) -> None:
        vis = _Visitor(name, page_url, own, out, clock, other_hosts)
        rep.visits[name] = vis.v

        async def run(_session, ctx) -> None:
            await ctx.route("**/*", vis.gate)
            page = await ctx.new_page()
            await vis.attach(ctx, page)
            await page.add_init_script(_probe_script(no_webgl))
            try:
                if not await vis.load(page):
                    return
                if idle:
                    vis.v.idle = await vis.idle(page)
                if facts:
                    with contextlib.suppress(Exception):
                        vis.v.facts_raw = await page.evaluate(_FACTS_JS)
                if do_steps:
                    await vis.steps(page, do_steps, canvas, sideways)
                if one_step is not None:
                    geo = await page.evaluate("window.__agySite.geometry()")
                    await vis.scroll_to(page, round(geo["max"] * one_step))
                    shot, secs, motion = await vis.settle(SETTLE_CAP_S)
                    vis.v.steps.append(Step(0, one_step, round(await page.evaluate("window.__agySite.y()")), settle_s=round(secs, 2),
                                            motion=motion))
                    vis.v.idle = await vis.idle(page)
                if not do_steps:
                    f = out / f"{name}.jpg"
                    shot, _, _ = await vis.settle(1.0)
                    f.write_bytes(shot)
                    vis.v.frame = str(f)
                info = await vis.info(page)
                vis.v.probe, vis.v.memory, vis.v.text_chars = info, info.get("memory"), info.get("textChars")
            finally:
                await vis.flush()
                if server is not None and name in ("desktop", "phone"):
                    for m in server.missing:
                        line = f"{m}"
                        if line not in vis.v.failed and not any(line.split(" (")[0] in f for f in vis.v.failed):
                            vis.v.failed.append(line)

        opts = dict(SIZES["desktop"] if name not in SIZES else SIZES[name])
        opts.update(ctx_opts)
        async with lanes:
            await web_browser.SHARED.run(run, exe=exe, args=_launch_args(), context_options=opts)

    lanes = asyncio.Semaphore(LANES)

    plan = {}
    if "desktop" in sizes:
        plan["desktop"] = visit("desktop", {}, do_steps=steps, canvas=True, facts=True, idle=True)
    if "phone" in sizes:
        plan["phone"] = visit("phone", {}, do_steps=steps, canvas=True, sideways=True)
    # the variants after the sizes (the lanes start them in this order); the reduced-motion visit's one step, at 50 %,
    # is compared with the desktop step nearest it
    plan["nowebgl"] = visit("nowebgl", {}, no_webgl=True)
    plan["reduced"] = visit("reduced", {"reduced_motion": "reduce"}, one_step=0.5)
    tasks = [asyncio.ensure_future(p) for p in plan.values()]
    try:
        done, pending = await asyncio.wait(tasks, timeout=max(1.0, clock.left() - 4.0))
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        for name, t in zip(plan, tasks):
            if t in pending and name not in ("desktop", "phone"):
                rep.unfinished.append(VISIT_LABEL[name])
            elif t not in pending and t.exception() is not None:
                e = t.exception()
                v = rep.visits.get(name)
                if v is not None and not v.load_error:
                    v.load_error = f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
        for name in ("desktop", "phone"):
            v = rep.visits.get(name)
            if v and v.loaded and len(v.steps) < steps:
                rep.unfinished.append(f"{name}: {len(v.steps)} of {steps} steps photographed")
            elif v and not v.loaded and not v.load_error:
                rep.unfinished.append(f"{name}: the page had not loaded")
    finally:
        if server is not None:
            await asyncio.to_thread(server.close)

    # pictures and facts
    for name in ("desktop", "phone"):
        v = rep.visits.get(name)
        if v and v.steps:
            frames = []
            for s in v.steps:
                flags = [f for f, on in (("3D empty", s.canvas_content is not None and s.canvas_content < EMPTY_CONTENT),
                                         ("unchanged", s.unchanged), ("still moving", s.motion == "moving"),
                                         ("sideways", s.sideways_px > 1)) if on]
                frames.append((s.frame, f"{s.index + 1}/{steps}  {s.share:.0%}" + (f"  {', '.join(flags)}" if flags else ""), bool(flags)))
            sheet = out / f"sheet_{name}.jpg"
            contact_sheet(frames, sheet, 480 if name == "desktop" else 200, 3 if name == "desktop" else min(len(frames), 5))
            rep.sheets[name] = str(sheet)
    d = rep.visits.get("desktop")
    rep.facts.update(judge_facts(d.facts_raw if d else None, src_dir))
    rep.seconds = time.monotonic() - t_start
    (out / "report.json").write_text(json.dumps(rep.to_dict(), indent=1, default=str))
    return rep


def check_site(target: str, **kw) -> SiteReport:
    """Blocking form of :func:`acheck_site` (the CLI's)."""
    return asyncio.run(acheck_site(target, **kw))
