---
name: parametric-kitbash
description: >-
  Builds the 3D model of a prop in the USD stage as Scene IR patches (box,
  cylinder, cone, sphere, capsule): a finished game prop with its secondary forms
  (rims, ribs, hinges, bolt heads, raised or recessed panels), not a blockout,
  then loops on a render and visual QA until the silhouette matches, and fixes
  floating parts, flicker (z-fighting) and triangle budget. Also builds modular
  kit pieces (wall, doorway, floor tile, pillar) to one metric. Use when the user
  asks to model, build, make or kitbash an asset, or to fix its shape, parts or
  gaps. Not for textures or materials (use pbr-material-forge), for a mesh file
  the user already has (use game-ready-sanitizer) or for export (use
  engine-truth-verify).
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
   metres, Z-up. The orthos and parts list come from `concept-to-blueprint`.
2. **Primary forms first.** Emit one Scene IR patch that creates the major
   parts as parametric primitives (box, cylinder, cone, sphere, capsule;
   group for a transform-only parent; mesh_ref for an existing mesh file)
   named from the parts list, sized from the ortho
   measurements in metres, parented in a clean hierarchy. Apply with
   `apply_scene_ir`. Keep each patch small (one part group per patch) so a
   failed patch is cheap to roll back.
3. **Secondary forms.** Add detail as further primitive patches of the same
   kinds: thin boxes for raised or recessed panels, plates and bars rather than
   chunky blocks. Scene IR has no boolean, array, mirror, bevel or instancing ops, so
   write each repeat as its own node and keep draw calls in budget.
4. **Silhouette lock loop.** `render_preview` (a 4-view clay contact sheet),
   then `run_visual_qa`, which takes no arguments. Lock when it reports no
   geometry repair. If it reports one, read the returned delta and fix line,
   emit one corrective `apply_scene_ir` patch that changes parameters only,
   and repeat. When the session was opened with a `brief` and the delta says a
   part the brief names is missing or the brief depiction is below its
   minimum, the patch adds those parts (and the secondary detail of the
   object) as new nodes instead. Cap at 6 iterations; after that, report the remaining delta to
   the user.
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

A pass is all three at once. If the session has no `brief`, step 3 only checks
shape and contact, never that the object is what was asked for.

## Rules

- Parameters, not vertices: every correction is a parameter change in a patch.
- Never call `export_engine_package` from this skill.

## Gotchas

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
- **Symptom:** corners and edges read blocky even with many parts. **Cause:**
  the scene format has no rounded edge, recess or profile, and a judge's
  remaining preference in the comparison was exactly that. **Remedy:** use thin
  plates and slim bars, wrap corners with corner guards, and say plainly in the
  hand-off that edges are not rounded. This sentence is stale once new shapes
  land.
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

Last verified 2026-11-22 against commit 29660b8.

- The scene format's schema already lists lathe and extrude, and has
  parameters for rounded edges and recesses, but at this commit the compiler
  does not build them: use only box, cylinder, cone, sphere, capsule, group and
  mesh_ref. Until the new shapes are built, the Gotcha on blocky corners
  stands.
- The 50 to 90 parts, the 2 mm separation and the 2 mm contact distance are
  figures of the code at this commit (`CRAFT_RULES`, `kernel/separate.py`).
- Kit metrics (0.5 m or 1 m grid, wall heights) are the defaults of `agy3d kit`
  at this commit.
