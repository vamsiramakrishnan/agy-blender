---
name: parametric-kitbash
description: >-
  Builds the 3D model of a prop in the USD stage as Scene IR patches: a finished
  game prop, not a blockout, from boxes, cylinders, cones, spheres, capsules,
  turned (lathe) and extruded parts, with rounded edges, recessed panels and
  mirrored or repeated parts; then loops on a render and visual QA until the
  silhouette matches, and fixes floating parts, flicker and triangle budget. Also builds modular kit pieces (wall, doorway, floor tile,
  pillar) to one metric. Changes an asset that already exists ("make the two
  side handles 30 % larger") by reopening it and
  patching only what is named, never by modelling it again. Use when the user
  asks to model, build, make or kitbash an asset, to change an existing one, to
  round the edges or add panels, or to fix its shape, parts or gaps. Not for textures or materials (use pbr-material-forge), for a mesh file
  the user already has (use game-ready-sanitizer) or for export (use
  engine-truth-verify) or for a website that shows the models (use
  scroll-3d-website).
---

# Parametric kitbash (Scene IR model + silhouette lock)

All geometry changes go through `apply_scene_ir`. Never write raw `bpy` for the
model; if an op is missing, report it instead of using the escape hatch.

The target is a finished production prop, not a blockout: the stage's own
instructions say so (`CRAFT_RULES` in `harness/agents.py`). Every part the
brief names gets its own nodes built from the pieces a real one has, secondary
forms go wherever they change the silhouette or catch the light, and the part
count is in the tens (the stage asks for 50 to 90 within the triangle budget),
not a handful of blocks.

## Steps

1. **Read the stage.** `inspect_manifest` for the current node tree, bounds
   and triangle total; `session_status` for the budget. The stage is always
   metres, Z-up. If a blueprint was made, its orthos and parts list come from
   `concept-to-blueprint`; a text brief alone is enough to start.
2. **Primary forms first.** Emit one Scene IR patch that creates the major
   parts, named from the parts list, sized from the ortho measurements in
   metres, parented in a clean hierarchy. Apply with `apply_scene_ir`. Keep
   each patch small (one part group per patch) so a failed patch is cheap to
   roll back. Use the shape that matches how the part is made: box, cylinder,
   cone, sphere, capsule; lathe for anything turned (a barrel, post, knob,
   lamp: one profile of 6 to 16 points, not a stack of cylinders); extrude for
   a constant section (a bracket, arch, ramp, L or U section); group for a
   transform-only parent; mesh_ref for an existing mesh file.
3. **Secondary forms.** Add detail as further patches. Nothing manufactured has
   a razor edge: round box and cylinder edges with a bevel of 3 to 5 % of the
   part's smallest size, on parts thicker than about 1 cm (1 segment for metal
   and small parts, 2 where a large rounded edge is seen). A panel set into a
   face is an inset face of that box, not a thin box stacked on it. Write a
   symmetric or repeated part once: mirror for a left and right pair, repeat
   with an offset for a row, repeat around an axis for a ring of bolts; model
   the original on the positive side. What each parameter means is in the tool's
   schema and the stage's instructions; the rules of mirror and repeat are in
   `ir_shortcuts.py`. The kernel never clamps a value: it builds it as given
   or refuses the node, naming it and the numbers.
4. **Look and improve.** Every model call sends the whole conversation, so
   build in a few patches (the first the materials and main body, then two or
   three of at most 25 operations, repeated parts written once with mirror or
   repeat; a reply is cut off at 16,384 output tokens, so split a call that was
   cut off and send it again) and put calls that can go together in one turn; when the
   design reads right, `deliver_package` reviews, packages and exports it in one
   call (pass the folder the request names for the result as `dest`; do not copy
   files by hand afterwards); its answer `DELIVERED` ends the run (reply with the path and the
   findings), `NOT DELIVERED: a person must decide` means reply with the reason
   and stop, and any other `NOT DELIVERED` names what to fix before calling it
   again. `render_preview` shows you the design: five 512 px
   views (front, right, top, two perspectives from opposite corners), shaded
   as the package ships with part outlines drawn, a numbered badge on every
   part and a legend from badge to node id. Small views arrive inline; open a
   view the reply marks "open it with view_file" with view_file. Look at every
   view after every few patches, compare it with the request and add what is
   missing, plain or wrong (name parts by node id). A design that uses less
   than half of the triangle budget and reads as plain boxes is not done. Then
   `run_visual_qa`, which takes no arguments: it judges the same five views,
   and its `finding` lines name each defect's parts with their badges and the
   views they are seen in. Lock when it reports no geometry repair. If it
   reports one, emit a corrective `apply_scene_ir` patch for the parts named,
   and repeat. When the session
   was opened with a `brief` and the delta says a part the brief names is
   missing or the brief depiction is below its minimum, the patch adds those
   parts (and the secondary detail of the object) as new nodes instead. Cap at
   6 iterations; after that, report the remaining delta to the user. In a
   preview judged by the uncalibrated fallback its scores are findings, not a
   wall: make at most two improvement rounds after the first review, then go
   on (the export lists what the judge found).
5. **Budget check.** `inspect_manifest` again: its header gives the total
   triangle count, which must be inside the budget `session_status` shows
   before lookdev. If over, reduce segment counts in the IR parameters rather
   than decimating.
6. **Gate.** Every `apply_scene_ir` runs Tier-1 and is rolled back when it
   fails. Its warnings do not roll back but name real defects: a z_fighting
   warning means two parts have same-facing faces in one plane that overlap
   (they flicker in every engine); move or resize one so the faces either
   meet back to back or differ by more than 1 mm. Parts pressed face to face
   are fine: the faces hidden between them are left out of the export.
   Parts are judged by the distance between their surfaces (2 mm): a
   floating_part warning means a part off the ground touches no other part,
   and a floating_group means two or more parts touch each other and nothing
   else; the detail names the nearest part, the gap and where it lies, so move
   the part (or group) until it touches. An enclosed_part warning means a part
   sits inside another without touching it: unseen while the outer part is
   opaque, floating if it is see-through. A not_watertight warning is an open
   mesh (critical for Roblox, which requires closed meshes).
   The model is clean when `session_status` shows `tier1=ok`. The
   `usd_verifier` subagent is not offered in BLOCKOUT, so do not delegate here.
   Then hand over to `pbr-material-forge`.

## Changing an asset that already exists

A request about an asset that is already made ("make the two side handles
30 % larger", "add a padlock to the front latch") is an edit, not a new model.
Modelling it again changes every part; the developer asked for one.

1. **Reopen it.** `open_existing_asset` with its prop output folder (holds
   `report.json`) or its package folder (holds `manifest.json`). The stage is
   the design as it shipped, after every repair, each material on its source
   maps; the stage is TWEAK. Never `open_asset_session` for it.
2. **Find the parts.** `inspect_manifest` lists every part id with its bounds.
   Ids of mirrored and repeated parts are expanded (`handle__mx__grip`).
3. **One patch, only what is named.** `apply_scene_ir` with ops on those
   parts: `set_param` for a size, `set_transform` to move or scale (a parent's
   transform moves its children), `upsert_node` to add, `remove_node` to remove.
   Keep a part's mount face where it was so it still touches what it sits on.
   Do not add a material: an edit generates no image, so a new material ships
   without a texture.
4. Then `render_preview` to check the change, and `deliver_package`.

From the command line the same is `agy3d edit OUT "<change>"`: one authoring
call shown the five badged views and the instruction, kept only when the judge
sees the change done and nothing else changed and no part floats; the replaced
package goes to `OUT/history/NNN/` and `agy3d edit OUT --undo` puts it back.
`agy3d edit OUT --patch ops.json` applies a patch you wrote, with no model call.

## The same loop in `agy3d prop`

The command runs this loop with the model that writes the design, and it
differs from a native session in what that model sees and when it stops:

- The author is sent five marked views of its design (front, right, top and
  two opposite perspectives, each its own image, 512 px on a side) beside the
  judge's findings, so it improves what it can see.
- A design that passes its checks but scores below the target on how fully it
  depicts the request gets improvement rounds, up to a number set by the spend
  plan (`--improve-rounds N` overrides it); a repair is for a flagged defect, an
  improvement for a score. A design whose repairs were kept gets its
  improvement rounds too: they do not share the repairs' rounds. A round is
  kept provisionally and undone at once only on the kernel's evidence; every
  two rounds the judge is shown the new state beside the last validated one,
  in both orders, and the rounds stand only when the new state is preferred
  both times. The rounds stop at the first validation that does not prefer it.
  A repair is asked again after it is undone, told what its patch did and why,
  and the pass stops after two undone repairs in a row.
- The judge sees each design in its own request, as separate 512 px views.
- An answer that does not read (it parses as JSON but fails validation, or a
  node fails the kernel's check) is sent back once with the failing nodes as
  written and the exact messages, and the author returns only the nodes to
  replace; a truncated, blocked, empty or timed-out answer is not retried this
  way.

## Modular kit pieces

When the asset is a piece of a modular environment kit (a wall, doorway, floor
tile or pillar meant to snap to others), build it to ONE kit metric and use the
same metric for every piece of that kit. The default interior metric: a 0.5 m
grid, 2 m modules, walls 3 m high and 0.25 m thick, a door opening 1 m wide and
2.5 m high (0.5 m jambs), floor tiles 0.1 m thick, pillars 0.5 x 0.5 m. The
exterior metric: a 1 m grid, 4 m modules, walls 4 m high and 0.5 m thick, a
2 x 3 m door, 0.2 m floors and 1 x 1 m pillars.

- Origin at the module's minimum corner on the floor: a wall occupies x 0 to
  the module length, y 0 to the thickness and z 0 to the height.
- The two ends (x) and the -Y face are mating faces. Keep them flat and full.
  Relief (trims, mouldings) goes on the +Y face only, at most 0.04 m proud
  (0.08 m exterior). A doorway's opening stays empty through the whole
  thickness. A floor tile's top and sides are flat and full.
- Build large surfaces from stacked parts no larger than 0.75 m on their
  shorter side. The kit shares one trim sheet whose strips hold less than
  0.9 m of surface, and a larger face is shrunk onto the strip at lower
  texel density.
- Check the result with `inspect_manifest` (each node's bounds) and
  `query_region` over the opening. In a session nothing runs the kit's metric
  check or snap test. Those run in the `agy3d kit` command, which fails a
  kit whose pieces are off the metric, do not snap, or differ in texel
  density. To check a piece that way, build the kit there.

## Check before handing over

1. `session_status` shows `tier1=ok`.
2. `inspect_manifest` header: the total triangle count is inside the budget
   `session_status` shows, and no warning of the kinds above is open.
3. `run_visual_qa` reports no geometry repair, with the session opened with the
   user's request as `brief`.
4. The triangle budget holds with the new shapes counted at what they cost: a
   box is 12 triangles, bevelled 44 (1 segment) or 108 (2), plus 16 per inset
   face; a cylinder 4 per segment, more when bevelled; a lathe 2 per segment
   per profile step; an extrude 4 x corners - 4; a sphere or capsule hundreds.

A pass is all three at once. If the session has no `brief`, step 3 only checks
shape and contact, never that the object is what was asked for.

## Rules

- Parameters, not vertices: every correction is a parameter change in a patch.
- Never call `export_engine_package` from this skill.

## Gotchas

- **Symptom:** a change request ("make the handles 30 % larger") comes back as
  a different asset: 0 parts unchanged and 11 changed that nobody asked about
  (the comparison harness, 2026-11-22, the sentence appended to the original
  prompt). **Cause:** the asset was modelled again from a longer prompt.
  **Remedy:** reopen it (`open_existing_asset`, or `agy3d edit`) and patch only
  the named parts; offline the same edit as a patch left 8 of 10 parts
  unchanged and resized the 2 named ones by exactly the ratio asked.
- **Symptom:** after an edit an untouched part flickers against an edited one.
  **Cause:** the edit made two faces flush, and fixing that would move the part
  that was not asked about, so `agy3d edit` leaves it and says so in a warning.
  **Remedy:** move or resize the edited part by more than 1 mm yourself.

- **Symptom:** the model reads as a rough box with blocks stuck on (15 to 21
  parts and 272 to 356 triangles of a 3,000 budget). **Cause:** the request to
  the author asked for the object and said nothing about craft, so it built
  the minimum. **Remedy:** build the pieces a real one has (a latch is a base
  plate, a clasp and a hinge barrel), add the secondary forms, and use the
  budget; the same brief then gave 55 parts and 868 triangles
  (`benchmarks/AFTER_THE_COMPARISON.md`, "Quality").
- **Symptom:** many interpenetration and z-fighting warnings after adding
  detail (103 to 116 and 28 to 38 per design in one experiment). **Cause:** the
  instruction was to sink parts a millimetre or two into what they sit on.
  **Remedy:** attach by contact, a face exactly on the surface it is mounted
  on, with no gap and no overlap.
- **Symptom:** a surface flickers in every engine. **Cause:** two parts have
  same-facing faces in one plane that overlap (a handle grip as thick as its
  bracket, a latch plate as thick as the panel it sits in). **Remedy:** the
  `agy3d prop` pipeline moves one face of the smaller part 2 mm by arithmetic
  after the design is chosen and after each repair (`kernel/separate.py`); in a
  session, act on the z_fighting warning yourself as step 6 says.
- **Symptom:** the Blender asset check refuses an export for coincident faces
  or, on Roblox, for open meshes. **Cause:** flush faces (above) or a part
  that is not closed. **Remedy:** fix it on the stage and re-run QA; never
  edit the exported file.
- **Symptom:** corners and edges read blocky even with many parts, and a
  judge preferred the stock agent's crate for exactly that. **Cause:** until
  round five the scene format had no rounded edge, recess or profile.
  **Remedy:** bevel boxes and cylinders, use inset faces for panels, one lathe
  per turned part, one extrude per constant section, as steps 2 and 3 say.
- **Symptom:** `apply_scene_ir` or the author's answer is refused naming a
  node. **Cause:** the kernel builds a value as given or refuses it, with the
  node, the parameter and the numbers: "bevel 0.5 must be below half the
  smallest extent (0.1)"; "inset_margin must be > 0 when inset_faces is set
  (without a frame the recess cuts the edge away)"; "inset_margin 0.6 leaves
  no panel on face +x ..."; a lathe profile that touches the axis between its
  ends, or an extrude outline that is not a simple polygon. **Remedy:** change
  that parameter as the message says; a bevel is below half the smallest
  extent, an inset face keeps a frame and a depth above zero.
- **Symptom:** a mirror or repeat is refused. **Cause:** the part must lie on
  one side of the mirror plane (one that straddles it would be copied onto
  itself), a mesh_ref cannot be mirrored, and a repeat counts 2 to 32 with
  the original, as a row (offset) or a ring (around), never both. **Remedy:**
  move the part to one side, or write the two halves by hand. After the
  shortcut is expanded the copies are plain nodes (ids such as id__r1 and
  id__mx): a later patch edits one copy only.
- **Symptom:** a candidate design was lost or rescued in the run report.
  **Cause:** its answer failed to read; it is corrected once (the report says
  "rescued" or "failed" with the messages), never for a truncated or empty
  answer. **Remedy:** none for a rescued one; for a failed one read both
  messages, they name the node and the parameter.
- **Symptom:** visual QA passes a plain box. **Cause:** the session was opened
  without a `brief`, and none of the judge's questions mentions what was
  asked. **Remedy:** `open_asset_session` with the user's words as `brief`;
  then a missing named part and a depiction below 2.5 of 4 both send the
  design back.
- **Symptom:** `agy3d prop` ended with no package after a model service
  answered 503 or a connection broke. **Cause:** a failure at one of about ten
  calls ended the run, and the design it paid for lived only in memory (round
  three). **Remedy:** rerun the same command with the same `--out`: the design
  and the images the failed run paid for are taken from the record, and only
  the rest is paid for. `--new-design` asks for a fresh design and keeps the
  images whose requests did not change. A 503 that says the request was not
  served is already retried at 2, 4, 8, 16 and 32 seconds.
- **Symptom:** a kit piece's large wall face looks blurry next to the others.
  **Cause:** the kit's trim sheet holds strips of less than 0.9 m, and a larger
  face is shrunk onto one at lower texel density. **Remedy:** build large
  surfaces from stacked parts no larger than 0.75 m on their shorter side.

## Version notes

Last verified 2026-11-22 against commit af568e0 (shapes, authoring and
install merged; the design loop and the judge views of round five not yet).

- Node kinds: box, cylinder, cone, sphere, capsule, lathe, extrude, group and
  mesh_ref. Box bevel and inset faces, cylinder bevel, lathe and extrude are
  built by `kernel/shapes.py`; `compile_ir.triangle_count` is the one source
  of a part's triangle cost.
- The part count follows the triangle budget: 50 to 90 parts when max_tris
  allows (50 plain boxes are 600 triangles), as many as fit when it is smaller
  (an art direction such as retro-lowres caps a prop at 400).
- Measured live (plan settings, read by the tests from the code): the
  improvement rounds per spend plan (rounds come in pairs: a round validated
  alone was never preferred). Provisional: their target score.
- The 2 mm separation and the 2 mm contact distance are figures of
  `kernel/separate.py`.
- Kit metrics (0.5 m or 1 m grid, wall heights) are the defaults of `agy3d kit`.
