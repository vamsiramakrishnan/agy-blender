# Commands

Every command of `agy3d` and `agy3d-plugin`, with its options as `--help` prints them, what it writes and what its
exit status means. The front page, [README.md](../README.md), shows the four things most people do; this page is
the reference behind it.

- [Install the package](#install-the-package)
- [Options most commands share](#options-most-commands-share)
- [`agy3d prop`: a prop from a sentence](#agy3d-prop-a-prop-from-a-sentence)
- [`agy3d kit`: modular pieces on one trim sheet](#agy3d-kit-modular-pieces-on-one-trim-sheet)
- [`agy3d sanitize`: meshes you already have](#agy3d-sanitize-meshes-you-already-have)
- [`agy3d install` and `agy3d uninstall`: into your game project](#agy3d-install-and-agy3d-uninstall-into-your-game-project)
- [`agy3d check-site`: look at a website you built](#agy3d-check-site-look-at-a-website-you-built)
- [`agy3d edit`: change an asset later](#agy3d-edit-change-an-asset-later)
- [`agy3d variants`: look variants](#agy3d-variants-look-variants)
- [What a run writes](#what-a-run-writes)
- [Running again in the same output folder](#running-again-in-the-same-output-folder)
- [`agy3d models`, `calibrate`, `benchmark`](#agy3d-models-calibrate-benchmark)
- [`agy3d plugin` and `agy3d-plugin`: the Antigravity plugin](#agy3d-plugin-and-agy3d-plugin-the-antigravity-plugin)
- [Replaying and promoting distilled repair rules](#replaying-and-promoting-distilled-repair-rules)

Keys, spend limits and model tiers are in [keys_and_cost.md](keys_and_cost.md).

## Install the package

Python 3.11 or newer is required. From a clone of this repository:

```bash
pip install -e ".[dev]"
```

Add the `web` extra (`pip install -e ".[dev,web]"`) for the three.js package check; it also needs a Chromium or
Chrome binary (`$CHROMIUM_BIN`, or one on `PATH`).

Any command without `--offline` makes live model calls and needs a Gemini API key:

```bash
export GEMINI_API_KEY=...
```

What else a run can use, all optional:

| For | Give it | Without it |
|---|---|---|
| Blender steps (the EEVEE render tier, the independent Blender check, the FBX) | `--blender PATH` (a stock Blender 5.3 executable), `--pool-socket PATH`, or the plugin's pool sidecar through `$AGY3D_POOL_SOCKET` | those steps are skipped with the reason |
| The Godot import check and `agy3d install` into a Godot project | `$GODOT_BIN`, or `godot` on `PATH` (`agy3d install --godot PATH`) | the step is skipped with the reason |
| The three.js check, and `agy3d install` into a three.js project | the `web` extra, Chromium, and the project's own `node_modules/three` (run `npm install` in it first) | the step is skipped with the reason, never counted as a pass |

A headless Linux machine needs a working display or Xvfb for the renderers (for example `xvfb-run -a agy3d ...`).

## Options most commands share

`prop`, `kit`, `sanitize` and `variants` take these. `calibrate`, `benchmark`, `models`, `plugin`, `install`,
`uninstall` and `edit` do not.

| Option | What it does |
|---|---|
| `--engine {unreal_5,unity_urp,godot_4,roblox_r15,web_three}` | the target engine and its rules; default `unreal_5`. `web_three` is a package for a three.js or React Three Fiber project: for it the three.js check in a headless browser is the required engine check, and the manifest records what a page downloads for the highest level (`web_download`) |
| `--max-tris N` | triangle budget (default: the engine budget's default) |
| `--keep-contact-faces` | ship every authored triangle, also the faces parts press together (for parts that will be moved apart in the engine; see [verification_and_limits.md](verification_and_limits.md#contact-faces)) |
| `--max-texture-px {256,512,1024,2048,4096}` | the largest side of the texture atlas (default: the size the parts need at the 1024 px/m target, at most 4096; at most 2048 for `web_three`, where a browser holds every map uncompressed and 4096 has to be asked for). A smaller atlas lowers the texel density instead of being reported as a defect; for `web_three` the delivery states the bytes a page downloads, the texture memory and up to how many pixels on screen the textures stay sharp, so the size can be chosen from those |
| `--surfaces {generated,flat}` | materials without an image: `generated` (the default) paints wear, dirt, grain and markings from the geometry; `flat` ships their flat colours |
| `--out OUT` | output directory (required) |

Exit status:

| Status | Means |
|---|---|
| 0 | the requested mode passed its gates |
| 1 | a failed gate, a hard policy flag or an escalation to a person; for `install` and `uninstall` a failed check; for `benchmark` a failed rollout gate; for `plugin` a bundle with problems |
| 2 | a usage or environment error; for `install` and `uninstall` also a refusal (another engine's package, a file the tool did not write, a file edited since) |
| 3 | only with `--strict` (see `prop`) |

## `agy3d prop`: a prop from a sentence

```bash
agy3d prop "wooden crate with iron brackets" --offline --engine godot_4 --out out/crate
```

That run needs no API key and no Blender, and takes about 15 s. With `--offline` the design is not written from your
words: an offline author picks one of nine built-in templates (crate, barrel, table, lamp post, shelf, wall segment,
floor tile, pillar, doorway) by a keyword in the prompt, the crate when none matches, and judge and images are
stand-ins. It shows the pipeline and the files, not what a model would make.

What the run prints: each pipeline step with its time, the review scores, the cost, the output paths, every skipped
step with its reason, then `publication:` and the gate result. What it writes, under `OUT/runs/<id>/`:

| File | What it is |
|---|---|
| `package/<asset>.glb` | the model at full detail (LOD0), self-contained |
| `package/<asset>_LOD<n>.glb` | lower-detail versions for distance; they reference the maps in `textures/`, so copy a package as a folder |
| `package/<asset>_colliders.glb` | convex collision shapes |
| `package/textures/` | the texture atlas: albedo, normal and ORM (occlusion, roughness, metallic) maps |
| `package/<asset>.usd`, `.usdz` | the OpenUSD stage |
| `package/<asset>.fbx` | Unreal, Unity and Roblox only, and only with a real Blender worker (see [verification_and_limits.md](verification_and_limits.md#the-fbx-file)) |
| `package/manifest.json` | budgets, LODs, colliders, materials, every check's result and where every file came from ([package_provenance.md](package_provenance.md)) |
| `package/inspector.html` | a standalone viewer with PBR channels, LODs and budget gauges; notes are kept in the page and exported as JSON |
| `package/sources/` | the maps the atlas was baked from, so the package can be edited later without its run folder; no engine file uses them and `agy3d install` does not copy them |
| `package/parts/` | the mesh file of each free-form part (a script's tubes, lofts and meshes, or a mesh file you supplied), named by its content, so the package can be opened and edited on another machine; no engine file uses them and `agy3d install` does not copy them. Only a part that is one file travels (`.ply`, `.stl`, `.glb`, `.obj`); a `.gltf` with separate buffers or a USD file is not carried, and the manifest says so (`parts.not_portable`) |
| `report.json` | candidate paths, skipped steps, provider usage and gate results (`outputs.inspector` names the viewer) |

A preview run keeps its own output directory, so a failed retry cannot replace a verified asset. A successful
verified run makes the package visible under the published path.

A live run requires a verified package by default (`--require-verified`). **Only Godot 4 and three.js (`web_three`)
packages can be verified**: no other engine has a render adapter for the engine-capture step, and each of these two
needs its engine on the machine (Godot through `$GODOT_BIN`, three.js through Chromium with the `web` extra).

A run that must be verified, for a target this machine cannot verify, is refused before any model call is made.
Nothing is generated and nothing is paid for. The command exits with status 2 and prints, here for the default
engine (the message is one line; the output folder in it is the one you gave):

```text
error: a verified run for unreal_5 (the default engine, as no --engine was given) cannot succeed on this machine: it has no engine capture, so the capture and parity steps a verified run needs cannot pass. It would end in a refusal after its model calls were made and paid for: nothing was generated. For an UNVERIFIED preview of unreal_5 run: agy3d prop "wooden crate" --engine unreal_5 --preview --token-ceiling N --max-images M --out out/crate; to have it verified, use a target that can be verified on this machine: --engine godot_4, --engine web_three
```

Which targets can be verified depends on what is installed: with Godot (`$GODOT_BIN`) and a Chromium browser,
`godot_4` and `web_three`; with the browser and no Godot, only `web_three`. For a target whose engine is missing the
refusal says so (`it can be verified, but its engine is not installed here (set $GODOT_BIN)`), and when nothing can
be verified here it names what to install. The answer is the same for every key. `--preview` builds an unverified
candidate instead, and `--offline` runs, which are previews, are never refused.

For a verified package you need a key with the calibrated judge, the target's engine (Godot, or Chromium), and
Blender (an executable or the pool sidecar's socket). `--preview` asks for a candidate instead.

```bash
export GODOT_BIN=/opt/godot/godot
agy3d prop "wooden crate with iron brackets" --engine godot_4 --out out/live-crate \
  --blender /opt/blender/blender --require-verified
```

Options of `prop` beyond the shared ones:

| Option | What it does |
|---|---|
| `--offline` | stand-in judge and images and the template author: no network, no API key; implies `--preview` |
| `--style PATH` | a style-anchor image |
| `--art-direction ID` | a named art direction (low-poly, retro-lowres, stylized, realistic, voxel, isometric, anime, hand-painted; see the plugin's `skills/art-direction`); composes with `--style` |
| `--max-rounds N` | the most repair rounds per refinement pass (default 4); improvement rounds have their own count |
| `--improve-rounds N` | improvement rounds after the design passes its checks (default: the spend plan's, 2 for a simple prop, 4 for a standard or hero one; 0 turns them off). A round costs about 40 to 80 s on the worker tier and every two rounds a validation adds about 20 to 30 s (measured live 2026-11-22) |
| `--no-cache`, `--new-design` | see [Running again in the same output folder](#running-again-in-the-same-output-folder) |
| `--blender PATH` | start a warm worker with this Blender executable |
| `--pool-socket PATH` | connect to a running Blender sidecar; reuses workers across processes |
| `--require-verified` | require real Blender, the calibrated judge and engine verification (the default for live runs; Godot 4 and `web_three` only, see above) |
| `--preview` | build a review candidate without requiring publication (the default for `--offline`) |
| `--single-candidate` | one candidate per tournament, for a matched baseline measurement |
| `--strict` | with an uncalibrated judge (a preview), exit 3 when its review is below target; by default it exits 0 and says so, since the review is a finding; a calibrated judge's gates decide either way |
| `--token-ceiling N`, `--max-images M` | spend limits for keys without configured prices ([keys_and_cost.md](keys_and_cost.md#keys-without-the-calibrated-judge)) |

How a prop is designed: the authoring request (and the BLOCKOUT stage prompt of an Antigravity session) asks for the parts a real
object has, the secondary forms that change the silhouette, 50 to 90 parts within the triangle budget, and parts
attached by exact contact, not overlap. A modular-kit piece is authored to its metric and does not get that guidance
(`PROP_CRAFT_GUIDANCE` in `tactics/metered_author.py`, `CRAFT_RULES` in `harness/agents.py`).

## `agy3d kit`: modular pieces on one trim sheet

```bash
agy3d kit "ruined stone temple" --pieces 4 --offline --out out/kit
```

Modular pieces (wall, doorway, floor tile, pillar) that share one trim sheet, assembled into one USD stage.

| Option | What it does |
|---|---|
| `--pieces N` | how many pieces (default 4) |
| `--metric SPEC` | the kit's shared metric (default `interior`): `interior` (0.5 m grid, 2 m modules, 3 m walls) or `exterior` (1 m grid, 4 m modules, 4 m walls), optionally with overrides, e.g. `interior,module=4,wall_height=3.5,pivot=bottom-center` |
| `--splat PATH` | an optional `.ply` or `.spz` Gaussian-splat backdrop (recorded; importing it needs Blender) |
| `--blender PATH`, `--pool-socket PATH` | a Blender worker; it adds the `blender_kit_check` node ([verification_and_limits.md](verification_and_limits.md#the-independent-blender-check)) |
| `--offline`, `--art-direction ID`, `--no-cache`, `--new-design`, `--token-ceiling N`, `--max-images M` | as for `prop`; for a kit, every piece and the trim sheet draw from one ceiling |

## `agy3d sanitize`: meshes you already have

```bash
agy3d sanitize FILES... --engine godot_4 --out out/clean [--jobs N]
```

Batch-repairs GLB, OBJ, PLY, STL or USD meshes: safe repairs, UVs, LODs and colliders, then a validated export.

| Option | What it does |
|---|---|
| `--jobs N` | worker processes (default: the CPU cores this process may use, at most one per file) |
| `--timeout SECONDS` | per-file limit; a file that exceeds it is killed and reported as failed (default 900, 0 = no limit) |

## `agy3d install` and `agy3d uninstall`: into your game project

```bash
agy3d install PACKAGE_DIR --project GAME_DIR [--dest DIR] [--dry-run] [--force]
agy3d uninstall --project GAME_DIR --dest DIR
```

`PACKAGE_DIR` is the package folder (the one that holds `manifest.json`), not the run's `--out` folder.

`install` reads the project's engine and version from its own files and refuses a package built for another engine:
a Godot project takes a `godot_4` package, a three.js project (a `three` dependency in its `package.json`) a
`web_three` one. Given a `godot_4` package for a three.js project the refusal says `the package was built for godot_4
but <project> is a threejs project (...); deliver it for the web: agy3d prop ... --engine web_three (a script asset:
engine="web_three" in its Asset(...) line). Nothing was written.` Build a web package with
`agy3d prop "..." --engine web_three --out out/web` and install `out/web/runs/<id>/package`.

**For a Godot 4 project** it writes, under one directory (default `agy3d/<asset>`):

- the package's level GLBs and `textures/` with explicit import settings: no Godot-generated LODs; maps
  VRAM-compressed with mipmaps, normal maps imported as normal maps; every level binds the one copy of each map;
- one material per glTF material, which Godot extracts to `materials/` and every level shares, so a material edit
  is made once;
- `<asset>.tscn`: the levels switched by distance (from the package's `lod_screen_fractions` and bounds, with
  hysteresis) and one StaticBody3D with the package's convex hulls. Drag that scene into a level.

It then imports an isolated copy of the project with the project's Godot binary, loads the scene and reports each
check (import log, scene, scale, levels, materials, collision, rendered level switching) as passed, failed or skipped
with the reason. A run into a fresh Godot 4.7 project printed:

```text
  check import           passed   headless import and scene load logged no error or warning naming the asset
  check scene            passed   WoodenCrateIron.tscn loaded in Godot 4.7.2-stable (official)
  check scale            passed   LOD0 bounds in the scene equal manifest glb_bounds_m within 0.5 % per axis face
  check levels           passed   2 level(s) with their triangle counts; LOD0->LOD1 at 29.10 m
  check materials        passed   10 map binding(s) on every level, each to the installed textures/ file, ...
  check collision        passed   one StaticBody3D with 8 convex shape(s), each within 256 points ...
  check level_switching  passed   rendered: the expected level alone at every stop, ...
  install_godot  installed  agy3d/WoodenCrateIron  checks: 7 passed, 0 failed, 0 skipped
```

Safety: nothing outside that directory is written; a file the tool did not write is never replaced; an update
refuses when one of its own files was edited since (`--force` overrides); its record is `<dest>/.agy3d/install.json`.
The record names the package by a path relative to the project, or only by its folder's name when a relative path
would cross a home directory; nothing an install writes into your project holds a path of your machine.
Running `install` again with a newer package of the same asset updates the directory in place.

**For a three.js or React Three Fiber project** it writes three kinds of file (the install itself downloads nothing):

- the asset's files where the dev server serves them unchanged: `public/agy3d/<asset>/` (or `static/` for SvelteKit,
  or the `public/` or `static/` folder the project already has; the result names the rule that chose it): the level
  GLBs, the textures, and `collision.json`;
- in the source tree, `src/agy3d/<asset>/`: a loader module `index.js` with its types `index.d.ts`, and with React
  Three Fiber in the project also `react.js` and `react.d.ts`. The install record is `.agy3d/install.json` beside the
  loader, never in the served folder;
- only for a compressed install, the KTX2 transcoder, copied from the project's own three.js into
  `<served>/agy3d/_basis/` and shared by every asset installed there.

What you write is one line, after the install (the function's name is the asset's, in camel case):

```js
import { loadMilitarySupplyCrate } from "./agy3d/military_supply_crate";
scene.add(await loadMilitarySupplyCrate({ renderer }));
```

`renderer` is needed for compressed textures. The result is a `THREE.Group` holding one `THREE.LOD` that follows the
live camera. The promise resolves once the nearest level (with its textures) and the collision data are loaded; each
farther level is fetched the first time the camera comes within 80 % of the distance where it appears, and until it
has arrived the nearest loaded level is drawn. A far level that fails to load leaves the nearer one drawn, with one
console warning naming the file. `group.userData.agy3d` holds `bounds`, `hulls` (the collision hulls as plain arrays
of points and triangle indices, for Rapier or cannon-es; no physics library is wired), `levels` (every level, each
with `loaded`), `loadLevels()` (a promise that every level is loaded, to fetch them ahead) and `dispose()`, which
frees what the loader made, also a level that arrives afterwards. Options: `baseUrl`, `transcoderPath`,
`collision: false` (do not fetch the collision data), `levels: "all"` (fetch every level before resolving; the
promise then rejects if one fails).
With React Three Fiber: `<MilitarySupplyCrate position={[0, 0, 0]} />` in a `<Canvas>`, imported from
`./agy3d/military_supply_crate/react`; it takes the same options as props (`levels="all"` among them).

Checks, run in a headless browser with the project's own three.js on a copy of what was written, each passed, failed
or skipped with the reason: `import`, `loader`, `scene`, `scale`, `levels`, `materials`, `outline`, `collision` (every
hull point against the package's colliders file), `level_switching`, `shared_textures`, `levels_on_demand` (with
default options only the nearest level's file is requested, and a camera beyond the next level's reach requests that
file once and the LOD then draws it, counted from the requests the check's page server answered),
`loader_vs_package` (replaced by `compression` when the files are compressed) and, with React Three Fiber,
`react_component`. The other checks load every level (`levels: "all"`). That is 12 checks, 13 with React Three
Fiber. Without a browser, or without `node_modules/three`, they are skipped, never passed. The result's notes say how
many bytes a page receives for the asset with default options, and how many the far levels add when they are
fetched. A run from before `levels_on_demand` existed printed:

```text
agy3d install: installed military_supply_crate: loader in src/agy3d/military_supply_crate, files in public/agy3d/military_supply_crate
  project: threejs 0.170.0
  check import           passed   5 check(s): no loader/console/page errors; no console warnings; shaders compile and link; ...
  check loader           passed   military_supply_crate: a Group with one THREE.LOD of 4 level(s) in three r170
  check collision        passed   8 hull(s) as plain arrays: every point within 1e-5 m of the package's colliders file, and the same triangle indices
  ...
  install_threejs  installed  src/agy3d/military_supply_crate  checks: 11 passed, 0 failed, 0 skipped
```

**Compression.** KTX2 textures and meshopt geometry are written only with six packages already in the project's
`node_modules`: `@gltf-transform/core`, `@gltf-transform/extensions`, `@gltf-transform/functions`, `ktx2-encoder`,
`meshoptimizer` and `sharp`. Without them the install ships the package's own web files (WebP textures where the
package has them, PNG otherwise) and prints the one command that enables compression: `npm install -D
@gltf-transform/core @gltf-transform/extensions @gltf-transform/functions ktx2-encoder meshoptimizer sharp` (the
install itself never runs it). Nothing compressed is written without a measurement: each encoding is rendered in the
headless browser against the package's own frame, and the first one inside the tolerance is written, so some assets
ship uncompressed (see [the web limits](verification_and_limits.md#three-js-installs)). With `--compress auto` an
encoding that is already larger than the files it would replace ends the search, and its frames are not compared:
the encodings after it are larger. That is the usual outcome for a package with WebP textures, which download
smaller than KTX2; `--compress always` trades the download for graphics memory. The texture maps are encoded side by
side, up to four at a time; `AGY3D_KTX2_WORKERS=1` encodes one at a time and needs about half the memory (three
2048 px maps: 810 MiB side by side, 424 MiB one by one). A native `gltfpack` or `ktx` binary is not used.

**three.js versions.** The checks were run on three.js 0.152.2, 0.160.0, 0.169.0, 0.170.0 and 0.187.0. Between r152
and r187 the files are written and checked; outside that range they are written and the result says `not checked on
this version`, and the loader may need changes.

**Refused**, with the reason in the message and nothing written: a package built for another engine (above); a
`--assets` folder that is absolute, outside the project, hidden, `node_modules`, not inside a served folder, or the
served folder itself; `--dest` and `--assets` that contain one another or an install of another asset; a loader
folder that exists with files this tool did not write; a project where the served folder cannot be told (no `vite`,
`next`, `react-scripts`, `astro` or `@sveltejs/kit` dependency and no `public/` or `static/` folder: pass `--assets`).

**For Unity, Unreal Engine and Roblox projects** it says what to import and writes nothing.

| Option | What it does |
|---|---|
| `--project DIR` | the game project's root directory (required) |
| `--dest DIR` | directory inside the project (Godot: default `agy3d/<asset>`; three.js: where the loader and the install record go, default `src/agy3d/<asset>`) |
| `--assets DIR` | three.js only: the folder for the asset's files, inside the project's served folder (default `<served>/agy3d/<asset>`) |
| `--compress {auto,always,never}` | three.js only, and only with the compression packages in the project. `auto` (the default) writes compressed files only when the page then downloads no more than before (the transcoder is counted unless the project already has it); `always` compresses for the GPU memory whatever the size; `never` installs the package's own files as they are |
| `--dry-run` | print what would be written; write nothing |
| `--force` | replace this tool's own files even where they were edited since |
| `--no-check` | do not run the in-project check (Godot: no import and load of the installed scene; three.js: no browser check, and so no compression, which is only written after a measured frame comparison) |
| `--camera-fov DEG` | vertical field of view the level switch distances are computed for (default 60, the levels' own; Godot's Camera3D default is 75, where they switch later than needed, never sooner) |
| `--godot PATH` | Godot binary for the check (default `$GODOT_BIN`, then `PATH`) |
| `--json` | print the machine-readable result |

`uninstall` removes exactly what `install` wrote, in a Godot or a three.js project (for three.js, `--dest` is the
loader folder, `src/agy3d/<asset>`; the record there lists the files, and the shared transcoder folder is kept while
another install needs it). It keeps any file you added there and refuses when one of its own files was edited since,
unless you pass `--force`. It takes `--project`, `--dest` (both required), `--dry-run`,
`--force` (remove this tool's files even where they were edited since) and `--json`.

## `agy3d check-site`: look at a website you built

```bash
agy3d check-site examples/straitjacket-film/site
agy3d check-site tests/fixtures/site_check/facts --sizes desktop --steps 3 --no-variants
```

It opens a built site (a folder holding `index.html`, or a running dev server on localhost) the way a visitor does and
answers in plain text, at most about 60 lines, in this order: what is broken, what a visitor would notice, the
measures, where the pictures are. The agent gets the same answer from the `check_site` tool. It needs what the three.js
check needs: the `web` extra and a Chromium.

What it does:

1. Serves the folder on 127.0.0.1 under a sub-path (`/site-<random>/`). A site that asks for `/assets/app.js` at the
   server root fails here as it fails on a host that publishes it under `/project/`; the answer says so and how to fix
   it (relative paths; for Vite, `base: './'`).
2. Opens it at 1440 x 900 and at 390 x 844 (a phone, with touch), both at a device pixel ratio of 1, and waits for
   the load event, a quiet network and a still picture.
3. Scrolls to `--steps` even positions (default 9, from 0 to 100 %), coarse to fine (0, 100, 50, 25, 75 % ...), so a
   check cut short still covers the whole scroll. At each it waits until the picture is still (two screenshots, taken at least two of the page's own frames apart,
   that agree; at most 4 s),
   keeps the frame, and takes the WebGL canvas alone. The frames go into one labelled contact sheet per size,
   `sheet_desktop.jpg` and `sheet_phone.jpg`; labels in red mark a finding.
4. Loads the page once with "reduce motion" set and once with WebGL taken away, and says what a visitor then gets.
5. Checks every element that names its source (below), and lists numbers and code shown without one.
6. At each step, when the page says where its subjects are (below), measures how much of each is cut by the frame's
   edge and how much lies under the page's HTML, and names those a visitor would notice.

Where the subjects are. A page may expose `window.__site.info()`, whose result holds `onScreen`: one entry per subject
on screen, keyed by its name, each `{ share, x, y, box: [x0, y0, x1, y1], kind }`. `box` is the subject's projected box
as fractions of the window from its top left, not clamped (below 0 or above 1 it runs out of the frame); `kind` is
`"prop"`, `"layer"` (an instanced layer or a group of generated geometry a shot is about; measured and reported exactly
as a prop, and called "the layer") or `"label"` (a 3D label; absent means prop). A page may also say which props the
current shot is about, with `subject: true` or `false` on each: where any prop is marked so, the others are the shot's
background, measured and kept in the record but not named in the answer. Labels always count for being cut or hidden.
The same flags let the check say three things about the shot itself, and only where the page gives them (a page that
flags nothing gets none of them, rather than a guess): whether the shot's subject keeps the same place and size over
a stretch of the scroll ("the picture does not travel"), whether it is the same size on screen at most steps ("one
scale throughout", a measure), and which labels are too small to read at a step where the shot is about them. A label
counts as the shot's where it is flagged `subject: true`, or at a step that flags nothing on a page that flags other
steps: the starter's establishing shot, which names no subject because the whole scene is its subject. The skill's starter project writes all of it. A page without `box` (only
`share`, `x`, `y`, as pages built before it have) or without the hook gets nothing measured and nothing said about its
subjects. The hook is the page's code and is not trusted: at most 64 subjects are read, names are cut to 80
characters, every number is checked, and a hook that throws or does not answer within 0.5 s leaves the step without
the measure. For each subject with a box, at the moment the step's frame is taken:

- cut: the share of the box outside the window, and by which edges;
- hidden: the share of the box's part in the window that lies under an element a visitor cannot see through. That is
  an element drawn above the 3D canvas at that point (the browser's own paint order, read with `elementsFromPoint` at
  up to 16 x 16 points of the box while `pointer-events` is forced on, so a panel with `pointer-events: none` still
  counts) whose background alpha times its own and its ancestors' opacity is at least 0.6, or that has a backdrop blur.
  Text, borders and images without a background do not count: the scene shows around them. The element is named as
  the facts are (`section#digest > div.panel`).

The answer gives one line per subject and size, with the steps, the worst share, what covers it or which edges cut it,
and the frame to look at; at most 8 subjects per size, props and layers first, then one line naming the rest. A
subject that does not travel gets one line per size, naming the subject, the steps, the share of the scroll and what to
do. Every subject at every step is in `report.json`. The contact sheet marks such steps `subject hidden`, `subject cut`,
`subject held` or `label small`.

Stills for the page without WebGL. When the page also gives `window.__site.chapters()`, a list of `{ id, rest }` (the
chapter's id, and the story time at which it rests), and `window.__site.setProgress(T)`, as the skill's starter does,
the desktop visit ends by drawing each chapter's rest and writing the 3D canvas alone as `stills/<chapter id>.webp`
in the output folder: read from the canvas's drawing buffer right after the page drew it, so no HTML and no scrollbar
is in it; at most 1280 px wide (the article's text column is 40rem, about 590 CSS px of image,
so 1280 covers a screen of two device pixels per CSS pixel), WebP at quality 80: 41 to 65 kB each on run 2's site
(quality 70 saves a fifth for 1 dB less, 90 costs 60 % more for 1.5 dB). At most 24
chapters and 25 s in all; ids become file names of letters, digits, `-` and `_`, made distinct. While it draws them,
from before the first until after the last (also when one fails), `<html>` carries the attribute `data-agy3d-still`
(empty), so a page may frame the shot's subject in the middle of the whole canvas instead of beside its text panel; a
page that ignores it gets the stills it would get anyway. The answer names the
folder under "Pictures"; `report.json` lists every still (`stills`) and why any was not made. A page without the hook
gets no `stills` folder and nothing said.

How each finding is decided (the only thresholds; everything else is reported as a number):

| Finding | Rule | Why |
|---|---|---|
| the 3D canvas shows nothing | under 1 % of the canvas (at 1/8 size) differs from its most common colour by more than 8 % | at 1/8 size the 1 px lines of a helper grid average away; an object does not. 1 % of a desktop canvas is a 120 x 108 px patch |
| the 3D canvas shows little | the canvas shows something on under a quarter of the share it shows at the page's median step; the answer adds how much of that is one solid shape (the largest region after an opening of two pixels at 1/4 size) | the scene may have left the frame between chapters (run 1 at 62 %: a grid and one arc, 4 % against 19 %, one shape 53 to 63 % of it), or a small subject is framed on purpose (the skill's starter, its wide closing shot: 3 to 5 %, one shape 90 to 98 %). The shape share is a hint, not a rule: the film's opening, many small things composed on purpose, scores 26 %. The frames tell which |
| the picture did not change | mean difference to the step before under 0.2 % (1/4-size grey frame) | the scroll moved and nothing on screen did |
| sideways scroll on a phone | the page is wider than the viewport by more than 1 px | a visitor's thumb finds it |
| a 3D label cut or hidden | 10 % of its box outside the window, or 10 % of its part in the window under opaque HTML | a label 15 % cut loses the ends or feet of its letters and no longer reads (run 2: a floor label 21 % below the bottom edge and one 56 % under the text panel, both called out by the reviewer) |
| a 3D label too small to read | its box is never taller than 16 px at any step where it is in the frame | a label that is small at some steps and readable at others is in the background there; one that is never larger cannot be read anywhere (run 2: a floor label in the last wide shot, a 15 px box and letters of a few pixels) |
| a 3D label too small where the shot is about it | under 16 px tall at a step where it is in the frame, not cut or hidden by 10 %, and the shot's (flagged `subject: true`, or the step flags nothing on a page that flags others) | run 3's closing wide shot of its three props showed their labels at 11 to 15 px on the desktop and 5 to 7 px on the phone, readable at every other step, so the rule above said nothing. A label of a prop the shot is not about stays its background |
| the picture does not travel | the shot's subject (flagged `subject: true`) keeps its box, every edge within 2 % of the window of where it was at the run's first step, over at least 3 neighbouring steps that cover at least 25 % of the scroll | run 3's phone layout fitted the gate into one place above the panel at steps 1 to 5 of 9: within 0.7 % of the window while the page scrolled half its length; small changes around it kept the frames from being "unchanged". On the desktop, not pinned, the subject moved 3.8 to 19 % of the window between neighbouring steps. Two steps are not enough (a page may rest at its top or bottom for a step), and a quarter of the scroll is more than one of run 3's six chapters, so a run inside one chapter's rest at 25 steps is not named |
| one scale throughout (a measure) | at least 4 steps flag a subject, and at least three quarters of them have the subject (the box around what is flagged, clamped to the window, the larger of its share of the height and the width) within a factor 1.25 of the median | shot sizes (wide, medium, close) differ by about a factor two in how much of the frame the subject fills. Run 3's desktop: 49 to 64 % of the window at all 8 such steps; a wide shot at 19 % is outside the band |
| a prop cut | 30 % of its box outside the window | a close shot often runs a prop a little out of frame on purpose: run 2's props at 11 to 22 % out read as framed close, the clamp at 42 % and 62 % out as cut off |
| a prop hidden | 15 % of its part in the window under opaque HTML | the box is the projection of the prop's 3D bounding box, whose corners stand off the silhouette: 12 % under run 2's top bar was only the empty corner above the gate, 20 % under the digest chapter's panel covered the gate's whole left column |
| (neither) | less than a fifth of the box in the window | the subject is entering or leaving the shot (run 2: the vault as a sliver at the right edge, 84 to 93 % out) |
| the download is heavy | the bytes take more than 5 s on a 10 Mbit/s connection | the first impression is over by then |
| an unsourced number | three or more digits, or a unit (%, ms, MB, tokens, ...); a bare year is not counted | small counts are prose; measurements are claims |

What the scene costs is read from the page's WebGL calls, which are wrapped before the page's scripts run
(`src/antigravity3d/deliver/site_probe.js`), so it works on a page that does not expose its renderer:

- draw calls per frame: calls of `drawArrays`, `drawElements`, `drawRangeElements`, their instanced forms and the
  `WEBGL_multi_draw` calls (one call counts once);
- triangles per frame: `count / 3` for `TRIANGLES`, `count - 2` for strips and fans, times the instance count (the
  rule three.js's `renderer.info` uses); lines and points are not counted;
- texture memory: the bytes of every texture level the page allocates or uploads, from its format, plus a third of
  level 0 for mipmaps, minus deleted textures; textures allocated without data (render targets) and render buffers are
  given apart.

The largest per-frame counts over the steps are reported. The tests hold the counts exactly on a raw WebGL page whose
counts are known, and equal to `renderer.info` on a three.js scene with meshes, an InstancedMesh and lines. A renderer
in a worker (OffscreenCanvas) or on WebGPU is not seen; the answer says when a page used one.

On software WebGL a heavy page draws a frame in about a second. GSAP treats any gap over 500 ms as 33 ms, so a
scrubbed animation would barely move; the page is therefore shown at most 250 ms passing between two of its frames
(`performance.now`, `Date.now` and the animation-frame time move together). A page that draws faster sees the real
clock. Frames still changing after 4 s are kept and named in the measures.

The check tests its camera before it believes it. A Chromium that puts pages together without the GPU path (a
machine without a GPU whose browser was started without `--use-angle=swiftshader`, or with `--disable-gpu`; some
container images wrap the browser in a script that removes such switches) draws a WebGL canvas wrongly in a screenshot
wherever an opaque box lies over it: the part opposite the box, mirrored top to bottom, is left black. Run 2's 48 px
top bar left a 48 px black band at the bottom of every frame; in run 3 the text panel's code block left a black box
over the 3D label on the phone, and the agent spent 16 of its 105 minutes on it and changed the site's styles twice.
No visitor's browser does this. So before the visits the check photographs a page of its own (a canvas of one colour
under an opaque bar), once per browser:

- the canvas shows opposite the bar: nothing is done and nothing is said;
- it does not: the visits give a WebGL canvas a layer of its own (a filter that moves no pixel, `blur(0px)`, in a
  rule that is first in the head and without specificity, so any rule of the page's own wins), which was measured to
  give the canvas back: on run 3's site the frames taken this way differ from those of the same browser on its GPU
  path in at most 0.06 % of their pixels, against 1.7 to 8.2 % before. The answer says so under `about these
  pictures`, and that a screenshot script of the agent's own on that machine will show the boxes. A filter makes its
  element a stacking context, so the layer is given canvas by canvas: always to a positioned canvas (fixed, absolute,
  relative, sticky), where it changes nothing; to a canvas in the normal flow only where what is painted over it
  stays over it (read at a grid of points before every picture). A canvas that would have been painted over a
  header written before it is left as it is, and the answer counts it;
- not even then: the answer says that the frames have the fault and that the site is not to be changed for it.

`report.json` carries the result as `camera` (`true`, `layer`, `faulty`, or empty when the machine has no WebGL). The
canvas-only frames and the stills are read from the drawing buffer and never had the fault; subjects are measured from
the page's geometry, not from the frames.

Facts. Mark an element with the file that holds what it shows; paths are relative to `--sources` (default: the site
folder):

| `data-src=` | the element must show |
|---|---|
| `"eval/run.json#/runs/0/tokens"` | the value at that JSON pointer. A number is compared as a number at the precision shown: for 302628, `302,628` and `302.6k` match; for 0.37, `37 %` does. A string: all of it, or one unbroken passage of it of at least 20 characters (half of it, for a shorter value), whitespace aside; nothing left out in the middle |
| `"tests/fixtures/digest.txt#L10-L14"` (or `#L10`) | those lines (indentation they share aside); one shown line may be part of them |
| `"README.md"` | text that occurs in the file, whitespace aside |

From `tests/fixtures/site_check/facts/index.html` (its sources are in `sources/` beside it):

```html
<p>Tokens in the first run: <span id="ok-num" data-src="sources/data.json#/runs/0/tokens">1,234</span>.</p>
<pre id="ok-code" data-src="sources/code.py#L2-L3">def add(a, b):
    return a + b</pre>
<blockquote id="ok-quote" data-src="sources/notes.md">Capture once, retrieve the line that matters.</blockquote>
```

Requests to other hosts are listed and not made by default (`--other-hosts try` makes them), so the page is seen as it
is when those hosts cannot be reached, and no answer depends on this machine's network. The check answers within
150 s, with what it has when time runs out, and says which steps are missing. It writes only its pictures and
`report.json` (the full record), by default into `./agy3d_site_check/<site>/`, never into the site folder.

| Option | Meaning |
|---|---|
| `--sources DIR` | folder that `data-src` paths are relative to (default: the site folder) |
| `--steps N` | scroll positions photographed (default 9, 2 to 25) |
| `--sizes LIST` | `desktop`, `phone` or both, comma-separated (default both) |
| `--other-hosts block\|try` | list requests to other hosts without making them (default), or make them |
| `--no-variants` | skip the visits with reduce motion set and without WebGL |
| `--out DIR` | where the frames go |
| `--json` | print the machine-readable record instead of the text |

Exit status: 0 when nothing is broken, 1 when something is, 2 for a usage error (a remote URL, a folder without
`index.html`).

## `agy3d edit`: change an asset later

```bash
agy3d edit OUT "make the two side handles 30 % larger" --token-ceiling N   # one patch, the rest kept
agy3d edit OUT --patch ops.json                                          # a patch you wrote; no model call
agy3d edit OUT --undo                                                    # put the last edit back
```

`OUT` is a prop's `--out` folder or a package folder. `edit` opens the asset as it shipped (the design embedded in
its package) and applies the instruction as one patch to the parts it names.

- **How it is judged.** The author is shown the five badged views of the design and the instruction. The edit is
  kept only when the judge sees it carried out and nothing else changed, no part ends up floating and no new defect
  appears; otherwise it is asked for once more and then left undone with the reason.
- **What is kept.** Every other part keeps its geometry exactly and every material its source maps; no image is
  generated.
- **Its place in the texture.** A part the edit does not change keeps the same UVs and the same texels under them
  while the atlas keeps its size. When the atlas has to grow, its UVs are rescaled and its texels stay where they
  were. That holds for image maps and flat colours.
- **When the atlas must be laid out again** (the change cannot fit in the space the other parts leave free): the
  manifest's edit record says `repacked` and why.
- **Then each untouched part's base colour is checked** against the shipped one: mean within 4/255, spread within
  8/255; a part outside it is named in the result. A surface generated from the geometry is held to that bound too,
  since a changed neighbour may change it.
- **Months later.** A package keeps the maps its atlas was baked from in `sources/` and the mesh files of its
  free-form parts in `parts/`, so a bare package can be edited without its run folder, on another machine too. A
  package written before parts travelled with it is refused with the way out (build it again from
  `source/build.py`, or export it again from the workspace that made it). An edit reads no file outside the
  package: a part must be one file that names no other, its bytes must match the hash in its name, and the
  package's USD must be the flat single file this project writes.
- **Undo.** The replaced package is kept in `OUT/history/NNN/` (beside a bare package, `<name>.history/`), and
  `--undo` puts it back. The new manifest lists the edit under `edits` and its provenance names it.
- **Not verified.** An edited package is never verified: no lookdev pass runs on it.

A patch file is the scene format's patch, `{"ops": [...]}`, for example
`{"ops": [{"op": "set_param", "node_id": "bracket_0", "name": "size", "value": [0.14, 0.14, 0.03]}]}`.

| Option | What it does |
|---|---|
| `--patch OPS.json` | apply this patch instead of asking a model; no model call |
| `--undo` | put back the package the newest edit replaced |
| `--role ROLE` | authoring tier for the edit (default: the run's improvement tier, else `worker`) |
| `--offline` | stand-ins only (needs `--patch`) |
| `--no-cache` | do not read or write the run folder's cache |
| `--json` | print the machine-readable result first |
| `--strict` | as for `prop` |
| `--blender PATH`, `--pool-socket PATH` | a Blender worker |
| `--token-ceiling N`, `--max-images M` | spend limits, as for `prop`. A live edit needs its own `--token-ceiling` (it does not inherit the asset's); `--max-images` defaults to 0 |

After an edit, run `agy3d install` again with the new package to update your project.

## `agy3d variants`: look variants

```bash
agy3d variants ASSET.usda... --offline --out out/looks
```

Look variants (pristine, muddy, snow_dusted, battle_scarred) as a USD variant set of material overrides. Takes the
shared options and `--offline`.

## What a run writes

Every path below was read from the code and seen in a run. `OUT` is the `--out` folder.

| Command | Path | What it is |
|---|---|---|
| `prop` | `OUT/runs/<id>/package/` | the package (see the table under `agy3d prop`); this is the folder `agy3d install` takes |
| `prop` | `OUT/runs/<id>/report.json`, `OUT/report.json` | the run's report: candidate paths, skipped steps, usage, gate results |
| `prop` | `OUT/runs/<id>/<asset>_authoring.usda`, `web_three.png` | the working stage and the three.js check's render |
| `prop`, `kit` | `OUT/.agy3d-cache/` | generated images and recorded designs (`--no-cache` turns it off) |
| `prop` | `OUT/ledgers/<key>/spend.sqlite3` (and `route.sqlite3`) | the asset's spend ledger; the key covers prompt, budget, style, judge and candidate mode |
| `kit` | `OUT/kit.usda`, `OUT/pieces/`, `OUT/src/`, `OUT/trim_sheet.png`, `OUT/trim_sheet.json`, `OUT/snap_test.usda`, `OUT/report.json` | the assembled kit, its pieces, the shared trim sheet and the report |
| `kit` | `OUT/spend.sqlite3`, or `OUT/spend_tokens.sqlite3` with `--token-ceiling` | the one ledger the whole kit draws from |
| `sanitize` | `OUT/<file>_<ext>/<asset>.usda`, `.glb`, `_LOD<n>.glb`, `_colliders.glb`; `OUT/report.json` | one folder per input file |
| `variants` | `OUT/<nn>_<asset>/<asset>_looks.usda`, one `.usda` per look, `textures/`, `spend.sqlite3`; `OUT/report.json` | one folder per input stage |
| `edit` | `OUT/runs/edit-<id>/attempt-<n>/package/` | the edited package |
| `edit` | `OUT/history/NNN/` (beside a bare package, `<name>.history/NNN/`) | the package each edit replaced; `--undo` restores it and keeps the record as `history/undone-NNN-<time>` |
| `edit` | `OUT/history/ledger/spend_tokens.sqlite3` (`spend.sqlite3` without a token ceiling) | the edits' own ledger |
| `install` (Godot) | `<project>/agy3d/<asset>/` and its `.agy3d/install.json`, `.agy3d/check/` | the installed files, the install record and the check's report and screenshot |
| `install` (three.js) | `<project>/public/agy3d/<asset>/`, `<project>/src/agy3d/<asset>/` and its `.agy3d/install.json`, `.agy3d/check/` | the served files, the loader, the install record and the check's report and screenshots |

## Running again in the same output folder

`prop` and `kit` keep a cache folder, `OUT/.agy3d-cache`. It holds every generated image, keyed by its request, and
every answered authoring request (each design and each repair), keyed by the whole request: model, thinking level,
output cap, rules text, budget, art direction, references (by the bytes uploaded) and the task with its brief, plus
the candidate's number.

A run that ends early, whether stopped, refused by a ceiling or ended by a service error, keeps what it paid for.
Running the same command again with the same `--out` continues from the recorded design:

- no authoring call is made, and its images come from the cache;
- the run summary says `design record: N design(s) ... reused, no call made (originally ...)`;
- the ledger gets a receipt with `"source": "record"` that books nothing and names what the original call cost.

Any change to the request misses: a different brief, budget, tier, art direction, or a new version of the rules text
or schema.

- `--new-design` asks the author for a new design instead and keeps every cached image whose request is unchanged.
  The new design replaces the recorded one, so the next rerun continues from it.
- `--no-cache` uses no cache folder: nothing is read or written there.
- The cache folder holds your brief's designs (the scenes authored for it, named after it) as well as the images.
  Delete the folder to remove them; the run's `report.json` beside it also holds the brief.
- On a rerun with less left in the ledger, the router may plan fewer candidates (Best-of-N). Their requests then
  say "design 1/3" instead of "1/4", so those designs are asked for again.
- An albedo or other image call answered without an image (finish `MAX_TOKENS`, `STOP` or `NO_IMAGE`, with no image
  part) is asked again once. Both attempts are booked as billed; the one without an image counts no image.
- A refusal (a safety or prohibited-content stop, a blocked prompt) is never asked again.
- If the second answer has no image either, the material is left untextured and both attempts are named.

How a rerun counts against the spend ledger is in
[keys_and_cost.md](keys_and_cost.md#the-spend-ledger-across-reruns).

## `agy3d models`, `calibrate`, `benchmark`

```bash
agy3d models                                         # print the model tier table, after overrides
agy3d calibrate LABELS.json --out studio_policy.yaml [--offline]   # fit studio thresholds from labelled history
agy3d benchmark cohort.json                          # spend per accepted asset from paired runs
```

The tier table and the cohort format are in [keys_and_cost.md](keys_and_cost.md).

## `agy3d plugin` and `agy3d-plugin`: the Antigravity plugin

```bash
agy3d plugin install [--target DIR]     # copies the bundle to the customization root (default ./.agents) and validates it
agy3d plugin validate [PATH]            # validates the bundled bundle, or the directory given
```

`agy3d-plugin` is the same installer as `python -m antigravity3d.plugin_install`, with more options
(`agy3d-plugin install --authoring script` is read as `agy3d-plugin --authoring script`):

| Option | What it does |
|---|---|
| `--target DIR` | customization root (default `./.agents`; the plugin lands in `<target>/plugins/antigravity-3d`). Use `~/.gemini/config` for a global install |
| `--symlink` | link the repository's copy instead of copying it (edits take effect at once) |
| `--workspace DIR`, `--depot DIR` | write absolute workspace and depot paths into the sidecars' permissions (copy installs only) |
| `--authoring {ir,script}` | `ir` (the default): the agent drives the scene tools. `script`: the agent writes each asset as one Python file and `run_build` runs it (copy installs only) |
| `--validate PLUGIN_DIR` | only validate a bundle and exit |

The scheduled automations do not start themselves; turn them on in the Automations dashboard. The plugin's pieces,
its two modes and how each was checked against the harness documentation are in the
[plugin's README](../plugin/antigravity-3d/README.md).

### Script mode: an asset as one Python file

`agy3d-plugin --authoring script` installs the plugin in its second mode. It is not the default.

- **What the agent writes:** one Python file per asset, `from agy3d import Asset`: the scene format's shapes plus
  rods along any path, skins through cross-sections, computed meshes and Blender snippets.
- **What runs it:** one tool, `run_build`, runs the file, checks the design and returns findings and colour views.
- **Delivery:** `deliver_package` is unchanged, and the script travels with the package as `source/build.py`.
- **Measured** against the default on three briefs: no faster and no cheaper for a first build; its assets were
  preferred in blind comparisons, and a change request became an edit of the file.
- **Not done yet:** no image-model textures; the check before a script runs is not a sandbox. Numbers and details:
  [`benchmarks/ROUND_SIX.md`](../benchmarks/ROUND_SIX.md).

## Replaying and promoting distilled repair rules

`tactics/distill.py` collects observed repairs. To replay a box-seating fix against two authored examples:

```bash
python -m antigravity3d.runtime.distill_job --workspace . --holdout examples/distill-seating-holdout.json
```

Supply independent studio labels for a studio review. An eligible report can be promoted by repeating the command
with `--promote seat-floating-box-v1 --evidence DIGEST --reviewed-by NAME`. The plugin's weekly scheduled job only
writes reports.
