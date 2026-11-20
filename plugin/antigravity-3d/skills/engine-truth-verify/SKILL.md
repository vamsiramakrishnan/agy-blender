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

1. **Confirm the gate.** Delegate to `usd_verifier` and read its verdict line.
   If it is not `PASS`, stop and report the blocking reason.
2. **Export.** `export_engine_package` with the target engine. The exporter
   does the per-engine conversion (UE5: Z-up, centimetres, DirectX normals,
   `UCX_` hulls; Unity / Godot / Roblox: Y-up metres). Never convert the stage
   itself.
3. **Headless import.** The export result includes the engine-side import
   report from the headless validator (UE5, Unity and Godot; Roblox has no
   headless importer, so only the validator runs). Treat import warnings
   about scale, missing textures, collision or LOD groups as failures.
4. **Parity check.** Compare stage and engine side with `inspect_manifest` on
   the exported package: triangle counts per LOD (within 1%), bounding box
   in engine units (within 0.5%), material slot count and order, texture
   resolutions and colour spaces, collider count. Render a turntable thumbnail
   with `render_preview` on both sides and score parity with `run_visual_qa`
   rubric `engine_parity` (pass at 4.0).
5. **Report.** One line: `<asset> -> <engine>: PASS|FAIL <first mismatch>`,
   plus the package path. On FAIL, name the stage-side fix (never patch the
   exported files by hand) and loop back through QA.
