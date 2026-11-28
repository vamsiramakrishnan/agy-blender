# Libraries for a scroll-driven 3D site

What each library is for, when not to use it, the version this guide was checked against, and one example that ran.
Use only what the project already has installed: a sandbox without network cannot add a package, so read the
project's `package.json` first and pick from what is there.

Last verified 2026-11-24. Versions are those of the round-eight seed workspace (read from the npm registry that day).
Every example below was built with Vite 8.3.8 and run in headless Chromium (software WebGL) against exactly these
versions, with no error and no warning in the console; the Node example ran on Node 22. Each example is a whole file
that replaces the starter's `src/main.js` in the starter's page (`starter/index.html`: a `<canvas id="stage">` and
`<section data-chapter>` chapters), so it may import the starter's own modules. The line after each example says what
the run checked. To run them again: the repository's `tests/test_scroll_site_libraries.py`, with
`AGY3D_SITE_SEED` pointing at a workspace that has these packages installed.

| Library | Version | Use it for | Not for |
|---|---|---|---|
| three | 0.187.0 | the scene: `WebGLRenderer` for a site | a renderer you write yourself |
| gsap + ScrollTrigger | 3.15.1 | page effects tied to scroll: panels, pinning, counters | the scene's state (see the starter) |
| lenis | 1.3.28 | smooth wheel scrolling on a desktop | touch (it leaves touch native by default; keep it so) |
| postprocessing + n8ao | 6.39.6, 2.1.0 | ambient occlusion, a little bloom, tone mapping, SMAA | a phone at full resolution; effects that hide a weak scene |
| troika-three-text | 0.53.1 | sharp text placed in the 3D scene (labels on parts) | paragraphs: those stay HTML |
| three-custom-shader-material | 6.4.0 | changing a standard material in a shader and keeping its light and shadows | a full-screen effect |
| three-mesh-bvh | 0.9.18 | fast picking and hover on dense models | a scene of a few boxes |
| detect-gpu | 5.0.70 | choosing a quality level before the first frame | deciding whether to show 3D at all |
| @react-three/fiber + drei | 9.8.2, 10.7.9 | a project that is already React | a plain page (adds React for nothing) |
| glTF-Transform, meshoptimizer, sharp, ktx2-encoder | 4.5.1, 1.3.0, 0.35.5, 0.6.0 | shrinking a model that did not come from this plugin | the plugin's own props: `install_into_project` already does it |

## three.js 0.187: the renderer

Use `WebGLRenderer`. Everything else in this guide (postprocessing, n8ao, troika, custom shader material, drei's
helpers) is written for it. What moved since 0.170, the version many examples online and older workspaces use:

- `PCFSoftShadowMap` is removed: the renderer warns and uses `PCFShadowMap`, whose `shadow.radius` now softens.
- `THREE.Clock` is deprecated (it warns): use `THREE.Timer` (in 0.170 it was an add-on). A scroll site rarely
  needs either: the scene is a function of scroll, not of time.
- WebGL 1 is gone (since 0.163): check for `webgl2`, not `webgl`, before loading the scene.
- Add-ons are imported from `three/addons/...` (the same files as `three/examples/jsm/...`).

The examples here size the renderer once from the window, to stay short. In the page, size it from the canvas's own
box and follow that box with a `ResizeObserver`, as the starter's `stage.js` does. Why: the starter's canvas is
`100lvh` tall, which a phone's address bar does not change, while `innerHeight` does; run 2 drew at `innerHeight` and
the browser stretched the picture to the taller box.

Many small things (slabs, tokens, rows, particles) are one `InstancedMesh`: one geometry, one material, a matrix per
copy, one draw call. Two thousand separate meshes are two thousand draw calls and a slow phone.

```js
// example: three-webgl
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { groundLook } from './src/stage.js';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
renderer.toneMapping = THREE.NeutralToneMapping;
renderer.shadowMap.enabled = true; // PCFShadowMap, the default

const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
pmrem.dispose();

scene.environmentIntensity = 0.65; // the starter's light (below): a key, a rim from behind, an environment
const rim = new THREE.DirectionalLight('#dce6f5', 0.85);
rim.position.set(10, 6, -8);
scene.add(rim);
const key = new THREE.DirectionalLight('#ffffff', 2.35);
key.position.set(-4, 8, 5);
key.castShadow = true;
key.shadow.radius = 3;
key.shadow.camera.left = key.shadow.camera.bottom = -6;
key.shadow.camera.right = key.shadow.camera.top = 6;
scene.add(key);

// a lit ground for the slabs' shadows to fall on, not washed out by their light: the starter's colour through its
// groundLook (the light section below)
const ground = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.MeshStandardMaterial({ ...groundLook(scene, '#474e5a'), roughness: 0.95 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

// 2,000 slabs, one draw call (and one more for the shadow map)
const COUNT = 2000;
const slabs = new THREE.InstancedMesh(new THREE.BoxGeometry(0.08, 0.04, 0.3),
  new THREE.MeshStandardMaterial({ color: '#c9c4b8', roughness: 0.5 }), COUNT);
const m = new THREE.Matrix4();
for (let i = 0; i < COUNT; i++) {
  m.makeTranslation((i % 50) * 0.12 - 3, 0.02, Math.floor(i / 50) * 0.35 - 7);
  slabs.setMatrixAt(i, m);
}
slabs.castShadow = true;
scene.add(slabs);

const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 100);
camera.position.set(5, 4, 7);
camera.lookAt(0, 0, -1);
renderer.render(scene, camera); // once: draw again only when something changes
```
<!-- check: picture -->
<!-- check: screen-draws 3 -->

### WebGPURenderer and TSL: when they are worth it

`WebGPURenderer` (import from `three/webgpu`) runs on WebGPU where the browser has it and on WebGL 2 where it does
not; its materials are written in TSL (`three/tsl`), node functions instead of GLSL strings, and its post-processing
is `RenderPipeline` (renamed from `PostProcessing` in 0.183; the old name warns). It is worth it for compute work:
a hundred thousand particles moved on the GPU, a simulation. It is not worth it for a site of a few props, because
pmndrs postprocessing, n8ao, troika, custom shader material and drei's shadow helpers do not work with it.
Call `await renderer.init()` before the first `render` (`renderAsync` is deprecated since 0.181). Where the browser
has no WebGPU (the headless check's Chromium has none) it warns once that it runs on WebGL 2: that is expected.

```js
// example: three-webgpu-tsl
import * as THREE from 'three/webgpu';
import { color, mix, normalLocal, positionLocal, sin, uniform } from 'three/tsl';

const renderer = new THREE.WebGPURenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
await renderer.init();

const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 2));

// A uniform the page sets from scroll: the material's look is a function of it, never of time.
const progress = uniform(0.35);
const material = new THREE.MeshStandardNodeMaterial({ roughness: 0.4 });
material.colorNode = mix(color('#2b6cb0'), color('#e0a33a'), progress);
material.positionNode = positionLocal.add(normalLocal.mul(sin(positionLocal.y.mul(10).add(progress.mul(6.28))).mul(0.05)));
scene.add(new THREE.Mesh(new THREE.IcosahedronGeometry(1, 24), material));

const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(0, 0.5, 5);
renderer.render(scene, camera);
```
<!-- check: picture -->
<!-- check: warning WebGPU is not available, running under WebGL2 backend -->

## Light that shows form, and colours that stay the design's

The starter lights the scene with three things (`LIGHT` in its stage.js): a key from above at the front left that
casts the shadow (2.35), a rim from behind that draws the edges (0.85), and an environment for reflections and fill
(0.65). Leave it as it is unless the frames give a reason. Why these numbers: they are run 2's, whose 3D a blind
judge preferred to run 4's and to run 5's in six of six verdicts each, and the same site was then judged under two
lights. Run 4's site, machines in the page's own dark steel, built under a dimmer light (a key of 1.8, the
environment at 0.25, no rim), was preferred under this one in six of six ("dark metals lose detail in shadow"). Run
5's site, light grey machines, drew three each, by the order shown. The dimmer light had been set after run 3 so that
a colour would show as its hex on a face turned up.

Light here is the factor F a face receives: the key's intensity times the cosine of its angle over pi, plus
`environmentIntensity` times what the environment gives that face. Measured with the starter's `RoomEnvironment`: a
face turned up gets 0.28 of the key and 1.84 of the environment (a face turned to the camera 1.91, a side wall 1.0),
so the starter lights a top at about 2. Under `NeutralToneMapping` a lit colour reaches the screen as itself only
under the tone mapper's knee: each channel of albedo times light below about 0.76 (linear) passes through; above it
the colour is compressed towards white. At F = 2 that is every channel above about 165 of 255. So:

- Give a prop's large surfaces the design's dark and middle values and let the light carry them. Measured on a face
  turned up, under the starter's light: a steel of #242931 at three quarters metal comes out #1F2835, its own colour,
  and a paint of #242931 comes out #3E434B. A paint of #7A828C comes out #9FA9B6, and one of #B8C1CC comes out
  #DBE5F2, most of the way to white. Why: run 5 gave its machines light grey bodies (#7A838E to #B8C1CC) on this
  round's earlier advice, and nine of the sixteen blind verdicts on craft that went against it call them flat,
  matte or grey; runs 2, 3 and 4 used #242931 for the bodies. `check_site` says when a shot's subject is hard to
  tell from the page.
- A part in one of the design's accent colours is lit, with its colour divided by the light: `color: lit('#F0B429')`
  (stage.js). Measured: the top of an amber part comes out #E3AC37 and a wall #D19F3A, the colour with its form;
  the plain colour comes out #F8C15B, washed. Only a flat mark that must match the panel's text exactly (a line, a
  tag, a status dot) is unlit: `MeshBasicMaterial({ color, toneMapped: false })` gives #F0B429, and `emissive` on a
  black `color` #EDB33B. Why: a block drawn unlit has no form. Run 5 drew the coloured blocks of its lanes that
  way, on this guide's earlier advice, and the blind judge calls them "flat unshaded blocks on the floor".
- The ground is lit scenery for shadows to fall on, not one of the page's surfaces: keep the starter's `--ground`
  (#474e5a, which shows at about 60 of 255 near the camera on a page at 18), or a colour of your own that shows
  between 50 and 70 there, and let the fog take it to the page's colour at the horizon. Do not give it the design's
  background or card colour. Why: a shadow cannot darken a floor that is already as dark as the page, so the props
  float. Run 6's floor was the design's #15181D, which shows at 19; the same site rebuilt with the floor at 61 and
  nothing else changed was preferred by a blind judge in five of six verdicts ("the near-black floor swallows the
  shadows and leaves the dark navy machines floating"), and the floor at 61 was preferred to one at 91 in six of
  six, where the labels and cards read worse; run 4's site, asked the same way, gave the same five of six
  (`benchmarks/results/round8/floor_compare/`). Runs 4, 5 and 6 all had the dark floor, because this guide asked
  for a ground in the page's colour.
- The ground must not be washed out by the light the props need: give a ground material the starter's
  `groundLook(scene, colour)` (stage.js). It divides the colour by what a face turned up receives and has the
  ground take the environment at 0.25. Both matter: most of what a dark floor shows is the environment's reflection
  in it, which its colour does not scale. A ground does not show as its hex either: the tone mapper's toe takes up
  to 0.04 (linear) out of a dark colour. Measured from straight above: #474e5a comes out #566071 as a plain
  material and #323b49 with `groundLook`; #15181D comes out #0B0F16, darker than the page it was meant to match.
  Choose the ground by its swatch. A floor shader of your own (the section on custom shader materials) takes the
  same two options.
- Do not add light. Run 3 added a fill light to a key of 2.7 and a rim and raised the environment to 0.95, F about
  2.8, on a ground that took it all: its #15181D floor came out #272B32 and its amber #FBC978. And do not take it
  away to fix a colour: under the dimmer light the same dark steel came out #0B1420, thirteen levels from the floor
  at most, and at six of run 4's nine scroll steps under a tenth of the canvas differed from the background.

Check a colour on screen against its hex: draw a swatch under the page's own light and read the canvas at once, as
this example does for the starter's light, for the dimmer one and for run 3's.

```js
// example: colour-swatch
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage') });
renderer.setSize(innerWidth, innerHeight, false);
renderer.toneMapping = THREE.NeutralToneMapping;
const scene = new THREE.Scene();
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
// The page's light: here the starter's (stage.js, LIGHT): a key, a rim from behind, an environment.
const key = new THREE.DirectionalLight('#ffffff', 2.35);
key.position.set(-0.45, 1.0, 0.55);
const rim = new THREE.DirectionalLight('#dce6f5', 0.85);
rim.position.set(10, 6, -8);
scene.add(key, rim);
scene.environmentIntensity = 0.65;

// A swatch lying on the ground, seen from straight above, drawn alone; its colour read from the canvas at once.
const swatch = new THREE.Mesh(new THREE.PlaneGeometry(4, 4).rotateX(-Math.PI / 2));
scene.add(swatch);
const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10);
camera.position.set(0, 5, 0);
camera.lookAt(0, 0, 0);
const gl = renderer.getContext();
function onScreen(material) {
  swatch.material = material;
  renderer.render(scene, camera);
  const px = new Uint8Array(4);
  gl.readPixels(gl.drawingBufferWidth >> 1, gl.drawingBufferHeight >> 1, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
  return '#' + [...px.slice(0, 3)].map((v) => v.toString(16).padStart(2, '0')).join('');
}
const seen = (options) => onScreen(new THREE.MeshStandardMaterial(options));
// The ground as the starter's groundLook() makes it: the colour over what a face turned up receives, the
// environment at 0.25.
const groundLight = 0.28 * key.intensity + 1.84 * 0.25 + rim.intensity * (6 / Math.hypot(10, 6, 8)) / Math.PI;
const ground = (hex) => seen({ color: new THREE.Color(hex).multiplyScalar(1 / groundLight), envMap: scene.environment, envMapIntensity: 0.25, roughness: 0.95 });
const floor = ground('#474e5a'); // the starter's --ground: a lit slate
const floorPlain = seen({ color: '#474e5a', roughness: 0.95 });
const floorDark = ground('#15181d'); // run 6's ground, the design's card colour: darker on screen than its page
const amberLit = seen({ color: '#f0b429', roughness: 0.6 });
// The same amber over what a face turned up receives, as the starter's lit() gives it: the colour again, with its form.
const propLight = 0.28 * key.intensity + 1.84 * scene.environmentIntensity + rim.intensity * (6 / Math.hypot(10, 6, 8)) / Math.PI;
const amberDivided = seen({ color: new THREE.Color('#f0b429').multiplyScalar(1 / propLight), roughness: 0.6 });
const amberEmissive = seen({ color: '#000000', emissive: '#f0b429', roughness: 0.6 });
const amberFlat = onScreen(new THREE.MeshBasicMaterial({ color: '#f0b429', toneMapped: false }));
// A dark metal keeps its colour; a light paint goes most of the way to white.
const steelDark = seen({ color: '#242931', metalness: 0.75, roughness: 0.5 });
const paintLight = seen({ color: '#b8c1cc', roughness: 0.6 });
// The dimmer light of runs 4 and 5: a key of 1.8, the environment at 0.25, no rim.
key.intensity = 1.8;
rim.intensity = 0;
scene.environmentIntensity = 0.25;
const steelDim = seen({ color: '#242931', metalness: 0.75, roughness: 0.5 });
// Run 3's light: a key of 2.7, a rim and a fill light, the environment at 0.95.
key.intensity = 2.7;
rim.intensity = 0.75;
scene.add(new THREE.DirectionalLight('#ffffff', 0.65));
scene.environmentIntensity = 0.95;
const floorRun3 = seen({ color: '#15181d', roughness: 0.95 });
const amberRun3 = seen({ color: '#f0b429', roughness: 0.6 });
document.documentElement.dataset.swatches = JSON.stringify({ floor, floorPlain, floorDark, amberLit, amberDivided, amberEmissive, amberFlat, steelDark, paintLight, steelDim, floorRun3, amberRun3 });
```
<!-- check: js (() => { const s = JSON.parse(document.documentElement.dataset.swatches), c = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)), near = (h, want, by) => c(h).every((v, i) => Math.abs(v - c(want)[i]) <= by); return s.amberFlat === '#f0b429' && near(s.amberEmissive, '#f0b429', 24) && near(s.amberDivided, '#f0b429', 20) && c(s.amberLit)[2] > 70 && c(s.amberRun3)[2] > c(s.amberLit)[2] && near(s.steelDark, '#242931', 12) && Math.max(...c(s.steelDim)) < 40 && Math.min(...c(s.paintLight)) > 205 && near(s.floor, '#323b49', 10) && c(s.floorPlain)[0] > 1.5 * c(s.floor)[0] && Math.max(...c(s.floorDark)) < 30 && c(s.floorRun3)[0] > 1.6 * 0x15; })() -->

## GSAP 3.15 with ScrollTrigger: page effects tied to scroll

For the HTML, what the starter does not do: the parts of a panel coming in one after another, a section pinned while
something in it plays, text that moves at its own pace. Not for what the starter already knows: the story's progress
is `T / ids.length` in its `draw()`, so a progress bar reads that, not a second measure of the scroll (run 3 wrote
`--progress` from ScrollTrigger and from T, and the two disagreed because the chapters differ in height). `scrub: true` ties a tween to the scroll position, so it plays backwards when the visitor scrolls back. Do not drive
the 3D scene with GSAP tweens of its own (`gsap.to(camera.position, ...)` started by a trigger): such a tween depends
on when it started, so the scene stops being a function of scroll and cannot be scrubbed or photographed at a given
position. Let the scene read the same scroll position (the starter does); with Lenis, tell ScrollTrigger on every
Lenis scroll and drive Lenis from GSAP's ticker so both move on the same frame. Do not call `ScrollTrigger.refresh()`
from a resize handler of your own: ScrollTrigger refreshes on resize by itself, and on a touch-only device it skips a
resize that keeps the width and changes the height by less than a quarter, which is the address bar sliding (gsap
3.15.1, `_onResize` in `ScrollTrigger.js`). Run 2 refreshed on every resize and undid that. Call it only after a
layout change it cannot see, such as content your script added.

Text a visitor reads is never tied to the scroll position, and never left part way: wherever the visitor stops, a
line is whole or not there. Bring a panel's lines in on time, once, when the panel comes up, as the example does.
Why: run 6 scrubbed each panel's lines over the 0.16 of the chapter that follows the camera's arrival, as this guide
then showed, so a visitor who stopped there read a half-faded card; rebuilt so that the stretch fell on a
photographed step, the site lost to itself in six of six blind verdicts, every one naming the faded card
(`benchmarks/results/round8/text_timing/`). The starter brings a panel up where its camera arrives, `MOVE` of the
way into its chapter in story time (the first panel is up from the start), and `window.__site.scrollAt(T)` is the
place on the page of a story time: start there. Not at the chapter's top plus a share of its height: the last chapter runs on its height less the window
(`src/chapters.js`), and run 6's last card, started by that sum, stood empty from 35 % to 64 % of its chapter and was
not whole before 95 %, while the menu's link to it lands at 70 %.

```js
// example: gsap-scrolltrigger
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import Lenis from 'lenis';
import { MOVE } from './src/shots.js';

gsap.registerPlugin(ScrollTrigger);
const lenis = new Lenis({ allowNestedScroll: true }); // smoothing is off by itself under reduced motion (respectReducedMotion)
lenis.on('scroll', ScrollTrigger.update);
gsap.ticker.add((seconds) => lenis.raf(seconds * 1000));
gsap.ticker.lagSmoothing(0);

await import('./src/main.js'); // the starter, unchanged
const site = window.__site;
site.scrollWith = (y) => lenis.scrollTo(y);
await site.ready;

// The lines of each chapter's panel come in one after another when the panel comes up: in time, once, and from then
// on they are whole wherever the visitor stops. Only in the film, and not under reduced motion. The text itself is
// never changed: check_site reads it.
if (site.mode === 'webgl') gsap.matchMedia().add('(prefers-reduced-motion: no-preference)', () => {
  document.querySelectorAll('[data-chapter]').forEach((chapter, i) => {
    gsap.from(chapter.querySelectorAll('.panel > *'), {
      opacity: 0, y: 16, duration: 0.6, ease: 'power2.out', stagger: 0.08,
      // the first panel is up from the start and plays at once; the others start where their camera arrives, a pixel
      // early so that a visitor resting exactly there has passed it
      scrollTrigger: i ? { start: () => site.scrollAt(site.at(chapter.dataset.chapter, MOVE)) - 1, end: 'max', once: true } : null,
    });
  });
});
```
<!-- check: picture -->
<!-- check: js (async () => { const site = window.__site, wait = (ms) => new Promise((r) => setTimeout(r, ms)), lines = (id) => [...document.querySelectorAll('[data-chapter="' + id + '"] .panel > *')].map((el) => +getComputedStyle(el).opacity), whole = (v) => v.length >= 2 && v.every((o) => o > 0.99), ids = site.chapters().map((c) => c.id), last = ids[ids.length - 1]; await wait(1500); const first = lines(ids[0]), waiting = lines(last); site.setProgress(site.at(last, 0.36)); let oneByOne = false; for (const t0 = performance.now(); performance.now() - t0 < 1500;) { await new Promise(requestAnimationFrame); const v = lines(last); oneByOne = oneByOne || v[0] > v[v.length - 1] + 0.15; } const rests = []; for (const [id, local] of [[last, 0.36], [last, 0.7], ['how', 0.36], ['how', 0.5], ['detail', 0.4], [ids[0], 0.5]]) { site.setProgress(site.at(id, local)); await wait(1200); rests.push(lines(id)); } return whole(first) && waiting.every((o) => o < 0.01) && oneByOne && rests.every(whole); })() -->

## Lenis 1.3: smooth scrolling

Lenis smooths the scroll position itself, so a page that reads `scrollY` every frame (the starter) gets smooth motion
without easing its own state. Add it on top of the starter as below and change nothing else. In 1.3.28
`respectReducedMotion` is on by default (no smoothing under reduced motion) and touch scrolls natively unless
`syncTouch` is set; leave both so. Do not add Lenis to a page whose scroll is already smoothed by another library
(locomotive-scroll 5 is Lenis inside).

Two settings matter for the starter's page:

- `allowNestedScroll: true`, and no `data-lenis-prevent` on the panels. Why: Lenis lets the browser have every wheel
  event over an element with that attribute, scrollable or not, so run 2's desktop panels, which cover a third of the
  window and never scroll, turned the wheel back to native steps there. With `allowNestedScroll` Lenis gives the
  wheel to an element only while it can still scroll that way (overflow `auto` or `scroll` and more content than
  room), and smooths everything else. Keep the attribute for a widget that must always scroll itself (a code block,
  a map).
- Chapter links go through the starter's `jumpTo`, and `jumpTo` through Lenis: set `window.__site.scrollWith`. Not
  `anchors: true`: Lenis then scrolls a link to the top of its section, where the camera is still on the previous
  chapter's picture (run 2's chapter links landed there), and it does so as well as the starter's own handler.

```js
// example: lenis
import Lenis from 'lenis';

const lenis = new Lenis({ autoRaf: true, allowNestedScroll: true });
await import('./src/main.js'); // the starter, unchanged: it reads scrollY, which Lenis now moves smoothly
// chapter links and __site.jumpTo(id) glide through Lenis to where the chapter rests
window.__site.scrollWith = (y) => lenis.scrollTo(y);
```
<!-- check: picture -->
<!-- check: js document.documentElement.classList.contains('lenis') && window.__site.mode === 'webgl' -->
<!-- check: js (() => { const wheel = (el) => { const e = new WheelEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true }); el.dispatchEvent(e); return e.defaultPrevented; }; const box = document.createElement('div'); box.style.cssText = 'height: 60px; overflow: auto'; box.innerHTML = '<div style="height: 400px">long</div>'; document.querySelector('.panel').append(box); return wheel(document.querySelector('.panel h1')) === true && wheel(box.firstChild) === false; })() -->
<!-- check: js (async () => { await window.__site.ready; window.__site.jumpTo('detail'); await new Promise((r) => setTimeout(r, 3000)); return Math.abs(window.__site.progress() - window.__site.chapters().find((c) => c.id === 'detail').rest) < 0.01; })() -->

## pmndrs postprocessing 6.39 and n8ao 2.1: effects, used sparingly and measured

Ambient occlusion (n8ao) gives contact and depth where a shadow map does not; a little bloom makes emissive parts
read; tone mapping and SMAA finish the picture. Ambient occlusion is the starter's already: `src/occlusion.js` adds an
N8AO pass through postprocessing when the project has both packages and the GPU can afford it (not on a phone or a
software renderer, but in `check_site`'s browser, which sets `data-agy3d-camera` on `<html>` to ask for what a visitor
with a GPU sees; `?ao=1` in the address forces it on, `?ao=0` off, and `info().occlusion` says which). Keep it on: run
3 had n8ao installed and drew none, and its stacked slabs read as toy blocks. It is not free: at 1440 x 900 it holds
about 95 MB of graphics memory beyond the plain starter's 38 MB (about 80 bytes per pixel drawn), so the starter caps
the drawing buffer at a pixel ratio of 1.5 and 3 million pixels while it is on, and `check_site` reports the total.
Leave the composer's `multisampling` at 0 and smooth edges with SMAA after the tone mapping: four samples held 124 MB
more in the starter for edges no smoother at 4x zoom. Write a composer of your
own only for another pass, and keep the starter's two details: the background and fog get the colour the effect
pass's tone mapping turns into the page's colour (tone mapped, #101215 came out #010308), and the renderer's
`info.autoReset` is off so the draw calls are the frame's, not the last pass's. Each pass is a full-screen draw at device resolution: measure the draw
calls and the frame on a phone size before keeping one, and drop to fewer passes or a lower resolution there.
postprocessing 6.39.6 declares three `>= 0.168 < 0.188`: check its peer range before moving three past 0.187. With
postprocessing, create the renderer with `antialias: false, stencil: false, depth: false` (the composer has its own
buffers), leave the renderer's tone mapping off and tone map in the effect pass, and turn n8ao's own gamma correction
off because the effect pass already outputs sRGB.

```js
// example: postprocessing-n8ao
import * as THREE from 'three';
import { BloomEffect, EffectComposer, EffectPass, RenderPass, SMAAEffect, ToneMappingEffect, ToneMappingMode } from 'postprocessing';
import { N8AOPostPass } from 'n8ao';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: false, stencil: false, depth: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
renderer.setSize(innerWidth, innerHeight, false);
const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 1.5));
const sun = new THREE.DirectionalLight('#ffffff', 2);
sun.position.set(-3, 6, 4);
scene.add(sun);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(20, 20), new THREE.MeshStandardMaterial({ color: '#2a2e35' }));
ground.rotation.x = -Math.PI / 2;
const block = new THREE.Mesh(new THREE.BoxGeometry(1.2, 1.2, 1.2), new THREE.MeshStandardMaterial({ color: '#8a8f96' }));
block.position.y = 0.6;
const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.15, 32, 16), new THREE.MeshStandardMaterial({ emissive: '#ffb347', emissiveIntensity: 6 }));
lamp.position.set(0.9, 0.15, 0.9);
scene.add(ground, block, lamp);
const camera = new THREE.PerspectiveCamera(35, innerWidth / innerHeight, 0.1, 50);
camera.position.set(3.5, 2.5, 4);
camera.lookAt(0, 0.5, 0);

const composer = new EffectComposer(renderer, { frameBufferType: THREE.HalfFloatType });
composer.addPass(new RenderPass(scene, camera));
const ao = new N8AOPostPass(scene, camera, innerWidth, innerHeight);
ao.configuration.aoRadius = 1.0;
ao.configuration.intensity = 2.5;
ao.configuration.gammaCorrection = false;
composer.addPass(ao);
composer.addPass(new EffectPass(camera,
  new BloomEffect({ luminanceThreshold: 1.0, intensity: 0.6, mipmapBlur: true }),
  new ToneMappingEffect({ mode: ToneMappingMode.NEUTRAL })));
composer.addPass(new EffectPass(camera, new SMAAEffect())); // after tone mapping: SMAA wants the final colours
composer.setSize(innerWidth, innerHeight);
composer.render();
```
<!-- check: picture -->

## troika-three-text 0.53: text in the scene

A label on a part, a number that sits on the object it counts: troika draws it from the font file as signed distance
fields, sharp at any size. Give it a font file the site serves: without `font` it fetches fonts from a CDN, which a
sandbox cannot reach and a strict host forbids. It reads `.ttf`, `.otf` and `.woff` (not `.woff2`). Paragraphs stay
HTML: they must be selectable, translatable and readable without WebGL.

```js
// example: troika-text
import * as THREE from 'three';
import { Text } from 'troika-three-text';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(0, 0.6, 4);

const label = new Text();
label.text = 'sharp at any size';
label.font = './fonts/label.ttf'; // served beside index.html (Vite: public/fonts/label.ttf)
label.fontSize = 0.3;
label.anchorX = 'center';
label.anchorY = 'middle';
label.color = '#e9e7e2';
label.position.set(0, 0.6, 0);
scene.add(label);
label.sync(() => renderer.render(scene, camera)); // draw once the glyphs are ready
```
<!-- check: picture -->

To label a prop in the starter's page, register the label with the stage: its `label` function inside
`stage.js`, or `window.__site.label(name, text, prop)` from a module of your own. It is added to that prop's holder (shown while the
prop is on stage), the page draws again when troika has its glyphs, and `__site.info().onScreen` reports its box with
`kind: "label"`, so `check_site` can tell when it is cut by the frame or lies under the panel. Stand a label upright
at about its prop's height, facing the shots that show it. Why: run 2 laid its labels flat on the floor, and at the
low angles of its shots they were slivers, cut at the frame's bottom edge or hidden by the panel. A label is flagged
`subject: true` while its prop is the shot's subject, and the stage hides one whose box is under 16 CSS px high on
screen until a closer shot makes it readable: run 3's finale showed its labels 5 to 7 px high on a phone. So size a
label for the shots that are about its prop (a `fontSize` of about 3 % of the camera's distance there), not for a
wide one.

```js
// example: troika-label
import { Text } from 'troika-three-text';

await import('./src/main.js'); // the starter's page, with its stand-in props
await window.__site.ready;
const label = new Text();
label.text = 'part, 0.5 m';
label.font = './fonts/label.ttf';
label.fontSize = 0.12;
label.anchorX = 'center';
label.anchorY = 'bottom';
label.color = '#e9e7e2';
label.position.set(0, 0.55, 0); // in the prop's own space: upright, just above the 0.45 m stand-in
window.__site.label('part-label', label, 'part');
```
<!-- check: picture -->
<!-- check: js (async () => { window.__site.setProgress(window.__site.at('how', 0.7)); await new Promise((r) => setTimeout(r, 1500)); const v = window.__site.info().onScreen['part-label']; return !!v && v.kind === 'label' && v.box[0] >= 0 && v.box[2] <= 1 && v.box[1] >= 0 && v.box[3] <= 1 && v.share > 0; })() -->

## three-custom-shader-material 6.4: a standard material with a shader inside

To change how a `MeshStandardMaterial` moves or colours its surface (a wave, a reveal, a heat colour) and keep its
lighting, environment and shadows: write only the part that changes, into `csm_Position`, `csm_DiffuseColor`,
`csm_Roughness` and the like. Import the plain three.js build from `three-custom-shader-material/vanilla`. A surface
that moves needs the same change in its depth material, or its shadow stays where the undisplaced surface was.

Three rules for any GLSL you write for a three.js material, this one or a `ShaderMaterial`:

- **Colours reach a shader as uniforms made from `THREE.Color`, never as typed numbers.** Why: inside the shader
  colours are linear, and three converts a `THREE.Color` from the sRGB hex you know to linear when it uploads it;
  `base = vec3(0.055, 0.064, 0.078)`, the bytes of `#0e1014` over 255, is taken as linear and shows as `#42474f`. Run 2's
  floor, meant to be near black, came out light grey that way (`csm_DiffuseColor` replaces three's diffuse colour
  after its own conversion, so nothing converts it for you). A colour that is computed in the shader is fine; a colour
  someone chose is a uniform.
- **Two palette colours that stand for two states are switched, never held as a blend:**
  a `mix` driven by a `smoothstep` over a short span of story time (`color = mix(a, b, smoothstep(e0, e1, t));`), so
  every resting picture shows one of them, never a `mix` held at a constant such as 0.6. Why: run 3 held its rail at a blend of its blue and its red, an off-palette pink
  (243,171,219) that no design rule allows.
- **A procedural edge (a stripe, a grid line, a reveal) is smoothed over one pixel with `fwidth`.** Why: `step`
  gives a hard edge that the renderer's antialiasing does not touch (it smooths only the edges of triangles), so it
  crawls and flickers as the camera moves. Write `edge = smoothstep(-w, w, d)` with `w = fwidth(d)`, `d` the signed distance
  to the edge in any unit.

A floor written this way looks after the ground's colour itself (the light section above): divide the colours it
writes by the starter's `GROUND_LIGHT` (a uniform, or `new THREE.Color(hex).multiplyScalar(1 / GROUND_LIGHT)` for
each) and give the material `envMap: scene.environment` and `envMapIntensity: GROUND_ENVIRONMENT`, both exported by
stage.js. Why: `csm_DiffuseColor` replaces the material's own colour, so the colour of `groundLook` does not reach
such a floor, and runs 4 and 5 both wrote theirs as a shader.

```js
// example: custom-shader-material
import * as THREE from 'three';
import CustomShaderMaterial from 'three-custom-shader-material/vanilla';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
renderer.shadowMap.enabled = true;
const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 1));
const sun = new THREE.DirectionalLight('#ffffff', 2.5);
sun.position.set(-3, 6, 4);
sun.castShadow = true;
scene.add(sun);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(20, 20), new THREE.MeshStandardMaterial({ color: '#2a2e35' }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

// The page's colours as THREE.Color uniforms (three hands them to the shader in linear values), and the scroll.
const uniforms = {
  uProgress: { value: 0.4 },
  uCold: { value: new THREE.Color('#2b6cb0') },
  uWarm: { value: new THREE.Color('#e0a33a') },
};
const wave = /* glsl */ `
  uniform float uProgress;
  varying float vHeight;
  void main() {
    vHeight = position.y;
    csm_Position = position + normal * 0.08 * sin(position.y * 10.0 + uProgress * 6.2832);
  }`;
const material = new CustomShaderMaterial({
  baseMaterial: THREE.MeshStandardMaterial,
  vertexShader: wave,
  // warm rises from below as the page scrolls; the line between the two is one pixel soft (fwidth)
  fragmentShader: /* glsl */ `
    uniform float uProgress;
    uniform vec3 uCold;
    uniform vec3 uWarm;
    varying float vHeight;
    void main() {
      float d = mix(-0.9, 0.9, uProgress) - vHeight;
      float w = fwidth(d);
      csm_DiffuseColor = vec4(mix(uCold, uWarm, smoothstep(-w, w, d)), 1.0);
    }`,
  uniforms,
  roughness: 0.4,
});
const blob = new THREE.Mesh(new THREE.IcosahedronGeometry(0.8, 32), material);
blob.position.y = 1;
blob.castShadow = true;
// the shadow follows the wave
blob.customDepthMaterial = new CustomShaderMaterial({
  baseMaterial: THREE.MeshDepthMaterial, vertexShader: wave, uniforms, depthPacking: THREE.RGBADepthPacking,
});
scene.add(blob);

const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(3, 2.4, 4.5);
camera.lookAt(0, 0.8, 0);
renderer.render(scene, camera);
```
<!-- check: picture -->

The repository's test of this example also draws the blob with each colour given to three's own `color` instead and
compares the pixels: the two agree within a few levels, so the colour on screen is the one chosen.

## three-mesh-bvh 0.9: picking on dense models

Hover labels and clicks on parts cast rays; on a model of tens of thousands of triangles three tests every triangle.
A bounds tree, built once per geometry, makes each ray a few dozen tests. Set `firstHitOnly` when the nearest hit is
all you need.

```js
// example: three-mesh-bvh
import * as THREE from 'three';
import { acceleratedRaycast, computeBoundsTree, disposeBoundsTree } from 'three-mesh-bvh';

THREE.BufferGeometry.prototype.computeBoundsTree = computeBoundsTree;
THREE.BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree;
THREE.Mesh.prototype.raycast = acceleratedRaycast;

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
const scene = new THREE.Scene();
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 2));
const geometry = new THREE.IcosahedronGeometry(1, 40); // 33,620 triangles
geometry.computeBoundsTree();
const material = new THREE.MeshStandardMaterial({ color: '#8a8f96' });
const model = new THREE.Mesh(geometry, material);
scene.add(model);
const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(0, 0, 5);
renderer.render(scene, camera);

const raycaster = new THREE.Raycaster();
raycaster.firstHitOnly = true;
const pointer = new THREE.Vector2();
addEventListener('pointermove', (e) => {
  pointer.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObject(model)[0];
  material.color.set(hit ? '#e0a33a' : '#8a8f96');
  document.body.dataset.hover = hit ? String(hit.faceIndex) : '';
  renderer.render(scene, camera);
});
```
<!-- check: picture -->
<!-- check: js (async () => { dispatchEvent(new PointerEvent('pointermove', { clientX: innerWidth / 2, clientY: innerHeight / 2 })); return document.body.dataset.hover !== '' && document.body.dataset.hover !== undefined; })() -->

## detect-gpu 5.0: choosing a quality level

Read the tier before the first frame and pick the pixel ratio, the shadow map size and the passes from it. Three
rules. Serve its benchmark files yourself (copy `node_modules/detect-gpu/dist/benchmarks` into the served folder and
pass `benchmarksURL`): by default it fetches them from unpkg.com. Pass the renderer's context as `glContext`: without
it detect-gpu 5.0.70 makes a WebGL 1 context of its own and never releases it, and a browser keeps only a few live
contexts per page. And never switch the 3D off at a low tier: a software renderer is in none of its benchmarks, so the
headless Chromium that every check uses (SwiftShader) gets tier 1 with type `FALLBACK`, and a renderer on its block
list gets tier 0. A page that hides the scene at a low tier shows its checks the fallback and not the site. A low tier
means a lower quality, not no scene.

Start every wait that does not need another at the same moment and await them together: the tier lookup, the models
(the starter starts their downloads as soon as the stage exists), a font. Why: run 2 awaited the tier, then imported
its 0.7 MB scene module, then loaded its models, three waits in a row before the first frame. Give the lookup a time
limit too: it fetches a file, and a slow one must not hold the page back.

```js
// example: detect-gpu
import * as THREE from 'three';
import { getGPUTier } from 'detect-gpu';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
const tier = Promise.race([
  getGPUTier({ glContext: renderer.getContext(), benchmarksURL: './detect-gpu' }), // the copied folder, beside index.html
  new Promise((resolve) => setTimeout(() => resolve({ tier: 1, type: 'TIMEOUT' }), 1500)),
]);
// in parallel with the lookup: whatever else the first frame waits for (here the page's font)
const font = new FontFace('Label', 'url(./fonts/label.ttf)').load().then((face) => document.fonts.add(face));
const [gpu] = await Promise.all([tier, font]);

const QUALITY = { 0: { dpr: 1, shadow: 1024, passes: 0 }, 1: { dpr: 1.5, shadow: 1024, passes: 1 },
  2: { dpr: 2, shadow: 2048, passes: 2 }, 3: { dpr: 2, shadow: 2048, passes: 2 } };
const quality = QUALITY[gpu.tier] ?? QUALITY[0];
renderer.setPixelRatio(Math.min(devicePixelRatio, quality.dpr));
renderer.setSize(innerWidth, innerHeight, false);
document.documentElement.dataset.gpuTier = String(gpu.tier);
document.documentElement.dataset.gpuType = gpu.type;
document.documentElement.dataset.maxDpr = String(quality.dpr);
```
<!-- check: js document.documentElement.dataset.gpuTier === '1' && document.documentElement.dataset.gpuType === 'FALLBACK' && document.documentElement.dataset.maxDpr === '1.5' -->
<!-- check: js document.fonts.check('16px Label') -->

## React Three Fiber 9.8 and drei 10.7: when the project is React

Only for a project that already uses React (React 19 for these versions; fiber 9.8 declares react `>= 19 < 19.4`).
The rules do not change: the scene is a function of scroll, so read the scroll position and set the scene from it,
and draw on demand (`frameloop="demand"` and `invalidate`), not every frame. drei's `ScrollControls` scrolls its
own box instead of the page, which breaks the page's own scroll, links and the article fallback: read the page's
scroll instead, as below (the starter's `shots.js` and `chapters.js` work unchanged). drei's `Environment` presets
download HDR files from a CDN: build the environment from `Lightformer`s, or serve your own file. With three 0.187,
`shadows` alone asks for the removed soft shadow type (a warning): write `shadows="percentage"`. fiber 9.8 creates a
`THREE.Clock`, which three 0.187 warns about once; that warning is fiber's and harmless.

```jsx
// example: r3f-drei
import { createRoot } from 'react-dom/client';
import { useEffect } from 'react';
import { Canvas, useThree } from '@react-three/fiber';
import { ContactShadows, Environment, Lightformer } from '@react-three/drei';
import { measure, timeAt } from './src/chapters.js';
import { evaluate } from './src/shots.js';

function ScrollCamera() {
  const camera = useThree((s) => s.camera);
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    let boxes = measure();
    const ids = boxes.map((b) => b.id);
    const update = () => {
      const s = evaluate(timeAt(boxes, scrollY), ids);
      camera.position.fromArray(s.cam.pos);
      camera.fov = s.cam.fov;
      camera.lookAt(...s.cam.target);
      camera.updateProjectionMatrix();
      invalidate();
    };
    const relayout = () => { boxes = measure(); update(); };
    addEventListener('scroll', update, { passive: true });
    addEventListener('resize', relayout);
    update();
    return () => { removeEventListener('scroll', update); removeEventListener('resize', relayout); };
  }, [camera, invalidate]);
  return null;
}

const host = document.createElement('div');
host.style.cssText = 'position:fixed;inset:0;z-index:0';
document.body.prepend(host);
createRoot(host).render(
  <Canvas frameloop="demand" shadows="percentage" dpr={[1, 2]} camera={{ fov: 30 }}>
    <color attach="background" args={['#101215']} />
    <Environment resolution={128}>
      <Lightformer intensity={2} position={[0, 4, -6]} scale={[10, 4, 1]} />
    </Environment>
    <directionalLight castShadow position={[-4, 8, 5]} intensity={2} />
    <mesh castShadow position={[0, 0.75, 0]}>
      <boxGeometry args={[1.4, 1.5, 0.9]} />
      <meshStandardMaterial color="#8a8f96" />
    </mesh>
    <ContactShadows opacity={0.6} scale={8} blur={2.5} far={2} frames={1} />
    <ScrollCamera />
  </Canvas>,
);
```
<!-- check: picture -->
<!-- check: warning THREE.Clock: This module has been deprecated -->

## glTF-Transform 4.5: shrinking a model that did not come from the plugin

The plugin's props need none of this: deliver them for web_three, install them with `install_into_project`, and the
installer writes the web-sized files and a loader with levels of detail (and compresses with KTX2 and meshopt when the
project has these packages). Use glTF-Transform for a model from elsewhere. WebP textures (through sharp) need no
decoder in the page; meshopt geometry and KTX2 textures do, and both decoders are WebAssembly: on a host that may
not run WebAssembly, use WebP alone.

```js
// example: gltf-transform (node)
import fs from 'node:fs';
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { dedup, meshopt, prune, textureCompress } from '@gltf-transform/functions';
import { MeshoptEncoder } from 'meshoptimizer';
import sharp from 'sharp';

const [input, output] = process.argv.slice(2);
await MeshoptEncoder.ready;
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.encoder': MeshoptEncoder });
const doc = await io.read(input);
await doc.transform(
  dedup(),
  prune(),
  textureCompress({ encoder: sharp, targetFormat: 'webp', resize: [2048, 2048] }), // no decoder needed in the page
  meshopt({ encoder: MeshoptEncoder, level: 'medium' }), // needs MeshoptDecoder (WebAssembly) in the page
);
await io.write(output, doc);
console.log(JSON.stringify({ before: fs.statSync(input).size, after: fs.statSync(output).size }));
```
<!-- check: node out.after < out.before -->

In the page, a meshopt file needs `loader.setMeshoptDecoder(MeshoptDecoder)` with `MeshoptDecoder` from
`three/addons/libs/meshopt_decoder.module.js`; a KTX2 file needs a `KTX2Loader` with its transcoder files served by the
site and `detectSupport(renderer)`. The installer's loader does both for the plugin's props.

## Left out, and why

- **Theatre.js** (`@theatre/core` 0.7.2): no release since May 2024; its timeline is keyed to time, and the scene here
  is keyed to scroll.
- **curtains.js** (8.1.6): no release since May 2024, and it draws only planes over the page.
- **locomotive-scroll** (5.1.1): version 5 is Lenis inside; use Lenis directly. **@studio-freight/lenis** is Lenis's
  old name, deprecated on npm.
- **ScrollMagic**: ScrollTrigger does the same and works with Lenis.
- **@14islands/r3f-scroll-rig**: last release December 2024, and it moves the scene's state into its own scroll
  proxy.
- **camera-controls** (in the seed, 3.1.2): for a camera the visitor drags; a scroll site's camera follows the
  scroll. Use it only for an explicit "turn the model" moment, and give back the scroll's camera after.
- **maath** (in the seed): its `damp` eases values per frame, which makes the picture depend on the path taken; use its
  maths helpers, not the damping, for the scene's state.
- **motion** (in the seed, 14.14): good for React interface animation; for scroll-linked page effects this guide uses
  GSAP, and one library for one job is enough.
- **lygia** (in the seed): a library of shader functions (noise, colour, lighting) to include in your own GLSL or
  TSL. Copy the one function you need rather than its whole tree.
- **lil-gui, stats-gl** (in the seed): development tools. Keep them out of the published page.
