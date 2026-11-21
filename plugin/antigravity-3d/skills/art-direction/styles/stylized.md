---
id: stylized
title: Stylized
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
    exaggerated proportions and a bold silhouette from clear shape language (round reads friendly, boxy reads
    sturdy, pointed reads dangerous); chunky, slightly oversized parts; no small detail in geometry.
  concept: >-
    stylized game art: exaggerated proportions, bold readable shapes, a limited harmonious palette
  texture: >-
    stylized painted texture: a few harmonious colours, soft colour variation, simple suggestive detail, no
    photographic noise
---

# Stylized

Deliberate shape language, exaggeration and colour harmony; detail is suggested in the texture, not modelled.

## What the pipeline applies

| Value | Setting | Source |
|---|---|---|
| Triangle budget, prop | engine budget | upstream: similar to standard budgets |
| Triangle budget, kit piece | engine budget | as above |
| Shading crease angle | project default | no number upstream |
| Texture size, prop | pipeline default | no number upstream |
| Texture size, kit trim sheet | pipeline default | no number upstream |
| Texture filtering | engine default | |
| Albedo palette | no limit | upstream limits hues (a few core colours), not texture colours |

This style changes only what the models are asked for; the shipped settings are the pipeline's own.

## Building it

- Pick one shape family per asset (round, boxy or pointed) and keep it consistent.
- Push proportions on purpose: thicker legs, bigger handles, oversized doors and windows.
- Keep the colour plan small: one dominant, one secondary, one accent; saturate the parts a player interacts with.

## Not applied (needs a pipeline feature)

Toon shading with stepped light bands, an outline pass, and rim light: these live in the engine's shaders, and a
glTF material cannot carry them.
