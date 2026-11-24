# Run 3: three.js and library review

*An independent review, made after the third run by a reviewer that took no part in building the plugin or the site.
It read the site's source, the skill the agent was taught from and the frames of the built site; it started no
browser. One sentence was corrected by the integrator and is marked: the review said the site check has no minimum
size for a label, where it has one that only speaks of a label that is never readable at any step. What was done
about each finding is in `../../ROUND_EIGHT.md`.*

Sources: `benchmarks/results/site_run3_plugin/site_src/` (the "site"), the skill in `plugin/antigravity-3d/skills/scroll-3d-website/`, and the frames of the site at nine scroll steps on a desktop and a phone. Pixel values were sampled from the canvas-only frames. Cause: (a) the skill, guide, recipes or starter taught or caused it; (b) the skill says nothing about it; (c) the skill says it and the agent did not do it.

Run 3 is clearly better than run 2. The colour-space fault, the sliding panels, the flat labels and the crops are gone, and every plumbing finding from the second review is fixed. What remains is mostly about the picture itself.

## Findings, most serious first

**1. On the phone, the evidence each chapter is about is out of frame, and the picture does not move.**
- **Evidence:**
  - Phone steps 0 to 4 report the gate's box as `[0.185, 0.172, 0.815, 0.466]`, and steps 5 to 7 give the same vertical extent to the third decimal (`report.json`, phone steps).
  - In `phone_02` and `phone_03` the red flood stack, the subject of chapters 2 and 3, is a sliver cut by the left edge. In `phone_05` the retrieved slice is cut too.
  - In the code, `fitted()` (`stage.js:212-226`, applied at `244-256`) rewrites `camera.fov` and the view offset on every frame so the named prop always fills `FILL`. This cancels the hold's dolly push, and nothing that is not a prop counts towards the frame.
- **Why it matters:** on the phone the story is told by the slabs, and the visitor sees a fixed portrait of a box that only turns.
- **Fix:** let a shot's subject include world-space boxes (`{ box: [min, max] }`, or a layer's `InstancedMesh.computeBoundingBox()`). Apply the push as a scale on the fitted size (`s *= 1 + push * local`) so the hold still closes in.
- **Cause (a):** the starter's `stage.js` says "the camera keeps the shot's position and direction and only the view ... is fitted: the subject's box fills FILL". Its `problems()` in `shots.js` rejects any subject that is not a prop in `SHOW`.

**2. Every chapter uses the same shot, so on desktop the picture hardly travels.**
- **Evidence:**
  - In `shots.js:8-58`, five of the six shots sit 4.9 to 5.8 m from their target, 13 to 14 degrees above it, with a 30 to 32 degree lens.
  - `--frame-x` puts the subject in the same slot every time, centred near x = 0.71.
  - A hold takes 65 % of each chapter (about 1.4 window heights of scroll) and closes in by only 5 to 11 %.
  - `desktop_00` and `desktop_01` are almost the same picture; the sheet reads as a carousel of three boxes.
- **Why it matters:** in a scroll-driven site the camera's travel is the experience. There is no change of scale, no low or top-down angle, no foreground passing the lens and no move into a part.
- **Fix:** give each shot its own scale and height, for example a macro of the needle slab at 1.2 m, a top-down view of the bus rails, a low pass along the intake. Let the hold drift instead of standing still: a `via` path, a 20 to 30 % push, or an orbit of a few degrees.
- **Cause (a):** `SKILL.md` says "Each chapter moves to its shot in the first third and holds (with a slow push-in)". The starter's `DEFAULT_PUSH = 0.05`, its placeholder shots and the recipe's `frame()` defaults (`elevation = 18, fill = 0.55`) all teach a single product-portrait shot. Nothing in the skill asks for different shot scales.

**3. The design's colours are washed out on screen, and the ground renders 2.5 times its own colour.**
- **Evidence:** pixels sampled from the canvas-only frames.

| Element | Set as | On screen |
|---|---|---|
| Red slabs | `#FF7768` (255,119,104) | (252,150,140), `desktop_02` |
| Amber straps and caps | `#F0B429` (240,180,41) | (246,190,103) to (250,196,106) |
| Ground | `#15181D` (21,24,29) | (47–60,54–62,60–69) |

  The rails come out as (139,163,253) against #6F96FF.
- **Mechanism:** one key light at 2.7, plus a rim light at 0.75 and a fill light at 0.65 that the agent added, plus `environmentIntensity` 0.95 (`stage.js:52-53, 69-87`). Together they push lit albedo above 1, and `NeutralToneMapping` then compresses those values towards white, which costs saturation.
- **Why it matters:** the project's design rules allow "semantic colour only". The 3D amber visibly fails to match the panel's amber, and the pastel red reads as plastic.
- **Fix:**
  - Drop the fill light, bring the environment down to about 0.4–0.5 and the key light to about 2.
  - For the objects that carry the design's colours, keep lit albedo × light below about 0.8, or use `toneMapped: false` with an emissive or `MeshBasicMaterial` part.
  - Check a rendered swatch against its hex value.
- **Cause (c) and (b):** the starter says "one key light that casts the shadow, nothing else" and uses `environmentIntensity` 0.55, and the agent added two lights. The skill never asks for on-screen colour to be checked against the design's hex values, and `check_site` does not measure it.

**4. The ending is the weakest picture of the story.**
- **Evidence:** in `desktop_08` the three props span x 0.46 to 0.96 and y 0.42 to 0.62, and the bottom 40 % of the frame is empty grid. The labels are 15, 11 and 15 px tall on desktop and 7, 5 and 7 px on the phone (`report.json`, step 8), which nobody can read.
- **Why it matters:** the final frame is the one the visitor leaves with.
- **Fix:** frame the establishing shot from the union of the three props' bounds with `frame()`, and lower the camera. Hide the troika labels when their projected height falls below about 14 px, or keep a constant on-screen height with `label.scale.setScalar(distance * k)`.
- **Cause (b) for the labels:** the check reports a label that is never large enough to read at any step, and says nothing of one that is too small at the step where it is shown. *(Corrected: see the note at the top.)*
- **Cause (c) for the framing:** the SKILL says "Every scroll position is a composed frame".

**5. There is no ambient occlusion, and the slabs are sharp-edged boxes, so the evidence looks like toy blocks.**
- **Evidence:** `evidence.js:145-147` builds every slab as a `BoxGeometry(1,1,1)`, and no pass is applied after rendering. Stacked slabs show no darkening where they touch (`desktop_05`, `desktop_06`).
- **Fix:** use `RoundedBoxGeometry` (`three/addons/geometries/RoundedBoxGeometry.js`, radius about 0.004) so edges catch the light. Add an `N8AOPostPass` at half resolution; the design forbids bloom, not occlusion.
- **Cause (c):** `SKILL.md:91-92` names "postprocessing for ambient occlusion" as one of the layers, and n8ao is installed.

**6. The pictures do not carry the numbers.**
- **Evidence:** the flood is 96 slabs and the digest is 6 (`evidence.js:188, 236`). That is a 16:1 ratio, while the text says 570×.
- **Why it matters:** showing a quantity is what 3D is for on this page, and `InstancedMesh` draws 20,000 slabs in one call.
- **Fix:** draw the flood as thousands of instances (one per 15 to 30 tokens, set up once), and let the digest collapse to a handful.
- **Cause (b):** the skill teaches `InstancedMesh` for draw calls, not for showing scale.

**7. Each model's three levels of detail are loaded up front, and its textures are stored uncompressed on the GPU.**
- **Evidence:**
  - `agy3d/*/index.js:104-105` loads every level at once: 856 kB of LOD1 and LOD2 files out of 1.56 MB of models, for props of only 3.4k to 3.5k triangles.
  - LOD2 switches in at about 45 m, beyond the end of the fog at 42 m (`stage.js:60`), so it is never shown.
  - The textures are WebP, so the nine 1024-pixel atlases take 51.6 MB of GPU memory (`uploaded_bytes` in `report.json`; 58 % of 89 MB).
- **Fix:** skip levels of detail for props under about 20k triangles, or load the far levels lazily. Choose KTX2/Basis (`KTX2Loader`) when GPU memory matters more than download size: ETC1S would need about 6 MB here.
- **Cause (a):** the plugin's installer made both choices. The agent's report quotes it: "kept WebP because WebP produced a smaller web payload".

**8. GSAP is loaded only for a progress bar, and the page writes that bar twice with two different measures.**
- **Evidence:** `main.js:85-92` sets `--progress` from ScrollTrigger's scroll share, and `main.js:155-156` sets it from `T / ids.length`. The two values disagree because the chapters differ in height.
- **Fix:** keep the `draw()` line and remove ScrollTrigger, or use GSAP for something real, such as scrubbed panel entrances.
- **Cause (a) and (c):** the example at `libraries.md:167-171` shows the bar done with ScrollTrigger, beside the starter's story time.

**9. The intake rail turns an off-palette pink.**
- **Evidence:** `evidence.js:103` blends blue and red by `uFlood*(1-uDigest*0.4)`, leaving the rail at (243,171,219) (`desktop_05`, `reduced.jpg`).
- **Fix:** switch between the two colours with a `smoothstep`; never hold a blend of them.
- **Cause (b).**

**10. During moves the diverting slabs pass through each other and through the grey stack.**
- **Evidence:** in `desktop_03`, all 96 slabs move on the same `divert` value (`evidence.js:199-201`).
- **Fix:** give each slab its own delay (`clamp01(divert * 1.6 - i / 96 * 0.6)`) and route them through a lane with headroom.
- **Cause (b).**

**11. The no-WebGL stills inherit the desktop panel offset.**
- **Evidence:** in `nowebgl.jpg` the gate sits in the right 60 % of the still, and the left of the picture is empty floor.
- **Fix:** draw the stills with `--frame-x: 0`.
- **Cause (a):** `recipes.md` takes the stills straight from `check_site`, which draws with the page's own framing.

## By area

- **Renderer:** colour management and pixel ratio are fine (capped per GPU tier, 1.5 on the phone). Tone mapping is the right choice but driven too hard (finding 3). Shadows are fine: `PCFShadowMap` with `radius`, the shadow box follows the shot and snaps to texels, and contact shadows are drawn.
- **Lighting and environment:** too much fill (finding 3). `RoomEnvironment` is rebuilt after a lost context: fine.
- **Materials and custom shader:** the floor shader is now correct (`THREE.Color` uniforms, `fwidth` everywhere). The slabs are plain (finding 5).
- **Camera and scroll:** the camera is a pure function of T, with monotone curves and `via` poses: fine. The picture barely travels (findings 1 and 2).
- **Loading and levels of detail:** stand-ins appear first, then the models; the tier lookup runs in parallel with a timeout. The levels of detail and textures are wasteful (finding 7).
- **Text in the scene:** troika with a locally served font, upright, turned to face the camera, and reported to the check. Fine except in the finale (finding 4).
- **Post-processing:** none (finding 5).
- **Performance:** fine. Desktop peaks at 18 draw calls and 24k triangles, and nothing is drawn while idle (0 of 66 frames). Two minor points:
  - on the phone the whole canvas renders under an opaque panel that covers 41 % of the screen;
  - the check counted 3 WebGL contexts created, probably including troika's SDF generator (not verified).
- **Phone:** the layout is fine; the framing is not (finding 1).
- **Reduced motion and no WebGL:** reduced motion is fine. The no-WebGL page reads well, but the stills are off-centre (finding 11).
- **Libraries:** Lenis is fine. GSAP does almost nothing (finding 8). No library is loaded for nothing, and nothing is hand-written that a library does better, apart from the occlusion n8ao would give.

## Second review's findings: where they stand

1. Floor shader colour space: **fixed**. `evidence.js:23-37` passes `THREE.Color` uniforms.
2. Panels invisible without the script: **fixed**. Dimming now hangs only on `.webgl-on` (`style.css:415-443`).
3. Phone field of view crops the props: **fixed** for props (`stage.js:212-256`). A new form of the problem appears (finding 1).
4. Desktop subjects under the panel or the edge: **fixed**. No step has a subject hidden or cut (`report.json`).
5. Phone panel slides over the 3D: **fixed**. The panel layer is fixed at the bottom (`style.css:457-467`).
6. Labels flat on the floor: **fixed**. Labels stand upright and turn to face the camera (`stage.js:286-300`). Their size in the finale is a new problem (finding 4).
7. Chapter links land on the previous picture: **fixed**. `jumpTo` goes to `at(id, REST)` (`main.js:161`).
8. Canvas drawn at `innerHeight`, and refresh on every resize: **fixed**. A `ResizeObserver` sizes the canvas from its own box, address-bar resizes are ignored, and no `refresh()` is called (`main.js:164-168`).
9. `data-lenis-prevent` on the panels: **fixed**. `allowNestedScroll: true` (`main.js:74`).
10. Slab bevel and `step` aliasing: **fixed / not applicable**. The slab shader is gone, and the floor uses `fwidth` throughout.
11. Lost WebGL context not handled: **fixed** (`stage.js:169-170`, `main.js:121-124`).
12. Extra WebGL contexts: **fixed**. The test context is released and `glContext` is passed to detect-gpu. A third context remains, probably troika's.
13. Serial waits before the first frame: **fixed**. The tier lookup runs in parallel with a 1.5 s timeout.
14. Hard-coded story times and `T / 7`: **fixed**. Timings use `at()` (`shots.js:178-186`), and the share is `T / n`.

## What separates this from a world-class scroll site

1. **A camera language, not a repeated portrait.** The best sites change scale and angle from chapter to chapter, move through things, and keep moving while the visitor reads. What the skill should do:
   - replace "holds with a slow push-in" with a short shot vocabulary (wide, medium, macro, top-down, low tracking), and require each chapter to differ from the last in scale or angle;
   - raise the starter's push to about 0.2, or give each hold a small orbit;
   - have `check_site` flag neighbouring rests whose subject box and camera elevation barely differ.
2. **Frame the story, not only the props.** What the starter should do: let `subject` name layer boxes, make the phone fit keep the push, and have the check report "the subject of this chapter is off-frame" for instanced layers as well as props.
3. **Light and finish.** One strong key light, a low environment, ambient occlusion, rounded edges, and exact brand colours. What the skill should do:
   - add a lighting budget to `libraries.md`: key about 2, environment at most 0.5, no extra fill, and a note that under Neutral tone mapping lit albedo above about 0.8 desaturates;
   - add a swatch check to `check_site` that compares a rendered brand colour with its hex value;
   - make n8ao the default rather than an option.

   The props themselves are still plain kitbash; that is the plugin's modelling side and outside this review.
4. **Make the numbers visible.** The 570× claim should be something the visitor sees. What the skill should do: add a recipe that turns a fact into an instance count and a transformation (thousands of slabs collapsing to a handful), with the `InstancedMesh` set up once and each instance's timing staggered.
