"""The stage of the story-slides-3d starter: one fluid three.js picture under a deck's slides.

Three things are held here (the contract, story/CONTRACT2.md, sections 2 to 5):

- **the deck the starter ships**, "How heavy is a cloud?": its page says what the contract's table says, its numbers
  follow from its premises and are the ones in ``facts.json`` (which the stage reads too), it boots clean, every beat
  draws a picture at a desktop and a phone size that differs from the beat before it or adds words, the same beat
  reached two ways gives the same pixels, and a frame stays under 80 draw calls;
- **themes**: a change of theme repaints what ``kit.paint`` made, re-lights what ``kit.rig`` made and switches the
  print's treatment; colours come from the page's CSS tokens, and a token the CSS leaves out from the built-in ones;
- **worlds, travel and the mechanisms**, on a small lab story this file lays over a copy of the starter: a world is
  built once; between two of its scenes the picture is not cut, the camera travels (at 0 the leaving scene's picture,
  at 1 the arriving scene's first state, between them a camera strictly between the two, one scene drawn per frame);
  and each of the six mechanisms is posed and gives its number back.

Built and served as in ``tests/test_story_slides_starter.py`` (whose helpers this file uses); the browser tests skip
without Playwright's browser or a workspace with Vite and three.js.
"""

from __future__ import annotations

import html
import io
import json
import math
import re
import time
from pathlib import Path

import numpy as np
import pytest
from test_story_slides_starter import (  # noqa: F401  (browser is a fixture)
    DESKTOP,
    PHONE,
    SMALL,
    STARTER,
    Page,
    _beats,
    _changed,
    _html_scenes,
    _project,
    _same,
    _shows_something,
    _vite_build,
    browser,
)

THEMES = ["paper", "ink", "poster", "halftone", "glyph"]
MECHANISMS = ["field", "frame", "ruler", "stack", "streams", "bars"]
# The deck as the contract's table gives it: scene, layout, heading (its tag and whether it is a kicker), and the
# words of each beat.
DECK = [
    ("cloud", "title", ("h1", False, "How heavy is a cloud?"), [["A short talk with one cloud in it."]]),
    ("looks", "statement", ("h2", True, "What it looks like"),
     [["It looks like nothing at all."], ["Take an ordinary one: a kilometre long, wide and tall."]]),
    ("drops", "statement", ("h2", True, "What it is made of"),
     [["It is water. Drops a fiftieth of a millimetre across."], ["Half a gram of them in every cubic metre."],
      ["A hundred thousand million million drops in all."]]),
    ("weight", "number", ("h2", True, "What it weighs"),
     [["Pour them together."], ["A cube of water eight metres on a side."], ["500 tonnes", "1 km3 x 0.5 g per m3"]]),
    ("floats", "statement", ("h2", True, "Why it stays up"),
     [["So why is it up there?"], ["Each drop falls about a centimetre a second."],
      ["The air under the cloud rises a hundred times faster."]]),
    ("up", "end", ("h2", False, "Look up."), [["Five hundred tonnes, held up by a breeze."]]),
]
# What the deck's worded numbers say, each in its entry's unit in facts.json.
SAID = {
    "a kilometre long, wide and tall": 1000, "Half a gram": 0.5, "a fiftieth of a millimetre": 1 / 50,
    "Five hundred tonnes": 500, "eight metres": 8, "A hundred thousand million million": 1e5 * 1e6 * 1e6,
    "about a centimetre a second": 1, "a hundred times": 100,
}


def _text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", markup)).split())


def _sections(page: Path) -> list[dict]:
    """Each section of a deck's page: its id, its layout, its heading and each beat's lines (notes left out)."""
    out = []
    # a section may carry more than its id and layout (class="scrim" asks for a label under its words)
    for m in re.finditer(r'<section data-scene="([a-z0-9-]+)"(?: data-layout="(\w+)")?(?: class="[\w -]+")?>(.*?)</section>', page.read_text(), re.S):
        head = re.search(r'<(h[12])( class="kicker")?>(.*?)</h[12]>', m.group(3), re.S)
        beats = []
        for b in re.finditer(r"<div data-beat>(.*?)</div>", m.group(3), re.S):
            beats.append([_text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", b.group(1), re.S)])
        out.append({"id": m.group(1), "layout": m.group(2), "head": (head.group(1), bool(head.group(2)), _text(head.group(3))),
                    "beats": beats, "markup": m.group(3)})
    return out


def test_the_starter_ships_the_cloud_deck_as_the_contract_gives_it() -> None:
    names = {p.relative_to(STARTER).as_posix() for p in STARTER.rglob("*") if p.is_file()}
    ids = [d[0] for d in DECK]
    mine = {"index.html", "facts.json", "src/film.js", "src/kit.js", "src/theme.js", "src/print.js", "src/cuts.js",
            "src/props.js", "src/story.js", "src/worlds/sky.js", *(f"src/mechanisms/{m}.js" for m in MECHANISMS),
            *(f"src/scenes/{i}.js" for i in ids)}
    assert mine <= names, sorted(mine - names)
    # nothing else in the folders that are the stage's own
    assert {n for n in names if n.startswith(("src/scenes/", "src/worlds/", "src/mechanisms/"))} <= mine
    page = (STARTER / "index.html").read_text()
    assert '<html lang="en" data-story-theme="paper">' in page
    sections = _sections(STARTER / "index.html")
    assert [(s["id"], s["layout"], s["head"], s["beats"]) for s in sections] == [d for d in DECK]
    assert sum(len(s["beats"]) for s in sections) == 13
    assert 'class="lede"' in sections[0]["markup"] and 'class="big"' in sections[3]["markup"] and 'class="source"' in sections[3]["markup"]
    noted = [s["id"] for s in sections if "<aside data-notes>" in s["markup"]]
    assert {"cloud", "weight"} <= set(noted), noted  # speaker's notes on at least the first and the fourth scene
    story = (STARTER / "src" / "story.js").read_text()
    assert "form: 'deck'" in story and "worlds: { sky }" in story
    assert re.search(r"scenes: \[(.*)\]", story).group(1).split(", ") == ids
    assert "treatment:" not in story.split("export const film")[1], "the deck's look is the theme's"
    for scene, section in zip(ids, sections):
        src = (STARTER / "src" / "scenes" / f"{scene}.js").read_text()
        assert f"id: '{scene}'" in src and "world: 'sky'" in src, scene  # one world: every change of scene is a travel
        assert "enterWith" not in src, scene
        assert len(re.findall(r"\{ dur: [\d.]+, hold: [\d.]+ \}", src)) == len(section["beats"]), scene
    for path in [STARTER / "src" / "worlds" / "sky.js", *(STARTER / "src" / "scenes").glob("*.js"),
                 *(STARTER / "src" / "mechanisms").glob("*.js")]:
        src = path.read_text()
        assert "Math.random" not in src and "Date.now" not in src and "performance.now" not in src, path.name
        # the deck is painted and lit by the theme only, so it can be shown in any theme
        assert not re.search(r"kit\.(mat|spot)\(|#[0-9a-fA-F]{6}\b|new THREE\.\w*Light\(", src), path.name
    # the house is a prop with a stand-in
    assert re.search(r"house: \{ size: \[", (STARTER / "src" / "props.js").read_text())
    assert "kit.prop('house', { standIn:" in (STARTER / "src" / "worlds" / "sky.js").read_text()


def test_each_mechanism_is_one_small_file_that_says_what_claim_it_is_for() -> None:
    for name in MECHANISMS:
        src = (STARTER / "src" / "mechanisms" / f"{name}.js").read_text()
        head = " ".join(line.lstrip("/ ") for line in src.split("import ", 1)[0].splitlines())
        assert head.startswith(f"{name}.js: for ") and "the claim is" in head, name
        assert re.search(rf"export function {name}\(kit, ", src), name
        assert len(src.splitlines()) <= 90, f"{name}.js is {len(src.splitlines())} lines"
        # one draw for the many: instances, not a mesh apiece
        assert "InstancedMesh" in src, name


def test_the_decks_numbers_follow_from_its_premises_and_the_page_shows_them() -> None:
    facts = json.loads((STARTER / "facts.json").read_text())
    p, d = facts["premises"], facts["derived"]
    side, water, drop, rise = (p[k]["value"] for k in ("side", "water", "drop", "rise"))
    assert d["volume"]["value"] == side ** 3
    assert d["tonnes"]["value"] == pytest.approx(side ** 3 * water / 1e6)          # grams to tonnes
    assert d["liquid"]["value"] == d["tonnes"]["value"]                             # a tonne of water is a cubic metre
    assert d["cube"]["value"] == pytest.approx(d["liquid"]["value"] ** (1 / 3), abs=0.005)
    r = drop / 2 / 1000                                                             # the drop's radius, metres
    assert d["dropVolume"]["value"] == pytest.approx(4 / 3 * math.pi * r ** 3, rel=0.005)
    assert d["drops"]["value"] == pytest.approx(d["liquid"]["value"] / d["dropVolume"]["value"], rel=0.005)
    stokes = 2 * r ** 2 * 9.81 * 1000 / (9 * 1.8e-5) * 100                          # centimetres a second
    assert d["fall"]["value"] == pytest.approx(stokes, abs=0.05)
    assert d["times"]["value"] == pytest.approx(rise * 100 / d["fall"]["value"], abs=0.5)
    # the words say the numbers, to the roundness words have
    said = [e for e in (*p.values(), *d.values()) if "said" in e]
    assert sorted(e["said"] for e in said) == sorted(SAID)
    for e in said:
        assert e["value"] == pytest.approx(SAID[e["said"]], rel=0.25), e["said"]

    # the page: every number names its source, and shows what the source holds
    page = (STARTER / "index.html").read_text()
    article = page.split('<article id="story"', 1)[1].split("</article>", 1)[0]
    sourced = re.findall(r'<(\w+)[^>]*data-src="facts\.json#(/[\w/]+)"[^>]*>(.*?)</\1>', article, re.S)
    assert len(sourced) == 10, sourced
    for _tag, pointer, shown in sourced:
        value = facts
        for key in pointer.strip("/").split("/"):
            value = value[key]
        shown = _text(shown)
        if isinstance(value, str):
            assert shown == value, (pointer, shown)
        else:
            assert [float(n) for n in re.findall(r"\d+(?:\.\d+)?", shown)] == [float(value)], (pointer, shown)
    bare = re.sub(r"<aside data-notes>.*?</aside>", "", re.sub(r'<(\w+)[^>]*data-src=[^>]*>.*?</\1>', "", article, flags=re.S), flags=re.S)
    assert not re.search(r"\d", _text(bare)), "a number in the deck's words that names no source"
    # and the stage takes the same numbers from the same file
    sky = (STARTER / "src" / "worlds" / "sky.js").read_text()
    assert "import facts from '../../facts.json'" in sky
    for used in ("premises.side.value", "premises.water.value", "premises.rise.value", "derived.cube.value",
                 "derived.fall.value", "derived.drops.value"):
        assert f"facts.{used}" in sky, used


# --------------------------------------------------------------------------- in a browser


@pytest.fixture(scope="module")
def deck(tmp_path_factory) -> Path:
    """The starter exactly as shipped, built with the workspace's Vite."""
    return _vite_build(_project(tmp_path_factory.mktemp("deck")))


# Only the canvas shows: whatever a page lays over its picture (words, a count, a progress line, controls) is not the
# stage's, and must not get into a comparison of pictures.
ONLY_THE_PICTURE = "body * { visibility: hidden !important } #stage { visibility: visible !important }"


def _frame(p: Page) -> np.ndarray:
    """The canvas as an RGB array, nothing over it."""
    from PIL import Image

    tag = p.page.add_style_tag(content=ONLY_THE_PICTURE)
    png = p.page.locator("#stage").screenshot()
    tag.evaluate("(e) => e.remove()")
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int16)


def _differs(a: np.ndarray, b: np.ndarray) -> float:
    """check_site's measure of two pictures (``picture_change``): the share of the pixels that differ by more than
    8 % of full scale in any channel. Under SAME they are the same picture to it."""
    return float((np.abs(a - b) > 0.08 * 255).any(axis=2).mean())


SAME = 0.0005


def _go(p: Page, scene: str, beat: int) -> tuple[np.ndarray, dict]:
    info = p.page.evaluate(f"__story.go('{scene}', {beat})")
    return _frame(p), info


def _theme(p: Page, name: str) -> dict:
    """Sets the page's theme as a page does (the attribute on <html>); the stage follows. Returns info() after a draw."""
    p.page.evaluate(f"document.documentElement.dataset.storyTheme = '{name}'")
    p.page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    return p.info()


@pytest.mark.browser
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_the_deck_boots_and_every_beat_draws_a_picture_that_changes_or_adds_words(browser, deck, viewport) -> None:
    p = Page(browser, deck, viewport=viewport)
    try:
        t0 = time.monotonic()
        state = p.open()
        ready = time.monotonic() - t0
        assert state["mode"] == "webgl", state
        assert [(s["id"], s["beats"]) for s in state["scenes"]] == [(d[0], len(d[3])) for d in DECK]
        info = state["info"]
        assert (info["scene"], info["beat"], info["form"], info["theme"], info["layout"]) == ("cloud", 0, "deck", "paper", "title"), info
        assert info["treatment"] == "grade" and info["props"] == {"house": "stand-in"}, info
        frames, infos = {}, {}
        for scene, beat in _beats(p):
            frames[scene, beat], infos[scene, beat] = _go(p, scene, beat)
        layouts = {scene: infos[scene, 0]["layout"] for scene, _ in frames}
        assert layouts == {d[0]: d[1] for d in DECK}, layouts
        shown = {k: round(_shows_something(f), 3) for k, f in frames.items()}
        assert min(shown.values()) > 0.03, shown  # check_site's floor is 1 %
        order = list(frames)
        for before, after in zip(order, order[1:]):
            change = _differs(frames[before], frames[after])
            if after[1] == 0:  # a new scene: the camera has travelled somewhere else
                assert change > 0.02, f"{after[0]} opens on the picture {before[0]} ended on ({change:.1%})"
            else:  # a beat: it changes the picture, or (a slide over a held picture) it adds words
                words = infos[after]["lines"] and infos[after]["lines"] != infos[before]["lines"]
                assert change > SAME or words, f"{after} changes {change:.2%} of the picture and adds no words"
        held = [k for k in order if k[1] and _differs(frames[k[0], k[1] - 1], frames[k]) <= SAME]
        assert held == [("weight", 2)], held  # the one beat the contract holds the picture for: the number
        calls = {k: i["calls"] for k, i in infos.items()}
        assert max(calls.values()) < 80, calls
        assert not [u for u in p.requested if not u.startswith("http://film.test")], p.requested
        assert p.messages == [], p.messages
        print(f"ready in {ready:.1f} s; shows {shown}; draw calls {calls}; triangles "
              f"{ {k: i['triangles'] for k, i in infos.items()} }")
    finally:
        p.close()


@pytest.mark.browser
def test_the_same_beat_reached_two_ways_gives_the_same_pixels(browser, deck) -> None:
    p = Page(browser, deck)
    try:
        p.open()
        first = {(s, b): _go(p, s, b)[0] for s, b in _beats(p)}
        # backwards through the whole deck, so every beat follows a different one from the first time
        again = {(s, b): _go(p, s, b)[0] for s, b in reversed(_beats(p))}
        worst = {k: int(np.abs(first[k] - again[k]).max()) for k in first}
        assert all(_same(first[k], again[k]) for k in first), worst
        # after playing through a travel into it, and after a theme and back
        for scene, beat in [("looks", 0), ("weight", 1), ("floats", 0)]:
            before = _beats(p)[_beats(p).index((scene, beat)) - 1]
            p.page.evaluate(f"__story.go('{before[0]}', {before[1]})")
            p.page.evaluate("__story.advance()")
            p.page.wait_for_timeout(250)
            assert _same(first[scene, beat], _go(p, scene, beat)[0]), (scene, beat)
        assert _theme(p, "ink")["theme"] == "ink"
        assert _theme(p, "paper")["theme"] == "paper"
        for key in [("cloud", 0), ("drops", 1), ("weight", 2)]:
            assert _same(first[key], _go(p, *key)[0]), key
        # a settled beat is held: the deck's scenes are ambient, but a beat shown with go() stands still
        frames = p.info()["frames"]
        p.page.wait_for_timeout(500)
        assert p.info()["frames"] == frames
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_deck_reads_in_every_theme_and_its_numbers_are_the_pages(browser, deck) -> None:
    p = Page(browser, deck)
    try:
        p.open()
        facts = json.loads((STARTER / "facts.json").read_text())
        shown = p.page.evaluate("[...document.querySelectorAll('#story [data-src]')].map((e) => [e.dataset.src, e.textContent.replace(/\\s+/g, ' ').trim()])")
        assert len(shown) == 10
        for src, text in shown:
            value = facts
            for key in src.split("#/")[1].split("/"):
                value = value[key]
            assert (text == value) if isinstance(value, str) else (str(value) in text), (src, text)
        pictures = {}
        for theme, treatment in zip(THEMES, ["grade", "grade", "poster", "halftone", "glyph"]):
            info = _theme(p, theme)
            assert (info["theme"], info["treatment"]) == (theme, treatment), info
            pictures[theme] = {key: _go(p, *key)[0] for key in [("cloud", 0), ("weight", 1), ("drops", 0)]}
            for key, frame in pictures[theme].items():
                assert _shows_something(frame) > 0.01, (theme, key)  # check_site's floor
            assert max(p.page.evaluate(f"__story.go('{s}', {b})")["calls"] for s, b in _beats(p)) < 80, theme
        for a, b in [(a, b) for i, a in enumerate(THEMES) for b in THEMES[i + 1:]]:
            assert _changed(pictures[a]["weight", 1], pictures[b]["weight", 1]) > 0.2, (a, b)
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_the_deck_travels_between_its_scenes_and_stands_still_under_reduced_motion(browser, deck) -> None:
    p = Page(browser, deck)
    try:
        p.open(query="?pace=6")
        p.page.evaluate("__story.go('cloud', 0)")
        # what runs between beats, noted frame by frame in the page (a travel at this pace is a quarter of a second)
        p.page.evaluate("""(() => { window.__ran = []; let last = null;
          const watch = () => { const i = __story.info(), now = i.cut && i.cut + ' into ' + i.scene;
            if (now && now !== last) window.__ran.push(now); last = now; requestAnimationFrame(watch); };
          watch(); })()""")
        for _ in range(len(_beats(p)) - 1):
            p.page.keyboard.press("ArrowRight")
            p.page.wait_for_function("() => !__story.info().cut")
            p.page.wait_for_timeout(60)
        assert (p.info()["scene"], p.info()["beat"]) == ("up", 0)
        # every change of scene in the one world is a travel, and nothing runs between two beats of a scene
        assert p.page.evaluate("window.__ran") == [f"travel into {d[0]}" for d in DECK[1:]]
        p.page.keyboard.press("ArrowLeft")
        p.page.wait_for_function("() => !__story.info().cut")
        p.page.wait_for_timeout(60)
        assert (p.info()["scene"], p.info()["beat"]) == ("floats", 2)
        assert p.page.evaluate("window.__ran")[-1] == "travel into floats"  # and so is the way back
        assert p.messages == [], p.messages
    finally:
        p.close()
    p = Page(browser, deck, reduced_motion="reduce")
    try:
        p.open()
        p.page.keyboard.press("ArrowRight")
        info = p.info()
        assert (info["scene"], info["cut"], info["playing"]) == ("looks", None, False), info
        a = _frame(p)
        p.page.wait_for_timeout(700)
        assert _same(a, _frame(p))
        assert p.messages == [], p.messages
    finally:
        p.close()


# --------------------------------------------------------------------------- the lab: a small story of this file's own

LAB_PAGE = """
    <section data-scene="a"><h2>A</h2><div data-beat><p>The first place in the world.</p></div></section>
    <section data-scene="b"><h2>B</h2><div data-beat><p>The second place.</p></div><div data-beat><p>A step in it.</p></div></section>
    <section data-scene="m"><h2>Bench</h2><div data-beat><p>Six mechanisms on a bench.</p></div></section>
    <section data-scene="c"><h2>C</h2><div data-beat><p>The world again, entered through a cut.</p></div></section>
"""
LAB = {
    "src/story.js": """
import * as bench from './worlds/bench.js';
import a from './scenes/a.js';
import b from './scenes/b.js';
import m from './scenes/m.js';
import c from './scenes/c.js';
export const film = { title: 'Lab', form: 'deck', worlds: { bench }, scenes: [a, b, m, c] };
""",
    # A world: a plain, a block painted `lit`, a marker painted `accent`, the theme's light. Built once.
    "src/worlds/bench.js": """
import * as THREE from 'three';
export async function create(kit) {
  const lab = (window.__lab ??= { worlds: 0 });
  lab.worlds++;
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(40, 1, 0.5, 300);
  const rig = kit.rig(scene, { shadow: 1024, reach: 40 });
  const lit = kit.paint('lit'), accent = kit.paint('accent'), wire = kit.paint('ink', { flat: true });
  const block = kit.box(2, 2, 2, lit, -2, 1, 0), marker = kit.box(1, 1, 1, accent, 2, 0.5, 0), post = kit.box(0.1, 3, 0.1, wire, 0, 1.5, -2);
  scene.add(rig.plain(300), block, marker, post);
  Object.assign(lab, { rig, lit, accent, wire, marker, camera });
  return { scene, camera, marker, rest() { marker.position.set(2, 0.5, 0); } };
}
""",
    "src/scenes/a.js": """
export default {
  id: 'a', world: 'bench', beats: [{ dur: 1, hold: 1 }],
  create(kit, w) {
    return { update(s) {
      w.camera.fov = 40; w.camera.position.set(0, 3, 12); w.camera.lookAt(0, 1, 0);
      window.__lab.a = { enter: s.enter, exit: s.exit };
    } };
  },
};
""",
    # It moves the world's marker from where the last scene left it (the world's rest) with s.enter, and lifts it
    # with its first beat.
    "src/scenes/b.js": """
export default {
  id: 'b', world: 'bench', travel: 0.9, beats: [{ dur: 1, hold: 1 }, { dur: 1, hold: 1 }],
  create(kit, w) {
    return { update(s) {
      w.camera.fov = 28; w.camera.position.set(8, 6, 5); w.camera.lookAt(1, 0.5, 0);
      w.marker.position.x = 2 + 3 * s.enter - 2 * s.p[1];
      w.marker.position.y = 0.5 + s.p[0];
      window.__lab.b = { enter: s.enter, p: s.p[0], t: s.t };
    } };
  },
};
""",
    "src/scenes/c.js": """
export default {
  id: 'c', world: 'bench', enterWith: 'iris', beats: [{ dur: 1, hold: 1 }],
  create(kit, w) {
    return { update() { w.camera.fov = 50; w.camera.position.set(0, 16, 0.01); w.camera.lookAt(0, 0, 0); } };
  },
};
""",
    # A set of its own (no world): the six mechanisms, each posed by the beat's progress or the scene's time.
    "src/scenes/m.js": """
import * as THREE from 'three';
import { field } from '../mechanisms/field.js';
import { frame } from '../mechanisms/frame.js';
import { ruler } from '../mechanisms/ruler.js';
import { stack } from '../mechanisms/stack.js';
import { streams } from '../mechanisms/streams.js';
import { bars } from '../mechanisms/bars.js';
export default {
  id: 'm', ambient: true, beats: [{ dur: 2, hold: 1 }],
  create(kit) {
    const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
    const rig = kit.rig(scene, { shadow: 1024, reach: 20 });
    const f = field(kit, { count: 200, seed: 3, shape: 'dot' });
    f.layout('rows', (i, o) => { o.x = -5 + (i % 20) * 0.5; o.y = 0.2 + Math.floor(i / 20) * 0.3; o.z = 0; o.size = 0.2; });
    f.layout('pile', (i, o, r) => { o.x = r() - 0.5; o.y = 4 + r(); o.z = r() - 0.5; o.size = 0.1; });
    const fr = frame(kit, { size: [2, 3, 1], thickness: 0.05 }), ru = ruler(kit, { length: 8, step: 1 });
    const st = stack(kit, { parts: 10, marked: 3, size: [1, 4, 1] });
    const sm = streams(kit, [
      { speed: 0.5, direction: [0, 1, 0], length: 4, count: 12, at: [-6, 2, -2] },
      { speed: 2, direction: [1, 0, 0], length: 4, count: 12, streak: 4, at: [6, 2, -2], paint: 'accent-2' },
    ]);
    const ba = bars(kit, { values: [1, 2, 4, 3], height: 2, marked: [2] });
    fr.mesh.position.set(-4, 0, -3); ru.mesh.position.set(4, 0, -3); st.group.position.set(0, 0, -3); ba.group.position.set(-1, 0, 2);
    scene.add(rig.plain(100), f.mesh, fr.mesh, ru.mesh, st.group, sm.group, ba.group);
    window.__lab.m = { f, fr, ru, st, sm, ba };
    return { scene, camera, update(s) {
      camera.position.set(0, 5, 14); camera.lookAt(0, 2, 0);
      f.show(s.p[0]); f.morph('rows', 'pile', s.p[0]);
      window.__lab.grown = Array.from(fr.grow(s.p[0]));
      ru.grow(s.p[0]); st.grow(s.p[0]); ba.grow(s.p[0]); sm.flow(s.t);
    } };
  },
};
""",
}


@pytest.fixture(scope="module")
def lab(tmp_path_factory) -> Path:
    """The starter with this file's lab story laid over it: a world of two scenes (and a third entered by a cut), and
    a bench of the six mechanisms. The engine, the kit and the page's script are the starter's own."""
    project = _project(tmp_path_factory.mktemp("lab"))
    page = project / "index.html"
    head, rest = page.read_text().split('<article id="story"', 1)
    page.write_text(head + '<article id="story" aria-live="polite">' + LAB_PAGE + "  </article>" + rest.split("</article>", 1)[1])
    for name, source in LAB.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(source)
    return _vite_build(project)


def _lab(p: Page, expression: str):
    return p.page.evaluate(f"(() => {{ const lab = window.__lab; return {expression}; }})()")


def _rgb(colour: str) -> np.ndarray:
    return np.array([int(colour[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)


def _is(frame: np.ndarray, colour: str, tol: int = 3) -> float:
    """The share of a picture's pixels that are the colour 'rrggbb'."""
    return float((np.abs(frame - _rgb(colour)).max(axis=2) <= tol).mean())


def _inked(frame: np.ndarray, paper: str, ink: str) -> tuple[np.ndarray, np.ndarray]:
    """Each pixel of a picture against one ink on a paper: how far it is from being a mix of the two (in levels), and
    how much of the ink that mix has (0 the paper .. 1 the ink)."""
    px, p = frame.reshape(-1, 3).astype(np.float64), _rgb(paper)
    d = _rgb(ink) - p
    t = np.clip(((px - p) @ d) / (d @ d), 0, 1)
    return np.abs(px - (p + t[:, None] * d)).max(axis=1), t


def _off_inks(frame: np.ndarray, paper: str, inks: list[str], tol: int = 14) -> float:
    """The share of a picture's pixels that are not the paper, one of the inks, or a mix of the paper and one ink."""
    return float((np.minimum.reduce([_inked(frame, paper, ink)[0] for ink in inks]) > tol).mean())


def _in_ink(frame: np.ndarray, paper: str, ink: str, other: str, tol: int = 14) -> float:
    """The share of a picture's pixels that are printed in `ink` (more than half of it) and not in `other`."""
    off, t = _inked(frame, paper, ink)
    return float(((off <= tol) & (t > 0.5) & (_inked(frame, paper, other)[0] > tol)).mean())


@pytest.mark.browser
def test_a_world_is_built_once_and_the_camera_travels_between_its_scenes(browser, lab) -> None:
    p = Page(browser, lab)
    try:
        state = p.open()
        assert state["mode"] == "webgl" and [s["id"] for s in state["scenes"]] == ["a", "b", "m", "c"], state
        assert _lab(p, "lab.worlds") == 1  # three scenes name it
        leaving, at_a = _go(p, "a", 0)
        arriving, at_b = _go(p, "b", 0)
        assert _lab(p, "lab.b.enter") == 1 and _lab(p, "lab.marker.position.x") == 5  # settled: the entrance is over
        assert at_a["shots"] == 1 and at_b["shots"] == 1 and at_a["cut"] is None
        a, b = at_a["camera"], at_b["camera"]
        assert a["position"] == [0, 3, 12] and a["fov"] == 40 and b["position"] == [8, 6, 5] and b["fov"] == 28
        # where a travel arrives: the scene's first state (its first beat has not begun: the marker is not lifted)
        p.page.evaluate("__story.at('b', 0, 0, 0)")
        first = _frame(p)
        assert _changed(first, arriving) > 0.001

        got = {}
        for q in (0, 0.25, 0.5, 0.75, 1):
            p.page.evaluate("__story.go('a', 0)")
            info = p.page.evaluate(f"__story.cutAt('travel', {q}, 'b', 0)")
            assert info["cut"] == "travel" and info["scene"] == "b", info
            # one picture: the world drawn once, with the camera between the two scenes' cameras
            assert info["shots"] == 1, (q, info["shots"])
            got[q] = (_frame(p), info["camera"], _lab(p, "[lab.marker.position.x, lab.marker.position.y]"))
        assert _same(got[0][0], leaving), int(np.abs(got[0][0] - leaving).max())
        assert _same(got[1][0], first), int(np.abs(got[1][0] - first).max())
        gone = []
        for q in (0.25, 0.5, 0.75):
            frame, camera, marker = got[q]
            for axis in range(3):
                lo, hi = sorted((a["position"][axis], b["position"][axis]))
                assert lo < camera["position"][axis] < hi, (q, camera)
            assert b["fov"] < camera["fov"] < a["fov"], (q, camera)
            assert _changed(frame, leaving) > 0.02 and _changed(frame, first) > 0.02, q
            # the arriving scene poses the world at its first state, with s.enter the travel's progress
            assert marker == pytest.approx([2 + 3 * q, 0.5]), (q, marker)
            gone.append(math.dist(camera["position"], a["position"]) / math.dist(b["position"], a["position"]))
        assert gone == sorted(gone) and gone[1] == pytest.approx(0.5, abs=0.01), gone  # eased: halfway at half time
        # the camera goes straight from one place to the other
        mid = got[0.5][1]["position"]
        assert mid == pytest.approx([4, 4.5, 8.5], abs=0.01), mid

        # a cut draws both scenes; a travel between two sets that are not one world is refused
        p.page.evaluate("__story.go('a', 0)")
        assert p.page.evaluate("__story.cutAt('fade', 0.5, 'b', 0)")["shots"] == 2
        p.page.evaluate("__story.go('m', 0)")
        refused = p.page.evaluate("__story.cutAt('travel', 0.5, 'b', 0).then(() => 'resolved', (e) => e.message)")
        assert "not one world" in refused, refused
        assert p.info()["scene"] == "m"
        # from a scene to a later one of the same world, whatever lies between
        p.page.evaluate("__story.go('b', 1)")
        assert p.page.evaluate("__story.cutAt('travel', 0.5, 'c', 0)")["shots"] == 1
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_a_travel_is_the_default_entrance_within_a_world_and_a_named_cut_still_cuts(browser, lab) -> None:
    p = Page(browser, lab)
    try:
        p.open()
        p.page.evaluate("__story.at('a', 0, 1, 5)")  # scene a, five seconds into the world's time
        t0 = time.monotonic()
        p.page.evaluate("__story.advance()")
        first = p.info()
        assert (first["scene"], first["cut"], first["playing"]) == ("b", "travel", True), first
        # on its way: read in one go, in the page, so that the travel cannot end between two questions
        way = p.page.evaluate("""new Promise((resolve) => {
          const look = () => { const b = window.__lab.b, i = __story.info();
            if (b && b.enter > 0.15 && b.enter < 1 && i.cut) resolve({ enter: b.enter, p: b.p, t: b.t, cut: i.cut, shots: i.shots });
            else requestAnimationFrame(look); };
          look(); })""")
        # the travel is the entrance: the beat has not begun (its progress is 0) while the camera is on its way; one
        # scene is drawn per frame; and the world's time carries on from scene to scene
        assert way["cut"] == "travel" and way["p"] == 0 and way["shots"] == 1 and 5 < way["t"] < 6.5, way
        p.page.wait_for_function("() => !__story.info().cut")
        took = time.monotonic() - t0
        assert 0.7 <= took <= 3, took  # scene b says travel: 0.9
        after_a_frame = "new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(__story.info()))))"
        p.page.wait_for_function("() => !__story.info().playing")
        p.page.evaluate(after_a_frame)
        # the beat's last frame is its end, exactly (scene b is not ambient: nothing is drawn after it)
        assert _lab(p, "lab.b.p") == 1 and _lab(p, "lab.b.enter") == 1
        # going back is a quick travel to the scene as it was left
        p.page.evaluate("__story.back()")
        back = p.info()
        assert (back["scene"], back["cut"], back["playing"]) == ("a", "travel", False), back
        p.page.wait_for_function("() => !__story.info().cut", timeout=5000)
        # a set of its own is entered by a cut; so is a scene of the world that names one
        p.page.evaluate("__story.go('b', 1)")
        p.page.evaluate("__story.advance()")
        info = p.page.evaluate(after_a_frame)
        assert (info["scene"], info["cut"], info["shots"]) == ("m", "fade", 2), info
        p.page.wait_for_function("() => !__story.info().cut", timeout=5000)
        p.page.evaluate("__story.advance()")
        info = p.page.evaluate(after_a_frame)
        assert (info["scene"], info["cut"], info["shots"]) == ("c", "iris", 2), info
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_a_theme_change_repaints_relights_and_switches_the_treatment(browser, lab) -> None:
    p = Page(browser, lab)
    paints = "({ lit: lab.lit.color.getHexString(), accent: lab.accent.color.getHexString(), wire: lab.wire.color.getHexString(), rough: lab.lit.roughness })"
    light = ("({ key: lab.rig.key.color.getHexString(), power: lab.rig.key.intensity, fill: lab.rig.fill.color.getHexString(),"
             " soft: lab.rig.key.shadow.radius, air: lab.rig.fog.color.getHexString(), thick: lab.rig.fog.density,"
             " sky: lab.rig.sky.material.uniforms.top.value.getHexString(), from: lab.rig.direction.toArray() })")
    try:
        p.open()
        assert p.page.evaluate("document.documentElement.dataset.storyTheme") == "paper"
        seen = {}
        for theme, treatment in zip(THEMES, ["grade", "grade", "poster", "halftone", "glyph"]):
            info = _theme(p, theme)
            assert (info["theme"], info["treatment"]) == (theme, treatment), info
            frame, _ = _go(p, "a", 0)
            seen[theme] = {"paints": _lab(p, paints), "light": _lab(p, light), "frame": frame}
        for i, a in enumerate(THEMES):
            for b in THEMES[i + 1:]:
                assert seen[a]["paints"] != seen[b]["paints"], (a, b)  # the paints took the theme's colours
                assert {k: seen[a]["light"][k] for k in ("key", "fill", "air", "sky")} != {k: seen[b]["light"][k] for k in ("key", "fill", "air", "sky")}, (a, b)
                assert seen[a]["light"]["from"] != seen[b]["light"]["from"], (a, b)  # this rig leaves the light where the theme puts it
                assert _changed(seen[a]["frame"], seen[b]["frame"]) > 0.1, (a, b)
        # the prints. A poster is flat colours: a handful of them cover the picture, and a painted surface is exactly
        # its paint where the light falls on it (the block is `lit`, the marker `accent`).
        flat = seen["poster"]["frame"]
        counts = np.unique(flat.reshape(-1, 3), axis=0, return_counts=True)[1]
        assert np.sort(counts)[-10:].sum() > 0.93 * counts.sum(), np.sort(counts)[-12:]
        assert _is(flat, seen["poster"]["paints"]["lit"]) > 0.005 and _is(flat, seen["poster"]["paints"]["accent"]) > 0.001
        # A print of dots or of characters has two inks: every pixel is the paper, the theme's ink or the accent's
        # (or between the paper and one of them, at a dot's edge), and what is painted in the accent is in the accent's.
        for theme in ("halftone", "glyph"):
            paper, ink, accent = seen[theme]["light"]["sky"], seen[theme]["paints"]["wire"], seen[theme]["paints"]["accent"]
            frame = seen[theme]["frame"]
            assert _off_inks(frame, paper, [ink, accent]) < 0.01, theme
            assert _in_ink(frame, paper, accent, ink) > 0.0005, theme  # the marker, in the second ink
            assert _in_ink(frame, paper, ink, accent) > 0.003, theme   # and the rest in the first
        # the numbers a theme carries: a poster's shadows are hard and its air is clear
        assert seen["poster"]["light"]["soft"] == 0 and seen["poster"]["light"]["thick"] == 0
        assert seen["paper"]["light"]["soft"] > 1 and seen["paper"]["light"]["thick"] > 0
        assert seen["poster"]["paints"]["rough"] == 1 and seen["ink"]["paints"]["rough"] < 1
        # the key and the fill are made from the lit colour and its shade: together they are white light
        for theme in THEMES:
            key, fill = (int(seen[theme]["light"][k], 16) for k in ("key", "fill"))
            assert key != 0xFFFFFF and fill != 0, theme

        # a name that is no theme is the default one; back in a theme the picture is the picture it was
        assert _theme(p, "nonsense")["theme"] == "paper"
        assert _theme(p, "ink")["theme"] == "ink"
        assert _same(_go(p, "a", 0)[0], seen["ink"]["frame"])

        # the colours are the page's: a token the CSS gives is taken, in whatever notation; one it leaves out stays
        before = _lab(p, paints)
        p.page.add_style_tag(content='html[data-story-theme="ink"] { --lit: rgb(18, 52, 86); --accent: hsl(120 100% 25%); --paper-2: #445566; }'
                                     ' html[data-story-theme="poster"] { --lit: #abcdef; --shade: #123123; }')
        assert _theme(p, "paper")["theme"] == "paper"
        assert _theme(p, "ink")["theme"] == "ink"
        after = _lab(p, paints)
        assert after["lit"] == "123456" and after["accent"] == "008000", after
        assert after["wire"] == before["wire"]  # --ink was not touched
        assert _changed(_go(p, "a", 0)[0], seen["ink"]["frame"]) > 0.1
        assert _theme(p, "poster")["theme"] == "poster" and _lab(p, paints)["lit"] == "abcdef"
        assert p.messages == [], p.messages
    finally:
        p.close()


@pytest.mark.browser
def test_each_mechanism_is_posed_and_gives_its_number_back(browser, lab) -> None:
    p = Page(browser, lab)
    numbers = """({
      shown: lab.m.f.mesh.count, first: lab.m.f.at(0).toArray(), last: lab.m.f.at(199).toArray(), size: lab.m.f.sizeOf(0),
      grown: lab.grown, upright: lab.m.fr.mesh.instanceMatrix.array[8 * 16 + 5], frame: lab.m.fr.size,
      reached: lab.m.ru.reached, ticks: lab.m.ru.shown, of: lab.m.ru.ticks,
      raised: lab.m.st.raised, marked: lab.m.st.group.children[0].count, plain: lab.m.st.group.children[1].count,
      slow: lab.m.sm.each[0].at(0).toArray(), fast: lab.m.sm.each[1].at(0).toArray(), gone: lab.m.sm.each.map((s) => s.gone(1)),
      tops: Array.from(lab.m.ba.tops), heights: lab.m.ba.heights, draws: [lab.m.f.mesh, lab.m.fr.mesh, lab.m.ru.mesh].every((m) => m.isInstancedMesh),
    })"""
    try:
        p.open()
        got = {}
        for x, t in [(0, 0), (0.5, 0), (1, 0), (1, 1)]:
            info = p.page.evaluate(f"__story.at('m', 0, {x}, {t})")
            got[x, t] = _lab(p, numbers)
            assert info["scene"] == "m" and info["calls"] < 40, info
        none, half, full, later = got[0, 0], got[0.5, 0], got[1, 0], got[1, 1]
        assert all(g["draws"] for g in got.values())
        # field: a count. The first share x of them is shown; a morph moves every one between two layouts.
        assert (none["shown"], half["shown"], full["shown"]) == (0, 100, 200)
        assert none["first"] == pytest.approx([-5, 0.2, 0]) and none["size"] == pytest.approx(0.2)
        assert full["size"] == pytest.approx(0.1) and 4 <= full["first"][1] <= 5 and abs(full["first"][0]) <= 0.5
        assert none["first"][1] < half["first"][1] < full["first"][1]
        way = [(half[k][1] - none[k][1]) / (full[k][1] - none[k][1]) for k in ("first", "last")]
        assert way[0] > 0.8 and way[1] < 0.2, way  # they go in turn: the first has nearly arrived, the last has barely set off
        # frame: an extent. The base is drawn, then the uprights rise to the box's height.
        assert none["grown"] == [0, 0, 0] and full["grown"] == [1, 1, 1] and half["grown"][0] == 1 and half["grown"][2] == 0
        assert full["frame"] == [2, 3, 1] and full["upright"] == pytest.approx(3 + 0.05, abs=1e-6)
        assert half["upright"] == pytest.approx(3 * half["grown"][1] + 0.05, abs=1e-6) and 0 < half["grown"][1] < 1
        # ruler: a length in metres, a tick each metre
        assert (none["reached"], half["reached"], full["reached"]) == (0, 4, 8)
        assert (none["ticks"], half["ticks"], full["ticks"], full["of"]) == (0, 5, 9, 9)
        # stack: a part of a whole. Three of ten are marked; half of them stand at one half.
        assert (full["marked"], full["plain"]) == (3, 7) and (none["raised"], half["raised"], full["raised"]) == (0, 5, 10)
        # streams: two rates. After a second each speck has gone its speed in metres, along its direction.
        assert full["gone"] == [0.5, 2]
        assert later["slow"][1] - full["slow"][1] == pytest.approx(0.5, abs=1e-5) and later["slow"][0] == pytest.approx(full["slow"][0])
        moved = (later["fast"][0] - full["fast"][0]) % 4  # it comes round again at the end of its stretch
        assert moved == pytest.approx(2, abs=1e-5) and later["fast"][1] == pytest.approx(full["fast"][1])
        # bars: a few numbers compared. Their heights are the numbers; they rise in turn.
        assert full["heights"] == [0.5, 1, 2, 1.5] and full["tops"] == pytest.approx([0.5, 1, 2, 1.5])
        assert none["tops"] == [0, 0, 0, 0] and half["tops"][0] == pytest.approx(0.5) and half["tops"][3] == 0
        # and the bench is a picture: something of every mechanism shows, and the beat moves it
        p.page.evaluate("__story.at('m', 0, 0.3, 0)")
        early = _frame(p)
        p.page.evaluate("__story.at('m', 0, 1, 0)")
        end = _frame(p)
        assert _shows_something(end) > 0.05 and _changed(early, end) > 0.01
        assert p.messages == [], p.messages
    finally:
        p.close()
