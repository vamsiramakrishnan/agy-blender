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

## A website is not an asset

A request for a website, web page, landing page or scroll-driven 3D explainer
(three.js, WebGL, shaders, "scrolls beautifully") goes to the skill
`scroll-3d-website` first, before any modelling: it says which props the story
needs, when to build them and how they reach the page. Its props are then built
by the short path below.

A request for a story, a short film, an animated explainer, a title sequence,
a talk or a slide deck told scene by scene in three.js or WebGL (a fixed frame
the viewer steps through, not a page that scrolls) goes to the skill
`story-slides-3d` first, before any modelling: its sets are boxes and light, and it
says when one prop is worth building.

## The short path

Every model call sends the whole conversation so far, so the number of calls
is what a session costs. For a new asset:

1. `open_asset_session` with the user's request as `brief`.
2. `apply_scene_ir`: the first call sets the materials and the main body, then
   two or three calls of at most 25 operations each; write anything that occurs
   more than once once, with `mirror` or `repeat`. A reply is cut off at 16,384
   output tokens, thinking included, and an operation costs about 100: a call
   that was cut off is split and sent again.
3. `render_preview` and look at the views; improve with one or two more
   `apply_scene_ir` patches and look again.
4. `deliver_package`: it reviews the design as it will ship, builds LODs and
   colliders, validates and exports, and says what the review found. When the
   request names a folder for the result, pass it as `dest`; the tool places the
   engine files there, so do not copy files by hand afterwards. Its
   answer starts with one of four:
   - `DELIVERED`: the run is finished; the package was validated, imported and
     checked by the tool. Reply to the user with its path and the findings,
     and do not inspect it again.
   - `NOT DELIVERED: a person must decide`: reply to the user with the reason
     and stop; changing the surfaces or the design to get past it is not wanted.
   - `NOT DELIVERED` otherwise: fix what it names with `apply_scene_ir` and
     call `deliver_package` again.
   - `STILL WORKING`: nothing failed and nothing needs fixing; the work goes on
     by itself. Make the call the answer spells out (the same tool, the same
     arguments, the session it names): it answers with the result and does no
     step twice. Any tool call that takes more than about two and a half
     minutes answers this way, and `session_status` shows which step it is
     at. (`check_site` is the exception: at its limit it answers with what it
     has seen so far and says so.)

Build the design with `apply_scene_ir`, not by scripting Blender: only a
design in the session's stage can be edited, checked and packaged by these
tools (Blender on the command line is for inspecting files).

Nothing else is required. Put tool calls that can go together in one turn,
read a tool's schema only for the tools you call, and do not explore folders:
the tools' answers say what you need. The stage tools below are for finer
control when the user asks for it.

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
- Everything you read stays in every later request, so read only what the
  next step needs: `parametric-kitbash` before modelling; `pbr-material-forge`
  only in a session that can generate images (with an image limit of 0 the
  surfaces are generated from the geometry when the package is written);
  `engine-truth-verify` only for a verified export or an export problem.

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
manifold geometry. A draw call is a material as it ships, not a material as it
is named: every opaque material shares one atlas and one draw call, however
many there are; a see-through material adds one, and so does each different
emissive strength, coat or sheen. Per-engine limits are in `engine-budgets.md`. The session's
budget is set by `open_asset_session` (target engine and `max_tris`); a
`budget.json` in the project's `.agy3d/` folder is read only by the SessionStart
hook, which shows it.

## Blender 5.3 (summary)

Use `BLENDER_EEVEE` (not `BLENDER_EEVEE_NEXT`), boolean solver `FLOAT` (not
`FAST`), the `gpu` module (no `bgl`), and `AssetRepresentation` (not
`AssetHandle`). Details in `blender-5-3-api.md`.
