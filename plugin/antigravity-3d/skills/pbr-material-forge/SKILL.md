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
`run_paintover_loop` (fixes from an art direction), `run_visual_qa`,
`render_lookdev`.

## Steps

1. **Material zones.** `inspect_manifest` lists each node with its material
   id. The summary has no UV status, so check UV stretch with
   `generate_lods_and_colliders`, which is available only in the EXPORT stage.
   Every zone needs non-overlapping UVs (except deliberate mirrored trims) and
   a target texel density from the budget.
2. **Albedo Best-of-6.** `forge_pbr_material` with `candidates=6` per zone
   (the server may cap this to the session's plan). Its `prompt` describes
   the surface only: the material and its finish in a few words, for example
   "olive drab painted steel, scuffed and chipped". Name the substance, not
   what is made of it ("tarnished brass", not "brass hardware", which comes
   back with hinges drawn in). Never the object, its parts,
   shapes or layout: the image model draws what it reads, and the texture
   tiles over every face, so a latch or a panel in the prompt is painted onto
   all of them. The tool adds the art direction's texture brief itself and
   keeps the prompt as the material's `surface`. Candidates are scored
   server-side; do not open the images. The tool binds the best candidate that
   passes the policy gate and reports how many passed.
3. **De-light.** `forge_pbr_material` de-lights the albedo before it derives
   maps, so there is no separate step. The albedo must have no baked shadows,
   AO or highlights: luminance variance at a fixed albedo should come only from
   material, dirt and wear.
4. **Derive maps.** `forge_pbr_material` derives roughness, metallic, AO and
   normal from the de-lit albedo and packs ORM as R = AO, G = roughness,
   B = metallic. Normals follow the material's normal convention, OpenGL by
   default.
5. **Policy gate.** The forge runs the PBR policy; it must pass before
   binding: albedo sRGB in roughly 30-240, metallic effectively binary, ORM
   and normal maps linear, no NaN or empty channels, texture sizes within the
   engine budget. If every candidate fails, the tool returns `BLOCKED` with the
   reasons: change the prompt and call `forge_pbr_material` again. Never bind a
   material the gate rejected.
6. **Bind.** `forge_pbr_material` already binds the maps to its `material_id`.
   Use `apply_shader_diff` with `assign_to` to bind that material to the zone's
   node ids (the LOOKDEV header's `bound:` line lists them). Never edit material
   nodes with raw scripts.
7. **Look check.** `render_lookdev` (engine `eevee` or `cycles`), then
   `run_visual_qa`, which takes no arguments. Delegate critique to
   `lookdev_critic`; apply its two-line delta with `run_paintover_loop`
   (its `direction` argument takes the art direction) or a new
   `apply_shader_diff`. At most 4 rounds.
8. **Gate.** Hand to `usd_verifier` for Tier-1; it does not run the visual
   check. Lookdev is done when `session_status` shows `tier15` at least 3.4.
