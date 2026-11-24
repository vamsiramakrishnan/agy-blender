---
name: scroll-3d-website
description: >-
  Builds a scroll-driven 3D website for a project: a landing page, product page
  or explainer in which scrolling moves a camera through a three.js scene, with
  the few 3D props it needs built by this plugin and installed with their
  loader, and shaders, smooth scrolling and effects from current web libraries
  (GSAP, Lenis, postprocessing, React Three Fiber). Gives the order of work
  (storyboard, page and scene from a tested starter, props, a look with
  check_site, revision), what makes a site scroll beautifully and a guide to
  the libraries with checked examples. Use when
  the user asks to generate, build, design or improve a website, web page,
  landing page or scrollytelling explainer, says it should scroll beautifully,
  or names three.js, WebGL or shaders for a site, also when it asks for 3D
  models: read it before modelling for the site. Not for
  modelling one prop on its own (use parametric-kitbash) or for putting a
  finished package into a game or app that already exists (use
  install-into-project).
---

# Scroll-driven 3D website

A page that tells how something works, one chapter at a time, while a fixed 3D scene behind the text moves with the
scroll. This skill is the method; three files beside it hold the details, read when the step needs them:

- `starter/`: the smallest project with the plumbing right (scroll to story time to scene state, chapters read from
  the page, a camera of shots, light with shadows, the installer's loader, reduced motion, no WebGL, a hook for
  checks). It is plumbing, not a design. Its test runs it in a browser with an installed prop.
- [libraries.md](libraries.md): what each current library is for, when not to use it, its version, and one example
  that ran against that version.
- [recipes.md](recipes.md): framing a shot from a prop's size, facts with their source, stills for the article
  fallback, chapter lengths.

## Read first, model later

1. Read the project the site is about: its README, its docs, and its own design rules when it has them (a design or
   brand page, a style guide, colour and type tokens). Write down, in two or three lines, what those rules allow and
   forbid. Run 1 read the project's visual design page and then used glow, particles and a gradient dome, which that
   page rules out.
2. Read the workspace's package.json: use the libraries it has installed and no others (a sandbox cannot install).
3. Do not start modelling. The storyboard decides which props exist; most chapters need none of their own.

## The order of work

| Step | What you make | Tools | Share of the time |
|---|---|---|---|
| 1. Storyboard | the chapter table below, in a file in the workspace | none | 10 % |
| 2. Page and scene | the starter copied in, chapters, shots, stand-ins, layers | `check_site` once | 30 % |
| 3. Props | two to four props, built, delivered, installed | the plugin | at most 30 % |
| 4. Look | contact sheets at desktop and phone size, the measures | `check_site` | 10 % |
| 5. Revise | the fixes the look found, then the look again | `check_site` | 20 % |

Do the page before the props: the starter draws a grey stand-in for each prop, so the shots, the layout and the
chapter lengths can be judged before any modelling, and a prop that runs late costs one stand-in, not the site. In
run 1 the three props took 45 of 69 minutes and the site itself 20: one pass and three small camera fixes.

### 1. Storyboard

One row per chapter, five to eight chapters. Each chapter says one thing.

| id | the visitor reads (one idea, at most 60 words) | the 3D shows (subject, shot, what changes) | facts and their source |
|---|---|---|---|
| intro | what the project is, in one line | the main prop, three-quarter view, still | none |
| how | ... | ... | each number, with the file and place it is copied from |

Then list the props: the objects that appear in more than one chapter or carry the story. A chapter that needs
something small and repeated (tokens, lines, rows) draws it in the page's own code as one `InstancedMesh`, not as a
prop. Everything else is a shot of the same world: the site is one place the camera travels through, not a slideshow
of scenes.

### 2. Page and scene

Copy `starter/index.html`, `starter/src/` and `starter/vite.config.js` into the project (keep the workspace's
package.json; the starter needs only `three` and `vite`). Then, each a local edit:

- a `<section class="chapter" data-chapter="id">` per storyboard row in `index.html`, its text in its panel;
- a shot per chapter in `src/shots.js` (`SHOTS`), and which chapters each prop is on stage for (`SHOW`); name what each
  shot is about (`subject: 'hero'`, or a list): then `check_site` reports a chapter's subject under the panel or cut
  by the frame, and does not count a prop that is only in the background of that shot;
- a stand-in per prop in `src/props.js` (`PROPS`: where it stands and its rough size in metres);
- the project's colours and type in `src/style.css`; its three frame properties (shift right, shift up, widen) decide
  where the 3D subject sits beside the text. A shot is designed for a horizontal coverage, what it shows across a
  1440 x 900 window (`DESIGN_ASPECT` in `src/shots.js`): a narrower window, a phone's, is widened to keep it, so frame
  and judge shots at that shape ([recipes.md](recipes.md)). Why: run 2 widened its phone view by a fixed factor and cut
  its props at both side edges;
- links to a chapter (`<a href="#how">`) and any jump use `jumpTo(id)`, which lands where the chapter rests, and every
  timing of a layer is named by chapter with `at`, as in `t = at('how', 0.5)`, in `evaluate` (`src/shots.js`). Why:
  at a chapter's top the camera still shows the previous picture, where run 2's links landed, and its
  `fade = ramp(T, 2.0, 2.85)` and `T / 7` broke the rule that chapters come from the HTML;
- the layers the design asks for, from [libraries.md](libraries.md): Lenis for smooth wheel scrolling, GSAP
  ScrollTrigger for page effects, postprocessing for ambient occlusion and a little bloom, troika for text in the scene.
  Before writing a shader read its two rules there (colours as `THREE.Color` uniforms, edges smoothed with `fwidth`).

`npx vite` serves it; `npx vite build` writes the site to `dist/` with relative paths, so it works from any folder of a
host (`npx vite build --outDir site` when the task names another folder for the result). Look at it with `check_site`
once now, with stand-ins: the shots and the layout are cheap to fix today.

### 3. Props

For each prop, in this order:

- Build it as the always-on rules say for this mode, with the skill `parametric-kitbash`. In the default mode:
  `open_asset_session` with this conversation's `conversation_id` (the session's first message gives it), the user's
  words for that object as `brief`, `target_engine` set to web_three and a `max_texture_px` chosen for the screen
  (below); then `apply_scene_ir` and `render_preview` as usual. In script
  authoring (the rules say an asset is a Python script): `Asset(..., engine="web_three", max_texture_px=...)` in its
  script, run with run_build.
- `deliver_package`. If its answer starts with `STILL WORKING`, nothing failed and nothing needs fixing: the work
  goes on by itself. Work on the page meanwhile and make the call the answer spells out (the same tool, the same
  arguments) to collect the result. A delivery that runs while you write the page costs no time.
- `install_into_project` into the site's project. The site is a project you are building in this session because the
  user asked for it, so call `install_into_project` with `dry_run` false at once, and never copy GLB files by hand
  (Antigravity asks the user to approve each install: that is expected, wait for it). It writes the files where the
  dev server serves them and a loader with levels of detail (and compresses for the web when the project has the
  tools). The delivery answer ends with a `NEXT:` line that gives this call and names the loader file.
- In `src/props.js` add two fields to the prop's entry: `asset`, the asset's name, and `module`, your own import of the
  loader file the install wrote (`() => import('./agy3d/<asset>/index.js')`; the install's answer names the folder).
  The stand-in stays as the fallback while the model loads or if it fails.

Texture size for the screen: the delivery answer for web_three states the atlas size, the bytes the page downloads,
the texture memory and up to how many pixels across on screen the textures stay sharp. Compare that last number with
the widest the prop appears in your shots, in pixels: its share of the window's width (the starter's
`window.__site.info().onScreen` gives the box around it as a share of the window at any moment) times the widest
window you design for, times 2 for a high-density screen. Choose the size from it, never by scaling the model. With no size given a web prop gets an atlas of at
most 2048 px; ask for 4096 only for a prop the camera comes close to, and for 1024 or 512 for one seen small or far
away, which needs neither fine parts nor large maps.

### 4. Look, 5. Revise

Build the site and run `check_site` with `target` the built folder and `sources` the folder of the project the site is
about, so the facts on the page are compared with that project's files. Open both contact sheets (desktop and phone) and look at every step, not
only the chapter stops. Then fix in this order and look again after each round: what is broken (errors, failed
files, a fallback where the scene should be), dead frames, framing (subject behind text, cut off, too small),
light and grounding, then polish.

## What makes a site scroll beautifully

Each is one instruction and its reason. The starter already does the ones marked (starter).

- **Every scroll position is a composed frame.** Plan the moves between chapters as carefully as the holds: the
  subject stays in frame during a move, and a prop leaves only when the next one is in view. Why: a visitor sees the
  moves as much as the stops; run 1's canvas was an empty grid for a large part of the scroll between its stations.
- **The scene's whole state is a function of scroll position.** (starter) Compute everything the scene shows from
  story time T; never ease a value toward a target per frame (no `lerp` by frame time, no tween started by a trigger).
  Smooth the input (Lenis), not the state. Why: then any position can be scrubbed, reversed, linked to and
  photographed, and it looks the same however the visitor arrived; run 1 eased its progress per frame, so the
  picture at a position depended on how fast one had scrolled there.
- **Camera moves are shots with holds.** (starter) Each chapter moves to its shot in the first third and holds (with
  a slow push-in) while the text is read; a move that would pass through a prop gets a `via` pose. Why: a camera that
  never stops cannot be read; run 1's camera rode one spline with no holds.
- **Light grounds the objects.** (starter) An environment for reflections, one key light with a shadow whose box
  follows the shot, a soft contact shadow under each prop, fog that fades the ground into the page colour. Why:
  without a shadow an object floats; nothing cast a shadow in run 1.
- **Many small things are one draw call.** Draw repeated items as one `InstancedMesh` (or one geometry). Why: draw
  calls, not triangles, are what a phone runs out of first.
- **Effects are used sparingly and measured.** Add a pass (ambient occlusion, bloom, depth of field) only when a
  frame is better with it, and keep it only when `check_site`'s draw calls and the phone frame stay acceptable. Why:
  every pass is a full-screen draw at device resolution, and a weak scene does not get better under glow.
- **Type and layout leave the 3D room.** (starter) One panel at a time, in a fixed layer cross-faded by chapter, on one
  side (desktop) or below (portrait phone); the camera frames the subject in the free part. Only `.webgl-on`, set once
  the 3D runs, may dim, hide or move a panel. Why: run 1's six panels covered the scene they described; run 2's phone
  panels slid up over it, and its panels, hidden under a class set before any script, vanished when the script did.
- **Something shows at once.** (starter) The text is HTML and readable before any script; the scene draws its stand-ins
  on the first frame and swaps each model in when it has loaded. Why: a blank canvas while 20 MB load reads as broken.
- **Reduced motion, no WebGL and a phone are designed, not an afterthought.** (starter) Reduced motion: no camera
  travel, one resting picture per chapter, no smooth scrolling. No WebGL: the page is an article with stills in place of
  the scene ([recipes.md](recipes.md)). Phone: the scene above, the text below, no sideways scroll, a lower pixel ratio.
  Why: these visitors exist, and run 1 handled none of them.
- **Draw only when something changed.** (starter) Why: a page that redraws a still picture sixty times a second
  drains a phone's battery, and `check_site` reports it.
- **A fact carries its source, and nothing that looks like a receipt is invented.** Every number, quote, code line or
  identifier on the page is copied from a file of the project and its element names that file with `data-src`
  ([recipes.md](recipes.md)). An illustration that is not real says so in its caption. Why: run 1 showed a "digest
  specimen" with a log line and an id that are in no file, and an example with made-up line numbers.
- **The project's own design rules win.** Use its colours, type and the motifs it allows; leave out what it forbids,
  however fashionable. Why: the site is the project's voice; run 1 used effects the project's design page rules out.

## Check

The site is done when `check_site` on the built folder reports, at both sizes:

- no uncaught error, no console error, no failed request, no request to another host the task does not allow;
- no step where the canvas shows only its background, and no two neighbouring steps that are identical unless a hold
  is meant;
- reduced motion: the scene rests per chapter; no WebGL: the page reads as an article;
- no sideways overflow on the phone; no fact that disagrees with its source, and nothing shown without a source that
  looks like a fact;
- no subject or label hidden by the text panel or cut by the frame's edge at any step; `check_site` reports both
  when the page keeps the starter's `window.__site.info().onScreen` hook, so keep it, name each shot's `subject` and
  register labels with it;
- draw calls, triangles, texture memory and bytes you can state and defend (each prop through the installer, no
  model above a few MB).

Look at the contact sheets yourself as well: the measures find broken frames, not ugly ones. Between looks, the
page's own hook answers quickly: `window.__site.setProgress(T)` draws story time T, and `window.__site.info()` gives
the draw calls and `onScreen`: for each prop and label (`kind`) on screen, its share of the window, its centre, its
whole box (`box`, fractions of the window, below 0 or above 1 where it runs out of the frame) and, when the shot names
one, whether it is the shot's `subject`.

## Gotchas

- **Symptom:** the props take most of the session and the site gets one pass. **Cause:** modelling started before the
  storyboard, and each prop was polished as if it were the product. **Remedy:** page and stand-ins first, props capped
  at a third of the time; a late prop keeps its stand-in while the site is finished.
- **Symptom:** a tool call is cut off after three minutes and the agent polls folders or switches to stage calls.
  **Cause:** Antigravity ends any MCP tool call after 3 minutes; run 1 lost two `deliver_package` calls that way.
  **Remedy:** every tool now answers inside that limit: an answer that starts with `STILL WORKING` means the work
  goes on, so make the call it spells out again; do not poll folders and do not switch to other tools.
- **Symptom:** a 17 MB model, then a prop scaled to 0.65 and loaded at 1/0.65 to shrink its textures. **Cause:** no
  texture-size lever was used. **Remedy:** pass a `max_texture_px` chosen for the screen to `open_asset_session`
  (section 3).
- **Symptom:** models copied into the site by hand and loaded with a hand-written `GLTFLoader`: no levels of detail,
  no compression, a 22.8 MB site. **Cause:** the installer was not used because its description asked for a plan to
  show first. **Remedy:** `install_into_project` with `dry_run` false into the project you are building, then the
  `module` line in `src/props.js`.
- **Symptom:** screenshots differ when the same position is reached from above or below, and scrolling back plays a
  different film. **Cause:** progress, colours and uniforms were eased toward a target every frame. **Remedy:** compute
  them from story time (the starter's `shots.js`); smooth scrolling belongs to Lenis.
- **Symptom:** the page works at the server root and loses its models under a sub-path. **Cause:** an absolute path
  (`/models/...`), or the installer's loader called outside Vite, where its default base is the site root.
  **Remedy:** relative URLs, Vite's `base: './'`, and the starter's `loadProp`, which passes the base URL relative to
  the page.
- **Symptom:** checks only at the chapter stops pass, and the visitor sees an empty grid between them. **Cause:** the
  agent wrote its own screenshot script for six stops. **Remedy:** `check_site` takes even steps over the whole scroll;
  look at all of them.
- **Symptom:** console warnings "PCFSoftShadowMap has been removed" and "Clock: This module has been deprecated".
  **Cause:** code written for three.js 0.170 or older running on 0.187. **Remedy:** `PCFShadowMap` with
  `shadow.radius`, no clock (state follows scroll); see [libraries.md](libraries.md) for what else moved.
- **Symptom:** text in the scene or an environment preset is missing offline, or a strict host refuses the page.
  **Cause:** troika's default font and drei's environment presets are fetched from CDNs at run time. **Remedy:** serve
  the font file and build the environment from the scene (`RoomEnvironment`, Lightformers).

## Version notes

Last verified 2026-11-24 against three 0.187.0, gsap 3.15.1, lenis 1.3.28, postprocessing 6.39.6, n8ao 2.1.0,
troika-three-text 0.53.1, three-custom-shader-material 6.4.0, three-mesh-bvh 0.9.18, detect-gpu 5.0.70, React 19.3.1
with @react-three/fiber 9.8.2 and drei 10.7.9, glTF-Transform 4.5.1 and Vite 8.3.8 (each example in libraries.md ran
against these), and three 0.187.0 for the starter's repository test. postprocessing 6.39.6 declares three below
0.188: check its range before moving three on. Run 1 (the evidence for the gotchas) is recorded in the repository's
`benchmarks/results/site_run1/`.
