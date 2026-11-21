---
id: voxel
title: Voxel
pipeline:
  tri_budget_prop: null
  tri_budget_kit_piece: null
  shading_crease_deg: 0
  texture_px_prop: null
  texture_px_kit: null
  texture_filter: nearest
  palette_max_colors: 32
briefs:
  author: >-
    build only from axis-aligned box nodes on one cubic grid: every size and position a whole multiple of one cell
    size, right-angle rotations only, no round parts; stepped forms instead of curves; every part at least one cell
    thick.
  concept: >-
    voxel style: built entirely from equal cubes on a grid, stepped edges, no curves
  texture: >-
    voxel block texture: flat colour blocks, few colours, crisp pixel edges, no gradients
---

# Voxel

Block-grid language: forms built from equal cells, silhouettes made of steps, a small palette.

## What the pipeline applies

| Value | Setting | Source |
|---|---|---|
| Triangle budget, prop | engine budget | no number upstream |
| Triangle budget, kit piece | engine budget | no number upstream |
| Shading crease angle | 0 deg | upstream voxel skill: no smooth shading that hides block edges |
| Texture size, prop | pipeline default | no number upstream |
| Texture size, kit trim sheet | pipeline default | no number upstream |
| Texture filtering | nearest | upstream voxel skill: nearest-neighbour textures |
| Albedo palette | 32 colours | upstream voxel grid reference (upper end) |

## Building it

- Choose one cell size for the asset (state it in your plan) and use only `box` nodes whose sizes and positions are
  whole multiples of it, with right-angle rotations.
- Curves become stairs of cells; a limb or rail is at least one cell thick.
- Keep the silhouette readable from all four side views.
- Merge runs of cells into one larger box node where they share a material: fewer nodes, same look.

## Not applied (needs a pipeline feature)

A grid check on the Scene IR (sizes and positions on the cell grid) and a voxel-to-mesh merge that drops faces
hidden between touching cells. Until then the brief steers the author and Tier-1 does not check the grid.
