# Technical review of run 2's site (three.js and library use), by an independent reviewer

The reviewer read all of the site's source, the skill and its starter, looked at 18 screenshots and loaded the built
site in headless Chromium. Findings are most serious first. "Skill verdict" says whether the skill or its starter
CAUSED the fault, is SILENT about it, or says the right thing and the agent did NOT FOLLOW it. Paths: the site's
source is `benchmarks/results/site_run2_plugin/site_src/` in the repository; the skill is
`plugin/antigravity-3d/skills/scroll-3d-website/`. Line numbers of site files refer to the agent's files.

1. **The floor shader writes sRGB colour values as if they were linear, so the dark ground renders light grey and
   everything looks washed out.** `evidence.js:50-52, 70`: `vec3 base = vec3(0.055, 0.064, 0.078)` is #0E1014 typed as
   a vec3; `majorCol` is #252B33; the rail `vec3(0.435, 0.588, 1.0)` is #6F96FF. In three-custom-shader-material 6.4.0
   (`vanilla/three-custom-shader-material.es.js`) `diffuseColor = csm_DiffuseColor` is inserted after
   `<color_fragment>`, so these values enter the lit, linear pipeline unchanged: albedo about #42474F instead of
   #0E1014; on screen the floor is (88,95,105) against a background of (13,15,18). Fix: pass colours as uniforms built
   from `THREE.Color` (three converts them to linear). **CAUSED**: the custom shader material example at
   `libraries.md:310` does the same (`vec3(0.17, 0.42, 0.69)` and `vec3(0.88, 0.64, 0.23)` are the bytes of `#2b6cb0`
   and `#e0a33a`, which the TSL example at `libraries.md:119` passes through `color()` correctly). The example should
   use colour uniforms and say once: colours in GLSL are linear; never type sRGB hex values as `vec3`. Confidence high.
2. **If the main script fails to load, or before it has run, every text panel is invisible.** `style.css:407-418`:
   `.js:not(.webgl-off) .panel { opacity: 0; pointer-events: none }`; only `.webgl-on .chapter.is-current .panel`
   brings one back, and only `main.js` adds `webgl-on`. Reproduced by blocking `index-*.js`: class stayed `js`, all
   seven panels at opacity 0. Fix: dim only once the script has confirmed the 3D is on
   (`.webgl-on .chapter:not(.is-current) .panel {...}`), keep `.js` for layout only. **CAUSED**:
   `starter/src/style.css:28-33` ties panel strength to a class only the script adds (the starter dims to 0.25, the
   agent went to 0). Confidence high.
3. **On the phone, props are cropped at the sides because the field of view is widened by a fixed factor instead of
   by the window's shape.** `stage.js:411-412`: `camera.fov = 2*atan(t0 * frame.fit)` with `--frame-fit: 1.36` on the
   phone. At 390x844 the horizontal view is about 20 degrees against about 51 on desktop; `phone_04` to `phone_07`
   show the vault and the clamp cut by both side edges. Fix: hold the horizontal coverage below a reference aspect
   (`const ref = 1.6; let t = t0 * frame.fit; if (W/H < ref) t *= Math.min(ref / (W/H), 2.2);`) or compute it from the
   prop bounds as the recipe in `recipes.md` does. **CAUSED**: `starter/src/stage.js:110-111` and
   `starter/src/style.css:38` use the same constant. The starter should make the view fit the aspect ratio, and the
   skill should say a shot is designed for a horizontal coverage, not a vertical field of view. Confidence high.
4. **On desktop, subjects sit under the panel or off the frame edge at the steps shown.** Sampled with
   `__site.info().onScreen`: digest chapter (T 2.5 to 3.0) `capture_gate` centred at x=0.49 covering 44 % of the
   window while the panel spans about 0.04 to 0.38 (`desktop_03` shows the gate and the red stacks under the panel);
   final shot (T 6.5 to 7) `anchor_clamp` at x=0.96 with a 3 % share, cut by the right edge (`desktop_08`); harnesses
   chapter: the clamp at x=0.90. Fix: frame each shot with `frame()` from `recipes.md` using the free area's aspect
   and the prop bounds; check every prop's box lies in x 0.42 to 0.98. **NOT FOLLOWED**: `SKILL.md:121-124` says to
   fix framing after looking, `recipes.md:5-34` gives the framing function. Confidence high.
5. **On the phone, each chapter's panel slides up across the 3D area during the camera's hold.** The panel is
   `position: sticky; top: 49vh; max-height: 48vh` inside a 225vh section (`style.css:447-453`); it stops sticking at
   about 57 % of the chapter and slides up over the top half until about 78 %; the next panel stays at opacity 0 until
   about 75 %. `phone_02`: the flood panel sits mid-screen over the red stacks. Fix: a fixed panel layer on the phone
   (`.webgl-on .panel { position: fixed; left: 12px; right: 12px; bottom: 12px; }`, cross-faded by `.is-current`), or
   make the sticky container end with the chapter. **CAUSED**: `starter/src/style.css:39` has the same
   sticky-in-section phone panel (56vh plus 40vh). Confidence medium-high.
6. **The 3D labels lie flat on the floor at grazing angles and are often cut by the frame edge or hidden by the
   panel.** `evidence.js:188-195` places most labels with `rotX = -pi/2` at y=0.015. `desktop_01`: "L14,238 QUIET
   NEEDLE" half behind the panel, "RAW FLOOD" cut at the bottom edge; `desktop_04`: two labels cut at the bottom edge;
   `phone_02`: "302,628 TOK RAW" huge and cropped; `desktop_08`: a label a few pixels tall. Fix: stand labels upright
   near prop height and face them to the camera, or anchor them inside the free area; include them in the on-screen
   report like props. **SILENT**: the skill recommends troika for labels, but `onScreen()` in the starter
   (`stage.js:140-160`) measures only props. It should also report labels, and each box's overlap with the panel
   rectangle and the frame edges. Confidence high.
7. **The chapter-number links land on the previous chapter's picture.** `main.js:79` calls `lenis.scrollTo(target)`
   with the section top, which is T = i, the start of the camera move. Fix: `lenis.scrollTo(scrollFor(boxes, idx +
   REST))`, same target in the reduced-motion branch. **SILENT**: the starter has no navigation; it should ship a
   `jumpTo(id)` that targets `i + REST`. Confidence high.
8. **The canvas is drawn at `innerHeight` but displayed at `100lvh`, and every mobile address-bar resize triggers a
   full re-measure and `ScrollTrigger.refresh()`.** `main.js:196-199` calls `stage.resize(innerWidth, innerHeight,
   ...)` and `ScrollTrigger.refresh()`; `stage.js:395` calls `setSize(W, H, false)` (leaves the CSS size alone) while
   `style.css:163` sets `height: 100lvh`. In gsap 3.15.1 `ScrollTrigger.js:397` and `:2047` already ignore
   address-bar resizes on touch devices; the unconditional refresh undoes that. Fix: size from the element itself
   (`canvas.getBoundingClientRect()`, ideally a `ResizeObserver` on the canvas); skip `measure()` and `refresh()` when
   only the height changed by less than 25 % on a touch device. **CAUSED**: the starter does the same
   (`starter/src/main.js:266`, `starter/src/style.css:24`). Confidence medium (mechanism verified in code; a real
   address bar could not be reproduced headlessly).
9. **Mouse-wheel scrolling over the panel is not smoothed on desktop.** `main.js:64-66` puts `data-lenis-prevent` on
   every `.panel`. In `lenis/dist/lenis.mjs:611` any element with that attribute in the event path makes Lenis return
   without `preventDefault`, so native scrolling takes over; desktop panels do not scroll internally yet cover about
   38 % of the window. Fix: remove the attribute and use `new Lenis({ anchors: true, allowNestedScroll: true })`,
   which only stops smoothing for an element that can actually scroll. **CAUSED**: `libraries.md:171` and `:180-181`
   tell the agent to add the attribute to every panel. Confidence high.
10. **The slab shader's "edge bevel" is a constant 22 % darkening, not a bevel.** `evidence.js:125-127`:
    `rim = max(|x|, |y|, |z|)` of the unit-box position is always 0.5 on every face. The stripe term is a hard `step`
    with no anti-aliasing; the floor shader's rail, frame lines and progress edge also use `step` without `fwidth`.
    Fix: use the distance to the nearest edge on that face and anti-alias with `fwidth`
    (`smoothstep(-w, w, ...)`, `w = fwidth(...)`). **SILENT**: a bug in the agent's own shader; a one-line note in
    `libraries.md` on anti-aliasing procedural edges would help. Confidence high.
11. **A lost WebGL context is never handled.** No `webglcontextlost` / `webglcontextrestored` handler; three restores
    its own state but nothing redraws and the environment map from `fromScene` is not rebuilt. Fix:
    `canvas.addEventListener('webglcontextlost', e => e.preventDefault())`; on restore rebuild the environment and set
    `dirty = true`, or fall back to the article layout. **SILENT**: the starter has no handler; it should ship one.
    Confidence high.
12. **Two extra WebGL contexts are created and never released.** `main.js:33-40` creates a WebGL2 context only to test
    for support; `getGPUTier` creates a WebGL1 context and calls `readPixels` on it (no `loseContext` in
    `detect-gpu/dist/detect-gpu.esm.js`; Chromium logged "GPU stall due to ReadPixels"). Fix: after the support test
    call `ctx.getExtension('WEBGL_lose_context')?.loseContext()`, or pass the renderer's context:
    `getGPUTier({ glContext: renderer.getContext(), benchmarksURL })` (the option exists in 5.0.70). **CAUSED**: the
    support test comes from `starter/src/main.js` (`webglWhyNot`), and the detect-gpu example at `libraries.md:387`
    does not pass `glContext`. Confidence high.
13. **The scene's start waits for each step in turn: the GPU benchmark data, then the 0.7 MB stage chunk, then the
    first frame.** `main.js:141` awaits `detectQuality()` before `await import('./stage.js')` at line 152. Fix:
    `const [quality, { createStage }] = await Promise.all([detectQuality(), import('./stage.js')]);` and give the
    tier lookup a timeout of about 1.5 s. **SILENT**: `libraries.md:376` says to read the tier before the first frame,
    but not to load in parallel. Confidence medium.
14. **The animation timings use hard-coded story times and a fixed chapter count of 7, which breaks the starter's
    rule that chapters come from the HTML.** `stage.js:448` has `bench.setProgress(T / 7)`; `evidence.js` is full of
    thresholds such as `ramp(T, 2.0, 2.85)`. Fix: pass the chapter ids into the layer and key the timings on them
    (`const at = (id, f) => ids.indexOf(id) + f`), use `T / ids.length`. **SILENT**: the starter's `evaluate` returns
    `index` and `local`, but the skill shows no layer keyed by chapter id. It should include an `at(id, local)`
    helper. Confidence high.

## What the site does well technically (keep these)

- Every value is a pure function of story time T; the camera moves through monotone curves with holds and
  intermediate poses; reduced motion rests at a fixed point in each chapter.
- Drawing on demand works: 0 draws in 84 idle frames; troika label loading and prop loading both trigger a redraw.
- Cheap scene: 14 draw calls, about 32k triangles; all slabs are one instanced mesh of 240, which casts shadows.
- Per-instance colour is kept correctly (the custom shader material replaces `diffuseColor` after
  `<color_fragment>`, so passing the instance colour through a varying is right; the slab colours come from colour
  objects, so they are correct in linear space).
- Shadows: `PCFShadowMap` with `radius` (correct for three 0.187), a shadow box that follows the shot, normal bias,
  contact shadows under the props.
- Lenis is driven from the GSAP ticker as `lenis.raf(s*1000)` with `lagSmoothing(0)` and `ScrollTrigger.update` on
  every scroll; detect-gpu uses locally served benchmarks and never switches the 3D off; troika uses a locally served
  `.woff` font.
- A prop that fails to load keeps its stand-in; all paths are relative; the no-WebGL article has lazily loaded stills
  that stay hidden in 3D mode; grid lines are anti-aliased with `fwidth`.

## The reviewer's five changes that would most improve the next site

1. Colour space: fix the custom shader material example in `libraries.md` to use colour uniforms, and add the rule.
2. Panel layout: in the starter's CSS replace the sticky-in-section panels with a fixed panel layer cross-faded by
   `.is-current`, at least on the phone; apply dimming only under `.webgl-on`, never under `.js`.
3. Framing: make the starter's field of view fit the window's aspect ratio (horizontal coverage held below a
   reference aspect); size the canvas from its own box with a `ResizeObserver`; do not re-measure or refresh
   ScrollTrigger on address-bar-only resizes; extend `onScreen()` to cover labels and to report overlap with the panel
   rectangle and edge clipping; add "no subject or label clipped by panel or edge" to the Check section.
4. Lenis and navigation: in `libraries.md` replace `data-lenis-prevent` on every panel with `allowNestedScroll: true`;
   ship a `jumpTo(id)` in the starter that scrolls to `scrollFor(boxes, i + REST)`.
5. Robustness: a context-loss and restore handler in the starter; release the support-test context; in the
   detect-gpu example pass `glContext: renderer.getContext()` and load the tier and `stage.js` in parallel with a
   timeout; an `at(chapterId, local)` helper so layers key their timing on chapter ids.

One observation the reviewer could not confirm: in every full-window screenshot, including the check's own (for
example `desktop_01.jpg`), the bottom 48 px show a flat background-coloured band, while the canvas itself is drawn to
the bottom (`desktop_08_canvas.jpg`); the band disappears when the fixed top bar is hidden. It may be a compositing
artefact of headless software rendering.
