---
id: retro-lowres
title: Retro low-res
pipeline:
  tri_budget_prop: 400
  tri_budget_kit_piece: 2000
  shading_crease_deg: 0
  texture_px_prop: 128
  texture_px_kit: 512
  texture_filter: nearest
  palette_max_colors: 64
briefs:
  author: >-
    chunky low-poly forms from very few parts; strong simple silhouette; no fine detail; low segment counts on
    round parts.
  concept: >-
    late-1990s console look: chunky faceted low-poly forms, small blocky textures
  texture: >-
    low-resolution retro game texture: chunky readable detail, few colours, flat colour areas, no photographic detail
---

# Retro low-res

The look of late-1990s console games: few triangles, flat-shaded faces, small blocky textures sampled without smoothing
and a limited palette. The mood (bright, grim, cosy) comes from the user's prompt, not from this style.

## What the pipeline applies

| Value | Setting | Source |
|---|---|---|
| Triangle budget, prop | 400 tris | upstream retro horror budgets, prop row (upper end) |
| Triangle budget, kit piece | 2000 tris | upstream retro horror budgets, room chunk row (upper end) |
| Shading crease angle | 0 deg | upstream retro horror skill: flat shading |
| Texture size, prop | 128 px | upstream retro horror budgets, prop row (upper end) |
| Texture size, kit trim sheet | 512 px | upstream retro horror budgets, room chunk atlas (upper end) |
| Texture filtering | nearest | upstream retro horror skill: nearest filtering |
| Albedo palette | 64 colours | upstream low-poly retro reference: limited palette (upper end) |

Nearest filtering is written as a glTF sampler on every texture of every GLB. The palette limit applies to each
material's albedo map separately, not to the whole scene. Each material's map is one continuous swatch, so the small
maps need no per-part layout; that would change if maps ever carried per-part content.

## Building it

- Very few Scene IR nodes; chunky proportions; faces and small parts are left out, not modelled small.
- Low `segments` on round parts; the crease angle above shades every face flat.
- Kit pieces: modular and repetitive by design.
- Emissive screens or lamps are fine as props: set `emissive_color` with `apply_shader_diff`.

## Not applied (needs a pipeline feature)

Affine texture warping and vertex jitter (engine shader), distance fog, the lighting and staging of horror scenes and
the analogue-video post effects (engine or compositor), dithering in gradients.
