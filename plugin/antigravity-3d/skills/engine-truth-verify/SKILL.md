---
name: engine-truth-verify
description: >-
  Exports a QA-passed asset to a target engine package and proves it imports
  correctly by running a headless engine import and a parity check against
  the USD stage. Use when the user asks to export, ship or deliver an asset to
  Unreal Engine 5, Unity URP, Godot 4 or Roblox, or to verify an existing
  export.
---

# Engine truth verify

Export is gated: `export_engine_package` is denied by the plugin's PreToolUse
hook unless the latest QA verdict has Tier-1 ok and Tier-1.5 at least 3.4 and
nothing changed since. Do not try to work around a denial; fix the asset and
re-run QA.

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
