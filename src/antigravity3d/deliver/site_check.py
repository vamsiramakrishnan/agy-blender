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
   with no source;
8. at each step, where the page says where its subjects are (``window.__site.info().onScreen``, each with a projected
   ``box``), measures how much of each is cut by the frame's edge and how much lies under HTML a visitor cannot see
   through, and names the ones a visitor would notice. Run 2 (benchmarks/results/site_run2_plugin) is why: the check
   came back clean while the gate sat under the text panel and labels ran off the frame at several steps. Where the
   page also flags the shot's subject, it says whether the picture travels with the scroll, whether every picture is
   the same kind of shot, and which labels are too small where the shot is about them: run 3's phone layout held the
   gate in one place for half the scroll, and nothing in the answer said so. After the steps it reads the subjects at
   four times as many positions between them (no picture), since what the steps alone find depends on where they stop
   (run 4: twelve steps named no subject out of the frame, nine named six). Where the page says its camera is between
   two shots (``info().moving``) it names nothing as cut, hidden or too small unless chapter text is readable there:
   run 5's site showed its new chapter's text as the camera set off, a blind judge named that frame in all six
   verdicts, and the check, told by the page that the camera was moving, had said nothing. It also measures how far
   the shot's subject stands from its ground: run 4's dark steel on a dark floor read as holes in the picture, and
   nothing said so;
9. where the page names its chapters and where each rests (``window.__site.chapters()``, ``setProgress``), writes one
   still per chapter for the page without WebGL, the 3D canvas alone, as ``stills/<chapter id>.webp``. Run 2's agent
   wrote a screenshot script for those twice (the first set had a scrollbar in every picture).

Everything is bounded: the whole check answers within ``ANSWER_S`` (the host ends a tool call at 180 s), with what it
has when time runs out, and says which parts are missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import re
import tempfile
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
#: the visits stop taking new pictures this long before ``ANSWER_S``: a step under way may take 8 s more, then closing
#: the contexts, the contact sheets and the facts need the rest.
WRAP_UP_S = 20.0
#: A visit still running this long before the answer is stuck in its page (a frame callback that never returns, a
#: screenshot that never comes): it is cancelled, and the answer waits this long for its browser context to close, so
#: no page of this check is left running in the shared browser. web_browser closes a cancelled visit's context within
#: CLOSE_TIMEOUT_S, or discards the browser.
STUCK_S = 11.0

#: The two sizes: a common laptop viewport, and a common phone's CSS viewport (390 x 844, iPhone 12 to 15). Both at a
#: device pixel ratio of 1: software WebGL draws every pixel on the CPU, and three times the pixels would cost three
#: times the wait without changing what the frames show about layout and composition.
SIZES: dict[str, dict[str, Any]] = {
    "desktop": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1},
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 1, "is_mobile": True, "has_touch": True},
}

#: A step's picture is "settled" when two frames, at least 100 ms and two animation frames apart, differ by less than
#: this (mean absolute difference of the 1/8-size grey image, share of full scale; JPEG noise of an unchanged picture is
#: 0, a blinking caret is about 0.0002).
STILL = 0.0015
#: how long one step may take to settle once the page has drawn at the new position (its first two frames after the
#: scroll); a page still changing after it is photographed as it is and named in the answer
SETTLE_CAP_S = 4.0
#: The picture "glides" after a scroll when it goes on changing for this long once the page has drawn at the new
#: place (``Step.changing_s``): smooth scrolling left on under reduce motion does, for about a second.
GLIDE_S = 0.5
#: ... and the most a step may take from the scroll itself, however long those first frames take. Counting the 4 s from
#: the scroll was wrong for a heavy page: run 4's site (188,000 triangles and an occlusion pass, drawn in software,
#: four visits at once) took 3.5 s for its first frame after the jump from the top of the page to its end, the check
#: gave up half a second later, in the middle of the 0.3 s cross-fade of the two chapters' panels, and its last frame
#: showed the first chapter's text over the last one's. No visitor sees that.
SETTLE_HARD_S = 12.0
#: The longest time the page is shown to pass between two of its frames (``site_probe.js``, "The page's clock"): below
#: GSAP's 500 ms lag threshold, so scrubbed animations advance by real time instead of 33 ms a frame on a slow frame.
FRAME_CAP_MS = 250
#: Frame-to-frame change below which a picture that keeps changing at the same rate counts as the page's own continuous
#: animation (a turning prop, drifting particles) rather than the scroll still arriving; a camera cut is far above it.
AMBIENT = 0.03
#: Two steps "did not change at all" below this mean difference (1/4-size grey image): the scroll moved and nothing on
#: screen did, which a visitor reads as a frozen page.
UNCHANGED = 0.002
#: A first scroll: one notch of a mouse wheel in CSS pixels (browsers scroll 100 to 120 px a notch at their default
#: setting). The desktop visit measures what it changes at the top of the page (``picture_shift``); a phone has no
#: notch, and a swipe there is several of them.
NOTCH_PX = 120
#: Two neighbours "step", and a pixel can "flip", beyond this many levels of 255 (``picture_shift``).
SHIFT_LEVELS = 16
#: A frame with fewer edge crossings than this per pixel is an empty picture: no shift is read from it. At 1440 x 900
#: it is 130 crossings, the outline of a shape 32 px square; the one stand-in block of the skill's starter has 500.
SHIFT_MIN_EDGES = 0.0001
#: "The first scroll gets no answer" when one notch moves the picture by less than this (``picture_shift``, about
#: pixels) and the page's text stays where it is. The numbers this was set on are beside ``_Visitor.first_notch``.
NOTCH_MIN_PX = 1.0
#: The top of the page "changes by itself" when any two of three frames of it, taken unevenly over about half a
#: second with no scroll between them, read as a shift of more than this (a page that stands still reads exactly 0 on
#: frames without JPEG's noise). ``settle`` calls a pulsing page still whenever two of its frames happen to match, and
#: so did two frames here when they fell a whole pulse apart.
NOTCH_IDLE_PX = 0.1
#: The page's text "moved with the notch" when this share of the characters readable in the window at the top of the
#: page is ``NOTCH_TEXT_PX`` or more from where it was, or gone: a page whose text scrolls answers the scroll with it.
NOTCH_TEXT_MOVED = 0.1
NOTCH_TEXT_PX = 8
#: The 3D canvas "shows nothing but its background" when less than this share of it differs from its most common colour
#: by more than 8 % of full scale, measured on the 1/8-size frame (1 px lines of a helper grid average out at that size,
#: an object does not). 1 % of a 1440 x 900 canvas is a 120 x 108 px patch.
EMPTY_CONTENT = 0.01
#: A step where the canvas shows less than this share of what it shows at the page's median step: the scene has all but
#: left the frame between chapters (run 1 at 62 %: a grid and one arc, 4 % of the canvas, against 19 % elsewhere; the
#: film's quietest step is 9 % against 21 %).
NEARLY_EMPTY = 0.25
#: The shot's subject against its ground (``subject_contrast``): three quarters of the pixels where the subject stands
#: within this share of full scale of the background's colour read as a hole in the picture. Run 4's site
#: (benchmarks/results/site_run4_plugin: dark steel on a dark floor; the blind judge said "dark machinery blends into
#: the dark floor" in every verdict on craft that went against it) measured 5 to 9 % at seven of nine steps on the
#: desktop and five on the phone (round 8's frames3/run4: desktop 6 6 8 5 7 36 29 9 6, phone 7 7 10 5 9 31 33 16 6);
#: run 3 (frames3/run3) 25 to 32 % on the desktop and 13 to 20 % on the phone. 10 % lies between.
CONTRAST_LOW = 0.10
#: The line is said when the measure is under the limit at this share of the steps that have one, and at
#: CONTRAST_STEPS of them at least: one dark step is a moment (a chapter set at night), a third of the scroll is the
#: site's look.
CONTRAST_SHARE = 1 / 3
CONTRAST_STEPS = 2
#: No measure where the subject's box, clipped to the frame, is under this share of the frame either way: at 1/8 size
#: that is a few pixels, and their percentile says nothing.
CONTRAST_MIN_BOX = 0.02
#: Download weight is reported as the wait on a 10 Mbit/s connection (a fair mobile link); it is called out when that
#: wait passes 5 s, the point where the page has had the visitor's whole attention span for a first impression.
LINK_BPS = 10e6 / 8
SLOW_WAIT_S = 5.0
#: Visits run at once. Software WebGL is bound by the CPU (4 cores here): the two sizes and the short no-WebGL visit side
#: by side, the reduced-motion visit when a lane frees. Measured on the film: 1 lane 67 s, 2 lanes 54 s, 4 lanes 40 s.
LANES = 3
VISIT_LABEL = {"reduced": "the visit with reduce motion set", "nowebgl": "the visit without WebGL"}
#: The page's subjects (``window.__site.info().onScreen``) are read for at most this long per step, and at most this
#: many of them (site_probe.js holds the same bound).
SUBJECTS_WAIT_S = 3.0
SUBJECTS_READ = 64
#: Chapter text is readable at a position when at least this many characters of headings, paragraphs and list items
#: are drawn over the canvas at half opacity or more, outside the page's header, nav and footer (site_probe.js,
#: ``chapterText``). There a camera the page says is moving is no excuse: the text asks the visitor to stop and read,
#: and what they read beside is a picture on its way. Measured at 33 even positions of the desktop's scroll (1440 x
#: 900): the starter, which shows a chapter's text only once its shot is held, 0 at all 9 positions in a move and 59
#: to 84 at rest (its placeholder panels are short); run 5's site (round 8's fifth build), whose text comes as the
#: move starts, 205 to 260 at its 10 positions in a move and 153 to 260 at rest; run 4's site
#: (benchmarks/results/site_run4_plugin), which does not say when its camera moves, 256 to 565 everywhere. Its top bar
#: (a header or nav) and the labels drawn in the scene count 0. 20 characters is a short heading.
TEXT_READABLE = 20
#: What a finding at such a position says after its numbers: in full at the first one of an answer, in a few words at
#: the others (``noticed``). The check's first answer on run 5's site said the sentence seven times.
TEXT_IN_MOVE = (" There the chapter's text is up while the camera is still on its way to the chapter's shot: show the "
                "text when the shot is held, as the skill's starter does, or finish the move first.")
TEXT_IN_MOVE_AGAIN = " The text is up mid-move there too."
#: When a subject is reported as cut by the frame's edge or hidden under the page's HTML. Shares are of the subject's
#: projected box: "cut" is the part of the box outside the window, "hidden" the part of what is inside that lies under
#: an element a visitor cannot see through (background alpha times opacity at least site_probe.js's COVER_ALPHA, 0.6,
#: or a backdrop blur). Calibrated on run 2's site (benchmarks/results/site_run2_plugin, 9 steps at both sizes, frames
#: looked at one by one against the reviewer's findings):
#: - a 3D label 15 % cut loses the ends or the feet of its letters and no longer reads, so a label counts from 10 %
#:   either way (run 2: '302,628 TOK RAW FLOOD' 21 % below the bottom edge and 'L14,238 QUIET NEEDLE' 56 % under the
#:   panel, both called out by the reviewer);
#: - a prop in a close shot often runs a little out of frame on purpose: run 2's gate, vault and clamp at 11 to 22 %
#:   out read as framed close, the clamp at 42 % and 62 % out as cut off (the reviewer's finding). Props count from 30 %;
#: - a prop's box is the projection of its 3D bounding box, whose corners stand off the silhouette; run 2's gate
#:   under the top bar by 12 % of its box was only the empty corner above its knobs, while the gate 20 % under the
#:   digest chapter's panel showed its whole left column covered (the reviewer's finding). Props count from 15 % hidden;
#: - a subject less than a fifth in the frame is entering or leaving the shot (run 2: the vault as a sliver at the right
#:   edge for the first three steps, 84 to 93 % out); the frame is composed around what is in it, and it is not named.
LABEL_CUT = 0.10
LABEL_HIDDEN = 0.10
#: A 3D label whose box is never taller than this many CSS pixels at any step cannot be read anywhere: run 2's
#: 'STRAITJACKET · EVIDENCE BENCH', flat on the floor in the last wide shot, had a 15 px box and letters of a few pixels.
#: A label that is small at some steps and readable at others is in the background there and is not named, unless the
#: page says the shot is about it at such a step (``small_labels``): run 3's last step, a wide shot of the three props
#: its labels name, showed the labels at 11 to 15 px on the desktop and 5 to 7 px on the phone.
LABEL_MIN_PX = 16
PROP_CUT = 0.30
PROP_HIDDEN = 0.15
IN_FRAME_MIN = 0.2
#: subjects named one per line, per size, props first, each in the order the scroll meets it; the rest are named together
#: in one line (run 2's site had 9 such subjects on desktop and 11 on the phone)
SUBJECT_LINES = 8
#: The picture does not travel: the shot's subject (an entry the page flags ``subject: true``) keeps its box over a run
#: of neighbouring steps, every edge within HELD_MOVE of the window of where it was at the run's first step. Run 3's
#: phone layout (benchmarks/results/site_run3_plugin; its record in round 8's frames3/run3/report.json) fitted the gate
#: into the same place above the panel at steps 1 to 5 of 9: its edges stayed within 0.7 % of the window of step 1
#: while the page scrolled half its length, and the pixel comparison said nothing, because small things around it
#: changed. On the desktop, which was not pinned, the subject's box moved by 3.8 % (the intro's push, the smallest) to
#: 19 % of the window between neighbouring steps. 2 % lies three times above the first and half way to the second.
HELD_MOVE = 0.02
#: A run counts from HELD_STEPS steps that together cover HELD_SHARE of the scroll. Two steps are not enough: a page
#: may rest at its very top or bottom for a step (run 3 at 9 steps: two steps are an eighth of the scroll, which a
#: chapter's hold can take). A quarter of the scroll is more than one chapter of run 3's six; at 25 steps it keeps a
#: run of three steps that lie inside one chapter's rest from being named. Run 3's phone run of five steps (half the
#: scroll) is named; its desktop has no two neighbouring steps with the same box.
HELD_STEPS = 3
HELD_SHARE = 0.25
#: One scale throughout: the shot's subject (the union of the entries flagged ``subject: true``, labels aside) fills
#: about the same share of the window at most steps. Its size is the larger of its share of the window's height and
#: width (whichever it fills more; clamped to the window). The steps within a factor SCALE_BAND of the median are
#: counted, and the line is said when they are at least SCALE_MOST of the steps that name a subject, and at least
#: SCALE_STEPS of them. Shot sizes in film (wide, medium, close) differ by about a factor of two in how much of the
#: frame the subject fills; a quarter either way stays within one. Run 3's desktop: 49 to 64 % of the window's height
#: at all 8 steps that name a subject (median 56 %), every picture the same kind of shot; the starter's closing wide
#: shot, or run 3's own last step at 19 %, is outside the band.
SCALE_BAND = 1.25
SCALE_MOST = 0.75
SCALE_STEPS = 4
#: Subjects between the photographed steps (``_Visitor.between``): read at BETWEEN_PER_STEP times as many positions
#: as the steps, for at most BETWEEN_S per visit, and a frame taken at the worst position of at most BETWEEN_FRAMES
#: findings found only there. A position is read once the page has drawn two frames there and its CSS transitions
#: have ended, for at most BETWEEN_TRANSITION_MS (the starter's panels cross-fade in 0.4 s). Measured at 9 steps (24
#: positions between them): the starter with stand-ins read all 24 in 8 to 10 s per size, and its whole check went from
#: 19 to 31 s; run 4's site (188,000 triangles and an occlusion pass, drawn in software) takes 1.2 to 1.8 s a position
#: and read 12 to 16 in 20 s, its whole check from 68 to 78 s of the 150 s it has.
BETWEEN_PER_STEP = 4
BETWEEN_S = 20.0
BETWEEN_FRAMES = 3
BETWEEN_TRANSITION_MS = 600
#: Stills for the page without WebGL (one per chapter, its resting picture, the 3D canvas alone), when the page gives
#: window.__site.chapters() -> [{ id, rest }] and setProgress(T). Width: the article layout shows a still at most as
#: wide as its text column, 40rem (640 CSS px) in the skill's starter, about 590 px inside the panel's padding; 1280 px
#: covers that on a screen of 2 device pixels per CSS pixel, and a 3x phone's column of about 360 px needs 1080. Run 2's
#: agent shipped 1280 px stills too. A canvas narrower than that is kept at its own width, never enlarged.
STILL_WIDTH = 1280
#: WebP quality (Pillow, libwebp, method as web_textures.WEBP_METHOD).
STILL_QUALITY = 80
STILL_METHOD = 4
#: at most this many chapters are read from the page, and all stills together take at most STILLS_S, each at most
#: STILL_WAIT_S, inside the answer's limit
STILLS_READ = 24
STILLS_S = 25.0
STILL_WAIT_S = 8.0
#: a still's file name: letters, digits, '-' and '_' of the chapter id, at most this long
STILL_NAME_CHARS = 60
#: set (empty) on the page's <html> while the stills are drawn, and removed after the last, also when a still fails
STILL_ATTR = "data-agy3d-still"
#: set (empty) on the page's <html> by the probe before any script of the page runs, in every visit: this browser
#: stands in for a visitor's, so a page may draw what a visitor with a GPU sees although it renders in software, time
#: does not matter. The skill's starter then turns its ambient occlusion on (src/occlusion.js): without it the frames
#: lacked the contact shading a visitor sees, and the pass's memory (95 MB of textures at 1440 x 900 in the starter)
#: was never measured.
CAMERA_ATTR = "data-agy3d-camera"
#: how a page says that it draws no ambient occlusion in this browser because it renders in software (the start of
#: ``window.__site.info().occlusion``; the skill's starter before it read CAMERA_ATTR, ``src/occlusion.js``)
OCCLUSION_OFF_HERE = "off: a software renderer"
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
    # of that, how long the picture went on changing after the page had drawn at the new place (0: its first frame
    # there was its last). The rest of settle_s is the wait for that first frame, which on a heavy page in software is
    # seconds and says nothing about the page's motion.
    changing_s: float = 0.0
    motion: str = "still"   # how the picture settled: still | ambient | moving (see _Visitor.settle)
    canvas_content: float | None = None  # share of the canvas showing something other than its background
    shape: float | None = None  # share of that which is one solid shape (largest_shape)
    unchanged: bool = False
    draws: int = 0
    triangles: int = 0
    shown_triangles: int = 0  # triangles of the latest frame that drew: the picture the step's frame shows
    sideways_px: int = 0
    reached_by: str = "scroll"
    # the page's subjects at this step (window.__site.info().onScreen, measured by site_probe.js): one dict per subject
    # with a box: name, kind, box, px, cut, edges, hidden, under. None when the page gives no box; subjects_note says why.
    subjects: list[dict] | None = None
    subjects_note: str = ""
    # the page said its camera is between two shots here (``window.__site.info().moving``): no subject is named as cut,
    # hidden or too small at this position, unless chapter text is readable here (``text``, ``TEXT_READABLE``)
    moving: bool = False
    # characters of chapter text a visitor can read over the canvas here (site_probe.js, ``chapterText``); None: not
    # read
    text: int | None = None
    # how far what stands where the shot's subject is lies from the background (``subject_contrast``); None: no measure
    contrast: float | None = None
    # a position read between the photographed steps (``_Visitor.between``): subjects only, and a frame only where a
    # finding's worst position is here (``extra frames``)
    between: bool = False


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
    # the positions read between the photographed steps (``_Visitor.between``), how many were planned and the seconds
    # the pass took; planned 0: the pass did not run (no page hook, or no time)
    between: list[Step] = field(default_factory=list)
    between_planned: int = 0
    between_s: float = 0.0
    # what one wheel notch from the top of the page changed (``_Visitor.first_notch``): {px, motion, window, canvas,
    # text}; None: not measured (not the desktop visit, a page shorter than two notches, or no time)
    first_notch: dict | None = None


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
    budget_s: float = ANSWER_S
    # stills for the page without WebGL: {"dir", "made": [{id, file, rest, width, height, bytes, content, how}],
    # "skipped": [{id, why}]}, or {"note": why there are none} (no hook: nothing is said in the answer)
    stills: dict = field(default_factory=dict)
    # the test of the browser's screenshots (``camera``): 'true', 'layer', 'faulty' or '' (not known)
    camera: str = ""

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
        for v in self.visits.values():
            lost = (v.probe or {}).get("lost", 0)
            if lost and v.name in ("desktop", "phone"):
                mem = (v.memory or {}).get("texture_bytes", 0)
                out.append(f"{v.name.capitalize()}: the WebGL context was lost {lost} time{_s(lost)}; until the page "
                           f"restores it the canvas draws nothing (the graphics memory ran out or the driver reset; "
                           f"the page holds {_mb(mem)} of textures).")
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
            shares = sorted(s.canvas_content for s in v.steps if s.canvas_content is not None)
            if len(shares) >= 4:
                median = shares[len(shares) // 2]
                thin = [s for s in v.steps if s.canvas_content is not None and EMPTY_CONTENT <= s.canvas_content
                        < NEARLY_EMPTY * median]
                if thin:
                    out.append(f"{name.capitalize()}: the 3D canvas shows little at " + _steps_list(thin, self.steps)
                               + " (" + ", ".join(f"{s.canvas_content:.0%}" for s in thin) + " of it shows something, "
                               f"against {median:.0%} at a typical step; one solid shape is "
                               + ", ".join("-" if s.shape is None else f"{s.shape:.0%}" for s in thin)
                               + " of that). Look at those frames: a small subject framed on purpose is usually one "
                               "solid shape; what is left when the subject has gone (grid lines, arcs) is scattered.")
            if notch := self.notch_line(v):
                out.append(notch)
            same = [s for s in v.steps if s.unchanged]
            if same:
                out.append(f"{name.capitalize()}: the picture did not change at all from the step before at "
                           + _steps_list(same, self.steps) + ".")
            if held := self.held_line(v):
                out.append(held)
            if faint := self.contrast_line(v):
                out.append(faint)
            out += self.subject_lines(v)
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
        told = False
        for i, line in enumerate(out):  # the reason for a finding in a move with the text up: once in full
            if TEXT_IN_MOVE in line:
                if told:
                    out[i] = line.replace(TEXT_IN_MOVE, TEXT_IN_MOVE_AGAIN)
                told = True
        return out

    def subject_lines(self, v: Visit) -> list[str]:
        """One line per subject of the page that is cut by the frame's edge or hidden under the page's HTML at some
        position read (the thresholds above), with where, the worst share, what covers it or which edge cuts it, and the
        frame to look at (``subject_findings``)."""
        found = subject_findings(v)
        if not found:
            return []
        positions = _positions(v)
        size = v.name.capitalize()
        out = []
        for f in found[:SUBJECT_LINES]:
            what = _what(f["kind"], f["name"], 60)
            parts = []
            if f["hidden"]:
                p, s = max(f["hidden"], key=lambda t: t[1]["hidden"])
                covers = list(dict.fromkeys([s["under"]] + [x["under"] for _, x in f["hidden"]]))
                covers = [c for c in covers if c] or ["the page's HTML"]
                worst = f"{s['hidden']:.0%} of what is in the frame"
                parts.append(f"lies under {_names(covers)} " + self._where(f["hidden"], positions, worst, p))
            if f["cut"]:
                p, s = max(f["cut"], key=lambda t: t[1]["cut"])
                edges: dict[str, float] = {}
                for _, x in f["cut"]:
                    for e, share in x["edges"].items():
                        edges[e] = max(edges.get(e, 0.0), share)
                named = [e for e, share in sorted(edges.items(), key=lambda kv: -kv[1]) if share >= 0.02] or ["frame's"]
                parts.append(f"runs out of the frame at the {_and(named)} edge{'s' if len(named) > 1 else ''} "
                             + self._where(f["cut"], positions, f"{s['cut']:.0%} of it outside", p))
            if "small" in f:
                st, s = f["small"]
                parts.append(f"is never taller than {s['px'][1]} px at any step (its largest is at "
                             + _steps_short([st], self.steps) + "), too small to read")
            if f.get("small_at"):
                p, px = min(f["small_at"], key=lambda t: t[1])
                at = self._where([(q, {}) for q, _ in f["small_at"]], positions,
                                 f"{px} px tall, under {LABEL_MIN_PX}", p)
                parts.append(f"is in the frame where the shot is about it, but too small to read {at}: make it "
                             "larger there or frame it closer")
            look = _rel(_look_frame(f["look"], v), self.out_dir)
            why = TEXT_IN_MOVE if f["text_in_move"] else ""
            out.append(f"{size}: {what} " + "; it ".join(parts) + f".{why} Look at {look}.")
        rest = found[SUBJECT_LINES:]
        if rest:
            out.append(f"{size}: also cut, hidden or too small at some step: "
                       + _names([_what(f["kind"], f["name"], 40) for f in rest], SUBJECT_LINES)
                       + " (every subject at every step is in report.json).")
        return out

    def _where(self, hits: list[tuple[Step, dict]], positions: list[Step], worst: str, at: Step) -> str:
        """Where a fault was found and how bad it was at worst. Found at photographed steps only, the steps, as before
        the positions between them were read: 'at steps 4 (38%) and 5 (50%) of 9 (at worst 40% of it outside)'. Found
        between them too, the stretches of the scroll it covers, neighbouring positions read with the same fault
        merged into one: 'from 70% to 78% of the scroll, between the photographed steps (at worst 45% of it outside,
        at 74%)', or 'from 0% to 100% of the scroll (at steps 1 (0%) and 2 (100%) of 2 and between them; ...)'."""
        if not any(p.between for p, _ in hits):
            return f"at {_steps_short([p for p, _ in hits], self.steps)} (at worst {worst})"
        spans = []
        for r in _runs(hits, positions):
            a, b = r[0][0].share, r[-1][0].share
            spans.append(f"at {a:.0%}" if len(r) == 1 or f"{a:.0%}" == f"{b:.0%}" else f"from {a:.0%} to {b:.0%}")
        steps_in = [p for p, _ in hits if not p.between]
        if steps_in:
            return (_and(spans) + f" of the scroll (at {_steps_short(steps_in, self.steps)} and between them; at worst "
                    f"{worst}, at {at.share:.0%})")
        return _and(spans) + f" of the scroll, between the photographed steps (at worst {worst}, at {at.share:.0%})"

    def contrast_line(self, v: Visit) -> str:
        """The shot's subject against its ground (``subject_contrast``): said when it is under ``CONTRAST_LOW`` at
        ``CONTRAST_SHARE`` of the steps that have a measure, and at ``CONTRAST_STEPS`` at least; '' otherwise."""
        measured = [s for s in v.steps if s.contrast is not None]
        low = [s for s in measured if s.contrast < CONTRAST_LOW]
        if len(low) < max(CONTRAST_STEPS, CONTRAST_SHARE * len(measured)):
            return ""
        worst = min(low, key=lambda s: s.contrast)
        return (f"{v.name.capitalize()}: the shot's subject hardly stands out from its ground at "
                + _steps_short(low, self.steps) + f" ({len(low)} of the {len(measured)} steps whose subject the page "
                "names): three quarters of what stands where the subject is lies within "
                + ", ".join(f"{s.contrast:.0%}" for s in low) + f" of the background's colour, at worst "
                f"{worst.contrast:.0%} (under {CONTRAST_LOW:.0%} the subject reads as a hole in the picture). A dark "
                "material on a dark ground does, and a light one on a light ground: give the prop's main material a "
                "value well away from the background's, or bring light to it. Look at "
                f"{_rel(worst.canvas_frame, self.out_dir)}.")

    def notch_line(self, v: Visit) -> str:
        """The finding when the first wheel notch moves next to nothing (``NOTCH_MIN_PX``), else ''."""
        if not first_notch_unanswered(v):
            return ""
        return (f"{v.name.capitalize()}: the first scroll gets no answer. One wheel notch ({NOTCH_PX} px) from the top of "
                f"the page moves the picture by about {first_notch_px(v):.1f} px and none of the text, so a visitor who "
                "scrolls once sees nothing happen. "
                "Start the opening at speed: the skill's starter opens turned back from its first shot and swings round "
                "from the first pixel (shots.js, OPENING), and whatever of your own plays first must not ease in from "
                "rest.")

    def notch_measure(self, v: Visit) -> str:
        """The measures' line about the first wheel notch, '' when it was not tried."""
        n = v.first_notch
        if not n:
            return ""
        px, text = first_notch_px(v), n.get("text") or 0.0
        if n.get("motion") != "still":
            return (f"The first wheel notch ({NOTCH_PX} px from the top of the page) was not measured: the top of the "
                    "page keeps changing by itself.")
        moved = ([] if px is None else [f"the picture by about {px:.1f} px"]) + ([f"{text:.0%} of the text in view"] if text >= 0.01 else [])
        if not moved:
            return ""
        return f"The first wheel notch ({NOTCH_PX} px from the top of the page) moves " + " and ".join(moved) + "."

    def held_line(self, v: Visit) -> str:
        """'The picture does not travel' (``held_runs``): one line per size, naming the subject, the steps, the share of
        the scroll and what to do; '' when no run is long enough."""
        runs = held_runs(v.steps)
        if not runs:
            return ""
        runs.sort(key=lambda r: (-(r["steps"][-1].share - r["steps"][0].share), r["steps"][0].index))

        def where(r: dict) -> str:
            a, b = r["steps"][0], r["steps"][-1]
            return (f"steps {a.index + 1} to {b.index + 1} of {self.steps} ({a.share:.0%} to {b.share:.0%}, "
                    f"{b.share - a.share:.0%} of the scroll)")

        r = runs[0]
        also = "".join(f"; so does {_what(x['kind'], x['name'], 40)} at {where(x)}" for x in runs[1:3])
        a, b = r["steps"][0], r["steps"][-1]
        return (f"{v.name.capitalize()}: the picture does not travel: {_what(r['kind'], r['name'], 60)}, the shot's "
                f"subject, keeps its place and size on screen at {where(r)}: its box moves by at most "
                f"{r['moved']:.0%} of the window while the page scrolls on{also}. "
                "Let the camera move through those chapters (a push in, a turn round the subject, a new angle per "
                f"chapter) instead of framing the subject the same way at every step. Look at "
                f"{_rel(a.frame, self.out_dir)} to {_rel(b.frame, self.out_dir)}.")

    def scale_line(self, v: Visit) -> str:
        """'One scale throughout' (``subject_scale``): a measure, not a fault; '' unless most steps share one scale."""
        sizes = subject_scale(v.steps)
        if len(sizes) < SCALE_STEPS:
            return ""
        vals = sorted(sizes.values())
        med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        near = [x for x in vals if med / SCALE_BAND <= x <= med * SCALE_BAND]
        if len(near) < SCALE_MOST * len(vals):
            return ""
        return (f"{v.name.capitalize()}: one scale throughout: the shot's subject fills {vals[0]:.0%} to "
                f"{vals[-1]:.0%} of the window (its height or width, whichever it fills more) at the {len(vals)} steps "
                f"whose subject the page names, {len(near)} of them within a quarter of the median ({med:.0%}): every "
                "picture is the same kind of shot. A story usually changes scale between chapters (wide to set the "
                "scene, close for a detail).")

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
        return (f"Downloaded (desktop, whole scroll): {_mb(self.total_bytes())} in {len(d.requests)} file{_s(d.requests)}: {kinds}. "
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
        lines = [f"Scene (desktop, the largest over the steps): {md:,} draw call{_s(md)} and {mt:,} triangle{_s(mt)} in one frame"
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
        # A scroll that still glides to a stop under the setting (smooth scrolling left on) is reported; the times of a
        # page that never holds still say nothing. What counts is how long the picture went on changing once the page
        # had drawn at the new place, not the whole wait: run 6's page comes to rest in its first frame under the
        # setting, and this line said "takes 9.5 s to come to rest (without the setting 2.2 s)", which was the time this
        # browser, drawing four visits at once in software, took to draw that frame.
        if r_step and d_step and r_step.motion == "still" and r_step.changing_s >= GLIDE_S:
            glide = (f"; after a scroll the picture still goes on changing for {r_step.changing_s:.1f} s once it has "
                     f"drawn at the new place (without the setting {d_step.changing_s:.1f} s)")
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
        seen = set((d.probe or {}).get("notices", [])) if d else set()
        said = next((t for t in (w.probe or {}).get("notices", []) if t not in seen), None)
        chars = w.text_chars or 0
        dchars = (d.text_chars if d else None) or 0
        images = (w.probe or {}).get("images", 0)
        # Handled: nothing thrown and the reading survives (at least half the text the page shows with WebGL). A message
        # about the missing 3D is reported, not required: a page that turns into a good document needs none.
        ok = not errs and chars > 0.5 * max(dchars, 1) and chars > 0
        return ((f"Without WebGL: {len(errs)} uncaught error{_s(errs)} ({_q(errs[0])}); " if errs else "Without WebGL: no errors; ")
                + f"the page shows {chars:,} characters of text" + (f" (with WebGL {dchars:,})" if dchars else "")
                + f" and {images} image{_s(images)}, "
                + (f"and says so on screen ({_q(said, 60)})" if said else "and no message about the missing 3D")
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
        if self.error and not self.visits:
            # Nothing was looked at (no browser on this machine): that is not a finding about the site. CI's first job
            # has no Playwright, and the answer there read "SITE CHECKED in 0 s: 1 thing broken".
            return (f"NOT CHECKED: {self.error} Nothing of the site was opened, so nothing is known about it; the "
                    "check needs a Chromium that Playwright can start.")
        broken, noticed = self.broken(), self.noticed()
        head = (f"SITE CHECKED in {self.seconds:.0f} s: "
                + (f"{len(broken)} thing{_s(broken)} broken" if broken else "nothing broken")
                + f", {len(noticed)} thing{_s(noticed)} a visitor would notice. "
                + ("Open the contact sheets below with your file viewer and look at every frame before you change anything."
                   if self.sheets else ""))
        lines = [head]
        if self.unfinished:
            lines.append(f"The check stopped at its {self.budget_s:.0f} s limit before it was done ("
                         + "; ".join(self.unfinished) + "). Nothing needs fixing because of that: a heavy page on software "
                         "WebGL is slow here. For a quicker look call again with steps=5 or sizes=\"desktop\".")
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
                moving = [s for s in v.steps if s.motion == "moving"]
                lines.append(f"- {name.capitalize()} {vp['width']}x{vp['height']}: settled {v.load_s:.1f} s after the page was "
                             f"asked for; {len(v.steps)} of {self.steps} steps photographed; share of the 3D canvas showing "
                             "something, per step: "
                             + " ".join("-" if s.canvas_content is None else f"{s.canvas_content:.0%}" for s in v.steps) + "."
                             + (f" Still changing {SETTLE_CAP_S:.0f} s after the page drew there at {_steps_list(moving, self.steps)}: "
                                "those frames may show the scene on its way (software WebGL draws a heavy frame in about a "
                                "second; a visitor's GPU in a few milliseconds)." if moving else "")
                             + (f" The page's subjects read at {len(v.between)} of {v.between_planned} positions "
                                f"between the steps as well ({v.between_s:.0f} s)." if v.between_planned else ""))
                if scale := self.scale_line(v):
                    lines.append(f"- {scale}")
                if notch := self.notch_measure(v):
                    lines.append(f"- {notch}")
        for line in [self.bytes_line(), *self.scene_line()]:
            if line:
                lines.append(f"- {line}")
        for r in (self.reduced_line(), self.nowebgl_line()):
            if r and r[1]:  # an unhandled case is already listed above
                lines.append(f"- {r[0]}")
        lines += [f"- {x}" for x in self.facts_lines()]
        lines.append("")
        lines.append("Pictures:")
        for name, sheet in self.sheets.items():
            v = self.visits[name]
            frames = [s.frame for s in v.steps if s.frame]
            lines.append(f"- {name} contact sheet: {sheet}" + (f" (frames {_rel(frames[0], self.out_dir)} to "
                                                             f"{_rel(frames[-1], self.out_dir)})" if frames else ""))
        if line := self.stills_line():
            lines.append(f"- {line}")
        lines.append(f"- all frames, the canvas-only frames and the full record (report.json) are in {self.out_dir}")
        left = max((int((v.probe or {}).get("layerLeft") or 0) for v in self.visits.values()), default=0)
        if line := camera_line(self.camera, left):
            lines.append(f"- about these pictures: {line}.")
        if line := self.occlusion_line():
            lines.append(f"- about these pictures: {line}")
        return "\n".join(lines)

    def occlusion_line(self) -> str:
        """A page built on the skill's starter draws ambient occlusion where the visitor has a GPU and, on a software
        renderer, only when the check asks for a visitor's picture (``CAMERA_ATTR``). A page that does not read the
        attribute (an older starter) leaves it off here and says so itself (``window.__site.info().occlusion``, "off: a
        software renderer ..."); the answer passes that on, so that nobody judges the contact of things from these
        frames or thinks the pass is missing, and says how to look at such a page once."""
        d = self.visits.get("desktop")
        said = str((d.probe or {}).get("occlusion") or "") if d else ""
        if not said.startswith(OCCLUSION_OFF_HERE):
            return ""
        return ("the page leaves its ambient occlusion off in this browser, which draws in software "
                f"(it says: {_q(said, 140)}), so these frames and the memory measured are without it, and a visitor "
                f"with a GPU sees it. The skill's starter turns it on here when <html> has {CAMERA_ATTR}, which the "
                "check sets; this page does not read it. For one look at it, give a running dev server's address with "
                "?ao=1 as the target.")

    def stills_line(self) -> str:
        st = self.stills
        made, skipped = st.get("made") or [], st.get("skipped") or []
        if not made and not skipped:
            return ""
        notmade = ("; not made: " + "; ".join(f"{_q(x['id'], 40)} ({x['why']})" for x in skipped[:3])
                   + (f" and {len(skipped) - 3} more" if len(skipped) > 3 else "")) if skipped else ""
        if not made:
            return "no stills for the page without WebGL" + notmade + "."
        w, h = made[0]["width"], made[0]["height"]
        blank = [x["file"] for x in made if x["content"] < EMPTY_CONTENT]
        return ("stills for the page without WebGL, one per chapter at the picture it rests on, the 3D canvas alone "
                f"({len(made)}, {w} x {h} px WebP, {_mb(sum(x['bytes'] for x in made) / len(made))} each on average): "
                f"{st['dir']} (" + _names([x["file"] for x in made], 4) + "). Copy them beside the page "
                "(stills/<chapter id>.webp) and give each an alt text that says what it shows"
                + (f"; {_names(blank, 3)} show{'s' if len(blank) == 1 else ''} nothing but the background" if blank else "")
                + notmade + ".")

    def to_dict(self) -> dict:
        def visit(v: Visit) -> dict:
            out = {k: getattr(v, k) for k in ("name", "load_s", "loaded", "load_error", "page_errors", "console_errors", "failed",
                                              "other_hosts", "other_failed", "idle", "memory", "overflow", "text_chars", "frame",
                                              "notes")}
            out["steps"] = [s.__dict__ for s in v.steps]
            out["between"] = [s.__dict__ for s in v.between]
            out["between_planned"], out["between_s"] = v.between_planned, v.between_s
            out["first_notch"] = v.first_notch
            out["requests"] = v.requests
            out["probe"] = v.probe
            return out

        return {"target": self.target, "seconds": round(self.seconds, 1), "broken": self.broken(), "noticed": self.noticed(),
                "unfinished": self.unfinished, "sheets": self.sheets, "facts": self.facts, "stills": self.stills,
                "camera": self.camera,
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


def _steps_short(steps: list[Step], n: int) -> str:
    """'step 4 of 9 (38%)', or 'steps 4 (38%), 5 (50%) and 9 (100%) of 9'."""
    if len(steps) == 1:
        return _steps_list(steps, n)
    return "steps " + _and([f"{s.index + 1} ({s.share:.0%})" for s in steps]) + f" of {n}"


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _names(names: list[str], n: int = 2) -> str:
    return ", ".join(names[:n]) + (f" and {len(names) - n} more" if len(names) > n else "")


def _what(kind: str, name: str, n: int) -> str:
    """How a line names a subject: the 3D label, the layer (an instanced layer or a group of generated geometry that a
    shot is about, reported and measured as a prop is) or the prop."""
    return {"label": "the 3D label", "layer": "the layer"}.get(kind, "the prop") + f" {_q(name, n)}"


def _flags_subjects(steps: list[Step]) -> bool:
    """Whether the page says, at any step, which subjects the shot is about."""
    return any(x.get("subject") is not None for st in steps for x in st.subjects or [])


def _about(st: Step) -> list[dict]:
    """The props and layers the page flags as the shot's subject at this step."""
    return [x for x in st.subjects or [] if x.get("subject") is True and x["kind"] != "label"]


def _positions(v: Visit) -> list[Step]:
    """Every position of a visit whose subjects were read, photographed steps and the positions between them, in the
    order of the scroll."""
    return sorted([*v.steps, *v.between], key=lambda st: st.share)


def _runs(hits: list[tuple[Step, Any]], positions: list[Step]) -> list[list[tuple[Step, Any]]]:
    """``hits`` grouped into runs of neighbouring positions (neighbours in ``positions``: a position whose subjects
    could not be read, or where the subject has no fault, ends a run)."""
    at = {id(p): i for i, p in enumerate(positions)}
    runs: list[list[tuple[Step, Any]]] = []
    prev = None
    for p, x in sorted(hits, key=lambda t: at[id(t[0])]):
        i = at[id(p)]
        if runs and prev == i - 1:
            runs[-1].append((p, x))
        else:
            runs.append([(p, x)])
        prev = i
    return runs


def subject_findings(v: Visit) -> list[dict]:
    """The subjects a visitor would notice cut by the frame's edge, hidden under the page's HTML or too small, at every
    position read (``subject_faults``, ``small_labels``; a position where the page says its camera is moving and no
    chapter text is readable has none), in the order the answer names them: props and layers first, each in the order
    the scroll meets it. One dict per subject: name, kind, the hits per fault ('hidden', 'cut', 'small_at': [(position,
    entry or px)], 'small': (step, entry) for a label never readable at any step), 'look', the position whose frame
    shows the worst of it, and 'text_in_move', whether a hit lies where the camera is moving with chapter text up."""
    positions = _positions(v)
    found: dict[str, dict] = {}
    for st in positions:
        for s in st.subjects or []:
            cut, hidden = subject_faults(s)
            if cut or hidden:
                f = found.setdefault(s["name"], {"name": s["name"], "kind": s["kind"], "cut": [], "hidden": []})
                if cut:
                    f["cut"].append((st, s))
                if hidden:
                    f["hidden"].append((st, s))
    # never readable anywhere: decided on the photographed steps, which span the scroll; a label that is readable at
    # some step is readable, whatever the positions between show
    tallest: dict[str, tuple[Step, dict]] = {}
    for st in v.steps:
        for s in st.subjects or []:
            best = tallest.get(s["name"], (None, {"px": [0, 0]}))[1]["px"][1]
            if s["kind"] == "label" and s["cut"] <= 1 - IN_FRAME_MIN and s["px"][1] > best:
                tallest[s["name"]] = (st, s)
    for name, (st, s) in tallest.items():
        if s["px"][1] < LABEL_MIN_PX:
            found.setdefault(name, {"name": name, "kind": "label", "cut": [], "hidden": []})["small"] = (st, s)
    for name, seen in small_labels(positions).items():
        f = found.setdefault(name, {"name": name, "kind": "label", "cut": [], "hidden": []})
        if "small" not in f:  # never readable anywhere says more than "too small where the shot is about it"
            f["small_at"] = seen
    for f in found.values():
        looks = []
        if f["hidden"]:
            looks.append(max(((s["hidden"], p) for p, s in f["hidden"]), key=lambda t: t[0]))
        if f["cut"]:
            looks.append(max(((s["cut"], p) for p, s in f["cut"]), key=lambda t: t[0]))
        if "small" in f:
            looks.append((0.0, f["small"][0]))
        if f.get("small_at"):
            looks.append((0.0, min(f["small_at"], key=lambda t: t[1])[0]))
        f["look"] = max(looks, key=lambda t: t[0])[1]
        hits = f["cut"] + f["hidden"] + ([f["small"]] if "small" in f else []) + f.get("small_at", [])
        f["first"] = min(p.share for p, _ in hits)
        f["text_in_move"] = any(_text_in_move(p) for p, _ in hits)
    return sorted(found.values(), key=lambda f: (f["kind"] == "label", f["first"], f["name"]))


def extra_frames(v: Visit) -> list[Step]:
    """The positions between the steps where a finding the answer names is at its worst and that have no frame yet, in
    the order the answer names the findings."""
    out: list[Step] = []
    for f in subject_findings(v)[:SUBJECT_LINES]:
        p = f["look"]
        if p.between and not p.frame and all(p is not q for q in out):
            out.append(p)
    return out


def _look_frame(p: Step, v: Visit) -> str:
    """The frame of position ``p``; for a position between the steps that has none (more findings than
    ``BETWEEN_FRAMES``, or no time left), the frame of the photographed step nearest it."""
    if p.frame or not v.steps:
        return p.frame
    return min(v.steps, key=lambda st: abs(st.share - p.share)).frame


def between_levels(n: int) -> list[list[float]]:
    """The positions read between ``n`` photographed steps: the scroll cut into BETWEEN_PER_STEP times as many even
    parts as the steps cut it into, the steps' own positions left out (they are read already), in levels coarse to fine
    (the middles between two steps, then the quarters): a pass cut short by its time allowance has read the whole
    scroll at an even spacing."""
    parts = BETWEEN_PER_STEP * max(n - 1, 1)
    levels: list[list[float]] = []
    seen: set[int] = set()
    stride = BETWEEN_PER_STEP
    while stride > 1:
        half = stride // 2
        level = [k for k in range(half, parts, stride) if k % BETWEEN_PER_STEP and k not in seen]
        seen.update(level)
        levels.append([k / parts for k in level])
        stride = half
    rest = [k / parts for k in range(1, parts) if k % BETWEEN_PER_STEP and k not in seen]
    return [lv for lv in levels + [rest] if lv]


def _near_fault(share: float, positions: list[Step]) -> bool:
    """Whether the position read last before ``share`` or first after it has a subject cut or hidden: within a level the
    pass reads those first, so a fault the steps found gets its stretch of the scroll before a stretch without one
    gets a finer reading."""
    before = [p for p in positions if p.share < share]
    after = [p for p in positions if p.share > share]
    near = (([max(before, key=lambda p: p.share)] if before else [])
            + ([min(after, key=lambda p: p.share)] if after else []))
    return any(any(subject_faults(x)) for p in near for x in p.subjects or [])


async def _bounded(page, js: str, default: Any) -> Any:
    try:
        return await asyncio.wait_for(page.evaluate(js), SUBJECTS_WAIT_S)
    except Exception:  # noqa: BLE001  a page stuck in its own code or one that navigated
        return default


def held_runs(steps: list[Step]) -> list[dict]:
    """Runs of neighbouring steps in which one subject of the shot keeps its box (``HELD_MOVE``), long enough to name
    (``HELD_STEPS``, ``HELD_SHARE``): [{name, kind, steps, moved}], moved being the largest distance, as a share of the
    window, of any edge of the box from where it was at the run's first step. Only entries the page flags
    ``subject: true`` count: without the flag the check cannot tell the subject from a prop standing in the
    background, and says nothing rather than guess."""
    out: list[dict] = []
    open_: dict[str, dict] = {}
    prev_index = None
    for st in sorted(steps, key=lambda s: s.index):
        if prev_index is None or st.index != prev_index + 1:
            out += open_.values()
            open_ = {}
        prev_index = st.index
        now = {x["name"]: x for x in _about(st)}
        for name in list(open_):
            r = open_[name]
            x = now.get(name)
            moved = max(abs(a - b) for a, b in zip(x["box"], r["box"])) if x else None
            if moved is None or moved > HELD_MOVE:
                out.append(open_.pop(name))
            else:
                r["steps"].append(st)
                r["moved"] = max(r["moved"], moved)
        for name, x in now.items():
            if name not in open_:
                open_[name] = {"name": name, "kind": x["kind"], "box": x["box"], "steps": [st], "moved": 0.0}
    out += open_.values()
    return [{k: r[k] for k in ("name", "kind", "steps", "moved")} for r in out
            if len(r["steps"]) >= HELD_STEPS and r["steps"][-1].share - r["steps"][0].share >= HELD_SHARE - 1e-9]


def subject_scale(steps: list[Step]) -> dict[int, float]:
    """Step index -> how much of the window the shot's subject fills there: the union of the boxes the page flags
    ``subject: true`` (labels aside), clamped to the window, the larger of its share of the height and of the width.
    Steps that flag no subject are left out (an establishing shot names none, and the check does not guess)."""
    out = {}
    for st in steps:
        boxes = [x["box"] for x in _about(st)]
        if not boxes:
            continue
        x0, y0 = max(0.0, min(b[0] for b in boxes)), max(0.0, min(b[1] for b in boxes))
        x1, y1 = min(1.0, max(b[2] for b in boxes)), min(1.0, max(b[3] for b in boxes))
        if x1 > x0 and y1 > y0:
            out[st.index] = max(x1 - x0, y1 - y0)
    return out


def small_labels(steps: list[Step]) -> dict[str, list[tuple[Step, int]]]:
    """Labels too small to read at a step where the shot is about them: name -> [(step, height in px)]. A label counts
    at a step where it is in the frame and not cut or hidden enough to be named for that (``LABEL_CUT``,
    ``LABEL_HIDDEN``: those lines say so already), is under ``LABEL_MIN_PX`` tall, and is the shot's: flagged
    ``subject: true``, or shown at a step that names no subject at all on a page that names one at other steps. The
    skill's starter says so of such a shot ("without one, every prop counts as a subject"): an establishing shot of
    the whole scene, which run 3 ended with, its three labels shown on purpose at 11 to 15 px. A label of a prop the
    shot is not about is its background there and is not named; on a page that never names a subject only the rule
    of ``LABEL_MIN_PX`` (never readable anywhere) applies, as before. Nor is one where the page says its camera is
    between two shots and no chapter text is readable (``subject_faults``)."""
    if not _flags_subjects(steps):
        return {}
    out: dict[str, list[tuple[Step, int]]] = {}
    for st in steps:
        subs = st.subjects or []
        names_none = all(x.get("subject") is None for x in subs)
        for s in subs:
            if s["kind"] != "label" or _in_transit(s) or not (s.get("subject") is True or names_none):
                continue
            hidden = s.get("hidden")
            if s["cut"] < LABEL_CUT and (hidden is None or hidden < LABEL_HIDDEN) and s["px"][1] < LABEL_MIN_PX:
                out.setdefault(s["name"], []).append((st, s["px"][1]))
    return out


def _text_in_move(p: Step) -> bool:
    """Whether at this position the page says its camera is moving while chapter text is readable."""
    return p.moving and p.text is not None and p.text >= TEXT_READABLE


def _in_transit(s: dict) -> bool:
    """Whether a measured subject is excused as a picture on its way: the page says its camera is between two shots
    there and no chapter text is readable (``subject_faults``)."""
    return bool(s.get("moving")) and not s.get("text_up")


def subject_faults(s: dict) -> tuple[bool, bool]:
    """(cut by the frame's edge, hidden under the page's HTML) for one measured subject at one step. Neither where the
    page says its camera is between two shots (``moving``) and no chapter text is readable there (``text_up``,
    ``TEXT_READABLE``): a camera on its way is no fault where nothing asks the visitor to stop and read. Where the text
    is up, a moving camera excuses nothing. Run 4's site and run 5's are the two sides of it. Run 4's site at 38 % of
    the scroll had the new chapter's text up while the camera was still on its way to that chapter's subject, and the
    check called the subject out of the frame; its stations stand on one line and the camera slides along it, so the
    frame in transit still looked composed, and a blind judge preferred it. So the check was told to say nothing where
    the page says its camera is moving, and on run 5's site, whose stations stand at angles, it said nothing at 62 %:
    the card of chapter 04 fully up, the cabinet it had left half under the card and the machine it was going to and
    its label cut by the frame's right edge, the frame a blind judge named in all six verdicts against it. A visitor
    stops scrolling where new text appears; what the frame shows there is what they see."""
    if s["cut"] > 1 - IN_FRAME_MIN or s.get("background") or _in_transit(s):
        return False, False
    label = s["kind"] == "label"
    hidden = s.get("hidden")
    return (s["cut"] >= (LABEL_CUT if label else PROP_CUT),
            hidden is not None and hidden >= (LABEL_HIDDEN if label else PROP_HIDDEN))


def _rel(p: str, base: Path) -> str:
    if not p:
        return "(none)"
    with contextlib.suppress(ValueError):
        return str(Path(p).relative_to(base))
    return p


def _short(url: str, v: Visit) -> str:
    base = (v.probe or {}).get("base") or ""
    name = url[len(base):] if base and url.startswith(base) else urlsplit(url).path.rsplit("/", 1)[-1]
    return name or "the page itself"


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


def picture_shift(a: bytes, b: bytes) -> float | None:
    """About how far a picture moved between two frames of one size, in pixels.

    The pixels that flipped, per place where a row or a column of the first frame crosses an edge. A pixel flipped
    when its brightness changed by more than ``SHIFT_LEVELS`` and by more than half of the contrast around it (the
    range of its 3 x 3 neighbourhood, in either frame). An edge crossing is a run of neighbours along a row or a
    column that step the same way by more than ``SHIFT_LEVELS``, so a soft edge counts once however wide it is drawn;
    the count is the mean of the rows' and the columns'.

    An edge that moves d pixels flips a band d pixels wide, so a picture that moves d pixels in any direction reads
    about d: an anti-aliased disc moved 1, 2, 4 and 8 px reads 0.7, 1.7, 3.7 and 7.6 (the band's soft rim does not
    flip). An edge that moves under half a pixel changes its own pixels by under half their contrast and flips none:
    a picture that only shimmers reads 0, whatever its contrast. Text that stays put adds crossings and no flips,
    which is why the 3D canvas is also read alone; something that appears counts as change. None for frames of two
    sizes, or a first frame with next to no edges (``SHIFT_MIN_EDGES`` crossings per pixel): an empty picture, which
    other findings name."""
    from PIL import Image

    x, y = (np.asarray(Image.open(io.BytesIO(f)).convert("L"), dtype=np.float32) for f in (a, b))
    if x.shape != y.shape:
        return None

    def crossings(axis: int) -> int:
        d = np.diff(x, axis=axis)
        step = np.sign(d) * (np.abs(d) > SHIFT_LEVELS)
        before = np.roll(step, 1, axis=axis)
        before[(slice(None), 0) if axis else 0] = 0
        return int(((step != 0) & (step != before)).sum())

    edges = (crossings(0) + crossings(1)) / 2
    if edges < SHIFT_MIN_EDGES * x.size:
        return None

    def spread(f: np.ndarray) -> np.ndarray:
        h, w = f.shape
        pad = np.pad(f, 1, mode="edge")
        near = [pad[i:i + h, j:j + w] for i in range(3) for j in range(3)]
        return np.maximum.reduce(near) - np.minimum.reduce(near)

    flipped = np.abs(y - x) > np.maximum(SHIFT_LEVELS, 0.5 * np.maximum(spread(x), spread(y)))
    return float(flipped.sum()) / edges


def first_notch_px(v: Visit) -> float | None:
    """How far the first wheel notch moved the picture, the larger of the window's reading and the 3D canvas's; None:
    not measured, or the page kept changing by itself, so that what the notch changed could not be told from it."""
    n = v.first_notch or {}
    read = [x for x in (n.get("window"), n.get("canvas")) if x is not None]
    return max(read) if read and n.get("motion") == "still" else None


def first_notch_unanswered(v: Visit) -> bool:
    """The first wheel notch was measured, moved the picture by less than ``NOTCH_MIN_PX`` and left the page's text
    where it was."""
    px = first_notch_px(v)
    return px is not None and px < NOTCH_MIN_PX and ((v.first_notch or {}).get("text") or 0.0) < NOTCH_TEXT_MOVED


def canvas_content(jpeg: bytes) -> float:
    """Share of a canvas-only frame that shows something other than its background: pixels of the 1/8-size image that
    differ from the frame's most common colour (quantised to 16 levels per channel) by more than 8 % of full scale in
    any channel. Averaging to 1/8 size first lets a 1 px helper grid fade into the background; an object stays."""
    from PIL import Image

    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im = im.reduce(8) if min(im.size) >= 128 else im
    a = np.asarray(im, dtype=np.int16)
    far = (np.abs(a.reshape(-1, 3) - _background(a)) > 0.08 * 255).any(axis=1)
    return float(far.mean())


def _background(a: np.ndarray) -> np.ndarray:
    """The most common colour of an RGB array (quantised to 16 levels per channel; the mean of the pixels in that bin),
    as ``canvas_content`` finds it."""
    q = (a // 16).reshape(-1, 3)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    return a.reshape(-1, 3)[keys == np.bincount(keys).argmax()].mean(axis=0)


def subject_contrast(jpeg: bytes, boxes: list[list[float]], rect: dict | None = None,
                     window: tuple[int, int] | None = None) -> float | None:
    """How far what stands where the shot's subject is lies from the canvas's background: on the 1/8-size canvas-only
    frame, each pixel's largest difference to the background over the three channels (share of full scale), and of
    the pixels inside the subject's box the 75th percentile. Three quarters of what stands there is within this much
    of the background's colour. None without boxes, or when the box, clipped to the frame, is under
    ``CONTRAST_MIN_BOX`` of it either way.

    The subject's box is the one box round all of ``boxes`` (fractions of the window), as ``subject_scale`` takes it:
    a shot about a prop and the layer beside it is about the ground between them too. Taking only the boxes
    themselves measured run 4's phone 3 to 4 points higher at four steps (a prop's box hugs its lit faces), and the
    line, which the judge's verdicts bear out on both sizes, would not have been said on the phone.

    ``rect`` is where the canvas-only frame lies in the window (viewport px, as ``stepEnd`` gives it) and ``window`` the
    window's size; without them the frame is taken to fill the window, as a fixed full-screen canvas does."""
    from PIL import Image

    if not boxes:
        return None
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    if rect and window and rect.get("width") and rect.get("height"):
        x0, x1 = ((x * window[0] - rect["x"]) / rect["width"] for x in (x0, x1))
        y0, y1 = ((y * window[1] - rect["y"]) / rect["height"] for y in (y0, y1))
    x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)
    if x1 - x0 < CONTRAST_MIN_BOX or y1 - y0 < CONTRAST_MIN_BOX:
        return None
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im = im.reduce(8) if min(im.size) >= 128 else im
    a = np.asarray(im, dtype=np.int16)
    h, w = a.shape[:2]
    inside = a[int(y0 * h):max(int(y0 * h) + 1, round(y1 * h)), int(x0 * w):max(int(x0 * w) + 1, round(x1 * w))]
    dist = np.abs(inside - _background(a)).max(axis=2) / 255.0
    return float(np.percentile(dist, 75))


def largest_shape(jpeg: bytes) -> float:
    """Share of what a canvas-only frame shows that is one solid shape: the largest connected region of the content mask
    (1/4 size, same rule as ``canvas_content``) after an opening of two pixels, over the whole mask. A subject composed small is one
    shape and scores high (the skill's starter, its wide closing shot: 0.90 to 0.98); what is left when the subject has
    gone, grid lines and an arc, scores lower (run 1 at 62 %: 0.53 to 0.63 over three checks). It is no verdict: the
    film's opening frame, composed on purpose of many small things, scores 0.26. So it is given as a number beside the
    finding and the agent looks at the frame."""
    from PIL import Image
    from scipy import ndimage

    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im = im.reduce(4) if min(im.size) >= 64 else im
    a = np.asarray(im, dtype=np.int16)
    q = (a // 16).reshape(-1, 3)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    bg = a.reshape(-1, 3)[keys == np.bincount(keys).argmax()].mean(axis=0)
    far = (np.abs(a - bg) > 0.08 * 255).any(axis=2)
    if not far.any():
        return 0.0
    labels, n = ndimage.label(ndimage.binary_opening(far, iterations=2))  # thin lines go, a shape keeps its size
    return float(np.bincount(labels.ravel())[1:].max() / far.sum()) if n else 0.0


def sheet_flags(s: Step, held: set[int], small: set[int]) -> list[str]:
    """The few words the contact sheet writes under a step's frame, one per thing found there; ``held`` and ``small``
    are the indices of the steps in a held run and with a label too small. "text mid-move": a subject is named at a
    step where the page says its camera is moving while chapter text is up (``subject_faults``)."""
    faults = [subject_faults(x) for x in s.subjects or []]
    return [f for f, on in (("3D empty", s.canvas_content is not None and s.canvas_content < EMPTY_CONTENT),
                            ("unchanged", s.unchanged), ("still moving", s.motion == "moving"),
                            ("sideways", s.sideways_px > 1), ("subject hidden", any(h for _, h in faults)),
                            ("subject cut", any(c for c, _ in faults)), ("subject held", s.index in held),
                            ("label small", s.index in small),
                            ("blends in", s.contrast is not None and s.contrast < CONTRAST_LOW),
                            ("text mid-move", _text_in_move(s) and any(c or h for c, h in faults))) if on]


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


# A passage of a longer text counts as that text from this many characters on. Run 2 of the website task (2026-11-24):
# the page quoted the first five lines of a 400-character digest, 130 characters and exact; the rule then wanted half
# of the value, the check said "broken", and the agent took Straitjacket's real digest off the page.
QUOTE_MIN = 20


def _passage_mismatch(shown: str, want: str, where: str) -> str:
    """Why ``shown`` is not the text at ``where`` (``want``), in the words that say what to change: too short a passage
    to tell, a quote that leaves the text part-way (and where), or another text altogether."""
    if shown and shown in want:
        return (f"the page shows only {_q(shown, 60)}, {len(shown)} of the {len(want)} characters at {where}: too little to "
                f"tell that it is this value. Show an unbroken passage of at least {QUOTE_MIN} characters")
    same = len(os.path.commonprefix([shown, want]))
    if same >= 12:
        return (f"the page agrees with {where} for {same} characters, then goes on {_q(shown[same:], 40)} where the file has "
                f"{_q(want[same:], 40)}. A quote is one unbroken passage of the value: nothing left out in the middle")
    return f"the page shows {_q(shown, 60)} where {where} holds {_q(want, 60)}"


def check_fact(src: str, shown: str, sources: Path | None) -> str | None:
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
                want = _norm(value)
                # The whole value, or one unbroken passage of it long enough not to be there by chance.
                if want in shown_n or (shown_n in want and len(shown_n) >= min(QUOTE_MIN, 0.5 * len(want))):
                    return None
                return _passage_mismatch(shown_n, want, f"{path}#{frag}")
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
        why = check_fact(el["src"], el["text"], sources)
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
    src: el.getAttribute('data-src'), text: el.textContent, sel: sel(el) }));
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
  // short visible texts that speak of WebGL, a fallback, or 3D / graphics being unavailable (the no-WebGL visit's
  // message is one of them that the page with WebGL does not show)
  const notices = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length || !vis(el)) continue;
    const t = el.textContent.trim().replace(/\s+/g, ' ');
    if (t.length < 200 && /webgl|fallback|\b3d\b.{0,30}\b(unavailable|not available|off|disabled|not supported)|graphics (card|acceleration)|hardware acceleration/i.test(t)) notices.push(t);
    if (notices.length >= 20) break;
  }
  const images = [...document.querySelectorAll('img, svg, picture, video')].filter((el) => vis(el) && el.getBoundingClientRect().width > 48 && !el.closest('svg svg')).length;
  // what the page says of its ambient occlusion (the skill's starter: "on ..." or "off: <why>"), where it says it
  let occlusion = null;
  try { const i = window.__site && typeof window.__site.info === 'function' ? window.__site.info() : null; if (i && typeof i.occlusion === 'string') occlusion = i.occlusion.slice(0, 160); } catch (e) {}
  return { contexts: (S.contexts || []).length, lost: S.lost || 0, webgl2: (S.contexts || []).some((c) => window.WebGL2RenderingContext && c instanceof WebGL2RenderingContext),
           webgpu: !!S.webgpu, offscreen: !!S.offscreen, reducedMotionQueried: !!S.reducedMotionQueried, reducedMotionCss: css,
           memory: S.memory ? S.memory() : null, textChars: (document.body && document.body.innerText || '').length,
           layer: S.layer || null, layerLeft: S.layerLeft || 0, occlusion,
           notices, images, base: location.href.replace(/[^/]*([?#].*)?$/, '') };
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

    async def shot_png(self, clip: dict | None = None) -> bytes:
        """The viewport, or ``clip`` of it, without JPEG's noise: for a measure that counts pixels (``picture_shift``)."""
        import base64

        data = base64.b64decode((await self.cdp.send("Page.captureScreenshot", {"format": "png"}))["data"])
        if not clip:
            return data
        from PIL import Image

        box = (round(clip["x"]), round(clip["y"]), round(clip["x"] + clip["width"]), round(clip["y"] + clip["height"]))
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).crop(box).save(buf, "PNG", compress_level=1)
        return buf.getvalue()

    async def first_notch(self, page, rect: dict | None) -> None:
        """What one wheel notch from the top of the page changes: called with the page settled and still at its top.

        Two readings of ``picture_shift``: the window as a visitor sees it, and the 3D canvas alone (``rect``: where
        it is; read as ``canvas_alone`` reads it), because a panel of text that stays put adds edges and no change. A
        page that answers with its HTML (text that scrolls) shows in the first, a page that answers with its scene in
        the second.

        Why: on every site built on the skill's starter the first chapter of the page was one long hold whose
        camera started from rest, and the first notch moved the picture by a fraction of a pixel. Their checks said
        nothing; the stock agent's page, a third of whose window the same notch changes, was the livelier at the
        first touch. Read at 1440 x 900, the canvas alone and then the window: the sites of runs 2 to 5 0.07, 0.11,
        0.32 and 0.15 (0.03, 0.05, 0.14 and 0.07); run 5 rebuilt with a camera curve that starts at speed 2.46 and
        1.13, and with the starter's opening move as well 8.83 and 3.97; the skill's starter 0.02 and 0.00 before
        its opening move, 9.1 and 1.41 with it (benchmarks/results/round8/first_scroll). The stock agent's page is
        not read: its top keeps changing by itself."""
        import base64

        async def frames() -> tuple[bytes, bytes | None]:
            window, alone = await self.shot_png(), None
            if rect:
                with contextlib.suppress(Exception):  # no canvas frame: the window's reading alone
                    url = await asyncio.wait_for(page.evaluate("window.__agySite.grabCanvas(3, 'image/png')"), 5.0)
                    if url:
                        alone = base64.b64decode(url.split(",", 1)[1])
                    else:  # a page that draws only on change: its HTML hidden for one photograph
                        await page.evaluate("window.__agySite.stepEnd(true)")
                        try:
                            alone = await self.shot_png(rect)
                        finally:
                            await page.evaluate("window.__agySite.unhide()")
            return window, alone

        with contextlib.suppress(Exception):  # what the step's own reads set off (a hidden page shown again) has drawn
            await asyncio.wait_for(page.evaluate("(n) => window.__agySite.afterFrames(n)", 2), 3.0)
        top = await frames()
        seen = [top[0]]
        for wait in (0.15, 0.25):  # two more looks, unevenly apart: a motion that repeats does not match twice
            await asyncio.sleep(wait)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(page.evaluate("(n) => window.__agySite.afterFrames(n)", 2), 3.0)
            seen.append(await self.shot_png())
        if max(picture_shift(seen[i], seen[j]) or 0.0 for i, j in ((0, 1), (1, 2), (0, 2))) > NOTCH_IDLE_PX:
            # it moves without a scroll: what the notch changed could not be told from that
            self.v.first_notch = {"px": NOTCH_PX, "motion": "ambient"}
            return
        noted = await _bounded(page, "window.__agySite.textPlaces()", None)
        await self.scroll_to(page, NOTCH_PX)
        _shot, secs, motion = await self.settle(page, SETTLE_CAP_S)
        self.v.first_notch = {"px": NOTCH_PX, "motion": motion}
        if motion == "still":
            on = await frames()
            window = picture_shift(top[0], on[0])
            alone = picture_shift(top[1], on[1]) if top[1] and on[1] else None
            self.v.first_notch.update(window=None if window is None else round(window, 2),
                                      canvas=None if alone is None else round(alone, 2))
            text = await _bounded(page, f"window.__agySite.textMoved({NOTCH_TEXT_PX})", None) if noted else None
            if isinstance(text, dict) and text.get("chars"):
                self.v.first_notch["text"] = round(min(1.0, float(text.get("moved", 0)) / float(text["chars"])), 2)
        with contextlib.suppress(Exception):  # the frames drawn here are not the next step's
            await page.evaluate("void window.__agySite.mark()")
        self.log(f"first notch: {self.v.first_notch} after {secs:.1f} s")

    # ---- waiting
    async def settle(self, page, cap: float) -> tuple[bytes, float, str]:
        """Take frames until the picture stops changing. Returns (last frame, seconds, how): 'still' (two frames agree),
        'ambient' (after a second the change from frame to frame stopped shrinking: what the scroll set in motion is
        over and what remains is the page's own continuous animation), or 'moving' (still changing at the cap).

        Two animation frames of the page pass before every screenshot: on run 1's page two screenshots taken without
        one between them were the same compositor frame, so the picture "stopped changing" before the scene had moved,
        and the 62 % frame showed an empty grid where the scene, a moment later, showed the router."""
        t0 = time.monotonic()
        left = self.clock.left() - WRAP_UP_S
        cap, hard = max(0.5, min(cap, left)), max(0.5, min(max(cap, SETTLE_HARD_S), left))
        prev, last_d, shot, arrived = None, None, b"", None
        self.changing_s = 0.0  # of this settle: until when, after the page drew here, a frame still differed from the last
        while True:
            with contextlib.suppress(Exception):  # a page that navigated or never ran the probe: take the frame anyway
                await asyncio.wait_for(page.evaluate("(n) => window.__agySite.afterFrames(n)", 2), max(0.5, hard if arrived is None else cap))
            if arrived is None:
                arrived = time.monotonic() - t0  # the page has drawn here: what the scroll set off starts now
            shot = await self.shot()
            small = _grey(shot, 8)
            el = time.monotonic() - t0
            if prev is not None:
                d = _diff(prev, small)
                self.log(f"  frame change {d:.4f} at {el:.1f} s (drawn here after {arrived:.1f} s)")
                if d < STILL:
                    return shot, el, "still"
                self.changing_s = max(0.0, el - arrived)
                if last_d is not None and el - arrived >= 1.0 and d >= 0.7 * last_d and d < AMBIENT:
                    return shot, el, "ambient"
                last_d = d
            if el - arrived > cap or el > hard:
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
        await self.settle(page, 4.0)
        self.v.load_s = round(time.monotonic() - t0, 2)
        self.v.loaded = True
        self.log("settled after loading")
        return True

    async def idle(self, page, seconds: float = 1.5) -> dict:
        """What the page does while nothing is touched: frames and draws counted over ``seconds``, and whether the
        picture changed. Three pictures at uneven intervals, not two: a motion that repeats can show the same picture
        at both ends of one interval (the test page's colour cycle of 0.94 s did, whenever a loaded machine stretched
        the 1.5 s between two pictures to twice that, and the check then called a moving page still)."""
        a = await self.shot()
        await page.evaluate("window.__agySite && window.__agySite.mark()")
        await asyncio.sleep(seconds * 0.4)
        m = await self.shot()
        await asyncio.sleep(seconds * 0.6)
        w = await page.evaluate("window.__agySite ? window.__agySite.mark() : null") or {"frames": 0, "drawn": 0}
        b = await self.shot()
        ga, gm, gb = _grey(a, 8), _grey(m, 8), _grey(b, 8)
        changed = max(_diff(ga, gm), _diff(gm, gb), _diff(ga, gb)) >= STILL
        return {"seconds": seconds, "frames": w["frames"], "drawn": w["drawn"], "changed": changed}

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

    async def subjects(self, page, js: str = "window.__agySite.subjects()",
                       wait: float = SUBJECTS_WAIT_S) -> tuple[list[dict] | None, str, int | None]:
        """The page's subjects as it shows them now, measured in the page (``site_probe.js``, ``S.subjects``), why not
        when they cannot be, and the characters of chapter text readable over the canvas (``TEXT_READABLE``). The page's
        hook runs inside this call: it is bounded in time here and in what it returns there, and whatever comes back is
        checked again, since the page can replace anything it is given. Where the page says its camera is between two
        shots (``moving``), every entry carries ``moving: True`` and ``text_up``, whether chapter text is readable."""
        try:
            got = await asyncio.wait_for(page.evaluate(js), wait)
        except Exception as e:  # noqa: BLE001  a hook that hangs or a page that navigated: the step is kept without it
            return None, f"not read ({type(e).__name__})", None
        if not isinstance(got, dict):
            return None, "not read", None
        if not isinstance(got.get("list"), list):
            return None, str(got.get("why") or "")[:200], None
        out = [x for x in (_subject(r) for r in got["list"][:SUBJECTS_READ]) if x]
        # Where the page says which props the shot is about, the others are its background and are measured but not
        # named: on run 2's site a prop of an earlier chapter, left standing behind the next chapter's panel, was
        # reported at five steps, which a person looking at those frames would not call a fault. Labels always count.
        if any(x["subject"] is True for x in out):
            for x in out:
                x["background"] = x["kind"] != "label" and x["subject"] is not True
        text = got.get("text")
        text = int(text) if isinstance(text, (int, float)) and not isinstance(text, bool) and 0 <= text < 1e7 else None
        text_up = text is not None and text >= TEXT_READABLE
        if got.get("moving") is True:
            for x in out:
                x["moving"] = True
                x["text_up"] = text_up
        note = (f"{len(out)} subject{_s(out)} with a box" + (", more not read" if got.get("more") else "")
                + (("; the camera is between two shots" + (f", {text} characters of text readable" if text_up else ""))
                   if got.get("moving") is True else ""))
        return (out or None), note, text

    async def stills(self, page) -> dict:
        """One still per chapter the page names (``site_probe.js``, ``chapterList`` and ``stillAt``), written as
        ``stills/<name>.webp`` in the output folder. Bounded in count, in time per chapter and in all; the ids become
        file names that cannot leave the folder or overwrite one another."""
        t0 = time.monotonic()
        try:
            got = await asyncio.wait_for(page.evaluate("window.__agySite.chapterList()"), SUBJECTS_WAIT_S)
        except Exception as e:  # noqa: BLE001  a hook that hangs or a page that navigated: no stills, and why
            return {"note": f"chapters not read ({type(e).__name__})"}
        if not isinstance(got, dict) or not isinstance(got.get("list"), list):
            return {"note": str(got.get("why") or "")[:200] if isinstance(got, dict) else "chapters not read"}
        chapters = [c for c in got["list"][:STILLS_READ] if isinstance(c, dict) and isinstance(c.get("id"), str)
                    and isinstance(c.get("rest"), (int, float)) and not isinstance(c.get("rest"), bool) and math.isfinite(c["rest"])]
        if not chapters:
            return {"note": "chapters() gave no { id, rest }"}
        folder = (self.out / "stills").resolve()
        folder.mkdir(exist_ok=True)
        made, skipped = [], []
        # The page is told it is drawing stills from before the first until after the last, also when one fails: a
        # page may then frame the shot's subject in the middle of the whole canvas instead of beside its text panel
        # (the shared contract of round 8; the starter does). The visit ends with the stills, so nothing measured
        # after them could see the attribute, and it is taken away all the same.
        await _still_mode(page, True)
        try:
            for c, name in zip(chapters, still_names([c["id"] for c in chapters])):
                left = min(STILLS_S - (time.monotonic() - t0), self.clock.left() - WRAP_UP_S - 2.0)
                if left < 1.0:
                    skipped.append({"id": c["id"], "why": "no time left"})
                    continue
                try:
                    r = await asyncio.wait_for(page.evaluate("(T) => window.__agySite.stillAt(T)", c["rest"]),
                                               min(STILL_WAIT_S, left))
                except Exception as e:  # noqa: BLE001
                    skipped.append({"id": c["id"], "why": f"not drawn in time ({type(e).__name__})"})
                    continue
                data = r.get("data") if isinstance(r, dict) else None
                if not isinstance(data, str) or not data.startswith("data:image/") or len(data) > 64_000_000:
                    why = r.get("why") if isinstance(r, dict) else "no picture"
                    skipped.append({"id": c["id"], "why": str(why)[:160]})
                    continue
                f = folder / f"{name}.webp"
                # still_names makes this impossible; the folder is the one place written
                if f.resolve().parent != folder:
                    skipped.append({"id": c["id"], "why": "no safe file name"})
                    continue
                try:
                    webp, size = await asyncio.to_thread(still_webp, data)
                except Exception as e:  # noqa: BLE001  a picture Pillow cannot read
                    skipped.append({"id": c["id"], "why": f"unreadable picture ({type(e).__name__})"})
                    continue
                f.write_bytes(webp)
                made.append({"id": c["id"], "file": f.name, "rest": c["rest"], "width": size[0], "height": size[1],
                             "bytes": len(webp), "content": round(canvas_content(webp), 4),
                             "how": str(r.get("how", ""))[:40]})
        finally:
            await _still_mode(page, False)
        if got.get("more"):
            skipped.append({"id": f"chapters after the first {STILLS_READ}", "why": "not read"})
        if not made:
            with contextlib.suppress(OSError):
                folder.rmdir()
        return {"dir": str(folder), "made": made, "skipped": skipped}

    async def canvas_alone(self, page, rect: dict) -> bytes:
        """The largest WebGL canvas in view without the page's HTML: read from its drawing buffer after the page's next
        frame (``site_probe.js``, ``grabCanvas``), or, for a page that draws only on change, photographed with every
        other element hidden."""
        import base64

        url = await page.evaluate("window.__agySite.grabCanvas(3)")
        if url:
            return base64.b64decode(url.split(",", 1)[1])
        await page.evaluate("window.__agySite.stepEnd(true)")
        try:
            return await self.shot(rect)
        finally:
            await page.evaluate("window.__agySite.unhide()")

    async def steps(self, page, n: int, canvas: bool, sideways: bool, notch: bool = False) -> None:
        geo = await page.evaluate("window.__agySite.geometry()")
        if geo["inner"]:
            self.v.notes.append("the page scrolls inside an element, not the window")
        for i in step_order(n):
            if self.clock.left() < WRAP_UP_S + 2.0:
                break
            share = i / max(n - 1, 1)
            how = await self.scroll_to(page, round(geo["max"] * share))
            shot, secs, motion = await self.settle(page, SETTLE_CAP_S)
            end = await page.evaluate("window.__agySite.stepEnd(false)")
            w = end["win"]
            step = Step(i, share, round(end["y"]), settle_s=round(secs, 2), changing_s=round(self.changing_s, 2),
                        motion=motion, reached_by=how,
                        draws=int(w.get("maxDraws", 0)), triangles=int(w.get("maxTris", 0)),
                        shown_triangles=int((end.get("last") or {}).get("tris", 0)))
            step.subjects, step.subjects_note, step.text = await self.subjects(page)
            step.moving = any(x.get("moving") for x in step.subjects or [])
            f = self.out / f"{self.v.name}_{i:02d}.jpg"
            f.write_bytes(shot)
            step.frame = str(f)
            if canvas and end["rect"]:
                cshot = await self.canvas_alone(page, end["rect"])
                cf = self.out / f"{self.v.name}_{i:02d}_canvas.jpg"
                cf.write_bytes(cshot)
                step.canvas_frame = str(cf)
                step.canvas_content = round(canvas_content(cshot), 4)
                step.shape = round(largest_shape(cshot), 3)
                if about := _about(step):
                    vp = page.viewport_size or {}
                    c = subject_contrast(cshot, [x["box"] for x in about], end["rect"],
                                         (vp.get("width", 0), vp.get("height", 0)))
                    step.contrast = None if c is None else round(c, 4)
            if sideways:
                step.sideways_px = int(end["sideways"]["over"])
                for c in end["sideways"]["culprits"]:
                    if c not in self.v.overflow:
                        self.v.overflow.append(c)
            self.v.steps.append(step)
            self.log(f"step {i}: {how}, {motion} after {secs:.1f} s")
            if i == 0 and notch and geo["max"] >= 2 * NOTCH_PX and self.clock.left() > WRAP_UP_S + SETTLE_CAP_S + 4.0:
                if motion == "still":
                    await self.first_notch(page, end["rect"] if canvas else None)
                else:  # the top of the page changes by itself: a notch's answer cannot be told from that
                    self.v.first_notch = {"px": NOTCH_PX, "motion": motion}
        self.v.steps.sort(key=lambda st: st.index)
        for a_, b_ in zip(self.v.steps, self.v.steps[1:]):
            if b_.index == a_.index + 1:
                b_.unchanged = _diff(_grey(Path(a_.frame).read_bytes(), 4), _grey(Path(b_.frame).read_bytes(), 4)) < UNCHANGED

    async def between(self, page, n: int) -> None:
        """Read the page's subjects between the photographed steps (``BETWEEN_PER_STEP``): the scroll is read at
        ``between_levels(n)``, coarse to fine (within a level next to a known fault first, ``_near_fault``), without a
        screenshot or a wait for the picture to settle, until ``BETWEEN_S`` is used up. Run 4's agent checked with
        twelve steps and was told of no subject out of the frame; nine steps named six at two positions: what the check
        found depended on where it happened to stop. A page without the hook has no subjects and is not read. Then one
        frame at each of at most ``BETWEEN_FRAMES`` positions where a finding is at its worst between the steps
        (``extra_frames``)."""
        if not any(st.subjects for st in self.v.steps):
            return
        geo = await _bounded(page, "window.__agySite.geometry()", None)
        if not isinstance(geo, dict):
            return
        levels = between_levels(n)
        self.v.between_planned = sum(len(lv) for lv in levels)
        t0 = time.monotonic()
        for level in levels:
            for share in sorted(level, key=lambda sh: (not _near_fault(sh, _positions(self.v)), sh)):
                left = min(BETWEEN_S - (time.monotonic() - t0), self.clock.left() - WRAP_UP_S - 2.0)
                if left < 0.5:
                    break
                await self.scroll_to(page, round(geo["max"] * share))
                subs, note, text = await self.subjects(page, f"window.__agySite.readBetween({BETWEEN_TRANSITION_MS})",
                                                       min(left, SETTLE_HARD_S))
                if subs is None and (time.monotonic() - t0 >= BETWEEN_S - 0.1
                                     or self.clock.left() < WRAP_UP_S + 2.1):
                    break  # cut off by the allowance, not a position the page could not answer at
                y = await _bounded(page, "window.__agySite.y()", 0)
                self.v.between.append(Step(-1, share, round(y or 0), subjects=subs, subjects_note=note, between=True,
                                           moving=any(x.get("moving") for x in subs or []), text=text))
        self.v.between.sort(key=lambda st: st.share)
        self.v.between_s = round(time.monotonic() - t0, 2)
        self.log(f"read {len(self.v.between)} of {self.v.between_planned} positions between the steps "
                 f"in {self.v.between_s:.1f} s")
        for k, st in enumerate(extra_frames(self.v)[:BETWEEN_FRAMES], 1):
            if self.clock.left() < WRAP_UP_S + 6.0:
                break
            await self.scroll_to(page, round(geo["max"] * st.share))
            shot, _, _ = await self.settle(page, SETTLE_CAP_S)
            f = self.out / f"{self.v.name}_x{k}.jpg"
            f.write_bytes(shot)
            st.frame = str(f)


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


async def _still_mode(page, on: bool) -> None:
    """Set or remove ``STILL_ATTR`` on the page's <html>, bounded in time (a page stuck in its own code cannot answer,
    and the stills that follow are then skipped for want of time anyway)."""
    name = json.dumps(STILL_ATTR)
    js = (f"document.documentElement.setAttribute({name}, '')" if on
          else f"document.documentElement.removeAttribute({name})")
    with contextlib.suppress(Exception):
        await asyncio.wait_for(page.evaluate(js), SUBJECTS_WAIT_S)


def still_names(ids: list[str]) -> list[str]:
    """A file name (without .webp) per chapter id: its letters, digits, '-' and '_' (anything else becomes '-'), at most
    STILL_NAME_CHARS long, never empty, never a name Windows reserves, and distinct even where the file system ignores
    case ('Intro' and 'intro', or 'a/b' and 'a-b', get 'intro' and 'intro-2')."""
    out: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(ids):
        n = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw)).strip("-_")[:STILL_NAME_CHARS].strip("-_") or f"chapter-{i + 1}"
        if n.lower() in _RESERVED:
            n = f"chapter-{n}"
        base, k = n, 2
        while n.lower() in seen:
            n, k = f"{base}-{k}", k + 1
        seen.add(n.lower())
        out.append(n)
    return out


def still_webp(data_url: str) -> tuple[bytes, tuple[int, int]]:
    """A canvas read back as a data URL, made at most STILL_WIDTH wide (never wider than it was), as WebP."""
    import base64

    from PIL import Image

    im = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))).convert("RGB")
    if im.width > STILL_WIDTH:
        im = im.resize((STILL_WIDTH, round(im.height * STILL_WIDTH / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=STILL_QUALITY, method=STILL_METHOD)
    return buf.getvalue(), im.size


def _subject(r: Any) -> dict | None:
    """One measured subject, as the page handed it back, with every field checked (None when it is not one)."""
    def num(v: Any, lo: float = -1000.0, hi: float = 1000.0) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi else None

    if not isinstance(r, dict) or not isinstance(r.get("name"), str):
        return None
    box = r.get("box")
    cut, hidden = num(r.get("cut"), 0, 1), num(r.get("hidden"), 0, 1)
    if not isinstance(box, list) or len(box) != 4 or any(num(b) is None for b in box) or cut is None:
        return None
    edges = r.get("edges") if isinstance(r.get("edges"), dict) else {}
    px = r.get("px") if isinstance(r.get("px"), list) and len(r.get("px")) == 2 else [0, 0]
    return {"name": r["name"][:80], "kind": r["kind"] if r.get("kind") in ("label", "layer") else "prop",
            "subject": r.get("subject") if isinstance(r.get("subject"), bool) else None, "box": [float(b) for b in box],
            "px": [int(num(p, 0, 1e6) or 0) for p in px], "cut": cut,
            "edges": {k: v for k in ("left", "right", "top", "bottom") if (v := num(edges.get(k), 0, 1))},
            "hidden": hidden, "under": str(r.get("under") or "")[:120]}


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


def _probe_script(no_webgl: bool, canvas_layer: bool = False) -> str:
    cfg = json.dumps({"noWebGL": no_webgl, "frameCapMs": FRAME_CAP_MS, "canvasLayerCss": CANVAS_LAYER_CSS if canvas_layer else "",
                      "canvasLayerAttr": CANVAS_LAYER_ATTR, "cameraAttr": CAMERA_ATTR})
    return f"window.__agySiteConfig = {cfg};\n" + (_HERE / "site_probe.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the camera
#
# A Chromium that puts pages together without the GPU path (no GPU and no SwiftShader for compositing: a launch whose
# graphics switches a wrapper script removed, or one with --disable-gpu) draws a WebGL canvas wrongly wherever an
# opaque box lies over it: the part of the canvas that is covered is drawn, and the part opposite it, mirrored top to
# bottom, is left black. A visitor's browser does not do this. In run 3 (benchmarks/results/site_run3_plugin) every
# phone frame with the text panel open had such a black box over the 3D picture; the agent spent 16 of its 105 minutes
# on it, wrote six screenshot scripts of its own (which, started the same way, showed the same box) and changed the
# site's styles twice for a fault no visitor would ever see.
#
# So the check tests its camera before it believes it: a page with a WebGL canvas of one colour under an opaque bar,
# photographed the way every frame is. Where the fault shows, the visits give each canvas a layer of its own (a filter
# that moves no pixel but cannot be drawn without one), which was measured to give the canvas back, and the answer says
# so; where even that does not help, the answer says the frames have the fault. Tested once per browser.

#: the layer: zero specificity and first in the document, so any rule of the page's own wins. Filters that only change
#: colour (opacity(1), brightness(1)) did not give the canvas back on the test page; blur(0px) and a zero drop-shadow did.
#: A filter makes its element a stacking context, painted with the positioned content. For a positioned canvas that
#: changes nothing. A canvas in the normal flow would then be painted over positioned content that comes before it in
#: the document (a header written above the canvas), so the probe marks canvas by canvas (``site_probe.js``,
#: ``layerCanvases``): a positioned one always, another only where what lies over it stays over it, and it counts the
#: ones it left as they are.
CANVAS_LAYER_ATTR = "data-agy3d-layer"
CANVAS_LAYER_CSS = f":where(canvas[{CANVAS_LAYER_ATTR}]){{filter:blur(0px)}}"
CAMERA_BAR = 0.4            # the opaque bar covers this share of the window, from the top (under a half: see ``camera``)
CAMERA_WAIT_S = 10.0
_CAMERA_PAGE = """<!doctype html><meta charset="utf-8"><style>html,body{margin:0;height:100%;background:#000}
canvas{position:fixed;left:0;top:0;width:100%;height:100%}
#bar{position:fixed;left:0;top:0;width:100%;height:%BAR%%;background:#203040}</style>
<canvas id="c"></canvas><div id="bar"></div><script>
const c = document.getElementById('c'); c.width = innerWidth; c.height = innerHeight;
const g = c.getContext('webgl');
window.__gl = !!g;
if (g) { const draw = () => { g.clearColor(1, 0, 1, 1); g.clear(g.COLOR_BUFFER_BIT); requestAnimationFrame(draw); }; draw(); }
window.__frames = (n) => new Promise((ok) => { const f = () => (n-- > 0 ? requestAnimationFrame(f) : ok(true)); f(); });
</script>""".replace("%BAR%", f"{CAMERA_BAR * 100:.0f}")
_CAMERA_WHAT = ("this browser puts pages together without the GPU path, and its screenshots then leave a black box in a "
                "WebGL canvas opposite every opaque panel (mirrored top to bottom). No visitor's browser does")


def camera_line(result: str, left: int = 0) -> str:
    """What the answer says about its pictures for a camera result that is worth a line ('' otherwise). ``left``: the
    canvases the probe could not give a layer without changing what the page paints."""
    if result == "layer":
        if left:
            n = f"{left} canvas{'es' if left != 1 else ''}"
            return (f"{_CAMERA_WHAT}. The check tested for it and photographs a canvas on a layer of its own, which "
                    f"gives the true picture. {n} in the page's normal flow could not be given one without being "
                    "painted over content that lies above it: in its frames a black box in the 3D picture, where the "
                    "canvas-only frame (*_canvas.jpg) shows the scene, is the browser's, not the site's. Do not "
                    "change the site for it")
        return (f"{_CAMERA_WHAT}. The check tested for it and photographed each canvas on a layer of its own, which "
                "gives the true picture: the frames here are what a visitor sees. A screenshot script of your own on "
                "this machine will show those boxes; they are not the site's")
    if result == "faulty":
        return (f"{_CAMERA_WHAT}, and the frames here have it: a black box in the 3D picture where the canvas-only "
                "frame (*_canvas.jpg) shows the scene is the browser's, not the site's. Do not change the site for it")
    return ""


async def camera(session, ctx) -> str:
    """Does a screenshot of this browser show a WebGL canvas that lies under an opaque box?

    'true' (it does), 'layer' (only with the canvas on a layer of its own, ``CANVAS_LAYER_CSS``), 'faulty' (not even
    then) or '' (not known: no WebGL here, the test page did not draw, or the test did not finish in ``CAMERA_WAIT_S``).
    A result is kept on the browser's session; a test that did not finish is made again by the next check.

    The wait covers the test alone. Bounded from outside it would also cover the browser's start, and a start slower
    than the wait (a loaded machine, software rendering: where this fault lives) would cancel the run, which closes the
    browser that had just come up and leaves the check without a camera test."""
    if "site_camera" in session.notes:
        return session.notes["site_camera"]
    try:
        got = await asyncio.wait_for(_camera_test(ctx), CAMERA_WAIT_S)
    except Exception as e:  # noqa: BLE001  a test that hangs or fails leaves the frames as they were before it existed
        _log.debug("check_site: camera test not finished (%s)", type(e).__name__)
        return ""
    session.notes["site_camera"] = got
    _log.debug("check_site: camera test of this browser: %r", got)
    return got


async def _camera_test(ctx) -> str:
    import base64

    from PIL import Image

    page = await ctx.new_page()
    cdp = await ctx.new_cdp_session(page)

    async def shows() -> tuple[bool, bool]:
        """(the canvas shows at mid height, which is neither under the bar nor opposite it; it shows opposite the bar)"""
        await page.evaluate("window.__frames(2)")
        data = base64.b64decode((await cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True}))["data"])
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size

        def canvas_at(y: float) -> bool:
            r, g, b = im.getpixel((w // 2, min(h - 1, round(h * y))))
            return r > 200 and g < 60 and b > 200
        return canvas_at(0.5), canvas_at(1 - CAMERA_BAR / 2)

    await page.set_content(_CAMERA_PAGE)
    if not await page.evaluate("window.__gl"):
        return ""
    middle, opposite = await shows()
    if not middle:
        return ""
    if opposite:
        return "true"
    await page.add_style_tag(content=CANVAS_LAYER_CSS)
    await page.evaluate(f"document.getElementById('c').setAttribute({json.dumps(CANVAS_LAYER_ATTR)}, '')")
    middle, opposite = await shows()
    return "layer" if middle and opposite else "faulty"


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
                      sizes: tuple[str, ...] = ("desktop", "phone"), variants: tuple[str, ...] = ("reduced", "nowebgl"),
                      other_hosts: str = "block", budget_s: float = ANSWER_S, exe: str | None = None) -> SiteReport:
    """Check a built site; see the module docstring.

    ``sources``: the folder ``data-src`` paths resolve against (default: the site folder). ``sizes``: of ``SIZES``.
    ``variants``: the extra visits, 'reduced' (reduce motion set) and 'nowebgl'. ``other_hosts``: 'block' (default:
    requests to other hosts are recorded and not made, so the check sees the page as a visitor does when they are
    unreachable, and no answer depends on this machine's network) or 'try'."""
    if isinstance(sizes, str):
        sizes = tuple(x.strip() for x in sizes.split(",") if x.strip())
    if isinstance(variants, str):
        variants = tuple(x.strip() for x in variants.split(",") if x.strip())
    unknown = [x for x in sizes if x not in SIZES] + [x for x in variants if x not in VISIT_LABEL]
    if unknown or not sizes:
        raise ValueError(f"unknown size or variant {unknown}: sizes are {', '.join(SIZES)}; variants are {', '.join(VISIT_LABEL)}")
    if other_hosts not in ("block", "try"):
        raise ValueError("other_hosts is 'block' or 'try'")
    from antigravity3d.adapters.engines import web_browser
    from antigravity3d.adapters.engines.web_three import WebThreeValidator, _launch_args

    t_start = time.monotonic()
    clock = _Clock(t_start, budget_s)
    steps = max(2, min(int(steps), 25))
    folder, url = _resolve_target(str(target))
    out = Path(out_dir).resolve() if out_dir else out_dir_for(Path.cwd(), str(folder or url))
    if folder is not None and out.is_relative_to(folder.resolve()):
        raise ValueError(f"the pictures would go into {out}, inside the site, where the next check would weigh them and "
                         f"the site would publish them; give an output folder outside {folder}")
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("*.jpg"), *out.glob("stills/*.webp")]:
        old.unlink()
    with contextlib.suppress(OSError):
        (out / "stills").rmdir()  # a still from an earlier check would pass for one of this page's
    src_dir = Path(sources).expanduser().resolve() if sources else folder
    rep = SiteReport(str(target), out, steps=steps, budget_s=budget_s)
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
                    sideways: bool = False, facts: bool = False, idle: bool = False, one_step: float | None = None,
                    stills: bool = False) -> None:
        vis = _Visitor(name, page_url, own, out, clock, other_hosts)
        rep.visits[name] = vis.v

        async def run(_session, ctx) -> None:
            opened.append((_session, ctx))
            await ctx.route("**/*", vis.gate)
            page = await ctx.new_page()
            await vis.attach(ctx, page)
            await page.add_init_script(_probe_script(no_webgl, canvas_layer=rep.camera == "layer"))
            try:
                if not await vis.load(page):
                    return
                if idle:
                    vis.v.idle = await vis.idle(page)
                if facts:
                    with contextlib.suppress(Exception):
                        vis.v.facts_raw = await page.evaluate(_FACTS_JS)
                if do_steps:
                    await vis.steps(page, do_steps, canvas, sideways, notch=name == "desktop")
                    await vis.between(page, do_steps)
                if one_step is not None:
                    geo = await page.evaluate("window.__agySite.geometry()")
                    await vis.scroll_to(page, round(geo["max"] * one_step))
                    shot, secs, motion = await vis.settle(page, SETTLE_CAP_S)
                    vis.v.steps.append(Step(0, one_step, round(await page.evaluate("window.__agySite.y()")), settle_s=round(secs, 2),
                                            changing_s=round(vis.changing_s, 2), motion=motion))
                    vis.v.idle = await vis.idle(page)
                if not do_steps:
                    f = out / f"{name}.jpg"
                    shot, _, _ = await vis.settle(page, 1.0)
                    f.write_bytes(shot)
                    vis.v.frame = str(f)
                info = await vis.info(page)
                vis.v.probe, vis.v.memory, vis.v.text_chars = info, info.get("memory"), info.get("textChars")
                if stills:
                    # last: drawing each chapter's rest moves the page, and nothing measured after would be the visit's
                    rep.stills = await vis.stills(page)
            finally:
                await vis.flush()
                if server is not None and name in ("desktop", "phone"):
                    # the server knows why a file was missing; the browser only that the request failed
                    why = {m.split(" (")[0].lstrip("/"): m for m in server.missing}
                    vis.v.failed = list(dict.fromkeys(why.get(f.split(" (")[0].lstrip("/"), f) for f in vis.v.failed))
                    vis.v.failed += [m for m in why.values() if m not in vis.v.failed]

        opts = dict(SIZES["desktop"] if name not in SIZES else SIZES[name])
        opts.update(ctx_opts)
        async with lanes:
            await web_browser.SHARED.run(run, exe=exe, args=_launch_args(), context_options=opts)

    lanes = asyncio.Semaphore(LANES)
    opened: list = []

    try:  # the camera first (see ``camera``); a browser that does not start is reported by the visits, as before
        rep.camera = await web_browser.SHARED.run(camera, exe=exe, args=_launch_args(),
                                                  context_options={"viewport": {"width": 200, "height": 200}})
    except Exception as e:  # noqa: BLE001
        _log.debug("check_site: no camera test (%s)", type(e).__name__)

    plan = {}
    if "desktop" in sizes:
        # Stills once, at desktop size: the article layout is the same page at every size, and its column is narrower than
        # a desktop canvas, so one set serves all
        plan["desktop"] = visit("desktop", {}, do_steps=steps, canvas=True, facts=True, idle=True, stills=True)
    if "phone" in sizes:
        plan["phone"] = visit("phone", {}, do_steps=steps, canvas=True, sideways=True)
    # the variants after the sizes (the lanes start them in this order); the reduced-motion visit's one step, at 50 %,
    # is compared with the desktop step nearest it
    if "nowebgl" in variants:
        plan["nowebgl"] = visit("nowebgl", {}, no_webgl=True)
    if "reduced" in variants:
        plan["reduced"] = visit("reduced", {"reduced_motion": "reduce"}, one_step=0.5)
    tasks = [asyncio.ensure_future(p) for p in plan.values()]
    try:
        done, pending = await asyncio.wait(tasks, timeout=max(1.0, clock.left() - STUCK_S))
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2.0)
            # A cancelled visit's own task ends at once; its context is closed on the browser's thread
            # (web_browser.SharedChromium._in_context). The answer waits for that, so no page of this check outlives it.
            await _closed(opened, STUCK_S - 3.0)
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
            held = {st.index for r in held_runs(v.steps) for st in r["steps"]}
            small = {st.index for seen in small_labels(v.steps).values() for st, _ in seen}
            for s in v.steps:
                flags = sheet_flags(s, held, small)
                frames.append((s.frame, f"{s.index + 1}/{steps}  {s.share:.0%}" + (f"  {', '.join(flags)}" if flags else ""), bool(flags)))
            sheet = out / f"sheet_{name}.jpg"
            contact_sheet(frames, sheet, 480 if name == "desktop" else 200, 3 if name == "desktop" else min(len(frames), 5))
            rep.sheets[name] = str(sheet)
    d = rep.visits.get("desktop")
    rep.facts.update(judge_facts(d.facts_raw if d else None, src_dir))
    rep.seconds = time.monotonic() - t_start
    (out / "report.json").write_text(json.dumps(rep.to_dict(), indent=1, default=str))
    return rep


async def _closed(opened: list, timeout: float) -> None:
    """Wait until no context of ``opened`` (session, context) is open; one still open after ``timeout`` has its browser
    discarded, as web_browser does with a context that will not close."""
    from antigravity3d.adapters.engines import web_browser

    async def alive() -> list:
        return [(s, c) for s, c in opened if s.alive() and c in s.browser.contexts]

    t0 = time.monotonic()
    while left := await asyncio.wrap_future(web_browser.SHARED.submit(alive())):
        if time.monotonic() - t0 > timeout:
            for s in {id(s): s for s, _ in left}.values():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.wrap_future(web_browser.SHARED.submit(web_browser.SHARED._discard(s))), 15)
            _log.warning("check_site: %d browser context(s) would not close; the browser was discarded", len(left))
            return
        await asyncio.sleep(0.2)


def out_dir_for(root: Path, target: str) -> Path:
    """Where the pictures of ``target`` go: ``<root>/agy3d_site_check/<name>``, one folder per site, replaced on every
    check (the agent checks often; old frames would only be mistaken for new ones). Never inside the site folder, which
    gets published."""
    if re.match(r"^https?://", target, re.I):
        u = urlsplit(target)
        name = f"{u.hostname}-{u.port or 80}"
        return Path(root) / "agy3d_site_check" / re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    site = Path(target).resolve()
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", site.name or "site")
    out = Path(root).resolve() / "agy3d_site_check" / name
    if out.is_relative_to(site):
        # The site is the workspace itself (built at its root, or target="."): any folder of the workspace would be
        # served, weighed and photographed by the next check, and published with the site. The system's temporary
        # folder is outside it; the answer names the absolute path.
        digest = hashlib.sha256(str(site).encode()).hexdigest()[:8]
        out = Path(tempfile.gettempdir()) / "agy3d_site_check" / f"{name}-{digest}"
    return out


def check_site(target: str, **kw) -> SiteReport:
    """Blocking form of :func:`acheck_site` (the CLI's)."""
    return asyncio.run(acheck_site(target, **kw))
