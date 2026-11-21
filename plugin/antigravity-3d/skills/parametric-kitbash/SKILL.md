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
   and repeat. Cap at 6 iterations; after that, report the remaining delta to
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
   The blockout is clean when `session_status` shows `tier1=ok`. The
   `usd_verifier` subagent is not offered in BLOCKOUT, so do not delegate here.
   Then hand over to `pbr-material-forge`.

## Rules

- Parameters, not vertices: every correction is a parameter change in a patch.
- Never call `export_engine_package` from this skill.
