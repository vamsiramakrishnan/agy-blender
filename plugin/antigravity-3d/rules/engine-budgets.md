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
geometry. A project `.agy3d/budget.json` may tighten these per asset class.
Entries marked "hint" are conservative studio hints, not documented engine
hard limits.

| | Unreal Engine 5 | Unity URP | Godot 4 | Roblox R15 |
| --- | --- | --- | --- | --- |
| Up axis at export | Z (native) | Y | Y | Y |
| Units at export | cm (metersPerUnit 0.01) | m | m | m (1 stud is about 0.28 m, hint) |
| Collision naming | `UCX_<Mesh>_NN` hulls | collider components, no named hulls | `-col` / `-convcol` / `-colonly` suffixes | engine-generated or simplified parts |
| Per-mesh triangle cap | none enforced; keep to budget | 65,535 vertices per 16-bit index mesh | none enforced; keep to budget | 10,000 tris per MeshPart |
| Max texture side | 8192 (hint; larger needs VT) | 8192 (importer default, hint) | 16384 (GPU-dependent, hint) | 1024 (larger is downscaled) |
| Textures per material | ORM + albedo + normal | ORM + albedo + normal (URP Lit) | ORM + albedo + normal | 4 SurfaceAppearance maps: Color, Normal, Metalness, Roughness |
| Power-of-two textures | required (mip streaming) | recommended | recommended | recommended |
| Normal map convention | DirectX (green flipped at export) | OpenGL | OpenGL | OpenGL |
| Node and prim names | `[A-Za-z0-9_]+` | `[A-Za-z0-9_]+` (studio convention) | no `. : @ / " %` | `[A-Za-z0-9_]+` |
| Headless import check | yes (engine-truth-verify) | yes | yes | none; validator only |

LODs: LOD1 about 50%, LOD2 about 25%, LOD3 about 12% of LOD0 triangles unless
the budget file says otherwise; preserve silhouette and UV seams.

Before writing anything under an `*_exports/` folder, the QA gate must have
passed (Tier-1 ok, Tier-1.5 at least 3.4). Do not hand-edit files in export
folders; re-export from the stage.
