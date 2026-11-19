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

1. **Read the stage.** `inspect_manifest` for the current node tree, units
   (must be metres, Z-up) and the registered orthos and parts list.
2. **Primary forms first.** Emit one Scene IR patch that creates the major
   parts as parametric primitives (box, cylinder, capsule, sweep, lathe,
   bevelled box) named from the parts list, sized from the ortho
   measurements in metres, parented in a clean hierarchy. Apply with
   `apply_scene_ir`. Keep each patch small (one part group per patch) so a
   failed patch is cheap to roll back.
3. **Secondary forms.** Add kitbash details as further patches: booleans
   (solver `FLOAT` in the Blender worker), arrays, mirror, bevel. Re-use
   instanced parts for repeats so draw calls stay in budget.
4. **Silhouette lock loop.** For each ortho view: `render_preview` the matching
   orthographic silhouette, then `run_visual_qa` with rubric
   `silhouette_iou` against the ortho. Lock condition: IoU at least 0.92 in
   every view. If a view fails, read the returned delta (which part, which
   axis, how far), emit one corrective `apply_scene_ir` patch that changes
   parameters only, and repeat. Cap at 6 iterations; after that, report the
   worst view and its delta to the user.
5. **Budget check.** `inspect_manifest` again: triangle count and draw calls
   must be inside the session budget before lookdev. If over, reduce segment
   counts in the IR parameters rather than decimating.
6. **Gate.** Delegate to the `usd_verifier` subagent for Tier-1 (manifold,
   scale, naming) so the blockout is clean before `pbr-material-forge`.

## Rules

- Parameters, not vertices: every correction is a parameter change in a patch.
- Never call `export_engine_package` from this skill.
