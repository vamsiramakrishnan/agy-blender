---
trigger: glob
globs: "**/*.usd, **/*.usda, **/*.usdc, **/*.usdz, **/unreal_exports/**, **/unity_exports/**, **/godot_exports/**, **/roblox_exports/**"
description: Per-engine budgets and conventions for antigravity-3d stages and engine export packages.
---

# Engine budgets and export conventions

The design is authored in metres, Z-up. `deliver_package` converts per
engine when it exports; never pre-rotate or pre-scale the script's geometry. Per-asset defaults (studio budget,
`antigravity3d.state.EngineBudget`): 15,000 triangles, 4 draw calls,
1024 px/m texel density, UV stretch at most 0.05, 8 collision hulls, manifold
geometry. The session budget is set by the script's `Asset(engine=...,
max_tris=...)` line; a project
`.agy3d/budget.json` is read only by the SessionStart hook, which reports it.
Entries marked "hint" are conservative studio hints, not documented engine
hard limits.

| | Unreal Engine 5 | Unity URP | Godot 4 | Roblox R15 | three.js (web) |
| --- | --- | --- | --- | --- | --- |
| Up axis at export | Z (native) | Y | Y | Y | Y (glTF rule) |
| Units at export | m (metersPerUnit 1.0; not scaled to cm) | m | m | m (1 stud is about 0.28 m, hint) | m (glTF rule) |
| Collision naming | `UCX_<Mesh>_NN` hulls | collider components, no named hulls | `-col` / `-convcol` / `-colonly` suffixes | engine-generated or simplified parts | `UCX_<asset>_NN` hulls in `<asset>_colliders.glb`; three.js reads none |
| Per-mesh triangle cap | none enforced; keep to budget | 65,535 vertices per 16-bit index mesh | none enforced; keep to budget | 10,000 tris per MeshPart | none enforced; keep to budget |
| Max texture side | 8192 (hint; larger needs VT) | 8192 (importer default, hint) | 16384 (GPU-dependent, hint) | 1024 (Creator Hub: "up to 1024x1024 pixel spaces for texture maps"; maps are generated at 1024) | 4096 (hint) |
| Textures per material | ORM + albedo + normal | ORM + albedo + normal (URP Lit) | ORM + albedo + normal | 4 SurfaceAppearance maps: Color, Normal, Metalness, Roughness; the package also writes separate metalness and roughness PNGs (manifest key roblox_surface_appearance) beside the packed ORM map | ORM + albedo + normal |
| Power-of-two textures | required (mip streaming) | recommended | recommended | recommended | not needed |
| Normal map convention | DirectX (green flipped at export) | OpenGL | OpenGL | OpenGL | OpenGL |
| Node and prim names | `[A-Za-z0-9_]+` | `[A-Za-z0-9_]+` (studio convention) | no `. : @ / " %` | `[A-Za-z0-9_]+` | no `. : / [ ]` or spaces (three.js renames such nodes) |
| Headless import check | yes (deliver_package imports it) | yes | yes | none; validator only | yes, required: a failure blocks delivery; not run is reported as not run |

For a three.js (web) package the manifest also records web_download: the bytes a page
fetches for the highest level (the GLB and what it references, per file). LOD0
embeds its images; the `textures/` files serve the lower-detail levels.

LODs are not fixed triangle ratios. `deliver_package` builds the levels and
the collision shapes: it emits a level
only where the asset can get at least 25% cheaper while staying within one
pixel of LOD0 (on a 1080 px frame) at the distance that level is shown; an
asset nothing can be removed from keeps LOD0 only. Far levels may leave out
whole parts that cannot be seen at that distance. Do not hand-decimate.

A package stores each map once in `textures/`. `<asset>.glb` (LOD0) embeds
its images and is the one GLB that may be handed over on its own.
`<asset>_LOD<n>.glb` (n >= 1) reference the maps as `textures/<file>`:
copy, move or import the package as a whole directory, never a far LOD file
alone. The package's `manifest.json` records, per GLB, which images are
embedded and which are referenced.

Before writing anything under an `*_exports/` folder, the QA gate must have
passed (Tier-1 ok, Tier-1.5 at least 3.4). Do not hand-edit files in export
folders; change the script, run_build and deliver_package again.
