---
id: isometric
title: Isometric
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
    seen from a high three-quarter view: strong top and side planes, clear height; no detail on undersides or on
    faces hidden from above; footprint aligned to a square ground grid.
  concept: >-
    isometric strategy-game asset: seen from a high three-quarter angle, strong top and side planes, diorama
    readability
  texture: >-
    texture readable from far above: large clear shapes, strong contrast between top and side surfaces, little fine
    detail
---

# Isometric

Readable from a fixed high three-quarter camera, sized to a ground grid.

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

- Decide the ground cell (for example one metre) and size the footprint to whole cells: one by one for props, two by
  two for landmarks, one by two for walls and roads.
- Strong top and side planes; the top must read against the ground.
- Spend parts on what the camera sees; nothing on undersides.

## Not applied (needs a pipeline feature)

The camera itself: the pipeline's previews and engine captures use their own views, not an isometric one.
