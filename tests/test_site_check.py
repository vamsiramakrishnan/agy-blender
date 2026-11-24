"""check_site (deliver/site_check.py): the agent's eyes for a website it builds.

Run 1 (benchmarks/results/site_run1) is why it exists: the agent wrote four throw-away Playwright scripts, looked at six
chapter stops and never saw the empty grid between them. The fixtures in tests/fixtures/site_check are one small page
per finding; the tests without a browser hold the parts that need none (facts, pictures, the answer's shape).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from antigravity3d.deliver import site_check as sc

FIX = Path(__file__).parent / "fixtures" / "site_check"


def site(tmp_path: Path, name: str) -> Path:
    """A fixture copied into its own folder with the shared gl.js, as a built site would be."""
    dest = tmp_path / "sites" / name
    shutil.copytree(FIX / name, dest)
    shutil.copy(FIX / "gl.js", dest / "gl.js")
    return dest


def jpeg(a: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(a.astype(np.uint8)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


# --------------------------------------------------------------------------- without a browser


def test_numbers_are_read_as_numbers():
    got = [(v, d, u) for v, d, u, _ in sc._numbers("1,234 tokens in 12.5 ms, 99.82% kept, 1 815 768 turns")]
    assert got == [(1234.0, 0, "tokens"), (12.5, 1, "ms"), (99.82, 2, "%"), (1815768.0, 0, "turns")]
    assert sc._number_matches(20001, sc._numbers("20,001 lines"))
    assert sc._number_matches(0.37, sc._numbers("37 %"))            # a fraction shown as a percentage
    assert sc._number_matches(1815768, sc._numbers("1.8 M"))        # rounded to what is shown
    assert not sc._number_matches(20001, sc._numbers("19,999"))
    assert not sc._number_matches(1815768, sc._numbers("1.7 M"))


def test_a_fact_is_checked_against_its_source(tmp_path):
    src = tmp_path / "src"
    (src / "eval").mkdir(parents=True)
    (src / "eval" / "run.json").write_text(json.dumps({"runs": [{"tokens": 302628}], "name": "field needle", "ok": True}))
    (src / "eval" / "digest.txt").write_text("header\n  line one\n    line two\nfooter\n")
    (src / "README.md").write_text("Capture once,\nretrieve the line that matters.\n")
    check = lambda s, text: sc.check_fact(s, text, src)  # noqa: E731
    assert check("eval/run.json#/runs/0/tokens", "302,628") is None
    assert "holds '302628'" in check("eval/run.json#/runs/0/tokens", "302,629")
    assert check("eval/run.json#/name", "the field needle run") is None
    assert check("eval/run.json#/ok", "true") is None
    assert "nothing at /runs/3" in check("eval/run.json#/runs/3/tokens", "1")
    assert "point at the one value" in check("eval/run.json#/runs", "1")
    # lines: the specimen equals them (indentation in common does not matter), or one shown line occurs in them
    assert check("eval/digest.txt#L2-L3", "line one\n  line two") is None
    assert check("eval/digest.txt#L3", "line two") is None
    bad = check("eval/digest.txt#L2-L3", "line one\n  line 2")
    assert "differs from the file at line 3" in bad and "'line 2'" in bad
    assert "has 4 lines" in check("eval/digest.txt#L3-L9", "x")
    # a quote must occur in the file, whitespace aside
    assert check("README.md", "Capture once, retrieve the line that matters.") is None
    assert "does not occur in README.md" in check("README.md", "Capture everything.")
    # what cannot be checked says why
    assert "there is no missing.json" in check("missing.json#/a", "1")
    assert "outside the sources folder" in check("../etc/passwd", "root")
    assert "names no file" in check("https://example.com/a.json#/a", "1")
    assert "JSON pointer" in check("README.md#line3", "x")
    assert "no sources folder" in sc.check_fact("a.json#/a", "1", None)


def test_unsourced_numbers_and_code_are_listed_and_years_are_not():
    raw = {"sourced": [{"src": "a.json#/x", "text": "5", "sel": "span#a"}],
           "code": ["$ ctx run -- pytest / [ctx run:8956c843e556]"],
           "texts": [{"text": "We saved 4,321 tokens in 12 ms.", "sel": "p"}, {"text": "© 2026", "sel": "footer"},
                     {"text": "Step 3 of 7", "sel": "p"}, {"text": "531", "sel": "span"}]}
    f = sc.judge_facts(raw, None)
    assert f["checked"] == 1 and len(f["mismatches"]) == 1  # no sources folder: it says it cannot check
    assert [n.split(" in ")[0] for n in f["unsourced_numbers"]] == ["4,321 tokens", "12 ms", "531"]
    assert f["unsourced_code"] == ["$ ctx run -- pytest / [ctx run:8956c843e556]"]


def test_canvas_content_ignores_a_helper_grid_and_sees_an_object():
    bg = np.full((400, 640, 3), 30)
    assert sc.canvas_content(jpeg(bg)) == 0.0
    grid = bg.copy()
    grid[::40, :] = 90
    grid[:, ::40] = 90                                       # a 1 px grid: an "almost empty grid" is still empty
    assert sc.canvas_content(jpeg(grid)) < sc.EMPTY_CONTENT
    obj = bg.copy()
    obj[100:300, 200:440] = (200, 120, 40)                   # an object covering 19 % of the canvas
    assert 0.17 < sc.canvas_content(jpeg(obj)) < 0.21


def test_steps_go_coarse_to_fine_so_a_cut_short_check_still_covers_the_scroll():
    assert sc.step_order(9) == [0, 8, 4, 2, 6, 1, 3, 5, 7]
    for n in range(2, 26):
        assert sorted(sc.step_order(n)) == list(range(n))
        first = sorted(sc.step_order(n)[:3])
        assert first[0] == 0 and first[-1] == n - 1


def test_the_folder_is_served_under_a_sub_path(tmp_path):
    root = tmp_path / "s"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<p>hi</p>")
    (root / "assets" / "a.css").write_text("p{}")
    (tmp_path / "secret.txt").write_text("no")
    srv = sc._SiteServer(root)
    try:
        get = lambda url: urllib.request.urlopen(url, timeout=5)  # noqa: E731
        assert srv.url.startswith(srv.origin + "/site-") and srv.url.endswith("/")
        assert get(srv.url).read() == b"<p>hi</p>"                      # a folder is its index.html
        r = get(srv.url + "assets/a.css")
        assert r.headers["Content-Type"] == "text/css"
        for url in (srv.origin + "/assets/a.css", srv.url + "../secret.txt", srv.url + "%2e%2e/secret.txt"):
            with pytest.raises(urllib.error.HTTPError):
                get(url)
        assert srv.missing[0] == "/assets/a.css (asked at the server root)"
    finally:
        srv.close()


def test_contact_sheet_labels_every_frame(tmp_path):
    frames = []
    for i in range(5):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(jpeg(np.full((90, 144, 3), 40 * i)))
        frames.append((str(p), f"{i + 1}/5", i == 2))
    sc.contact_sheet(frames, tmp_path / "sheet.jpg", 100, 3)
    im = Image.open(tmp_path / "sheet.jpg")
    assert im.size == (3 * 106 + 6, 2 * (62 + 22 + 6) + 6)  # 90 x 144 frames at 100 px wide are 62 px high


def _report(tmp_path: Path) -> sc.SiteReport:
    rep = sc.SiteReport("site", tmp_path, steps=9, seconds=42.0)
    d = sc.Visit("desktop", load_s=3.0, loaded=True, page_errors=["TypeError: boom"], failed=["x.png (HTTP 404)"],
                 other_hosts={"fonts.googleapis.com": 2}, idle={"seconds": 1.5, "frames": 90, "drawn": 90, "changed": False},
                 memory={"texture_bytes": 9_000_000, "uploaded_bytes": 8_000_000, "textures": 4, "renderbuffer_bytes": 0},
                 probe={"contexts": 1, "notices": []}, text_chars=4000)
    d.requests = {f"http://h/site/m{i}.glb": {"kind": "models", "bytes": 3_000_000, "status": 200, "own": True} for i in range(3)}
    for i in range(9):
        d.steps.append(sc.Step(i, i / 8, i * 100, frame=str(tmp_path / f"desktop_{i:02d}.jpg"), canvas_content=0.003 if i == 5 else 0.3,
                               draws=12, triangles=40_000))
    p = sc.Visit("phone", load_s=2.0, loaded=True, probe={"contexts": 1})
    for i in range(9):
        p.steps.append(sc.Step(i, i / 8, i * 100, frame=str(tmp_path / f"phone_{i:02d}.jpg"), canvas_content=0.3, sideways_px=40))
    p.overflow = ["div#wide (40 px)"]
    r = sc.Visit("reduced", loaded=True, probe={"reducedMotionQueried": False}, idle={"changed": True})
    w = sc.Visit("nowebgl", loaded=True, page_errors=["Error creating WebGL context."], text_chars=3900, probe={"notices": [], "images": 0})
    rep.visits = {"desktop": d, "phone": p, "reduced": r, "nowebgl": w}
    rep.sheets = {"desktop": str(tmp_path / "sheet_desktop.jpg"), "phone": str(tmp_path / "sheet_phone.jpg")}
    rep.facts = sc.judge_facts({"sourced": [], "code": ["$ made up"], "texts": [{"text": "531 tokens", "sel": "p"}]}, tmp_path)
    return rep


def test_the_answer_leads_with_the_outcome_and_keeps_its_order(tmp_path):
    text = _report(tmp_path).text()
    lines = text.splitlines()
    assert lines[0].startswith("SITE CHECKED in 42 s: 2 things broken, ") and "contact sheets" in lines[0]
    heads = [lines.index(h) for h in ("Broken:", "A visitor would notice:", "Measures:", "Pictures:")]
    assert heads == sorted(heads)
    assert len(lines) <= 60
    assert "step 6 of 9 (62%)" in text                                  # the empty canvas
    assert "nearly vanishes" not in text                                 # every other step shows 30 %
    assert "scrolls sideways by up to 40 px; widest: div#wide" in text
    assert "about 7 s on a 10 Mbit/s connection" in text                 # 9 MB, over the stated threshold
    assert "keeps drawing an unchanging picture" in text
    assert "never asks for it" in text and "Without WebGL: 1 uncaught error" in text
    assert "Shown without a source: 1 code specimen" in text
    assert str(tmp_path / "sheet_desktop.jpg") in text


def test_a_remote_url_or_a_folder_without_a_page_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a localhost URL"):
        sc._resolve_target("https://example.com/")
    with pytest.raises(ValueError, match="holds no index.html"):
        sc._resolve_target(str(tmp_path))
    assert sc._resolve_target("http://localhost:5173/") == (None, "http://localhost:5173/")
    assert sc.out_dir_for(tmp_path, "http://localhost:5173/") == tmp_path / "agy3d_site_check" / "localhost-5173"
    assert sc.out_dir_for(tmp_path, str(tmp_path / "dist")) == tmp_path / "agy3d_site_check" / "dist"


def test_check_site_is_a_tool_in_both_modes_and_a_command():
    from antigravity3d.cli import build_parser
    from antigravity3d.runtime import mcp_server as M

    assert "check_site" in M.MANAGEMENT_TOOLS and "check_site" in M.SCRIPT_MANAGEMENT_TOOLS
    for script in (False, True):
        host = M.StudioHost()
        host.script = script
        tools = M._management(host)
        assert "check_site" in tools and "data-src" in tools["check_site"][1]
    a = build_parser().parse_args(["check-site", "dist", "--steps", "5", "--sizes", "desktop"])
    assert (a.target, a.steps, a.sizes) == ("dist", 5, "desktop")


# --------------------------------------------------------------------------- in the browser


def run(target: Path, tmp_path: Path, **kw) -> sc.SiteReport:
    kw.setdefault("steps", 3)
    kw.setdefault("sizes", ("desktop",))
    kw.setdefault("variants", ())
    return sc.check_site(str(target), out_dir=tmp_path / "out", **kw)


@pytest.mark.browser
def test_the_scene_cost_is_counted_exactly_on_a_page_whose_counts_are_known(tmp_path):
    r = run(site(tmp_path, "known_counts"), tmp_path)
    d = r.visits["desktop"]
    assert {(s.draws, s.triangles) for s in d.steps} == {(4, 118)}
    # 256 x 256 RGBA8 + a third for its mipmaps, 128 x 128 RGBA16F with no data, one 64 x 64 DEPTH_COMPONENT16
    assert d.memory == {"texture_bytes": 262144 + 87381 + 131072, "uploaded_bytes": 262144 + 87381, "textures": 2,
                        "renderbuffer_bytes": 8192}
    assert d.idle["drawn"] == d.idle["frames"] > 0 and not d.idle["changed"]
    assert "keeps drawing an unchanging picture" in r.text()
    assert all(0.3 < s.canvas_content < 0.45 for s in d.steps)     # read from the drawing buffer (it draws every frame)


@pytest.mark.browser
def test_the_count_agrees_with_three_js_own(tmp_path):
    """The probe's numbers against renderer.info on a three.js scene: meshes, an InstancedMesh and line segments."""
    import asyncio

    three = os.environ.get("AGY3D_THREE_JS")
    if not three:
        pytest.skip("set AGY3D_THREE_JS to a local three.module.js")
    from playwright.async_api import async_playwright

    from antigravity3d.adapters.engines.web_three import WebThreeValidator, _launch_args

    root = site(tmp_path, "three_counts")
    shutil.copy(three, root / "three.module.js")

    async def go():
        srv = sc._SiteServer(root)
        try:
            async with async_playwright() as pw:
                b = await pw.chromium.launch(executable_path=WebThreeValidator().binary(), args=list(_launch_args()))
                page = await b.new_page(viewport={"width": 800, "height": 600})
                await page.add_init_script(sc._probe_script(False))
                await page.goto(srv.url)
                await page.wait_for_function("window.__info && window.__info.calls > 0")
                await page.evaluate("window.__agySite.mark()")
                await page.wait_for_timeout(300)
                win, info = await page.evaluate("[window.__agySite.mark(), window.__info]")
                await b.close()
                return win, info
        finally:
            srv.close()

    win, info = asyncio.run(go())
    assert (win["maxDraws"], win["maxTris"]) == (info["calls"], info["triangles"])
    assert info["calls"] == 7  # five boxes, the instanced spheres, the lines


@pytest.mark.browser
def test_the_empty_canvas_steps_are_found_with_the_html_hidden(tmp_path):
    r = run(site(tmp_path, "empty_middle"), tmp_path, steps=9)
    d = r.visits["desktop"]
    empty = [s.index for s in d.steps if s.canvas_content < sc.EMPTY_CONTENT]
    assert empty == [4, 5], [(s.index, s.canvas_content) for s in d.steps]   # 50 % and 62 %
    assert "the 3D canvas shows nothing but its background at step 5 of 9 (50%), step 6 of 9 (62%)" in r.text()
    assert Path(d.steps[4].canvas_frame).is_file() and Path(r.sheets["desktop"]).is_file()


@pytest.mark.browser
def test_a_page_that_does_not_change_on_scroll_is_called_frozen(tmp_path):
    r = run(site(tmp_path, "frozen"), tmp_path)
    assert [s.unchanged for s in r.visits["desktop"].steps] == [False, True, True]


@pytest.mark.browser
def test_errors_failed_requests_the_server_root_and_other_hosts(tmp_path):
    r = run(site(tmp_path, "broken"), tmp_path)
    broken, text = r.broken(), r.text()
    assert any("uncaught error" in b and "fixture boom" in b for b in broken)
    assert any("console error" in b and "fixture console error" in b for b in broken)
    failed = next(b for b in broken if "requests for the site's own files failed" in b)
    assert "/app.js (asked at the server root)" in failed and "missing.png" in failed and "base: './'" in failed
    assert "never created a WebGL context" in text
    assert r.other_hosts() == {"fonts.example.com": 1} and "not requested by this check" in text


@pytest.mark.browser
def test_sideways_scroll_on_a_phone(tmp_path):
    r = run(site(tmp_path, "phone_overflow"), tmp_path, sizes=("phone",))
    assert max(s.sideways_px for s in r.visits["phone"].steps) == 210
    assert "scrolls sideways by up to 210 px; widest: div#wide (210 px)" in r.text()


@pytest.mark.browser
def test_reduce_motion_and_no_webgl_ignored(tmp_path):
    r = run(site(tmp_path, "careless"), tmp_path, steps=2, variants=("reduced", "nowebgl"))
    reduced, nowebgl = r.reduced_line(), r.nowebgl_line()
    assert not reduced[1] and "never asks for it" in reduced[0] and "movement while nothing is touched" in reduced[0]
    assert not nowebgl[1] and "1 uncaught error" in nowebgl[0] and "no message about the missing 3D" in nowebgl[0]
    assert Path(r.visits["nowebgl"].frame).is_file()


@pytest.mark.browser
def test_reduce_motion_and_no_webgl_handled(tmp_path):
    r = run(site(tmp_path, "careful"), tmp_path, steps=2, variants=("reduced", "nowebgl"))
    reduced, nowebgl = r.reduced_line(), r.nowebgl_line()
    assert reduced[1] and "asks matchMedia" in reduced[0] and "stays still (without the setting it changes)" in reduced[0]
    assert nowebgl[1] and "says so on screen ('The 3D view is unavailable: WebGL is off" in nowebgl[0]
    assert not [n for n in r.noticed() if "reduce motion" in n or "Without WebGL" in n]


@pytest.mark.browser
def test_facts_on_the_page_against_their_sources(tmp_path):
    r = run(site(tmp_path, "facts"), tmp_path)
    f = r.facts
    assert f["checked"] == 7
    assert [m.split(":")[0] for m in f["mismatches"]] == ["Fact span#bad-num", "Fact pre#bad-code", "Fact blockquote#bad-quote"]
    assert [n.split(" in ")[0] for n in f["unsourced_numbers"]] == ["4,321 tokens", "12 ms"]
    assert f["unsourced_code"] == ["$ invented --command / result 42"]


@pytest.mark.browser
def test_weight_by_type_and_the_largest_files(tmp_path):
    root = tmp_path / "heavy"
    (root / "models").mkdir(parents=True)
    (root / "models" / "big.glb").write_bytes(os.urandom(7_000_000))
    (root / "index.html").write_text("<!doctype html><title>Heavy</title><p>heavy</p>"
                                     "<script>fetch('models/big.glb').then((r) => r.arrayBuffer())</script>")
    r = run(root, tmp_path, steps=2)
    line = r.bytes_line()
    assert r.total_bytes() > 7_000_000 and "models 7.0 MB" in line and "Largest: models/big.glb 7.0 MB" in line
    assert any("downloads 7.0 MB: about 6 s on a 10 Mbit/s connection" in n for n in r.noticed())


@pytest.mark.browser
def test_it_answers_within_its_budget_with_what_it_has(tmp_path):
    t0 = time.monotonic()
    budget = sc.WRAP_UP_S + 10
    r = run(site(tmp_path, "known_counts"), tmp_path, steps=25, budget_s=budget)
    assert time.monotonic() - t0 < budget + 5
    d = r.visits["desktop"]
    assert d.loaded and 0 < len(d.steps) < 25
    assert any(f"{len(d.steps)} of 25 steps photographed" in u for u in r.unfinished)
    assert f"stopped at its {budget:.0f} s limit" in r.text() and "steps=5" in r.text()


async def test_the_tool_refuses_a_remote_url_in_one_line():
    from antigravity3d.runtime.mcp_server import StudioHost, _management

    tool = _management(StudioHost())["check_site"][0]
    text = await tool(target="https://example.com/", ctx=None)
    assert text.startswith("NOT CHECKED: https://example.com/ is not a localhost URL") and "\n" not in text


@pytest.mark.browser
async def test_the_tool_checks_a_folder_of_the_workspace(tmp_path, monkeypatch):
    from antigravity3d.runtime.mcp_server import StudioHost, _management

    site(tmp_path, "careful")
    monkeypatch.chdir(tmp_path)  # no client roots: the workspace is the server's folder
    tool = _management(StudioHost())["check_site"][0]
    text = await tool(target="sites/careful", ctx=None, steps=2, sizes="desktop")
    out = tmp_path / "agy3d_site_check" / "careful"
    assert text.startswith("SITE CHECKED in ") and f"desktop contact sheet: {out / 'sheet_desktop.jpg'}" in text
    assert (out / "sheet_desktop.jpg").is_file() and (out / "report.json").is_file()
    assert "With reduce motion set the page asks matchMedia" in text


def test_a_step_where_the_scene_all_but_leaves_the_frame_is_named(tmp_path):
    rep = sc.SiteReport("site", tmp_path, steps=5)
    v = sc.Visit("desktop", loaded=True, load_s=1.0)
    v.steps = [sc.Step(i, i / 4, 0, canvas_content=c) for i, c in enumerate((0.29, 0.19, 0.03, 0.15, 0.0))]
    rep.visits = {"desktop": v}
    noticed = rep.noticed()
    assert any("nearly vanishes at step 3 of 5 (50%) (3% of the canvas shows something, against 15% at a typical step)" in n
               for n in noticed), noticed
    assert any("nothing but its background at step 5 of 5 (100%)" in n for n in noticed)
