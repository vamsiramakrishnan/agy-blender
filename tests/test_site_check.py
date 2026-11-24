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


@pytest.fixture(autouse=True)
def _a_browser_test_runs_only_where_there_is_a_browser(request):
    """The marker alone skips nothing: CI's first job runs every test not marked live or blender, and has no Playwright."""
    if request.node.get_closest_marker("browser"):
        from antigravity3d.adapters.engines.web_three import WebThreeValidator

        if (why := WebThreeValidator()._unavailable()) is not None:
            pytest.skip(why)


def test_without_a_browser_the_answer_says_not_checked_and_nothing_about_the_site(tmp_path, monkeypatch):
    from antigravity3d.adapters.engines.web_three import WebThreeValidator

    monkeypatch.setattr(WebThreeValidator, "_unavailable", lambda self: "web_three needs the Python 'playwright' package")
    text = sc.check_site(str(site(tmp_path, "careful")), out_dir=tmp_path / "out").text()
    assert text.startswith("NOT CHECKED: No browser to look with: web_three needs the Python 'playwright' package.")
    assert "broken" not in text and "SITE CHECKED" not in text


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
    # a string value: all of it, or one unbroken passage long enough not to be there by chance (run 2: the first five
    # lines of a 400-character digest, exact, were called broken and the agent took the digest off the page)
    digest = ("[ctx run:8956c843e556 profile=logtemplate/v1]\ncwd: .\ncommand: cat corpus.log\nexit: 0\n"
              "stdout: 20,001 lines · 979.1 KiB · est 250,648 tokens\nstderr: 0 lines · 0 B\n" + "templates: 11 cover 20,001 lines\n" * 8)
    (src / "eval" / "record.json").write_text(json.dumps({"arms": [{"output_excerpt": digest}]}))
    at = "eval/record.json#/arms/0/output_excerpt"
    first_five = "\n".join(digest.split("\n")[:5])
    assert len(first_five) < 0.5 * len(digest) and check(at, first_five) is None
    assert check(at, "exit: 0\nstdout: 20,001 lines") is None  # from the middle, whitespace aside
    left_out = check(at, "[ctx run:8956c843e556 profile=logtemplate/v1]\nstdout: 20,001 lines · 979.1 KiB")
    assert "agrees with eval/record.json#/arms/0/output_excerpt for 46 characters" in left_out and "'cwd: . command" in left_out
    assert "one unbroken passage" in left_out and '\\n' not in left_out and '"[' not in left_out  # the text, not its JSON spelling
    short = check(at, "exit: 0")
    assert "shows only 'exit: 0', 7 of the" in short and "at least 20 characters" in short
    assert "holds '[ctx run:8956c843e556" in check(at, "something else entirely")
    assert check("eval/run.json#/name", "needle") is None and "shows only 'ne'" in check("eval/run.json#/name", "ne")  # a short value: half of it
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
    assert "shows little" not in text                                    # every other step shows 30 %
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


def test_a_motion_that_repeats_is_not_called_still_because_two_pictures_happened_to_match():
    """The idle measure compared two pictures 1.5 s apart. The "careful" fixture cycles its colour every 0.94 s, and on
    a loaded machine the two pictures fell two cycles apart: the same colour, so a moving page was called still."""
    import asyncio

    frames = [jpeg(np.full((90, 144, 3), v)) for v in (40, 200, 40)]  # first and last alike, the middle one not

    class Page:
        async def evaluate(self, _):
            return {"frames": 90, "drawn": 90}

    class V:
        def __init__(self, shots):
            self.shots = list(shots)

        async def shot(self):
            return self.shots.pop(0)

    moving = asyncio.run(sc._Visitor.idle(V(frames), Page(), seconds=0.05))
    assert moving["changed"] is True and moving["drawn"] == 90
    still = asyncio.run(sc._Visitor.idle(V([frames[0]] * 3), Page(), seconds=0.05))
    assert still["changed"] is False


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
    v.steps = [sc.Step(i, i / 4, 0, canvas_content=c, shape=0.5) for i, c in enumerate((0.29, 0.19, 0.03, 0.15, 0.0))]
    rep.visits = {"desktop": v}
    noticed = rep.noticed()
    assert any("shows little at step 3 of 5 (50%) (3% of it shows something, against 15% at a typical step; one solid "
               "shape is 50% of that)" in n
               for n in noticed), noticed
    assert any("nothing but its background at step 5 of 5 (100%)" in n for n in noticed)


def _contexts_alive() -> int:
    """Browser contexts open in the process's shared browser (0 when it is not running)."""
    from antigravity3d.adapters.engines import web_browser

    async def count() -> int:
        s = web_browser.SHARED._session
        return len(s.browser.contexts) if s is not None and s.alive() else 0

    return web_browser.SHARED.submit(count()).result(30)


@pytest.mark.browser
def test_a_page_that_hangs_is_left_behind_closed_and_the_next_check_works(tmp_path):
    """At the bound a visit stuck in its page is cancelled; web_browser.SHARED.run closes the cancelled visit's context
    (or discards the browser), and the answer waits for that, so nothing of the check is left running."""
    from antigravity3d.adapters.engines import web_browser

    # another user of the shared browser (an asset verification in the same server) keeps it running: so its contexts
    # are what is counted, not a browser closed because the check was its last user
    other = web_browser.SHARED.lease()
    try:
        budget = sc.WRAP_UP_S + 12
        t0 = time.monotonic()
        r = run(site(tmp_path, "hangs"), tmp_path, steps=5, budget_s=budget)
        assert time.monotonic() - t0 < budget + 3
        assert r.unfinished and "stopped at its" in r.text()
        assert _contexts_alive() == 0
    finally:
        other.release(keep=False)
    again = run(site(tmp_path, "known_counts"), tmp_path / "again", steps=2)
    assert {(s.draws, s.triangles) for s in again.visits["desktop"].steps} == {(4, 118)}


@pytest.mark.browser
def test_a_page_that_never_finishes_loading_is_reported_and_closed(tmp_path):
    import http.server
    import threading

    class Hanging(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802  the method name http.server dispatches a GET to
            if self.path.startswith("/hang"):
                time.sleep(120)  # never within the check's time
                return
            body = b"<!doctype html><title>Never loads</title><p>waiting</p><img src='/hang.png'>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hanging)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        budget = sc.WRAP_UP_S + 12
        t0 = time.monotonic()
        r = sc.check_site(f"http://localhost:{srv.server_address[1]}/", out_dir=tmp_path / "out", steps=3,
                          sizes=("desktop",), variants=(), budget_s=budget)
        assert time.monotonic() - t0 < budget + 3
        assert not r.visits["desktop"].loaded and "did not load at desktop size" in r.text()
        assert _contexts_alive() == 0
    finally:
        srv.shutdown()


def test_the_pictures_never_go_inside_the_site(tmp_path):
    (tmp_path / "index.html").write_text("<p>a site built at the workspace root</p>")
    out = sc.out_dir_for(tmp_path, str(tmp_path))
    assert not out.is_relative_to(tmp_path.resolve()) and out.is_absolute()
    with pytest.raises(ValueError, match="inside the site"):
        sc.check_site(str(tmp_path), out_dir=tmp_path / "shots")


def test_one_solid_shape_scores_high_and_scattered_lines_low():
    bg = np.full((400, 640, 3), 20)
    prop = bg.copy()
    prop[180:240, 300:360] = (200, 150, 90)      # a small prop, framed wide on purpose
    prop[244:250, 290:370] = (60, 60, 60)        # its shadow, a little apart
    assert sc.largest_shape(jpeg(prop)) > 0.7
    left = bg.copy()
    for x in range(0, 640, 80):
        left[:, x:x + 3] = 90                    # grid lines and an arc: what is left when the subject has gone
    left[300:304, 50:600] = 120
    assert sc.largest_shape(jpeg(left)) < 0.5
    assert sc.largest_shape(jpeg(bg)) == 0.0


# --------------------------------------------------------------------------- the page's subjects


def _subj(name: str, kind: str = "prop", cut: float = 0.0, edges: dict | None = None, hidden: float | None = 0.0,
          under: str = "", px: tuple[int, int] = (576, 360)) -> dict:
    return {"name": name, "kind": kind, "box": [0.2, 0.2, 0.6, 0.6], "px": list(px), "cut": cut, "edges": edges or {},
            "hidden": hidden, "under": under}


def test_subjects_cut_or_hidden_are_named_once_each_with_their_steps(tmp_path):
    """Thresholds from run 2's site: a label from 10 % cut or hidden, a prop from 30 % cut or 15 % hidden, nothing for
    a subject less than a fifth in the frame."""
    rep = sc.SiteReport("site", tmp_path, steps=5)
    v = sc.Visit("desktop", loaded=True, load_s=1.0)
    v.steps = [sc.Step(i, i / 4, 0, frame=str(tmp_path / f"desktop_{i:02d}.jpg")) for i in range(5)]
    v.steps[0].subjects = [_subj("RAW FLOOD", "label", cut=0.15, edges={"bottom": 0.15}),
                           _subj("gate", cut=0.11, edges={"right": 0.11}),                 # a close shot
                           _subj("vault", cut=0.84, edges={"right": 0.84})]                 # a sliver at the edge
    v.steps[1].subjects = [_subj("gate", hidden=0.20, under="section#digest > div.panel"),
                           _subj("QUIET NEEDLE", "label", hidden=0.12, under="section#flood > div.panel"),
                           _subj("clamp", hidden=0.12, under="header.topbar")]              # a box's empty corner
    v.steps[3].subjects = [_subj("RAW FLOOD", "label", cut=0.52, edges={"left": 0.52}), _subj("gate", hidden=0.5,
                                                                                                under="section#retrieve > div.panel")]
    v.steps[4].subjects = None
    rep.visits = {"desktop": v}
    lines = [n for n in rep.noticed() if "the prop" in n or "the 3D label" in n]
    assert lines == [
        ("Desktop: the prop 'gate' lies under section#retrieve > div.panel, section#digest > div.panel at steps 2 (25%) "
         "and 4 (75%) of 5 (at worst 50% of what is in the frame). Look at desktop_03.jpg."),
        ("Desktop: the 3D label 'RAW FLOOD' runs out of the frame at the left and bottom edges at steps 1 (0%) and 4 (75%) "
         "of 5 (at worst 52% of it outside). Look at desktop_03.jpg."),
        ("Desktop: the 3D label 'QUIET NEEDLE' lies under section#flood > div.panel at step 2 of 5 (25%) (at worst 12% of "
         "what is in the frame). Look at desktop_01.jpg.")], lines
    assert rep.text().count("the 3D label") == 2


def test_where_the_page_names_the_shots_subject_the_props_behind_it_are_not_reported(tmp_path):
    """Run 2's site rebuilt with boxes: the gate, left standing behind later chapters' panels, was named at five steps."""
    marked = [{"name": "gate", "kind": "prop", "subject": False, "box": [0.0, 0.2, 0.3, 0.6], "px": [400, 300], "cut": 0.0,
               "edges": {}, "hidden": 0.7, "under": "div#panel"},
              {"name": "vault", "kind": "prop", "subject": True, "box": [0.1, 0.2, 0.4, 0.6], "px": [400, 300], "cut": 0.0,
               "edges": {}, "hidden": 0.4, "under": "div#panel"},
              {"name": "A LABEL", "kind": "label", "subject": None, "box": [0.1, 0.2, 0.4, 0.3], "px": [400, 90], "cut": 0.0,
               "edges": {}, "hidden": 0.5, "under": "div#panel"}]

    class Page:
        def __init__(self, rows):
            self.rows = rows

        async def evaluate(self, _):
            return {"list": self.rows}

    import asyncio

    read = lambda rows: asyncio.run(sc._Visitor.subjects(None, Page(rows)))  # noqa: E731  (the method uses no state)
    got, note = read(marked)
    by = {s["name"]: s for s in got}
    assert by["gate"]["background"] is True and by["vault"]["background"] is False and by["A LABEL"]["background"] is False
    assert sc.subject_faults(by["gate"]) == (False, False) and sc.subject_faults(by["vault"]) == (False, True)
    assert sc.subject_faults(by["A LABEL"]) == (False, True) and note.startswith("3 subjects")
    # a page that marks nothing: every prop counts, as before
    unmarked = [{**m, "subject": None} for m in marked]
    got, _ = read(unmarked)
    assert all("background" not in s for s in got) and sc.subject_faults(got[0]) == (False, True)


def test_a_label_that_is_never_large_enough_to_read_is_named_and_one_that_is_small_only_far_away_is_not(tmp_path):
    """Run 2: a label flat on the floor in the last wide shot had a 15 px box and letters of a few pixels."""
    rep = sc.SiteReport("site", tmp_path, steps=3)
    v = sc.Visit("desktop", loaded=True, load_s=1.0)
    v.steps = [sc.Step(i, i / 2, 0, frame=str(tmp_path / f"desktop_{i:02d}.jpg")) for i in range(3)]
    v.steps[0].subjects = [_subj("NEAR THEN FAR", "label", px=(300, 40)), _subj("ALWAYS TINY", "label", px=(90, 9))]
    v.steps[1].subjects = [_subj("NEAR THEN FAR", "label", px=(60, 8)), _subj("ALWAYS TINY", "label", px=(140, 15)),
                           _subj("a small prop", px=(20, 10))]
    v.steps[2].subjects = [_subj("ALWAYS TINY", "label", px=(400, 60), cut=0.9, edges={"right": 0.9})]  # a sliver: not its size
    rep.visits = {"desktop": v}
    lines = [n for n in rep.noticed() if "the prop" in n or "the 3D label" in n]
    assert lines == ["Desktop: the 3D label 'ALWAYS TINY' is never taller than 15 px at any step (its largest is at step 2 of 3 "
                     "(50%)), too small to read. Look at desktop_01.jpg."], lines
    assert sc.LABEL_MIN_PX == 16


def test_many_subjects_keep_the_answer_short(tmp_path):
    rep = sc.SiteReport("site", tmp_path, steps=2)
    v = sc.Visit("phone", loaded=True, load_s=1.0)
    v.steps = [sc.Step(i, i, 0, frame=str(tmp_path / f"phone_{i:02d}.jpg")) for i in range(2)]
    v.steps[0].subjects = [_subj(f"LABEL {i}", "label", cut=0.5, edges={"left": 0.5}) for i in range(64)]
    rep.visits = {"phone": v}
    lines = [n for n in rep.noticed() if n.startswith("Phone:")]
    assert len(lines) == sc.SUBJECT_LINES + 1
    assert lines[-1].startswith("Phone: also cut, hidden or too small at some step: the 3D label 'LABEL 16'") and "and 48 more" in lines[-1]


def test_a_measured_subject_is_checked_again_when_it_comes_back_from_the_page():
    good = {"name": "n" * 300, "kind": "label", "box": [-0.1, 0.2, 0.5, 0.6], "px": [10, 20], "cut": 0.2,
            "edges": {"left": 0.2, "bogus": 5}, "hidden": None, "under": "div#panel"}
    s = sc._subject(good)
    assert s["name"] == "n" * 80 and s["edges"] == {"left": 0.2} and s["hidden"] is None and s["kind"] == "label"
    for bad in (None, "x", {"name": 1}, {**good, "box": [0, 0, 1]}, {**good, "box": [0, 0, float("nan"), 1]},
                {**good, "cut": 2.0}, {**good, "cut": True}, {**good, "box": "abcd"}):
        assert sc._subject(bad) is None
    assert sc._subject({**good, "kind": "<script>"})["kind"] == "prop"


@pytest.mark.browser
def test_a_lost_context_counts_only_on_a_canvas_the_visitor_can_see(tmp_path):
    """The starter releases the context of the canvas it tests WebGL with (never in the document); the check called
    that broken. The scene's own canvas losing its context is the fault it means."""
    released = run(site(tmp_path, "test_context"), tmp_path, steps=2)
    assert released.visits["desktop"].probe["lost"] == 0 and "context was lost" not in released.text()
    lost = sc.check_site(str(site(tmp_path, "context_lost")), out_dir=tmp_path / "out2", steps=2, sizes=("desktop",), variants=())
    assert lost.visits["desktop"].probe["lost"] == 1
    assert any("the WebGL context was lost 1 time" in b for b in lost.broken()), lost.broken()


def _frame_at(report, y: float) -> tuple[int, int, int]:
    """The colour of the first desktop frame at mid width and ``y`` of its height."""
    im = Image.open(report.visits["desktop"].steps[0].frame).convert("RGB")
    return im.getpixel((im.width // 2, round(im.height * y)))


def _opposite_the_bar(report) -> tuple[int, int, int]:
    """At 80 % of the height the fixtures' canvas shows, opposite their bar."""
    return _frame_at(report, 0.8)


def _is_the_canvas(px) -> bool:
    return px[0] > 200 and px[1] < 60 and px[2] > 200


def _is_the_bar(px) -> bool:  # #203040, through a JPEG
    return abs(px[0] - 32) < 24 and abs(px[1] - 48) < 24 and abs(px[2] - 64) < 24


@pytest.mark.browser
def test_the_frames_show_a_canvas_that_lies_under_an_opaque_box(tmp_path):
    """Run 3: a Chromium that puts pages together without the GPU path left a black box in every frame, opposite the
    text panel, and the agent spent 16 minutes on a fault no visitor sees. Whatever this machine's browser does, the
    frame shows the canvas, and the answer speaks of the camera only where it had to work around it."""
    r = sc.check_site(str(site(tmp_path, "under_a_bar")), out_dir=tmp_path / "out", steps=2, sizes=("desktop",), variants=())
    assert r.camera in ("true", "layer"), r.camera
    assert _is_the_canvas(_opposite_the_bar(r)), (_opposite_the_bar(r), r.camera)
    assert ("about these pictures" in r.text()) == (r.camera == "layer")
    assert json.loads((tmp_path / "out" / "report.json").read_text())["camera"] == r.camera


@pytest.mark.browser
def test_a_browser_without_the_gpu_path_is_found_out_and_worked_around(tmp_path, monkeypatch):
    """The same browser started without the switch that puts everything on SwiftShader: on a machine without a GPU it
    then composites in software and has the fault. The check finds it, gives the canvas a layer and photographs the
    truth; with the layer taken away it says that the frames have the fault. Each browser is tested once."""
    from antigravity3d.adapters.engines import web_browser, web_three

    tests_made = []
    real_test = sc._camera_test

    async def counted(ctx):
        tests_made.append(1)
        return await real_test(ctx)

    monkeypatch.setattr(sc, "_camera_test", counted)
    monkeypatch.setattr(web_three, "CHROMIUM_ARGS", [a for a in web_three.CHROMIUM_ARGS if not a.startswith("--use-angle")])
    try:
        folder = site(tmp_path, "under_a_bar")
        r = sc.check_site(str(folder), out_dir=tmp_path / "out", steps=2, sizes=("desktop",), variants=())
        assert _is_the_canvas(_opposite_the_bar(r)), (_opposite_the_bar(r), r.camera)
        if r.camera != "layer":
            pytest.skip(f"this machine's browser shows the canvas without help (camera: {r.camera!r}): nothing to work around")
        assert "photographed each canvas on a layer of its own" in r.text()
        assert r.visits["desktop"].probe["layer"] == {"given": 1, "left": 0}
        browser = web_browser.SHARED._session
        assert browser.notes == {"site_camera": "layer"} and len(tests_made) == 1
        again = sc.check_site(str(folder), out_dir=tmp_path / "out", steps=2, sizes=("desktop",), variants=())
        # the same browser, and its camera was not tested a second time
        assert again.camera == "layer" and web_browser.SHARED._session is browser and len(tests_made) == 1
        # without the layer: the fault is in the frame, and the answer says whose it is
        web_browser.SHARED.shutdown()
        monkeypatch.setattr(sc, "CANVAS_LAYER_CSS", ":where(canvas){}")
        bare = sc.check_site(str(folder), out_dir=tmp_path / "out", steps=2, sizes=("desktop",), variants=())
        assert bare.camera == "faulty" and not _is_the_canvas(_opposite_the_bar(bare)), (bare.camera, _opposite_the_bar(bare))
        assert "the frames here have it" in bare.text() and "Do not change the site for it" in bare.text()
        assert not bare.broken() and not [n for n in bare.noticed() if "canvas" in n.lower() and "black" in n.lower()]
    finally:
        web_browser.SHARED.shutdown()  # the next test starts the browser as the product does


@pytest.mark.browser
def test_a_camera_test_that_hangs_costs_its_wait_and_not_the_browser(tmp_path, monkeypatch):
    """The wait is for the test, not for the browser's start: bounded from outside, a slow start cancelled the run,
    which closes the browser that had just come up, and the check went on without knowing its camera."""
    import asyncio

    from antigravity3d.adapters.engines import web_browser

    web_browser.SHARED.shutdown()
    real_test = sc._camera_test

    async def hangs(ctx):
        await asyncio.sleep(60)

    monkeypatch.setattr(sc, "_camera_test", hangs)
    monkeypatch.setattr(sc, "CAMERA_WAIT_S", 0.3)
    folder = site(tmp_path, "under_a_bar")
    r = sc.check_site(str(folder), out_dir=tmp_path / "out", steps=2, sizes=("desktop",), variants=())
    assert r.camera == "" and r.visits["desktop"].loaded and "about these pictures" not in r.text()
    browser = web_browser.SHARED._session
    assert browser is not None and browser.alive() and web_browser.SHARED.launches == browser.generation
    assert "site_camera" not in browser.notes  # not known is not a result: the next check asks again
    monkeypatch.setattr(sc, "_camera_test", real_test)
    monkeypatch.setattr(sc, "CAMERA_WAIT_S", 10.0)
    again = sc.check_site(str(folder), out_dir=tmp_path / "out", steps=2, sizes=("desktop",), variants=())
    assert again.camera in ("true", "layer") and web_browser.SHARED._session is browser


async def _camera_says_layer(session, ctx) -> str:
    return "layer"


@pytest.mark.browser
def test_a_canvas_in_the_normal_flow_gets_a_layer_only_where_nothing_changes_for_a_visitor(tmp_path, monkeypatch):
    """The layer is a filter, and a filter moves a canvas that is not positioned in with the positioned content, over
    what was written before it. With the layer asked for whatever this browser needs: a canvas under a header that
    comes first in the document is left as it is and counted; one whose overlay comes after it gets the layer."""
    monkeypatch.setattr(sc, "camera", _camera_says_layer)
    covered = sc.check_site(str(site(tmp_path, "layer_in_flow_covered")), out_dir=tmp_path / "out1", steps=2,
                            sizes=("desktop",), variants=())
    assert covered.visits["desktop"].probe["layer"] == {"given": 0, "left": 1} and covered.visits["desktop"].probe["layerLeft"] == 1
    assert _is_the_bar(_frame_at(covered, 0.2)), _frame_at(covered, 0.2)  # the header is still over the canvas
    text = covered.text()
    assert "1 canvas in the page's normal flow could not be given one" in text and "Do not change the site for it" in text
    free = sc.check_site(str(site(tmp_path, "layer_in_flow_free")), out_dir=tmp_path / "out2", steps=2,
                         sizes=("desktop",), variants=())
    assert free.visits["desktop"].probe["layer"] == {"given": 1, "left": 0}
    assert _is_the_bar(_frame_at(free, 0.2)) and _is_the_canvas(_opposite_the_bar(free)), (_frame_at(free, 0.2), _opposite_the_bar(free))
    assert "photographed each canvas on a layer of its own" in free.text()
    fixed = sc.check_site(str(site(tmp_path, "under_a_bar")), out_dir=tmp_path / "out3", steps=2, sizes=("desktop",), variants=())
    assert fixed.visits["desktop"].probe["layer"] == {"given": 1, "left": 0} and _is_the_canvas(_opposite_the_bar(fixed))


def test_the_answer_speaks_of_the_camera_only_when_it_was_at_fault(tmp_path):
    for cam, said in (("", False), ("true", False), ("layer", True), ("faulty", True)):
        text = sc.SiteReport("site", tmp_path, camera=cam).text()
        assert ("about these pictures" in text) == said, (cam, text)
    assert "2 canvases in the page's normal flow could not be given one" in sc.camera_line("layer", 2)
    assert sc.camera_line("true", 3) == "" and "canvases" not in sc.camera_line("faulty", 3)


@pytest.mark.browser
def test_subjects_under_opaque_html_or_cut_by_the_edge_are_reported(tmp_path):
    r = run(site(tmp_path, "subjects"), tmp_path, steps=2)
    d = r.visits["desktop"]
    for st in d.steps:
        got = {s["name"]: s for s in st.subjects}
        assert "old_style" not in got and len(got) == 8, st.subjects_note        # no box: nothing measured
        assert got["under_panel"]["hidden"] == 1.0 and got["under_panel"]["under"] == "div#panel"
        assert got["under_veil"]["hidden"] == 1.0 and got["under_veil"]["under"] == "div#veil"   # pointer-events: none
        for name in ("under_glass", "under_ghost", "under_dim", "CLEAR LABEL"):  # transparent, opacity 0, faded parent
            assert got[name]["hidden"] == 0.0, (name, got[name])
        assert got["CUT LABEL"]["edges"] == {"right": 0.4} and abs(got["CUT LABEL"]["cut"] - 0.4) < 1e-3
        assert 0.08 < got["edge_prop"]["cut"] < 0.1
    noticed = [n for n in r.noticed() if "the prop" in n or "the 3D label" in n]
    assert len(noticed) == 3, noticed
    assert any("the prop 'under_panel' lies under div#panel at steps 1 (0%) and 2 (100%) of 2 (at worst 100%" in n for n in noticed)
    assert any("the prop 'under_veil' lies under div#veil" in n for n in noticed)
    assert any("the 3D label 'CUT LABEL' runs out of the frame at the right edge" in n and "at worst 40% of it outside" in n
               for n in noticed)
    assert "edge_prop" not in r.text() and "under_glass" not in r.text()
    record = json.loads((tmp_path / "out" / "report.json").read_text())
    assert record["visits"]["desktop"]["steps"][0]["subjects"][0]["name"] == "under_panel"


@pytest.mark.browser
def test_a_hook_that_throws_lies_hangs_or_floods_breaks_nothing(tmp_path):
    t0 = time.monotonic()
    r = run(site(tmp_path, "subjects_hook"), tmp_path, steps=6)
    assert time.monotonic() - t0 < sc.ANSWER_S
    d = r.visits["desktop"]
    assert d.loaded and len(d.steps) == 6 and not r.unfinished and not d.page_errors
    notes = [s.subjects_note for s in d.steps]
    assert "threw: fixture hook boom" in notes[0]
    assert d.steps[1].subjects is None and notes[1] == "onScreen gives no box"
    many = d.steps[2].subjects
    assert len(many) == 64 and all(len(s["name"]) <= 80 for s in many) and notes[2] == "64 subjects with a box, more not read"
    assert d.steps[3].subjects is None and notes[3] == "onScreen gives no box"           # the old fields only
    assert d.steps[4].subjects is None and "did not resolve" in notes[4]
    assert d.steps[5].subjects is None and "proxy boom" in notes[5]
    lines = [n for n in r.noticed() if n.startswith("Desktop:") and "3D label" in n]
    assert len(lines) == sc.SUBJECT_LINES + 1 and len(r.text().splitlines()) <= 60


@pytest.mark.browser
def test_a_page_without_the_hook_says_nothing_about_subjects(tmp_path):
    r = run(site(tmp_path, "frozen"), tmp_path, steps=2)
    assert all(s.subjects is None and s.subjects_note == "no window.__site.info()" for s in r.visits["desktop"].steps)
    assert "the prop" not in r.text() and "3D label" not in r.text()


# --------------------------------------------------------------------------- stills for the page without WebGL


def test_chapter_ids_become_safe_distinct_file_names():
    names = sc.still_names(["intro", "Intro", "../../etc/passwd", "...", "", "a/b", "a-b", "con", "x" * 300, "été 2"])
    assert names == ["intro", "Intro-2", "etc-passwd", "chapter-4", "chapter-5", "a-b", "a-b-2", "chapter-con", "x" * 60,
                     "t-2"]
    assert len({n.lower() for n in names}) == len(names) and all("/" not in n and "." not in n for n in names)


def test_the_stills_line_says_where_they_are_and_what_they_are_for(tmp_path):
    rep = sc.SiteReport("site", tmp_path, steps=2)
    assert rep.stills_line() == "" and "page without WebGL" not in rep.text()
    rep.stills = {"note": "no window.__site.chapters() and setProgress()"}
    assert rep.stills_line() == ""
    made = [{"id": i, "file": f"{i}.webp", "rest": 0.7, "width": 1280, "height": 800, "bytes": 40_000, "content": 0.3,
             "how": ""} for i in ("intro", "how")]
    made[1]["content"] = 0.0
    rep.stills = {"dir": str(tmp_path / "stills"), "made": made, "skipped": [{"id": "outro", "why": "not drawn in time (TimeoutError)"}]}
    assert rep.stills_line() == (
        f"stills for the page without WebGL, one per chapter at the picture it rests on, the 3D canvas alone (2, 1280 x 800 "
        f"px WebP, 40 kB each on average): {tmp_path / 'stills'} (intro.webp, how.webp). Copy them beside the page "
        "(stills/<chapter id>.webp) and give each an alt text that says what it shows; how.webp shows nothing but the "
        "background; not made: 'outro' (not drawn in time (TimeoutError)).")


def _pixel(p: Path) -> tuple[int, int, int]:
    im = Image.open(p).convert("RGB")
    return im.getpixel((im.width // 2, im.height - 5))  # the bottom rows: where a browser without the GPU path leaves a band


@pytest.mark.browser
def test_one_still_per_chapter_shows_that_chapter_and_stays_in_its_folder(tmp_path):
    r = run(site(tmp_path, "stills"), tmp_path, steps=2)
    stills = tmp_path / "out" / "stills"
    made = {x["id"]: x for x in r.stills["made"]}
    assert sorted(p.name for p in stills.iterdir()) == sorted(
        ["intro.webp", "how.webp", "escape.webp", "Intro-2.webp", "chapter-5.webp", "a-b.webp", "a-b-2.webp"]), r.stills
    assert not any(p.name == "escape.webp" for p in tmp_path.rglob("*.webp") if p.parent != stills)
    want = {"intro": (255, 0, 0), "how": (0, 255, 0), "../../escape": (0, 0, 255), "Intro": (255, 255, 0),
            "...": (255, 0, 0), "a/b": (0, 255, 0), "a-b": (0, 0, 255)}
    for cid, rgb in want.items():
        got = _pixel(stills / made[cid]["file"])
        assert all(abs(a - b) <= 12 for a, b in zip(got, rgb)), (cid, got)
        assert (made[cid]["width"], made[cid]["height"]) == (1280, 800)
    assert {made[c]["how"] for c in ("intro", "how")} == {"drawn by setProgress", "drawn on the next frame"}
    record = json.loads((tmp_path / "out" / "report.json").read_text())
    assert len(record["stills"]["made"]) == 7 and "stills for the page without WebGL" in r.text()


@pytest.mark.browser
def test_a_chapters_hook_that_throws_or_is_missing_writes_no_stills(tmp_path):
    root = site(tmp_path, "frozen")
    r = run(root, tmp_path, steps=2)
    assert not (tmp_path / "out" / "stills").exists() and "page without WebGL" not in r.text()
    assert r.stills == {"note": "no window.__site.chapters() and setProgress()"}
    page = (root / "index.html").read_text().replace(
        "</script>", "window.__site = { setProgress() {}, chapters() { throw new Error('fixture chapters boom'); } };</script>")
    (root / "index.html").write_text(page)
    r = run(root, tmp_path / "again", steps=2)
    assert not (tmp_path / "again" / "out" / "stills").exists() and "page without WebGL" not in r.text()
    assert "threw: fixture chapters boom" in r.stills["note"]


# --------------------------------------------------------------------------- run 3: a picture that does not travel,
# one scale throughout, labels too small where the shot is about them, layers


def _box(name: str, box: list[float], kind: str = "prop", subject: bool | None = True, cut: float = 0.0,
         hidden: float | None = 0.0, px: tuple[int, int] | None = None, size: tuple[int, int] = (1440, 900)) -> dict:
    """One measured subject with its own box (``px`` from the box at ``size`` unless given)."""
    px = px or (round((box[2] - box[0]) * size[0]), round((box[3] - box[1]) * size[1]))
    return {"name": name, "kind": kind, "subject": subject, "box": box, "px": list(px), "cut": cut, "edges": {},
            "hidden": hidden, "under": ""}


def _visit(name: str, rows: list[list[dict] | None], tmp_path: Path, n: int | None = None) -> sc.Visit:
    n = n or len(rows)
    v = sc.Visit(name, loaded=True, load_s=1.0)
    v.steps = [sc.Step(i, i / max(n - 1, 1), 0, frame=str(tmp_path / f"{name}_{i:02d}.jpg"), subjects=r)
               for i, r in enumerate(rows) if r != "missing"]
    return v


# The shot's subject at each step of run 3 (benchmarks/results/site_run3_plugin), as its record (round 8's
# frames3/run3/report.json) has it: on the phone the gate fitted into one place above the panel for steps 1 to 5; on
# the desktop the subject moved between every two steps. The last step, a wide shot of all three props, names none.
RUN3_PHONE = [("gate", [0.1852, 0.1724, 0.8148, 0.4663]), ("gate", [0.1847, 0.1724, 0.8153, 0.4663]),
              ("gate", [0.1897, 0.1724, 0.8103, 0.4663]), ("gate", [0.1919, 0.1724, 0.8081, 0.4663]),
              ("gate", [0.18, 0.1737, 0.82, 0.465]), ("vault", [0.2143, 0.1724, 0.7857, 0.4663]),
              ("anchor", [0.2327, 0.1724, 0.7673, 0.4663]), ("anchor", [0.2063, 0.1724, 0.7937, 0.4663])]
RUN3_DESKTOP = [("gate", [0.5379, 0.2748, 0.8844, 0.835]), ("gate", [0.519, 0.2507, 0.9044, 0.8728]),
                ("gate", [0.607, 0.2643, 0.9368, 0.8051]), ("gate", [0.5308, 0.302, 0.8273, 0.7918]),
                ("gate", [0.4411, 0.2709, 0.7941, 0.8273]), ("vault", [0.5357, 0.2517, 0.8719, 0.8505]),
                ("anchor", [0.7092, 0.3014, 0.9988, 0.8528]), ("anchor", [0.5221, 0.2455, 0.8905, 0.8837])]
RUN3_LAST = {"desktop": [("gate", [0.4595, 0.43, 0.5808, 0.6154]), ("gate-label", [0.4834, 0.4067, 0.5502, 0.4236], 15),
                         ("vault-label", [0.6851, 0.4057, 0.7413, 0.4184], 11),
                         ("anchor-label", [0.8756, 0.4247, 0.9348, 0.4415], 15)],
             "phone": [("gate", [0.0966, 0.2753, 0.2948, 0.3628]), ("gate-label", [0.1357, 0.2643, 0.2449, 0.2723], 7),
                       ("vault-label", [0.4653, 0.2638, 0.5571, 0.2698], 5),
                       ("anchor-label", [0.7765, 0.2728, 0.8732, 0.2807], 7)]}


def _run3(name: str, tmp_path: Path) -> sc.Visit:
    rows: list = []
    for prop, box in RUN3_PHONE if name == "phone" else RUN3_DESKTOP:
        label = [box[0] + 0.1, box[1] - 0.05, box[2] - 0.1, box[1] - 0.01]
        rows.append([_box(prop, box), _box(prop + "-label", label, "label", px=(130, 22 if name == "phone" else 46))])
    last = RUN3_LAST[name]
    rows.append([_box(last[0][0], last[0][1], subject=None)]
                + [_box(n, b, "label", subject=None, px=(80, h)) for n, b, h in last[1:]])
    return _visit(name, rows, tmp_path)


def test_run3_record_the_phone_picture_does_not_travel_the_desktop_does(tmp_path):
    """Run 3: on the phone the gate kept its box (edges within 0.7 % of the window) over steps 1 to 5 while the page
    scrolled half its length; the frames still differed in small things, so the pixel comparison said nothing. On the
    desktop the subject moved by 3.8 % or more between any two steps, and nothing is said."""
    rep = sc.SiteReport("site", tmp_path, steps=9)
    rep.visits = {"desktop": _run3("desktop", tmp_path), "phone": _run3("phone", tmp_path)}
    runs = sc.held_runs(rep.visits["phone"].steps)
    assert [(r["name"], [s.index for s in r["steps"]]) for r in runs] == [("gate", [0, 1, 2, 3, 4])]
    assert 0.006 < runs[0]["moved"] < 0.007 and sc.held_runs(rep.visits["desktop"].steps) == []
    held = [n for n in rep.noticed() if "does not travel" in n]
    assert held == [
        "Phone: the picture does not travel: the prop 'gate', the shot's subject, keeps its place and size on screen "
        "at steps 1 to 5 of 9 (0% to 50%, 50% of the scroll): its box moves by at most 1% of the window while the "
        "page scrolls on. Let the camera move through those chapters (a push in, a turn round the subject, a new "
        "angle per chapter) instead of framing the subject the same way at every step. Look at phone_00.jpg to "
        "phone_04.jpg."], held
    # the closing wide shot names no subject, and its three labels are 5 to 15 px tall: each is named, per size
    small = [n for n in rep.noticed() if "too small to read at" in n]
    assert len(small) == 6 and all("step 9 of 9 (100%)" in n for n in small)
    assert ("Desktop: the 3D label 'vault-label' is in the frame where the shot is about it, but too small to read at "
            "step 9 of 9 (100%) (at worst 11 px tall, under 16): make it larger there or frame it closer. Look at "
            "desktop_08.jpg.") in small
    text = rep.text()
    measures = text[text.index("Measures:"):]
    assert ("- Desktop: one scale throughout: the shot's subject fills 49% to 64% of the window (its height or width, "
            "whichever it fills more) at the 8 steps whose subject the page names, 8 of them within a quarter of the "
            "median (56%): every picture is the same kind of shot. A story usually changes scale between chapters "
            "(wide to set the scene, close for a detail).") in measures
    assert "does not travel" not in measures and len(text.splitlines()) <= 60


def test_a_held_subject_is_named_only_over_a_real_stretch_of_the_scroll(tmp_path):
    still = [0.3, 0.2, 0.7, 0.8]
    near = [0.31, 0.21, 0.71, 0.79]                       # within 2 % of the window: the same box
    away = [0.33, 0.2, 0.73, 0.8]                         # 3 %: a camera that moves, if slowly

    def held(rows, n=None):
        runs = sc.held_runs(_visit("phone", rows, tmp_path, n).steps)
        return [(r["name"], [s.index for s in r["steps"]]) for r in runs]

    # two steps at the top of the page: a page may rest there for a step
    assert held([[_box("hero", still)], [_box("hero", still)], [_box("hero", away)], [_box("hero", still)]]) == []
    # three steps over half the scroll: named; a slow drift counts from the run's first box, not step by step
    assert held([[_box("hero", still)], [_box("hero", near)], [_box("hero", still)], [_box("hero", away)], None]) == [
        ("hero", [0, 1, 2])]
    drift = [[_box("hero", [0.3 + 0.015 * i, 0.2, 0.7 + 0.015 * i, 0.8])] for i in range(5)]
    assert held(drift) == []
    # three neighbouring steps of 25 cover 8 % of the scroll: one chapter's rest, not named; seven are
    rows = [[_box("hero", still)]] * 3 + [[_box("hero", [0.1 * (i % 2), 0.1, 0.5, 0.5])] for i in range(22)]
    assert held(rows) == []
    assert held([[_box("hero", still)]] * 7 + rows[7:]) == [("hero", [0, 1, 2, 3, 4, 5, 6])]
    # a step missing (the check ran out of time) breaks the run; so does another subject in the same place
    assert held([[_box("hero", still)], "missing", [_box("hero", still)], [_box("hero", still)]], 4) == []
    assert held([[_box("hero", still)], [_box("other", still)], [_box("hero", still)], [_box("hero", still)]]) == []
    # only what the page names as the shot's subject: a prop in the background or a page that names nothing
    moving = [[_box("hero", still, subject=False), _box("x", [0.1 * i, 0.2, 0.3 + 0.1 * i, 0.5])] for i in range(3)]
    assert held(moving) == []
    assert held([[_box("hero", still, subject=None)]] * 5) == []
    assert held([[_box("HERO", still, "label")]] * 5) == []
    # a layer is a subject like a prop, and is called a layer
    rep = sc.SiteReport("site", tmp_path, steps=3)
    rep.visits = {"phone": _visit("phone", [[_box("slabs", still, "layer")]] * 3, tmp_path)}
    assert any(n.startswith("Phone: the picture does not travel: the layer 'slabs', the shot's subject")
               for n in rep.noticed())
    # a page without the hook says nothing
    rep.visits = {"phone": _visit("phone", [None] * 3, tmp_path)}
    assert not [n for n in rep.noticed() if "travel" in n]


def test_one_scale_is_a_measure_said_only_when_most_steps_share_it(tmp_path):
    def line(sizes, subject=True):
        rows = [[_box("hero", [0.5 - s / 2, 0.5 - s / 2, 0.5 + s / 2, 0.5 + s / 2], subject=subject)] for s in sizes]
        rep = sc.SiteReport("site", tmp_path, steps=len(rows))
        rep.visits = {"desktop": _visit("desktop", rows, tmp_path)}
        return rep.scale_line(rep.visits["desktop"]), rep

    said, rep = line([0.5, 0.55, 0.6, 0.5, 0.45, 0.2])            # 5 of 6 within a quarter of the median
    assert "fills 20% to 60% of the window" in said and "at the 6 steps whose subject the page names, 5 of them" in said
    assert said in rep.text() and not [n for n in rep.noticed() if "one scale" in n]   # a measure, not a fault
    assert line([0.8, 0.5, 0.25, 0.5, 0.12, 0.6])[0] == ""        # a story that changes scale
    assert line([0.5, 0.5, 0.5])[0] == ""                         # too few steps to say
    assert line([0.5] * 6, subject=None)[0] == ""                  # no subject named: nothing guessed
    # two subjects of one shot are one picture: the box around both
    rows = [[_box("a", [0.1, 0.4, 0.3, 0.5]), _box("b", [0.6, 0.4, 0.8, 0.5]),
             _box("c", [0, 0, 1, 1], subject=False)]] * 4
    assert list(sc.subject_scale(_visit("desktop", rows, tmp_path).steps).values()) == [pytest.approx(0.7)] * 4


def test_a_label_too_small_where_the_shot_is_about_it_is_named_and_one_in_the_background_is_not(tmp_path):
    rep = sc.SiteReport("site", tmp_path, steps=4)
    big, small = (300, 40), (60, 9)
    rows = [[_box("hero", [0.3, 0.3, 0.6, 0.6]), _box("HERO", [0.3, 0.2, 0.5, 0.25], "label", px=big),
             _box("FAR", [0.8, 0.2, 0.9, 0.21], "label", subject=False, px=small),           # background: not named
             _box("WIDE", [0.3, 0.2, 0.5, 0.25], "label", subject=False, px=big)],
            [_box("hero", [0.3, 0.3, 0.6, 0.6]), _box("HERO", [0.3, 0.2, 0.5, 0.25], "label", px=small),
             _box("FAR", [0.8, 0.2, 0.9, 0.3], "label", subject=False, px=big)],
            [_box("hero", [0.3, 0.3, 0.6, 0.6]), _box("HERO", [0.3, 0.2, 0.5, 0.25], "label", px=(60, 12)),
             _box("CUT", [0.9, 0.2, 1.1, 0.21], "label", cut=0.5, px=small)],                # cut: that line says so
            [_box("hero", [0.3, 0.3, 0.6, 0.6], subject=None),                               # a wide shot names none
             _box("WIDE", [0.3, 0.2, 0.5, 0.21], "label", subject=None, px=(60, 14)),
             _box("HERO", [0.3, 0.2, 0.5, 0.25], "label", subject=None, px=big)]]
    rep.visits = {"desktop": _visit("desktop", rows, tmp_path)}
    lines = [n for n in rep.noticed() if "3D label" in n]
    assert lines == [
        "Desktop: the 3D label 'HERO' is in the frame where the shot is about it, but too small to read at steps 2 "
        "(33%) and 3 (67%) of 4 (at worst 9 px tall, under 16): make it larger there or frame it closer. Look at "
        "desktop_01.jpg.",
        "Desktop: the 3D label 'CUT' runs out of the frame at the frame's edge at step 3 of 4 (67%) (at worst 50% of "
        "it outside); it is never taller than 9 px at any step (its largest is at step 3 of 4 (67%)), too small to "
        "read. Look at desktop_02.jpg.",
        "Desktop: the 3D label 'WIDE' is in the frame where the shot is about it, but too small to read at step 4 of "
        "4 (100%) (at worst 14 px tall, under 16): make it larger there or frame it closer. Look at desktop_03.jpg."
    ], lines
    # a page that never names a subject: only the old rule (never readable at any step), as before
    plain = [[{**s, "subject": None} for s in r] for r in rows]
    rep.visits = {"desktop": _visit("desktop", plain, tmp_path)}
    assert [n[:30] for n in rep.noticed() if "3D label" in n] == ["Desktop: the 3D label 'CUT' ru"]
    assert sc.small_labels(rep.visits["desktop"].steps) == {}


def test_a_layer_is_measured_and_named_as_a_prop_is():
    row = {"name": "slabs", "kind": "layer", "box": [0.5, 0.2, 1.3, 0.6], "px": [1152, 360], "cut": 0.375,
           "edges": {"right": 0.375}, "hidden": 0.2, "under": "div#panel", "subject": True}
    s = sc._subject(row)
    assert s["kind"] == "layer" and sc.subject_faults(s) == (True, True)          # a prop's thresholds, 30 % and 15 %
    assert sc.subject_faults({**s, "cut": 0.25, "hidden": 0.12}) == (False, False)
    import asyncio

    class Page:
        async def evaluate(self, _):
            return {"list": [row, {**row, "name": "hero", "kind": "prop", "subject": False}]}

    got, _ = asyncio.run(sc._Visitor.subjects(None, Page()))
    assert [(x["name"], x["kind"], x["background"]) for x in got] == [("slabs", "layer", False), ("hero", "prop", True)]
    rep = sc.SiteReport("site", Path("."), steps=2)
    v = sc.Visit("desktop", loaded=True, load_s=1.0)
    v.steps = [sc.Step(0, 0.0, 0, frame="desktop_00.jpg", subjects=got), sc.Step(1, 1.0, 0, frame="desktop_01.jpg")]
    rep.visits = {"desktop": v}
    assert [n for n in rep.noticed() if "slabs" in n] == [
        "Desktop: the layer 'slabs' lies under div#panel at step 1 of 2 (0%) (at worst 20% of what is in the frame); "
        "it runs out of the frame at the right edge at step 1 of 2 (0%) (at worst 38% of it outside). Look at "
        "desktop_00.jpg."]


@pytest.mark.browser
def test_a_pinned_subject_a_small_label_and_layers_through_the_page_hook(tmp_path):
    r = run(site(tmp_path, "subjects_pinned"), tmp_path, steps=5)
    d = r.visits["desktop"]
    kinds = {s["name"]: s["kind"] for st in d.steps for s in st.subjects}
    assert kinds == {"hero": "prop", "HERO LABEL": "label", "slabs": "layer", "deck": "layer"}, kinds
    assert d.steps[0].subjects[2]["hidden"] == 1.0  # the slabs lie under the panel, measured
    noticed = r.noticed()
    held = [n for n in noticed if "does not travel" in n]
    assert len(held) == 1 and ("the prop 'hero', the shot's subject, keeps its place and size on screen at steps 1 "
                               "to 3 of 5 (0% to 50%, 50% of the scroll): its box moves by at most 0% of the window"
                               in held[0]), held
    assert [n.split(":")[1].strip() for n in noticed if "too small to read" in n] == [
        "the 3D label 'HERO LABEL' is in the frame where the shot is about it, but too small to read at step 5 of 5 "
        "(100%) (at worst 9 px tall, under 16)"]
    assert any(n.startswith("Desktop: the layer 'deck' runs out of the frame at the right edge at step 5 of 5 (100%)")
               for n in noticed), noticed
    assert "slabs" not in r.text()                   # a layer in the background, under the panel: not named
    assert "Desktop: one scale throughout: the shot's subject fills 40% to 55% of the window" in r.text()
    assert "5 steps whose subject the page names, 4 of them" in r.text()


@pytest.mark.browser
def test_the_page_knows_it_is_drawing_stills_and_only_then(tmp_path):
    """The contract of round 8: data-agy3d-still is on <html> from before the first still until after the last, also
    when a still fails, so a page may frame its subject for the whole canvas there; it is gone afterwards."""
    import asyncio

    from playwright.async_api import async_playwright

    from antigravity3d.adapters.engines.web_three import WebThreeValidator, _launch_args

    root = site(tmp_path, "still_mode")
    out = tmp_path / "out"
    out.mkdir()

    async def go():
        srv = sc._SiteServer(root)
        try:
            async with async_playwright() as pw:
                b = await pw.chromium.launch(executable_path=WebThreeValidator().binary(), args=list(_launch_args()))
                page = await b.new_page(viewport={"width": 640, "height": 400})
                await page.add_init_script(sc._probe_script(False))
                await page.goto(srv.url)
                before = await page.evaluate("document.documentElement.hasAttribute('data-agy3d-still')")
                vis = sc._Visitor("desktop", srv.url, srv.origin, out, sc._Clock(time.monotonic(), 120.0), "block")
                stills = await vis.stills(page)
                after = await page.evaluate("[document.documentElement.hasAttribute('data-agy3d-still'), "
                                            "window.__seen, window.__changes]")
                await b.close()
                return before, stills, after
        finally:
            srv.close()

    before, stills, (after, seen, changes) = asyncio.run(go())
    assert before is False and after is False and changes == [True, False]
    assert {t for t, _ in seen} == {1, 2, 3} and all(on for _, on in seen), seen   # the third, after the failed second
    assert [x["id"] for x in stills["made"]] == ["one", "three"] and "fixture still boom" in stills["skipped"][0]["why"]
    for x in stills["made"]:
        assert all(abs(a - b) <= 12 for a, b in zip(_pixel(out / "stills" / x["file"]), (0, 255, 0))), x
