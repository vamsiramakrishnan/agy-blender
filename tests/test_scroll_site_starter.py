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
nothing moves; reduced motion rests; without WebGL the page is an article; a phone has no sideways scroll.
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
