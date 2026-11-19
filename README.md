# antigravity-3d

antigravity-3d turns a text prompt, or existing mesh files, into game-engine-ready 3D assets: a reviewed OpenUSD stage and glTF/USD packages checked against per-engine rules for Unreal 5, Unity URP, Godot 4 and Roblox R15. It is built on the Google Antigravity agent SDK, the Gemini API and OpenUSD, with an optional warm Blender 5.3 worker pool for the steps that need Blender.

## Status

- **Works and is tested offline** (no API key, no Blender). The whole pipeline runs from prompt to exported package, using built-in template geometry and procedural stand-in images. The suite has about 650 automated tests.
- **Verified against the live Gemini API:** stage agents built on the Antigravity SDK running on `gemini-4-hydrogen` and `gemini-4-carbon`, and image generation. A full non-offline `agy3d prop` run completed but **failed** its quality gates, because the model-authored blockout was weak.
- **Decisions API not verified live.** `gemini-decision-1` returned 404 for the API key used during development. The decision layer has only been tested against a fake that enforces the SDK's wire format. When the Decisions API is unavailable, the package asks a generative model the same questions. Those answers are marked uncalibrated and cannot auto-approve an asset. That fallback was exercised live on `gemini-4-neon`.
- **Not verified:**
  - Anything that runs inside Blender. No Blender was available, so that code is exercised only through a stand-in process.
  - Imports into Unreal, Unity, Godot or Roblox. No engines were available, so those steps report "skipped".
  - The plugin bundle inside a running Antigravity app.
- Dollar costs printed by the tool are estimates from placeholder prices, not real billing figures.

## Install

Python 3.11 or newer is required.

```bash
pip install -e ".[dev]"
```

Live runs (any command without `--offline`) need a Gemini API key:

```bash
export GEMINI_API_KEY=...
```

## Quick start

Build an offline prop. This needs no API key and no Blender:

```bash
agy3d prop "wooden crate with iron brackets" --offline --engine unreal_5 --out out/crate
```

Trimmed output of that command. Paths are shortened to `out/crate/`:

```
agy3d prop: 'wooden crate with iron brackets' -> unreal_5
  offline: fake decisions, fake images, TemplateAuthor
DAG prop_to_engine: OK wall=6738ms ok=9 skipped=1 failed=0 overlap=2
pyramid rounds: A1:approve t15=3.60; B1:approve t15=3.60
render tier: preview; stopped: {'A': 'converged in 1 round(s)', 'B': 'approved in 1 round(s)', 'C': 'verification run'}
scores: tier15=3.60 tier1=OK silhouette=3.60 proportion_accuracy=3.60 pbr_realism=3.60 material_separation=3.60 shippability=3.60
cost: governor(est) $0.4818 | decisions plane $0.002338 over 28 calls | ...
outputs:
  usd: out/crate/package/WoodenCrateIron.usd
  glb: out/crate/package/WoodenCrateIron.glb
  inspector: out/crate/package/inspector.html
  report: out/crate/report.json
skipped:
  - pyramid:engine_import: unreal_5 binary not found (set $UNREAL_EDITOR_CMD or add UnrealEditor-Cmd to PATH)
  ...
gates: PASS
```

The run writes these files under the output directory:

- `package/WoodenCrateIron.usd` and `package/WoodenCrateIron.usdz`: the OpenUSD asset.
- `package/WoodenCrateIron_authoring.usda`: the authoring layer that the Scene IR compiles to.
- `package/WoodenCrateIron.glb`, `_LOD1.glb`, `_LOD2.glb`, `_LOD3.glb`: the mesh and its LOD levels.
- `package/WoodenCrateIron_colliders.glb`: collision hulls.
- `package/textures/`: albedo, normal (DirectX convention) and ORM maps.
- `package/manifest.json`: the package manifest.
- `package/inspector.html`: the single-file WebGL inspector for the asset.
- `package/ortho.png` and `ortho.png`: orthographic reference renders.
- `WoodenCrateIron_stage.usda` and `report.json`: the stage and the run report.
- `.agy3d-cache/`: the build cache. Pass `--no-cache` to skip it.

`examples/offline_crate.py` runs the same pipeline from Python. See `examples/README.md`.

Other subcommands:

```bash
agy3d sanitize FILES... --engine godot_4 --out out/clean [--jobs N]   # batch-repair GLB, OBJ, PLY, STL or USD meshes
agy3d kit "ruined stone temple" --pieces 4 --offline --out out/kit [--splat bg.ply]   # modular kit on one shared trim sheet
agy3d variants ASSET.usda... --offline --out out/looks   # look variants as a USD variant set
agy3d calibrate LABELS.json --out studio_policy.yaml [--offline]   # fit studio thresholds from labelled history
agy3d models                                         # print the model tier table
agy3d plugin install [--target DIR] | agy3d plugin validate [PATH]   # install or check the Antigravity plugin
```

`prop`, `sanitize`, `kit` and `variants` take `--engine` (`unreal_5`, `unity_urp`, `godot_4` or `roblox_r15`; default `unreal_5`) and an optional `--max-tris N`. `calibrate`, `models` and `plugin` do not. Exit status: `0` means every gate passed, `1` means a gate failed, `2` means a usage or environment error.

## Architecture

| Layer | Folder | What it does |
| --- | --- | --- |
| 0. Adapters | `src/antigravity3d/adapters/` | Connects to external tools: the Blender 5.3 worker pool (`blender_5_3/`) and headless engine import checks (`engines/`). |
| 1. Kernel | `src/antigravity3d/kernel/` | Deterministic geometry and OpenUSD code: the stage manager, Scene IR compiler, UVs, LODs, colliders, validation and export. |
| 2. Harness | `src/antigravity3d/harness/` | Wraps the Antigravity SDK: stage agent factories, lifecycle hooks, the tools the model calls, and session state. |
| 3. Decisions and image generation | `src/antigravity3d/decisions/`, `src/antigravity3d/genmedia/` | Routes questions to the Decisions API under calibrated thresholds; generates images and derives PBR maps. |
| 4. Tactics and pipelines | `src/antigravity3d/tactics/`, `src/antigravity3d/dags/` | Refinement, tournaments, caching and budgets, plus the DAGs that run whole pipelines. |

Also in the package: `runtime/` holds the sidecar entrypoints for the plugin (MCP server, depot watcher, inspector server, weekly distillation, Blender pool daemon). `artifacts/` builds the inspector HTML. `ir.py` defines the Scene/Asset IR and `state.py` the session state shared by tools and hooks. `models.py` holds the model tier table, `cli.py` the `agy3d` command, and `plugin_install.py` the plugin installer.

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
- `tactics/distill.py`: failure-to-skill log and clustering.
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
| `image` | `gemini-3.1-flash-lite-image` | - | Orthos, albedo, trim sheets, paint-overs. The SDK default; override it for Nano Banana 2.1. |

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
- **Sidecars:** `blender_pool`, `depot_watcher`, `inspector` (a side-pane panel) and two scheduled automations: `nightly_variant_factory` (daily at 02:17 UTC) and `weekly_distill` (Sundays at 03:43 UTC).
- **Workflow script:** `workflows/batch_sanitize.py`, run with `run_workflow`.
- **MCP server:** `mcp_config.json` registers the stdio server `studio` (`antigravity3d.runtime.mcp_server`). The harness exposes it as `antigravity-3d_studio`.

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

The two markers, defined in `pyproject.toml`:

- `live`: needs `GEMINI_API_KEY` and network access.
- `blender`: needs a Blender 5.3 or newer binary on `PATH` or in `$BLENDER_BIN`.


## Known limitations

- Contact settling is vertical only (no tilting).
- Trim-sheet mapping distorts smooth, round shapes.
- The preview renderer is a software rasteriser, not a PBR render.
- The LOD-pop and UV-stretch warning thresholds are uncalibrated defaults.
- Rolling back an edit measured about 2 ms on a 50-node scene. The design target was under 1 ms.
- Collider generation on concave meshes can exceed the 250 ms pipeline target.

## License

See [LICENSE](LICENSE).
