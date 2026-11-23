# How it works

Where the code for each step lives, for when you want to change it or find out why it did something. What a run
does, end to end: a design is written as a scene description, compiled into an OpenUSD stage, checked, unwrapped
into one texture atlas, given lower-detail versions and collision shapes, textured, packaged as GLB and USD (and FBX
for some engines) and verified. Blender 5.3 is used only as a worker process, and only for the steps that need it.

The project is built on the Google Antigravity agent SDK, the Gemini API and OpenUSD, with an optional warm Blender
5.3 worker pool.

## The five layers

| Layer | Folder | What it does |
| --- | --- | --- |
| 0. Adapters | `src/antigravity3d/adapters/` | Connects to external tools: the Blender 5.3 worker pool (`blender_5_3/`) and headless engine import checks (`engines/`). |
| 1. Kernel | `src/antigravity3d/kernel/` | Deterministic geometry and OpenUSD code: the stage manager, Scene IR compiler, UVs, LODs, colliders, validation and export. |
| 2. Harness | `src/antigravity3d/harness/` | Wraps the Antigravity SDK: stage agent factories, lifecycle hooks, the tools the model calls, and session state. |
| 3. Decisions and image generation | `src/antigravity3d/decisions/`, `src/antigravity3d/genmedia/` | Routes questions to the Decisions API under calibrated thresholds; generates images and derives PBR maps. |
| 4. Tactics and pipelines | `src/antigravity3d/tactics/`, `src/antigravity3d/dags/` | Refinement, tournaments, caching and budgets, plus the DAGs that run whole pipelines. |

Also in the package:

- `runtime/`: the entry points for the plugin (the MCP server and its inspector MCP App, the depot watcher, the
  weekly distillation, the Blender pool daemon).
- `artifacts/`: builds the inspector HTML.
- `ir.py`: the Scene/Asset IR (the scene description). `state.py`: the session state shared by tools and hooks.
- `models.py`: the model tier table ([keys_and_cost.md](keys_and_cost.md#model-tiers)). `cli.py`: the `agy3d`
  command. `plugin_install.py`: the plugin installer.
- `delivery.py`: what a finished run delivered, what its review found, and the exit code that follows (`agy3d prop`
  and `agy3d edit`).

## Where each command's work lives

| What | Where |
|---|---|
| Installing into a game project (`agy3d install`, `install_into_project`) | `deliver/`: `install.py` (install and check), `detect.py` (which engine and version a project is), `godot.py` (the files written into a Godot project and the check in its engine), `glb.py`, `paths.py` |
| Changing a shipped asset (`agy3d edit`) | `dags/edit_asset.py` |
| Script mode (`from agy3d import Asset`, `run_build`) | `build/`: `asset.py` (the `Asset` object), `shapes.py` (free-form parts), `blender_part.py` (Blender snippets), `guard.py` (the read of a script before it runs), `runner.py` (runs it in a bare process), `handoff.py`; the script-mode texts in `build/plugin_script_mode/` |
| A prop, a kit, a batch of meshes, look variants | `dags/prop_to_engine.py`, `dags/environment_kit.py`, `dags/batch_sanitize.py`, `dags/variant_factory.py` |

## Every module, layer by layer

From the module docstrings.

### Layer 0: adapters

- `adapters/blender_5_3/pool.py`: warm pool of Blender daemons.
- `adapters/blender_5_3/daemon.py`: Blender worker daemon.
- `adapters/blender_5_3/protocol.py`: wire protocol between the pool and a daemon.
- `adapters/blender_5_3/linter.py`: AST compatibility and safety gate for scripts sent to `run_script`.
- `adapters/blender_5_3/shim_5_3.py`: normalises pre-5.3 Blender API usage in legacy scripts.
- `adapters/blender_5_3/viewport_bridge.py`: live-viewport bridge for artists. Written to spec and not exercised.
- `adapters/engines/validators.py`: headless engine import smoke tests behind one interface.

### Layer 1: kernel

- `kernel/usd_stage.py`: OpenUSD stage manager with transactional sublayers and variant-set branches.
- `kernel/compile_ir.py`: compiles a Scene IR into USD opinions on the edit target.
- `kernel/fast_ops.py`: deterministic in-process mesh kernels for Tier-1 QA, UVs, LODs, colliders and export.
- `kernel/trim_atlas.py`: trim-sheet-first UVs, with one shared texture per kit.
- `kernel/settle.py`: deterministic vertical contact settling.
- `kernel/separate.py`: moves apart the flush (coplanar, same-facing) faces of different parts, without a model call.
- `kernel/lod_diff.py`: LOD pop diffing at the switch distance.
- `kernel/validator.py`: USD validation and per-engine compliance rules.
- `kernel/package.py`: engine package writer, shared by the harness and the DAGs.
- `kernel/raster.py`: software clay rasteriser for the preview tier.
- `kernel/som.py`: set-of-mark numbered badges on renders.
- `kernel/spatial.py`: octree index over prim bounding boxes.
- `kernel/cache.py`: content-addressed Merkle build cache.

### Layer 2: harness

- `harness/agents.py`: stage-scoped agent factories, the Turn-0 director and `run_stage`.
- `harness/hooks.py`: lifecycle hooks for stage agents.
- `harness/tools.py`: model-facing tools.
- `harness/session.py`: session wiring shared by tools and hooks.
- `harness/traceback_compressor.py`: squeezes an exception into one line for tool results.

### Layer 3: decisions and image generation

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

### Layer 4: tactics and pipelines

- `tactics/authoring.py`: the Scene IR authoring interfaces, the non-deterministic seam. The craft guidance a prop's
  authoring request carries is described in [commands.md](commands.md#agy3d-prop-a-prop-from-a-sentence).
- `tactics/pyramid.py`: multi-resolution refinement: silhouette lock, lookdev, final verification.
- `tactics/tournament.py`: speculative Best-of-N.
- `tactics/governor.py`: cost and quality budget governor.
- `tactics/cached.py`: Merkle-cache wrappers for the expensive pure steps, and the design record (`DesignRecord`) of
  answered authoring requests.
- `tactics/package.py`: engine-ready export artefacts (UV-split meshes, LODs, hulls).
- `tactics/distill.py`: collects observed repairs ([commands.md](commands.md#replaying-and-promoting-distilled-repair-rules) has the job that
  replays and promotes them).
- `dags/prop_to_engine.py`: prompt to engine-ready, reviewed prop package.
- `dags/environment_kit.py`: modular pieces sharing one trim sheet, assembled into one USD stage.
- `dags/batch_sanitize.py`: many meshes to safe repairs, UVs, LODs and colliders, then validated export.
- `dags/variant_factory.py`: look variants as a USD variant set of material overrides.
- `dags/runner.py`: small async DAG runner.

## Where the Antigravity plugin is described

`plugin/antigravity-3d/` is the Antigravity plugin bundle: rules, skills, two subagents, hooks, sidecars, a workflow
script, an MCP server and the inspector MCP App. Its [README](../plugin/antigravity-3d/README.md) says what each
piece does, how the tools reach a conversation, and which plugin formats the harness documentation confirms. Read
that table before relying on any plugin behaviour.
