# Performance results, before and after the fixes (2026-11-20)

The first version of this report measured the system as it was (commit `14814fd`) and listed the defects the
measurements exposed. None of them was changed at the time. This version covers what came next: each defect was
traced to its cause and fixed, more turned up along the way and were fixed too, and everything was measured again.

One rule governed the fixes: remove waste and raise quality, and never buy either by cutting capability. No
thinking level, candidate count, resolution or gate was lowered to produce a number in this report.

The data is in `results/`. [`README.md`](README.md) says what each file is and how to reproduce every table.

## Summary

What someone making assets has now that they did not have before:

- **Authoring calls return scenes.** Live, 10 of 10 one-shot authoring requests returned a scene that passes the
  geometry checks, on all four model tiers. Before, 1 of 10 did: the two larger models spent their whole output
  allowance thinking and returned three tokens.
- **A 1K texture request returns a texture**, and image spend is booked from what the provider reports.
- **Stage agents send the asset task, not a coding-assistant prompt.** Prompt tokens per turn fell from about
  21.8k to about 7.7k on the same task, with the same number of model calls and the same result.
- **UVs of primitives have no distortion.** The lamp post went from 0.083 area distortion, which is over the 0.05
  budget, to zero, and primitives reach the same or a higher texel density than before.
- **Unwrapping no longer dominates export.** A crate exports in 108 ms instead of 231, a 50,000-triangle mesh in
  3.4 s instead of 17.5.
- **LOD levels are bounded by what a viewer can see**: one pixel on a 1080-pixel frame, measured. Levels that
  were copies of LOD0, and levels that collapsed thin parts, are gone; three of the five template props, which
  had nothing real to remove, now ship LOD0 alone.
- **Packages with textures write 7 to 20 times faster**, are byte-identical from run to run, and no longer carry
  the author's local paths.
- **Every exported package is loaded in three.js** and checked against its manifest and its USD silhouette. Until
  then nothing in the pipeline had loaded the shipped GLB in any runtime. The first time it was loaded, it had no
  vertex normals.
- **The review inspector shows the GLB that ships.** It is an MCP App, and a reviewer's notes are now stored with
  the asset session where the agent can read them.
- **Materials cover the glTF PBR set**: emission, alpha modes, double-sided, clearcoat, transmission, index of
  refraction, sheen and specular, from the scene description through USD and GLB into the web runtime.
- **Batch sanitize uses the cores it is given**: 52 to 108 meshes per minute at four jobs. A crash in a native
  library now fails one file, not the batch.

What got worse (details under "What got worse"):

- **The offline pipeline, by 0.6 to 0.9 s per asset**, because the three.js check waits on a headless browser.
  Offline throughput on fixtures is a quarter lower. The pipeline's own CPU per asset fell by 20 to 40% at the
  same time. In a live run the step is a second or two beside minutes of model time.
- **LOD generation on dense single meshes**, 9 to 21 times, because the error is now measured: 98 ms to 914 ms
  at 50,000 triangles. The export of that mesh as a whole is still 5 times faster.
- **Authoring requests carry about 6.7k tokens of schema.** On the smallest model the call takes the same 12.5 s
  as before; the difference is that the answer is valid.
- **Computed meshes reach up to 5% less texel density**, because bounding the curvature a chart may flatten means
  more charts and more gutter. Their distortion went from over the budget to well inside it.

What is still open (details under "Not fixed, and not measured"):

- A small computed mesh still pays about a quarter of a second in the UV packer.
- Convex decomposition of a concave mesh still takes about 450 ms.
- Visual QA still runs on the fallback judge at 16 to 22 s per call, because the native Decisions API returns 404
  on the development key.
- No Blender and no game engine ran on the benchmark machine. Those paths run in CI.

## How the comparison was made

Same machine as the first report (4 vCPU Intel Xeon @ 2.10 GHz, 15.7 GB RAM, no GPU, Linux 6.18, Python 3.13.18;
usd-core 26.11, trimesh 5.2.1, xatlas 0.0.11, meshoptimizer 0.2.30a0, coacd 1.0.14, numpy 2.5.5; google-genai
3.6.1, google-antigravity 0.1.24), same harness, numeric libraries and OpenUSD pinned to one thread, medians
throughout.

**The machine drifted, so both sides were measured together.** The pre-fix code, run again three to four hours
after the first report's run, was slower on most benchmarks: 4 to 6% at the median, more than 10% on about a
quarter of the cases, and up to 1.4 times. Comparing the fixed code with the first report's numbers would have
blamed the fixes for the machine. Every offline "before" figure below is therefore the pre-fix tree (`14814fd`,
in a separate checkout with its own harness) measured in the same session as the final tree (`f9e039a`), on the
idle machine: 23 of its 35 benchmarks immediately before the final tree's run and the other 12 immediately after.
The split was not planned. The pre-fix run ended with a segmentation fault as it entered the export-cache
benchmark, in native code that ran without incident in three other runs of the same tree that day. Commits after
`f9e039a` change timeouts, error handling and what the manifest records, not the measured paths.

**Noise was measured, not assumed.** A second full run of the pre-fix code, an hour earlier, shows what the same
code does twice: 86% of the cases agree within 10%, and the tool-level edit benchmarks differ by up to 26%.
`compare.py` takes that run as `--repeat` and reports a change only when it is larger than the same-code
difference for that case, times a 10% floor. One repeat is a lower bound on noise, so the rows that only just
cleared their band were checked against the other runs of the day; where that changes the reading, the text says
so.

**Live figures** come from one bounded run of `perf_live.py` on the fixed code (`acfc094`): 23 counted calls, 36
requests to the API, limits enforced in code, retries off, no prices set. The live "before" figures are the first
report's. Sample counts are 1 to 3, so these are observations, not distributions.

## The defects, their causes, and what changed

### 1. Authoring requests returned nothing usable

*Finding.* One of ten one-shot authoring requests produced a valid scene. Carbon and Argon returned 3 output
tokens after 4,093 thinking tokens. Neon's JSON was cut off. Two of three Hydrogen answers had a string where a
boolean belongs.

*Cause.* On these models thinking counts against `max_output_tokens`: in every cut-off response, answer plus
thinking equalled the cap exactly (4,096). The cap had been sized for the answer alone. A cut-off answer was then
parsed as if it were complete, and re-asked at the same budget. The scene structure was requested in prose, with
no schema.

*Change.* Each tier's cap is sized from measurements as answer plus thinking: 16,384 tokens for the two small
tiers, and the model's maximum of 32,768 for the two large ones. A cut-off or empty response is its own typed
outcome that carries the finish reason and the billed usage, and is never parsed or blindly re-asked. Requests
carry a response schema derived mechanically from the scene model, so prose, schema and parser cannot drift
apart. Thinking levels are unchanged.

| Tier (model, thinking) | Before | After | After: wall, median (range) | After: answer + thinking tokens |
|---|---|---|---|---|
| worker (Hydrogen, low) | 1 of 3 valid, 12.5 s | 3 of 3 valid and passing the geometry checks | 12.5 s (12.2–13.8) | 630–1,553 + 1,066–1,263 |
| bulk (Neon, low) | 0 of 3, cut off, 30 s | 3 of 3 | 36 s (30–39) | 1,747–1,919 + 1,410–2,418 |
| builder (Carbon, medium) | 0 of 3, 3 tokens of answer, 55 s | 3 of 3 | 209 s (203–285) | 9,965–10,286 + 9,289–9,846 |
| director (Argon, high) | 0 of 1, 3 tokens of answer, 112 s | 1 of 1 | 262 s | 17,634 + 7,893 |

The larger models do not write the same scene more slowly. For "wooden crate with iron corner brackets", Hydrogen
authored 9 to 13 parts, Neon 25 or 26, Carbon 123 to 131 and Argon 168. Input per call went from 546 to 7,238
tokens, nearly all of it the schema.

Three things follow from honest budgets and were fixed with them:

- **Candidates are not dropped silently.** A plan's best-of-N used to shrink without a word when N worst-case
  reservations did not fit under its ceiling at once (the hero plan asks for 8 director candidates). Admission
  now waits for budget that settling calls release, and every requested candidate ends with a recorded outcome.
- **A failed repair no longer ends the run.** It is a recorded stop reason, with one retry on the director tier
  where the plan allows it.
- **Timeouts follow the measurements.** They are per tier, at least twice the slowest call measured for that
  tier, and a test holds them to that rule. The old single 300 s was within reach of a healthy Argon call. The
  run in this report produced the slowest calls yet (Carbon 285 s, Argon 262 s), so the limits were raised again
  after it, to 570 s and 540 s. During the run they were 420 s and 480 s, and no call reached them.

One limit cannot be engineered away: Carbon and Argon cap output at 32,768 tokens, and the largest calls seen
used 25,433 and 25,527 of them. A scene that needs more comes back as a typed "cut off" outcome with its usage,
not as a parse error.

### 2. A 1K texture request returned no image

*Cause.* The same budget error. At 1K the image alone is 1,120 tokens, and the call had already used 1,038 of
its 2,048 on thinking and text. Receipts fell back to an estimate because the provider itemises only the image
tokens and the code required the items to add up to the total.

*Change.* The cap is the image's own tokens plus measured headroom. A missing image is a typed outcome. Receipts
are booked from provider usage, with the unitemised part recorded as such.

| Size | Before | After |
|---|---|---|
| 1K | no image after 9.7 s | 1024×1024 in 12.4 s; 1,402 output tokens (1,120 image), 544 thinking; receipt from provider usage |
| 2K | an image, with a receipt from an estimate | 2048×2048 in 20.9 s; 2,001 output tokens (1,680 image), 804 thinking; receipt from provider usage |

### 3. Stage agents re-sent a prompt that was 90% unrelated to the task

*Finding.* A stage agent sent about 7.2k prompt tokens on every model call. About a tenth of that described the
asset task.

*Cause.* Read from the harness's own saved request: the SDK appends a plain-string instruction to its default
coding-assistant prompt (identity, markdown and LaTeX style, how to read transcript files). Subagents were
enabled on every stage, which brings four tools and two prompt sections with them. A file viewer was enabled
that no stage uses. Tool arguments were JSON inside a string, with no schema.

*Change.* Stage agents give the harness their own instructions and enable only what the stage uses. Subagents
stay in the two stages where they do something the stage's own tools do not. Tool arguments are typed from the
same scene schema as the authoring requests, with a strict parse that names the operation, the field and the
expectation. An offline test pins what each stage's request contains.

| Stage | Static characters per model call, before | After |
|---|---|---|
| BLOCKOUT | 26,224 | 6,613 |
| TWEAK | 25,351 | 6,274 |
| VARIANTS | 25,946 | 14,203 |
| LOOKDEV | 26,256 | 11,855 |
| EXPORT | 25,914 | 1,667 |

| Live, BLOCKOUT stage | Before | After |
|---|---|---|
| Crate task on Hydrogen: prompt tokens per turn | 21,739 and 21,878 | 7,738 and 7,657 |
| Crate task on Hydrogen: wall | 9.0 and 6.9 s | 7.8 and 7.7 s |
| Crate task on Hydrogen: outcome | geometry checks pass | the same, in 3 model calls, first edit valid, visual QA approved |
| Repair task (seat a floating lid, add a handle) on Hydrogen | not in the first run | 10,825 prompt tokens over 4 calls, 8.3 s, geometry exact |
| Repair task on Carbon | not in the first run | 15,209 prompt tokens over 5 calls, geometry exact, 341 s (see below) |

The Carbon stage samples of this run met a slow provider. One model call took 195 s to return 25 tokens, and the
crate sample ended with an upstream 502 after 347 s; retries are off by design, so it is recorded as a failed
sample. Earlier the same day the same two tasks, through the same harness, took 22 to 54 s on Carbon in four
runs, with 11.3k to 15.3k prompt tokens per turn against 30.3k and 38.4k before the change. Wall time on this
tier is therefore not something this run can speak to.

*Also found.* The harness did not enforce the list of subagents a stage may start. A stage agent could start a
copy of itself with the stage's write tools, a general research agent, or a subagent it had just defined.
Starting anything but the stage's declared subagents is now refused at the gate.

### 4. UV unwrapping was slow, distorted, and reported the wrong density

*Cause.* Every primitive was handed to xatlas to rediscover charts that the scene description already knows
exactly. On overflow the whole unwrap ran twice. xatlas's fit mode returned atlases larger than requested and not
square (3289×2808 was seen), and UVs were normalised to that, so texels were stretched and the reported density
was wrong. On dense meshes the time was in computing charts, which grows faster than linearly with chart size.

*Change.* Primitives get their exact developable charts from the compiler (box faces, cylinder sides and caps,
one chart per latitude band on round shapes), packed by a deterministic packer. Computed meshes run xatlas once
and repeat only the packing on overflow, and a chart may flatten only a bounded amount of curvature. UVs always
map into a square texture. The stretch warning band that had been added as a stopgap is removed: the budget is a
gate again.

| Subject | Area distortion, before | After | Achieved texel density, before | After |
|---|---|---|---|---|
| crate (12 boxes) | 0.005 | 0 | 820 px/m | 842 |
| barrel (5 cylinders) | 0.018 | 0 | 1,024 | 1,024 |
| dining table | 0.000 | 0 | 950 | 1,024 |
| lamp post | 0.083 (over the 0.05 budget) | 0 | 721 | 746 |
| shelf | 0.006 | 0 | 818 | 918 |
| UV sphere as a computed mesh, 448 triangles | 0.087 | 0.015 | 927 | 888 |
| the same, 1,920 triangles | 0.088 | 0.015 | 895 | 892 |
| the same, 10,080 triangles | 0.064 | 0.014 | 909 | 864 |

Area distortion is the area-weighted mean deviation of a triangle's share of UV area from its share of surface
area; the request was 1,024 px/m into a 2,048-pixel atlas. Angle distortion, measured only after the change, is 0
for the primitives and 0.026 to 0.030 for the computed spheres. One number went the wrong way: the computed
spheres reach up to 5% less texel density, because more charts mean more gutter. They were over the distortion
budget before and are well inside it now.

### 5. LOD levels collapsed silhouettes or were identical

*Cause.* Simplification ran toward a triangle ratio with an unbounded error target, so a welded box could
collapse to four triangles. Levels were written whether or not they differed from the one before. The checker
rendered far levels so small that a collapsed lamp pole covered no pixel and could not fail.

*Change.* Generator and checker share one definition: a level may deviate from LOD0 by one pixel of a 1080-pixel
frame at the size it is shown. The deviation is measured as a two-sided surface distance (the simplifier's own
figure under-reports: 0.015 m where the surfaces were 0.031 m apart), thin parts are protected by a silhouette
test, and a level is emitted only if it removes at least a quarter of the triangles. In an assembly, a part may
be absent at a level when its whole surface is within that level's budget of what remains. The pop check renders
at the scale the budget is defined at, and is a gate again.

| Subject | LOD triangle counts, before | After (and the largest on-screen size each level is used at) |
|---|---|---|
| crate (12 boxes) | 144, 144, 144, 144 | 144; 24 below 2.5% of frame height (2 of 12 parts remain) |
| barrel | 400, 200, 108, 76 | 400; 258 below 2.5% |
| dining table | 60, 60, 60, 60 | 60 |
| lamp post | 264, 132, 66, 40 (failed the pop check) | 264 |
| shelf | 60, 60, 60, 60 | 60 |
| UV sphere, 1,920 triangles | 1,920, 960, 480, 192 | 1,920; 1,166 below 25%; 672 below 10%; 298 below 5% |
| UV sphere, 10,080 triangles | 10,080, 5,040, 2,520, 1,008 | 10,080; 3,448 below 50%; 1,902 below 25%; 690 below 10%; 216 below 5% |
| UV sphere, 49,920 triangles | 49,920, 24,960, 12,480, 4,992 | 49,920, 3,898, 2,354, 716, 314, 224 |

Read the table in two halves. The template props are already close to their minimum (boxes and 12-segment
cylinders): their old far levels were either copies of LOD0 or shapes that visibly popped, and three of the five
now ship LOD0 alone. Where a mesh is denser than a pixel can show, the bound cuts much harder than a ratio did:
the first far level of the 50,000-triangle sphere is 92% smaller instead of 50%.

### 6. Package writing re-encoded every texture for every file

*Cause.* Each texture was decoded and PNG-encoded again for every GLB and for the inspector: 40 encodes for one
package with six maps.

*Change.* A GLB writer that embeds the bytes a texture file already holds, with images de-duplicated by content.
It also writes vertex normals, which the old exporter omitted.

### 7. Batch sanitize did not scale with jobs

*Cause.* The UV and LOD kernels hold the interpreter lock, so threads ran one at a time, and one step ran on the
event loop and was starved.

*Change.* One worker process per job, with per-file timeouts and replacement of a worker that dies. Output files
are byte-identical to the in-process path.

### Found while fixing those

- **Nothing had ever loaded the shipped GLB.** A three.js verifier now loads each package in headless Chromium as
  a pipeline step. It checks loader and shader errors, triangle counts, bounds, textures, materials and LOD files
  against the manifest, and the silhouette against the USD (IoU at least 0.98, the same gate the engine import
  checks use). CI runs it on all four engine targets.
- **The inspector did not show what ships.** It built its own geometry and materials in the page. It now loads a
  GLB built by the package's own writer, and notes that used to stay in the reviewer's browser are saved with the
  asset session and exposed to the agent as a tool.
- **The software rasteriser failed on triangles wider than 256 pixels.** Two callers had worked around it
  independently. Its tile table now follows the frame size.
- **Packages were not reproducible and leaked local paths.** USD carried the working directory in a comment and
  in the embedded scene description, USDZ archives carried the wall-clock time, and the manifest carried facts
  about the run: the export's timings, each pipeline step's duration, and whether the cache was hit. Those stay in
  the run report now. Two runs of one prompt, cold cache or warm, ship byte-identical packages, and a test holds
  the whole pipeline to that.
- **Every edit parsed the scene description twice.** Found while profiling a regression that one of the fixes
  had introduced (a material sync on every edit, also fixed). The transaction is now faster than it was at the
  start.
- **A failed stage session lost its error.** Outside the governed pipeline, the stage-session author dropped
  failed sessions and returned an empty list, which the environment-kit pipeline then indexed. A timeout or a 502
  surfaced as an `IndexError`. Every requested candidate now ends with a recorded outcome, and the first failure
  is raised when none produced a scene.
- **Plugin documents described tools that do not exist**, and a shipped workflow gated on a score its subagent
  could not measure. Thirty-three statements were corrected against the code, and a test now checks that the
  tools, operations and subagents the documents name exist.

## Offline numbers

Medians in milliseconds unless a unit is given. "Before" is the pre-fix tree measured in the same session.

### Edit loop

Scenes are grids of boxes, cylinders and spheres resting on the ground.

| Operation | 5 nodes | 20 nodes | 50 nodes | 200 nodes |
|---|---|---|---|---|
| Transaction only (`apply_patch`) | 0.54 → 0.55 | 0.96 → 0.89 | 1.77 → 1.51 | 6.35 → 4.98 |
| Transaction + mesh extraction + geometry check | 2.9 → 2.7 | 8.1 → 6.7 | 19.5 → 16.2 | 62.5 → 63.8 |
| `apply_scene_ir` tool, accepted edit | 5.7 → 5.4 | 11.4 → 10.8 | 23.1 → 19.8 | 88.3 → 75.9 |
| `apply_scene_ir` tool, rejected edit with rollback | 5.4 → 5.4 | 10.3 → 10.4 | 20.9 → 21.1 | 80.7 → 76.5 |
| `undo_last_edit` tool | 6.6 → 6.5 | 12.1 → 11.7 | 23.7 → 22.5 | 62.4 → 75.7 |
| Edit + check + 4-view preview sheet (256 px) | 42 → 45 | 62 → 71 | 116 → 101 | 342 → 384 |
| `run_visual_qa` tool overhead, fixture judge | 5.7 → 5.7 | 5.8 → 6.1 | 6.0 → 6.4 | 5.7 → 5.9 |
| Rollback of a rejected edit | 0.17 → 0.10 | 0.13 → 0.13 | 0.17 → 0.17 | 0.22 → 0.23 |
| Undo that keeps history | 0.35 → 0.29 | 0.48 → 0.45 | 0.68 → 0.65 | 1.94 → 1.97 |
| First read after a rollback | 0.70 → 0.73 | 1.76 → 1.83 | 3.66 → 4.18 | 14.0 → 16.0 |

Two changes here are real. The transaction is 1.2 to 1.3 times faster at 50 and 200 nodes, because the scene
description is parsed once. The first read after a rollback is about 12% slower at 50 and 200 nodes, in every run
of the day. Everything else at the tool level is inside what two runs of the same code show: for the pre-fix code
alone, the accepted edit at 200 nodes measured 70 ms in one run and 88 ms in the next. The undo row at 200 nodes
looks like a slowdown and is not: run alone, alternating between the trees, undo measured 72 and 77 ms before
and 74 and 64 ms after.

Reaching the tool through the MCP stdio server adds 1.4 to 3.2 ms per call (1.6 to 4.4 before; inside noise).
Settling 10 props takes 7.0 ms, a spatial index over 10,000 boxes builds in 76 ms and answers a small region
query in 1.4 ms, and the geometry check takes 2.0 ms for the crate and 5.5 ms for the barrel: all unchanged.

### Geometry export

| Operation | Subject | Before | After | Change |
|---|---|---|---|---|
| UV unwrap, computed mesh | sphere, 448 triangles | 268 | 256 | none |
| | sphere, 1,920 | 373 | 284 | 1.3x faster |
| | sphere, 10,080 | 1,487 | 530 | 2.8x faster |
| | sphere, 49,920 | 17.3 s | 2.49 s | 6.9x faster |
| | concave U prism, 1,964 | 165 | 80 | 2.1x faster |
| LOD generation, as the pipeline calls it | sphere, 448 | 0.7 | 13.4 | 19x slower |
| | sphere, 1,920 | 2.6 | 53 | 21x slower |
| | sphere, 10,080 | 13.4 | 217 | 16x slower |
| | sphere, 49,920 | 98 | 914 | 9.4x slower |
| | concave U prism, 1,964 | 3.5 | 9.2 | 2.6x slower |
| Collision hulls | convex spheres, 448 to 49,920 | 1.4 to 223 | 1.8 to 240 | none |
| | concave U prism (CoACD) | 436 | 454 | none |
| UV + LOD + colliders | crate, 144 triangles in 12 parts | 231 | 108 | 2.1x faster |
| | barrel, 320 triangles in 4 parts | 275 | 73.5 | 3.7x faster |
| | sphere, 448 | 270 | 278 | none |
| | sphere, 1,920 | 437 | 388 | 1.1x faster, inside noise |
| | sphere, 10,080 | 1,421 | 773 | 1.8x faster |
| | sphere, 49,920 | 17.5 s | 3.41 s | 5.1x faster |
| | concave U prism, 1,964 | 653 | 535 | 1.2x faster |

Inside the crate's 108 ms, unwrapping is 30 ms (223 before) and LOD generation 73 ms (3 before). The balance has
moved from a step that produced distortion to a step that measures what it removes.

### Packaging

| Operation | Crate, before | After | Barrel, before | After |
|---|---|---|---|---|
| Write package, no textures | 73 | 70 | 43 | 49 |
| Write package, six 256 px maps | 917 | 136 | 867 | 95 |
| Write package, six 1024 px maps (one sample) | 13.6 s | 0.70 s | not run | not run |
| Validate a written package from cold | 22.6 | 17.2 | 16.3 | 13.5 |
| Export pipeline through the cache, miss | 269 | 135 | 285 | 92 |
| Export pipeline through the cache, hit | 10.4 | 9.1 | 3.8 | 3.0 |

The package with six 1024 px maps went from 79.7 MB to 69.7 MB. The maps are still embedded in every GLB and in
the USDZ.

### Pipeline, command line and batch

`PropToEngineDAG` in process, fixture images and judges, template geometry, `unreal_5`, cold cache, three repeats.

| Prompt | Best-of-N, wall | Best-of-N, CPU of the pipeline process | Single candidate, wall | Preview gates |
|---|---|---|---|---|
| wooden crate with iron brackets | 2.34 → 3.02 s | 3.21 → 2.46 s | 2.13 → 2.72 s | pass → pass |
| rusty metal barrel | 3.13 → 3.13 s | 4.26 → 3.00 s | 2.85 → 2.91 s | pass → pass |
| oak dining table | 1.45 → 2.38 s | 1.94 → 1.58 s | 1.35 → 2.13 s | pass → pass |
| iron street lamp post | 2.10 → 2.94 s | 2.89 → 2.33 s | 1.76 → 2.57 s | fail (LOD pop) → pass |
| wooden storage shelf | 1.63 → 2.42 s | 2.15 → 1.61 s | 1.54 → 2.12 s | pass → pass |

Wall time rose while the pipeline's own CPU fell. The refinement stage went from 1.56 to 2.26 s at the median:
the three.js check adds about 1.5 s of waiting on a headless browser, whose CPU is in child processes and not in
the CPU column, and the faster unwrap, LOD and package steps give about half of that back.

| Measure | Before | After |
|---|---|---|
| `agy3d prop ... --offline` from a shell (crate) | 4.20 s | 5.18 s |
| `agy3d models` | 0.65 s | 0.64 s |
| Import of `antigravity3d.kernel.usd_stage` in a fresh interpreter | 321 ms | 364 ms |
| 12 offline props, 1 worker process | 26.2 s, 1,647 per hour | 34.7 s, 1,244 per hour |
| 12 offline props, 2 worker processes | 14.8 s, 2,923 per hour | 19.5 s, 2,210 per hour |
| 12 offline props, 4 worker processes | 10.9 s, 3,963 per hour | 15.0 s, 2,876 per hour |
| Of those 12, passing the preview gates | 11 | 12 |
| Batch sanitize, 24 meshes, 1 job | 43.2 s, 33 per minute | 38.6 s, 37 per minute (inside noise) |
| Batch sanitize, 2 jobs | 32.4 s, 44 per minute | 19.9 s, 72 per minute |
| Batch sanitize, 4 jobs | 27.6 s, 52 per minute | 13.3 s, 108 per minute |

Throughput and sanitize are one sample each. In both trees 21 of the 24 sanitize meshes pass and 3 are reported
as over the 15,000-triangle budget. Offline throughput is local compute with fixtures: a live run waits on model
and image APIs, so none of these is a live throughput figure.

### With library-default threading

The tables above pin the numeric libraries and OpenUSD to one thread so that results are comparable between
machines. The edit, rollback, raster, unwrap and export benchmarks were run again on the same commit with each
library's own defaults (`results/offline_default_threads.json`). Of the 44 cases above 1 ms, 41 are within 15% of
the pinned run, and the median ratio is 0.99. The two extremes are both at 200 nodes: an undo that keeps history
is 25% faster with default threads (1.5 ms against 2.0), and the first read after a rollback is 27% slower (20
ms against 16).

## Against the design document's targets

| Design-document target | Before | After | Verdict |
|---|---|---|---|
| Per-edit latency under 90 ms | Edit + check: 5.7 / 11.4 / 23.1 / 88 ms for 5 / 20 / 50 / 200 nodes. With a 4-view preview: 42 / 62 / 116 / 342 ms | 5.4 / 10.8 / 19.8 / 76 ms. With the preview: 45 / 71 / 101 / 384 ms | Unchanged: met to about 20 nodes with the preview, missed at 200 |
| Rollback under 1 ms | Rejected edit 0.13–0.22 ms; undo keeping history 0.35–1.94 ms | 0.10–0.23 ms; 0.29–1.97 ms | Unchanged: met for rejected edits at every size; undo exceeds 1 ms at 200 nodes |
| UV + LOD + collider under 250 ms | Crate 231 ms, barrel 275 ms, 1,920-triangle sphere 437 ms, concave shape 653 ms, 10,080 triangles 1.4 s, 49,920 triangles 17.5 s | 108 ms, 73.5 ms, 388 ms, 535 ms, 0.77 s, 3.4 s | Met with margin for props built from primitives; still missed for computed meshes |
| Visual QA pass about 200 ms | Native Decisions API: 404. Fallback judge: 15–22 s | The same: 404, and 16–22 s | Not measurable natively |
| First-pass engine QA above 99% | No engine was run. Offline, 11 of 12 props passed the preview gates | 12 of 12, and every package is verified in three.js | Not measured in an engine on this machine; CI runs Blender 5.3, Godot 4.7 and Chrome |
| All-in cost per hero asset $0.95–1.60 | No prices configured; hero authoring returned no scene | No prices configured; one director call is 7.2k input and 25.5k output and thinking tokens, and returns a scene | Not measured in dollars |
| 120+ assets per hour on 8 workers | Local compute alone: 1,647 per hour on 1 worker, 3,963 on 4 | 1,244 and 2,876 | Local compute is still not the constraint; live throughput was not measured |

Design-document kernel figures:

| Operation | Design figure | Before | After |
|---|---|---|---|
| Geometry check (Tier-1) | about 15 ms | 2.0 ms (crate), 5.9 ms (barrel) | 2.0 ms, 5.5 ms |
| UV unwrap | about 40 ms | 223 ms inside the crate's export; 268 ms at 448 triangles; 17.3 s at 49,920 | 30 ms inside the crate's export; 256 ms at 448 computed triangles; 2.5 s at 49,920 |
| LOD generation | about 25 ms | 0.7 ms at 448 triangles; 98 ms at 49,920 | 13 ms at 448; 53 ms at 1,920; 914 ms at 49,920 |
| Collision hulls (CoACD) | about 60 ms | Convex shapes skip CoACD: 1.4–42 ms up to 10,080 triangles. Concave, 1,964 triangles: 436 ms | The same: 1.8–38 ms, and 454 ms |
| Clay preview render | about 15 ms | Software rasteriser, 256 px: 14 ms per view at 960 triangles, 27 ms at 5,280, 75 ms at 20,160 | The same: 14, 28 and 79 ms |

The design document's speed-up factors over Blender (15–80x) remain unverified: no Blender binary ran on this
machine.

## What got worse

- **The offline pipeline, by 0.6 to 0.9 s per asset** (the barrel, whose old unwrap and package write were the
  slowest, comes out even). The three.js check takes about 1.5 s when nothing overlaps it (1.46 and 1.60 s in
  two command-line runs), about a third of it starting Playwright and Chromium. Where an engine is installed it
  runs beside the engine import, capture and parity steps; offline there is nothing to overlap with. Two things
  would give most of it back and are not done: starting the browser while the earlier passes run, and keeping one
  browser alive across the assets of a batch.
- **LOD generation on dense single meshes.** Measuring the error costs more than not measuring it: one exact
  distance pass per candidate level on top of the simplifier's own work. Export as a whole is faster for props
  built from primitives and for computed meshes from 10,000 triangles up, and unchanged for computed meshes of
  2,000 triangles and below.
- **Authoring requests are larger**: 7,238 input tokens per call instead of 546. A leaner schema form was
  measured for the stage tools (1,326 tokens per call against 3,946 for the full form) and is used there. The
  authoring request keeps the full form because it makes a mismatch between a part's kind and its parameters
  impossible.
- **Three small ones.** Importing the USD kernel takes 43 ms longer (more model classes are built at import).
  The engine-compliance check went from 0.5 to 0.6–0.8 ms (the material checks). The first read after a rollback
  is about 12% slower at 50 nodes and above.
- **Up to 5% less texel density on computed meshes**, described under defect 4.

## Not fixed, and not measured

Not fixed:

- **The UV packer's fixed cost on computed meshes.** The cost follows the texel area being packed, not the
  triangle count, and a surface that does not fit the atlas at the requested density is packed twice. The
  448-triangle sphere (3.1 m² at 1,024 px/m into 2,048 pixels) takes 255 ms; the same mesh takes 38 ms at
  512 px/m, where it fits the first time. Primitives do not go through this packer.
- **Convex decomposition.** A 1,964-triangle concave mesh takes 450 ms in CoACD, against the design figure of
  60 ms. Its settings were left alone: lowering them trades hull quality for time.
- **Textures are embedded once per GLB file and once in the USDZ.** Writing them is now fast; the bytes on disk
  are still repeated.
- **The environment-kit pipeline authors through stage sessions**, which are not admitted against the spend
  ceiling the way one-shot authoring calls are.

Not measured:

- Blender and the game engines. Nothing was executed on the benchmark machine. CI runs Blender 5.3, Godot 4.7
  and Chrome on the final commit; their speed is not measured here.
- The native Decisions API, which still returns 404 on the development key, and with it the governed live
  pipeline end to end.
- Asset quality as an artist would judge it.
- Wall time of stage runs on Carbon (see defect 3), and how long an answer takes that uses a small tier's whole
  allowance: the allowance has four times headroom over the largest answer seen, the timeout two times over the
  slowest call seen, and no call came near either.
- How Unreal, Unity, Godot and Roblox importers treat a part that is inactive in a far LOD variant, and how they
  show the added material parameters.
- The inspector inside a running Antigravity application. It is tested against the MCP Apps SDK's own host
  bridge in Chromium.
- Dollar cost. No prices are configured.

## Live calls spent on measurement

| Run | Counted calls | Tokens (input / output / thinking) |
|---|---|---|
| First report | 20 | 95,578 / 16,034 / 29,916 |
| Fixing the model calls: probes and verification | 22 text, 4 image | at most 276,398 in total (one call's usage is unknown) |
| Fixing the stage agents: before and after runs | 64 model calls in 16 stage runs | 245,391 / 9,028 / 12,812 |
| This report | 23 (36 requests) | 136,597 / 63,118 / 53,881, plus 12,200 reserved for the failed sample |
