# Recipes

Small pieces the site needs more than once. Code here ran like the examples in [libraries.md](libraries.md).

## Frame a shot from a prop's size

A shot written by hand for a stand-in is wrong once the real model, with its own size, replaces it. The loader that
`install_into_project` wrote exports the model's box (`bounds`: `center` and `radius` in metres, +Y up, in the prop's
own space), so a shot can be computed from it: the distance at which the prop fills a share of the free part of the
window, seen from a direction.

```js
// example: frame-a-shot (node)
// The camera pose at which a sphere of `radius` around `center` fills `fill` of the window's height (and of its width,
// whichever is tighter), seen from `azimuth` degrees round the vertical and `elevation` degrees above the horizon.
export function frame({ center, radius }, { fov = 30, aspect = 16 / 9, fill = 0.55, azimuth = 35, elevation = 18 } = {}) {
  const t = Math.tan((fov * Math.PI) / 360);
  const d = Math.max(radius / (t * fill), radius / (t * aspect * fill));
  const az = (azimuth * Math.PI) / 180, el = (elevation * Math.PI) / 180;
  const pos = [center[0] + d * Math.cos(el) * Math.sin(az), center[1] + d * Math.sin(el), center[2] + d * Math.cos(el) * Math.cos(az)];
  return { pos, target: [...center], fov };
}

// shots.js stays free of three.js and of the loader (a test and a tool call it as it is), so do not import the loader
// there: copy the two numbers from `bounds` in src/agy3d/<asset>/index.js into the shot, and add the prop's place
// from props.js (`at`) to the centre when the prop does not stand at the origin:
//   intro: { ...frame({ center: [0, 0.4, 0], radius: 0.75 }, { azimuth: 30 }), span: 0.75 * 6 },
console.log(JSON.stringify(frame({ center: [0, 0.4, 0], radius: 0.75 })));
```
<!-- check: node out.pos[2] > 2 && out.pos[1] > 0.4 && out.fov === 30 -->

A shot is designed for a horizontal coverage: what it shows across a window of the design shape (`DESIGN_ASPECT` in
`src/shots.js`, 1.6, a 1440 x 900 window). In a narrower window the starter widens the view to keep that coverage (a
portrait phone sees as much across as the desktop, and more above and below), so frame each shot at the design shape
only. Why: a shot fitted by its vertical view alone is cut at the sides on a phone; run 2 widened its phone view by a
fixed 1.36 and showed about 20 degrees across instead of 51.

The free part of the window is narrower than the window when the text takes a side: pass the free part's aspect at
the design shape (about 0.6 of 1.6 with the starter's desktop layout, so about 1) and keep `fill` at or under 0.6, so
the subject breathes. Fit the prop the shot names as its `subject`, and give the shot that name. Then check every
chapter's rest with `window.__site.info().onScreen`: each subject's `box` lies inside 0..1 and clear of the panel (on
the starter's desktop layout `box[0]` at least 0.42; on a portrait phone `box[3]` less than the panel's top).

## Time a layer by chapter

Everything that changes with the story is computed in `evaluate` in `src/shots.js`, which makes `at` from the page's
chapter ids (`timeline` in the same file): name each moment by its chapter,
`fade = ramp(T, at('flood', 0.2), at('flood', 0.6))`, return the value beside `show`, and let `stage.js` read it from
the state. A share of the whole story is `T / ids.length`. Why: the chapters come from the HTML, so a raw story time
(`fade = ramp(T, 2.0, 2.85)`) or a fixed count (`T / 7`), as run 2 wrote, points at the wrong chapter as soon as one
is added or moved. The same goes for links: the starter's `jumpTo(id)` lands on `T = at(id, REST)`, where the
chapter's move is done and its text is up.

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

## Stills for the page without WebGL

Without WebGL (or with JavaScript off) the starter's page is an article; each chapter can show a still of its shot.
Make them from the finished site: `check_site` writes a frame per step, or open the built page in a browser, call
`window.__site.setProgress(i + 0.7)` for chapter `i` (0.7 is where reduced motion rests) and take a screenshot of the
window with the text hidden. Save them as WebP beside the page and add one per panel:

```html
<img class="still" src="stills/how.webp" alt="The gate from the front, the stream of lines entering it" width="1280" height="800" loading="lazy">
```

`starter/src/style.css` shows `.still` only in the article layout. The `alt` text says what the picture shows, since
it is the scene's only description for a reader who cannot see it.

## How long a chapter is

A chapter must last as long as its text takes to read plus its move: about one window height of scroll for the move
and one for every 40 words of the panel. The starter's chapters are 220 % of the window's height (the first 180 %);
change `min-height` per chapter in the CSS when a chapter says more or less. Too short and the camera arrives after the
reader has left; too long and the scroll feels stuck.
