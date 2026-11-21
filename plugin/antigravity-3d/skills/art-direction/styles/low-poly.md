---
id: low-poly
title: Low-poly
pipeline:
  tri_budget_prop: 500
  tri_budget_kit_piece: 800
  shading_crease_deg: 0
  texture_px_prop: 512
  texture_px_kit: 1024
  texture_filter: null
  palette_max_colors: null
briefs:
  author: >-
    few large parts with a silhouette that reads from far away; no small parts, trims or fasteners (that detail
    belongs in the texture); low segment and ring counts on round parts; flat faceted surfaces.
  concept: >-
    low-poly game asset: large flat facets, simple chunky shapes, no fine detail
  texture: >-
    low-poly game texture: large simple colour areas, little fine detail, no photographic noise
---

# Low-poly

Readable silhouettes from a few large forms; every face earns its place. Surface detail is painted, never modelled.

## What the pipeline applies

| Value | Setting | Source |
|---|---|---|
| Triangle budget, prop | 500 tris | upstream low-poly skill, prop row (upper end) |
| Triangle budget, kit piece | 800 tris | upstream low-poly skill, environment module row (upper end) |
| Shading crease angle | 0 deg | upstream low-poly skill and its retro reference: flat shading |
| Texture size, prop | 512 px | upstream low-poly skill: default texture size |
| Texture size, kit trim sheet | 1024 px | upstream low-poly skill: largest size it allows (hero only) |
| Texture filtering | engine default | point filtering is an optional retro touch upstream; see `retro-lowres` |
| Albedo palette | no limit | |

A budget only ever lowers the engine's; the engine's and `--max-tris` still apply when they are lower.

## Building it

- Block the asset from the fewest Scene IR nodes that keep its silhouette. Merge what would read as one shape
  into one `box` or `cylinder` node.
- Round parts: keep `segments` and `rings` low; facets are part of the look, and the crease angle above makes
  every face shade flat.
- Hard edges, no rounded corners, no micro detail. If the silhouette does not read in the flat-colour preview,
  simplify the shape; do not add parts.
- One or two materials; colour variation comes from the albedo, not from extra materials.

## Not applied (needs a pipeline feature)

Vertex colours instead of textures, a single atlas shared by a whole scene, and snapping vertices to a coarse grid.
