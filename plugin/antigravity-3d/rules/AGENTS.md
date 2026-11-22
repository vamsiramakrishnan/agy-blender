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
- Their IR arguments are typed objects described by each tool's schema:
  `apply_scene_ir(ops=[...])`, `propose_variants(set_name, variants=[{name,
  ops}])`, `apply_shader_diff(material_id, diff={...}, assign_to=[...])`.
  A rejected argument comes back as one `ERROR invalid IR: ...` line naming
  the op, the field and what was expected; fix that field and resend.
- A material's texture is generated from words that name its surface only:
  the material and its finish ("olive drab painted steel, scuffed and chipped"),
  never the object, its parts or layout, because the texture tiles over every
  face. Name the substance, not what is made of it ("tarnished brass", not
  "brass hardware"). That is the `prompt` of `forge_pbr_material` and a material's
  `surface` field.
- Raw `bpy` is an escape hatch, used only when no Scene IR op can express the
  change. Every raw script must pass the Blender 5.3 linter
  (`antigravity3d.adapters.blender_5_3.linter.assert_safe`); the PreToolUse
  hook denies scripts that fail it. No `os`, `subprocess`, file writes,
  `eval`/`exec` or `bpy.app.handlers` in scripts.

## How the tools reach you

- In a conversation you did not start through the Python SDK (the app, the CLI,
  `agentapi`, a workflow agent) the studio tools are served by the plugin's MCP
  server (`studio`, which the harness namespaces `antigravity-3d_studio`; the tool
  list may show the names with that prefix). Call `open_asset_session` first: it
  opens the USD stage and makes it the active session. Pass the user's request
  for the asset, in their words, as its `brief`: visual QA shows it to the judge,
  which then checks that every part it names is there and how fully the asset
  depicts it. Without a `brief` nothing is checked against the request.
- Only the active stage's tools are callable; anything else is refused with a
  line naming the stage. Move with `set_stage` (BLOCKOUT, TWEAK, VARIANTS,
  LOOKDEV, EXPORT) and read `session_status` when unsure where you are.
- The refusals are the gates working, not errors to route around: export needs a
  passing Tier-1 and Tier-1.5 >= 3.4 on the current geometry.

## Units and axes

- Author in metres (`metersPerUnit = 1.0`), Z-up, right-handed.
- Axis conversion happens only at export, per engine, inside
  `export_engine_package` (UE5 keeps Z-up; Unity, Godot and Roblox get Y-up).
  Units stay in metres for every engine (`metersPerUnit = 1.0`). Never
  pre-rotate or pre-scale authored geometry to suit an engine.

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
- In a preview judged by the uncalibrated fallback, the score is a finding,
  not a gate: export needs Tier-1 and the validator to pass and a lookdev
  review (`run_visual_qa` in LOOKDEV) of the current scene, whatever it
  scored. Use its findings for at most two improvement rounds after the first
  review, then export: the package and the tool's answer list what the judge
  found, for the user to look at. A hard policy flag or a review that
  escalates to a person still blocks; ask the user.
- A verified export needs an approval from the native Decisions API. When the
  key has no access to it, `open_asset_session` says so: visual QA and export
  will be refused. Do not work around that. Ask the user whether they want an
  UNVERIFIED preview, and ask for their two limits: a token ceiling and an image
  limit. Never choose those numbers yourself. Then call `start_preview` with
  them (the scene is kept), or open the session with `preview` and both limits.
  The user is shown both numbers and approves before the preview opens. A
  preview is judged by an uncalibrated judge and its package says
  `verified: false`; it is never verified and must not be presented as such.
- Where your stage offers them, delegate gating to the `usd_verifier` subagent
  (VARIANTS and LOOKDEV) and visual critique to the `lookdev_critic` subagent
  (LOOKDEV) so their renders and logs stay out of your context.
- Look at your work: `render_preview` returns the design's five views with
  numbered part badges (small ones inline; open any the reply marks with
  view_file). Look after every few edits and improve what you see before
  `run_visual_qa`, whose findings name the parts and views. Other renders
  (`render_lookdev`, `run_paintover_loop`) return file paths and scores.
- A preview opened with an image limit of 0 generates no texture: the image
  tools answer `NO IMAGES`, and the package writer makes the surfaces from the
  geometry. Keep materials' ids and base colours and continue to export.
- To change an asset that already exists, `open_existing_asset` and edit only
  the parts asked for; never author it again.

## Engine budgets (summary)

Default studio budget per asset: at most 15,000 triangles, 4 draw calls,
1024 px/m texel density, UV stretch at most 0.05, 8 collision hulls,
manifold geometry. Per-engine limits are in `engine-budgets.md`. The session's
budget is set by `open_asset_session` (target engine and `max_tris`); a
`budget.json` in the project's `.agy3d/` folder is read only by the SessionStart
hook, which shows it.

## Blender 5.3 (summary)

Use `BLENDER_EEVEE` (not `BLENDER_EEVEE_NEXT`), boolean solver `FLOAT` (not
`FAST`), the `gpu` module (no `bgl`), and `AssetRepresentation` (not
`AssetHandle`). Details in `blender-5-3-api.md`.
