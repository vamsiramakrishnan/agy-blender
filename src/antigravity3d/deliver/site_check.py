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
9. at every photographed step, every position read between them and, for a page with the subjects hook, at a finer
   grid read for the text alone (``_Visitor.text_pass``, the page's drawing left out), names chapter text that is
   faded or missing in a card that is up, judged against the fullest each line is drawn at anywhere in the visit
   (``faded_findings``), with the stretch of the scroll it covers. Run 6's agent tied the opacity of its cards' lines
   to the scroll; a visitor who stopped after a card came up read a half-faded card, a blind judge named it in six of
   six verdicts, and the nine steps had missed every such stretch. A read waits for a timed entrance to end, and never
   reports one;
10. where the page names its chapters and where each rests (``window.__site.chapters()``, ``setProgress``), writes one
   still per chapter for the page without WebGL, the 3D canvas alone, as ``stills/<chapter id>.webp``. Run 2's agent
   wrote a screenshot script for those twice (the first set had a scrollbar in every picture).

A story (a page with no scroll that sets ``window.__story``, the story contract: a deck whose slides are HTML over a
3D stage, or a film; the names in this module say film, which the first stories were) is stepped beat by beat instead
(``_Visitor.beats``): every beat shown settled with ``go(scene, beat)`` and photographed at both sizes, its picture
alone and its window; the answer, headed STORY CHECKED, names a hook call that hangs or rejects, an empty picture, a
beat without a line or with a line its HTML does not hold, a beat that changes nothing, a scene that stands still,
a beat whose words are in the page and are not shown there (``words_unseen``), text that does not fit, words that
are hard to read over the picture (``words_contrast``), the arrow key and the hash (``film_findings``), and writes
one still per scene. A beat that only adds words to a slide is counted, not named (``same_picture``).

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
from html.parser import HTMLParser
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
#: Chapter text faded or missing in a card that is up (site_probe.js, ``S.fadedText``; ``faded_findings``). Run 6's
#: agent tied the opacity of each line of a chapter's card to the scroll position (a GSAP stagger with ``scrub``). Read
#: at rest (benchmarks/results/round8/text_timing, run6_card_lines.txt): in chapters 2 to 5 the card is up from 35 % of
#: the chapter and its lines are part way until about 50 % (at 40 %: 0.76, 0.41, 0.05, 0.01, 0.01 and a last line
#: whole); in the last chapter they sit at 0.01 from 35 % to 64 % and are not whole before 95 %. A blind judge shown
#: the site twice, that stretch falling on a photographed step in one of them, preferred the other in six of six
#: verdicts, twice, every reason naming the "half-faded, unreadable text card"; this check had said nothing, its nine
#: steps having missed the stretches. In the page a line is faded where its card is up (chain of opacities 0.9 or
#: more) and the line is under 0.75 of the fullest it is drawn at anywhere in the visit (at 0.75 a line still reads;
#: run 6's 0.41 and 0.05 do not), provided it is drawn at 0.5 or more somewhere: text that is never fuller than that is
#: dim by design. A position counts for a chapter when FADED_CHARS or more of its characters are faded there and they
#: are at least FADED_SHARE of its text in the window: 20 characters is a short heading (as ``TEXT_READABLE``), and a
#: quarter keeps one faded line of a long card from being named. Run 6's cards hold 182 to 278 characters of such text
#: on the desktop, and at the worst position of each chapter named 182 to 261 of them were faded (anchors at 61 %, 69
#: of 278, its last line still coming, does not count).
FADED_CHARS = 20
FADED_SHARE = 0.25
#: the read waits in the page for a timed entrance to end (site_probe.js, REST_MS: 1.5 s); the call is bounded by that
#: and a few frames of a slow page more
FADED_WAIT_S = 4.0
#: The text pass (``_Visitor.text_pass``): after the pass between the steps, the chapter text alone is read where it has
#: not been, on a grid of FADED_PER_STEP times as many even parts as the steps cut the scroll into, coarse to fine and
#: each level in the order of the scroll (a jump back makes the page's cards cross-fade again), for at most FADED_PASS_S
#: per visit, the page's WebGL draw calls left out meanwhile (site_probe.js, ``S.quiet``). Run 6's faded stretches are
#: 2.7 % of the scroll wide (flood 22 to 25 %, digest 40 to 43 %, anchors 58 to 61 %, hosts 77 to 80 %; start 94 to
#: 99 %): at 9 steps a grid of 1/64 meets each, the pass between the steps (1/32 at best) would miss flood, and on run
#: 6's page it read 7 to 9 of its 24 positions in its 20 s. Measured there, a text read costs 1.9 s drawing and 0.07 s
#: without; a running CSS transition is ended rather than waited for (site_probe.js, ``S.readText``).
FADED_PER_STEP = 8
FADED_PASS_S = 8.0
#: What a finding of faded text says after its numbers; the cause and the fix once per answer, as ``TEXT_IN_MOVE``
#: (run 6 has five such chapters at each of two sizes).
FADED_SEEN = " A visitor who stops there reads part of a card."
FADED_FIX = (" Text tied to the scroll position does this; bring it in on time when the card comes up (the skill's "
             "libraries.md, the GSAP section).")
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

# ---- a story (a deck or a film): a page with no scroll that the story contract steps beat by beat through
# window.__story. The names say film, which the first stories were.
#: Beats photographed per size at most; a longer film is sampled: every scene's last beat (its still and the reading
#: of whether it moves are taken there), then an even spread of the others. The talk deck the owner showed has 13
#: scenes and 43 beats, and is seen whole. A beat costs a settled go(), two frames, two pictures and two short reads:
#: 0.4 s on the test film of tests/test_site_check.py (5 beats in 2 s at each size, the two sizes at once), more on a
#: heavy page drawn in software; 48 beats at 1.5 s are 72 s, which with the wait for ready (FILM_READY_S) still leaves
#: the answer inside ANSWER_S. The clock bounds it in any case. The reading of the words over the picture
#: (``WORDS_CELL``) adds what ``_Visitor.beats`` says it measured, a few seconds over 48 beats.
FILM_BEATS_MAX = 48
#: one call of the hook (go, at, advance) may take this long: a settled go() draws one frame, and on software WebGL a
#: heavy frame takes 0.5 to 2 s (site_probe.js, "The page's clock"); the same bound as one still (STILL_WAIT_S)
FILM_CALL_S = 8.0
#: the wait for window.__story.ready. Before it resolves the film draws every scene at the end of each beat and each cut
#: at its middle (the contract's warm-up): the deck's 43 beats and 12 cuts at 0.5 s a frame are about 28 s.
FILM_READY_S = 30.0
#: ... and without WebGL, where the film has nothing to warm up and says so at once
FILM_READY_NOGL_S = 3.0
#: Two pictures of the film are "the same" when less than this share of the pixels differs by more than 8 % of full
#: scale in any channel (``picture_change``, at full size: a small light that comes on is a few pixels). A settled beat
#: is drawn the same every time (the contract), so a beat shown twice measures 0, as the test films do; 0.05 % is a
#: patch of 25 x 25 px at 1440 x 900 and of 13 x 13 px at 390 x 844.
FILM_SAME = 0.0005
#: A line longer than this many characters is a paragraph, which the viewer reads instead of looking at the picture
#: (the contract: "a line longer than about 140 characters"; the example film's longest line has 71).
FILM_LINE_CHARS = 140
#: the reading of whether each scene moves (``_Visitor.film_moves``: three at() calls at its last beat) takes at most
#: this long in all, on the desktop only (motion does not depend on the size)
FILM_MOVES_S = 20.0
#: how long a key press, or a page opened at a hash, is given to show its beat (polled; a cut may be running)
FILM_KEY_S = 2.0
#: a tile's label on the contact sheet: the scene, the beat and its first line cut to this many characters
FILM_LABEL_CHARS = {"desktop": 44, "phone": 18}
#: why a read of where the story is came back empty when the page gave no answer in the time a read has
#: (``SUBJECTS_WAIT_S``): a page busy drawing, not a story that does not say where it is (``_Visitor.film_hash``)
UNANSWERED = "the page did not answer in time"
#: the contract's members of window.__story, in its order
FILM_MEMBERS = ("ready", "mode", "title", "treatment", "scenes", "go", "play", "at", "advance", "back", "setAuto", "info")
#: The layout of a scene whose words are a caption: one beat's lines at a time, under the picture or over its lower
#: third (``info().layout``; a page that gives none is a film, and its words are captions). Any other layout is a
#: slide: its heading stays up and its beats add to what is shown, so a beat there may add words and leave the
#: picture as it was (``same_picture``).
CAPTION = "caption"
#: Words over the picture (``words_contrast``): WCAG's contrast ratio between a line's colour and what is drawn behind
#: it, which must reach WORDS_NEED_LARGE for large type (WORDS_LARGE_PX and up, or WORDS_BOLD_PX and up in bold: WCAG's
#: 18 pt and 14 pt bold) and WORDS_NEED for the rest (the story contract, part 2).
#:
#: What is drawn behind the words is photographed, not worked out: the window with the letters' fill left out
#: (site_probe.js, ``S.wordsOff``), so a scrim or a card under the words, a halo round them and a picture dimmed by
#: CSS count as the viewer sees them. The contract's measure is the canvas alone; where nothing lies between the
#: canvas and the words the two are the same picture.
#:
#: The picture under a line is averaged over squares WORDS_CELL to the type's size on a side (in linear light, as the
#: eye adds up what it cannot resolve), each square is compared with the line's colour, and the tenth of the squares
#: with the least contrast is left out (WORDS_WORST: the contract's "middle 80 % by luminance"; of the two tenths it
#: drops, only the one nearest the words' own tone can lower the ratio). Why squares and not pixels: the halftone and
#: glyph treatments print a picture in two inks (dots, text characters), where every pixel is paper or ink and a line
#: in the ink's colour over dots that cover a tenth of the paper would read 1.0, however well it reads. What is finer
#: than a letter's parts is a tone to the eye; what is their size hides them.
#:
#: Both choices, on one slide: the example deck's figure ("500 tonnes" at 185 px, a line at 29 px, its source at 17
#: px) shown in the halftone theme as the deck stood before its merge, the words on the theme's scrim over a picture
#: printed in their own ink. The canvas alone read pixel by pixel gives 1.3, 1.2 and 1.7 and would name three lines
#: a viewer reads without effort; the canvas alone in squares 7.7, 7.0 and 3.3, and would still name the source; the
#: photograph in squares 10.1, 10.1 and 5.0.
#:
#: Calibrated on the skill's film "Keeper" (halftone, dots 6 px apart on the desktop and 4.5 px on
#: the phone, a headline in the ink's colour with a halo in the paper's), each frame looked at. Where the headline
#: ships, in the dark lower left, its two lines read 9.8 and 7.2 at 41 px on the desktop and 11.7 and 11.0 at 16 px
#: on the phone (against the canvas alone, the halo not counted: 6.7 and 4.2, 9.5 and 9.2). Moved up over the lit
#: beam, five places per size: on the desktop the three lines whose last words are lost in the dots read 2.0 to 2.3
#: and the seven that read with some effort 3.1 to 4.8; on the phone nine of the ten lines read 1.5 to 2.8 and are
#: hard to read, and the one that clears the beam 6.4. Against the canvas alone the same lines read 1.1 to 2.3 on
#: the desktop: the halo is why the headline can be read at all there. A headline set over the same film's morning
#: scene (a light sky, the same light ink) reads 1.5 at both sizes, halo and all.
WORDS_CELL = 4
WORDS_WORST = 10
WORDS_LARGE_PX = 24
WORDS_BOLD_PX = 19
WORDS_NEED_LARGE = 3.0
WORDS_NEED = 4.5
#: lines named one per sentence, per size, each with the beats it is hard to read at; the rest are named together
#: (a deck whose ink is wrong for its picture has one such line on every slide)
WORDS_LINES = 6
#: how long a beat's text is given to come to rest before the window is photographed (site_probe.js, ``S.filmRest``):
#: an entrance of half a second and its stagger, where the page lets one run after go(..., { settle: true })
WORDS_REST_S = 1.5
#: ... at this many beats of a visit. Text that is still changing after the wait (a line that pulses from script
#: for as long as it is up) will be so at every beat, and 48 waits are 72 s: after these the text is read as it is.
WORDS_RESTLESS = 3
#: The reading of the words over the picture takes at most this long in all per visit (``_Visitor.beats``, which
#: says what a beat costs): beats past it are photographed without it, and the measure says how many. On a film of
#: 48 beats drawn by the starter's engine, every beat's two lines over the picture with a halo, it took 7 to 8 s at
#: 1440 x 900 and 5 s at 390 x 844 of a check of 42 s. A page that draws a heavy frame for every photograph (a second
#: each in software) would take 50 to 100 s without the bound, and the 48 beats would not all be photographed.
WORDS_S = 20.0
#: what the first finding about words says after its numbers; the others say the numbers only (``TEXT_IN_MOVE``)
WORDS_RULE = (f" (WCAG's ratio; {WORDS_NEED_LARGE:g} for type of {WORDS_LARGE_PX} px and up or bold of {WORDS_BOLD_PX} px "
              f"and up, {WORDS_NEED:g} for smaller type)")
WORDS_FIX = (" Move the words over a {tone} or quieter part of the picture, or lay a scrim in the theme's paper behind "
             "them.")

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
    # chapter text faded or missing in a card that is up here (site_probe.js, ``S.fadedText``, judged at the end of the
    # visit): one dict per group with any, {kind, group, faded, whole, total, low}; [] none; None: not read (no read,
    # a read still changing after its wait, or a document replaced since). faded_read: which read of the page's log.
    faded: list[dict] | None = None
    faded_read: int | None = None


@dataclass
class Beat:
    """One beat of a film as a visit photographed it (``_Visitor.beats``)."""
    scene: str
    beat: int               # index in its scene, from 0 (the hash counts from 1: #<scene>-<beat + 1>)
    order: int              # place in the whole film, from 0
    first: bool = False     # the scene's first beat
    last: bool = False      # the scene's last beat
    frame: str = ""         # the window
    canvas_frame: str = ""  # the picture alone
    content: float | None = None   # share of the picture showing something other than its commonest colour
    change: float | None = None    # share of the picture that differs from the beat before in its scene; None: none
    lines: list[str] = field(default_factory=list)  # info().lines
    foreign: list[str] = field(default_factory=list)  # of those, the lines the page's HTML does not hold
    info: dict | None = None       # info() after go(), as checked (``_film_info``): its layout says what the words are
    # the beat's element in the HTML (site_probe.js, ``S.filmBeat``): found, silent, over, text, shown (any of the
    # beat's own lines is drawn where a viewer can read it; 'faint': the fullest opacity they are drawn at where none
    # is), boxes, outside, scrolls, and 'rest' (how the text came to rest, ``S.filmRest``); None: not read
    html: dict | None = None
    # the scene's lines that lie over the picture at this beat, each with its contrast to what is drawn behind it
    # (``words_contrast``): text, own (the beat's own line, not one shown since an earlier beat or the heading), px,
    # bold, ratio, need, light, shadow, cells; [] none lies over the picture; None: not read
    words: list[dict] | None = None
    call: str = ""          # why go() failed ('late' or 'rejected: ...'); '' it answered
    draws: int = 0
    triangles: int = 0
    seconds: float = 0.0


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
    # the positions where only the chapter text was read (``_Visitor.text_pass``; ``between`` True, no subjects), and
    # the seconds that pass took
    text_reads: list[Step] = field(default_factory=list)
    text_s: float = 0.0
    # what one wheel notch from the top of the page changed (``_Visitor.first_notch``): {px, motion, window, canvas,
    # text}; None: not measured (not the desktop visit, a page shorter than two notches, or no time)
    first_notch: dict | None = None
    # a story (window.__story): what the hook gave (``_film_hook``) and what the visit read through it: 'planned' (beats
    # to photograph), 'total', 'beats_s', of that 'rest_s' (the text brought to rest) and 'words_s' (the reading of the
    # words over the picture; 'words_unread': the beats left without it once ``WORDS_S`` was used up), 'form' and
    # 'theme' (info()'s, where it gives them), 'calls' (hook calls that failed: {call, method, why}), 'moves', 'key',
    # 'hash', 'reduced', 'story'; None: the page has no window.__story
    film: dict | None = None
    beats: list[Beat] = field(default_factory=list)


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
    # a story (window.__story at desktop or phone size): the hook as the first of those visits read it, with the
    # 'form' and 'theme' its info() gave; {} a site
    film: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ verdicts

    def broken(self) -> list[str]:
        if self.film and not self.error:
            return self.film_lines()[0]
        return self._broken()

    def _broken(self) -> list[str]:
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
        if self.film and not self.error:
            return self.film_lines()[1]
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
            out += self.faded_lines(v)
            out += self.subject_lines(v)
        p = self.visits.get("phone")
        if p and p.steps:
            wide = [s for s in p.steps if s.sideways_px > 1]
            if wide:
                worst = max(s.sideways_px for s in wide)
                out.append(f"Phone ({SIZES['phone']['viewport']['width']} px): the page scrolls sideways by up to {worst} px"
                           + (f"; widest: {', '.join(p.overflow[:3])}" if p.overflow else "") + ".")
        if weight := self.weight_line():
            out.append(weight)
        if hosts := self.hosts_line():
            out.append(hosts)
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
        told = False
        for i, line in enumerate(out):  # and the cause of faded text, once
            if line.endswith(FADED_FIX):
                if told:
                    out[i] = line[:-len(FADED_FIX)]
                told = True
        return out

    def weight_line(self) -> str:
        """The download weight when it passes ``SLOW_WAIT_S`` on a 10 Mbit/s link, else ''."""
        total = self.total_bytes()
        if total and total / LINK_BPS > SLOW_WAIT_S:
            return (f"The page downloads {_mb(total)}: about {total / LINK_BPS:.0f} s on a 10 Mbit/s connection "
                    f"before everything has arrived.")
        return ""

    def hosts_line(self) -> str:
        """The other hosts the page asks, else ''."""
        hosts = self.other_hosts()
        if hosts:
            return f"It asks {len(hosts)} other host{_s(hosts)} ({', '.join(sorted(hosts))}): " + self._other_host_note() + "."
        return ""

    def faded_lines(self, v: Visit) -> list[str]:
        """One line per group (chapter, section or the page) whose text is faded or missing in a card that is up at
        some position read (``faded_findings``): the stretches of the scroll it covers (neighbouring positions read
        merged into one, as ``_where``; at photographed steps only, the steps), the worst position with its numbers,
        what a visitor sees and the likely cause and fix."""
        found = faded_findings(v)
        if not found:
            return []
        positions = [st for st in _text_positions(v) if st.faded is not None]
        out = []
        for f in found:
            who = {"chapter": f"chapter {_q(f['group'], 60)}", "section": f"section {_q(f['group'], 60)}"}.get(
                f["kind"], "the page")
            at, g = f["worst"]
            if any(p.between for p, _ in f["hits"]):
                spans = []
                for r in _runs(f["hits"], positions):
                    a, b = r[0][0].share, r[-1][0].share
                    spans.append(f"at {a:.0%}" if f"{a:.0%}" == f"{b:.0%}" else f"from {a:.0%} to {b:.0%}")
                where = _and(spans) + " of the scroll"
            else:
                where = "at " + _steps_short([p for p, _ in f["hits"]], self.steps)
            out.append(f"{v.name.capitalize()}: the text of {who} is faded or missing in its card {where} (at "
                       f"{at.share:.0%}: {g['faded']} of its {g['total']} characters there faded or missing, the "
                       f"faintest at opacity {g['low']:.2f}, the card up).{FADED_SEEN}{FADED_FIX}")
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

    def bytes_line(self, what: str = "whole scroll") -> str:
        d = self.visits.get("desktop")
        if not d or not d.requests:
            return ""
        by: dict[str, int] = {}
        for r in d.requests.values():
            by[r["kind"]] = by.get(r["kind"], 0) + r["bytes"]
        kinds = ", ".join(f"{k} {_mb(b)}" for k, b in sorted(by.items(), key=lambda kv: -kv[1]) if b)
        top = sorted(d.requests.items(), key=lambda kv: -kv[1]["bytes"])[:5]
        tops = ", ".join(f"{_short(u, d)} {_mb(r['bytes'])}" for u, r in top)
        return (f"Downloaded (desktop, {what}): {_mb(self.total_bytes())} in {len(d.requests)} file{_s(d.requests)}: {kinds}. "
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
        return self._cost_lines(d, f"Scene (desktop, the largest over the steps): {md:,} draw call{_s(md)} and {mt:,} "
                                   f"triangle{_s(mt)} in one frame" + (f" (most triangles at {at.share:.0%})" if at and mt else ""),
                                "top of the page")

    def _cost_lines(self, d: Visit, head: str, where: str) -> list[str]:
        """``head`` (the draw calls and triangles), the textures on the graphics card, and what the page does while
        nothing is touched at ``where``."""
        m = d.memory or {}
        lines = [head
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
                lines.append(f"While nothing moves ({where}, {idle['seconds']:.1f} s, no input): it drew in "
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
        if self.film and not self.error:
            return self.film_text()
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
                                f"between the steps as well ({v.between_s:.0f} s)." if v.between_planned else "")
                             + (f" The chapter text alone read at {len(v.text_reads)} more positions "
                                f"({v.text_s:.0f} s)." if v.text_reads else ""))
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

    # ------------------------------------------------------------------ a story

    def film_lines(self) -> tuple[list[str], list[str]]:
        """(broken, a viewer would notice) for a story: the page's own faults as a site's (errors, failed requests,
        facts), then each finding of ``film_findings`` in its words, in the order found. The rule for words over the
        picture and what to do about them are said once, at the first such finding."""
        broken = self._broken()
        noticed: list[str] = []
        said = False
        for f in film_findings(self):
            line = self._film_sentence(f, again=said and f["kind"] in ("words", "more_words"))
            said = said or f["kind"] in ("words", "more_words")
            (broken if f["broken"] else noticed).append(line)
        if weight := self.weight_line():
            noticed.append(weight)
        for r in (self.film_reduced_line(), self.film_nowebgl_line()):
            if r and not r[1]:
                noticed.append(r[0])
        return broken, noticed

    def _film_sentence(self, f: dict, again: bool = False) -> str:
        """One finding of ``film_findings`` as the answer says it: who, what, where, the number, and what to do.
        ``again``: the rule and the remedy for words over the picture were said by an earlier finding."""
        k, size = f["kind"], f.get("size", "").capitalize()
        rel = lambda p: _rel(p, self.out_dir)  # noqa: E731
        if k == "missing":
            return (f"window.__story lacks {_and(f['members'])}: the check steps the story through that hook (its members "
                    f"are {', '.join(FILM_MEMBERS)}), and what it cannot call goes unchecked. Give the hook every member.")
        if k == "ready":
            if f["state"] == "rejected":
                return (f"window.__story.ready rejected ({_q(f['why'], 100)}): the story never said it was ready. Its beats "
                        "were photographed all the same.")
            return (f"window.__story.ready did not resolve within {FILM_READY_S:.0f} s of the page loading: resolve it "
                    "once every scene has been drawn at each of its beats. Its beats were photographed all the same.")
        if k == "no_scenes":
            return ("window.__story.scenes names no scene with beats ({ id, title, beats } with beats a count): there was "
                    "nothing to photograph.")
        if k == "call":
            what = (f"did not answer within {FILM_CALL_S:.0f} s" if f["why"] == "late"
                    else f"rejected it ({_q(f['why'].removeprefix('rejected: '), 100)})")
            return (f"window.__story.{f['call']} {what} ({_and(f['sizes'])}): {f['lost']}. A call of the hook resolves "
                    "with info() once its frame is drawn, and rejects only for a scene or beat that does not exist.")
        if k == "empty":
            return (f"{size}: the picture shows nothing at {_beat_names(f['beats'])}: under {EMPTY_CONTENT:.0%} of it "
                    f"differs from its commonest colour ({', '.join(_share(b.content) for b in f['beats'])}). A camera "
                    f"that looks away from its set, or a set with no light on it, does this. Look at "
                    f"{rel(f['beats'][0].canvas_frame or f['beats'][0].frame)}.")
        if k == "no_line":
            b = f["beat"]
            where = (" and its <div data-beat> is not marked data-silent" if (b.html or {}).get("found")
                     else f" and the HTML has no <div data-beat> for it in <section data-scene=\"{b.scene}\">")
            return (f"Beat {_beat_name(b)} has no line: info().lines is empty there{where}. Give it a <p> in the HTML, "
                    "or mark the div data-silent where the beat is meant to be silent.")
        if k == "foreign":
            b = f["beat"]
            return (f"Beat {_beat_name(b)} shows a line the page's HTML does not hold: {_q(f['line'], 100)} "
                    "(info().lines). The story's text is the HTML, which a browser without WebGL shows and this check "
                    "compares: put the line in that beat's <div data-beat>, or show the one that is there.")
        if k == "hosts":
            return self.hosts_line()
        if k == "same":
            b, prev = f["beat"], f["prev"]
            return (f"{size}: beat {_beat_name(b)} shows the same picture as {_beat_name(prev)} before it "
                    f"({_share(b.change)} of the picture differs) and only its caption changes: under a caption a beat "
                    "must change something the viewer can see in the picture (a camera move, an object, the light). A "
                    "beat that only adds words belongs on a slide (a scene whose info().layout is not 'caption'). "
                    f"Look at {rel(prev.frame)} and {rel(b.frame)}.")
        if k == "nothing":
            b, prev = f["beat"], f["prev"]
            return (f"{size}: beat {_beat_name(b)} changes nothing: it shows the same picture as {_beat_name(prev)} "
                    f"before it ({_share(b.change)} of the picture differs) and no new words. A beat must change "
                    "something the viewer can see: the picture (a camera move, an object, the light) or, on a slide, "
                    f"the words. Look at {rel(prev.frame)} and {rel(b.frame)}.")
        if k == "still":
            m = f["move"]
            where = (f"at its last beat ({m['beat']})" if m.get("last", True)
                     else f"at {m['beat']}, the last of its beats that changes the picture,")
            return (f"Scene {_q(m['scene'], 60)} stands still: {where} the picture at progress "
                    f"0.3 and at 1 is the same, and so is the picture at ambient time 1 s and 3 s ({_share(m['p'])} and "
                    f"{_share(m['t'])} of it differs). Give each beat one slow camera move or one event, and the scene a "
                    "little ambient life (a flicker, rain, a turning light).")
        if k == "unseen":
            beats = f["beats"]
            if len(beats) == 1:
                faint = (beats[0].html or {}).get("faint")
                how = ("none of its lines is drawn (display: none, visibility: hidden or a box without size)"
                       if faint is None else f"its lines are drawn at {faint:.0%} opacity (under half its opacity a "
                                             "line is not counted as shown)")
                what = f"the words of beat {_beat_name(beats[0])} are in the page and are not shown there: {how}"
            else:
                what = (f"the words of beats {_beat_names(beats)} are in the page and are not shown there: none of "
                        "their lines is drawn where a viewer can read it (display: none, visibility: hidden, a box "
                        "without size, or under half its opacity)")
            # lines read while they were still coming in (the wait of S.filmRest ran out): said, so that an entrance
            # that ignores settle is not taken for words that are never shown
            moving = [b for b in beats if ((b.html or {}).get("rest") or {}).get("settled") is False]
            note = ((" The text was still changing when it was read" + (f" at {_beat_names(moving, 3)}" if len(beats) > 1 else "")
                     + ": go(id, beat, { settle: true }) is to show a beat's lines whole at once.") if moving else "")
            return (f"{_and(f['sizes']).capitalize()}: {what}. The viewer gets the picture without its words. Show a "
                    "beat's lines while it is the current beat, or mark its <div data-beat> data-silent where it is "
                    f"meant to have none.{note} Look at {rel(beats[0].frame)}.")
        if k == "caption":
            b, h = f["beat"], f["beat"].html
            if h.get("scrolls"):
                how = (f"{h['scrolls']['sel']} scrolls (its content is {h['scrolls']['height']} px tall in a box of "
                       f"{h['scrolls']['box']} px)")
            else:
                how = (f"{h['outside']} of its {h['boxes']} line boxes lie outside "
                       + ("the picture or the window" if h.get("over") else "the window or the box that holds them"))
            if h.get("over") or _layout(b) == CAPTION:
                return (f"{size}: the {'headline' if h.get('over') else 'caption'} of beat {_beat_name(b)} does not "
                        f"fit: {how}. Shorten the line, or give it more room at this size. Look at {rel(b.frame)}.")
            also = f" (nor at {_beat_names(f['also'], 4)})" if f.get("also") else ""
            return (f"{size}: the slide's text at beat {_beat_name(b)} does not fit{also}: {how}. Shorten the lines, put "
                    f"fewer on the slide, or give them more room at this size. Look at {rel(b.frame)}.")
        if k == "words":
            beats = f["beats"]
            type_ = f"{f['px']:.0f} px{' bold' if f['bold'] else ''} type"
            worst = f" at worst ({_beat_name(f['worst'])})" if len(beats) > 1 else ""
            head = f"{size}: {_first_words(f['text'])} is hard to read over the picture at {_beat_names(beats, 4)}: {type_} "
            if again:
                return (head + f"at a contrast of {_ratio(f['ratio'], f['need'])}{worst}, where it needs {f['need']:g}. "
                        f"Look at {rel(f['worst'].frame)}.")
            return (head + f"at a contrast of {_ratio(f['ratio'], f['need'])} with what is drawn behind it{worst}, where "
                    f"it needs {f['need']:g}{WORDS_RULE}." + WORDS_FIX.format(tone="darker" if f["light"] else "lighter")
                    + f" Look at {rel(f['worst'].frame)}.")
        if k == "more_words":
            lines = f["lines"]
            low = min(lines, key=lambda g: g["ratio"] / g["need"])
            beats = sorted({b.order: b for g in lines for b in g["beats"]}.values(), key=lambda b: b.order)
            return (f"{size}: {len(lines)} more line{_s(lines)} {'is' if len(lines) == 1 else 'are'} hard to read over the "
                    f"picture, at {_beat_names(beats, 4)}: the furthest under its bar is {_first_words(low['text'], 48)} at a "
                    f"contrast of {_ratio(low['ratio'], low['need'])} ({_beat_name(low['worst'])}, {low['px']:.0f} px"
                    f"{' bold' if low['bold'] else ''} type, which needs {low['need']:g})"
                    + ("" if again else WORDS_RULE) + "."
                    + ("" if again else WORDS_FIX.format(tone="darker" if low["light"] else "lighter"))
                    + " The contact sheet marks their beats 'hard to read'.")
        if k == "long":
            b = f["beat"]
            return (f"Beat {_beat_name(b)}: a line of {len(f['line'])} characters ({_q(f['line'], 60)}): over "
                    f"{FILM_LINE_CHARS} a line is a paragraph, which the viewer reads instead of looking at the picture. "
                    "Shorten it or split it over two beats.")
        if k == "tone":
            return (f"{size}: the picture is nearly one tone at {_beat_names(f['beats'])} ("
                    + ", ".join(_share(b.content) for b in f["beats"]) + " of it shows something, against "
                    + ", ".join(_share(x) for x in f["other_content"]) + f" on the {f['other']}): the framing at this size "
                    "has lost the subject. Frame it for this shape of picture (kit.fit takes a field of view for portrait "
                    f"and one for landscape). Look at {rel(f['beats'][0].frame)}.")
        if k == "key":
            got = f["got"]
            went = ("stayed there" if got == f["from"] else f"went to {got}") if got else f"did not say where it was ({f['why']})"
            return (f"The right arrow key does not advance: pressed at {f['from']}, the story {went} (the next beat is "
                    f"{f['want']}). Listen for ArrowRight on window and call advance(), as the contract's keys say.")
        if k == "hash":
            got = f["got"]
            showed = f"showed {got}" if got else f"did not say which beat it showed ({f['why']})"
            return (f"The hash does not open its beat: opened at {f['want']}, the story {showed} once ready. Read "
                    "location.hash once the story is ready, and on hashchange, and go to that beat.")
        raise ValueError(k)

    def words_only_line(self) -> str:
        """The measure of the beats that add words to a slide and leave its picture as it was (``same_picture``): a
        deck does this on purpose, so they are counted, not named as faults. A beat the two sizes disagree on says
        at which size its picture held."""
        sized = [name for name in ("desktop", "phone") if (v := self.visits.get(name)) and v.film]
        at: dict[str, list[str]] = {}
        order: dict[str, int] = {}
        for name in sized:
            for b, _, what in same_picture(self.visits[name]):
                if what == "words":
                    at.setdefault(_beat_name(b), []).append(name)
                    order[_beat_name(b)] = b.order
        if not at:
            return ""
        names = [n + ("" if len(at[n]) == len(sized) else f" ({_and(at[n])} only)") for n in sorted(at, key=order.get)]
        return ("Beats that change only the words (a slide gains a line and its picture holds, which a deck may do): "
                + (_and(names) if len(names) <= 12 else ", ".join(names[:12]) + f" and {len(names) - 12} more") + ".")

    def words_line(self) -> str:
        """The measure of the words over the picture: per size, how many lines were read and the one nearest its bar
        (the least ratio over what its size needs), so that a line that only just passes is seen. The rule is said
        here unless a finding above has said it."""
        parts, named = [], False
        for name in ("desktop", "phone"):
            v = self.visits.get(name)
            read = [(b, w) for b in (v.beats if v else []) for w in b.words or [] if w.get("ratio") is not None]
            unread = int(((v.film if v else None) or {}).get("words_unread") or 0)
            if not read and not unread:
                continue
            part = f"{name} no line read"
            if read:
                named = named or any(w["ratio"] < w["need"] for _, w in read)
                b, w = min(read, key=lambda x: x[1]["ratio"] / x[1]["need"])
                n = len({(x.scene, y["text"]) for x, y in read})
                part = (f"{name} {n} line{_s(n)}, the nearest to its bar {_first_words(w['text'], 48)} at "
                        f"{_ratio(w['ratio'], w['need'])} ({_beat_name(b)}, {w['px']:.0f} px{' bold' if w['bold'] else ''}, "
                        f"needs {w['need']:g})")
            parts.append(part + (f", and {unread} beat{_s(unread)} with words over the picture not read (the {WORDS_S:.0f} s "
                                 "the reading has were used up)" if unread else ""))
        if not parts:
            return ""
        return ("Words over the picture, their contrast with what is drawn behind them" + ("" if named else WORDS_RULE)
                + ": " + "; ".join(parts) + ".")

    def film_reduced_line(self) -> tuple[str, bool] | None:
        """(the line, whether the story handles it): a beat reached under reduce motion is shown finished at once."""
        r = self.visits.get("reduced")
        if not r:
            return None
        if not r.loaded:
            return (f"With reduce motion set: the page did not load ({r.load_error}).", False)
        got = (r.film or {}).get("reduced") or {}
        if got.get("why"):
            return (f"With reduce motion set: no beat was read ({got['why']}).", False)
        if got.get("change") is None:
            return None
        how = []
        if r.probe and r.probe.get("reducedMotionQueried"):
            how.append("asks matchMedia")
        if r.probe and r.probe.get("reducedMotionCss"):
            how.append("has a CSS rule")
        asks = f"the page {' and '.join(how)}" if how else "the page never asks for it (no matchMedia query, no CSS rule)"
        same = got["change"] < FILM_SAME
        if same:
            return (f"With reduce motion set {asks}; beat {got['beat']}, reached with advance(), is shown finished at once: "
                    f"two pictures of the window a second apart are the same ({_share(got['change'])} of it differs), "
                    f"its words included. Picture: {_rel(r.frame, self.out_dir)}.", True)
        return (f"With reduce motion set {asks}, and beat {got['beat']}, reached with advance(), still changes a second "
                f"after it began ({_share(got['change'])} of the window differs): a viewer who asked for less motion "
                "gets it all. Under the setting show a beat finished at once, its words whole and ambient time "
                f"stopped. Picture: {_rel(r.frame, self.out_dir)}.", False)

    def film_nowebgl_line(self) -> tuple[str, bool] | None:
        """(the line, whether the story handles it): without WebGL the story's text is readable, the hook says
        mode 'no-webgl', and nothing is thrown."""
        w = self.visits.get("nowebgl")
        if not w:
            return None
        if not w.loaded:
            return (f"Without WebGL: the page did not load ({w.load_error}).", False)
        errs = _merged([w.page_errors])
        st = (w.film or {}).get("story") or {}
        mode = st.get("mode")
        ok = (not errs and mode == "no-webgl" and st.get("withText", 0) > 0
              and st.get("shown") == st.get("withText"))
        said = (f"window.__story.mode is {_q(mode, 40)}" if mode else
                "window.__story gives no mode" if st.get("hook") else "the page gives no window.__story")
        return (("Without WebGL: " + (f"{len(errs)} uncaught error{_s(errs)} ({_q(errs[0])}); " if errs else "no errors; "))
                + said + (f" (why: {_q(st['why'], 80)})" if st.get("why") else "")
                + f"; {st.get('shown', 0)} of the {st.get('withText', 0)} beats with lines show them as text "
                f"({st.get('chars', 0):,} characters)"
                + ("" if ok else ". The page must read as an article without WebGL: every beat's lines shown, and "
                   "window.__story.mode 'no-webgl'")
                + f". Picture: {_rel(w.frame, self.out_dir)}.", ok)

    def film_stills_line(self) -> str:
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
        return ("stills for the page without WebGL, one per scene at its last beat, the picture alone "
                f"({len(made)}, {w} x {h} px WebP, {_mb(sum(x['bytes'] for x in made) / len(made))} each on average): "
                f"{st['dir']} (" + _names([x["file"] for x in made], 4) + "). Copy them beside the page "
                "(stills/<scene id>.webp), show each in its scene's section of the article, and give it an alt text "
                "that says what it shows"
                + (f"; {_names(blank, 3)} show{'s' if len(blank) == 1 else ''} nothing but the background" if blank else "")
                + notmade + ".")

    def film_text(self) -> str:
        """The answer for a story: the site's parts and wording, beats where a site has steps."""
        broken, noticed = self.film_lines()
        head = (f"STORY CHECKED in {self.seconds:.0f} s: "
                + (f"{len(broken)} thing{_s(broken)} broken" if broken else "nothing broken")
                + f", {len(noticed)} thing{_s(noticed)} a viewer would notice. "
                + ("Open the contact sheets below with your file viewer and look at every frame before you change anything."
                   if self.sheets else ""))
        lines = [head]
        if self.unfinished:
            lines.append(f"The check stopped at its {self.budget_s:.0f} s limit before it was done ("
                         + "; ".join(self.unfinished) + "). Nothing needs fixing because of that: a heavy page on software "
                         "WebGL is slow here. For a quicker look call again with sizes=\"desktop\".")
        if broken:
            lines += ["", "Broken:"] + [f"- {b}" for b in broken]
        if noticed:
            lines += ["", "A viewer would notice:"] + [f"- {b}" for b in noticed]
        lines += ["", "Measures:"]
        f = self.film
        ready = f.get("ready") or {}
        # the form and the theme, where info() gives them: which of the story's looks these pictures are of
        form, theme = f.get("form"), f.get("theme")
        kind = (f", a {form}" if form in ("deck", "film") else f", form {_q(form, 20)}" if form else "")
        kind += (f" in the theme {_q(theme, 40)}" if kind else f", theme {_q(theme, 40)}") if theme else ""
        lines.append(f"- Story: {_q(f.get('title') or '', 80)}{kind}, treatment {_q(f.get('treatment') or '', 40)}, "
                     f"{len(f.get('scenes') or [])} scene{_s(f.get('scenes') or [])} and {f.get('total', 0)} "
                     f"beat{_s(f.get('total', 0))}"
                     + (f"; ready {ready['s']:.1f} s after the page was asked for ({f.get('size', 'desktop')})"
                        if ready.get("state") == "resolved" and ready.get("s") is not None else "") + ".")
        for name in ("desktop", "phone"):
            v = self.visits.get(name)
            if not (v and v.loaded and v.film):
                continue
            vp = SIZES[name]["viewport"]
            per: dict[str, list[str]] = {}
            for b in v.beats:
                per.setdefault(b.scene, []).append("-" if b.content is None else _share(b.content))
            sampled = (f" (sampled evenly: each scene's last beat and an even spread of the rest, at most "
                       f"{FILM_BEATS_MAX})" if v.film.get("planned", 0) < v.film.get("total", 0) else "")
            shot = len([b for b in v.beats if b.frame])
            # text that had not come to rest when its beat was photographed (the wait of S.filmRest ran out, or was
            # no longer given): those frames, and what is read from them of the words, show a slide part way
            moving = [b for b in v.beats if ((b.html or {}).get("rest") or {}).get("settled") is False]
            lines.append(f"- {name.capitalize()} {vp['width']}x{vp['height']}: {shot} of {v.film.get('total', 0)} "
                         f"beats photographed{sampled} ({v.film.get('beats_s', 0):.0f} s); share of the picture showing "
                         "something, per beat: " + "; ".join(f"{s} " + " ".join(x) for s, x in per.items()) + "."
                         + (f" The words were still changing when photographed at {_beat_names(moving, 4)}: those "
                            "frames may show them on their way in (go(id, beat, { settle: true }) is to show a beat's "
                            "lines whole at once)." if moving else ""))
        for line in (self.words_only_line(), self.words_line()):
            if line:
                lines.append(f"- {line}")
        d = self.visits.get("desktop")
        moves = [m for m in ((d.film or {}).get("moves") or [])] if d else []
        read = [m for m in moves if m.get("p") is not None and m.get("t") is not None]
        if read:
            where = ("at each scene's last beat" if all(m.get("last", True) for m in read)
                     else "at each scene's last beat that changes its picture")
            lines.append(f"- Motion while a beat plays (desktop, {where}; share of the picture that "
                         "differs between progress 0.3 and 1, and between ambient time 1 s and 3 s): "
                         + "; ".join(f"{m['scene']} {_share(m['p'])} and {_share(m['t'])}" for m in read) + ".")
        key, hsh = ((d.film or {}).get("key") if d else None), ((d.film or {}).get("hash") if d else None)
        # not read is not a fault of the story: this browser draws in software, beside the other visits
        if key and key.get("unread"):
            lines.append(f"- The right arrow key, pressed at {key['from']}: whether it advances was not read "
                         f"({key['unread']}: it was busy drawing, which takes long in this browser).")
        elif key and key.get("got") == key.get("want"):
            lines.append(f"- The right arrow key goes from {key['from']} to {key['want']}.")
        if hsh and hsh.get("unread"):
            lines.append(f"- Opened at {hsh['want']}: whether the hash opens its beat was not read ({hsh['unread']}: "
                         "the page was loaded a second time, and drawing takes long in this browser).")
        elif hsh and hsh.get("got") == hsh.get("want"):
            lines.append(f"- Opened at {hsh['want']}, the story shows that beat.")
        if line := self.bytes_line("all beats"):
            lines.append(f"- {line}")
        if d and d.probe is not None and d.beats:
            if not d.probe.get("contexts"):
                lines += [f"- {x}" for x in self.scene_line()]
            else:
                at = max(d.beats, key=lambda b: (b.triangles, -b.order))
                md, mt = max(b.draws for b in d.beats), at.triangles
                lines += [f"- {x}" for x in self._cost_lines(
                    d, f"Scene (desktop, the heaviest beat): {md:,} draw call{_s(md)} and {mt:,} triangle{_s(mt)} in one "
                       f"frame" + (f" (most triangles at {_beat_name(at)})" if mt else ""), "the first beat")]
        for r in (self.film_reduced_line(), self.film_nowebgl_line()):
            if r and r[1]:
                lines.append(f"- {r[0]}")
        lines += [f"- {x}" for x in self.facts_lines()]
        lines += ["", "Pictures:"]
        for name, sheet in self.sheets.items():
            frames = [b.frame for b in self.visits[name].beats if b.frame]
            lines.append(f"- {name} contact sheet, one tile per beat: {sheet}"
                         + (f" (frames {_rel(frames[0], self.out_dir)} to {_rel(frames[-1], self.out_dir)})" if frames else ""))
        if line := self.film_stills_line():
            lines.append(f"- {line}")
        lines.append(f"- all frames, the picture-only frames and the full record (report.json) are in {self.out_dir}")
        left = max((int((v.probe or {}).get("layerLeft") or 0) for v in self.visits.values()), default=0)
        if line := camera_line(self.camera, left):
            lines.append(f"- about these pictures: {line}.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        def visit(v: Visit) -> dict:
            out = {k: getattr(v, k) for k in ("name", "load_s", "loaded", "load_error", "page_errors", "console_errors", "failed",
                                              "other_hosts", "other_failed", "idle", "memory", "overflow", "text_chars", "frame",
                                              "notes")}
            out["steps"] = [s.__dict__ for s in v.steps]
            out["between"] = [s.__dict__ for s in v.between]
            out["between_planned"], out["between_s"] = v.between_planned, v.between_s
            out["text_reads"], out["text_s"] = [s.__dict__ for s in v.text_reads], v.text_s
            out["first_notch"] = v.first_notch
            out["requests"] = v.requests
            out["probe"] = v.probe
            if v.film is not None:  # a film's record; a site's report.json is as it was
                out["film"] = v.film
                out["beats"] = [b.__dict__ for b in v.beats]
            return out

        out = {"target": self.target, "seconds": round(self.seconds, 1), "broken": self.broken(), "noticed": self.noticed(),
               "unfinished": self.unfinished, "sheets": self.sheets, "facts": self.facts, "stills": self.stills,
               "camera": self.camera,
               "visits": {k: visit(v) for k, v in self.visits.items()}, "total_bytes": self.total_bytes()}
        if self.film:
            out["film"] = self.film
        return out


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


def _text_positions(v: Visit) -> list[Step]:
    """Every position of a visit whose chapter text was read for faded lines: the positions of ``_positions`` and those
    of the text pass, in the order of the scroll."""
    return sorted([*v.steps, *v.between, *v.text_reads], key=lambda st: st.share)


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


def _faded_group(r: Any) -> dict | None:
    """One group of a faded read as the page gave it, checked again (the page can replace anything it is given)."""
    if not isinstance(r, dict) or r.get("kind") not in ("chapter", "section", "page"):
        return None
    nums = {}
    for k, hi in (("faded", 1e7), ("whole", 1e7), ("total", 1e7), ("low", 1.0)):
        x = r.get(k)
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) or not 0 <= x <= hi:
            return None
        nums[k] = x
    return {"kind": r["kind"], "group": str(r.get("group") or "")[:80], "faded": int(nums["faded"]),
            "whole": int(nums["whole"]), "total": int(nums["total"]), "low": round(float(nums["low"]), 2)}


def _faded_counts(g: dict) -> bool:
    """Whether a group's faded text at one position is enough to name (``FADED_CHARS``, ``FADED_SHARE``)."""
    return g["faded"] >= FADED_CHARS and g["faded"] >= FADED_SHARE * max(g["total"], 1)


def faded_findings(v: Visit) -> list[dict]:
    """The groups (chapters, sections, the page) whose text is faded or missing in a card that is up at some position
    read (``Step.faded``, ``FADED_CHARS``), in the order the scroll meets them: {kind, group, hits: [(position, entry)],
    worst: (position, entry)}, the worst being the position with the most characters faded (the first of equals)."""
    found: dict[tuple[str, str], dict] = {}
    for st in _text_positions(v):
        for g in st.faded or []:
            if _faded_counts(g):
                f = found.setdefault((g["kind"], g["group"]), {"kind": g["kind"], "group": g["group"], "hits": []})
                f["hits"].append((st, g))
    for f in found.values():
        f["worst"] = max(f["hits"], key=lambda t: (t[1]["faded"], -t[0].share))
    return sorted(found.values(), key=lambda f: (f["hits"][0][0].share, f["group"]))


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


def text_levels(n: int, done: list[float]) -> list[list[float]]:
    """The positions of the text pass for ``n`` photographed steps: the scroll cut into FADED_PER_STEP times as many
    even parts as the steps cut it into, the positions in ``done`` (read already) left out, in levels coarse to fine,
    each level in the order of the scroll."""
    parts = FADED_PER_STEP * max(n - 1, 1)
    seen = {round(x * parts) for x in done if abs(x * parts - round(x * parts)) < 1e-6}
    levels: list[list[float]] = []
    stride = FADED_PER_STEP
    while stride >= 1:
        level = [k for k in range(0, parts + 1, stride) if k not in seen]
        seen.update(level)
        levels.append([k / parts for k in level])
        stride //= 2
    return [lv for lv in levels if lv]


def _near_fault(share: float, positions: list[Step]) -> bool:
    """Whether the position read last before ``share`` or first after it has a subject cut or hidden: within a level the
    pass reads those first, so a fault the steps found gets its stretch of the scroll before a stretch without one
    gets a finer reading."""
    before = [p for p in positions if p.share < share]
    after = [p for p in positions if p.share > share]
    near = (([max(before, key=lambda p: p.share)] if before else [])
            + ([min(after, key=lambda p: p.share)] if after else []))
    return any(any(subject_faults(x)) for p in near for x in p.subjects or [])


async def _bounded(page, js: str, default: Any, wait: float = SUBJECTS_WAIT_S) -> Any:
    try:
        return await asyncio.wait_for(page.evaluate(js), wait)
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


# --------------------------------------------------------------------------- a story (a deck or a film)


def _share(x: float | None) -> str:
    """A share of a picture as the answer writes it: whole percent, or two decimals under 1 % (a beat that changes a
    small light changes a fraction of a percent, and '0%' would say it changed nothing)."""
    if x is None:
        return "-"
    return "0%" if x == 0 else f"{x:.2%}" if x < 0.01 else f"{x:.0%}"


def _beat_name(b: Beat) -> str:
    """A beat as the answer names it: its hash, which opens it (#<scene>-<beat, from 1>)."""
    return f"#{b.scene}-{b.beat + 1}"


def _beat_names(beats: list[Beat], n: int = 6) -> str:
    if len(beats) > n:
        return ", ".join(_beat_name(b) for b in beats[:n]) + f" and {len(beats) - n} more"
    return _and([_beat_name(b) for b in beats])


def _first_words(text: str, n: int = 60) -> str:
    """A line as the answer names it, in quotes: whole when it has at most ``n`` characters, else its first words up
    to there, cut between two words (the agent looks for them in the page's HTML)."""
    t = " ".join(str(text).split())
    if len(t) <= n:
        return f"'{t}'"
    head = t[:n + 1]
    return f"'{head.rsplit(' ', 1)[0] if ' ' in head[n // 2:] else t[:n]}...'"


def _ratio(r: float, need: float) -> str:
    """A contrast ratio as the answer writes it: to one decimal, or to two where one would put it on the other side
    of what the type needs (2.96 under a bar of 3 is not "3.0")."""
    s = f"{r:.1f}"
    return s if (float(s) >= need) == (r >= need) else f"{r:.2f}"


def _layout(b: Beat) -> str:
    """How the beat's scene lays out its words (``info().layout``); a page that says nothing shows captions."""
    return (b.info or {}).get("layout") or CAPTION


def same_picture(v: Visit) -> list[tuple[Beat, Beat, str]]:
    """The beats of a visit that show the picture of the beat before them in their scene (``FILM_SAME``), each with
    that beat and what the beat is then:

    - 'nothing': it brings no new words either. Nothing a viewer can see changes: a fault in every layout;
    - 'caption': only the caption changes. Under a caption the picture is what a beat is for: a fault, as before;
    - 'words': a slide gains words and its picture holds. A deck does that on purpose (a list built line by line, a
      figure and then its source): counted in a measure, never named.

    A beat's words are the lines it brings (``info().lines``, the current beat's own: on a slide, where the beats
    before stay up, they are what the beat adds). A caption shows one beat's lines in place of the last one's, so
    there the words are new only if they differ from the lines of the beat before."""
    out = []
    at = {(b.scene, b.beat): b for b in v.beats}
    for b in v.beats:
        prev = at.get((b.scene, b.beat - 1))
        if prev is None or b.change is None or b.change >= FILM_SAME:
            continue
        slide = _layout(b) != CAPTION
        new = bool(b.lines) and (slide or b.lines != prev.lines)
        out.append((b, prev, "nothing" if not new else "words" if slide else "caption"))
    return out


def words_unseen(b: Beat) -> bool:
    """The beat has words and shows none of them: it is not marked silent, ``info().lines`` gives lines, its element
    is in the HTML, and none of that element's lines is drawn where a viewer can read it (site_probe.js,
    ``S.filmBeat``: ``shown``, which is about the beat's own lines, so on a slide about the ones this beat adds; a
    speaker's note is never a line). A beat that was not read (no answer from the page) says nothing either way."""
    h = b.html or {}
    return bool(b.lines) and h.get("found") is True and h.get("shown") is False and not h.get("silent")


def words_need(px: float, bold: bool) -> float:
    """The least contrast type of this size may have with what is behind it (WCAG 1.4.3: 3 for large type, 4.5 for
    the rest). The size is rounded to the pixel, as the answer writes it: 18.7 px bold is WCAG's 14 pt bold."""
    large = round(px) >= WORDS_LARGE_PX or (bold and round(px) >= WORDS_BOLD_PX)
    return WORDS_NEED_LARGE if large else WORDS_NEED


#: sRGB to linear light, for a channel of 0..255
_LINEAR = np.where(np.arange(256) <= 0.04045 * 255, np.arange(256) / 255 / 12.92, ((np.arange(256) / 255 + 0.055) / 1.055) ** 2.4)
#: the weights of red, green and blue in WCAG's relative luminance
_LUMA = np.array([0.2126, 0.7152, 0.0722])


def words_contrast(behind: bytes, runs: list[dict], window: tuple[float, float], plain: bytes | None = None) -> list[dict]:
    """The contrast of each line over the picture with what is drawn behind it, as a WCAG ratio (``WORDS_CELL`` says
    how and why): ``behind`` is the window photographed with the letters' fill left out, ``runs`` the lines as
    site_probe.js found them (``wordsOver``: text, px, bold, colour, alpha, shadow, rects in CSS px), ``window`` the
    window's size in CSS px. Returns one dict per run that has a picture under it: text, own, px, bold, ratio, need
    (``words_need``), light (the words are lighter than what is behind them), shadow, cells.

    Per run: the picture under each of its line boxes is averaged over squares of a quarter of the type's size, in
    linear light; each square's ratio to the line's colour is (lighter + 0.05) / (darker + 0.05) of the two relative
    luminances; the run's ratio is the one nine tenths of its squares reach. A line drawn part see-through (an alpha
    under 1: a muted colour, an opacity) is compared as it is seen, mixed with the square behind it the way the
    browser mixes them (in sRGB values).

    ``plain``: the same photograph with the words' shadows left out as well, given where a run has a shadow of its
    own. Such a run takes the better of its two ratios: a dark halo round light words is a ground of the words' own
    and counts for them; a glow in the words' own colour, which lies densest under their strokes, where the viewer
    never sees it, must not count against them."""
    from PIL import Image

    def squares(a: np.ndarray, run: dict) -> np.ndarray:
        """The run's squares as linear RGB, one row each."""
        sx, sy = a.shape[1] / window[0], a.shape[0] / window[1]
        side = max(1, round(run["px"] / WORDS_CELL * sx))
        out = []
        for x0, y0, x1, y1 in run["rects"]:
            crop = a[max(0, round(y0 * sy)):max(0, round(y1 * sy)), max(0, round(x0 * sx)):max(0, round(x1 * sx))]
            h, w = crop.shape[:2]
            if h < 1 or w < 1:
                continue
            th, tw = min(side, h), min(side, w)  # a box under one square either way is one row or one column of them
            ny, nx = h // th, w // tw
            lin = _LINEAR[crop[:ny * th, :nx * tw]]
            out.append(lin.reshape(ny, th, nx, tw, 3).mean(axis=(1, 3)).reshape(-1, 3))
        return np.concatenate(out) if out else np.zeros((0, 3))

    def ratio(cells: np.ndarray, run: dict) -> tuple[float, bool]:
        ground = cells @ _LUMA
        colour = np.asarray(run["colour"], dtype=np.float64) / 255.0
        alpha = float(run.get("alpha", 1.0))
        if alpha < 0.995:
            encoded = np.where(cells <= 0.0031308, cells * 12.92, 1.055 * np.maximum(cells, 1e-9) ** (1 / 2.4) - 0.055)
            seen = alpha * colour + (1 - alpha) * encoded
        else:
            seen = np.broadcast_to(colour, cells.shape)
        words = np.where(seen <= 0.04045, seen / 12.92, ((seen + 0.055) / 1.055) ** 2.4) @ _LUMA
        r = (np.maximum(words, ground) + 0.05) / (np.minimum(words, ground) + 0.05)
        return float(np.percentile(r, WORDS_WORST)), bool(np.median(words) > np.median(ground))

    pictures = [np.asarray(Image.open(io.BytesIO(p)).convert("RGB")) for p in (behind, plain) if p]
    out = []
    for run in runs:
        px = float(run.get("px") or 0)
        if px <= 0 or not run.get("rects"):
            continue
        cells = [c for a in (pictures if run.get("shadow") else pictures[:1]) if len(c := squares(a, run))]
        if not cells:
            continue
        best, light = max(ratio(c, run) for c in cells)
        out.append({"text": str(run.get("text") or "")[:80], "own": bool(run.get("own")), "px": round(px, 1),
                    "bold": bool(run.get("bold")), "ratio": round(best, 2), "need": words_need(px, bool(run.get("bold"))),
                    "light": light, "shadow": bool(run.get("shadow")), "cells": len(cells[0])})
    return out


def hard_words(v: Visit) -> list[dict]:
    """The lines of a visit that are hard to read over the picture (a ratio under what their size needs), one entry
    per line in the story's order, a line that stays up through several beats being one: scene, text, the beats it
    is hard to read at, and of the worst of them its beat ('worst'), ratio, px, bold, need and light."""
    found: dict[tuple[str, str], dict] = {}
    for b in v.beats:
        for w in b.words or []:
            if w.get("ratio") is None or w["ratio"] >= w["need"]:
                continue
            g = found.setdefault((b.scene, w["text"]), {"scene": b.scene, "text": w["text"], "beats": [], "ratio": None})
            if not g["beats"] or g["beats"][-1] is not b:
                g["beats"].append(b)
            if g["ratio"] is None or w["ratio"] < g["ratio"]:
                g.update(ratio=w["ratio"], worst=b, px=w["px"], bold=w["bold"], need=w["need"], light=w["light"])
    return list(found.values())


def film_order(scenes: list[dict]) -> list[tuple[str, int]]:
    """Every beat of the film in its order: (scene id, beat index from 0)."""
    return [(s["id"], k) for s in scenes for k in range(s["beats"])]


def film_sample(scenes: list[dict], cap: int = FILM_BEATS_MAX) -> list[tuple[str, int]]:
    """The beats photographed, in film order: all of them up to ``cap``; beyond it every scene's last beat (its still and
    the reading of whether it moves are taken there; an even spread of those when the scenes alone pass ``cap``), then
    the other beats evenly spread over the film until ``cap``."""
    order = film_order(scenes)
    if len(order) <= cap:
        return order
    last = {(s["id"], s["beats"] - 1) for s in scenes if s["beats"]}
    keep = [b for b in order if b in last]
    if len(keep) >= cap:
        return [keep[round(i * (len(keep) - 1) / max(cap - 1, 1))] for i in range(cap)]
    rest = [b for b in order if b not in last]
    room = cap - len(keep)
    picked = {rest[round(i * (len(rest) - 1) / max(room - 1, 1))] for i in range(room)} if room > 1 else {rest[len(rest) // 2]}
    chosen = set(keep) | picked
    return [b for b in order if b in chosen]


def picture_change(a: bytes, b: bytes) -> float | None:
    """Share of the pixels that differ between two pictures of one size by more than 8 % of full scale in any channel
    (the rule of ``canvas_content``), at full size: what a beat changes may be a small light. None for two sizes."""
    from PIL import Image

    x, y = (np.asarray(Image.open(io.BytesIO(f)).convert("RGB"), dtype=np.int16) for f in (a, b))
    if x.shape != y.shape:
        return None
    return float((np.abs(x - y) > 0.08 * 255).any(axis=2).mean())


class _TextOf(HTMLParser):
    """The text of an HTML document, scripts, styles and templates left out, character references decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "template"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    """The text of the page's HTML as served, with no whitespace at all: a line is compared without its whitespace,
    since tags between two words (a <br>, an <em>) leave none or some, and the reader sees the same words."""
    p = _TextOf()
    with contextlib.suppress(Exception):  # a document the parser gives up on: what it read so far
        p.feed(html)
        p.close()
    return re.sub(r"\s+", "", "".join(p.parts))


def foreign_lines(lines: list[str], text: str) -> list[str]:
    """The lines of ``info().lines`` that do not occur in the HTML's text (``html_text``), whitespace aside."""
    return [x for x in lines if (s := re.sub(r"\s+", "", x)) and s not in text]


def film_findings(rep: SiteReport) -> list[dict]:
    """What the answer says of a story beyond a site's faults, in its order: one dict per finding with 'kind', 'broken'
    (True: must be fixed; False: a viewer would notice) and what its sentence needs (``SiteReport._film_sentence``).
    A fault of a beat's lines (none, not in the HTML, too long) is a fault of the page and is named once, whichever size
    found it; a fault of a picture, or of words over it, is named per size (words that are not shown: once, where both
    sizes hide the same beats)."""
    out: list[dict] = []
    f = rep.film
    if f.get("missing"):
        out.append({"kind": "missing", "broken": True, "members": f["missing"]})
    ready = f.get("ready") or {}
    if ready.get("state") in ("rejected", "pending"):
        out.append({"kind": "ready", "broken": True, "state": ready["state"], "why": ready.get("why", "")})
    if not f.get("total"):
        out.append({"kind": "no_scenes", "broken": True})
    sized = [(name, v) for name in ("desktop", "phone") if (v := rep.visits.get(name)) and v.film]
    calls: dict[tuple[str, str], dict] = {}
    for name, v in sized:
        for c in v.film.get("calls") or []:
            g = calls.setdefault((c["call"], c["why"]), {"kind": "call", "broken": True, "call": c["call"], "why": c["why"],
                                                         "lost": c["lost"], "sizes": []})
            if name not in g["sizes"]:
                g["sizes"].append(name)
    out += calls.values()
    for name, v in sized:
        empty = [b for b in v.beats if b.content is not None and b.content < EMPTY_CONTENT]
        if empty:
            out.append({"kind": "empty", "broken": True, "size": name, "beats": empty})
    beats: dict[tuple[str, int], Beat] = {}
    for _, v in sized:
        for b in v.beats:
            if not b.call and b.info is not None:
                beats.setdefault((b.scene, b.beat), b)
    ordered = sorted(beats.values(), key=lambda b: b.order)
    for b in ordered:
        if not b.lines and not (b.html or {}).get("silent"):
            out.append({"kind": "no_line", "broken": True, "beat": b})
    for b in ordered:
        for line in b.foreign:
            out.append({"kind": "foreign", "broken": True, "beat": b, "line": line})
    if rep.hosts_line():
        out.append({"kind": "hosts", "broken": True})
    for name, v in sized:
        for b, prev, what in same_picture(v):
            if what != "words":  # a slide that gains words under a picture that holds is a measure (words_only_line)
                out.append({"kind": "nothing" if what == "nothing" else "same", "broken": False, "size": name,
                            "beat": b, "prev": prev})
    d = rep.visits.get("desktop")
    for m in ((d.film or {}).get("moves") or []) if d else []:
        if m.get("p") is not None and m.get("t") is not None and m["p"] < FILM_SAME and m["t"] < FILM_SAME:
            out.append({"kind": "still", "broken": False, "move": m})
    unseen = {name: [b for b in v.beats if words_unseen(b)] for name, v in sized}
    if len(sized) == 2 and unseen[sized[0][0]] and len({tuple((b.scene, b.beat) for b in bs) for bs in unseen.values()}) == 1:
        # the same beats at both sizes, as a rule that hides the words does: said once
        out.append({"kind": "unseen", "broken": False, "sizes": [name for name, _ in sized], "beats": unseen[sized[0][0]]})
    else:
        out += [{"kind": "unseen", "broken": False, "sizes": [name], "beats": bs} for name, bs in unseen.items() if bs]
    for name, v in sized:
        slides: dict[str, dict] = {}
        for b in v.beats:
            h = b.html or {}
            if not (h.get("scrolls") or h.get("outside")):
                continue
            if h.get("over") or _layout(b) == CAPTION:
                out.append({"kind": "caption", "broken": False, "size": name, "beat": b})
            elif b.scene in slides:  # a slide keeps its lines up: what does not fit at one beat does not at the next
                slides[b.scene]["also"].append(b)
            else:
                slides[b.scene] = {"kind": "caption", "broken": False, "size": name, "beat": b, "also": []}
                out.append(slides[b.scene])
    for name, v in sized:
        hard = hard_words(v)
        out += [{"kind": "words", "broken": False, "size": name, **g} for g in hard[:WORDS_LINES]]
        if len(hard) > WORDS_LINES:
            out.append({"kind": "more_words", "broken": False, "size": name, "lines": hard[WORDS_LINES:]})
    for b in ordered:
        for line in b.lines:
            if len(line) > FILM_LINE_CHARS:
                out.append({"kind": "long", "broken": False, "beat": b, "line": line})
    if len(sized) == 2:
        (na, va), (nb, vb) = sized
        for (n1, v1), (n2, v2) in (((na, va), (nb, vb)), ((nb, vb), (na, va))):
            there = {(b.scene, b.beat): b for b in v2.beats if b.content is not None}
            low = [(b, there[(b.scene, b.beat)].content) for b in v1.beats if b.content is not None
                   and (b.scene, b.beat) in there and EMPTY_CONTENT <= b.content < NEARLY_EMPTY * there[(b.scene, b.beat)].content]
            if low:
                out.append({"kind": "tone", "broken": False, "size": n1, "other": n2, "beats": [b for b, _ in low],
                            "other_content": [x for _, x in low]})
    for k in ("key", "hash"):
        r = (d.film or {}).get(k) if d else None
        # a page opened at a hash that gave no story to ask says nothing of the hash (``film_hash``; a measure says so)
        if r and r.get("want") and r.get("got") != r["want"] and not r.get("unread"):
            out.append({"kind": k, "broken": False, **r})
    return out


def _film_hook(got: Any) -> dict | None:
    """The hook as site_probe.js read it (``S.filmHook``), checked again; None: no window.__story."""
    if not isinstance(got, dict) or got.get("hook") is not True:
        return None
    scenes = []
    seen: set[str] = set()
    for s in (got.get("scenes") or [])[:64]:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or s["id"] in seen:
            continue
        n = s.get("beats")
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 200:
            continue
        seen.add(s["id"])
        scenes.append({"id": s["id"][:80], "title": str(s.get("title") or "")[:200], "beats": n})
    r = got.get("ready") if isinstance(got.get("ready"), dict) else {}
    at = r.get("at")
    state = r.get("state") if r.get("state") in ("resolved", "rejected", "pending", "missing") else "missing"
    text = lambda k, n: str(got[k])[:n] if isinstance(got.get(k), str) else None  # noqa: E731
    return {"missing": [m for m in FILM_MEMBERS if m in (got.get("missing") or [])], "scenes": scenes,
            "total": sum(s["beats"] for s in scenes), "more": bool(got.get("more")),
            "mode": text("mode", 40), "why": text("why", 200), "title": text("title", 200),
            "treatment": text("treatment", 80),
            "ready": {"state": state, "why": str(r.get("why") or "")[:200],
                      "s": round(at / 1000, 2) if isinstance(at, (int, float)) and math.isfinite(at) and at >= 0 else None}}


def _word_runs(got: Any) -> list[dict]:
    """The lines over the picture as site_probe.js found them (``wordsOver``), every field checked again: what a page
    hands back is the page's."""
    def num(v: Any, lo: float, hi: float) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi else None

    out: list[dict] = []
    for r in got[:24] if isinstance(got, list) else []:
        if not isinstance(r, dict):
            continue
        px, alpha, colour = num(r.get("px"), 1, 2000), num(r.get("alpha"), 0, 1), r.get("colour")
        rects = [[float(x) for x in q] for q in (r.get("rects") if isinstance(r.get("rects"), list) else [])[:12]
                 if isinstance(q, list) and len(q) == 4 and all(num(x, -1e5, 1e5) is not None for x in q)
                 and q[2] > q[0] and q[3] > q[1]]
        if (px is None or alpha is None or not isinstance(colour, list) or len(colour) != 3
                or any(num(c, 0, 255) is None for c in colour) or not rects):
            continue
        out.append({"text": " ".join(str(r.get("text") or "").split())[:80], "own": r.get("own") is True, "px": px,
                    "bold": r.get("bold") is True, "colour": [float(c) for c in colour], "alpha": alpha,
                    "shadow": r.get("shadow") is True, "rects": rects})
    return out


def _film_info(i: Any) -> dict | None:
    """info() as site_probe.js cut it (``cleanInfo``), checked again."""
    if not isinstance(i, dict):
        return None
    beat = i.get("beat")
    text = lambda k, n: str(i[k]).strip()[:n] if isinstance(i.get(k), str) and i[k].strip() else None  # noqa: E731
    return {"scene": str(i["scene"])[:80] if isinstance(i.get("scene"), str) else None,
            "beat": beat if isinstance(beat, int) and not isinstance(beat, bool) else None,
            "lines": [str(x)[:2000] for x in (i.get("lines") or [])[:8] if isinstance(x, str)],
            "place": str(i.get("place") or "")[:20], "cut": str(i["cut"])[:40] if isinstance(i.get("cut"), str) else None,
            "playing": i.get("playing") is True,
            # the scene's layout (None: a caption), and the story's form and theme, where the hook gives them
            "layout": text("layout", 20), "form": text("form", 20), "theme": text("theme", 40)}


def _where_info(i: dict | None) -> str:
    """Where info() says the film is, as a hash, or ''."""
    if not i or i.get("scene") is None or i.get("beat") is None:
        return ""
    return f"#{i['scene']}-{i['beat'] + 1}"


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


def film_flags(b: Beat, same: str = "") -> list[str]:
    """The few words a story's contact sheet writes under a beat's tile, one per thing found there. ``same``: what the
    beat is where its picture is the one before it (``same_picture``); a slide that only gains words is no finding."""
    h = b.html or {}
    return [f for f, on in (("empty", b.content is not None and b.content < EMPTY_CONTENT),
                            ("same", same == "caption"), ("no change", same == "nothing"),
                            ("no line", b.info is not None and not b.lines and not h.get("silent")),
                            ("not in HTML", bool(b.foreign)), ("words not shown", words_unseen(b)),
                            ("does not fit", bool(h.get("scrolls") or h.get("outside"))),
                            ("hard to read", any(w["ratio"] < w["need"] for w in b.words or []))) if on]


def film_sheet(v: Visit, out: Path) -> bool:
    """A story's contact sheet: one tile per beat photographed, labelled with the scene, the beat (from 1) and its first
    line cut short (``FILM_LABEL_CHARS``), the findings in red. False: no beat was photographed."""
    n = FILM_LABEL_CHARS.get(v.name, 20)
    same = {(b.scene, b.beat): what for b, _, what in same_picture(v)}
    frames = []
    for b in v.beats:
        if not b.frame:
            continue
        first = " ".join((b.lines or [""])[0].split())
        flags = film_flags(b, same.get((b.scene, b.beat), ""))
        label = f"{b.scene} {b.beat + 1}  " + (first[:n] + ("..." if len(first) > n else "") if first else "(no line)")
        frames.append((b.frame, label + (f"  {', '.join(flags)}" if flags else ""), bool(flags)))
    if frames:
        contact_sheet(frames, out, 480 if v.name == "desktop" else 200, 3 if v.name == "desktop" else min(len(frames), 5))
    return bool(frames)


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
        self.faded_doc: str | None = None  # the document whose text the visit's faded reads are of (``faded_text``)
        self.document = None  # the response that brought the page: a film's lines are compared with its HTML
        self.why = ""  # why the latest call of a film's hook failed (``film_call``)

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
            self.document = await page.goto(self.url, wait_until="load", timeout=max(5.0, min(60.0, self.clock.left() - WRAP_UP_S)) * 1000)
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
            step.faded_read = await self.faded_text(page)
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
                read = await self.faded_text(page)
                self.v.between.append(Step(-1, share, round(y or 0), subjects=subs, subjects_note=note, between=True,
                                           moving=any(x.get("moving") for x in subs or []), text=text, faded_read=read))
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

    async def text_pass(self, page, n: int) -> None:
        """Read the chapter text alone where no position of the visit has read it (``text_levels``, ``FADED_PER_STEP``),
        with the page's WebGL draw calls left out (site_probe.js, ``S.quiet``), until ``FADED_PASS_S`` is used up. Only
        after a pass between the steps (a page with the subjects hook), as that pass. Nothing is photographed after
        it."""
        if not self.v.between_planned or self.faded_doc is None:
            return
        geo = await _bounded(page, "window.__agySite.geometry()", None)
        if not isinstance(geo, dict):
            return
        t0 = time.monotonic()
        await _bounded(page, "window.__agySite.quietDraws(true)", None)
        try:
            for level in text_levels(n, [st.share for st in _text_positions(self.v) if st.faded_read is not None]):
                for share in level:
                    left = min(FADED_PASS_S - (time.monotonic() - t0), self.clock.left() - WRAP_UP_S - 2.0)
                    if left < 0.5:
                        break
                    await self.scroll_to(page, round(geo["max"] * share))
                    read = await self.faded_text(page, "window.__agySite.readText()", min(left, FADED_WAIT_S + 1.0))
                    if read is None:
                        continue
                    y = await _bounded(page, "window.__agySite.y()", 0)
                    self.v.text_reads.append(Step(-1, share, round(y or 0), between=True, faded_read=read,
                                                  subjects_note="chapter text only"))
        finally:
            await _bounded(page, "window.__agySite.quietDraws(false)", None)
        self.v.text_reads.sort(key=lambda st: st.share)
        self.v.text_s = round(time.monotonic() - t0, 2)
        self.log(f"read the chapter text at {len(self.v.text_reads)} more positions in {self.v.text_s:.1f} s")

    async def faded_text(self, page, js: str = "window.__agySite.fadedText()", wait: float = FADED_WAIT_S) -> int | None:
        """Read the chapter text in the cards that are up here (site_probe.js, ``S.fadedText``): which read of the page's
        log it is, or None (no read, or a document other than the one read first in this visit: identity across reads
        is the text node itself, so another document's log says nothing about this one's nodes)."""
        if self.clock.left() < WRAP_UP_S + 2.0:
            return None
        got = await _bounded(page, js, None, wait)
        if not isinstance(got, dict) or not isinstance(got.get("read"), int) or isinstance(got.get("read"), bool):
            return None
        doc = str(got.get("doc") or "")
        if self.faded_doc is None:
            self.faded_doc = doc
        return got["read"] if doc == self.faded_doc else None

    async def faded(self, page) -> None:
        """After the visit's reads: what was faded at each, judged by the fullest each line was seen at anywhere
        (site_probe.js, ``S.fadedReport``), attached to its position as ``Step.faded``. A read still changing at the end
        of its wait, or of another document, stays None. The page's answer is checked again here."""
        read = [st for st in _text_positions(self.v) if st.faded_read is not None]
        if not read or self.faded_doc is None:
            return
        got = await _bounded(page, "window.__agySite.fadedReport()", None)
        if (not isinstance(got, dict) or str(got.get("doc") or "") != self.faded_doc
                or not isinstance(got.get("reads"), list)):
            return
        reads = got["reads"]
        for st in read:
            r = reads[st.faded_read] if 0 <= st.faded_read < len(reads) else None
            if not isinstance(r, dict) or r.get("settled") is not True or not isinstance(r.get("groups"), list):
                continue
            st.faded = [g for g in (_faded_group(x) for x in r["groups"][:SUBJECTS_READ]) if g]

    # ---- a story (a deck or a film)
    async def read_film(self, page, wait: float) -> None:
        """Whether the page is a story (window.__story), and its hook after waiting at most ``wait`` for ready: sets
        ``Visit.film`` to {'hook': ...}, or leaves it None for a page without the hook."""
        got = await _bounded(page, f"window.__agySite.filmWait({round(wait * 1000)})", None, wait + 3.0)
        hook = _film_hook(got)
        if hook is not None:
            self.v.film = {"hook": hook, "calls": []}
            self.log(f"a story: {hook['total']} beats in {len(hook['scenes'])} scenes, ready {hook['ready']}")

    async def film_call(self, page, method: str, args: list, picture: str | None, lost: str) -> dict | None:
        """One call of the film's hook, bounded by ``FILM_CALL_S`` (in the page, and here for a page stuck in its own
        code). ``picture``: 'image/jpeg', or 'both' for a PNG beside it, read in the task the call resolved in. A call
        that hangs or rejects is recorded with what was lost by it (``lost``), and None is returned."""
        call = f"{method}(" + ", ".join(f"'{a}'" if isinstance(a, str) else "{ settle: true }" if isinstance(a, dict)
                                       else str(a) for a in args) + ")"
        js = (f"window.__agySite.filmCall({json.dumps(method)}, {json.dumps(args)}, {round(FILM_CALL_S * 1000)}, "
              f"{json.dumps(picture)})")
        got = await _bounded(page, js, {"why": "late"}, FILM_CALL_S + 3.0)
        if not isinstance(got, dict) or got.get("ok") is not True:
            why = str((got or {}).get("why") or "late")[:200] if isinstance(got, dict) else "late"
            if why != "missing":  # a member the hook lacks is named once, with the others
                self.v.film["calls"].append({"call": call, "method": method, "why": why, "lost": lost})
            self.log(f"story: {call} failed ({why})")
            self.why = why
            return None
        return got

    async def film_picture(self, page, got: dict, key: str = "picture") -> bytes | None:
        """The picture a hook call left: read in its own task by the probe, or else the canvas photographed alone in
        the frames that follow (``canvas_alone``); None without a canvas in view."""
        import base64

        data = got.get(key)
        if isinstance(data, str) and data.startswith("data:image/") and len(data) < 64_000_000:
            with contextlib.suppress(Exception):
                return base64.b64decode(data.split(",", 1)[1])
        end = await _bounded(page, "window.__agySite.stepEnd(false)", None)
        if not isinstance(end, dict) or not end.get("rect"):
            return None
        with contextlib.suppress(Exception):
            return await asyncio.wait_for(self.canvas_alone(page, end["rect"]), FILM_CALL_S)
        return None

    async def words(self, page, runs: list[dict], window: tuple[float, float]) -> list[dict] | None:
        """The lines over the picture read against what is drawn behind them (``words_contrast``): the window
        photographed with the letters' fill left out (site_probe.js, ``S.wordsOff``, says why a photograph), and,
        only where a line with a shadow of its own then reads under what its size needs, once more with the words'
        shadows left out as well, the line keeping the better of its two readings. So a halo that does its work
        costs one photograph, and a glow is never held against its words. The page is put back whatever happens;
        None where it could not be photographed."""
        off = lambda mode: _bounded(page, f"window.__agySite.wordsOff({mode})", None)  # noqa: E731
        try:
            if await off(1) is not True:
                return None
            fill = await asyncio.wait_for(self.shot(), FILM_CALL_S)
            read = words_contrast(fill, runs, window)
            if any(w["shadow"] and w["ratio"] < w["need"] for w in read) and await off(2) is True:
                read = words_contrast(fill, runs, window, await asyncio.wait_for(self.shot(), FILM_CALL_S))
            return read
        except Exception:  # noqa: BLE001  a page that cannot be photographed now: the beat goes without the reading
            return None
        finally:
            await off(0)

    async def beats(self, page, stills: bool) -> dict:
        """Photograph the story beat by beat (``film_sample``): go(scene, beat) settled, the picture alone, the
        scene's text brought to rest (site_probe.js, ``S.filmRest``), two frames, the window, info(), what the HTML
        says of that beat (``S.filmBeat``) and, where lines lie over the picture, their contrast with what is drawn
        behind them (``words``, ``words_contrast``). With ``stills``, each scene's last beat is also written as
        ``stills/<scene id>.webp``. Returns the stills record.

        What the words cost a beat, measured with the four visits side by side on four cores that other work was
        using too. The rest: 2 ms in the page where the text stands whole (it shares the call that waits for the
        window's two frames), and one more look, 0.1 s, where a line is part see-through on purpose or the beat's
        own lines are not drawn. The reading, where lines lie over the picture: one photograph of the window, two
        calls to take the letters' fill away and put it back, and the squares, 0.11 s at 1440 x 900 and 0.08 s at
        390 x 844 on a deck of 13 beats; 0.14 to 0.17 s and 0.10 to 0.11 s on a film of 48 beats drawn by the
        starter's engine whose every beat sets two lines with a halo over the picture, 24 of them photographed
        twice on the desktop and 12 on the phone. Nothing where no line lies over the picture, as in seven of the
        example film's eight beats. That film of 48 beats was checked in 42 s where it took 35 s before the words
        were read (medians of three): 7 s more for the desktop's beats, 4 s more for the phone's. ``WORDS_S`` bounds
        the reading for a page whose photographs are slow."""
        import base64

        f = self.v.film
        hook = f["hook"]
        plan = film_sample(hook["scenes"])
        f["planned"], f["total"], f["rest_s"], f["words_s"], f["words_unread"] = len(plan), hook["total"], 0.0, 0.0, 0
        restless = 0  # beats whose text was still changing after its wait (WORDS_RESTLESS)
        order = {b: i for i, b in enumerate(film_order(hook["scenes"]))}
        last = {(s["id"], s["beats"] - 1) for s in hook["scenes"] if s["beats"]}
        html = ""
        if self.document is not None:
            with contextlib.suppress(Exception):
                html = await asyncio.wait_for(self.document.text(), 5.0)
        text = html_text(html)
        ids = [s["id"] for s in hook["scenes"]]
        names = dict(zip(ids, still_names(ids)))
        folder = (self.out / "stills").resolve()
        made: list[dict] = []
        skipped: list[dict] = []
        pictures: dict[tuple[str, int], bytes] = {}
        t0 = time.monotonic()
        for scene, k in plan:
            if self.clock.left() < WRAP_UP_S + 2.0:
                break
            tb = time.monotonic()
            b = Beat(scene, k, order[(scene, k)], first=k == 0, last=(scene, k) in last)
            self.v.beats.append(b)
            want_still = stills and b.last
            got = await self.film_call(page, "go", [scene, k, {"settle": True}], "both" if want_still else "image/jpeg",
                                       f"beat #{scene}-{k + 1} was not photographed")
            if got is None:
                b.call = self.why
                if want_still:
                    skipped.append({"id": scene, "why": "its last beat did not answer"})
                continue
            b.info = _film_info(got.get("info"))
            b.lines = (b.info or {}).get("lines") or []
            b.foreign = foreign_lines(b.lines, text) if text else []
            for key in ("form", "theme"):  # the story's, as the first beat that names them says
                if not f.get(key) and (b.info or {}).get(key):
                    f[key] = b.info[key]
            pic = await self.film_picture(page, got)
            # the text brought to rest, then the two frames the window's photograph needs, in one call of the page
            # (in the page: the wait, one look more, and 2.5 s at most for the frames)
            wait = WORDS_REST_S if restless < WORDS_RESTLESS else 0.0
            rest = await _bounded(page, f"window.__agySite.filmRest({json.dumps(scene)}, {k}, {round(wait * 1000)}, 2)",
                                  None, wait + 4.0)
            if isinstance(rest, dict):
                restless += rest.get("settled") is False
                ms = rest.get("ms")
                f["rest_s"] += ms / 1000 if isinstance(ms, (int, float)) and 0 <= ms < 60_000 else 0.0
            shot = await self.shot()
            end = await _bounded(page, "window.__agySite.stepEnd(false)", None)
            w = (end or {}).get("win") or {} if isinstance(end, dict) else {}
            b.draws, b.triangles = int(w.get("maxDraws", 0)), int(w.get("maxTris", 0))
            b.html = await _bounded(page, f"window.__agySite.filmBeat({json.dumps(scene)}, {k})", None)
            if isinstance(b.html, dict):
                b.html["rest"] = rest if isinstance(rest, dict) else None
                runs, win = _word_runs(b.html.pop("words", None)), b.html.pop("win", None)
                b.words = []
                if runs and isinstance(win, list) and len(win) == 2 and all(isinstance(x, (int, float)) and x > 0 for x in win):
                    if f["words_s"] >= WORDS_S:  # the reading's time is used up: the beat is photographed without it
                        b.words, f["words_unread"] = None, f["words_unread"] + 1
                    else:
                        tw = time.monotonic()
                        b.words = await self.words(page, runs, (win[0], win[1]))
                        f["words_s"] += time.monotonic() - tw
            fw = self.out / f"{self.v.name}_b{b.order:02d}.jpg"
            fw.write_bytes(shot)
            b.frame = str(fw)
            if pic is not None:
                fc = self.out / f"{self.v.name}_b{b.order:02d}_canvas.jpg"
                fc.write_bytes(pic)
                b.canvas_frame = str(fc)
                b.content = round(canvas_content(pic), 4)
                pictures[(scene, k)] = pic
                if (prev := pictures.get((scene, k - 1))) is not None:
                    c = picture_change(prev, pic)
                    b.change = None if c is None else round(c, 5)
            if want_still:
                png = got.get("png")
                data = png if isinstance(png, str) and png.startswith("data:image/") else (
                    "data:image/jpeg;base64," + base64.b64encode(pic).decode() if pic is not None else None)
                if data is None:
                    skipped.append({"id": scene, "why": "no picture"})
                else:
                    try:
                        webp, size = await asyncio.to_thread(still_webp, data)
                        folder.mkdir(exist_ok=True)
                        fs = folder / f"{names[scene]}.webp"
                        fs.write_bytes(webp)
                        made.append({"id": scene, "file": fs.name, "beat": _beat_name(b), "width": size[0],
                                     "height": size[1], "bytes": len(webp), "content": round(canvas_content(webp), 4),
                                     "how": "read as the beat was drawn" if isinstance(png, str) else "photographed"})
                    except Exception as e:  # noqa: BLE001  a picture Pillow cannot read
                        skipped.append({"id": scene, "why": f"unreadable picture ({type(e).__name__})"})
            b.seconds = round(time.monotonic() - tb, 2)
        f["beats_s"], f["rest_s"], f["words_s"] = round(time.monotonic() - t0, 2), round(f["rest_s"], 2), round(f["words_s"], 2)
        self.log(f"story: {len(self.v.beats)} of {len(plan)} beats in {f['beats_s']:.1f} s; the text's rest "
                 f"{f['rest_s']:.2f} s, the words over the picture {f['words_s']:.2f} s")
        return {"dir": str(folder), "made": made, "skipped": skipped} if stills else {}

    async def film_moves(self, page) -> None:
        """Whether each scene moves while a beat plays: at(scene, beat, 0.3, 1) against at(scene, beat, 1, 1) (the
        beat's own motion), and that against at(scene, beat, 1, 3) (ambient time), the pictures read as
        ``film_picture`` reads them, compared by ``picture_change``. Within ``FILM_MOVES_S``.

        Read at the scene's last beat, or, on a slide whose last beats only add words, at the last beat that changes
        its picture (``same_picture``; its first beat where none does): a slide that ends on a line of text over a
        picture that holds would otherwise be told that it stands still, for doing what a deck does."""
        f = self.v.film
        f["moves"] = []
        t0 = time.monotonic()
        held = {(b.scene, b.beat) for b, _, what in same_picture(self.v) if what == "words"}
        for s in f["hook"]["scenes"]:
            if not s["beats"]:
                continue
            if time.monotonic() - t0 > FILM_MOVES_S or self.clock.left() < WRAP_UP_S + 4.0:
                f["moves"].append({"scene": s["id"], "why": "no time left"})
                continue
            last = s["beats"] - 1
            while last > 0 and (s["id"], last) in held:
                last -= 1
            pics = []
            for p, t in ((0.3, 1), (1, 1), (1, 3)):
                got = await self.film_call(page, "at", [s["id"], last, p, t], "image/jpeg",
                                           f"whether scene '{s['id']}' moves was not read")
                pic = await self.film_picture(page, got) if got else None
                if pic is None:
                    break
                pics.append(pic)
            m: dict = {"scene": s["id"], "beat": f"#{s['id']}-{last + 1}", "last": last == s["beats"] - 1}
            if len(pics) == 3:
                m["p"], m["t"] = picture_change(pics[0], pics[1]), picture_change(pics[1], pics[2])
            else:
                m["why"] = "not read"
            f["moves"].append(m)

    async def _film_where(self, page, other: str, cap: float) -> tuple[str, str]:
        """Where info() says the story is once it is no longer ``other`` (polled for at most ``cap``): (hash, why
        not). ``UNANSWERED`` where the page gave no answer at all in the time a read has: that is no word about where
        the story is."""
        t0, where = time.monotonic(), ""
        while True:
            got = await _bounded(page, "window.__agySite.filmInfo()", UNANSWERED)
            where = "" if got == UNANSWERED else _where_info(_film_info(got))
            if (where and where != other) or time.monotonic() - t0 > cap:
                return where, "" if where else UNANSWERED if got == UNANSWERED else "info() gave no scene and beat"
            await asyncio.sleep(0.1)

    async def film_key(self, page) -> None:
        """Whether a real press of the right arrow key advances: from the film's first beat, settled, to the next."""
        order = film_order(self.v.film["hook"]["scenes"])
        if len(order) < 2:
            return
        (s0, k0), (s1, k1) = order[0], order[1]
        if await self.film_call(page, "go", [s0, k0, {"settle": True}], None, "the right arrow key was not tried") is None:
            return
        frm, want = f"#{s0}-{k0 + 1}", f"#{s1}-{k1 + 1}"
        with contextlib.suppress(Exception):
            await page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
            await asyncio.wait_for(page.keyboard.press("ArrowRight"), 5.0)
        got, why = await self._film_where(page, frm, FILM_KEY_S)
        self.v.film["key"] = {"from": frm, "want": want, "got": got, "why": why}
        if why == UNANSWERED:  # nothing is known of the key (film_hash says how this was found)
            self.v.film["key"]["unread"] = UNANSWERED

    async def film_hash(self, ctx, canvas_layer: bool) -> None:
        """Whether the hash opens its beat: the page loaded afresh, in a page of its own, at #<scene>-<beat> of the
        story's middle beat, and info() read once the story is ready.

        'unread': why nothing is known of the hash, where this second load gave no story to ask: ready had not
        resolved in the time it had, the page had no WebGL this time, or it gave no answer when asked where it was.
        Then nothing is said of the hash but that. A deck of 13 beats and 260,000 triangles, checked while the
        machine's four cores had other work, was ready on this load and then busy drawing for longer than a read of
        info() waits, in three checks of four; before this was told apart the answer read "the hash does not open
        its beat ... the story did not say which beat it showed", of a page that opens it."""
        order = film_order(self.v.film["hook"]["scenes"])
        if len(order) < 2 or self.clock.left() < WRAP_UP_S + 8.0:
            return
        s, k = order[len(order) // 2]
        want = f"#{s}-{k + 1}"
        rec: dict = {"want": want, "got": "", "why": ""}
        page = await ctx.new_page()
        try:
            await page.add_init_script(_probe_script(False, canvas_layer=canvas_layer))
            wait = max(2.0, min(FILM_READY_S, self.clock.left() - WRAP_UP_S - 6.0))
            await page.goto(self.url.split("#")[0] + want, wait_until="load", timeout=wait * 1000)
            hook = _film_hook(await _bounded(page, f"window.__agySite.filmWait({round(wait * 1000)})", None, wait + 3.0))
            rec["hook"] = {"ready": hook["ready"]["state"], "mode": hook["mode"]} if hook else None
            if hook is not None and hook["ready"]["state"] == "pending":
                rec["unread"] = f"the story was not ready {wait:.0f} s after that load"
            elif hook is not None and hook["mode"] == "no-webgl" and self.v.film["hook"].get("mode") != "no-webgl":
                rec["unread"] = "the page had no WebGL on that load" + (f" ({_q(hook['why'], 80)})" if hook.get("why") else "")
            else:
                got, why = await self._film_where(page, "", 0.0)
                if got != want:  # a page that goes to the hash's beat after ready: a moment more
                    got, why = await self._film_where(page, got or "-", FILM_KEY_S)
                rec.update(got=got or "", why=why)
                if why == UNANSWERED:
                    rec["unread"] = UNANSWERED
        except Exception as e:  # noqa: BLE001  a page that does not load a second time: said, and the visit goes on
            rec["why"] = f"the page did not load again ({type(e).__name__})"
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(page.close(), 5.0)
        self.v.film["hash"] = rec

    async def film_reduced(self, page) -> None:
        """Under reduce motion, a beat reached as a viewer reaches it (advance() from the beat before the film's middle
        beat) must be shown finished at once: the window photographed two frames after the call and again a second
        later, compared by ``picture_change``."""
        order = film_order(self.v.film["hook"]["scenes"])
        if not order:
            return
        m = max(1, len(order) // 2) if len(order) > 1 else 0
        s, k = order[m]
        rec: dict = {"beat": f"#{s}-{k + 1}"}
        self.v.film["reduced"] = rec
        if m:
            ps, pk = order[m - 1]
            ok = await self.film_call(page, "go", [ps, pk, {"settle": True}], None, "reduce motion was not read")
            ok = ok and await self.film_call(page, "advance", [], None, "reduce motion was not read")
        else:
            ok = await self.film_call(page, "go", [s, k], None, "reduce motion was not read")
        if not ok:
            rec["why"] = "the hook did not answer"
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(page.evaluate("(n) => window.__agySite.afterFrames(n)", 2), 3.0)
        a = await self.shot_png()
        await asyncio.sleep(1.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(page.evaluate("(n) => window.__agySite.afterFrames(n)", 2), 3.0)
        b = await self.shot_png()
        rec["change"] = picture_change(a, b)
        f = self.out / f"{self.v.name}.jpg"
        from PIL import Image

        Image.open(io.BytesIO(b)).convert("RGB").save(f, "JPEG", quality=80)
        self.v.frame = str(f)

    async def film_story(self, page) -> None:
        """What the page without WebGL shows of the story (site_probe.js, ``S.filmStory``): kept for a page that has
        the film's hook or its sections."""
        got = await _bounded(page, "window.__agySite.filmStory()", None)
        if isinstance(got, dict) and (got.get("hook") or got.get("withText")):
            num = lambda k: int(got[k]) if isinstance(got.get(k), int) and 0 <= got[k] < 1e8 else 0  # noqa: E731
            self.v.film = {**(self.v.film or {}), "story": {
                "hook": got.get("hook") is True, "mode": str(got["mode"])[:40] if isinstance(got.get("mode"), str) else None,
                "why": str(got["why"])[:200] if isinstance(got.get("why"), str) else None,
                "withText": num("withText"), "shown": num("shown"), "chars": num("chars")}}


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
                # a film (window.__story) is stepped beat by beat in every visit instead of scrolled; its ready is
                # waited for first, so that what follows sees the film and not its "Loading"
                await vis.read_film(page, FILM_READY_NOGL_S if no_webgl else FILM_READY_S)
                film, film_stills = vis.v.film is not None and not no_webgl, {}
                if idle:
                    vis.v.idle = await vis.idle(page)
                if facts:
                    with contextlib.suppress(Exception):
                        vis.v.facts_raw = await page.evaluate(_FACTS_JS)
                if do_steps and film:
                    film_stills = await vis.beats(page, stills)
                    if name == "desktop":
                        await vis.film_moves(page)
                        await vis.film_key(page)
                        await vis.film_hash(ctx, rep.camera == "layer")
                elif do_steps:
                    await vis.steps(page, do_steps, canvas, sideways, notch=name == "desktop")
                    await vis.between(page, do_steps)
                    await vis.text_pass(page, do_steps)
                    await vis.faded(page)
                if one_step is not None and film:
                    await vis.film_reduced(page)
                elif one_step is not None:
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
                if no_webgl:
                    await vis.film_story(page)
                if stills and film:
                    rep.stills = film_stills  # one per scene, taken at its last beat by the beats pass
                elif stills:
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
            if v and v.film is not None:
                # beats not tried for want of time; a beat whose call failed is named with the call
                if v.loaded and len(v.beats) < v.film.get("planned", 0):
                    shot = len([b for b in v.beats if b.frame])
                    rep.unfinished.append(f"{name}: {shot} of {v.film['planned']} beats photographed")
            elif v and v.loaded and len(v.steps) < steps:
                rep.unfinished.append(f"{name}: {len(v.steps)} of {steps} steps photographed")
            elif v and not v.loaded and not v.load_error:
                rep.unfinished.append(f"{name}: the page had not loaded")
    finally:
        if server is not None:
            await asyncio.to_thread(server.close)

    # pictures and facts
    for name in ("desktop", "phone"):
        v = rep.visits.get(name)
        if v and v.film is not None and not rep.film:
            rep.film = {**v.film["hook"], "size": name, "form": v.film.get("form"), "theme": v.film.get("theme")}
        if v and v.film is not None:
            if film_sheet(v, out / f"sheet_{name}.jpg"):
                rep.sheets[name] = str(out / f"sheet_{name}.jpg")
        elif v and v.steps:
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
