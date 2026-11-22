---
id: anime
title: Anime
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
    a clean, iconic silhouette from a few simple masses; simplified secondary forms; no noisy small parts; put edges
    where one colour region meets another.
  concept: >-
    anime cel-shaded look: clean outlines, flat colour regions, hard-edged shadow shapes
  texture: >-
    cel-shaded anime texture: flat colour regions with hard edges, very few tones per material, no noise, no
    gradients
---

# Anime

Hard colour breaks, clean silhouettes and simplified forms.

## What the pipeline applies

| Value | Setting | Source |
|---|---|---|
| Triangle budget, prop | engine budget | no number upstream |
| Triangle budget, kit piece | engine budget | no number upstream |
| Shading crease angle | project default | no number upstream |
| Texture size, prop | pipeline default | no number upstream |
| Texture size, kit trim sheet | pipeline default | no number upstream |
| Texture filtering | engine default | |
| Albedo palette | no limit | upstream's few value steps are shading bands, not albedo colours |

This style changes only what the models are asked for; the shipped settings are the pipeline's own.

## Building it

- Simple masses first; leave small secondary forms out rather than modelling them small.
- Split a part into separate nodes where its colour changes, so each colour region gets its own material.
- Avoid noisy roughness: keep `roughness` even across a material with `apply_shader_diff`.

## Not applied (needs a pipeline feature)

Cel shading (a few hard light bands) and outlines are engine shader work; a glTF material cannot carry them.
