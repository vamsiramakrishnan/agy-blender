---
name: game-ready-sanitizer
description: >-
  Makes an existing asset file (FBX, glTF/GLB, OBJ or USD) game-ready: imports
  it into a USD stage, normalises units and axes, fixes UVs, generates LODs
  and colliders, and runs the USD validator and QA gate. Use when the user
  hands over existing or third-party files, or when the depot watcher starts
  a batch-sanitize run.
---

# Game-ready sanitizer

For many files at once, run the plugin workflow
`workflows/batch_sanitize.py` with `run_workflow` (Args
`{"files": [...], "target_engine": "..."}`); it applies this skill per file
in parallel. For one file, follow the steps directly.

## Steps (per file)

1. **Import to USD.** Import the source into a new stage through the Scene IR
   import op (`apply_scene_ir` with an `import` node). The stage, not the
   source file, is the asset from here on.
2. **Normalise.** `inspect_manifest`: units must be metres and up axis Z. If
   not, apply one Scene IR transform op that converts scale and axis at the
   root. Apply transforms, recentre the pivot at the base centre, and rename
   prims to `[A-Za-z0-9_]+`.
3. **Geometry hygiene.** `run_usd_validator` and read the issues: non-manifold
   edges, zero-area faces, flipped normals, loose verts, over-budget
   triangles. Fix each with Scene IR cleanup ops, then validate again.
4. **UVs.** Every mesh needs a UV set with no overlaps (except deliberate
   mirroring), stretch at most 0.05, and texel density within 10% of the
   budget. Re-unwrap or repack through Scene IR if not.
5. **LODs and colliders.** `generate_lods_and_colliders` with the target
   engine: LOD1/2/3 at about 50/25/12% triangles, convex-decomposition hulls
   within the hull budget, engine collision naming (`UCX_` for UE5,
   `-col` suffixes for Godot, components for Unity).
6. **Validate and gate.** `run_usd_validator` until clean, then delegate to the
   `usd_verifier` subagent for the Tier-1 and Tier-1.5 verdict. Report the
   verdict line; do not export unless the user asked for it and the gate
   passed (then hand to `engine-truth-verify`).

## Output

One line per file: `<file>: PASS|FAIL tier1=<ok|fail> tier15=<score> tris=<n> lods=<n> hulls=<n>`.
