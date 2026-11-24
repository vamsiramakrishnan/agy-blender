# What is verified, and known limits

What each check proves, where it runs, what it does not prove, and the limits we know of. Read this before you rely
on a package in a shipped game. Measured comparisons with stock agents are in [`benchmarks/`](../benchmarks/).

- [What "verified" means](#what-verified-means)
- [Checks every package gets](#checks-every-package-gets)
- [The independent Blender check](#the-independent-blender-check)
- [The FBX file](#the-fbx-file)
- [Engines, one by one](#engines-one-by-one)
- [Known limits](#known-limits)
- [Running the tests](#running-the-tests)

## What "verified" means

A package's `manifest.json` says `verified: true` only when all of these hold (`PyramidResult.verified` in
`tactics/pyramid.py`, and `dags/prop_to_engine.py`):

- the run passed, and both refinement passes converged;
- the look was approved by the calibrated judge (the native Decisions API) on a Blender EEVEE or Cycles render;
- `usd_validate`, `engine_import`, `engine_capture` and `engine_parity` passed, the last judged by the calibrated judge;
- the run was not offline and not a preview.

An Antigravity session's export applies the same conditions (`export_block_reason` and `_verify_engine` in
`harness/tools.py`). Only Godot and three.js have an engine capture adapter, so today only a Godot 4 or a `web_three`
package can be verified. A missing target renderer, a stale verdict, or a simulated judge leaves `verified` false.
A successful verified run makes the package visible under the published path.

- **With no key** the plugin's server judges with a stand-in: a delivery is refused until the user allows an
  unverified preview with a token ceiling and an image limit (`start_preview`).
- **On a key without the calibrated judge** nothing is verified. The only live output is an unverified preview, judged
  by an uncalibrated generative fallback and bounded by your own token and image limits
  ([keys_and_cost.md](keys_and_cost.md#keys-without-the-calibrated-judge)).
- **An offline run** exercises the USD compiler, geometry kernels, material packaging and inspector with
  deterministic image and judge stand-ins. It produces a preview candidate.
- **An edited package** is never verified: no lookdev pass runs on it.
- **Looks** are judged by a model, not a person. In the measured rounds the pipeline's judge was the fallback model
  throughout, so every package there was an unverified preview.

## Checks every package gets

Every exported package is loaded in three.js under headless Chromium as a pipeline step (`web_runtime`). The GLB
and its LOD files must load and match the manifest (triangles, bounds, materials, textures) and the USD's
silhouette. The step is skipped, with the reason, where no browser is installed. For a package built with
`--engine web_three` this check is the required engine check (`engine_import` and `engine_capture`), as the Godot
import is for `godot_4`; for the other targets it is a check beside theirs, not a verification.

- It does not judge the rendered look, and it says nothing about Unreal, Unity, Godot or Roblox importers; those
  have their own steps where an engine is installed.
- It takes about 0.7 s per package once a browser is running in the process, 1.5 s with six 1,024 px maps, and
  several seconds with 2,048 px maps (software GL). The first check of a process also starts the browser.
- For the length of one check the page directory is served from 127.0.0.1 under a random path; every other request
  the page makes is blocked and fails the check.

Gates every package passes besides: Tier-1 structure, the engine's compliance rules, USD validation, UV stretch
within the engine budget and the LOD pop check (thresholds under [Known limits](#known-limits)).

## The independent Blender check

When a real Blender worker is attached (`--blender`, `--pool-socket`, or the plugin's pool sidecar through
`$AGY3D_POOL_SOCKET`), the exported GLB is also checked by a script the project did not write: `qa_check` of
EL4CTEO/blender-skills (pinned commit, vendored by hash).

- The worker runs it on the GLB with each part's coincident vertices merged (parts in contact stay separate parts)
  and the target engine's profile (`roblox` for Roblox, `game` otherwise).
- It is the pipeline step `blender_asset_qa`, recorded under `validation.blender_asset_qa` in `manifest.json`. It
  runs in `agy3d prop` and in an Antigravity session's `export_engine_package`.
- An error from it (a floating or inverted part, z-fighting, missing UVs or materials, a Roblox mesh over 20,000
  triangles) fails the step, so the run is neither `ok` nor `verified`, as a failed engine import is. Its warnings
  are kept in the manifest.
- Without a real worker the step is skipped with the reason and changes nothing: a fake worker's result is never
  counted.
- `agy3d kit --blender PATH` (or `--pool-socket`) likewise runs that repository's `kit_check` on every piece file
  with the kit's grid, pivot and sheet size (node `blender_kit_check`; an error fails the kit's gates).

What it is not: `blender_asset_qa` and `blender_kit_check` are a second opinion from one upstream script each, not a
full QA of the asset. Merged, the scripts agree with this project's own counts on the props surveyed
(`benchmarks/README.md`, "Upstream checks survey"), but two things differ in kind:

- **Hidden back-to-back faces.** Its warning is a proximity test (faces within 1 mm facing each other); the export's
  rule is exact coplanar full coverage. It stays a warning.
- The crate and barrel each still carry one such warning: 16 pairs between the wooden and the iron batch (parts
  whose faces overlap only partly), and the barrel's hoops, which lie on a curved body without coplanar faces.
- **Roblox's 20,000-triangle limit.** Upstream counts a mesh of the GLB, where parts of one material are submitted
  as one batch once the asset has more nodes than `max_draw_calls`.
- So the package writer splits a material's batch at 20,000 triangles (parts kept whole; `glb_batches` and the
  Roblox note count the split meshes), and `validate_package` counts the triangles of every mesh of the shipped GLB.
- The authored-part rule still rejects a single part over the limit, which no split can fix.

Cost: about 0.1 s per file on a warm worker (about 0.4 s for the four pieces of a kit, beside the other nodes). A
run that has no other use for Blender would pay about 0.7 s to start one, so a worker is never started for this step
alone.

## The FBX file

When a real Blender worker is attached, a package for `unreal_5`, `unity_urp` or `roblox_r15` also gets
`<asset>.fbx`, written into the package by the worker with EL4CTEO/blender-skills' `export_asset` script (pinned
commit, vendored by hash; its `unity`, `unreal` and `roblox` presets) from the package's own GLB files. Godot gets
none (it imports the glTF). It is the pipeline step `export_fbx`, after every other step, and the manifest lists the
file with its real hash and an `fbx` record.

What it contains, by what each engine's documentation describes:

| Engine | In the FBX |
|---|---|
| Unity | One mesh per LOD level named `<asset>_LOD0`, `_LOD1`, ... (Unity builds an LOD Group from that suffix). No collision hulls: Unity documents no FBX naming for them. |
| Unreal Engine | The LOD0 mesh `<asset>` and the convex hulls as `UCX_<asset>_<nn>` (Epic's FBX pipeline page names the `UCX_` rule; it documents "Import LODs" but no LOD naming inside one FBX, so the LOD levels stay in the GLB and USD files). Its normal map is the DirectX one the package ships for Unreal. |
| Roblox | One mesh, with its textures embedded (Roblox's export page asks for Copy and Embed Textures): the same bytes as the package's `roblox_surface_appearance` colour and normal maps. |

- For Unity and Unreal the FBX refers to the package's `textures/` files by relative path and copies nothing, so
  the package can be moved with the FBX in it.
- An FBX carries geometry, UVs, LODs where the engine documents them, collision for Unreal, and the texture links
  Blender's FBX exporter writes (base colour and normal). Roughness, metallic, occlusion and emission maps are not
  in it (`fbx.maps.lost` lists them); the GLB and USD stay the full-fidelity files.
- **What was checked:** the written file was re-imported in Blender and compared with the package's GLB files
  (object, material and triangle counts, UV layers, bounding box in metres, image bytes).
- Its header was parsed from the file (unit scale, axes, per-model rotation and scale).
- **It was not opened in Unity, Unreal Engine or Roblox Studio, none of which runs in CI or on the machines this was
  built on.**
- Its hash is not reproducible: Blender writes the time of export into its header and offers no option to fix it
  (`fbx.reproducibility`).
- The FBX is an extra output, not a check. If it cannot be made, the package ships without it and the manifest
  says `fbx.status: failed` with the reason.
- The step then reads `output_failed` (printed as `FAILED OUTPUT export_fbx`), and `verified`, `preview` and `ok`
  are unchanged.
- Without a real worker the step is skipped with the reason and nothing pretends an FBX exists.

## Engines, one by one

| Target | Exported as | Opened by the real engine here | Can be installed into a project | Can be verified |
|---|---|---|---|---|
| Godot 4 | GLB with LOD files and collider GLB, USD | yes, when Godot is installed | yes | yes |
| three.js (`web_three`) | the same GLB files, maps as WebP where the frames show no difference (`web_textures`); the manifest records what a page downloads (`web_download`) | loaded in three.js r160 (vendored) under headless Chromium; `agy3d install` repeats it with the project's own three.js | yes | yes |
| Unreal Engine 5 | GLB, USD, and FBX with a Blender worker | no: never run here or in CI | no | no |
| Unity URP | GLB, USD, and FBX with a Blender worker | no: never run here or in CI | no | no |
| Roblox R15 | GLB, USD, and FBX with a Blender worker | no: no headless verifier | no | no |

In more detail:

| Engine | Checked by | Installed into a project |
|---|---|---|
| Godot 4 | The first target with a rendered verification path: a headless import when `$GODOT_BIN` is set, and `agy3d install`'s seven checks in your own project | Yes, by `agy3d install` or the plugin's `install_into_project` |
| Unreal Engine 5 | An import check adapter that runs when `UnrealEditor-Cmd` is found; never run here or in CI | No: `install` says what to import and writes nothing |
| Unity URP | An import check adapter that runs when the Unity editor is found; never run here or in CI. Unity staging of far LOD files is untested | No, as above |
| Roblox R15 | No headless verifier | No, as above |
| three.js | The headless-browser check (`web_runtime`, above) on every package; for `web_three` it is the required engine check. `agy3d install` runs 11 checks in the project (12 with React Three Fiber) with the project's own three.js, run on 0.152.2, 0.160.0, 0.169.0, 0.170.0 and 0.187.0 | Yes, by `agy3d install` or the plugin's `install_into_project`, into a project with a `three` dependency (React Three Fiber included); a package built for another engine is refused |

Renderers: stock Blender 5.3 supports the warm worker path over Unix IPC and shared memory. Workbench, EEVEE and CPU
Cycles render the regression fixtures. A headless Linux renderer needs a working display or Xvfb. Use
`--pool-socket PATH` to reuse workers across command-line processes.

The plugin: installation, stage-tool registration and hook behaviour have automated tests. Interactive host
discovery and studio first-pass acceptance need their own environments. The inspector's MCP App is tested against
the MCP Apps SDK's own host bridge in Chromium, not inside a running Antigravity app.

## Known limits

### Geometry and contact

- Contact settling is vertical only (no tilting).
- <a id="contact-faces"></a>**Faces two parts press together** (a lid's underside on a box, a strap against a
  side) are left out of the shipped render meshes in every LOD level when another opaque part covers them
  completely: they cannot be seen from outside, and they cost triangles and atlas space.
- The working stage, Tier-1 and the colliders keep the closed parts, and `manifest.json` lists what was removed per
  part (`contact_faces`).
- Each part stays its own USD prim, so a part moved away from where it was authored (a lid opened in the engine)
  shows an opening where it touched another part. Export with `--keep-contact-faces` (the `keep_contact_faces`
  field of the budget, or of `open_asset_session`) to ship the closed parts.
- Roblox requires closed meshes, so nothing is removed for `roblox_r15`.

### UVs and textures

- Trim-sheet mapping distorts smooth, round shapes.
- A mesh that needs computed charts gets them from xatlas, re-flattened and packed here. Chart computation dominates
  on dense meshes (about 2.3 s at 50,000 triangles), and a curved mesh does not always reach the requested texel
  density (a sphere reaches about 890 to 930 px/m of 1,024). Primitives get exact charts.
- The material model covers the glTF 2.0 PBR set (emission, alpha modes, double-sided, clearcoat, transmission,
  index of refraction, sheen, specular).
- Transmission is thin-surface only: there is no volume (thickness, attenuation).
- Only the emissive map and the albedo alpha channel were added as textures; the other new parameters are factors.
- Generated surfaces (the textures painted when no image model is used) size wear, relief and grime by how wide a
  part is across: a rod's diameter, a plate's width. A loft carries one radius per section, so a wide, thin lofted
  plank is treated as narrow. Parts under 3 cm across collect grime only where occlusion is deep.
- In a package only `<asset>.glb` (LOD0) is self-contained. `<asset>_LOD<n>.glb` reference the maps in the
  package's `textures/`, so a package is copied or imported as a directory. A map in a format glTF cannot reference
  (anything but PNG or JPEG) is still embedded in every LOD file.

### Lower-detail versions (LODs)

UV stretch and LOD pop are gates, not warnings. Their thresholds are engineering defaults that no studio acceptance
data has calibrated:

| Gate | Threshold |
|---|---|
| UV stretch | within the engine budget |
| LOD generation | each level within one pixel of LOD0 on a 1080 px frame |
| LOD pop, per level | silhouette IoU at least 0.90, shading difference at most 0.06, edge drift at most 2.5 px, each level lit by the normals it ships |

- The generator bounds geometry only, so nothing guarantees a level passes the shading part of the pop check; on
  the 30 stress subjects measured, all do.
- LOD levels are produced only where the asset gets at least 25% cheaper within that error budget, so some assets
  ship LOD0 only. A part hidden inside another (a barrel hoop's inner faces) is never dropped at distance, because
  the measure is surface distance, not visibility.
- LOD generation measures the deviation of every simplification attempt and costs more than plain simplification
  on dense meshes (about 0.7 s at 50,000 triangles); see `benchmarks/RESULTS.md`. The deviation recorded for a level
  is a bound on it, not the largest distance sampled.
- Each LOD level gets its shading normals from its own geometry (smooth across faces within 40 degrees), so a curved
  mesh can turn faceted at a far level. The pop check sees that; LOD0's normals are not carried through
  simplification.
- In a far LOD level a part can be absent. In USD that part is inactive inside the LOD variant; no importer was run
  here to confirm how each engine treats it.

### Collision

- Collider generation on concave meshes can exceed the 250 ms pipeline target. A solid made of flat faces is
  decomposed as that solid, not as its triangles (about 150 ms for a 2,000-triangle prism); a curved concave solid
  still costs CoACD about 1 ms per triangle (about 2 s for a 1,600-triangle torus), and an open surface 2.5 to 12 s.
- A collider input that is not one closed solid (a hole, a non-manifold edge, duplicate or flipped faces, bodies
  that only touch) is repaired on a private copy before decomposition.
- A decomposition that is not known to be bounded runs in a child process with a deadline of 30 s or more. When it
  expires, or the child crashes, the body gets its convex hull and the manifest's `collider_sources` says why.
- The child is killed by process group, which is POSIX only.

### three.js installs

What `agy3d install` into a three.js project does not do, and what it cannot tell you:

- **Some assets ship PNG.** Compressed textures (KTX2) are written only when a rendered comparison against the
  uncompressed files, made in the headless browser, is inside a fixed tolerance (mean difference 1.5 %, at most 1 %
  of the asset's pixels more than 8 % off, silhouette overlap at least 0.999). Cheaper encodings are tried first;
  when none is inside the tolerance, or `--no-check` or `--compress never` is given, or the compression packages are
  not in the project, the textures stay PNG and the result says why.
- **The GPU memory figures are computed from the texture format, not observed.** This machine renders in software, so
  it has no GPU to read them from.
- **The React component is checked only with the project's own bundler**: esbuild (Vite up to version 7 ships it)
  or Rolldown (Vite 8 ships it instead). With neither, `react_component` is skipped and the component is written
  unchecked. For Rolldown the entry file is written for a moment under the project's `node_modules/.cache`, because
  Rolldown takes no entry from standard input and looks for `react` from the folder of the importing file.
- **Other three.js versions are not checked.** The in-project check was run on 0.152.2, 0.160.0, 0.169.0, 0.170.0
  and 0.187.0. Outside r152 to r187 the files are written and the result says "not checked on this version".
- **Collision hulls are handed over as plain arrays** (`group.userData.agy3d.hulls`, points and triangle indices).
  No physics library is wired; three.js has none, and Rapier or cannon-es takes the points.
- **A first install with compression takes tens of seconds** (the table below: 20 to 103 s with `--compress
  always`, 2 to 8 s with `--compress never`), because every encoding is rendered and compared in a browser.
- **The install itself downloads nothing.** It uses only packages already in the project's `node_modules`; without the
  compression packages it ships PNG and prints the one command that enables compression
  (`npm install -D @gltf-transform/core @gltf-transform/extensions @gltf-transform/functions ktx2-encoder
  meshoptimizer sharp`).

Measured once: three props (round six's scripts, delivered for the web with round seven's surfaces), two projects
(Vite with three.js 0.170.0, Vite with React Three Fiber on three.js 0.169.0), one run each, while other jobs were
using the machine. The installs, the in-project checks and the undo (`agy3d uninstall` left the project's files
byte-identical) passed in all six cells. The goal of half the bytes of today's files was not reached: the crate's
files came to 50.4 %, the chair's to 54.4 % and the hydrant's to 81.6 % of today's. With the shared KTX2
transcoder (584,862 bytes, counted once per project) a page loading one asset gets 77.0 % for the crate, and more
than today's file for the chair (110.6 %) and the hydrant (113.5 %).

The hydrant stayed PNG under `auto`: its cheapest encoding was outside the tolerance (5.62 % of pixels over the
limit against 1 %), the next was outside too (3.01 %), and the one inside saved too little to pay for the
transcoder. The chair is compressed under `auto`, which compares like with like: the compressed install against the
PNG install, every level and the transcoder included (1,149,777 against 1,178,713 bytes).

"Today's files" are the highest level's GLB as a page fetches it from the package (`web_download`). Seconds are for
`--compress always`; sizes in bytes; GPU memory in MB (uncompressed, then block-compressed), computed.

| three.js | Asset | Install, s (`never` / `always`) | PNG install | Compressed install | Of today's | With transcoder | `auto` | GPU MB, computed |
|---|---|---|---|---|---|---|---|---|
| 0.170.0 | crate | 4.8 / 39.2 | 2,398,154 | 1,108,978 (etc1s, albedo and ORM) | 50.4 % | 77.0 % | compressed | 33.6 to 5.6 |
| 0.170.0 | hydrant | 2.3 / 102.4 | 1,998,172 | 1,498,985 (uastc, every map) | 81.6 % | 113.5 % | stayed PNG | 16.8 to 4.2 |
| 0.170.0 | chair | 2.5 / 20.5 | 1,178,713 | 564,915 (etc1s, albedo and ORM) | 54.4 % | 110.6 % | compressed | 8.4 to 1.4 |
| 0.169.0, React Three Fiber | crate | 7.7 / 42.5 | 2,398,154 | 1,108,978 | 50.4 % | 77.0 % | compressed | 33.6 to 5.6 |
| 0.169.0, React Three Fiber | hydrant | 5.9 / 102.7 | 1,998,172 | 1,498,985 | 81.6 % | 113.5 % | stayed PNG | 16.8 to 4.2 |
| 0.169.0, React Three Fiber | chair | 4.4 / 23.7 | 1,178,713 | 564,915 | 54.4 % | 110.6 % | compressed | 8.4 to 1.4 |

Today's bytes: crate 2,199,072, hydrant 1,836,004, chair 1,039,268. Checks: 11 passed, 0 failed, 0 skipped in the
first three rows, 12 in the last three.

### Judging, rendering and editing

- Without the calibrated judge nothing is verified. A preview's judge reports probabilities that are not
  calibrated, and a token ceiling bounds tokens, not money: image tokens cost far more than text tokens, which is
  why the image count is a separate limit.
- The preview renderer is a software rasteriser, not a PBR render.
- Rolling back a rejected 50-node speculative edit takes 0.17 ms at the median on the development machine, and an
  undo that keeps history 0.65 ms (2.0 ms at 200 nodes). The first read after a rollback costs about 4 ms more at
  50 nodes. Sub-millisecond worst-case rollback remains unqualified.

### Where this project is behind a stock agent with Blender

From [`benchmarks/ROUND_SEVEN.md`](../benchmarks/ROUND_SEVEN.md) and
[`benchmarks/ROUND_SIX.md`](../benchmarks/ROUND_SIX.md):

- First builds take minutes, and script mode is not faster or cheaper than the default.
- The wooden chair is about even with a stock agent's chair, not ahead of it (7 of 11 blind verdicts). Its
  generated wood is preferred in every verdict; what loses the others is its shape: turned legs and spindles built
  as few-sided rods read as lumpy. The default mode's chair has not been judged again since round six, when it
  lost every verdict.
- Image-model textures are not wired into script mode.
- The check before a script runs is not a sandbox.

## Running the tests

CI (`.github/workflows/ci.yml`) runs this on Python 3.12 and 3.13:

```bash
python -m pytest -q -m "not live and not blender"
```

Integration markers:

- `live`: real model calls and image generations (a stage-agent session, a judge call, a 2K image). They run only
  when asked for, with `--run-live` or `AGY3D_RUN_LIVE=1`, and need `GEMINI_API_KEY`. A key in the environment alone
  does not run them, so a plain `pytest` never spends quota.
- `blender`: needs a Blender 5.3 or newer binary on `PATH` or in `$BLENDER_BIN`. Set `AGY3D_BLENDER_SOAK=1` to run
  the 300-render isolation test.
- `browser`: needs Playwright and Chromium. The inspector tests also need `AGY3D_THREE_JS` pointing to three.js
  0.160.0; the package verifier uses the copy vendored in the wheel. The qualification workflow
  (`.github/workflows/qualification.yml`) supplies all of it on a Linux software-rendering runner.
