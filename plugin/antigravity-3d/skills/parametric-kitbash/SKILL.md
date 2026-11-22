---
name: parametric-kitbash
description: >-
  Builds a parametric blockout from an approved ortho blueprint by applying
  Scene IR patches, then runs a silhouette-lock loop until the blockout's
  silhouettes match the orthos. Use after concept-to-blueprint, or whenever
  the user asks for a blockout, greybox or kitbash of an asset in the USD stage.
---

# Parametric kitbash (Scene IR blockout + silhouette lock)

All geometry changes go through `apply_scene_ir`. Never write raw `bpy` for a
blockout; if an op is missing, report it instead of using the escape hatch.

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
   kinds. Scene IR has no boolean, array, mirror, bevel or instancing ops, so
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
   The blockout is clean when `session_status` shows `tier1=ok`. The
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

## Rules

- Parameters, not vertices: every correction is a parameter change in a patch.
- Never call `export_engine_package` from this skill.
