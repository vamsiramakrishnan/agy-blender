# Recipes

Small pieces the site needs more than once. Code here ran like the examples in [libraries.md](libraries.md).

## Shots: the vocabulary and frame()

A site's camera speaks in shots. Pick each chapter's from these, by what the chapter says, and make it differ from the
one before in scale (another row) or in angle (30 degrees or more round or up). Why: run 3's six shots stood 4.9 to
5.8 m off, 13 to 14 degrees up, on a 30 to 32 degree lens; its first two pictures were nearly the same and its contact
sheet read as a carousel of three boxes. The numbers are those of `SHOT` in the starter's `src/shots.js`, which
`frame()` applies:

| type | what it is for | fill | elevation | lens | the hold |
|---|---|---|---|---|---|
| wide | where things are: the opening, a group, the last picture | 0.4 | 16 | 34 | push 0.1, orbit 8 |
| medium | one object and what touches it | 0.62 | 22 | 30 | push 0.12, orbit 6 |
| close | one part: a detail, a label, a mechanism at work | 0.8 | 30 | 22 | push 0.1, orbit 5 |
| top | a layout, a flow, a count, seen from above | 0.7 | 72 | 30 | push 0.1, orbit 14 |
| low | scale and weight; orbiting low it tracks along a row | 0.6 | 5 | 40 | push 0.06, orbit 16 |

fill: the share of the free part of the window (its height, or its width when that binds) that the subject's bounding
sphere covers; a box covers about two thirds of its sphere. elevation: degrees above the horizon. lens: the vertical
view in degrees at the design shape; a narrow lens flattens and isolates a part, a wide one gives a low shot its depth.
The hold travels: during it the camera closes `push` of its distance to the target and turns `orbit` degrees round it,
so near and far things move against each other while the visitor reads, and the picture is never a still portrait. A
stronger push crowds the frame by the end of the hold (a push of a quarter takes a fill of 0.6 to 0.8).

`frame(bounds, type, options)` makes the pose of a shot around a sphere: `center` and `radius` in metres, the two
numbers of `bounds` in the loader that `install_into_project` wrote (`src/agy3d/<asset>/index.js`, in the prop's own
space: add its place `at` from `src/props.js`), or `around(min, max)` of a box. A prop the real model replaces keeps
a shot framed from its size, where a shot typed by hand for its stand-in is wrong. Any field of the table can be
overridden; `azimuth` turns the camera round the vertical (0 from +Z, 90 from +X):

```js
// example: frame-a-shot (node)
import { around, frame } from './src/shots.js';

const gate = { center: [0, 0.8, 0], radius: 0.9 };    // bounds of the installed gate, standing at the origin
const vault = { center: [3, 0.6, 0], radius: 0.7 };   // bounds of the vault, plus its place in props.js
const needle = around([1.2, 0, -0.2], [1.5, 0.06, 0.2]); // one slab of a layer, from its box
const both = around([-0.9, 0, -0.9], [3.7, 1.7, 0.7]); // the two props together, from the union of their boxes
export const SHOTS = {
  intro: { ...frame(gate, 'wide', { azimuth: 30 }), span: 9 },
  flood: { ...frame(gate, 'low', { azimuth: -55 }), span: 7, subject: 'gate' },
  needle: { ...frame(needle, 'close', { azimuth: 40 }), span: 2, subject: 'flood' },
  routes: { ...frame(both, 'top', { azimuth: 10 }), span: 10, subject: ['gate', 'vault'] },
  vault: { ...frame(vault, 'medium', { azimuth: 60 }), span: 6, subject: 'vault' },
  outro: { ...frame(both, 'wide', { azimuth: 20, elevation: 9 }), span: 12, subject: ['gate', 'vault'] },
};
const d = (s) => Math.hypot(...s.pos.map((p, j) => p - s.target[j]));
const up = (s) => (Math.asin((s.pos[1] - s.target[1]) / d(s)) * 180) / Math.PI;
console.log(JSON.stringify(Object.fromEntries(Object.entries(SHOTS).map(([id, s]) => [id, { d: d(s), up: up(s), fov: s.fov }]))));
```
<!-- check: node out.needle.d < 0.5 * out.intro.d && out.routes.up > 70 && out.flood.up < 6 && out.needle.fov < out.flood.fov -->

A shot is designed for a horizontal coverage: what it shows across a window of the design shape (`DESIGN_ASPECT` in
`src/shots.js`, 1.6, a 1440 x 900 window). Pass `aspect`, the free part's width over its height at that shape (about 1
beside the starter's panel, the default), so the subject is framed in the part the text leaves free. Side by side with
the panel (desktop, landscape phone), a narrower window gets a wider view that keeps the coverage. In the stacked
layout (portrait phone, panel below) and in a still, the starter keeps the camera's position and direction and fits
only the view: seen from the pose the shot arrives at, the shot's `subject` is centred in the free area and covers the
share of it that it covers of the free part of the design window, at least 0.3 and at most 0.85 (the shot's own `fill`
replaces that share), and from there the hold travels as on a desktop. So frame each shot at the design shape only, and
name its subject. Why: run 2 widened its phone view by a fixed 1.36 and cut its props; holding the desktop's coverage
left the subject 22 % of the width; run 3's phone refitted the subject to one share at every frame, which cancelled the
hold and gave every chapter the same scale.

A shot's subject is the props (names from `SHOW`) or layers (names from `LAYERS`) it is about. A layer is what the
chapter shows that is not a prop: an `InstancedMesh` of slabs, generated geometry. Register it in your own module with
`window.__site.layer(name, object, { update, box })` (or `layer` in `stage.js`) and list its name in `LAYERS`. Its box
is the object's bounds at each frame (an `InstancedMesh`'s from its instances), or `box`, a fixed world box
`[[x0, y0, z0], [x1, y1, z1]]`, when the framing must not follow the layer's motion. Why: run 3's chapters were about
its slabs, a subject could only be a prop, and on the phone the slabs fell out of the frame.

Then check every chapter's rest with `window.__site.info().onScreen`: each subject's `box` lies inside 0..1 and clear of
the panel (on the starter's desktop layout `box[0]` at least 0.42; on a portrait phone `box[3]` less than the panel's
top).

## The last picture

The last chapter's picture is the one a visitor leaves with, and the one the page rests on longest. Frame it as a group:
`frame(around(min, max), 'wide' or 'medium', ...)` from the union of the boxes of the props it gathers, the camera low
(elevation under about 10 degrees) so the props stand against the background rather than lie on the floor, and name them
all as its subject. Why: run 3 ended on its three instruments in a row across the upper half of the frame at y 0.42 to
0.62, the bottom 40 % empty grid, with labels 5 to 7 px high on a phone.

## Numbers a visitor can see

When the page says a number, the picture can show it: one instance per unit, so a ratio of 570 to 1 is thousands of
slabs becoming a handful, not 96 becoming 6 (run 3, whose text said 570 times). One `InstancedMesh` holds them all
(one draw call), set up once; story time moves them. Give each instance its own delay so they arrive and leave in an
order (here the bottom level lands first, the nearest row leaves first) and route each through a lane of its own (its
own row and height), so none passes through another: run 3 moved its 96 slabs on one value, and they slid through each
other and through the grey stack.

```js
// example: numbers-as-instances
import * as THREE from 'three';
import { ramp } from './src/shots.js';

await import('./src/main.js'); // the starter's page, with its stand-ins
const site = window.__site;
await site.ready;
const at = site.at;

// The fact, as the page's text gives it (copied with its data-src): 302,628 tokens in, 531 kept. One slab for every
// 50 tokens: 6,053 slabs become 11, and the ratio on screen is the ratio in the text.
const BEFORE = 302628, AFTER = 531, PER = 50;
const N = Math.round(BEFORE / PER), KEEP = Math.round(AFTER / PER);
// The flood: rows of 30 slabs along x, 20 rows deep, as many levels as it takes; the gate they shrink into at x = GATE.
const COLS = 30, DEEP = 20, ROWS = Math.ceil(N / COLS), LEVELS = Math.ceil(ROWS / DEEP);
const X0 = 2.4, Z0 = -0.6, DX = 0.1, DZ = 0.07, DY = 0.035, GATE = 2.15, FALL = 1.5;

const flood = new THREE.InstancedMesh(new THREE.BoxGeometry(0.08, 0.025, 0.05),
  new THREE.MeshStandardMaterial({ color: '#c9c4b8', roughness: 0.6 }), N);
flood.castShadow = flood.receiveShadow = true;
const m = new THREE.Matrix4(), p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();

function update({ T }) {
  for (let i = 0; i < N; i++) {
    const row = Math.floor(i / COLS), col = i % COLS, level = Math.floor(row / DEEP);
    const y = 0.0125 + level * DY, z = Z0 + (row % DEEP) * DZ;
    // In "how" the flood lands, the bottom level first: a slab falls the same height on the same curve as the one
    // under it, only later, so it never reaches it. Times are in chapters: 0.3 is three tenths of one.
    const land = at('how', 0.05 + 0.45 * (level / LEVELS) + 0.1 * (col / COLS));
    const fall = ramp(T, land, land + 0.3);
    // In "detail" it leaves, the nearest row first: a whole row slides towards the gate at once, so its slabs keep
    // their spacing, and each shrinks to nothing before it reaches the gate, except the front slab of the first KEEP
    // rows, which stops there: the handful that is left.
    const go = at('detail', 0.1 + 0.5 * (row / ROWS));
    const x = X0 + col * DX - ramp(T, go, go + 0.3) * (X0 + COLS * DX - GATE);
    const kept = row < KEEP && col === 0;
    const size = T <= land ? 0 : kept ? 1 : THREE.MathUtils.clamp((x - GATE - 0.04) / DX, 0, 1);
    m.compose(p.set(Math.max(x, GATE), y + FALL * (1 - fall), z), q, s.setScalar(size));
    flood.setMatrixAt(i, m);
  }
  flood.instanceMatrix.needsUpdate = true;
}
// A layer: drawn from story time before each frame, and reported by onScreen() with kind "layer". List 'flood' in
// LAYERS (src/shots.js) to name it as a shot's subject.
site.layer('flood', flood, { update });
site.setProgress(at('how', 0.95));
```
<!-- check: picture -->
<!-- check: js (() => { const s = window.__site, area = (b) => (b[2] - b[0]) * (b[3] - b[1]); s.setProgress(s.at('how', 0.95)); const a = s.info().onScreen.flood; s.setProgress(s.at('outro', 0.5)); const b = s.info().onScreen.flood; return a.kind === 'layer' && !!b && area(a.box) > 10 * area(b.box); })() -->

The draw is one call for all of them (two with the shadow map), whatever N is. A plain `BoxGeometry` has 12
triangles; three's `RoundedBoxGeometry` (`three/addons/geometries/RoundedBoxGeometry.js`, radius about 0.004 for a slab
of a few centimetres) gives small boxes edges that catch the light, at 108 triangles with one segment: use it for tens
or hundreds of instances, not for thousands.

## Time a layer by chapter

Everything that changes with the story is computed from story time: in `evaluate` in `src/shots.js` for what the
stage draws itself, or in a layer's `update`, which is called with that state (above). Name each moment by its chapter,
`fade = ramp(T, at('flood', 0.2), at('flood', 0.6))`, with `at` from `timeline` in `src/shots.js` or
`window.__site.at`. A share of the whole story is `T / ids.length`. Why: the chapters come from the HTML, so a raw
story time (`fade = ramp(T, 2.0, 2.85)`) or a fixed count (`T / 7`), as run 2 wrote, points at the wrong chapter as soon
as one is added or moved. The same goes for links: the starter's `jumpTo(id)` lands on `T = at(id, REST)`, where the
chapter's move is done and its text is up. And for an effect of the page's own that must start at such a moment (a
ScrollTrigger, say): its place on the page is `window.__site.scrollAt(T)`, not the chapter's top plus a share of its
height. Why: the last chapter runs on its height less the window, so that sum is late there; run 6 started the lines of
its last panel by it, and the card stood empty from 35 % to 64 % of the chapter.

## Facts with their source

Every number, quote, identifier or code line on the page is copied from a file of the project, and its element says
which file, so `check_site` can compare the two. Give the path relative to the sources folder passed to `check_site`
(the project's repository), then a JSON pointer, a line range or nothing:

```html
<!-- the names below are placeholders: use the project's real files -->
<p>Tokens kept out: <span data-src="RESULTS.json#/path/to/the/number">the number, as that file holds it</span>.</p>
<pre data-src="RESULTS.json#/path/to/a/text"><code>that text, or one unbroken passage of it (20 characters or more):
its first lines, say, with nothing left out in the middle</code></pre>
<pre data-src="src/MODULE.py#L40-L52"><code>lines 40 to 52 of that file, exactly as they are</code></pre>
<blockquote data-src="README.md">a sentence that occurs in the README, word for word</blockquote>
```

When a picture or example on the page is made up to explain something, say so in its caption ("an illustration,
not a real log line"). Do not invent identifiers, hashes, line numbers or log lines that look like receipts.

A sourced number is an element of its own, and inside a flex or grid container (a button, a badge, a row of metrics)
every element and every run of text between elements becomes an item, with the spaces at their edges dropped. Put the
whole label in one element inside such a container, `<a class="button"><span>Follow one <span data-src="...">20,001</span>-line
log</span></a>`. Why: run 4's first button read "FOLLOW ONE20,001-LINE LOG", and the blind judge named it in five of six
verdicts on type and layout.

## Stills for the page without WebGL

Without WebGL (or with JavaScript off) the starter's page is an article; each chapter can show a still of its shot.
`check_site` makes them: on a page that keeps the starter's `window.__site.chapters()` and `setProgress`, it draws each
chapter where it rests and writes the 3D canvas alone, 1280 px wide, as `stills/<chapter id>.webp` in its output folder
(the answer's "Pictures" says where). While it draws them it sets `data-agy3d-still` on `<html>`, and the starter then
frames each shot's subject in the middle of the whole canvas, as in the stacked layout: a still stands in the article
with no panel beside it, and drawn with the desktop's framing run 3's stills had their subject in the right 60 % and
empty floor on the left. Copy that folder beside `index.html` after the last change to the shots, and add one still per
panel. Without the hook, set the attribute, call `window.__site.setProgress(T)` at each chapter's rest yourself and
save the canvas's own pixels (`canvas.toDataURL()` right after it draws), not a screenshot of the window:

```html
<img class="still" src="stills/how.webp" alt="The gate from the front, the stream of lines entering it" width="1280" height="800" loading="lazy">
```

`starter/src/style.css` shows `.still` only in the article layout. The `alt` text says what the picture shows, since
it is the scene's only description for a reader who cannot see it.

## How long a chapter is

A chapter must last as long as its text takes to read plus its move: about one window height of scroll for the move
and one for every 40 words of the panel. The text is shown only once the move is over (`MOVE` in `src/shots.js`, 0.35
of each chapter after the first in the starter), so the reading time is the part after it. The starter's chapters are
220 % of the window's height (the first 180 %); change `min-height` per chapter in the CSS when a chapter says more or
less. Too short and the camera arrives after the
reader has left; too long and the scroll feels stuck.
