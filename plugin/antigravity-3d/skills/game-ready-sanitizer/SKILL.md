---
name: game-ready-sanitizer
description: >-
  Makes an existing asset file (glTF/GLB, OBJ, PLY, STL or USD) game-ready: imports
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

1. **Import to USD.** Open an asset session for the target engine. Send a
   patch to `apply_scene_ir` with the absolute local source path.

   ```json
   {"ops":[{"op":"upsert_node","node":{"id":"source","kind":"mesh_ref","params":{"path":"/depot/prop.glb"}}}]}
   ```

2. **Check placement.** Read `inspect_manifest` to check the world bounds
   in metres. glTF imports convert Y-up to Z-up. For formats with unspecified
   units, use the source's unit declaration to set the node scale.
3. **Repair rejected geometry.** Read the Tier-1 rejection. The deterministic
   `agy3d sanitize FILE --engine ENGINE --out OUT` command welds vertices,
   removes degenerate/duplicate faces, fixes winding and regrounds the mesh.
   Re-import a repaired mesh when available and report remaining failures.
4. **Build the engine geometry.** Run `generate_lods_and_colliders`. It
   unwraps UVs and returns per-level triangle counts, hull count and UV
   stretch. Check these values against the requested engine budget.
5. **Validate and gate.** `run_usd_validator` until clean, then delegate to the
   `usd_verifier` subagent for the Tier-1 and Tier-1.5 verdict. Report the
   verdict line; do not export unless the user asked for it and the gate
   passed (then hand to `engine-truth-verify`).

## Output

One line per file: `<file>: PASS|FAIL tier1=<ok|fail> tier15=<score> tris=<n> lods=<n> hulls=<n>`.
