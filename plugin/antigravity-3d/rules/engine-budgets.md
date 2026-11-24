---
trigger: glob
globs: "**/*.usd, **/*.usda, **/*.usdc, **/*.usdz, **/unreal_exports/**, **/unity_exports/**, **/godot_exports/**, **/roblox_exports/**"
description: Per-engine budgets and conventions for antigravity-3d stages and engine export packages.
---

# Engine budgets and export conventions

The stage is authored in metres, Z-up. `export_engine_package` converts per
engine; never pre-convert the stage. Per-asset defaults (studio budget,
`antigravity3d.state.EngineBudget`): 15,000 triangles, 4 draw calls,
1024 px/m texel density, UV stretch at most 0.05, 8 collision hulls, manifold
geometry. The session budget is set by `open_asset_session`; a project
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
| Headless import check | yes (engine-truth-verify) | yes | yes | none; validator only | yes, required: a failure blocks delivery; not run is reported as not run |

For a three.js (web) package the manifest also records web_download: the bytes a page
fetches for the highest level (the GLB and what it references, per file). LOD0
embeds its images; the `textures/` files serve the lower-detail levels.

Texture size is a parameter: `max_texture_px` (256, 512, 1024, 2048 or 4096) on
`open_asset_session`, in a script's `Asset(...)` line, or `--max-texture-px`,
caps the side of the texture atlas. With none given the atlas is as large as
the parts need: at most 4096 px, and at most 2048 px for a web package (four
4096 px maps are 341 MiB of graphics memory on a visitor's device; ask for 4096
only for a model the camera comes close to). Never scale the object to get
smaller textures. The delivery's answer for a web package states the atlas, the bytes a
page downloads, the texture memory and up to how many pixels on screen the
textures stay sharp; choose the size from the largest the model is shown at.
A web package's GLBs carry their maps as WebP (EXT_texture_webp) when the
three.js frames of the WebP GLB match the PNG GLB's within the installer's
compression tolerance; otherwise PNG. The manifest's web_textures record says
which, the bytes and the measured difference. The PNG maps stay in
`textures/`. Put a delivered package into a project with
`install_into_project`, never by copying its files.

LODs are not fixed triangle ratios. `generate_lods_and_colliders` emits a level
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
folders; re-export from the stage.
