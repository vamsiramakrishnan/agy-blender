# antigravity-3d

antigravity-3d turns a text prompt, or existing mesh files, into game-engine-ready 3D assets: a reviewed OpenUSD stage and glTF/USD packages checked against per-engine rules for Unreal 5, Unity URP, Godot 4 and Roblox R15. It is built on the Google Antigravity agent SDK, the Gemini API and OpenUSD, with an optional warm Blender 5.3 worker pool for the steps that need Blender.

## Status

- An offline run exercises the USD compiler, geometry kernels, material packaging and inspector with deterministic image and judge fixtures. It produces a preview candidate.
- Every exported package is loaded in three.js under headless Chromium as a pipeline step (`web_runtime`): the GLB and its LOD files must load, match the manifest (triangles, bounds, materials, textures) and match the USD's silhouette. The step is skipped, with the reason, where no browser is installed.
- When a real Blender worker is attached (`--blender`, `--pool-socket`, or the plugin's pool sidecar through `$AGY3D_POOL_SOCKET`), the exported GLB is also checked by a script the project did not write: `qa_check` of EL4CTEO/blender-skills (pinned commit, vendored by hash), run by the worker on the GLB with coincident vertices merged and the target engine's profile (`roblox` for Roblox, `game` otherwise). It is the pipeline step `blender_asset_qa` (and a record under `validation.blender_asset_qa` in `manifest.json`). An error from it (a floating or inverted part, z-fighting, missing UVs or materials, a Roblox mesh over 20,000 triangles) fails the step, so the run is not `ok` and not `verified`, as a failed engine import is; its warnings are kept in the manifest. Without a real worker the step is skipped with the reason and changes nothing: a fake worker's result is never counted. It runs in `agy3d prop` and in a native session's `export_engine_package`. `agy3d kit --blender PATH` (or `--pool-socket`) likewise runs that repository's `kit_check` on every piece file with the kit's grid, pivot and sheet size (node `blender_kit_check`; an error fails the kit's gates).
- When a real Blender worker is attached, a package for `unreal_5`, `unity_urp` or `roblox_r15` also gets an FBX, `<asset>.fbx`, written into the package by the worker with EL4CTEO/blender-skills' `export_asset` script (pinned commit, vendored by hash; its `unity`, `unreal` and `roblox` presets) from the package's own GLB files. Godot gets none (it imports the glTF). It is the pipeline step `export_fbx`, after every other step, and the manifest lists the file with its real hash and an `fbx` record. What it contains, by what each engine's documentation describes: **Unity**: one mesh per LOD level named `<asset>_LOD0`, `_LOD1`, ... (Unity builds an LOD Group from that suffix), no collision hulls (Unity documents no FBX naming for them); **Unreal Engine**: the LOD0 mesh `<asset>` and the convex hulls as `UCX_<asset>_<nn>` (Epic's FBX pipeline page names the `UCX_` rule; it documents "Import LODs" but no LOD naming inside one FBX, so the LOD levels stay in the GLB and USD files), and its normal map is the DirectX one the package ships for Unreal; **Roblox**: one mesh, with its textures embedded (Roblox's export page asks for Copy and Embed Textures), the same bytes as the package's `roblox_surface_appearance` colour and normal maps. For Unity and Unreal the FBX refers to the package's `textures/` files by relative path and copies nothing, so the package can be moved with the FBX in it. An FBX carries geometry, UVs, LODs where the engine documents them, collision for Unreal, and the texture links Blender's FBX exporter writes (base colour and normal): roughness, metallic, occlusion and emission maps are not in it (`fbx.maps.lost` lists them), and the GLB and USD stay the full-fidelity files. **What was checked: the written file was re-imported in Blender and compared with the package's GLB files (object, material and triangle counts, UV layers, bounding box in metres, image bytes) and its header was parsed from the file (unit scale, axes, per-model rotation and scale). It was not opened in Unity, Unreal Engine or Roblox Studio, none of which runs in CI or on the machines this was built on.** The FBX's hash is not reproducible (Blender writes the time of export into its header and offers no option to fix it; `fbx.reproducibility`). The FBX is an extra output, not a check: if it cannot be made the package ships without it, the manifest says `fbx.status: failed` with the reason, the step reads `output_failed` (printed as `FAILED OUTPUT export_fbx`), and `verified`, `preview` and `ok` are unchanged. Without a real worker the step is skipped with the reason and nothing pretends an FBX exists.
- Stock Blender 5.3 supports the warm worker path over Unix IPC and shared memory. Workbench, EEVEE and CPU Cycles render regression fixtures. Godot 4 is the first target with a rendered verification path. Unreal and Unity adapters currently cover import checks. Roblox lacks a headless verifier.
- Native Decisions access depends on the project. A verified run needs a native Decisions deployment. On a key without it, the only live output is an unverified preview. It is judged by an uncalibrated generative fallback and bounded by your own token and image limits (see "Keys without the native Decisions API" below). The credentialed canaries in the test suite exercise native Decisions and Nano Banana independently.
- The structured CLI author, Decisions and GenMedia report usage with provenance. Monetary estimates depend on the configured deployment rates. Studio acceptance and reconciled billing are separate inputs to the cohort benchmark.
- Antigravity plugin installation, stage-tool registration and hook behavior have automated tests. Interactive host discovery and studio first-pass acceptance require their respective environments.

## Install

Python 3.11 or newer is required.

```bash
pip install -e ".[dev]"
```

Add the `web` extra (`pip install -e ".[dev,web]"`) for the three.js package verifier; it also needs a Chromium or Chrome binary (`$CHROMIUM_BIN`, or one on `PATH`).

Live runs (any command without `--offline`) need a Gemini API key:

```bash
export GEMINI_API_KEY=...
```

## Quick start

Build an offline prop. This needs no API key and no Blender:

```bash
agy3d prop "wooden crate with iron brackets" --offline --engine unreal_5 --out out/crate
```

Read `out/crate/report.json` for the candidate paths, skipped steps, provider usage and gate results. `outputs.inspector` opens the asset with PBR channels, LODs and budget gauges. The package contains the USD, USDZ, GLBs, textures and a manifest. Preview runs keep their own output directory so a failed retry cannot replace a verified asset.

For a verified Godot publication, configure Gemini access, install Godot and pass a stock Blender executable or the managed sidecar's socket. The live command requires publication by default. Use `--preview` to request a candidate.

```bash
export GODOT_BIN=/opt/godot/godot
agy3d prop "wooden crate with iron brackets" --engine godot_4 --out out/live-crate \
  --blender /opt/blender/blender --require-verified
```

Set `AGY3D_TEXT_RATES_JSON` to a JSON object mapping each configured text-model ID to its contracted `[input USD per million tokens, output USD per million tokens]` pair. The metered author checks the configured rate before dispatch. Standard tiers use the worker role and hero assets use the director role. Model overrides need matching rates.

### Keys without the native Decisions API or text prices

A key whose `decisions.create` answers 404 cannot produce a verified package. It can produce an unverified preview, judged by a generative fallback. That judge is uncalibrated: it may route, rank candidates, drive the repair rounds and fail a gate, but it never verifies, publishes or clears anything. Without configured prices, spend is bounded by two limits you state: a token ceiling, which is input plus output tokens with no price applied, and a hard image count.

```bash
agy3d prop "wooden crate with iron brackets" --preview --token-ceiling 150000 --max-images 4 --single-candidate --out out/preview-crate
agy3d kit "ruined stone temple" --pieces 4 --token-ceiling 400000 --max-images 1 --out out/preview-kit
```

- `--token-ceiling` is the most the whole command may use. For a kit, every piece and the trim sheet draw from it. Every call reserves its worst case, in tokens, before it is sent, and books the provider's reported usage afterwards. Receipts say `"unit": "tokens", "price_applied": false`.
- `--max-images` is required with `--token-ceiling` whenever the command can generate an image. Without `--single-candidate`, a prop's plan needs several images per material.
- Texture images are generated for the winning design's materials only, largest surface first, from each material's own surface words. When the image limit does not cover every material, the ones left untextured are named in the run's warnings and in the manifest.
- The fallback judge can take minutes for one call: on 2026-11-21 the same texture ranking took 30 s, 176 s and more than 240 s. A call with no answer within 600 s is not sent again, and what it reserved stays counted.
- The preview's `manifest.json` says `verified: false`, `preview: true`, who judged it and what that means. The command ends with `publication: UNVERIFIED PREVIEW` and `review: required`. Exit status 0 means every other gate passed; a hard policy flag, an escalation to a person or a failed gate exits 1.
- Without `--preview`, or with `--require-verified`, such a key is refused at start with the command to run instead. A ceiling too small for one judge call and one authoring call is also refused at start, with the smallest ceiling that can work. In both cases nothing is generated.
- In an Antigravity session the same switch is `open_asset_session(preview=true, token_ceiling=N, max_images=M)`, or `start_preview` on an open session. The plugin asks you to approve both numbers.

A headless Linux renderer needs a working display or Xvfb. A missing target renderer, a stale verdict, or a simulated judge leaves `verified` false. A successful verified run makes the package visible under the published path. Use `--pool-socket PATH` to reuse workers across CLI processes.

Other subcommands:

```bash
agy3d sanitize FILES... --engine godot_4 --out out/clean [--jobs N]   # batch-repair GLB, OBJ, PLY, STL or USD meshes
agy3d kit "ruined stone temple" --pieces 4 --offline --out out/kit [--splat bg.ply] [--blender PATH]   # modular kit on one shared trim sheet; a worker adds the upstream kit_check node
agy3d variants ASSET.usda... --offline --out out/looks   # look variants as a USD variant set
agy3d calibrate LABELS.json --out studio_policy.yaml [--offline]   # fit studio thresholds from labelled history
agy3d models                                         # print the model tier table
agy3d plugin install [--target DIR] | agy3d plugin validate [PATH]   # install or check the Antigravity plugin
```

`prop`, `sanitize`, `kit` and `variants` take `--engine` (`unreal_5`, `unity_urp`, `godot_4` or `roblox_r15`; default `unreal_5`) and an optional `--max-tris N`. `calibrate`, `models` and `plugin` do not. Exit status 0 means the requested mode passed its gates. Status 1 signals a failed gate and 2 a usage or environment error.

### Measure the cost per accepted asset

For each brief, run the same prompt and engine budget twice in separate output directories under the same cache policy. Add `--single-candidate` to the baseline only. Record studio first-pass and final acceptance, rework minutes, reconciled spend and input/output tokens across the entire run, including rejected attempts. Use independent human labels. The benchmark rejects reused reports, mismatched budgets, fixture-backed verdicts and first-pass labels without a verified asset.

```json
{"ceiling_usd": 2.2, "pairs": [
  {"brief_id": "crate-01",
   "single": {"report": "baseline/report.json", "first_pass": false, "accepted": true,
              "rework_minutes": 12, "billed_usd": 0.41, "input_tokens": 9800, "output_tokens": 3200},
   "governed": {"report": "governed/report.json", "first_pass": true, "accepted": true,
                "rework_minutes": 0, "billed_usd": 0.70, "input_tokens": 16000, "output_tokens": 5400}}
]}
```

These numbers illustrate the input format. `agy3d benchmark cohort.json` reports spend per accepted asset, total tokens, wall p50/p95 and paired quality deltas. The rollout gate needs at least 50 pairs, every governed run within the studio ceiling, and either a 10-point first-pass gain or a 20% reduction in rework minutes.

## Architecture

| Layer | Folder | What it does |
| --- | --- | --- |
| 0. Adapters | `src/antigravity3d/adapters/` | Connects to external tools: the Blender 5.3 worker pool (`blender_5_3/`) and headless engine import checks (`engines/`). |
| 1. Kernel | `src/antigravity3d/kernel/` | Deterministic geometry and OpenUSD code: the stage manager, Scene IR compiler, UVs, LODs, colliders, validation and export. |
| 2. Harness | `src/antigravity3d/harness/` | Wraps the Antigravity SDK: stage agent factories, lifecycle hooks, the tools the model calls, and session state. |
| 3. Decisions and image generation | `src/antigravity3d/decisions/`, `src/antigravity3d/genmedia/` | Routes questions to the Decisions API under calibrated thresholds; generates images and derives PBR maps. |
| 4. Tactics and pipelines | `src/antigravity3d/tactics/`, `src/antigravity3d/dags/` | Refinement, tournaments, caching and budgets, plus the DAGs that run whole pipelines. |

Also in the package: `runtime/` holds the entrypoints for the plugin (MCP server and its inspector MCP App, depot watcher, weekly distillation, Blender pool daemon). `artifacts/` builds the inspector HTML. `ir.py` defines the Scene/Asset IR and `state.py` the session state shared by tools and hooks. `models.py` holds the model tier table, `cli.py` the `agy3d` command, and `plugin_install.py` the plugin installer.

Main modules, from the module docstrings:

**Layer 0: adapters**
- `adapters/blender_5_3/pool.py`: warm pool of Blender daemons.
- `adapters/blender_5_3/daemon.py`: Blender worker daemon.
- `adapters/blender_5_3/protocol.py`: wire protocol between the pool and a daemon.
- `adapters/blender_5_3/linter.py`: AST compatibility and safety gate for scripts sent to `run_script`.
- `adapters/blender_5_3/shim_5_3.py`: normalises pre-5.3 Blender API usage in legacy scripts.
- `adapters/blender_5_3/viewport_bridge.py`: live-viewport bridge for artists. Written to spec and not exercised.
- `adapters/engines/validators.py`: headless engine import smoke tests behind one interface.

**Layer 1: kernel**
- `kernel/usd_stage.py`: OpenUSD stage manager with transactional sublayers and variant-set branches.
- `kernel/compile_ir.py`: compiles a Scene IR into USD opinions on the edit target.
- `kernel/fast_ops.py`: deterministic in-process mesh kernels for Tier-1 QA, UVs, LODs, colliders and export.
- `kernel/trim_atlas.py`: trim-sheet-first UVs, with one shared texture per kit.
- `kernel/settle.py`: deterministic vertical contact settling.
- `kernel/lod_diff.py`: LOD pop diffing at the switch distance.
- `kernel/validator.py`: USD validation and per-engine compliance rules.
- `kernel/package.py`: engine package writer, shared by the harness and the DAGs.
- `kernel/raster.py`: software clay rasteriser for the preview tier.
- `kernel/som.py`: set-of-mark numbered badges on renders.
- `kernel/spatial.py`: octree index over prim bounding boxes.
- `kernel/cache.py`: content-addressed Merkle build cache.

**Layer 2: harness**
- `harness/agents.py`: stage-scoped agent factories, the Turn-0 director and `run_stage`.
- `harness/hooks.py`: lifecycle hooks for stage agents.
- `harness/tools.py`: model-facing tools.
- `harness/session.py`: session wiring shared by tools and hooks.
- `harness/traceback_compressor.py`: squeezes an exception into one line for tool results.

**Layer 3: decisions and image generation**
- `decisions/engine.py`: `DecisionControlPlane`, the single choke point for Decisions API calls.
- `decisions/factory.py`: builds the control plane on whichever backend the key can use.
- `decisions/questions.py`: typed question builders and the studio question banks.
- `decisions/compose.py`: deterministic composition of answers (pure functions).
- `decisions/fallback.py`: generative fallback for the Decisions API.
- `decisions/calibration.py`: ROC/AUC, ECE and threshold selection from labelled history.
- `decisions/fakes.py`: `FakeDecisionsClient`, a stand-in that enforces the wire format.
- `decisions/models.py`: SDK-independent result types.
- `genmedia/nano_banana.py`: `ImageForge` for blueprints, textures, trim sheets, decals and paint-overs.
- `genmedia/pbr_deriver.py`: deterministic PBR map synthesis and auto-fixers (no model calls).

**Layer 4: tactics and pipelines**
- `tactics/authoring.py`: the Scene IR authoring interfaces, the non-deterministic seam.
- `tactics/pyramid.py`: multi-resolution refinement: silhouette lock, lookdev, final verification.
- `tactics/tournament.py`: speculative Best-of-N.
- `tactics/governor.py`: cost and quality budget governor.
- `tactics/cached.py`: Merkle-cache wrappers for the expensive pure steps.
- `tactics/package.py`: engine-ready export artefacts (UV-split meshes, LODs, hulls).
- `tactics/distill.py` collects observed repairs. Run `python -m antigravity3d.runtime.distill_job --workspace . --holdout examples/distill-seating-holdout.json` to replay a box-seating fix against two authored examples. Supply independent studio labels for a studio review. An eligible report can be promoted by repeating the command with `--promote seat-floating-box-v1 --evidence DIGEST --reviewed-by NAME`. The scheduled job only writes reports.
- `dags/prop_to_engine.py`: prompt to engine-ready, reviewed prop package.
- `dags/environment_kit.py`: modular pieces sharing one trim sheet, assembled into one USD stage.
- `dags/batch_sanitize.py`: many meshes to safe repairs, UVs, LODs and colliders, then validated export.
- `dags/variant_factory.py`: look variants as a USD variant set of material overrides.
- `dags/runner.py`: small async DAG runner.

## Model tiers

Defined in `src/antigravity3d/models.py`:

| Role | Model id | Thinking | Used for |
| --- | --- | --- | --- |
| `director` | `gemini-4-argon` | HIGH | Plan, hard maths, ambiguous escalations. Slow; give it a generous timeout. |
| `builder` | `gemini-4-carbon` | MEDIUM | Stage agents: blockout, lookdev, export. |
| `worker` | `gemini-4-hydrogen` | LOW | Ephemeral critics, swarm prop builders. |
| `bulk` | `gemini-4-neon` | LOW | Batch sanitise, trivial IR edits. |
| `decision` | `gemini-decision-1` | - | Routing, Best-of-N ranking, visual QA, policy. |
| `image` | `gemini-nano-banana-2.1` | - | 2K blueprints, texture maps, trim sheets, decals and paint-overs. |

The prop DAG writes `ledgers/<asset-key>/spend.sqlite3` under the output directory. The key includes the prompt, engine budget, style content, judge backend and candidate mode. Retries reuse the original route and ledger. Pending reservations count against the ceiling across restarts. Image receipts retain model, provider response ID, token counts by modality, estimated USD and artifact SHA-256. Replays have zero generation cost. When a dispatched request times out or the provider omits modality counts, the ledger retains the reserved amount. For a custom image deployment, set `AGY3D_MODEL_IMAGE` and all three per-million-token rates: `AGY3D_IMAGE_INPUT_USD_PER_M`, `AGY3D_IMAGE_TEXT_OUTPUT_USD_PER_M`, and `AGY3D_IMAGE_IMAGE_OUTPUT_USD_PER_M`. The legacy `AGY3D_IMAGE_MODEL` override takes precedence.


The verification notes in `models.py`: `gemini-4-hydrogen`, `gemini-4-carbon` and `gemini-4-neon` answered live against the Gemini API; `gemini-4-argon` exists but is slow. The harness bundled with google-antigravity 0.1.24 names hydrogen, carbon and argon but not neon. The live API rejects `ThinkingLevel.MINIMAL` for neon and hydrogen, so LOW is the floor.

Overrides:

- `AGY3D_MODEL_DIRECTOR`, `AGY3D_MODEL_BUILDER`, `AGY3D_MODEL_WORKER`, `AGY3D_MODEL_BULK`, `AGY3D_MODEL_DECISION`, `AGY3D_MODEL_IMAGE`: replace the model id for one role.
- `AGY3D_NEON_AVAILABLE=0`: routes the `bulk` tier to hydrogen if a harness build rejects neon.

Run `agy3d models` to print the effective table after overrides.

## Antigravity plugin

`plugin/antigravity-3d/` is the Antigravity plugin bundle. It contains:

- **Rules:** `rules/AGENTS.md` (always on), `rules/blender-5-3-api.md` (Python and .blend files) and `rules/engine-budgets.md` (USD files and `*_exports/` folders).
- **Skills:** `concept-to-blueprint`, `parametric-kitbash`, `pbr-material-forge`, `game-ready-sanitizer` and `engine-truth-verify`.
- **Subagents:** `agents/usd_verifier.md` (read-only QA gate) and `agents/lookdev_critic.md` (visual critic).
- **Hooks:** `hooks.json` with four scripts in `hooks/scripts/`. They block export until Tier-1 QA passes, record tool telemetry, add a session-start budget message, and stop the agent from finishing while QA is unmet.
- **Sidecars:** `blender_pool`, `depot_watcher` and two scheduled automations: `nightly_variant_factory` (daily at 02:17 UTC) and `weekly_distill` (Sundays at 03:43 UTC).
- **Workflow script:** `workflows/batch_sanitize.py`, run with `run_workflow`.
- **MCP server:** `mcp_config.json` registers the stdio server `studio` (`antigravity3d.runtime.mcp_server`). The harness exposes it as `antigravity-3d_studio`.
- **MCP App:** the same server serves the 3D review inspector as an MCP App (`open_inspector`; inline or from the side pane "+" menu). Review notes are saved with the asset session, reach the agent through `updateModelContext` and the `review_notes` tool, and are marked stale, not dropped, after an edit. The front end lives in `apps/inspector` (rebuild with `npm ci && npm run build`; the built document is committed and a test fails when it is stale). The standalone `inspector.html` in each package is the same front end.

Install and validate:

```bash
agy3d plugin install [--target DIR]     # copies the bundle to the customization root (default ./.agents) and validates it
agy3d plugin validate [PATH]            # validates the bundled bundle, or the directory given
```

`agy3d-plugin` is the same installer as `python -m antigravity3d.plugin_install`. That entry point also takes `--symlink`, `--workspace` and `--depot`. The scheduled automations do not start themselves; turn them on in the Automations dashboard.

`plugin/antigravity-3d/README.md` has a table of which plugin formats the Antigravity harness documentation confirms and which it does not. Read it before relying on any plugin behaviour.

## Testing

CI (`.github/workflows/ci.yml`) runs this on Python 3.12 and 3.13:

```bash
python -m pytest -q -m "not live and not blender"
```

Integration markers include:

- `live`: real model calls and image generations (a stage-agent session, a judge call, a 2K image). They run only when
  asked for, with `--run-live` or `AGY3D_RUN_LIVE=1`, and need `GEMINI_API_KEY`. A key in the environment alone does
  not run them, so a plain `pytest` never spends quota.
- `blender`: needs a Blender 5.3 or newer binary on `PATH` or in `$BLENDER_BIN`. Set `AGY3D_BLENDER_SOAK=1` to run the 300-render isolation test.
- `browser`: needs Playwright and Chromium. The inspector tests also need `AGY3D_THREE_JS` pointing to Three.js 0.160.0; the package verifier uses the copy vendored in the wheel. The qualification workflow supplies all of it on a Linux software-rendering runner.


## Known limitations

- Without the native Decisions API nothing is verified. A preview's judge reports probabilities that are not calibrated, and a token ceiling bounds tokens, not money: image tokens cost far more than text tokens, which is why the image count is a separate limit.
- Contact settling is vertical only (no tilting).
- Trim-sheet mapping distorts smooth, round shapes.
- The preview renderer is a software rasteriser, not a PBR render.
- UV stretch and LOD pop are gates, not warnings. Their thresholds are engineering defaults that no studio acceptance data has calibrated: stretch within the engine budget; LOD levels generated within one pixel of LOD0 on a 1080 px frame; and a pop check per level (silhouette IoU at least 0.90, shading difference at most 0.06, edge drift at most 2.5 px), with each level lit by the normals it ships. The generator bounds geometry only, so nothing guarantees a level passes the shading part of the check; on the 30 stress subjects measured, all do.
- LOD levels are produced only where the asset gets at least 25% cheaper within that error budget, so some assets ship LOD0 only. A part hidden inside another (a barrel hoop's inner faces) is never dropped at distance, because the measure is surface distance, not visibility.
- LOD generation measures the deviation of every simplification attempt and costs more than plain simplification on dense meshes (about 0.7 s at 50,000 triangles); see `benchmarks/RESULTS.md`. The deviation recorded for a level is a bound on it, not the largest distance sampled.
- Each LOD level gets its shading normals from its own geometry (smooth across faces within 40 degrees), so a curved mesh can turn faceted at a far level. The pop check sees that; LOD0's normals are not carried through simplification.
- The material model covers the glTF 2.0 PBR set (emission, alpha modes, double-sided, clearcoat, transmission, index of refraction, sheen, specular). Transmission is thin-surface only: there is no volume (thickness, attenuation). Only the emissive map and the albedo alpha channel were added as textures; the other new parameters are factors.
- `blender_asset_qa` and `blender_kit_check` are a second opinion from one upstream script each; they are not a full QA of the asset. Merged, the scripts agree with this project's own counts on the props surveyed (`benchmarks/README.md`, "Upstream checks survey"), but two things differ in kind. Its "hidden back-to-back faces" warning is a proximity test (faces within 1 mm facing each other), the export's rule is exact coplanar full coverage: the crate and barrel each still carry one such warning (16 pairs between the wooden and the iron batch: parts whose faces overlap only partly, and the barrel's hoops, which lie on a curved body without coplanar faces), and it stays a warning. And upstream's Roblox limit of 20,000 triangles counts a mesh of the GLB, where parts of one material are submitted as one batch once the asset has more nodes than `max_draw_calls`: the package writer therefore splits a material's batch at 20,000 triangles (parts kept whole; `glb_batches` and the Roblox note count the split meshes), `validate_package` counts the triangles of every mesh of the shipped GLB, and the authored-part rule still rejects a single part over the limit, which no split can fix. The step costs about 0.1 s per file on a warm worker (about 0.4 s for the four pieces of a kit, beside the other nodes); a run that has no other use for Blender would pay about 0.7 s to start one, so a worker is never started for this step alone.
- The three.js verifier compares what the runtime loaded with the manifest and the USD silhouette. It does not judge the rendered look, and it says nothing about Unreal, Unity, Godot or Roblox importers; those have their own steps where an engine is installed.
- Faces that two parts press together (a lid's underside on a box, a strap against a side) are left out of the shipped render meshes in every LOD level when another opaque part covers them completely: they cannot be seen from outside, and they cost triangles and atlas space. The working stage, Tier-1 and the colliders keep the closed parts, and `manifest.json` lists what was removed per part (`contact_faces`). Each part stays its own USD prim, so a part moved away from where it was authored (a lid opened in the engine) shows an opening where it touched another part: export with `--keep-contact-faces` (the `keep_contact_faces` field of the budget, or of `open_asset_session`) to ship the closed parts. Roblox requires closed meshes, so nothing is removed for `roblox_r15`.
- In a far LOD level a part can be absent. In USD that part is inactive inside the LOD variant; no importer was run here to confirm how each engine treats it.
- The inspector's MCP App is tested against the MCP Apps SDK's own host bridge in Chromium, not inside a running Antigravity app.
- Rolling back a rejected 50-node speculative edit takes 0.17 ms at the median on the development machine, and an undo that keeps history 0.65 ms (2.0 ms at 200 nodes). The first read after a rollback costs about 4 ms more at 50 nodes. Sub-millisecond worst-case rollback remains unqualified.
- Collider generation on concave meshes can exceed the 250 ms pipeline target. A solid made of flat faces is decomposed as that solid, not as its triangles (about 150 ms for a 2,000-triangle prism); a curved concave solid still costs CoACD about 1 ms per triangle (about 2 s for a 1,600-triangle torus), and an open surface 2.5 to 12 s.
- A collider input that is not one closed solid (a hole, a non-manifold edge, duplicate or flipped faces, bodies that only touch) is repaired on a private copy before decomposition. A decomposition that is not known to be bounded runs in a child process with a deadline of 30 s or more; when it expires, or the child crashes, the body gets its convex hull and the manifest's `collider_sources` says why. The child is killed by process group, which is POSIX only.
- A mesh that needs computed charts gets them from xatlas, re-flattened and packed here. Chart computation dominates on dense meshes (about 2.3 s at 50,000 triangles), and a curved mesh does not always reach the requested texel density (a sphere reaches about 890 to 930 px/m of 1,024). Primitives get exact charts.
- The three.js verification takes about 0.7 s per package once a browser is running in the process, 1.5 s with six 1,024 px maps, and several seconds with 2,048 px maps (software GL). The first verification of a process also starts the browser. For the length of one verification the page directory is served from 127.0.0.1 under a random path; every other request the page makes is blocked and fails the check.
- In a package only `<asset>.glb` (LOD0) is self-contained. `<asset>_LOD<n>.glb` reference the maps in the package's `textures/`, so a package is copied or imported as a directory. A map in a format glTF cannot reference (anything but PNG or JPEG) is still embedded in every LOD file. Unity staging of far LOD files is untested: no Unity runs in CI.

## License

See [LICENSE](LICENSE).
