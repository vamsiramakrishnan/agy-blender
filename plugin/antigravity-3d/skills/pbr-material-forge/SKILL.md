---
name: pbr-material-forge
description: >-
  Creates game-ready PBR materials for an asset: Best-of-6 albedo generation,
  de-lighting, derivation of ORM and normal maps, and a PBR policy gate before
  the material is bound in the USD stage. Use when the user asks for
  materials, textures, lookdev or surfacing on an asset that already has a
  blockout or final mesh with UVs.
---

# PBR material forge

Tools: `forge_pbr_material` (generation, de-light, derivation, packing),
`apply_shader_diff` (binds and edits materials in the stage),
`run_paintover_loop` (targeted fixes), `run_visual_qa`, `render_preview`.

## Steps

1. **Material zones.** `inspect_manifest` to list material slots and UV
   status. Every zone needs non-overlapping UVs (except deliberate mirrored
   trims) and a target texel density from the budget.
2. **Albedo Best-of-6.** `forge_pbr_material` with `candidates=6` per zone
   from the zone's material description and the style anchor. Candidates are
   scored server-side; do not open the images. Keep the top candidate and the
   runner-up.
3. **De-light.** Run the de-light step on the winner. The result must have no
   baked shadows, AO or highlights: luminance variance at a fixed albedo
   should come only from material, dirt and wear.
4. **Derive maps.** Derive roughness, metallic, AO and normal from the
   de-lit albedo and the mesh bake. Pack ORM as R = AO, G = roughness,
   B = metallic. Normals in OpenGL convention.
5. **Policy gate.** The forge runs the PBR policy; it must pass before
   binding: albedo sRGB in roughly 30-240, metallic effectively binary, ORM
   and normal maps linear, no NaN or empty channels, texture sizes within the
   engine budget. If it fails, fix the reported channel (re-run that
   derivation) instead of overriding the gate.
6. **Bind.** `apply_shader_diff` to bind the packed maps to the zone's USD
   material. Never edit material nodes with raw scripts.
7. **Look check.** `render_preview` under the studio neutral HDRI, then
   `run_visual_qa` with rubric `lookdev`. Delegate critique to
   `lookdev_critic`; apply its two-line delta with `run_paintover_loop`
   (region-limited) or a new `apply_shader_diff`. At most 4 rounds.
8. **Gate.** Hand to `usd_verifier` for Tier-1 and Tier-1.5. Lookdev is done
   when Tier-1.5 is at least 3.4.
