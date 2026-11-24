"""Every example in the scroll-3d-website skill's library guide and recipes runs against the versions the guide names.

The guide (``skills/scroll-3d-website/libraries.md``, and ``recipes.md`` beside it) holds each example as a fenced block whose first line is
``// example: <name>`` (``(node)`` after the name for a Node script) and, on the lines after the block, what a run must
show, one ``<!-- check: ... -->`` line each: ``picture`` (the window shows something), ``screen-draws N`` (at most N
draw calls to the page per frame), ``js <expression>`` (true in the page), ``node <expression>`` (true of the
script's last line of output, as ``out``), ``warning <text>`` (a console warning the guide says to expect). This test reads them from the
guide itself, so an example and its check are edited in one place.

The libraries are not the repository's: they are a workspace's ``node_modules`` (the round-eight seed: three 0.187.0,
gsap, lenis, postprocessing, n8ao, troika-three-text, three-custom-shader-material, three-mesh-bvh, detect-gpu,
React 19 with @react-three/fiber and drei, glTF-Transform, meshoptimizer, sharp, Vite 8). Point ``AGY3D_SITE_SEED`` at
such a workspace (the folder that holds ``node_modules``); without it the browser tests skip. The format checks run
always.

Each browser example is built with the workspace's Vite into a temporary project made of the starter's page with the
example as its script (the workspace is linked, never written to), served under a sub-path from a routed fake origin
and opened in headless Chromium. A run passes when nothing failed to load, nothing went to another host, the console
holds no error or warning, and the example's checks hold.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.plugin_install import bundle_dir

SKILL = bundle_dir() / "skills" / "scroll-3d-website"
GUIDES = (SKILL / "libraries.md", SKILL / "recipes.md")
REPO = Path(__file__).resolve().parent.parent
SEED = Path(os.environ["AGY3D_SITE_SEED"]) if os.environ.get("AGY3D_SITE_SEED") else None
ORIGIN = "http://examples.test"
SUB = "/a/sub/path/"
NODE_INPUT = REPO / "examples" / "straitjacket-film" / "site" / "assets" / "context_frame.glb"
DRIVER_NOISE = re.compile(r"GL Driver Message|GPU stall due to ReadPixels|Automatic fallback to software WebGL")
_BLOCK = re.compile(r"```(js|jsx)\n// example: ([a-z0-9-]+)( \(node\))?\n(.*?)```\n((?:<!-- check: .*? -->\n)+)", re.S)

# Draw calls issued while the page's own framebuffer is bound, per animation frame: the scene's main pass, without the
# environment map, shadow maps or effect buffers that render to targets.
_COUNTER = """(() => {
  const g = { screen: 0, frames: [] };
  window.__gl = g;
  for (const C of [window.WebGL2RenderingContext, window.WebGLRenderingContext]) {
    if (!C) continue;
    const bind = C.prototype.bindFramebuffer;
    C.prototype.bindFramebuffer = function (target, fb) { this.__toScreen = !fb; return bind.call(this, target, fb); };
    for (const f of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced', 'drawRangeElements']) {
      const o = C.prototype[f];
      if (o) C.prototype[f] = function (...a) { if (this.__toScreen !== false) g.screen++; return o.apply(this, a); };
    }
  }
  const tick = () => { if (g.screen) { g.frames.push(g.screen); g.screen = 0; } requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
})();"""


def examples() -> list[dict]:
    text = "\n".join(g.read_text(encoding="utf-8") for g in GUIDES)
    out = []
    for lang, name, node, code, check in _BLOCK.findall(text):
        out.append({"name": name, "lang": lang, "node": bool(node), "code": code,
                    "checks": re.findall(r"<!-- check: (.*?) -->", check)})
    return out


EXAMPLES = examples()


def test_every_fenced_example_of_the_guide_is_found_with_a_check() -> None:
    fences = re.findall(r"```(?:js|jsx)\n// example: ", "\n".join(g.read_text(encoding="utf-8") for g in GUIDES))
    assert len(fences) == len(EXAMPLES) >= 12
    names = [e["name"] for e in EXAMPLES]
    assert len(set(names)) == len(names)
    for e in EXAMPLES:
        for c in e["checks"]:
            assert re.fullmatch(r"picture|screen-draws \d+|js .+|node .+|warning .+", c, re.S), (e["name"], c)
    # the libraries the brief asks the guide to cover each have an example
    assert {"three-webgl", "three-webgpu-tsl", "gsap-scrolltrigger", "lenis", "postprocessing-n8ao", "troika-text",
            "custom-shader-material", "three-mesh-bvh", "detect-gpu", "r3f-drei", "gltf-transform"} <= set(names)


@pytest.fixture(scope="module")
def seed() -> Path:
    if SEED is None:
        pytest.skip("AGY3D_SITE_SEED is not set: no workspace with the guide's libraries to run the examples against")
    if not (SEED / "node_modules" / "vite").is_dir():
        pytest.skip(f"{SEED} has no node_modules/vite")
    return SEED


def _font() -> Path:
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if Path(p).is_file():
            return Path(p)
    pytest.skip("no TrueType font on this machine for the troika example")


def _build(seed: Path, example: dict, root: Path) -> Path:
    """A project: the starter's page and modules, the example as its script, the workspace's node_modules linked."""
    project = root / example["name"]
    shutil.copytree(SKILL / "starter", project)
    script = f"example.{example['lang']}"
    (project / script).write_text(example["code"], encoding="utf-8")
    html = (project / "index.html").read_text()
    (project / "index.html").write_text(html.replace('src="./src/main.js"', f'src="./{script}"'))
    (project / "node_modules").symlink_to(seed / "node_modules", target_is_directory=True)
    public = project / "public"
    (public / "fonts").mkdir(parents=True)
    shutil.copy(_font(), public / "fonts" / "label.ttf")
    shutil.copytree(seed / "node_modules" / "detect-gpu" / "dist" / "benchmarks", public / "detect-gpu")
    react = "import react from '@vitejs/plugin-react';\n" if example["lang"] == "jsx" else ""
    (project / "vite.config.js").write_text(
        f"import {{ defineConfig }} from 'vite';\n{react}"
        f"export default defineConfig({{ base: './', cacheDir: {json.dumps(str(root / 'vite-cache'))}, logLevel: 'error'"
        f"{', plugins: [react()]' if react else ''}, build: {{ chunkSizeWarningLimit: 4000 }} }});\n")
    r = subprocess.run(["node", str(project / "node_modules" / "vite" / "bin" / "vite.js"), "build"], cwd=project,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return project / "dist"


@pytest.fixture(scope="module")
def browser(seed):
    from antigravity3d.adapters.engines.web_three import WebThreeValidator, _launch_args

    v = WebThreeValidator()
    if why := v._unavailable():
        pytest.skip(why)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=v.binary(), args=list(_launch_args()))
        yield b
        b.close()


def _picture(page) -> float:
    """Share of the window, the page's text hidden, that differs clearly from its most common colour."""
    from PIL import Image

    page.add_style_tag(content="#story{visibility:hidden}")
    frame = np.asarray(Image.open(io.BytesIO(page.screenshot())).convert("RGB")).astype(np.int16).reshape(-1, 3)
    colours, counts = np.unique(frame // 4, axis=0, return_counts=True)
    return float((np.abs(frame - colours[counts.argmax()] * 4).max(axis=1) > 24).mean())


@pytest.mark.browser
@pytest.mark.parametrize("example", [e for e in EXAMPLES if not e["node"]], ids=lambda e: e["name"])
def test_browser_example_runs_against_the_seed(example, seed, browser, tmp_path) -> None:
    dist = _build(seed, example, tmp_path)

    def serve(route):
        path = route.request.url[len(ORIGIN):].split("?")[0]
        f = dist / (path[len(SUB):] or "index.html") if path.startswith(SUB) else None
        if f is None or ".." in path or not f.is_file():
            return route.fulfill(status=404, body="not found")
        kind = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json",
                ".ttf": "font/ttf"}.get(f.suffix, "application/octet-stream")
        route.fulfill(status=200, body=f.read_bytes(), headers={"content-type": kind})

    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    problems: list[str] = []
    ctx.route("**/*", lambda route: serve(route) if route.request.url.startswith(ORIGIN) else
              (problems.append(f"request to another host: {route.request.url}"), route.abort()))
    ctx.add_init_script(_COUNTER)
    page = ctx.new_page()
    expected = [c[len("warning "):] for c in example["checks"] if c.startswith("warning ")]
    page.on("console", lambda m: m.type in ("error", "warning") and not DRIVER_NOISE.search(m.text)
            and not (m.type == "warning" and any(w in m.text for w in expected)) and problems.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: r.url.startswith(ORIGIN) and problems.append(f"failed: {r.url}"))
    try:
        page.goto(ORIGIN + SUB, wait_until="networkidle")
        page.wait_for_timeout(2500)
        for check in example["checks"]:
            if check == "picture":
                share = _picture(page)
                # 1 %: about a 113 px square of this window (a line of in-scene text covers 2 %)
                assert share > 0.01, f"{example['name']}: the window shows almost nothing ({share:.3f})"
            elif check.startswith("screen-draws "):
                frames = page.evaluate("window.__gl.frames")
                assert frames and frames[-1] <= int(check.split()[1]), f"{example['name']}: draws per frame {frames}"
            elif check.startswith("js "):
                assert page.evaluate(check[3:]) is True, f"{example['name']}: {check}"
        assert problems == [], problems
    finally:
        ctx.close()


@pytest.mark.parametrize("example", [e for e in EXAMPLES if e["node"]], ids=lambda e: e["name"])
def test_node_example_runs_against_the_seed(example, seed, tmp_path) -> None:
    (tmp_path / "node_modules").symlink_to(seed / "node_modules", target_is_directory=True)
    script = tmp_path / "example.mjs"
    script.write_text(example["code"], encoding="utf-8")
    r = subprocess.run(["node", str(script), str(NODE_INPUT), str(tmp_path / "out.glb")], capture_output=True, text=True,
                       timeout=300, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    for check in example["checks"]:
        assert check.startswith("node ")
        expr = check[5:]
        verdict = subprocess.run(["node", "-e", f"const out = {json.dumps(out)}; process.exit(({expr}) ? 0 : 1)"])
        assert verdict.returncode == 0, (expr, out)
