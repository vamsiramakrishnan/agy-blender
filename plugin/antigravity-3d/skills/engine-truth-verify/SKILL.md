---
name: engine-truth-verify
description: >-
  Exports a QA-passed asset as an engine package for Unreal Engine 5, Unity URP,
  Godot 4 or Roblox (USD, USDZ, GLB with LODs, collision hulls, textures, FBX where
  the engine takes one, inspector, manifest.json) and proves it: a headless engine
  import, a parity check against the stage and an independent Blender check; on a
  key without the native judge it writes an unverified preview instead. Use when
  the user asks to export, ship, package or hand off an asset, to verify an
  existing export, or why an export was refused. Not for fixing the shape (use
  parametric-kitbash), for textures (use pbr-material-forge) or for cleaning a
  mesh file the user already has (use game-ready-sanitizer).
---

# Engine truth verify

Export is gated: `export_engine_package` is denied by the plugin's PreToolUse
hook unless the latest QA verdict has Tier-1 ok and Tier-1.5 at least 3.4 and
nothing changed since. Do not try to work around a denial; fix the asset and
re-run QA.

## Unverified previews

Without the native Decisions API (the key answers 404) no export is verified.
The user may still want an UNVERIFIED preview package. Ask the user for a
token ceiling and an image limit: they are the user's numbers, never yours.
Then call `start_preview` with them on the open session (the scene is kept and
earlier verdicts are discarded), or open the session with `open_asset_session`
and `preview`, `token_ceiling` and `max_images`. The plugin shows the user both
numbers and asks before the preview opens. In a preview session:

- `export_engine_package` needs Tier-1 ok, Tier-1.5 at least 3.4 and an
  approving lookdev visual QA verdict on the current scene, from whichever
  judge the key has. It runs the headless import when the engine is installed
  and refuses on a failed import; parity is not checked.
- The package's `manifest.json` says `verified: false`, `preview: true` and
  who judged it. Report it as `<asset> -> <engine>: PREVIEW (unverified)`,
  never as PASS, and say that a person must review it.

## Steps

1. **Confirm the gate.** Read `session_status`: it must show `tier1=ok`,
   `tier15` at least 3.4 and `export=allowed`. If not, stop and report the
   blocking reason. `usd_verifier` is not offered in the EXPORT stage.
2. **Export.** `export_engine_package` with an `out_name`; the target engine
   is the one given to `open_asset_session`. The exporter does the per-engine
   conversion (UE5: Z-up, DirectX normals, `UCX_` hulls; Unity / Godot /
   Roblox: Y-up; metres for all). Never convert the stage itself.
3. **Headless import.** `export_engine_package` runs the engine's headless
   import itself and refuses the package (`REFUSED ...`, nothing written) on
   a failure. The import metrics are recorded in the package's `manifest.json`
   under `validation.engine_import`. (Roblox has no headless importer, so only
   the validator runs.) Treat import warnings about scale, missing textures,
   collision or LOD groups as failures.
   When a real Blender worker is attached (the plugin's pool sidecar), the
   same export also runs EL4CTEO/blender-skills' qa_check script on the exported GLB
   with coincident vertices merged and the target engine's profile, and refuses
   the package (`REFUSED blender_asset_qa: ...`, nothing written) when that
   script reports an error. Its result, including its warnings, is recorded in
   `manifest.json` under `validation.blender_asset_qa`; without a real worker it
   is recorded as skipped with the reason, never as passed. A refusal here names
   what the script found: fix it on the stage and re-run QA.
   For Unreal Engine 5, Unity URP and Roblox the same export, with a real
   Blender worker, also writes an FBX into the package (`<asset>.fbx`, listed in
   `manifest.json` with its hash and described under `fbx`); Godot gets none.
   Unity: one mesh per LOD level named `<asset>_LOD0`, `_LOD1`, ... and no
   collision hulls. Unreal Engine: the LOD0 mesh and the `UCX_` hulls, with the
   DirectX normal map; its LOD levels are in the GLB and USD files, because
   Epic documents no LOD naming inside one FBX. Roblox: one mesh with its
   textures embedded. An FBX carries geometry, UVs and the base colour and
   normal links only: the roughness, metallic, occlusion and emission maps it
   lacks are listed in `fbx.maps.lost`; the GLB and USD are the full files. The
   file was re-imported in Blender and compared with the package's GLB files and
   its header was parsed; it was never opened in Unity, Unreal Engine or Roblox
   Studio, so say "re-imported in Blender and equal to the GLB", never that it
   "imports correctly" in an engine. The FBX is an extra output: if it cannot be
   made the answer says `FBX FAILED (not shipped; the package is intact)`, the
   manifest says `fbx.status: failed` with the reason, and the package is
   exported as usual; report that failure to the user plainly. Without a worker
   it is skipped with the reason.
4. **Parity check.** `export_engine_package` already compares the engine
   render with the stage render (silhouette IoU and a parity score) and refuses
   the package on a mismatch; the figures are in `validation.engine_parity` of
   `manifest.json`. Then compare the stage, from `inspect_manifest`, with the
   package's `manifest.json`: triangle counts per LOD (within 1%), bounding box
   in metres (within 0.5%), material slot count and order, texture resolutions
   and colour spaces, collider count. No tool renders the package for a
   separate visual score.
5. **Report.** One line: `<asset> -> <engine>: PASS|FAIL <first mismatch>`,
   plus the package path. On FAIL, name the stage-side fix (never patch the
   exported files by hand) and loop back through QA.
6. **Disclosure.** Packages of schema version 4 or later carry a `provenance`
   record in `manifest.json`: per shipped file, the sources it derives from and
   the deterministic steps between, and per source who made it (a model that
   authored the design, an image model, the user, an offline stand-in).
   `provenance.generated_content` is true when a model made part of what ships,
   false when none did and null when a source was not recorded;
   `provenance.models` names the models and `provenance.unrecorded` the sources
   nobody recorded. Quote it when the user asks what a store's AI-content
   disclosure should say, never answer "no generated content" from a null, and
   say that it is a record of identities and hashes, not a legal opinion.
   The exact shape is in `docs/package_provenance.md` of the repository.

## Check

The report line of step 5 is the check. A pass is `PASS` with the package path,
`validation.package_ok` true, `validation.engine_import`
recorded (not skipped) and `validation.engine_parity` within the figures of step
4. For a preview the only honest line is `PREVIEW (unverified)`.

## Gotchas

- **Symptom:** export is denied or says `REFUSED`. **Cause:** the gate is
  working (Tier-1 ok, Tier-1.5 at least 3.4, nothing changed since), or the
  import, parity or Blender check found something. **Remedy:** read the named
  reason, fix it on the stage and re-run QA; never patch the exported files and
  never look for a way round the hook.
- **Symptom:** the Blender check refuses a package for faces in one plane or
  for inconsistent face winding. **Cause:** flush faces of parts that press
  together (every detailed design recorded in round four failed on them), or a
  part that is truly wound the wrong way (parts merely touching at a corner no
  longer fail: the worker reads each part on its own). **Remedy:** the prop pipeline now moves
  flush faces 2 mm apart by itself; for anything it names, fix the part on the
  stage.
- **Symptom:** `validation.blender_asset_qa` says skipped. **Cause:** no real
  Blender worker was attached. **Remedy:** report it as skipped with its reason,
  never as passed.
- **Symptom:** the user opens the FBX in Unity or Unreal and a map is missing.
  **Cause:** an FBX carries geometry, UVs and the base colour and normal links
  only; the lost maps are listed in `fbx.maps.lost`. **Remedy:** point the user
  to the GLB or USD for the full material, and say "re-imported in Blender and
  equal to the GLB", never that it imports correctly in the engine.
- **Symptom:** a Unity build is much larger than the textures suggest.
  **Cause:** per the reference skill pack (glTFast 6.20.0 on Unity 6000.5.10f1,
  not tried by this project), textures embedded in a `.glb` ship uncompressed
  with mipmaps, while image files the importer finds in the project follow the
  Texture Importer. This package's `<asset>.glb` embeds every image, and the far
  LOD GLBs and the USD reference the files in `textures/`. **Remedy:** ask the
  user to check the build report, and to use the `textures/` files as project
  textures so that their compression settings apply; report what was and was
  not checked.
- **Symptom:** Roblox shows more MeshParts than expected. **Cause:** a glTF mesh
  becomes one MeshPart per primitive, and parts are submitted in batches split
  at the engine's triangle limit; the manifest's notes say how many to expect.
  **Remedy:** quote the note; the importer was not run in Studio.
- **Symptom:** the user has no verified package and the key answers 404 for the
  Decisions API. **Cause:** only the native judge verifies. **Remedy:** the
  preview path above, with the user's own token ceiling and image limit, and
  the report line `PREVIEW (unverified)` with who judged.
- **Symptom:** an `agy3d prop` run ended without a package after a busy
  service. **Cause:** a failure at one model or image call ended the run.
  **Remedy:** rerun with the same `--out`; the design and images
  already paid for are reused (see `parametric-kitbash`).

## Version notes

Last verified 2026-11-22 against commit 29660b8.

- Engines run for the import check in this project's CI: Blender 5.3.0 and
  Godot 4.7.2. Unity, Unreal Engine and Roblox Studio were never run.
- The Tier-1.5 threshold of 3.4 is `EXPORT_MIN_TIER15_SCORE` in `state.py`.
- The Unity texture fact is from the reference pack's own check, dated
  2026-10-24; re-read its glTF-textures reference before relying on it.
- Roblox has no headless importer here; only the validator runs.
- Schema version 4 added the `provenance` record; a package written before it has none.
