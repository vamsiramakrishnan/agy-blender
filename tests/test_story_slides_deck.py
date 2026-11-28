"""The story-slides-3d starter as a deck: slides in HTML over the picture, laid out by each scene's data-layout, in one
of five themes, with a presenter's controls. What the HTML and CSS side does (the contract, story/CONTRACT2.md,
sections 1 and 2), checked in a headless browser on a small deck this file writes.

The deck is the starter with three files replaced (``index.html``, ``src/story.js`` and one scene file, ``sky.js``: a
box and a few spheres under a light, used for every scene), so these tests are about the page, not about the example
deck's stage. It is built the way an agent's project builds it (Vite over ``three`` from
``AGY3D_STARTER_NODE_MODULES``, or ``tests/fixtures/web_project/node_modules``) and served from a routed fake origin
under a sub-path. Without Playwright's browser, or without a workspace that has Vite and three.js, the browser tests
skip with the reason.

What is checked: every layout puts its words where the contract says, inside the frame and uncut, at a desktop and a
phone size; beats accumulate without moving the words before them, and a beat marked ``swap`` replaces them; a beat's
lines come in timed, 80 ms apart, and at once under reduced motion; without script, and without WebGL, the page is
the article, every heading and beat in order; each of the five themes gives the page its own colours and faces, and
the key t goes through them; words over a picture as dark as they are stand on the theme's scrim in ``poster``,
``halftone`` and ``glyph``, and in any theme for a scene that asks; the overview lists the scenes and goes to one; the progress line
and the count follow; the deck's strip and controls fade when the pointer rests; a note shows over the slide and is
never one of its lines; the hook says the layout, the form and the theme; a film keeps its caption under the picture
and lays any other layout over it; the build passes with the font packages and without them.
"""

from __future__ import annotations

import io
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.plugin_install import bundle_dir

REPO = Path(__file__).resolve().parent.parent
STARTER = bundle_dir() / "skills" / "story-slides-3d" / "starter"
FIXTURE = REPO / "tests" / "fixtures" / "web_project"
NODE_MODULES = Path(os.environ.get("AGY3D_STARTER_NODE_MODULES") or FIXTURE / "node_modules")
ORIGIN = "http://deck.test"
SUB = "/some/sub/path/"  # a deck that only works at the server root fails here, as it would on a real host
DRIVER_NOISE = re.compile(r"GL Driver Message|GPU stall due to ReadPixels|Automatic fallback to software WebGL")
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
SMALL = {"width": 960, "height": 600}  # the size of the tests that are not about framing: the software renderer is slow
WALL = {"width": 1920, "height": 1080}  # a projector: the type and the margins scale with the frame
THEMES = ["paper", "ink", "poster", "halftone", "glyph"]
FONT_PACKAGES = ["@fontsource", "@fontsource-variable"]
# The tokens the contract names (theme.css), which the stage reads too, and the one the scrim adds.
TOKENS = ["--paper", "--paper-2", "--ink", "--muted", "--accent", "--accent-2", "--lit", "--shade", "--font-display",
          "--font-text", "--font-mono", "--weight-display", "--radius", "--ease", "--dur", "--scrim"]
# The themes whose words over the picture stand on a scrim without a scene asking (theme.css says why for each).
SCRIMMED = ("poster", "halftone", "glyph")

# The deck: nine scenes, every layout, nineteen beats. The words of six of them are the example deck's (the contract's
# "How heavy is a cloud?"), so a look at these frames is a look at that deck's slides; three more hold what it has
# not: a column with a list, a column with a drawing, a typed caption. (id, layout, beats)
SCENES = [("cloud", "title", 1), ("looks", "statement", 2), ("drops", "statement", 3), ("weight", "number", 3),
          ("sums", "left", 2), ("drop", "right", 2), ("floats", "statement", 3), ("aside", "caption", 2), ("up", "end", 1)]
LAYOUT = {i: layout for i, layout, _ in SCENES}
BEATS = sum(n for _, _, n in SCENES)
# What each scene says: its heading, then each beat's lines, as the page below has them.
WORDS = {
    "cloud": ("How heavy is a cloud?", [["A short talk with one cloud in it."]]),
    "looks": ("What it looks like", [["It looks like nothing at all."], ["Take an ordinary one: a kilometre long, wide and tall."]]),
    "drops": ("What it is made of", [["It is water. Drops a fiftieth of a millimetre across."], ["Half a gram of them in every cubic metre."],
                                     ["A hundred thousand million million drops in all."]]),
    "weight": ("What it weighs", [["Pour them together."], ["A cube of water eight metres on a side."], ["500 tonnes", "1 km3 x 0.5 g per m3"]]),
    "sums": ("The sum, in three lines", [["Two things are assumed. The rest follows from them."],
                                         ["A cloud a kilometre each way", "Half a gram of water in each cubic metre", "So: five hundred tonnes of it"]]),
    "drop": ("One drop", [["A fiftieth of a millimetre across: a tenth of a hair."], ["The drop, and the width of a hair beside it."]]),
    "floats": ("Why it stays up", [["So why is it up there?"], ["Each drop falls about a centimetre a second."],
                                   ["The air under the cloud rises a hundred times faster."]]),
    "aside": ("A caption", [["In a film the words sit under the picture."], ["In a deck they are a lower third over it, typed in."]]),
    "up": ("Look up.", [["Five hundred tonnes, held up by a breeze."]]),
}
SWAPS = {"floats": [2]}  # the beats marked data-beat="swap": each replaces what the scene showed before it
NOTES = {("cloud", 0): "Let the cloud drift for a breath before the first word.", ("weight", 2): "Say the number, then say nothing for two seconds."}

DECK_HTML = """<!doctype html>
<html lang="en"%(theme)s>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>How heavy is a cloud?</title>
  <link rel="stylesheet" href="./src/style.css">
</head>
<body>
  <header class="strip">
    <span class="mark">How heavy is a cloud?</span>
    <span class="count" id="count"></span>
  </header>
  <main class="picture" id="picture">
    <canvas id="stage" aria-hidden="true"></canvas>
    <p class="loading" id="loading" role="status"></p>
  </main>
  <p class="note" id="note" hidden>This browser cannot show the pictures, so here is the text.</p>
  <article id="story" aria-live="polite">
    <section data-scene="cloud" data-layout="title">
      <h1>How heavy is a cloud?</h1>
      <div data-beat>
        <p class="lede">A short talk with one cloud in it.</p>
        <aside data-notes>Let the cloud drift for a breath before the first word.</aside>
      </div>
    </section>

    <section data-scene="looks" data-layout="statement">
      <h2 class="kicker">What it looks like</h2>
      <div data-beat><p>It looks like nothing at all.</p></div>
      <div data-beat><p>Take an ordinary one: a kilometre long, wide and tall.</p></div>
    </section>

    <section data-scene="drops" data-layout="statement">
      <h2 class="kicker">What it is made of</h2>
      <div data-beat><p>It is water. Drops a fiftieth of a millimetre across.</p></div>
      <div data-beat><p>Half a gram of them in every cubic metre.</p></div>
      <div data-beat><p>A hundred thousand million million drops in all.</p></div>
    </section>

    <section data-scene="weight" data-layout="number">
      <h2 class="kicker">What it weighs</h2>
      <div data-beat><p>Pour them together.</p></div>
      <div data-beat><p>A cube of water eight metres on a side.</p></div>
      <div data-beat>
        <p class="big">500 tonnes</p>
        <p class="source">1 km3 x 0.5 g per m3</p>
        <aside data-notes>Say the number, then say nothing for two seconds.</aside>
      </div>
    </section>

    <section data-scene="sums" data-layout="left">
      <h2>The sum, in three lines</h2>
      <div data-beat><p>Two things are assumed. The rest follows from them.</p></div>
      <div data-beat>
        <ul>
          <li>A cloud a kilometre each way</li>
          <li>Half a gram of water in each cubic metre</li>
          <li>So: five hundred tonnes of it</li>
        </ul>
      </div>
    </section>

    <section data-scene="drop" data-layout="right">
      <h2>One drop</h2>
      <div data-beat><p>A fiftieth of a millimetre across: a tenth of a hair.</p></div>
      <div data-beat>
        <figure>
          <svg viewBox="0 0 240 90" role="img" aria-label="A drop beside a hair, to scale">
            <circle cx="34" cy="45" r="9" fill="var(--accent)"/>
            <rect x="96" y="2" width="90" height="86" fill="none" stroke="currentColor" stroke-width="2"/>
            <path d="M25 70h18M25 66v8M43 66v8" fill="none" stroke="currentColor" stroke-width="2"/>
          </svg>
          <figcaption>The drop, and the width of a hair beside it.</figcaption>
        </figure>
      </div>
    </section>

    <section data-scene="floats" data-layout="statement">
      <h2 class="kicker">Why it stays up</h2>
      <div data-beat><p>So why is it up there?</p></div>
      <div data-beat><p>Each drop falls about a centimetre a second.</p></div>
      <div data-beat="swap"><p>The air under the cloud rises a hundred times faster.</p></div>
    </section>

    <section data-scene="aside">
      <h2>A caption</h2>
      <div data-beat><p>In a film the words sit under the picture.</p></div>
      <div data-beat><p>In a deck they are a lower third over it, typed in.</p></div>
    </section>

    <section data-scene="up" data-layout="end">
      <h2>Look up.</h2>
      <div data-beat><p>Five hundred tonnes, held up by a breeze.</p></div>
    </section>
  </article>
  <nav class="controls" aria-label="Story">
    <button type="button" id="back">Back</button>
    <button type="button" id="auto" aria-pressed="false">Auto</button>
    <button type="button" id="next">Next</button>
  </nav>
  <script type="module" src="./src/main.js"></script>
</body>
</html>
"""

# One scene file for every scene: a ground, a box, a cloud of spheres, one light that casts shadows, painted in the
# theme's colours as the page's CSS gives them when the scene is built. Each beat turns the box and moves the camera
# a little, so every beat changes the picture; update(s) reads nothing but s. `dark` paints the whole picture in the
# theme's ink, the worst ground there is for words set in that ink.
SKY_JS = """import * as THREE from 'three';

export const token = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

export function sky(id, beats, { dark = false, from = [6, 2.6, 12], look = [3, 2.3, 0], side = 1 } = {}) {
  return {
    id,
    beats: Array.from({ length: beats }, () => ({ dur: 0.6, hold: 0.6 })),
    create(kit) {
      const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(36, 1, 0.3, 120);
      const paint = (name, fallback) => kit.mat(dark ? token('--ink', '#111111') : token(name, fallback), 0.9);
      scene.background = new THREE.Color(dark ? token('--ink', '#111111') : token('--paper', '#eeeeee'));
      const ground = kit.plane(120, 120, paint('--paper-2', '#cccccc'));
      const box = kit.box(1.6, 1.6, 1.6, paint('--accent', '#3355ff'), 2.4, 0.8, 0.6);
      const house = kit.box(1, 0.8, 1.2, paint('--lit', '#ffffff'), 4.6, 0.4, -1.5);
      const cloud = new THREE.Group();
      [[0, 0, 0, 1.1], [1.2, 0.2, 0.2, 0.9], [-1.1, 0.1, -0.2, 0.8], [0.4, 0.6, -0.3, 0.8], [2.1, -0.1, 0, 0.6]].forEach(([x, y, z, r]) => {
        const blob = new THREE.Mesh(new THREE.SphereGeometry(r, 20, 14), paint('--lit', '#ffffff'));
        blob.position.set(x, y, z);
        blob.castShadow = true;
        cloud.add(blob);
      });
      cloud.position.set(3.2, 4.4, -1);
      scene.add(ground, box, house, cloud, new THREE.HemisphereLight(0xffffff, 0x8899aa, dark ? 3 : 1.1));
      kit.spot(scene, [9, 14, 7], [2.5, 0.5, 0], { intensity: 3.2, angle: 0.5, penumbra: 0.7, distance: 60, shadow: 1024 });
      return {
        scene,
        camera,
        update(s) {
          const done = s.p.reduce((a, b) => a + b, 0);
          box.rotation.y = 0.5 + 0.7 * done;
          cloud.position.x = 3.2 - 0.5 * done;
          // The set keeps the right of a wide frame and the top of an upright one: the words have the rest.
          const upright = s.aspect < 1, d = kit.fit(camera, s.aspect, upright ? 8 : 15, upright ? 14 : 8);
          const aim = upright ? [look[0], look[1] - 3, look[2]] : [look[0] - 3.2 * side, look[1] - 0.6, look[2]];
          const dir = new THREE.Vector3(from[0] - look[0], from[1] - look[1], from[2] - look[2]).normalize();
          camera.position.set(aim[0] + dir.x * d, aim[1] + dir.y * d + 0.3 * done, aim[2] + dir.z * d);
          camera.lookAt(aim[0], aim[1], aim[2]);
        },
      };
    },
  };
}
"""

# Where each scene's camera stands, and which side of the frame its set keeps (the words have the other). The number
# scene's picture is `dark`: all of it in the theme's ink.
VIEWS = {"cloud": "{ from: [8, 1.8, 12] }", "looks": "{ from: [4, 1.2, 12] }", "drops": "{ from: [7, 4, 11] }", "weight": "{ dark: true }",
         "drop": "{ side: -1, from: [0, 2.2, 12] }", "floats": "{ from: [5, 0.9, 12] }", "up": "{ side: 0, from: [3, 2.2, 12], look: [3, 3.2, 0] }"}

STORY_JS = """// The test deck: one small set, seen from another place in every scene.
import { sky, token } from './scenes/sky.js';

export const film = {
  title: 'How heavy is a cloud?',
  form: '%(form)s',
  treatment: 'grade',
  paper: token('--paper', '#eef1f5'),
  ink: token('--ink', '#11141a'),
  scenes: [
%(scenes)s
  ],
};
"""


def deck_html(theme: str | None = None) -> str:
    """The deck's page; ``theme`` is <html data-story-theme> (None: the page names no theme)."""
    return DECK_HTML % {"theme": f' data-story-theme="{theme}"' if theme else ""}


def story_js(form: str = "deck") -> str:
    return STORY_JS % {"form": form, "scenes": "\n".join(f"    sky('{i}', {n}, {VIEWS.get(i, '{}')})," for i, _, n in SCENES)}


def write_deck(project: Path, *, theme: str | None = None, form: str = "deck") -> None:
    """Turns a copy of the starter into the test deck: its page, its story.js and its one scene file."""
    (project / "index.html").write_text(deck_html(theme))
    (project / "src" / "story.js").write_text(story_js(form))
    (project / "src" / "scenes" / "sky.js").write_text(SKY_JS)


def on_slide(scene: str, beat: int) -> list[str]:
    """The lines a slide shows at a beat: the heading, then the lines of every beat from the last swap at or before
    it up to it (a caption: the current beat's lines only)."""
    heading, beats = WORDS[scene]
    first = beat if LAYOUT[scene] == "caption" else max([0] + [k for k in SWAPS.get(scene, []) if k <= beat])
    return [heading] + [line for k in range(first, beat + 1) for line in beats[k]]


def test_the_deck_this_file_writes_is_the_starter_with_its_story_replaced() -> None:
    """The files the deck is made of are the starter's own, and the page above says what WORDS says."""
    for name in ("src/style.css", "src/theme.css", "src/caption.js", "src/main.js", "src/film.js", "vite.config.js"):
        assert (STARTER / name).is_file(), name
    theme = (STARTER / "src" / "theme.css").read_text()
    assert all(f'[data-story-theme="{t}"]' in theme for t in THEMES[1:]) and '[data-story-theme="paper"]' not in theme  # paper is :root
    for token in TOKENS:
        assert theme.count(f"\n  {token}: ") == len(THEMES), token
    assert "@import './theme.css';" in (STARTER / "src" / "style.css").read_text()
    assert "base: './'" in (STARTER / "vite.config.js").read_text()
    html = deck_html()
    assert [(m.group(1), m.group(2) or "caption") for m in re.finditer(r'<section data-scene="([a-z0-9-]+)"(?: data-layout="(\w+)")?', html)] \
        == [(i, layout) for i, layout, _ in SCENES]
    assert html.count("<div data-beat") == BEATS == 19
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    at = 0
    for scene, _, beats in SCENES:
        heading, lines = WORDS[scene]
        assert len(lines) == beats
        for words in [heading] + [line for beat in lines for line in beat]:
            assert words in text[at:], words  # every heading and line, in the page's order
            at = text.index(words, at)


@pytest.fixture(scope="module")
def browser():
    """One Chromium for the module, with the software WebGL the plugin's browser checks use."""
    from antigravity3d.adapters.engines.web_three import WebThreeValidator, _launch_args

    v = WebThreeValidator()
    if why := v._unavailable():
        pytest.skip(why)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=v.binary(), args=list(_launch_args()))
        yield b
        b.close()


def _vite_build(project: Path) -> Path:
    r = subprocess.run(["node", str(project / "node_modules" / "vite" / "bin" / "vite.js"), "build", "--logLevel", "error"],
                       cwd=project, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return project / "dist"


def _project(root: Path, node_modules: Path | None = None) -> Path:
    if not (NODE_MODULES / "vite" / "bin" / "vite.js").is_file() or not (NODE_MODULES / "three" / "package.json").is_file():
        pytest.skip(f"{NODE_MODULES} has no Vite and three.js to build the starter with")
    project = root / "project"
    shutil.copytree(STARTER, project)
    (project / "node_modules").symlink_to(node_modules or NODE_MODULES, target_is_directory=True)
    return project


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    """The test deck, built with the workspace's Vite. Its page names no theme: a deck is then `paper`."""
    project = _project(tmp_path_factory.mktemp("deck"))
    write_deck(project)
    return _vite_build(project)


@pytest.fixture(scope="module")
def film_dist(tmp_path_factory) -> Path:
    """The same story with story.js saying it is a film: the caption under the picture, as the starter has always been."""
    project = _project(tmp_path_factory.mktemp("deck-film"))
    write_deck(project, form="film")
    return _vite_build(project)


def _serve(dist: Path):
    def handle(route):
        path = route.request.url[len(ORIGIN):].split("?")[0].split("#")[0]
        if not path.startswith(SUB) or ".." in path:
            return route.fulfill(status=404, body="not served")
        f = dist / (path[len(SUB):] or "index.html")
        if not f.is_file():
            return route.fulfill(status=404, body="not found")
        kind = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".woff2": "font/woff2",
                ".woff": "font/woff"}.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        route.fulfill(status=200, body=f.read_bytes(), headers={"content-type": kind})

    return handle


# The slide as it is on screen: the frame, the picture's box, the block of words, and every line that is rendered,
# with where its text lies (the text's own boxes, not its element's), how many rows it wraps to and the most words in
# a row, its type and its colours.
SLIDE = r"""(() => {
  const sec = document.querySelector('#story section.is-current');
  const words = sec.querySelector('.words') || sec;
  const box = (e) => { const b = e.getBoundingClientRect(); return { x: b.left, y: b.top, w: b.width, h: b.height, r: b.right, b: b.bottom }; };
  const rendered = (e) => { const cs = getComputedStyle(e); return e.getClientRects().length > 0 && cs.visibility === 'visible'; };
  const opacity = (e) => { let o = 1; for (let n = e; n && n !== document.documentElement; n = n.parentElement) o *= +getComputedStyle(n).opacity; return o; };
  const lines = [...sec.querySelectorAll('h1, h2, h3, h4, h5, h6, p, li, figcaption')].filter((e) => !e.closest('aside') && rendered(e)).map((e) => {
    const range = document.createRange();
    range.selectNodeContents(e);
    const rects = [...range.getClientRects()].filter((b) => b.width > 0.5 && b.height > 0.5);
    const rows = {};
    const walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      for (const m of t.data.matchAll(/\S+/g)) {
        const r = document.createRange();
        r.setStart(t, m.index);
        r.setEnd(t, m.index + m[0].length);
        const top = Math.round(r.getBoundingClientRect().top);
        rows[top] = (rows[top] || 0) + 1;
      }
    }
    const cs = getComputedStyle(e);
    return { tag: e.tagName.toLowerCase(), cls: e.className, text: e.textContent.replace(/\s+/g, ' ').trim(), opacity: opacity(e),
             x: Math.min(...rects.map((b) => b.left)), r: Math.max(...rects.map((b) => b.right)),
             y: Math.min(...rects.map((b) => b.top)), b: Math.max(...rects.map((b) => b.bottom)),
             rows: Object.keys(rows).length, widest: Math.max(...Object.values(rows)), typed: !!e.querySelector('.ch'),
             size: parseFloat(cs.fontSize), weight: +cs.fontWeight, family: cs.fontFamily, colour: cs.color, align: cs.textAlign };
  });
  const shown = [...sec.querySelectorAll('.words > .is-shown')].map((e) => ({ ...box(e), ground: getComputedStyle(e).backgroundColor }));
  const piece = sec.querySelector('.words > .patch');
  const patch = piece && getComputedStyle(piece).visibility === 'visible' ? { ...box(piece), ground: getComputedStyle(piece).backgroundColor, edge: getComputedStyle(piece).boxShadow } : null;
  return { frame: { w: innerWidth, h: innerHeight }, picture: box(document.getElementById('picture')), words: box(words), lines, shown, patch,
           layout: sec.dataset.layout || 'caption', over: sec.classList.contains('over'),
           scrolls: words.scrollWidth > words.clientWidth + 1 || words.scrollHeight > words.clientHeight + 1 };
})()"""


class Page:
    def __init__(self, browser, dist: Path, **context):
        self.ctx = browser.new_context(**{"viewport": SMALL, **context})
        self.ctx.route(f"{ORIGIN}/**", _serve(dist))
        self.page = self.ctx.new_page()
        self.messages: list[str] = []
        self.page.on("console", lambda m: m.type in ("error", "warning") and not DRIVER_NOISE.search(m.text)
                     and self.messages.append(f"{m.type}: {m.text}"))
        self.page.on("pageerror", lambda e: self.messages.append(f"pageerror: {e}"))
        self.page.on("requestfailed", lambda r: self.messages.append(f"failed: {r.url}"))
        self.requested: list[str] = []
        self.page.on("request", lambda r: self.requested.append(r.url))

    def open(self, query: str = "", hash: str = "", init: str | None = None) -> dict:
        if init:
            self.ctx.add_init_script(init)
        self.page.goto(ORIGIN + SUB + query + hash)
        self.page.evaluate("window.__story.ready")
        return self.page.evaluate("({mode: __story.mode, why: __story.why || null, scenes: __story.scenes, form: __story.form, "
                                  "info: __story.info()})")

    def js(self, expression: str):
        return self.page.evaluate(expression)

    def go(self, scene: str, beat: int) -> dict:
        """The beat shown settled (the hook's go): its words whole, nothing moving. Returns info()."""
        return self.page.evaluate(f"__story.go('{scene}', {beat})")

    def slide(self) -> dict:
        return self.page.evaluate(SLIDE)

    def texts(self) -> list[str]:
        """The lines a viewer can read on the slide now, in the page's order."""
        return [line["text"] for line in self.slide()["lines"] if line["opacity"] > 0.99]

    def theme(self) -> str:
        return self.page.evaluate("__story.info().theme")

    def settled(self) -> None:
        """Waits until nothing on the slide is on its way in or out. A beat's words begin their entrance with the
        engine's next frame, not with the move that brought the beat (caption.js: show() leaves it pending), so
        the wait is for the current beat to be shown and then for its lines to have come to rest: lines with no
        animation running, looked at before the entrance began, are not a slide at rest."""
        self.page.wait_for_function(
            "() => !__story.info().cut && !document.querySelector('#story section.is-leaving') &&"
            " document.querySelector('#story section.is-current [data-beat].is-current')?.classList.contains('is-shown') &&"
            " [...document.querySelectorAll('#story section.is-current .line')].every((e) => e.getAnimations().length === 0)")

    def close(self) -> None:
        self.ctx.close()


def _rgb(colour: str) -> tuple[int, int, int]:
    """A CSS colour as the browser computes it ("rgb(17, 20, 26)") or as theme.css writes it ("#11141a")."""
    if colour.startswith("#"):
        return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(int(float(v)) for v in re.findall(r"[\d.]+", colour)[:3])


def _luminance(rgb) -> np.ndarray:
    c = np.asarray(rgb, dtype=float) / 255.0
    return np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4) @ np.array([0.2126, 0.7152, 0.0722])


def _ratio(a, b) -> float:
    la, lb = float(_luminance(a)), float(_luminance(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _contrast(p: Page) -> list[tuple[str, float, float]]:
    """For each line shown: (its words, the WCAG ratio of its colour against what the page shows under it, the ratio
    it needs). What is under it is photographed with the words made transparent, so it is the picture and whatever
    ground the page lays over it; as check_site does, the middle 80 % of those pixels by luminance. Large type (24 px
    and up, or 19 px and bold) needs 3, the rest 4.5."""
    from PIL import Image

    lines = [line for line in p.slide()["lines"] if line["opacity"] > 0.99]
    p.js("(() => { const s = document.createElement('style'); s.id = 'no-words';"
         " s.textContent = '#story .words * { color: transparent !important; } #story .words *::before { visibility: hidden !important; }';"
         " document.head.append(s); })()")
    img = np.asarray(Image.open(io.BytesIO(p.page.screenshot())).convert("RGB")).astype(float)
    p.js("document.getElementById('no-words').remove()")
    out = []
    for line in lines:
        under = np.sort(_luminance(img[int(line["y"]):int(np.ceil(line["b"])), int(line["x"]):int(np.ceil(line["r"]))].reshape(-1, 3)))
        cut = len(under) // 10
        ground, ink = float(under[cut:len(under) - cut].mean()), float(_luminance(_rgb(line["colour"])))
        large = line["size"] >= 24 or (line["size"] >= 19 and line["weight"] >= 700)
        out.append((line["text"], (max(ink, ground) + 0.05) / (min(ink, ground) + 0.05), 3.0 if large else 4.5))
    return out


@pytest.mark.browser
def test_the_deck_boots_clean_and_the_hook_says_the_layout_the_form_and_the_theme(browser, dist) -> None:
    p = Page(browser, dist, viewport=DESKTOP)
    try:
        state = p.open()
        assert state["mode"] == "webgl" and state["form"] == "deck", state
        assert [(s["id"], s["layout"], s["beats"]) for s in state["scenes"]] == SCENES
        assert [s["title"] for s in state["scenes"]] == [WORDS[i][0] for i, _, _ in SCENES]
        info = state["info"]
        assert (info["scene"], info["beat"], info["layout"], info["form"], info["theme"]) == ("cloud", 0, "title", "deck", "paper"), info
        # a deck whose page names no theme is paper, and <html> says so
        assert p.js("[document.documentElement.dataset.storyTheme, document.documentElement.dataset.form]") == ["paper", "deck"]
        for scene, layout, beats in SCENES:
            for k in range(beats):
                info = p.go(scene, k)
                assert (info["scene"], info["beat"], info["layout"], info["form"]) == (scene, k, layout, "deck"), info
                # info().lines is the current beat's lines (p, li, figcaption), notes left out; the note is its own field
                assert info["lines"] == WORDS[scene][1][k] and info["notes"] == NOTES.get((scene, k), ""), info
                assert p.js("__story.info().layout") == layout
        # the frame is the picture: the canvas fills the window, and nothing was asked of another host
        assert p.js("(() => { const b = document.getElementById('stage').getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; })()") \
            == [0, 0, DESKTOP["width"], DESKTOP["height"]]
        assert not [u for u in p.requested if not u.startswith(ORIGIN)], p.requested
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE, WALL], ids=["desktop", "phone", "wall"])
def test_every_layout_puts_its_words_where_the_contract_says_inside_the_frame_and_uncut(browser, dist, viewport) -> None:
    p = Page(browser, dist, viewport=viewport)
    try:
        p.open()
        w, h = viewport["width"], viewport["height"]
        for scene, layout, beats in SCENES:
            p.go(scene, beats - 1)
            s = p.slide()
            lines = [line for line in s["lines"] if line["opacity"] > 0.99]
            assert [line["text"] for line in lines] == on_slide(scene, beats - 1), (scene, [line["text"] for line in lines])
            assert s["layout"] == layout and s["over"], (scene, s["layout"])
            # inside the frame with a margin, inside the block, nothing scrolled or cut (a line set tighter than its
            # face is tall has its letters' box reach a little past its own: a sixth of its size is allowed for that)
            for line in lines:
                assert line["x"] >= 14 and line["r"] <= w - 14 and line["y"] >= 14 and line["b"] <= h - 10, (scene, line)
                assert s["words"]["x"] - 1 <= line["x"] and line["r"] <= s["words"]["r"] + 1, (scene, line)
                assert s["words"]["y"] - line["size"] / 6 <= line["y"] and line["b"] <= s["words"]["b"] + line["size"] / 6, (scene, line)
            assert not s["scrolls"], scene
            left, right = min(line["x"] for line in lines), max(line["r"] for line in lines)
            top, bottom = min(line["y"] for line in lines), max(line["b"] for line in lines)
            cx, cy = (left + right) / 2, (top + bottom) / 2
            # each line by its class (lede, big, source, kicker) or else its tag; caption.js's own mark, line, aside
            by_class = {([c for c in line["cls"].split() if c != "line"] or [line["tag"]])[0]: line for line in lines}
            # a headline is never more than about twelve words wide (the typed caption is not one)
            assert all(line["widest"] <= 12 for line in lines if not line["typed"]), (scene, [line["widest"] for line in lines])
            if w < h:
                # an upright frame: every layout is a block in the lower 45 % of the frame; the picture keeps the rest
                assert top >= 0.55 * h, (scene, top / h)
                assert left <= 0.1 * w or layout == "end", (scene, left)
                if layout == "end":
                    assert abs(cx - w / 2) <= 0.02 * w and all(line["align"] == "center" for line in lines), (scene, cx)
                continue
            if layout in ("title", "statement", "number", "caption"):
                # the lower left: on the left margin, standing on the bottom margin, its middle in the lower left quarter
                assert left <= 0.07 * w and bottom >= 0.85 * h and cx < 0.5 * w and cy > 0.5 * h, (scene, left, bottom, cx, cy)
            if layout == "title":
                h1, lede = by_class["h1"], by_class["lede"]
                assert h1["size"] >= 2.5 * lede["size"] and h1["b"] <= lede["y"] + 1, (h1, lede)  # the h1 large, the lines under it
            if layout == "statement":
                assert all(not (line["r"] > 0.72 * w and line["y"] < 0.5 * h) for line in lines), scene  # the upper right is the picture's
                words = [line for line in lines if line["tag"] == "p"]
                assert all(line["size"] >= 2.8 * by_class["kicker"]["size"] for line in words), scene  # large words, a small label
            if layout == "number":
                big, source = by_class["big"], by_class["source"]
                others = [line for line in lines if line["tag"] == "p" and line is not big]
                assert all(big["size"] >= 3 * line["size"] for line in others), (big["size"], [line["size"] for line in others])
                assert source["y"] >= big["y"] + 0.5 * big["size"] and source["size"] < 0.2 * big["size"], (big, source)  # a line under the figure
            if layout == "left":
                assert s["words"]["r"] <= 0.42 * w + 1 and left <= 0.07 * w, (scene, s["words"])  # at most 42 % of the width
                assert abs((s["words"]["y"] + s["words"]["b"]) / 2 - h / 2) <= 0.08 * h, (scene, s["words"])
            if layout == "right":
                assert s["words"]["x"] >= 0.58 * w - 1 and right <= w - 14, (scene, s["words"])
                assert abs((s["words"]["y"] + s["words"]["b"]) / 2 - h / 2) <= 0.08 * h, (scene, s["words"])
            if layout == "caption":
                assert top >= 0.66 * h, (scene, top / h)  # a lower third
            if layout == "end":
                assert abs(cx - w / 2) <= 0.01 * w and abs(cy - h / 2) <= 0.06 * h, (scene, cx, cy)  # centred
                assert all(line["align"] == "center" for line in lines)
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_beats_accumulate_without_moving_the_words_before_them_and_a_swap_replaces_them(browser, dist) -> None:
    p = Page(browser, dist)

    def places() -> dict:
        return {line["text"]: (round(line["x"], 1), round(line["y"], 1), round(line["r"], 1)) for line in p.slide()["lines"] if line["opacity"] > 0.99}

    try:
        p.open(query="?pace=6")
        # every beat up to the current one is shown, in order, and the words already there never move
        seen, widths = {}, []
        for k in range(3):
            info = p.go("drops", k)
            now = places()
            assert list(now) == on_slide("drops", k) and info["lines"] == WORDS["drops"][1][k], (k, list(now))
            assert all(now[text] == seen[text] for text in seen), (k, now, seen)
            seen = now
            widths.append(p.slide()["words"]["w"])
        assert widths == sorted(widths)  # the block only ever grows: a beat still to come takes room but no width
        # the same when a viewer steps through it: forwards the beat enters, backwards it leaves, the rest stands
        p.go("drops", 0)
        for k in (1, 2):
            p.js("__story.advance()")
            p.page.wait_for_function(f"() => __story.info().beat === {k}")
            p.settled()
            assert places() == {text: seen[text] for text in on_slide("drops", k)}, k
        p.js("__story.back()")
        p.settled()
        assert places() == {text: seen[text] for text in on_slide("drops", 1)}
        # a beat marked swap replaces what the scene showed before it; the heading stays
        first = {}
        for k in range(2):
            p.go("floats", k)
            assert list(places()) == on_slide("floats", k)
            first = places()
        p.go("floats", 2)
        assert list(places()) == ["Why it stays up", "The air under the cloud rises a hundred times faster."] == on_slide("floats", 2)
        assert p.js("[...document.querySelectorAll('[data-scene=floats] [data-beat]')].map((b) => getComputedStyle(b).display)") == ["none", "none", "block"]
        p.js("__story.back()")  # back over the swap: what it replaced is there again, where it was
        p.settled()
        assert places() == first
        # in a typed caption only the current beat's lines show
        for k in range(2):
            p.go("aside", k)
            assert list(places()) == on_slide("aside", k) == ["A caption", WORDS["aside"][1][k][0]]
        assert p.messages == [], p.messages
    finally:
        p.close()


# Records, at the moment a beat's entrance is set off (caption.js sets .is-entering), the transitions the browser has
# started on each of its lines: read in the same task, so the reading does not depend on how fast frames are drawn.
WATCH = r"""(() => {
  window.__entered = [];
  const text = (e) => e.textContent.replace(/\s+/g, ' ').trim();
  new MutationObserver(() => {
    const lines = [...document.querySelectorAll('#story section.is-current .words > .is-entering .line')];
    if (!lines.length) return;
    window.__entered.push({
      lines: lines.map((e) => ({
        text: text(e), opacity: +getComputedStyle(e).opacity, rise: new DOMMatrix(getComputedStyle(e).transform).f / parseFloat(getComputedStyle(e).fontSize),
        moves: Object.fromEntries(e.getAnimations().map((a) => [a.transitionProperty, [Math.round(a.effect.getTiming().delay), Math.round(a.effect.getTiming().duration)]])),
      })),
      moving: [...document.querySelectorAll('#story .line')].filter((e) => e.getAnimations().length).map(text),
    });
  }).observe(document.getElementById('story'), { subtree: true, attributes: true, attributeFilter: ['class'] });
})()"""
MOVING = "[...document.querySelectorAll('#story section.is-current .line')].filter((e) => e.getAnimations().length).map((e) => e.textContent.trim())"


@pytest.mark.browser
def test_a_beats_lines_come_in_timed_one_after_another_and_the_lines_before_them_stand_still(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        p.open()
        p.go("weight", 1)
        before = {line["text"]: (line["x"], line["y"]) for line in p.slide()["lines"] if line["opacity"] > 0.99}
        assert list(before) == on_slide("weight", 1) and p.js(MOVING) == []  # a beat shown settled is whole at once
        p.js(WATCH)
        p.js("__story.advance()")  # the third beat: the figure and its source
        p.page.wait_for_function("() => window.__entered.length > 0")
        entered = p.js("window.__entered[0]")
        assert [line["text"] for line in entered["lines"]] == ["500 tonnes", "1 km3 x 0.5 g per m3"], entered
        # each line rises and fades in over the theme's --dur (paper: half a second), 80 ms after the line before it
        assert [line["moves"] for line in entered["lines"]] == [{"opacity": [0, 500], "transform": [0, 500]},
                                                               {"opacity": [80, 500], "transform": [80, 500]}], entered
        # from nothing, 0.4 em below where it will stand; and nothing else on the slide moves
        assert all(line["opacity"] == 0 and abs(line["rise"] - 0.4) < 0.01 for line in entered["lines"]), entered
        assert sorted(entered["moving"]) == ["1 km3 x 0.5 g per m3", "500 tonnes"], entered
        p.settled()
        after = {line["text"]: (line["x"], line["y"]) for line in p.slide()["lines"] if line["opacity"] > 0.99}
        assert list(after) == on_slide("weight", 2) and all(after[text] == before[text] for text in before), (before, after)
        assert p.js("getComputedStyle(document.querySelector('[data-scene=weight] .big')).transform") == "none"
        # arriving in a scene, its heading comes in first and the beat's lines after it, once the scene left behind
        # has had a moment to go (0.2 s)
        p.js("window.__entered = []")
        p.js("__story.advance()")
        p.page.wait_for_function("() => window.__entered.length > 0")
        entered = p.js("window.__entered[0]")
        assert [(line["text"], line["moves"]["opacity"]) for line in entered["lines"]] == [
            ("The sum, in three lines", [200, 500]), ("Two things are assumed. The rest follows from them.", [280, 500])], entered
        p.settled()
        assert p.texts() == on_slide("sums", 0)
        assert p.js("[...document.querySelectorAll('#story section')].filter((s) => getComputedStyle(s).display !== 'none').length") == 1
        # the time is the theme's: ink is slower
        p.js("__story.setTheme('ink')")
        p.js("window.__entered = []")
        p.js("__story.advance()")
        p.page.wait_for_function("() => window.__entered.length > 0")
        assert [line["moves"]["opacity"] for line in p.js("window.__entered[0]")["lines"]] == [[0, 700], [80, 700], [160, 700]]  # three list items
        # a settled view of a beat whose lines are still on their way shows them whole at once
        p.go("sums", 1)
        assert p.js(MOVING) == [] and p.texts() == on_slide("sums", 1)
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_under_reduced_motion_the_words_are_whole_at_once(browser, dist) -> None:
    p = Page(browser, dist, reduced_motion="reduce")
    try:
        p.open()
        p.go("weight", 1)
        p.js(WATCH)
        p.js("__story.advance()")
        p.page.wait_for_function("() => __story.info().beat === 2")
        p.js("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
        assert p.js("window.__entered") == [] and p.js(MOVING) == []  # no entrance was set off, and nothing is in motion
        assert p.texts() == on_slide("weight", 2)
        p.js("__story.advance()")  # to the next scene: the words left behind are gone at once too
        p.page.wait_for_function("() => __story.info().scene === 'sums'")
        p.js("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
        assert p.js("window.__entered") == [] and p.js(MOVING) == [] and p.texts() == on_slide("sums", 0)
        assert p.js("[...document.querySelectorAll('#story section')].filter((s) => getComputedStyle(s).display !== 'none').length") == 1
        assert p.messages == [], p.messages
    finally:
        p.close()


# The article as a reader has it: every heading and every line that is rendered, with where it is; the notes.
ARTICLE = r"""(() => {
  const seen = (e) => { const cs = getComputedStyle(e); return e.getClientRects().length > 0 && cs.visibility === 'visible' && +cs.opacity === 1; };
  const text = (e) => e.textContent.replace(/\s+/g, ' ').trim();
  const lines = [...document.querySelectorAll('#story :is(h1, h2, h3, p, li, figcaption)')].filter((e) => !e.closest('aside'));
  return { lines: lines.filter(seen).map((e) => ({ text: text(e), top: e.getBoundingClientRect().top + scrollY, heading: /^H\d$/.test(e.tagName) })), all: lines.length,
           notes: [...document.querySelectorAll('aside[data-notes]')].filter(seen).map((e) => ({ text: text(e), size: parseFloat(getComputedStyle(e).fontSize) })),
           body: parseFloat(getComputedStyle(document.body).fontSize), scrolls: document.scrollingElement.scrollHeight > innerHeight + 200,
           hidden: ['picture', 'stage', 'progress', 'overview'].map((id) => { const e = document.getElementById(id); return !e || e.getClientRects().length === 0; }),
           controls: document.querySelector('.controls').getClientRects().length, wrapped: document.querySelectorAll('#story .words, #story .ch').length,
           paper: getComputedStyle(document.body).backgroundColor, ink: getComputedStyle(document.body).color };
})()"""


def _is_the_article(a: dict) -> None:
    """Every scene's heading and every beat's content, in order, top to bottom; notes as small text; nothing hidden."""
    expected = [words for scene, _, _ in SCENES for words in [WORDS[scene][0]] + [line for beat in WORDS[scene][1] for line in beat]]
    assert [line["text"] for line in a["lines"]] == expected and a["all"] == len(expected), [line["text"] for line in a["lines"]]
    assert [line["text"] for line in a["lines"] if line["heading"]] == [WORDS[scene][0] for scene, _, _ in SCENES]
    tops = [line["top"] for line in a["lines"]]
    assert tops == sorted(tops) and len(set(tops)) == len(tops), tops  # it reads from top to bottom, one line after another
    assert [note["text"] for note in a["notes"]] == list(NOTES.values()) and all(note["size"] < a["body"] for note in a["notes"]), a["notes"]
    assert a["scrolls"] and all(a["hidden"]) and a["controls"] == 0 and a["wrapped"] == 0, a
    assert (_rgb(a["paper"]), _rgb(a["ink"])) == ((238, 241, 245), (17, 20, 26))  # in the theme's colours: paper


@pytest.mark.browser
def test_without_script_the_page_is_the_article(browser, dist) -> None:
    ctx = browser.new_context(java_script_enabled=False, viewport=SMALL)
    ctx.route(f"{ORIGIN}/**", _serve(dist))
    page = ctx.new_page()
    requested: list[str] = []
    page.on("request", lambda r: requested.append(r.url))
    try:
        page.goto(ORIGIN + SUB)
        # script is off for the page; the driver still reads it, with the same expression as below
        _is_the_article(page.evaluate(ARTICLE))
        assert not [u for u in requested if not u.startswith(ORIGIN)], requested
    finally:
        ctx.close()


@pytest.mark.browser
def test_without_webgl_the_page_is_the_article_with_a_note(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        state = p.open(init="HTMLCanvasElement.prototype.getContext = function () { return null; };")
        assert state["mode"] == "no-webgl" and "WebGL" in state["why"] and state["info"] == {}, state
        assert p.page.locator("#note").is_visible()
        _is_the_article(p.js(ARTICLE))
        err = p.js("__story.go('looks', 0).then(() => 'resolved', (e) => e.message)")
        assert "no picture" in err, err
        # the engine (three.js with it) is not even fetched: it is imported only once WebGL is known to work
        assert len([u for u in p.requested if u.endswith(".js")]) == 1, p.requested
        assert p.messages == [], p.messages
    finally:
        p.close()


# How the page looks in a theme: the tokens as CSS computes them on <html>, the page's colours, and how the title
# slide's headline, its lede and a label are set.
LOOK = r"""(tokens) => {
  const cs = getComputedStyle(document.documentElement);
  const face = (sel) => { const s = getComputedStyle(document.querySelector(sel));
    return { family: s.fontFamily.split(',')[0].replace(/"/g, '').trim(), weight: s.fontWeight, style: s.fontStyle, upper: s.textTransform, colour: s.color }; };
  return { tokens: Object.fromEntries(tokens.map((n) => [n, cs.getPropertyValue(n).trim()])),
           paper: getComputedStyle(document.body).backgroundColor, ink: getComputedStyle(document.body).color,
           head: face('[data-scene=cloud] h1'), text: face('[data-scene=cloud] .lede'), label: face('.count'),
           line: getComputedStyle(document.querySelector('#progress span')).backgroundColor,
           attribute: document.documentElement.dataset.storyTheme, info: __story.info().theme };
}"""


@pytest.mark.browser
def test_each_of_the_five_themes_gives_the_page_its_own_colours_and_faces(browser, dist) -> None:
    looks = {}
    for theme in THEMES:
        p = Page(browser, dist)
        try:
            p.open(query=f"?theme={theme}")  # ?theme=name picks a theme for a look
            p.go("cloud", 0)
            looks[theme] = look = p.page.evaluate(LOOK, TOKENS)
            assert look["attribute"] == look["info"] == theme, look
            tokens = look["tokens"]
            assert all(tokens[name] for name in TOKENS), tokens  # every token the contract names, in every theme
            colours = {name: tokens[name] for name in TOKENS[:8]}
            assert all(re.fullmatch(r"#[0-9a-f]{6}", value) for value in colours.values()), colours  # a notation three.js reads
            assert tokens["--ease"].startswith("cubic-bezier(") and re.fullmatch(r"0?\.\d+s", tokens["--dur"]), tokens
            # the page is painted and set from them
            assert (_rgb(look["paper"]), _rgb(look["ink"]), _rgb(look["head"]["colour"])) == (_rgb(tokens["--paper"]), _rgb(tokens["--ink"]), _rgb(tokens["--ink"]))
            assert _rgb(look["line"]) == _rgb(tokens["--accent"])
            assert look["head"]["family"] == tokens["--font-display"].split(",")[0].strip('" ') and look["head"]["weight"] == tokens["--weight-display"]
            assert look["text"]["family"] == tokens["--font-text"].split(",")[0].strip('" ') and look["label"]["family"] == tokens["--font-mono"].split(",")[0].strip('" ')
            # text can be read on the theme's paper: ink at 7 to 1 or better, quiet text at 4.5
            assert _ratio(_rgb(tokens["--ink"]), _rgb(tokens["--paper"])) >= 7 and _ratio(_rgb(tokens["--muted"]), _rgb(tokens["--paper"])) >= 4.5, tokens
            # the scrim: none where the picture leaves the words room, the theme's paper where the picture can be as
            # dark as the type (poster's flat fields of the type's colour, halftone's dots, glyph's characters)
            assert tokens["--scrim"] == (tokens["--paper"] if theme in SCRIMMED else "transparent"), tokens["--scrim"]
            assert p.messages == [], (theme, p.messages)
        finally:
            p.close()
    # clearly different from one another: no two share their paper, their ink or their accent, or the way a headline is set
    for a in THEMES:
        for b in THEMES[THEMES.index(a) + 1:]:
            for name in ("--paper", "--ink", "--accent"):
                assert looks[a]["tokens"][name] != looks[b]["tokens"][name], (a, b, name)
            assert _ratio(_rgb(looks[a]["tokens"]["--paper"]), _rgb(looks[b]["tokens"]["--paper"])) > 1.15 or \
                _hue_apart(looks[a]["tokens"]["--paper"], looks[b]["tokens"]["--paper"]), (a, b)
            assert looks[a]["head"] != looks[b]["head"], (a, b, looks[a]["head"])
    # light and dark, a serif and capitals among them; glyph is the mono face throughout
    assert [looks[t]["tokens"]["--paper"] > "#8" for t in THEMES] == [True, False, True, True, False]
    assert looks["ink"]["head"]["style"] == "italic" and looks["poster"]["head"]["upper"] == "uppercase" and looks["poster"]["head"]["weight"] == "900"
    assert looks["glyph"]["head"]["family"] == looks["glyph"]["text"]["family"] == looks["glyph"]["label"]["family"] == "JetBrains Mono"


def _hue_apart(a: str, b: str) -> bool:
    """Two colours of like lightness that are still plainly not the same: their hues more than 40 degrees apart."""
    import colorsys

    ha, hb = (colorsys.rgb_to_hsv(*(v / 255 for v in _rgb(c)))[0] * 360 for c in (a, b))
    return min(abs(ha - hb), 360 - abs(ha - hb)) > 40


@pytest.mark.browser
def test_the_key_t_goes_through_the_themes(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        p.open()
        assert p.js("__story.themes") == THEMES and p.theme() == "paper"
        papers = [p.js("getComputedStyle(document.body).backgroundColor")]
        for theme in THEMES[1:] + THEMES[:1]:
            p.page.keyboard.press("t")
            assert p.js("document.documentElement.dataset.storyTheme") == theme and p.theme() == theme
            papers.append(p.js("getComputedStyle(document.body).backgroundColor"))
        assert len(set(papers[:5])) == 5 and papers[5] == papers[0], papers  # five papers, and round to the first
        p.js("__story.setTheme('halftone')")
        assert p.theme() == "halftone"
        p.js("__story.setTheme('no-such-theme')")  # a name that is no theme changes nothing
        assert p.theme() == "halftone" and p.js("document.documentElement.dataset.storyTheme") == "halftone"
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_words_over_a_picture_as_dark_as_they_are_stand_on_the_scrim(browser, dist) -> None:
    """The number scene's picture is all in the theme's ink (sky.js, dark), the worst there is under words in that ink."""
    for theme in THEMES:
        p = Page(browser, dist)
        try:
            p.open(query=f"?theme={theme}")
            paper = _rgb(p.js("getComputedStyle(document.documentElement).getPropertyValue('--paper').trim()"))
            widths = []
            for k in range(3):
                p.go("weight", k)
                s = p.slide()
                lines = [line for line in s["lines"] if line["opacity"] > 0.99]
                widths.append(s["shown"][0]["w"])
                # the ground is the blocks shown (the heading's, then each beat's), one above the other with no gap, of one width
                assert len(s["shown"]) == k + 2 and all(b["x"] == s["shown"][0]["x"] and b["w"] == s["shown"][0]["w"] for b in s["shown"]), s["shown"]
                assert all(abs(lower["y"] - upper["b"]) < 0.6 for upper, lower in zip(s["shown"], s["shown"][1:])), s["shown"]
                patch = {"x": s["shown"][0]["x"], "y": s["shown"][0]["y"], "r": s["shown"][0]["r"], "b": s["shown"][-1]["b"]}
                # and one piece behind them, exactly over them, carries the edge
                assert all(abs(s["patch"][side] - patch[side]) < 0.6 for side in "xyrb") and s["patch"]["ground"] in {b["ground"] for b in s["shown"]}, s["patch"]
                # it lies under every line, and is as small as the words: their width and a little room on each side
                assert all(patch["x"] < line["x"] and line["r"] < patch["r"] and patch["y"] < line["y"] and line["b"] < patch["b"] for line in lines)
                room = min(line["x"] for line in lines) - patch["x"]
                assert 6 <= room <= 26 and patch["r"] - max(line["r"] for line in lines) <= room + 8, (theme, k, room, patch)
                grounds = {b["ground"] for b in s["shown"]}
                if theme in SCRIMMED:
                    assert {_rgb(g) for g in grounds} == {paper} and "rgba" not in "".join(grounds), (theme, grounds)
                    if theme == "poster":  # a hard edge, as everything on a poster has: no blur and no spread
                        assert s["patch"]["edge"] == "none" or re.search(r" 0px 0px$", s["patch"]["edge"]), s["patch"]
                    else:  # a soft edge
                        assert s["patch"]["edge"] != "none" and re.search(r"[1-9][\d.]*px [\d.]+px$", s["patch"]["edge"]), s["patch"]
                    assert all(got >= need for _, got, need in _contrast(p)), (theme, k, _contrast(p))
                else:
                    assert grounds == {"rgba(0, 0, 0, 0)"}, (theme, grounds)
            assert widths[0] < widths[1] < widths[2], (theme, widths)  # the patch grows with the words, beat by beat
            if theme not in SCRIMMED:
                # no scrim by default: here the words are lost in the picture ...
                assert all(got < need for _, got, need in _contrast(p)), (theme, _contrast(p))
                # ... and a scene that asks for one, class="scrim" on its section, gets the theme's paper under its words
                p.js("document.querySelector('[data-scene=weight]').classList.add('scrim')")
                assert all(got >= need for _, got, need in _contrast(p)), (theme, _contrast(p))
                assert {_rgb(b["ground"]) for b in p.slide()["shown"]} == {paper}
                p.go("looks", 1)  # the other scenes are as they were
                assert {b["ground"] for b in p.slide()["shown"]} == {"rgba(0, 0, 0, 0)"}
            assert p.messages == [], (theme, p.messages)
        finally:
            p.close()


@pytest.mark.browser
def test_the_overview_lists_the_scenes_and_goes_to_one(browser, dist) -> None:
    p = Page(browser, dist)
    cards = ("[...document.querySelectorAll('#overview button')].map((b) => [b.dataset.scene, b.querySelector('.n').textContent,"
             " b.querySelector('.h').textContent, b.querySelectorAll('.b i').length, b.getAttribute('aria-current')])")
    where = "[__story.info().scene, __story.info().beat]"
    try:
        p.open(query="?pace=8")
        p.go("drops", 1)
        assert p.page.locator("#overview").is_hidden()
        p.page.keyboard.press("o")
        assert p.page.locator("#overview").is_visible()
        # every scene a card: its number, its heading, a mark for each of its beats; the current one marked
        assert p.js(cards) == [[scene, f"{i + 1:02d}", WORDS[scene][0], beats, "true" if scene == "drops" else None]
                               for i, (scene, _, beats) in enumerate(SCENES)]
        assert p.js("document.activeElement.dataset.scene") == "drops"
        assert p.js("[...document.querySelectorAll('#overview button')].every((b) => { const r = b.getBoundingClientRect();"
                    " return r.left >= 0 && r.right <= innerWidth && r.top >= 0; })")
        # while it is up the arrows move among the cards and do not step the deck; Enter goes to the card's scene
        p.page.keyboard.press("ArrowRight")
        p.page.keyboard.press("ArrowRight")
        assert p.js("document.activeElement.dataset.scene") == "sums" and p.js(where) == ["drops", 1]
        p.page.keyboard.press("ArrowLeft")
        p.page.keyboard.press("Enter")
        p.page.wait_for_function("() => __story.info().scene === 'weight' && !__story.info().cut")
        assert p.js(where) == ["weight", 0] and p.page.locator("#overview").is_hidden()
        assert p.js("document.documentElement.classList.contains('overview-on')") is False
        # a click on a card goes there too, and the card of the scene one is in is the marked one
        p.page.keyboard.press("o")
        assert [c[0] for c in p.js(cards) if c[4]] == ["weight"]
        p.page.locator("#overview button[data-scene=up]").click()
        p.page.wait_for_function("() => __story.info().scene === 'up' && !__story.info().cut")
        assert p.js(where) == ["up", 0] and p.page.locator("#overview").is_hidden()
        # Escape closes it and nothing moves; so does o
        p.page.keyboard.press("o")
        p.page.keyboard.press("Escape")
        assert p.page.locator("#overview").is_hidden() and p.js(where) == ["up", 0]
        p.page.keyboard.press("o")
        p.page.keyboard.press("o")
        assert p.page.locator("#overview").is_hidden()
        p.page.keyboard.press("ArrowLeft")  # and the keys are the deck's again
        p.page.wait_for_function("() => __story.info().scene === 'aside' && !__story.info().cut")
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_progress_line_and_the_count_follow_every_move(browser, dist) -> None:
    p = Page(browser, dist)
    line = ("(() => { const bar = document.getElementById('progress'), b = bar.getBoundingClientRect(), s = bar.firstElementChild.getBoundingClientRect();"
            " return { now: +bar.getAttribute('aria-valuenow'), max: +bar.getAttribute('aria-valuemax'), share: s.width / b.width,"
            " left: b.left, width: b.width, bottom: innerHeight - b.bottom, height: b.height, role: bar.getAttribute('role') }; })()")
    try:
        p.open(query="?pace=8")
        done = 0
        for i, (scene, _, beats) in enumerate(SCENES):
            for k in range(beats):
                p.go(scene, k)
                done += 1
                assert p.page.locator("#count").inner_text() == f"{i + 1} / {len(SCENES)}"  # the count, as "3 / 9"
                p.page.wait_for_function(f"() => Math.abs(document.querySelector('#progress span').getBoundingClientRect().width / innerWidth - {done / BEATS}) < 0.002")
                got = p.js(line)
                assert (got["now"], got["max"], got["role"]) == (done, BEATS, "progressbar"), got  # beats done over all beats
                assert (got["left"], got["width"], got["bottom"]) == (0, SMALL["width"], 0) and got["height"] <= 4, got  # thin, along the bottom edge
        assert done == BEATS and abs(p.js(line)["share"] - 1) < 0.002
        p.page.keyboard.press("Home")
        p.page.wait_for_function("() => __story.info().scene === 'cloud' && !__story.info().cut")
        assert p.page.locator("#count").inner_text() == "1 / 9" and p.js(line)["now"] == 1
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_strip_and_the_controls_are_small_overlays_that_fade_when_the_pointer_rests(browser, dist) -> None:
    p = Page(browser, dist, viewport=DESKTOP)
    shown = "['.strip', '.controls'].map((s) => +getComputedStyle(document.querySelector(s)).opacity)"
    try:
        p.open()
        p.page.mouse.move(400, 300)
        p.page.mouse.move(420, 320)
        p.page.wait_for_function(f"() => {shown}.every((o) => o === 1)")
        boxes = p.js("['.strip .mark', '.strip .count', '.controls'].map((s) => { const b = document.querySelector(s).getBoundingClientRect();"
                     " return [b.left, b.top, b.right, b.bottom]; })")
        mark, count, controls = boxes
        w, h = DESKTOP["width"], DESKTOP["height"]
        # small, in the corners: the title top left, the count top right, the buttons bottom right
        assert mark[0] < 0.1 * w and mark[3] < 0.08 * h and count[2] > 0.9 * w and count[3] < 0.08 * h, boxes
        assert controls[0] > 0.7 * w and controls[1] > 0.85 * h and controls[3] - controls[1] < 48, boxes
        assert p.js("document.documentElement.classList.contains('idle')") is False
        # the pointer rests: they fade
        p.page.wait_for_function(f"() => document.documentElement.classList.contains('idle') && {shown}.every((o) => o === 0)", timeout=10_000)
        # a key steps the deck and does not bring them back; the pointer does, and the buttons work
        p.page.keyboard.press("ArrowRight")
        p.page.wait_for_function("() => __story.info().scene === 'looks' && !__story.info().cut")
        assert p.js("document.documentElement.classList.contains('idle')") is True
        p.page.mouse.move(600, 400)
        p.page.wait_for_function(f"() => {shown}.every((o) => o === 1)")
        p.page.locator("#next").click()
        p.page.wait_for_function("() => __story.info().scene === 'looks' && __story.info().beat === 1")
        p.page.locator("#picture").click(position={"x": 700, "y": 300})  # a click on the picture steps on, through the words laid over it
        p.page.wait_for_function("() => __story.info().scene === 'drops' && !__story.info().cut")
        words = p.page.locator("#story section.is-current p").first.bounding_box()
        p.page.mouse.click(words["x"] + words["width"] / 2, words["y"] + words["height"] / 2)  # and so does one on the words themselves
        p.page.wait_for_function("() => __story.info().scene === 'drops' && __story.info().beat === 1")
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_a_note_shows_over_the_slide_when_asked_and_is_never_one_of_its_lines(browser, dist) -> None:
    p = Page(browser, dist, viewport=DESKTOP)
    note = "#story [data-beat].is-current aside[data-notes]"
    try:
        p.open()
        info = p.go("weight", 2)
        assert info["notes"] == NOTES["weight", 2] and info["notes"] not in info["lines"] and NOTES["weight", 2] not in p.texts()
        assert p.page.locator(note).is_hidden()
        before = p.slide()["lines"]
        p.page.keyboard.press("n")
        assert p.page.locator(note).is_visible()
        box, w, h = p.page.locator(note).bounding_box(), DESKTOP["width"], DESKTOP["height"]
        assert box["x"] > 0.5 * w and box["x"] + box["width"] <= w - 14 and 40 <= box["y"] < 0.2 * h, box  # top right, in the frame
        assert p.slide()["lines"] == before  # the slide's words are where they were
        p.go("looks", 0)  # a beat without a note shows none
        assert p.page.locator("#story aside[data-notes]").first.is_hidden() and p.js("__story.info().notes") == ""
        p.go("cloud", 0)
        assert p.page.locator(note).is_visible()
        p.page.keyboard.press("n")
        assert p.page.locator(note).is_hidden()
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_a_film_keeps_its_caption_under_the_picture_and_lays_any_other_layout_over_it(browser, film_dist) -> None:
    p = Page(browser, film_dist, viewport=DESKTOP)
    try:
        state = p.open()
        info = state["info"]
        # story.js says film: the form; and a film whose page names no theme keeps the film's colours, ink
        assert (state["form"], info["form"], info["theme"], info["layout"]) == ("film", "film", "ink", "title"), state
        assert p.js("[document.documentElement.dataset.form, document.documentElement.dataset.storyTheme]") == ["film", "ink"]
        assert p.page.locator("#count").inner_text().lower() == "scene 1 of 9"
        assert p.page.locator("#progress").is_hidden()  # the progress line is the deck's
        w, h = DESKTOP["width"], DESKTOP["height"]
        # the caption scene: under the picture, typed, in the page's flow
        p.go("aside", 1)
        s = p.slide()
        assert not s["over"] and [line["text"] for line in s["lines"]] == on_slide("aside", 1), s
        assert s["picture"]["b"] < h - 100 and all(line["y"] >= s["picture"]["b"] for line in s["lines"]), s
        # a scene with another layout: its words over the picture, laid out in the picture's box and not the window's
        for scene in ("looks", "weight", "sums", "drop", "up"):
            beats = len(WORDS[scene][1])
            p.go(scene, beats - 1)
            s = p.slide()
            pic, lines = s["picture"], [line for line in s["lines"] if line["opacity"] > 0.99]
            assert s["over"] and [line["text"] for line in lines] == on_slide(scene, beats - 1), (scene, s["lines"])
            assert all(pic["x"] + 14 <= line["x"] and line["r"] <= pic["r"] - 14 and pic["y"] + 14 <= line["y"] and line["b"] <= pic["b"] - 10
                       for line in lines), (scene, pic, lines)
        p.go("looks", 1)
        s = p.slide()
        assert max(line["b"] for line in s["lines"]) >= s["picture"]["y"] + 0.85 * s["picture"]["h"]  # on the picture's bottom margin
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_build_passes_with_the_font_packages_and_without_them(browser, dist, tmp_path) -> None:
    faces = "[...new Set([...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family.replace(/\"/g, '')))].sort()"
    stacks = "['--font-display', '--font-text', '--font-mono'].map((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim())"
    # with them (the workspace's node_modules has all three): the faces are files of the deck, asked for from its own host
    if all((NODE_MODULES / name).is_dir() for name in FONT_PACKAGES):
        files = sorted(f.name for f in (dist / "assets").iterdir() if f.suffix == ".woff2")
        assert any(f.startswith("space-grotesk-latin-wght") for f in files) and any(f.startswith("inter-latin-wght") for f in files) \
            and any(f.startswith("jetbrains-mono-latin-400") for f in files), files
        p = Page(browser, dist)
        try:
            p.open()
            p.go("weight", 2)  # a headline in the grotesk, a line in the sans, labels in the mono
            p.js("document.fonts.ready")
            assert p.js(faces) == ["Inter Variable", "JetBrains Mono", "Space Grotesk Variable"]
            fonts = [u for u in p.requested if u.endswith(".woff2")]
            assert fonts and all(u.startswith(ORIGIN + SUB + "assets/") for u in fonts), fonts
            with_packages = p.js(stacks)
            assert p.messages == [], p.messages
        finally:
            p.close()
    else:
        with_packages = None
    # without them: a workspace's node_modules with everything but the font packages
    bare = tmp_path / "node_modules"
    bare.mkdir()
    for entry in NODE_MODULES.iterdir():
        if entry.name not in FONT_PACKAGES and not entry.name.startswith(".vite"):
            (bare / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    project = _project(tmp_path, bare)
    write_deck(project)
    built = _vite_build(project)
    assert not [f.name for f in (built / "assets").iterdir() if f.suffix in (".woff", ".woff2")]
    assert "@font-face" not in "".join(f.read_text() for f in (built / "assets").glob("*.css"))
    p = Page(browser, built)
    try:
        state = p.open()
        assert state["mode"] == "webgl" and state["info"]["theme"] == "paper"
        p.go("weight", 2)
        assert p.texts() == on_slide("weight", 2) and p.js("document.fonts.size") == 0
        assert not [u for u in p.requested if re.search(r"\.woff2?$", u)], p.requested
        # the stacks are the same, so the page is set in the next face each names: a real fallback, not the browser's default
        assert with_packages in (None, p.js(stacks))
        assert all(len(stack.split(",")) >= 4 and stack.split(",")[-1].strip() in ("sans-serif", "serif", "monospace") for stack in p.js(stacks))
        assert p.messages == [], p.messages
    finally:
        p.close()
