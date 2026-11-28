"""The story-slides-3d skill's starter runs: it is the engine an agent copies and fills in to tell a story as a short film,
so it must work as shipped, built with the workspace's Vite, in a headless browser.

The starter is built the way an agent's project builds it (Vite 8 over ``three`` from ``AGY3D_STARTER_NODE_MODULES``,
or ``tests/fixtures/web_project/node_modules``) and served from a routed fake origin under a sub-path: no port, no
server process. Without Playwright's browser, or without a workspace that has Vite and three.js, the browser tests
skip with the reason.

What is checked is what a viewer and check_site rely on (the contract, story/CONTRACT.md): the page boots clean and
its hook lists the HTML's scenes; every beat shows a picture at a desktop and a phone size, and each beat changes it;
a beat's picture is the same however it was reached; beats move while they play and ambient time moves; keys, clicks,
buttons, the hash and auto mode navigate; reduced motion stands still with whole captions; without WebGL the page is
the article; the story's text is never changed; the three treatments differ and every cut starts and ends on the
scenes' own pictures; the hook rejects what does not exist. And the contract's additions: a prop installed by the
plugin's installer takes the stand-in's place; speaker's notes and type over the picture.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.plugin_install import bundle_dir

REPO = Path(__file__).resolve().parent.parent
STARTER = bundle_dir() / "skills" / "story-slides-3d" / "starter"
FIXTURE = REPO / "tests" / "fixtures" / "web_project"
NODE_MODULES = Path(os.environ.get("AGY3D_STARTER_NODE_MODULES") or FIXTURE / "node_modules")
ORIGIN = "http://film.test"
SUB = "/some/sub/path/"  # a film that only works at the server root fails here, as it would on a real host
DRIVER_NOISE = re.compile(r"GL Driver Message|GPU stall due to ReadPixels|Automatic fallback to software WebGL")
CUTS = ["fade", "iris", "wipe", "fall", "flip"]
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
SMALL = {"width": 960, "height": 600}  # the size of the tests that are not about framing: the software renderer is slow


def _html_scenes() -> list[tuple[str, int]]:
    html = (STARTER / "index.html").read_text()
    out = []
    for m in re.finditer(r'<section data-scene="([a-z0-9-]+)">(.*?)</section>', html, re.S):
        out.append((m.group(1), len(re.findall(r"<div data-beat", m.group(2)))))
    return out


def test_the_starter_holds_the_files_the_skill_names_and_story_js_follows_the_html() -> None:
    names = {p.relative_to(STARTER).as_posix() for p in STARTER.rglob("*") if p.is_file()}
    scenes = _html_scenes()
    assert names == {"index.html", "vite.config.js", "src/main.js", "src/film.js", "src/print.js", "src/cuts.js",
                     "src/kit.js", "src/caption.js", "src/story.js", "src/props.js", "src/style.css",
                     *(f"src/scenes/{i}.js" for i, _ in scenes)}, names
    assert scenes == [("title", 1), ("tower", 2), ("room", 2), ("shore", 2), ("end", 1)], scenes
    story = (STARTER / "src" / "story.js").read_text()
    assert re.search(r"scenes: \[(.*)\]", story).group(1).split(", ") == [i for i, _ in scenes]
    for i, beats in scenes:
        src = (STARTER / "src" / "scenes" / f"{i}.js").read_text()
        assert f"id: '{i}'" in src and len(re.findall(r"\{ dur: [\d.]+, hold: [\d.]+ \}", src)) == beats, i
        assert "Math.random" not in src, f"{i}: update(s) must be a function of s"
    assert "base: './'" in (STARTER / "vite.config.js").read_text()
    # the cuts the contract names, each entering its scene
    enters = {i: re.search(r"enterWith: '(\w+)'", (STARTER / "src" / "scenes" / f"{i}.js").read_text()).group(1)
              for i, _ in scenes[1:]}
    assert enters == {"tower": "iris", "room": "wipe", "shore": "fall", "end": "fade"}, enters


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


def _project(root: Path) -> Path:
    if not (NODE_MODULES / "vite" / "bin" / "vite.js").is_file() or not (NODE_MODULES / "three" / "package.json").is_file():
        pytest.skip(f"{NODE_MODULES} has no Vite and three.js to build the starter with")
    project = root / "project"
    shutil.copytree(STARTER, project)
    (project / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
    return project


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    """The starter exactly as shipped, built with the workspace's Vite."""
    return _vite_build(_project(tmp_path_factory.mktemp("film")))


def _serve(dist: Path):
    def handle(route):
        path = route.request.url[len(ORIGIN):].split("?")[0].split("#")[0]
        if not path.startswith(SUB) or ".." in path:
            return route.fulfill(status=404, body="not served")
        f = dist / (path[len(SUB):] or "index.html")
        if not f.is_file():
            return route.fulfill(status=404, body="not found")
        kind = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".glb": "model/gltf-binary",
                ".json": "application/json"}.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        route.fulfill(status=200, body=f.read_bytes(), headers={"content-type": kind})

    return handle


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
        return self.page.evaluate("({mode: __story.mode, why: __story.why || null, scenes: __story.scenes, "
                                  "treatment: __story.treatment, info: __story.info()})")

    def frame(self) -> np.ndarray:
        """The canvas as an RGB array (the over-picture headline hidden: the picture alone)."""
        from PIL import Image

        self.page.add_style_tag(content="#story{visibility:hidden}")
        png = self.page.locator("#stage").screenshot()
        self.page.add_style_tag(content="#story{visibility:visible}")
        return np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int16)

    def go(self, scene: str, beat: int) -> np.ndarray:
        self.page.evaluate(f"__story.go('{scene}', {beat})")
        return self.frame()

    def info(self) -> dict:
        return self.page.evaluate("__story.info()")

    def caption(self) -> str:
        return self.page.evaluate("[...document.querySelectorAll('#story [data-beat].is-current > p')]"
                                  ".map((p) => p.textContent.replace(/\\s+/g, ' ').trim()).join(' | ')")

    def close(self) -> None:
        self.ctx.close()


def _same(a: np.ndarray, b: np.ndarray, tol: int = 2) -> bool:
    return a.shape == b.shape and int(np.abs(a - b).max()) <= tol


def _changed(a: np.ndarray, b: np.ndarray) -> float:
    """The share of pixels that differ by more than a few levels."""
    return float((np.abs(a - b).max(axis=2) > 24).mean())


def _shows_something(img: np.ndarray) -> float:
    """The share of the picture that differs from its commonest colour (check_site's "a beat that shows nothing")."""
    flat = img.reshape(-1, 3)
    colours, counts = np.unique(flat, axis=0, return_counts=True)
    common = colours[counts.argmax()]
    return float((np.abs(flat - common).max(axis=1) > 8).mean())


def _beats(p: Page) -> list[tuple[str, int]]:
    return [(s["id"], k) for s in p.page.evaluate("__story.scenes") for k in range(s["beats"])]


@pytest.mark.browser
def test_it_builds_and_boots_clean_and_the_hook_lists_the_htmls_scenes(browser, dist) -> None:
    p = Page(browser, dist, viewport=DESKTOP)
    try:
        t0 = time.monotonic()
        state = p.open()
        ready = time.monotonic() - t0
        assert state["mode"] == "webgl" and state["treatment"] == "halftone", state
        assert [(s["id"], s["beats"]) for s in state["scenes"]] == _html_scenes()
        assert [s["title"] for s in state["scenes"]] == ["Keeper", "The light", "The lamp room", "Morning", "End"]
        assert p.page.evaluate("__story.title") == "Keeper"
        assert state["info"]["scene"] == "title" and state["info"]["beat"] == 0, state["info"]
        assert p.page.locator("#count").inner_text().lower() == "scene 1 of 5"
        assert not [u for u in p.requested if not u.startswith(ORIGIN)], p.requested
        # a frame of the film stays under 60 draw calls (scenes, their shadow passes and the print)
        calls = {}
        for scene, beat in _beats(p):
            p.page.evaluate(f"__story.go('{scene}', {beat})")
            calls[scene, beat] = p.info()["calls"]
        assert max(calls.values()) < 60, calls
        assert p.messages == [], p.messages
        print(f"ready in {ready:.1f} s; draw calls per beat {calls}")
    finally:
        p.close()


@pytest.mark.browser
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_every_beat_shows_a_picture_and_each_beat_changes_it(browser, dist, viewport) -> None:
    p = Page(browser, dist, viewport=viewport)
    try:
        p.open()
        shown, frames = {}, {}
        for scene, beat in _beats(p):
            frames[scene, beat] = p.go(scene, beat)
            shown[scene, beat] = round(_shows_something(frames[scene, beat]), 3)
        assert min(shown.values()) > 0.1, shown  # check_site's floor is 1 %; every beat of Keeper fills far more
        for scene, beat in frames:
            if beat:
                change = _changed(frames[scene, beat - 1], frames[scene, beat])
                assert change > 0.05, f"{scene} beat {beat + 1} changes {change:.1%} of the picture"
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_a_beat_is_the_same_picture_however_it_was_reached(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        p.open()
        for scene, beat in [("tower", 1), ("room", 0), ("shore", 1)]:
            direct = p.go(scene, beat)
            others = [b for b in range(2) if b != beat]
            p.go(scene, others[0])
            p.page.evaluate("__story.advance()" if others[0] < beat else "__story.back()")
            again = p.go(scene, beat)
            p.go("end", 0)
            p.go("title", 0)
            third = p.go(scene, beat)
            assert _same(direct, again) and _same(direct, third), (scene, beat, int(np.abs(direct - again).max()),
                                                                   int(np.abs(direct - third).max()))
        # a settled beat is held: nothing is drawn while nothing moves
        before = p.info()["frames"]
        p.page.wait_for_timeout(600)
        assert p.info()["frames"] == before
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_something_moves_while_a_beat_plays_and_ambient_time_moves(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        p.open()
        still = []
        for scene, beat in _beats(p):
            p.page.evaluate(f"__story.at('{scene}', {beat}, 0.3, 1)")
            a = p.frame()
            info = p.page.evaluate(f"__story.at('{scene}', {beat}, 1, 1)")
            b = p.frame()
            if _changed(a, b) < 0.01:
                still.append((scene, beat))
            assert info["playing"] is False and info["scene"] == scene and info["beat"] == beat
        assert not still, f"nothing moves while these beats play: {still}"
        moving = []
        for s in p.page.evaluate("__story.scenes"):
            p.page.evaluate(f"__story.at('{s['id']}', 0, 1, 1)")
            a = p.frame()
            p.page.evaluate(f"__story.at('{s['id']}', 0, 1, 3)")
            if _changed(a, p.frame()) > 0.005:
                moving.append(s["id"])
        assert {"title", "tower", "room"} <= set(moving), moving
        # play() runs the beat as a viewer sees it and resolves at its end
        t0 = time.monotonic()
        info = p.page.evaluate("__story.play('title', 0)")
        assert time.monotonic() - t0 >= 3.5 and info["scene"] == "title" and info["playing"] is False
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_keys_clicks_and_buttons_navigate_and_the_count_and_caption_follow(browser, dist) -> None:
    p = Page(browser, dist)
    idle = "() => !__story.info().cut"

    def at() -> tuple[str, int]:
        p.page.wait_for_function(idle)
        i = p.info()
        return i["scene"], i["beat"]

    try:
        p.open(query="?pace=8")
        p.page.evaluate("__story.go('title', 0)")
        p.page.keyboard.press("ArrowRight")
        assert at() == ("tower", 0)
        assert p.page.locator("#count").inner_text().lower() == "scene 2 of 5"
        assert p.caption() == "The light went round every nine seconds. It had done so for sixty years."
        assert p.caption() == " | ".join(p.info()["lines"])
        p.page.keyboard.press(" ")
        assert at() == ("tower", 1)
        assert p.caption() == "Tonight something out there answered it."
        p.page.keyboard.press("PageDown")
        assert at() == ("room", 0)
        p.page.keyboard.press("Enter")
        assert at() == ("room", 1)
        p.page.keyboard.press("ArrowLeft")
        assert at() == ("room", 0)
        p.page.keyboard.press("PageUp")
        assert at() == ("tower", 1)
        p.page.keyboard.press("Backspace")
        assert at() == ("tower", 0)
        p.page.locator("#next").click()
        assert at() == ("tower", 1)
        p.page.locator("#back").click()
        assert at() == ("tower", 0)
        p.page.locator("#picture").click()
        assert at() == ("tower", 1)
        p.page.keyboard.press("End")
        assert at() == ("end", 0)
        assert p.page.locator("#next").is_disabled() and p.page.locator("#count").inner_text().lower() == "scene 5 of 5"
        p.page.keyboard.press("ArrowRight")
        assert at() == ("end", 0)  # the end is the end
        p.page.keyboard.press("Home")
        assert at() == ("title", 0)
        assert p.page.locator("#back").is_disabled()
        p.page.keyboard.press("a")
        assert p.info()["auto"] is True and p.page.locator("#auto").get_attribute("aria-pressed") == "true"
        p.page.locator("#auto").click()
        assert p.info()["auto"] is False
        # the buttons are buttons, reachable by Tab, and Space on a focused button does what the button does, once
        assert p.page.evaluate("[...document.querySelectorAll('.controls > *')].every((b) => b.tagName === 'BUTTON')")
        p.page.locator("#next").focus()
        p.page.keyboard.press(" ")
        assert at() == ("tower", 0)
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_hash_follows_every_move_and_opens_its_beat(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        p.open(query="?pace=8")
        assert p.page.evaluate("location.hash") == "#title-1"
        p.page.evaluate("__story.go('room', 1)")
        assert p.page.evaluate("location.hash") == "#room-2"
        p.page.evaluate("__story.back()")
        assert p.page.evaluate("location.hash") == "#room-1"
        p.page.evaluate("location.hash = '#shore-2'")
        p.page.wait_for_function("() => __story.info().scene === 'shore' && __story.info().beat === 1")
        p.close()
        p = Page(browser, dist)
        state = p.open(hash="#room-2")
        assert (state["info"]["scene"], state["info"]["beat"]) == ("room", 1), state["info"]
        assert p.caption() == "Then he did the one thing the job allowed. He kept the light turning."
        p.close()
        p = Page(browser, dist)
        state = p.open(hash="#nowhere-9")  # a hash that names nothing opens the start
        assert (state["info"]["scene"], state["info"]["beat"]) == ("title", 0)
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_auto_mode_advances_after_each_beats_time_and_stops_at_the_end(browser, dist) -> None:
    pace = 30
    p = Page(browser, dist)
    try:
        p.open(query=f"?pace={pace}", init="window.__seen = []; const r = history.replaceState.bind(history);"
               "history.replaceState = (s, t, u) => { window.__seen.push(String(u)); return r(s, t, u); };")
        p.page.evaluate("__story.go('title', 0)")
        p.page.evaluate("window.__seen = []")
        t0 = time.monotonic()
        p.page.evaluate("__story.setAuto(true)")
        p.page.wait_for_function("() => !__story.info().auto", timeout=60_000)
        took = time.monotonic() - t0
        log = p.page.evaluate("window.__seen")
        seen = [u for i, u in enumerate(log) if i == 0 or u != log[i - 1]]
        assert seen == ["#tower-1", "#tower-2", "#room-1", "#room-2", "#shore-1", "#shore-2", "#end-1"], seen
        info = p.info()
        assert (info["scene"], info["beat"], info["auto"]) == ("end", 0, False)
        # each beat held for its dur + hold after it began: the title's hold (its beat is shown finished, 4 s), then
        # 77 s of the other beats' dur + hold, at `pace` times the speed
        assert took >= 0.8 * (4 + 77) / pace, took
        assert p.page.locator("#auto").get_attribute("aria-pressed") == "false"
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_reduced_motion_shows_finished_beats_whole_captions_and_nothing_moving(browser, dist) -> None:
    p = Page(browser, dist, reduced_motion="reduce")
    try:
        p.open()
        info = p.page.evaluate("__story.play('tower', 1)")  # resolves at once: a beat appears finished
        assert info["playing"] is False and info["cut"] is None
        p.page.keyboard.press("ArrowRight")
        assert p.info()["cut"] is None and p.info()["scene"] == "room"  # cuts are instant
        a = p.frame()
        p.page.wait_for_timeout(1000)
        b = p.frame()
        assert _same(a, b), int(np.abs(a - b).max())
        assert p.page.evaluate("[...document.querySelectorAll('#story [data-beat].is-current .ch')]"
                               ".every((c) => getComputedStyle(c).opacity === '1')")
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_without_webgl_the_page_is_the_article_with_a_note(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        state = p.open(init="HTMLCanvasElement.prototype.getContext = function () { return null; };")
        assert state["mode"] == "no-webgl" and "WebGL" in state["why"], state
        assert p.page.locator("#note").is_visible()
        assert p.page.locator("#stage").is_hidden() and p.page.locator(".controls").is_hidden()
        heads = p.page.locator("#story h2")
        assert [heads.nth(i).inner_text() for i in range(heads.count())] == ["Keeper", "The light", "The lamp room", "Morning", "End"]
        assert all(heads.nth(i).is_visible() for i in range(heads.count()))
        lines = p.page.locator("#story p")
        assert lines.count() == 10 and all(lines.nth(i).is_visible() for i in range(lines.count()))
        assert p.page.locator("aside[data-notes]").first.is_visible()  # notes read as small text after their beat
        assert p.page.evaluate("document.scrollingElement.scrollHeight > innerHeight")  # it scrolls as an article
        err = p.page.evaluate("__story.go('tower', 0).then(() => 'resolved', (e) => e.message)")
        assert "no picture" in err, err
        # the film's code (three.js with it) is not even fetched: it is imported only once WebGL is known to work
        assert len([u for u in p.requested if u.endswith(".js")]) == 1, p.requested
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_storys_text_is_never_changed(browser, dist) -> None:
    ctx = browser.new_context(java_script_enabled=False)
    ctx.route(f"{ORIGIN}/**", _serve(dist))
    page = ctx.new_page()
    page.goto(ORIGIN + SUB)
    source = page.evaluate("document.getElementById('story').textContent")
    ctx.close()
    p = Page(browser, dist)
    try:
        p.open(query="?pace=12")
        assert p.page.evaluate("document.getElementById('story').textContent") == source
        for scene, beat in _beats(p):
            p.page.evaluate(f"__story.play('{scene}', {beat})")
            lines = p.info()["lines"]
            assert lines and all(line in re.sub(r"\s+", " ", source) for line in lines), lines
        p.page.keyboard.press("Home")
        p.page.keyboard.press("n")
        assert p.page.evaluate("document.getElementById('story').textContent") == source
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_three_treatments_differ_and_every_cut_ends_on_the_scenes_own_pictures(browser, dist) -> None:
    pictures = {}
    for name in ("halftone", "grade", "glyph"):
        p = Page(browser, dist)
        try:
            state = p.open(query=f"?print={name}")
            assert state["treatment"] == name
            pictures[name] = p.go("room", 0)
            assert _shows_something(pictures[name]) > 0.05, name  # glyph prints dark as nothing: less is drawn
            if name == "halftone":
                leaving, arriving = p.go("tower", 1), p.go("room", 0)
                for cut in CUTS:
                    got = {}
                    for q in (0, 0.5, 1):
                        p.page.evaluate("__story.go('tower', 1)")
                        info = p.page.evaluate(f"__story.cutAt('{cut}', {q}, 'room', 0)")
                        assert info["cut"] == cut and info["scene"] == "room"
                        got[q] = p.frame()
                    assert _same(got[0], leaving), (cut, int(np.abs(got[0] - leaving).max()))
                    assert _same(got[1], arriving), (cut, int(np.abs(got[1] - arriving).max()))
                    assert _changed(got[0.5], leaving) > 0.05 and _changed(got[0.5], arriving) > 0.05, cut
            assert p.messages == [], (name, p.messages)
        finally:
            p.close()
    for a, b in [("halftone", "grade"), ("halftone", "glyph"), ("grade", "glyph")]:
        assert _changed(pictures[a], pictures[b]) > 0.1, (a, b)


@pytest.mark.browser
def test_the_hook_rejects_what_does_not_exist(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        p.open()
        for call in ("__story.go('nowhere', 0)", "__story.go('tower', 2)", "__story.go('tower', -1)", "__story.go('tower', 0.5)",
                     "__story.at('nowhere', 0, 0.5, 1)", "__story.play('room', 7)", "__story.cutAt('dissolve', 0.5, 'room', 0)"):
            got = p.page.evaluate(f"Promise.race([{call}.then(() => 'resolved', (e) => 'rejected: ' + e.message),"
                                  " new Promise((r) => setTimeout(() => r('hung'), 3000))])")
            assert got.startswith("rejected: ") and len(got) < 80, (call, got)
        assert p.info()["scene"] == "title"  # nothing moved
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_notes_fullscreen_and_type_over_the_picture(browser, dist) -> None:
    p = Page(browser, dist)
    try:
        p.open()
        info = p.page.evaluate("__story.go('title', 0)")
        assert info["place"] == "over" and info["notes"].startswith("Wait for the beam"), info
        assert "Wait" not in p.caption() and not p.page.locator("#story aside[data-notes]").first.is_visible()
        # the headline lies in the picture's box, at most a third of its height, every line inside it
        pic = p.page.locator("#picture").bounding_box()
        head = p.page.locator("#story [data-beat].is-current").bounding_box()
        assert pic["x"] <= head["x"] and head["x"] + head["width"] <= pic["x"] + pic["width"]
        assert pic["y"] <= head["y"] and head["y"] + head["height"] <= pic["y"] + pic["height"]
        assert head["height"] <= pic["height"] / 3 + 1
        assert p.page.evaluate("(() => { const b = document.querySelector('#story [data-beat].is-current');"
                               " return b.scrollHeight <= b.clientHeight + 1; })()")
        p.page.keyboard.press("n")
        assert p.page.locator("#story [data-beat].is-current aside[data-notes]").is_visible()
        p.page.keyboard.press("n")
        assert p.page.locator("#story [data-beat].is-current aside[data-notes]").is_hidden()
        p.page.keyboard.press("f")  # fullscreen where the browser allows it, silently where not
        info = p.page.evaluate("__story.go('tower', 0)")
        assert info["place"] == "under" and info["notes"] == ""
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.fixture(scope="module")
def installed(tmp_path_factory) -> Path:
    """The starter with the offline crate built and installed by the plugin, named in props.js as the boat."""
    from antigravity3d.cli import main
    from antigravity3d.deliver import install_package

    root = tmp_path_factory.mktemp("film-prop")
    project = _project(root)
    shutil.copy(FIXTURE / "package.json", project / "package.json")
    out = root / "crate"

    def build_and_install():
        # the build runs its own event loop, which may not start in the thread where Playwright's is running
        assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "web_three", "--out", str(out)]) == 0
        (pkg,) = out.glob("runs/*/package")
        return install_package(pkg, project, compress="never", check=False)

    with ThreadPoolExecutor(1) as pool:
        r = pool.submit(build_and_install).result()
    assert r.status == "installed", r
    asset = json.loads((project / r.dest / ".agy3d" / "install.json").read_text())["asset"]
    props = project / "src" / "props.js"
    line = "  boat: { size: [1.3, 0.6, 3.2] },"
    assert line in props.read_text()
    props.write_text(props.read_text().replace(
        line, f"  boat: {{ size: [1.3, 0.6, 3.2], asset: '{asset}', module: () => import('./agy3d/{asset}/index.js') }},"))
    return _vite_build(project)


@pytest.mark.browser
def test_a_prop_installed_by_the_plugin_stands_in_the_shore(browser, installed, dist) -> None:
    p = Page(browser, installed)
    try:
        state = p.open()
        assert state["mode"] == "webgl" and state["info"]["props"] == {"boat": "installed"}, state["info"]
        with_crate = p.go("shore", 0)
        assert p.messages == [], p.messages
        assert not [u for u in p.requested if not u.startswith(ORIGIN)], p.requested
    finally:
        p.close()
    p = Page(browser, dist)
    try:
        assert p.open()["info"]["props"] == {"boat": "stand-in"}
        assert _changed(with_crate, p.go("shore", 0)) > 0.002  # the crate is not the stand-in boat
    finally:
        p.close()
