---
name: pbr-material-forge
description: >-
  Creates the surface of an asset: generates albedo candidates with an image
  model from words that name the material, removes baked lighting, derives
  roughness, metallic, occlusion and normal maps, packs ORM, checks the PBR
  policy and binds the material, then checks the look in a lookdev render. Use
  when the user asks for materials, textures, PBR, lookdev or surfacing ("make it
  look like rusted steel"), or reports seams, baked shadows or maps that look
  wrong. Needs a model with UVs. Not for modelling or reshaping (use
  parametric-kitbash), for choosing an overall style (use art-direction) or for
  export (use engine-truth-verify).
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

## Check before handing over

1. `session_status` shows `tier15` at least 3.4 after `run_visual_qa`.
2. `inspect_manifest` (LOOKDEV header, `bound:` line) lists a material for every
   zone that should have one.
3. The forge's last answer names no material as left untextured or `BLOCKED`.

A pass is all three. A zone left flat on purpose (the image limit was used up)
is reported to the user by name, not counted as a pass.

## Gotchas

- **Symptom:** every texture shows a picture of the object (panels with latches
  and handles drawn in), and a latch appears on every face. **Cause:** the
  prompt carried the asset brief, so the image model drew the object, and the
  texture tiles over every face (round three: nine images, all unusable).
  **Remedy:** the `prompt` of `forge_pbr_material` names the surface only, in a
  few words ("olive drab painted steel, scuffed and chipped").
- **Symptom:** hinges or rivets appear in a brass texture. **Cause:** the
  prompt named a kind of object ("tarnished brass hardware"), and the image
  model drew it. **Remedy:** name the substance, not what is made of it
  ("tarnished brass").
- **Symptom:** the judge reports baked lighting and visible seams and scores the
  look low (1.2 of 4 in round three). **Cause:** the surface was drawn as an
  object, with lighting in it (above). **Remedy:** a surface prompt; the forge
  de-lights every albedo, so do not ask for painted light or shadow, and a
  hand-painted art direction does not keep painted lighting either.
- **Symptom:** a material comes back untextured. **Cause:** an image request
  that returns no image (asked once more automatically since round four), or the
  run's image limit was used up: materials are textured largest surface first and
  the rest are named as left untextured. **Remedy:** call `forge_pbr_material`
  again for that material if the user has images left; otherwise report it.
- **Symptom:** a texture ranking takes minutes. **Cause:** the fallback judge
  (used on a key without the native Decisions API) took 30 s, 176 s and more
  than 240 s for the same ranking on one afternoon. **Remedy:** wait; do not
  call the forge again for the same material while one is running.
- **Symptom:** the look is scored, yet the package says unverified. **Cause:**
  on a key whose Decisions API answers 404 only the fallback judge is
  available. It is uncalibrated: it may steer a preview session, but
  `verified` stays false. **Remedy:** say so in the hand-off and see the
  preview notes in `engine-truth-verify`.

## Version notes

Last verified 2026-11-22 against commit 29660b8.

- The Tier-1.5 threshold of 3.4 is `EXPORT_MIN_TIER15_SCORE` in `state.py`.
- ORM packing is R = AO, G = roughness, B = metallic; the default normal
  convention is OpenGL, and the exporter converts for Unreal Engine.
- Best-of-6 is the plan's figure; the server may lower it to what the session's
  limits allow.
