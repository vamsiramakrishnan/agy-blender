---
name: game-ready-sanitizer
description: >-
  Makes an existing mesh file game-ready: imports glTF/GLB, OBJ, PLY, STL or USD
  into a USD stage, normalises units and axes, welds vertices, removes degenerate
  and duplicate faces, fixes winding, unwraps UVs, generates LODs and collision
  hulls, and runs the USD validator and QA gate, for one file or a whole depot
  folder. Use when the user hands over a file made by them or a third party
  ("clean up this OBJ", "my scan has flipped faces", "make this model ready for
  Unity"), or when the depot watcher starts a batch-sanitize run. Not for making
  a new asset from a prompt (use concept-to-blueprint, then parametric-kitbash),
  for materials (use pbr-material-forge) or for the engine package (use
  engine-truth-verify).
---

# Game-ready sanitizer

For many files at once, run the plugin workflow
`workflows/batch_sanitize.py` with `run_workflow` (Args
`{"files": [...], "target_engine": "..."}`); it applies this skill per file
in parallel. For one file, follow the steps directly.

## Steps (per file)

1. **Import to USD.** Open an asset session for the target engine. Call
   `apply_scene_ir` with an `ops` list (typed objects, not a JSON string)
   that references the absolute local source path; every node states its
   `transform`.

   ```json
   {"ops":[{"op":"upsert_node","node":{"id":"source","kind":"mesh_ref","params":{"path":"/depot/prop.glb"},"transform":{"translate":[0,0,0]}}}]}
   ```

2. **Check placement.** Read `inspect_manifest` to check the world bounds
   in metres. glTF imports convert Y-up to Z-up. For formats with unspecified
   units, use the source's unit declaration to set the node scale.
3. **Repair rejected geometry.** Read the Tier-1 rejection. The deterministic
   `agy3d sanitize FILE --engine ENGINE --out OUT` command welds vertices,
   removes degenerate/duplicate faces, fixes winding and regrounds the mesh.
   Re-import a repaired mesh when available and report remaining failures.
4. **Build the engine geometry.** Switch with `set_stage` to EXPORT, then run
   `generate_lods_and_colliders`. It unwraps UVs and returns per-level triangle
   counts, hull count and UV stretch. Check these values against the requested
   engine budget.
5. **Validate and gate.** `run_usd_validator` until clean. The gate needs
   Tier-1 ok and Tier-1.5 at least 3.4; `session_status` shows both, and
   Tier-1.5 comes from `run_visual_qa`, which runs in BLOCKOUT, VARIANTS and
   LOOKDEV. `usd_verifier` is offered only in VARIANTS and LOOKDEV, and it
   reports Tier-1 only, so delegate to it there. Report the verdict line; do
   not export unless the user asked for it and the gate passed (then hand to
   `engine-truth-verify`).

## Output

One line per file: `<file>: PASS|FAIL tier1=<ok|fail> tier15=<score> tris=<n> lods=<n> hulls=<n>`.

## Check

Run `agy3d sanitize FILE --engine ENGINE --out OUT` (several files at once are
fine, `--jobs N` sets the worker processes). A pass is the exit code 0 with a
`PASS` row per file in its table and no `remaining in` or `critical` line under
it; `report.json` in the output folder holds the same rows.

## Gotchas

- **Symptom:** a file's row says the largest dimension is implausible for a
  prop (its largest side is above 20 m or below 1 cm). **Cause:** the source
  has no unit declaration or a wrong one (millimetres read as metres). **Remedy:**
  the validator names the unit reading that would fit and does not rescale by
  itself; ask the user for the source unit, then set the node scale.
- **Symptom:** the row lists issues `remaining`. **Cause:** the repairs are
  limited to ones that cannot change the shape (weld, drop degenerate and
  duplicate faces, fix winding, reground); anything else is reported, not
  guessed at. **Remedy:** report them to the user with the file name; do not
  patch the mesh by hand to make the gate pass.
- **Symptom:** one file of a batch is `failed` with a crash or a timeout while
  the others finished. **Cause:** a worker died (the collider decomposition has
  segfaulted on some meshes) or passed the per-file limit (default 900 s); it
  is killed and replaced and only that file's row fails. **Remedy:** rerun
  that file alone, with a larger `--timeout` for a timeout; report a repeated
  crash with the file.
- **Symptom:** the imported asset sits half under the ground or floats.
  **Cause:** a source origin that is not at the base; the sanitizer regrounds
  the whole asset when its lowest point is more than 1 cm from zero.
  **Remedy:** check the bounds with `inspect_manifest` after import.
- **Symptom:** a glTF imports on its side. **Cause:** glTF is Y-up and the
  stage is Z-up; the import converts. **Remedy:** do not rotate again; read the
  world bounds in metres.

## Version notes

Last verified 2026-11-22 against commit 29660b8.

- Accepted inputs: .glb, .gltf, .obj, .ply, .stl and the USD extensions.
  Other formats (FBX among them) are not read.
- The per-file timeout default is 900 s; the slowest benchmark mesh (28,000
  triangles) needed about 3 s.
