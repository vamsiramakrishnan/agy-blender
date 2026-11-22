---
name: art-direction
description: >-
  Picks and applies a named art direction (low-poly, retro-lowres, stylized,
  realistic, voxel, isometric, anime, hand-painted) to an asset: a lower triangle
  budget where the look needs one, the shipped maps' size, palette and filtering,
  the shading crease, and short briefs for the author, concept and texture models.
  Use when the user names a visual style, a game look or an era ("late-1990s console look",
  "chunky low poly", "cel shaded", "voxel"), before modelling starts. Not for
  building the model (use parametric-kitbash), for the materials themselves (use
  pbr-material-forge) or for exporting (use engine-truth-verify).
---

# Art direction

A style is a file in `styles/` next to this skill: its table says what the
pipeline applies, the rest how to build in that look. Read only the chosen
style's file.

## Choose

| Id | Pick when the user asks for |
|---|---|
| `low-poly` | few faceted triangles, simple chunky props |
| `retro-lowres` (alias `psx`) | late-1990s console look: tiny blocky textures, flat shading |
| `voxel` | block or cube-grid worlds |
| `realistic` | photoreal, real-world scale and materials |
| `stylized` | exaggerated shapes and a harmonious palette |
| `anime` | cel-shaded, flat colour regions |
| `isometric` | strategy or city-builder view from above |
| `hand-painted` | brushwork in the albedo; painted lighting is NOT delivered (the pipeline de-lights maps) |

Only `low-poly`, `retro-lowres`, `voxel` and `realistic` change shipped
settings; the other four change only what the models are asked for. If no
style fits, use none: never invent an id.

## Apply

1. Open the session with the style: `open_asset_session` with `art_direction`
   set to the id. An unknown id is refused with the list of valid ids.
   `session_status` then shows `art=<id>`, and `tris<=` is the style's budget
   when it is lower than the engine's.
2. BLOCKOUT and LOOKDEV receive the style's brief in their instructions. Build
   with `apply_scene_ir` following the style file's "Building it" list.
3. `forge_pbr_material` generates maps in the style's look, size and palette;
   `export_engine_package` writes its shading crease and texture filtering and
   records the style and the applied values in `manifest.json`.

From the command line the same is `agy3d prop "<prompt>" --art-direction <id>`
(also `agy3d kit`). It composes with `--style`, which is a reference image: the
image steers the look, the art direction sets the budget and the shipped
settings.

## Rules

- A style never raises a budget and never relaxes a gate: Tier-1, UV, the PBR
  policy and visual QA apply unchanged.
- The style is fixed for the session; changing it means a new session.
- Effects a style lists under "Not applied" (outlines, cel shading, fog, vertex
  colours) do not ship; say so instead of approximating them in geometry.

## Check

`session_status` shows `art=<id>` and, for a style with its own budget,
`tris<=` the style's figure. After export, `art_direction.id` in the package's
`manifest.json` names the style (`art_direction.effective` holds the values); a
pass is the id there equal to the one asked for.

## Gotchas

- **Symptom:** the user asked for an outline or cel-shaded look and the engine
  shows none. **Cause:** outlines, cel shading and fog are engine shader work
  that a glTF material cannot carry (each style file lists them under "Not
  applied"); the style only changes what the models are asked for. **Remedy:**
  say so, and do not approximate them in geometry.
- **Symptom:** a hand-painted asset has no painted shadows. **Cause:** the
  pipeline de-lights every generated albedo and asks for flat, shadowless
  lighting. **Remedy:** tell the user; the painted lighting would be removed.
- **Symptom:** the user wanted a high triangle count in a low-poly style and
  `tris<=` is lower. **Cause:** a style never raises a budget, it only lowers
  it. **Remedy:** quote the figure; for a higher budget open the session without
  that style.
- **Symptom:** an unknown style id is refused. **Cause:** only the eight ids
  above exist (the one alias is shown in the table). **Remedy:** read the list in
  the refusal; never invent an id.
- **Symptom:** the style does not change after switching. **Cause:** it is
  fixed for the session. **Remedy:** open a new session with the new id.

## Version notes

Last verified 2026-11-22 against commit 29660b8.

- Only low-poly, retro-lowres, voxel and realistic change shipped settings (for
  example retro-lowres: 400 triangles for a prop, 128 px maps, nearest
  filtering, 64 colours); the other four change the model briefs only.

Origin and licence of the source material: `art-direction.provenance.json` and
`UPSTREAM-LICENSE.txt`.
