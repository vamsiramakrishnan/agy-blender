---
id: hand-painted
title: Hand-painted
pipeline:
  tri_budget_prop: null
  tri_budget_kit_piece: null
  shading_crease_deg: null
  texture_px_prop: null
  texture_px_kit: null
  texture_filter: null
  palette_max_colors: null
briefs:
  author: >-
    simple, slightly chunky forms with soft proportions; detail is painted in the texture, not modelled.
  concept: >-
    hand-painted game art: painterly brushwork, soft value transitions, warm and cool colour play
  texture: >-
    hand-painted albedo: visible brushwork, soft painted colour variation, warm and cool hues, even roughness, no
    photographic noise
---

# Hand-painted

Brushwork in the albedo; geometry supports the paint and carries no micro detail.

What this pack delivers: painterly colour and brushwork in the albedo, simple soft forms, even roughness. What it
does not deliver: light and shadow painted into the albedo, the defining property of the look. The pipeline
de-lights every generated albedo and asks for flat lighting, and this style does not switch that off. Choose it
knowing the result is painterly colour under the engine's own lighting.

## What the pipeline applies

| Value | Setting | Source |
|---|---|---|
| Triangle budget, prop | engine budget | no number upstream |
| Triangle budget, kit piece | engine budget | no number upstream |
| Shading crease angle | project default | no number upstream |
| Texture size, prop | pipeline default | no number upstream |
| Texture size, kit trim sheet | pipeline default | no number upstream |
| Texture filtering | engine default | |
| Albedo palette | no limit | |

This style changes only what the models are asked for; the shipped settings are the pipeline's own.

## Building it

- Simple forms with soft proportions; the paint does the work.
- Few materials; atlas-friendly for kits.
- Keep roughness even (`roughness` via `apply_shader_diff`); shine only on wet or metal accents.

## Not applied (needs a pipeline feature)

Light and shadow painted into the albedo: the pipeline de-lights every generated albedo and asks the image model for
flat, shadowless lighting, so painted lighting would be removed. Keeping it needs a style-aware switch for the
de-light step and the albedo prompt.
