# antigravity-3d studio invariants

These rules are always on while the antigravity-3d plugin is enabled. Two
glob-scoped rule files add detail when matching files are in play:
`blender-5-3-api.md` (Python / Blender scripts) and `engine-budgets.md`
(USD stages and engine export folders).

## Source of truth and editing

- The OpenUSD stage is the source of truth for every asset. The `.blend`,
  FBX, glTF and engine packages are derived artifacts; never hand-edit them
  and never treat them as canonical.
- Change scenes only through the Scene IR tools: `apply_scene_ir` for
  geometry and layout, `apply_shader_diff` / `forge_pbr_material` for
  materials, `generate_lods_and_colliders` for LODs and collision. Read state
  with `inspect_manifest`, not by opening files.
- Raw `bpy` is an escape hatch, used only when no Scene IR op can express the
  change. Every raw script must pass the Blender 5.3 linter
  (`antigravity3d.adapters.blender_5_3.linter.assert_safe`); the PreToolUse
  hook denies scripts that fail it. No `os`, `subprocess`, file writes,
  `eval`/`exec` or `bpy.app.handlers` in scripts.

## Units and axes

- Author in metres (`metersPerUnit = 1.0`), Z-up, right-handed.
- Axis and unit conversion happens only at export, per engine, inside
  `export_engine_package` (UE5 keeps Z-up and converts to centimetres; Unity,
  Godot and Roblox get Y-up). Never pre-rotate or pre-scale authored geometry
  to suit an engine.

## PBR

- Albedo is sRGB with values in roughly 30-240 (8-bit). No pure black, no
  blown-out white, and no baked lighting, shadows, AO or speculars in albedo.
  De-light generated albedo before use.
- Pack occlusion, roughness and metallic as ORM: R = ambient occlusion,
  G = roughness, B = metallic. ORM and normal maps are linear (non-colour).
- Normal maps are OpenGL convention (+Y up) in the stage. Flip green to
  DirectX only on Unreal export; that conversion is the exporter's job.
- Metallic is effectively binary per texel (0 or 1, with transitions only at
  edges or through dirt or oxidation masks).

## QA gates and export

- Never call `export_engine_package` unless Tier-1 QA passed and the Tier-1.5
  visual score is at least 3.4 for the current state of the scene. Any edit
  after QA makes the verdict stale; re-run QA. The PreToolUse hook enforces
  this and the Stop hook refuses to finish while a changed asset is ungated.
- Delegate gating to the `usd_verifier` subagent and visual critique to the
  `lookdev_critic` subagent so their renders and logs stay out of your
  context.
- Every render goes through the QA tools (`render_preview`, `run_visual_qa`,
  `run_paintover_loop`). They return scores, deltas and file paths, not
  images, so renders do not accumulate in context. Do not open render images
  yourself unless the user asks to see one.

## Engine budgets (summary)

Default studio budget per asset: at most 15,000 triangles, 4 draw calls,
1024 px/m texel density, UV stretch at most 0.05, 8 collision hulls,
manifold geometry. Per-engine limits are in `engine-budgets.md`; the
`budget.json` in the project's `.agy3d/` folder overrides the defaults.

## Blender 5.3 (summary)

Use `BLENDER_EEVEE` (not `BLENDER_EEVEE_NEXT`), boolean solver `FLOAT` (not
`FAST`), the `gpu` module (no `bgl`), and `AssetRepresentation` (not
`AssetHandle`). Details in `blender-5-3-api.md`.
