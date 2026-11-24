"""The scroll-3d-website skill's starter runs: it is the plumbing the skill tells the agent to start from, so it must
work as shipped, with a prop installed by the plugin's own installer, in a headless browser.

How the page finds three.js: the starter imports the bare names ``three`` and ``three/addons/...`` (Vite resolves
them in the agent's project). Here no bundler runs: the copy's ``index.html`` gets an import map onto
``tests/fixtures/web_project/node_modules/three`` (the version the fixture pins; ``npm ci`` there first), the same way
the installer's own browser check imports a project's three.js (``deliver/web_check.py``). To run the same checks on
another three.js, point ``AGY3D_STARTER_NODE_MODULES`` at a ``node_modules`` that has it. The site is served under a
sub-path, and ``public/`` at the site's root as Vite serves it, from a routed fake origin: no port, no server process.

What is checked, each the reason a starter exists: the page boots with no error or warning (three warns on removed
APIs); the installed prop loads through the installer's loader; every scroll position draws a composed frame; a
position draws the same frame whichever way it was reached; the camera holds inside a chapter; nothing is drawn while
nothing moves; reduced motion rests; without WebGL the page is an article; a phone has no sideways scroll. And what
round eight's review of run 2 found the starter got wrong or left out: panels readable when the main script does not
load; every prop inside the frame, and beside or above the panel, at every chapter's rest for a desktop, a portrait
and a landscape phone; the phone's panel fixed in the lower part of the window; the renderer sized from the canvas's
box and no re-measure on an address-bar resize; a lost context redrawn when restored and turned into the article when
not; chapter links and jumpTo landing on a chapter's rest, at() keyed on chapter ids; onScreen() reporting boxes and
labels in the contract check_site reads.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.plugin_install import bundle_dir

REPO = Path(__file__).resolve().parent.parent
STARTER = bundle_dir() / "skills" / "scroll-3d-website" / "starter"
FIXTURE = REPO / "tests" / "fixtures" / "web_project"
NODE_MODULES = Path(os.environ.get("AGY3D_STARTER_NODE_MODULES") or FIXTURE / "node_modules")
ORIGIN = "http://starter.test"
SUB = "/some/sub/path/"  # a site that only works at the server root fails here, as it would on a real host
IMPORTS = {"three": "/_three/build/three.module.js", "three/addons/": "/_three/examples/jsm/",
           "three/examples/jsm/": "/_three/examples/jsm/"}
# Messages the software renderer prints for screenshots and readbacks; they are the test's doing, not the page's.
DRIVER_NOISE = re.compile(r"GL Driver Message|GPU stall due to ReadPixels|Automatic fallback to software WebGL")



def test_the_starter_holds_the_files_the_skill_names() -> None:
    names = {p.relative_to(STARTER).as_posix() for p in STARTER.rglob("*") if p.is_file()}
    assert names == {"index.html", "vite.config.js", "src/main.js", "src/chapters.js", "src/shots.js", "src/stage.js",
                     "src/props.js", "src/style.css"}
    html = (STARTER / "index.html").read_text()
    ids = re.findall(r'data-chapter="([a-z0-9-]+)"', html)
    shots = (STARTER / "src" / "shots.js").read_text()
    assert ids and all(re.search(rf"^\s+{i}: {{ pos:", shots, re.M) for i in ids), ids
    assert "base: './'" in (STARTER / "vite.config.js").read_text()


@pytest.fixture(scope="module")
def browser(site):
    """One Chromium for the module, started after the site is built (the package build runs its own event loop, which
    must not meet Playwright's synchronous one)."""
    from antigravity3d.adapters.engines.web_three import WebThreeValidator, _launch_args

    if not (NODE_MODULES / "three" / "package.json").is_file():
        pytest.skip(f"{NODE_MODULES} has no three.js (npm ci --prefix tests/fixtures/web_project)")
    v = WebThreeValidator()
    if why := v._unavailable():
        pytest.skip(why)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=v.binary(), args=list(_launch_args()))
        yield b
        b.close()


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    """The starter as an agent would use it: copied into a project, one prop installed by install_package and named in
    props.js the way the skill says."""
    from antigravity3d.cli import main
    from antigravity3d.deliver import install_package

    root = tmp_path_factory.mktemp("starter")
    project = root / "project"
    shutil.copytree(STARTER, project)
    shutil.copy(FIXTURE / "package.json", project / "package.json")
    if NODE_MODULES.is_dir():
        (project / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
    out = root / "crate"
    assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "web_three", "--out", str(out)]) == 0
    (pkg,) = out.glob("runs/*/package")
    r = install_package(pkg, project, compress="never", check=False)
    assert r.status == "installed", r
    asset = json.loads((project / r.dest / ".agy3d" / "install.json").read_text())["asset"]
    props = project / "src" / "props.js"
    text = props.read_text()
    line = "  hero: { at: [0, 0, 0], turn: 0, size: [1.4, 1.5, 0.9] },"
    assert line in text
    props.write_text(text.replace(line, line[:-3] + f", asset: '{asset}', module: () => import('./agy3d/{asset}/index.js') }},"))
    html = (project / "index.html").read_text()
    (project / "index.html").write_text(html.replace("<head>", f'<head>\n<script type="importmap">{json.dumps({"imports": IMPORTS})}</script>', 1))
    return project


@pytest.fixture(scope="module")
def bare(tmp_path_factory) -> Path:
    """The starter exactly as shipped: stand-ins only, the shots and layout an agent starts from."""
    project = tmp_path_factory.mktemp("bare") / "project"
    shutil.copytree(STARTER, project)
    if NODE_MODULES.is_dir():
        (project / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
    html = (project / "index.html").read_text()
    (project / "index.html").write_text(html.replace("<head>", f'<head>\n<script type="importmap">{json.dumps({"imports": IMPORTS})}</script>', 1))
    return project


def _serve(project: Path):
    three = (project / "node_modules" / "three").resolve()

    def handle(route):
        path = route.request.url[len(ORIGIN):].split("?")[0]
        if path.startswith("/_three/"):
            f = three / path[len("/_three/"):]
        elif path.startswith(SUB):
            rel = path[len(SUB):] or "index.html"
            f = project / rel
            if not f.exists():
                f = project / "public" / rel  # Vite serves public/ at the site's root
        else:
            return route.fulfill(status=404, body="not served")
        if ".." in path or not f.is_file():
            return route.fulfill(status=404, body="not found")
        kind = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".glb": "model/gltf-binary",
                ".json": "application/json"}.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        route.fulfill(status=200, body=f.read_bytes(), headers={"content-type": kind})

    return handle


class Page:
    def __init__(self, browser, project: Path, **context):
        self.ctx = browser.new_context(**{"viewport": {"width": 1280, "height": 800}, **context})
        self.ctx.route(f"{ORIGIN}/**", _serve(project))
        self.page = self.ctx.new_page()
        self.messages: list[str] = []
        self.page.on("console", lambda m: m.type in ("error", "warning") and not DRIVER_NOISE.search(m.text)
                     and self.messages.append(f"{m.type}: {m.text}"))
        self.page.on("pageerror", lambda e: self.messages.append(f"pageerror: {e}"))
        self.page.on("requestfailed", lambda r: self.messages.append(f"failed: {r.url}"))
        self.requested: list[str] = []
        self.page.on("request", lambda r: self.requested.append(r.url))

    def open(self, init: str | None = None) -> dict:
        if init:
            self.ctx.add_init_script(init)
        self.page.goto(ORIGIN + SUB)
        self.page.evaluate("window.__site.ready")
        return self.page.evaluate("({mode: __site.mode, why: __site.why || null, chapters: __site.chapters, info: __site.info()})")

    def at(self, T: float) -> np.ndarray:
        """The canvas alone at story time T (the page's text hidden), as an RGB array."""
        from PIL import Image

        self.page.evaluate(f"__site.setProgress({T})")
        self.page.add_style_tag(content="#story{visibility:hidden}")
        png = self.page.locator("#stage").screenshot()
        self.page.add_style_tag(content="#story{visibility:visible}")
        return np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int16)

    def close(self) -> None:
        self.ctx.close()


@pytest.mark.browser
def test_the_starter_boots_with_its_installed_prop_and_draws_a_composed_frame_everywhere(browser, site) -> None:
    p = Page(browser, site)
    try:
        state = p.open()
        assert state["mode"] == "webgl", state
        assert state["chapters"] == re.findall(r'data-chapter="([a-z0-9-]+)"', (site / "index.html").read_text())
        assert state["info"]["loaded"] == ["hero"] and state["info"]["failed"] == [], state
        n = len(state["chapters"])
        seen = []
        for k in range(9):
            p.page.evaluate(f"__site.setProgress({n * k / 8})")
            seen.append(p.page.evaluate("__site.info().onScreen"))
        # 1 % of the window: about a 120 px square here; the installed crate at the widest placeholder shot covers 3 %
        empty = [k for k, shown in enumerate(seen) if max((v["share"] for v in shown.values()), default=0) < 0.01]
        assert not empty, f"steps {empty} of 8 show no prop: {seen}"
        # the text panel takes the left 40 % of a desktop window: the subject is framed in the rest
        hidden = [k for k, shown in enumerate(seen) if all(v["x"] < 0.42 for v in shown.values())]
        assert not hidden, f"steps {hidden} of 8 have their subject behind the text: {seen}"
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_a_scroll_position_draws_the_same_frame_however_it_was_reached_and_shots_hold(browser, site) -> None:
    p = Page(browser, site)
    try:
        n = len(p.open()["chapters"])
        p.at(0)
        forward = p.at(2.2)
        p.at(n)
        backward = p.at(2.2)
        assert int(np.abs(forward - backward).max()) <= 2, "the picture depends on the way there: state is eased per frame"
        for i in range(n):
            a, b = p.page.evaluate(f"[__site.evaluate({i} + 0.5), __site.evaluate({i} + 0.95)]")
            assert a["chapter"] == b["chapter"] and a["cam"]["target"] == pytest.approx(b["cam"]["target"]), i
            if i:
                moving = p.page.evaluate(f"[__site.evaluate({i} + 0.05), __site.evaluate({i} + 0.3)]")
                assert moving[0]["cam"]["pos"] != pytest.approx(moving[1]["cam"]["pos"], abs=1e-3), f"chapter {i} has no move"
        before = p.page.evaluate("__site.info().frames")
        p.page.wait_for_timeout(600)
        assert p.page.evaluate("__site.info().frames") == before, "the page keeps drawing while nothing moves"
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_reduced_motion_rests_on_one_picture_per_chapter(browser, site) -> None:
    p = Page(browser, site, reduced_motion="reduce")
    try:
        p.open()
        a = p.at(1.1)
        t_a = p.page.evaluate("__site.progress()")
        b = p.at(1.9)
        assert t_a == p.page.evaluate("__site.progress()") == pytest.approx(1.7)
        assert int(np.abs(a - b).max()) <= 2
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_without_webgl_the_page_is_an_article(browser, site) -> None:
    p = Page(browser, site)
    try:
        state = p.open(init="HTMLCanvasElement.prototype.getContext = function () { return null; };")
        assert state["mode"] == "no-webgl" and "WebGL" in state["why"], state
        assert p.page.evaluate("getComputedStyle(document.getElementById('stage')).display") == "none"
        assert p.page.locator("h1").is_visible()
        # three.js is not even fetched: the scene module is imported only once WebGL is known to work
        assert not [u for u in p.requested if "/_three/" in u or u.endswith("stage.js")], p.requested
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_a_phone_gets_the_scene_above_the_text_and_no_sideways_scroll(browser, site) -> None:
    p = Page(browser, site, viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    try:
        state = p.open()
        assert state["mode"] == "webgl"
        assert p.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for T in (0.6, 1.6, 2.6):
            p.page.evaluate(f"__site.setProgress({T})")
            hero = p.page.evaluate("__site.info().onScreen").get("hero")
            # the text takes the lower 45 % of a phone's window (style.css): the subject belongs above it
            assert hero and hero["share"] > 0.01 and hero["y"] < 0.55, (T, hero)
        assert p.messages == [], p.messages
    finally:
        p.close()


# --------------------------------------------------------------------------- round eight: what the review of run 2 found

REST = 0.7  # shots.js: where a chapter rests, its move done and its text up
SIZES = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
    "landscape phone": {"viewport": {"width": 844, "height": 390}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
}
_PANEL = """(i) => { const el = document.querySelectorAll('.panel')[i], r = el.getBoundingClientRect();
  return { box: [r.left / innerWidth, r.top / innerHeight, r.right / innerWidth, r.bottom / innerHeight],
           opacity: +getComputedStyle(el).opacity }; }"""


@pytest.mark.browser
@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_every_panel_is_readable_in_order_when_the_main_script_does_not_load(browser, bare, size) -> None:
    """Run 2 dimmed its panels under .js, a class the page sets before any script: with main.js blocked, no text."""
    p = Page(browser, bare, **SIZES[size])
    try:
        p.page.route("**/src/main.js", lambda route: route.abort())
        p.page.goto(ORIGIN + SUB)
        state = p.page.evaluate("""() => ({
          classes: document.documentElement.className,
          panels: [...document.querySelectorAll('.panel')].map((el) => {
            el.scrollIntoView({ block: 'center' });
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + Math.min(r.height / 2, 40));
            let opacity = 1;
            for (let e = el; e; e = e.parentElement) opacity *= +getComputedStyle(e).opacity;
            return { top: r.top + scrollY, opacity, visibility: getComputedStyle(el).visibility,
                     onTop: el.contains(hit), words: el.innerText.trim().split(/\\s+/).length };
          }),
        })""")
        assert "webgl-on" not in state["classes"], state
        panels = state["panels"]
        assert len(panels) == len(re.findall(r'data-chapter="', (bare / "index.html").read_text()))
        assert [q["top"] for q in panels] == sorted(q["top"] for q in panels), "the article is not in document order"
        for i, q in enumerate(panels):
            assert q["opacity"] == 1 and q["visibility"] == "visible" and q["onTop"] and q["words"] >= 5, (i, q)
        assert [m for m in p.messages if "main.js" not in m and "ERR_FAILED" not in m] == [], p.messages
    finally:
        p.close()


# The free area of the stacked (portrait phone) layout: the window above the highest panel of the fixed panel layer.
_FREE_BOTTOM = "Math.min(...[...document.querySelectorAll('.panel')].map((el) => el.getBoundingClientRect().top)) / innerHeight"


@pytest.mark.browser
@pytest.mark.parametrize("size", ["desktop", "landscape phone"])
def test_every_prop_on_stage_is_in_frame_and_beside_the_panel_at_each_chapters_rest(browser, bare, size) -> None:
    """Run 2 widened a phone's view by a fixed factor and cut its props at both side edges. Side by side, the starter
    holds each shot's horizontal coverage: every prop on stage is inside the window and right of the panel."""
    p = Page(browser, bare, **SIZES[size])
    try:
        ids = p.open()["chapters"]
        for i, chapter in enumerate(ids):
            T = i + REST
            p.page.evaluate(f"__site.setProgress({T})")
            on = p.page.evaluate("__site.info().onScreen")
            staged = [n for n, v in p.page.evaluate(f"__site.evaluate({T}).show").items() if v > 0.99]
            panel = p.page.evaluate(_PANEL, i)["box"]
            assert p.page.evaluate("document.querySelector('.is-current').dataset.chapter") == chapter
            assert staged, chapter
            for name in staged:
                assert name in on and on[name]["kind"] == "prop", (chapter, name, on)
                x0, y0, x1, y1 = on[name]["box"]
                assert -0.002 <= x0 and x1 <= 1.002 and -0.002 <= y0 and y1 <= 1.002, (size, chapter, name, on[name])
                assert x0 >= panel[2], (size, chapter, name, on[name]["box"], "panel", panel)
        assert p.messages == [], p.messages
    finally:
        p.close()


# In the stacked layout a subject must fill at least this share of the free area's width or of its height, whichever
# it reaches: the merged starter held the desktop's coverage there and its hero was 22 % of the width, a speck above a
# tall band of empty floor. stage.js aims at 62 % / 60 % for a named subject, 38 % / 35 % for an establishing shot.
SUBJECT_MIN, WIDE_MIN = 0.5, 0.3


@pytest.mark.browser
def test_the_stacked_phone_layout_fits_each_shots_subject_into_the_space_above_the_panel(browser, bare) -> None:
    p = Page(browser, bare, **SIZES["phone"])
    try:
        ids = p.open()["chapters"]
        bottom = p.page.evaluate(_FREE_BOTTOM)
        assert 0.5 < bottom < 0.9, bottom
        for i, chapter in enumerate(ids):
            T = i + REST
            p.page.evaluate(f"__site.setProgress({T})")
            on = p.page.evaluate("__site.info().onScreen")
            named = p.page.evaluate(f"__site.evaluate({T}).subject")
            # a shot that names no subject is an establishing shot of the props on stage in its chapter
            subjects = named or [n for n, v in p.page.evaluate(f"__site.evaluate({T}).show").items() if v > 0.99]
            assert subjects, chapter
            boxes = []
            for name in subjects:
                assert name in on and on[name].get("subject", True), (chapter, name, on)
                boxes.append(on[name]["box"])
            x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            assert -0.002 <= x0 and x1 <= 1.002 and -0.002 <= y0 and y1 <= bottom + 0.002, (chapter, (x0, y0, x1, y1), bottom)
            least = SUBJECT_MIN if named else WIDE_MIN
            assert max((x1 - x0), (y1 - y0) / bottom) >= least, (chapter, "a speck", (x0, y0, x1, y1), bottom)
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_stacked_framing_is_continuous_through_a_chapter_change_and_a_function_of_story_time(browser, bare) -> None:
    """The subject changes from one shot to the next during the move (how: hero and part; detail: part): the framing
    blends on the camera's curve, so the picture moves on without a jump, and it is the same however T was reached."""
    p = Page(browser, bare, **SIZES["phone"])
    try:
        ids = p.open()["chapters"]
        start = ids.index("detail") - 0.1
        boxes = lambda t0, dt, n: np.array(p.page.evaluate(
            f"[...Array({n}).keys()].map((k) => {{ __site.setProgress({t0} + k * {dt}); return __site.info().onScreen.hero.box; }})"))
        coarse = np.abs(np.diff(boxes(start, 0.005, 121), axis=0)).max(axis=1)
        k = int(coarse.argmax())
        assert coarse[k] < 0.1, coarse.round(3).tolist()
        # where it changes fastest, a step ten times smaller changes it about ten times less: a slope, not a jump
        fine = np.abs(np.diff(boxes(start + k * 0.005, 0.0005, 11), axis=0)).max()
        assert fine < 0.25 * coarse[k], (fine, coarse[k])
        at_change = boxes(ids.index("detail") - 1e-4, 2e-4, 2)
        assert np.abs(at_change[1] - at_change[0]).max() < 0.005, at_change
        p.page.evaluate("__site.setProgress(0)")
        forward = boxes(start + 0.25, 0, 1)
        p.page.evaluate(f"__site.setProgress({len(ids)})")
        backward = boxes(start + 0.25, 0, 1)
        assert np.abs(forward - backward).max() < 1e-6, (forward, backward)
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_phone_panel_stays_in_the_lower_part_of_the_window_through_each_chapter(browser, bare) -> None:
    """Run 2's phone panel was sticky inside its section and slid up across the 3D for a fifth of every chapter."""
    p = Page(browser, bare, **SIZES["phone"])
    try:
        ids = p.open()["chapters"]
        for i, chapter in enumerate(ids):
            places = []
            for local in (0.15, 0.5, 0.95):
                p.page.evaluate(f"__site.setProgress({i + local})")
                p.page.wait_for_function("""(i) => [...document.querySelectorAll('.panel')].every((el, k) =>
                    +getComputedStyle(el).opacity === (k === i ? 1 : 0))""", arg=i, timeout=5000)
                places.append(p.page.evaluate(_PANEL, i)["box"])
            for box in places:
                assert box[1] >= 0.6 and box[3] <= 1.0, (chapter, places)
            assert all(b == pytest.approx(places[0], abs=1e-3) for b in places), (chapter, "the panel moves", places)
        assert p.messages == [], p.messages
    finally:
        p.close()


_CANVAS = """() => { const c = document.getElementById('stage'), r = Math.min(devicePixelRatio, 2);
  return c.width === Math.floor(c.clientWidth * r) && c.height === Math.floor(c.clientHeight * r); }"""


@pytest.mark.browser
def test_the_renderer_follows_the_canvas_box_and_an_address_bar_does_not_remeasure(browser, bare) -> None:
    """Run 2 drew at innerHeight into a 100lvh canvas and re-measured the page on every address-bar resize."""
    p = Page(browser, bare, **SIZES["phone"])
    try:
        p.open()
        assert p.page.evaluate(_CANVAS)
        before = p.page.evaluate("__site.info().measures")
        # the address bar slides in: 54 px less height, same width
        p.page.set_viewport_size({"width": 390, "height": 790})
        p.page.wait_for_function(_CANVAS, timeout=5000)
        p.page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
        assert p.page.evaluate("__site.info().measures") == before, "a height-only resize re-measured the chapters"
        # a rotation or a split screen: a new width is a new layout
        p.page.set_viewport_size({"width": 430, "height": 790})
        p.page.wait_for_function(f"__site.info().measures > {before}", timeout=5000)
        p.page.wait_for_function(_CANVAS, timeout=5000)
        assert p.messages == [], p.messages
    finally:
        p.close()
    p = Page(browser, bare, **SIZES["desktop"])
    try:
        p.open()
        before = p.page.evaluate("__site.info().measures")
        # on a desktop a window made shorter changes every chapter's height (they are in vh)
        p.page.set_viewport_size({"width": 1440, "height": 800})
        p.page.wait_for_function(f"__site.info().measures > {before}", timeout=5000)
        p.page.wait_for_function(_CANVAS, timeout=5000)
        assert p.messages == [], p.messages
    finally:
        p.close()


def _canvas_now(p: Page) -> np.ndarray:
    """The canvas as it is (no new story time set, so nothing is drawn by the test), as an RGB array."""
    from PIL import Image

    p.page.add_style_tag(content="#story{visibility:hidden}")
    png = p.page.locator("#stage").screenshot()
    p.page.add_style_tag(content="#story{visibility:visible}")
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int16)


_LOSE = "document.getElementById('stage').getContext('webgl2').getExtension('WEBGL_lose_context')"


@pytest.mark.browser
def test_a_lost_context_is_redrawn_when_restored_and_becomes_the_article_when_not(browser, bare) -> None:
    p = Page(browser, bare)
    try:
        p.open()
        if not p.page.evaluate(f"!!{_LOSE}"):
            pytest.skip("this browser's WebGL has no WEBGL_lose_context: context loss cannot be caused here")
        # the support test gave its own context back: only the renderer's is alive
        before = p.at(1.7)
        frames = p.page.evaluate("__site.info().frames")
        p.page.evaluate(f"window.__lose = {_LOSE}; __lose.loseContext()")
        p.page.wait_for_function("document.getElementById('stage').getContext('webgl2').isContextLost()")
        p.page.evaluate("__lose.restoreContext()")
        # nothing scrolls: the page must notice by itself that the picture is gone and draw it again (three starts its
        # frame count again on a restored context, so any change of it is a frame drawn since)
        p.page.wait_for_function(f"!document.getElementById('stage').getContext('webgl2').isContextLost() && __site.info().frames !== {frames}", timeout=5000)
        p.page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
        after = _canvas_now(p)
        # the environment map is rebuilt: without it the props come back darker by tens of levels
        assert int(np.abs(after - before).max()) <= 3, int(np.abs(after - before).max())
        assert p.page.evaluate("__site.mode") == "webgl"

        p.page.evaluate("__lose.loseContext()")
        p.page.wait_for_function("__site.mode === 'no-webgl'", timeout=8000)
        assert p.page.evaluate("getComputedStyle(document.getElementById('stage')).display") == "none"
        assert p.page.evaluate("[...document.querySelectorAll('.panel')].every((el) => +getComputedStyle(el).opacity === 1)")
        assert [m for m in p.messages if "CONTEXT_LOST" not in m] == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_chapter_links_and_jump_to_land_on_the_chapters_rest_and_at_names_moments_by_chapter(browser, bare) -> None:
    p = Page(browser, bare)
    try:
        ids = p.open()["chapters"]
        how = ids.index("how")
        assert p.page.evaluate("__site.at('how', 0.5)") == how + 0.5
        with pytest.raises(Exception, match="no chapter of the page has that id"):
            p.page.evaluate("__site.at('no-such-chapter')")
        # the starter's own prop timings are keyed on chapter ids: the part rises during the move into "how"
        rise = p.page.evaluate("[0, 0.175, 0.35].map((f) => __site.evaluate(__site.at('how', f)).show.part)")
        assert rise[0] == 0 and 0 < rise[1] < 1 and rise[2] == 1, rise
        # the link at the end of the page lands where "how" rests, not at its top (the previous chapter's picture)
        p.page.evaluate(f"__site.setProgress({len(ids) - 0.3})")
        p.page.click('a[href="#how"]')
        p.page.wait_for_function(f"Math.abs(__site.progress() - {how + REST}) < 0.01", timeout=8000)
        assert p.page.evaluate("location.hash") == "#how"
        p.page.evaluate("__site.jumpTo('detail')")
        p.page.wait_for_function(f"Math.abs(__site.progress() - {ids.index('detail') + REST}) < 0.01", timeout=8000)
        assert p.messages == [], p.messages
    finally:
        p.close()
    p = Page(browser, bare)
    try:
        p.page.goto(ORIGIN + SUB + "#detail")
        p.page.evaluate("window.__site.ready")
        assert p.page.evaluate("__site.progress()") == pytest.approx(ids.index("detail") + REST, abs=0.01)
    finally:
        p.close()


@pytest.mark.browser
def test_on_screen_reports_each_props_and_labels_box_in_the_contract_check_site_reads(browser, bare) -> None:
    p = Page(browser, bare)
    try:
        p.open()
        # labels as a page registers them: one in the scene, one that belongs to the prop "part" (on stage from "how")
        p.page.evaluate("""async () => {
          const THREE = await import('three');
          const plane = (w, h) => new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color: '#ffffff' }));
          const wide = plane(12, 0.3);
          wide.position.set(0, 2.2, 0);
          window.__site.label('wide', wide);
          const tag = plane(0.4, 0.15);
          tag.position.set(0, 0.8, 0.3);
          window.__site.label('tag', tag, 'part');
        }""")
        p.page.evaluate("__site.setProgress(0.7)")
        intro = p.page.evaluate("__site.info().onScreen")
        assert "tag" not in intro, "a label of a prop that is off stage is reported"
        p.page.evaluate("__site.setProgress(1.7)")
        how = p.page.evaluate("__site.info().onScreen")
        assert {how[n]["kind"] for n in ("wide", "tag")} == {"label"} and how["hero"]["kind"] == "prop", how
        for name, v in {**intro, **how}.items():
            assert set(v) - {"subject"} == {"share", "x", "y", "box", "kind"}, (name, v)
            x0, y0, x1, y1 = v["box"]
            cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x1, 1), min(y1, 1)
            # share, x and y describe the part inside the window; box is the whole of it
            assert v["share"] == pytest.approx((cx1 - cx0) * (cy1 - cy0), abs=1e-6), (name, v)
            assert v["x"] == pytest.approx((cx0 + cx1) / 2, abs=1e-6) and v["y"] == pytest.approx((cy0 + cy1) / 2, abs=1e-6), (name, v)
        x0, _, x1, _ = how["wide"]["box"]
        assert x0 < 0 and x1 > 1, ("the box is clamped to the window", how["wide"])
        # the subject flag follows the chapter drawn: shots.js names hero for intro, hero and part for how, part for
        # detail, and nothing for outro (then the field is left out and every prop counts)
        flags = {}
        for T in (0.7, 1.2, 1.7, 2.7, 3.7):
            p.page.evaluate(f"__site.setProgress({T})")
            on = p.page.evaluate("__site.info().onScreen")
            flags[T] = {n: v.get("subject") for n, v in on.items() if v["kind"] == "prop"}
        assert flags[0.7] == {"hero": True}, flags
        assert flags[1.2] == flags[1.7] == {"hero": True, "part": True}, flags
        assert flags[2.7] == {"hero": False, "part": True}, flags
        assert flags[3.7] == {"hero": None}, flags
        assert "subject" not in p.page.evaluate("__site.info().onScreen")["hero"]
        assert p.messages == [], p.messages
    finally:
        p.close()
