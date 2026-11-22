<!-- before: commit 5a440fa80d, 2026-11-20T22:06:53Z, Intel(R) Xeon(R) Processor @ 2.10GHz, 4 logical CPUs, harness wall 1106.7 s -->
<!-- same code as before: commit 5a440fa80d, 2026-11-20T22:56:19Z, Intel(R) Xeon(R) Processor @ 2.10GHz, 4 logical CPUs, harness wall 1079.6 s -->
<!-- after: commit 5e1e8f92e1, 2026-11-20T22:44:10Z, Intel(R) Xeon(R) Processor @ 2.10GHz, 4 logical CPUs, harness wall 715.3 s -->
| Benchmark | Case | Before (median) | After (median) | Change | Noise band |
|---|---|---|---|---|---|
| `kernel.edit.patch` | nodes=5, tris=376, materials=3 | 0.508 ms | 0.536 ms | within noise | 26% |
| `kernel.edit.patch` | nodes=20, tris=1876, materials=3 | 0.881 ms | 0.883 ms | within noise | 17% |
| `kernel.edit.patch` | nodes=50, tris=4876, materials=3 | 1.3 ms | 1.3 ms | within noise | 25% |
| `kernel.edit.patch` | nodes=200, tris=19876, materials=3 | 4.9 ms | 5.2 ms | within noise | 19% |
| `kernel.edit.patch_qa` | nodes=5, tris=376, materials=3 | 2.6 ms | 2.6 ms | within noise | 10% |
| `kernel.edit.patch_qa` | nodes=20, tris=1876, materials=3 | 7.0 ms | 7.1 ms | within noise | 30% |
| `kernel.edit.patch_qa` | nodes=50, tris=4876, materials=3 | 15.5 ms | 15.8 ms | within noise | 12% |
| `kernel.edit.patch_qa` | nodes=200, tris=19876, materials=3 | 60.7 ms | 66.2 ms | within noise | 19% |
| `harness.edit.apply_scene_ir` | nodes=5, tris=376, materials=3 | 5.1 ms | 5.9 ms | 1.2x slower | 15% |
| `harness.edit.apply_scene_ir` | nodes=20, tris=1876, materials=3 | 11.0 ms | 12.8 ms | within noise | 22% |
| `harness.edit.apply_scene_ir` | nodes=50, tris=4876, materials=3 | 20.5 ms | 23.9 ms | within noise | 62% |
| `harness.edit.apply_scene_ir` | nodes=200, tris=19876, materials=3 | 67.0 ms | 112 ms | 1.7x slower | 52% |
| `harness.edit.rejected_rollback` | nodes=5, tris=376, materials=3 | 4.9 ms | 6.7 ms | 1.4x slower | 15% |
| `harness.edit.rejected_rollback` | nodes=20, tris=1876, materials=3 | 9.8 ms | 9.8 ms | within noise | 13% |
| `harness.edit.rejected_rollback` | nodes=50, tris=4876, materials=3 | 19.3 ms | 19.9 ms | within noise | 11% |
| `harness.edit.rejected_rollback` | nodes=200, tris=19876, materials=3 | 73.5 ms | 78.0 ms | within noise | 21% |
| `kernel.rollback.raw` | nodes=5, tris=376, materials=3, discard=True, include_next_read=False | 0.100 ms | 0.104 ms | within noise | 15% |
| `kernel.rollback.raw` | nodes=5, tris=376, materials=3, discard=False, include_next_read=False | 0.302 ms | 0.306 ms | within noise | 21% |
| `kernel.rollback.raw` | nodes=5, tris=376, materials=3, discard=True, include_next_read=True | 0.779 ms | 0.753 ms | within noise | 13% |
| `kernel.rollback.raw` | nodes=20, tris=1876, materials=3, discard=True, include_next_read=False | 0.108 ms | 0.130 ms | 1.2x slower | 11% |
| `kernel.rollback.raw` | nodes=20, tris=1876, materials=3, discard=False, include_next_read=False | 0.413 ms | 0.435 ms | within noise | 14% |
| `kernel.rollback.raw` | nodes=20, tris=1876, materials=3, discard=True, include_next_read=True | 1.8 ms | 1.8 ms | within noise | 12% |
| `kernel.rollback.raw` | nodes=50, tris=4876, materials=3, discard=True, include_next_read=False | 0.150 ms | 0.158 ms | within noise | 11% |
| `kernel.rollback.raw` | nodes=50, tris=4876, materials=3, discard=False, include_next_read=False | 0.661 ms | 0.645 ms | within noise | 15% |
| `kernel.rollback.raw` | nodes=50, tris=4876, materials=3, discard=True, include_next_read=True | 4.0 ms | 3.8 ms | within noise | 29% |
| `kernel.rollback.raw` | nodes=200, tris=19876, materials=3, discard=True, include_next_read=False | 0.195 ms | 0.198 ms | within noise | 15% |
| `kernel.rollback.raw` | nodes=200, tris=19876, materials=3, discard=False, include_next_read=False | 1.8 ms | 2.0 ms | within noise | 15% |
| `kernel.rollback.raw` | nodes=200, tris=19876, materials=3, discard=True, include_next_read=True | 16.5 ms | 15.7 ms | within noise | 16% |
| `harness.edit.undo` | nodes=5, tris=376, materials=3 | 6.0 ms | 6.5 ms | within noise | 13% |
| `harness.edit.undo` | nodes=20, tris=1876, materials=3 | 11.2 ms | 13.1 ms | within noise | 24% |
| `harness.edit.undo` | nodes=50, tris=4876, materials=3 | 21.2 ms | 24.1 ms | within noise | 24% |
| `harness.edit.undo` | nodes=200, tris=19876, materials=3 | 67.5 ms | 76.8 ms | 1.1x slower | 11% |
| `harness.edit.to_pixels` | nodes=5, tris=376, materials=3, preview_px_per_view=256, wireframe=True | 41.0 ms | 45.4 ms | within noise | 14% |
| `harness.edit.to_pixels` | nodes=20, tris=1876, materials=3, preview_px_per_view=256, wireframe=True | 60.4 ms | 69.4 ms | 1.1x slower | 13% |
| `harness.edit.to_pixels` | nodes=50, tris=4876, materials=3, preview_px_per_view=256, wireframe=True | 107 ms | 107 ms | within noise | 23% |
| `harness.edit.to_pixels` | nodes=200, tris=19876, materials=3, preview_px_per_view=256, wireframe=True | 372 ms | 326 ms | within noise | 27% |
| `harness.qa.visual_overhead` | nodes=5, tris=376, materials=3 | 6.2 ms | 6.0 ms | within noise | 32% |
| `harness.qa.visual_overhead` | nodes=20, tris=1876, materials=3 | 5.9 ms | 5.5 ms | within noise | 38% |
| `harness.qa.visual_overhead` | nodes=50, tris=4876, materials=3 | 6.0 ms | 5.0 ms | within noise | 30% |
| `harness.qa.visual_overhead` | nodes=200, tris=19876, materials=3 | 4.8 ms | 6.8 ms | 1.4x slower | 18% |
| `kernel.raster.contact_sheet` | tris=960, meshes=12, px_per_view=256, views=4, wireframe=True | 58.6 ms | 51.0 ms | within noise | 25% |
| `kernel.raster.contact_sheet` | tris=960, meshes=12, px_per_view=512, views=4, wireframe=True | 203 ms | 153 ms | 1.3x faster | 21% |
| `kernel.raster.contact_sheet` | tris=5280, meshes=12, px_per_view=256, views=4, wireframe=True | 111 ms | 91.0 ms | 1.2x faster | 10% |
| `kernel.raster.contact_sheet` | tris=5280, meshes=12, px_per_view=512, views=4, wireframe=True | 293 ms | 232 ms | 1.3x faster | 10% |
| `kernel.raster.contact_sheet` | tris=20160, meshes=12, px_per_view=256, views=4, wireframe=True | 303 ms | 289 ms | within noise | 15% |
| `kernel.raster.contact_sheet` | tris=20160, meshes=12, px_per_view=512, views=4, wireframe=True | 491 ms | 437 ms | within noise | 32% |
| `kernel.som.marked_sheet` | tris=960, meshes=12, px_per_view=256, views=4 | 131 ms | 147 ms | within noise | 16% |
| `kernel.som.marked_sheet` | tris=5280, meshes=12, px_per_view=256, views=4 | 169 ms | 144 ms | within noise | 20% |
| `kernel.som.marked_sheet` | tris=20160, meshes=12, px_per_view=256, views=4 | 212 ms | 195 ms | within noise | 13% |
| `kernel.uv.unwrap` | mesh=sphere_500, tris=448, verts=226, texel_density_px_per_m=1024.0, resolution=2048 | 262 ms | 33.6 ms | 7.8x faster | 12% |
| `kernel.uv.unwrap` | mesh=sphere_2000, tris=1920, verts=962, texel_density_px_per_m=1024.0, resolution=2048 | 285 ms | 49.9 ms | 5.7x faster | 18% |
| `kernel.uv.unwrap` | mesh=sphere_10000, tris=10080, verts=5042, texel_density_px_per_m=1024.0, resolution=2048 | 562 ms | 274 ms | 2.1x faster | 22% |
| `kernel.uv.unwrap` | mesh=sphere_50000, tris=49920, verts=24962, texel_density_px_per_m=1024.0, resolution=2048 | 2248 ms | 2348 ms | within noise | 20% |
| `kernel.uv.unwrap` | mesh=u_shape_concave, tris=1964, verts=984, texel_density_px_per_m=1024.0, resolution=2048 | 70.1 ms | 14.3 ms | 4.9x faster | 23% |
| `kernel.lod.generate` | mesh=sphere_500, tris=448, verts=226, with_uvs=True | 13.8 ms | 10.9 ms | 1.3x faster | 11% |
| `kernel.lod.generate` | mesh=sphere_500, tris=448, verts=226, with_uvs=False | 9.5 ms | 9.5 ms | within noise | 12% |
| `kernel.lod.generate` | mesh=sphere_2000, tris=1920, verts=962, with_uvs=True | 54.9 ms | 36.9 ms | 1.5x faster | 16% |
| `kernel.lod.generate` | mesh=sphere_2000, tris=1920, verts=962, with_uvs=False | 36.1 ms | 30.3 ms | 1.2x faster | 11% |
| `kernel.lod.generate` | mesh=sphere_10000, tris=10080, verts=5042, with_uvs=True | 224 ms | 141 ms | 1.6x faster | 15% |
| `kernel.lod.generate` | mesh=sphere_10000, tris=10080, verts=5042, with_uvs=False | 159 ms | 88.9 ms | 1.8x faster | 11% |
| `kernel.lod.generate` | mesh=sphere_50000, tris=49920, verts=24962, with_uvs=True | 918 ms | 722 ms | 1.3x faster | 20% |
| `kernel.lod.generate` | mesh=sphere_50000, tris=49920, verts=24962, with_uvs=False | 890 ms | 510 ms | 1.7x faster | 30% |
| `kernel.lod.generate` | mesh=u_shape_concave, tris=1964, verts=984, with_uvs=True | 8.8 ms | 9.8 ms | within noise | 22% |
| `kernel.lod.generate` | mesh=u_shape_concave, tris=1964, verts=984, with_uvs=False | 6.5 ms | 7.0 ms | within noise | 12% |
| `kernel.collider.convex` | mesh=sphere_500, tris=448, verts=226 | 1.7 ms | 1.7 ms | within noise | 13% |
| `kernel.collider.convex` | mesh=sphere_2000, tris=1920, verts=962 | 6.2 ms | 5.0 ms | 1.2x faster | 12% |
| `kernel.collider.convex` | mesh=sphere_10000, tris=10080, verts=5042 | 40.9 ms | 40.8 ms | within noise | 11% |
| `kernel.collider.convex` | mesh=sphere_50000, tris=49920, verts=24962 | 281 ms | 237 ms | 1.2x faster | 15% |
| `kernel.collider.concave` | mesh=u_shape_concave, tris=1964, verts=984 | 438 ms | 148 ms | 3.0x faster | 13% |
| `kernel.collider.concave` | mesh=three_bodies, tris=1940, verts=976 | 838 ms | 260 ms | 3.2x faster | 16% |
| `kernel.lod.assembly` | mesh=assembly_30_on_1, parts=31, tris=5028 | 54.5 s | 546 ms | 99.9x faster | 13% |
| `kernel.collider.imperfect` | mesh=two_cubes_sharing_an_edge, tris=24, verts=14 | 6443 ms | 2.9 ms | 2204.0x faster | 13% |
| `kernel.collider.imperfect` | mesh=sphere_six_faces_missing, tris=394, verts=202 | 98.2 s | 5.3 ms | 18469.0x faster | 13% |
| `kernel.collider.imperfect` | mesh=open_cup, tris=72, verts=50 | 37.2 s | 2499 ms | 14.9x faster | 18% |
| `kernel.export.pipeline` | subject=sphere_500, mesh=sphere_500, tris=448, verts=226, parts=1 | 254 ms | 47.5 ms | 5.3x faster | 15% |
| `kernel.export.pipeline` | subject=sphere_2000, mesh=sphere_2000, tris=1920, verts=962, parts=1 | 353 ms | 100 ms | 3.5x faster | 12% |
| `kernel.export.pipeline` | subject=sphere_10000, mesh=sphere_10000, tris=10080, verts=5042, parts=1 | 821 ms | 444 ms | 1.9x faster | 12% |
| `kernel.export.pipeline` | subject=sphere_50000, mesh=sphere_50000, tris=49920, verts=24962, parts=1 | 3593 ms | 3310 ms | within noise | 14% |
| `kernel.export.pipeline` | subject=u_shape_concave, mesh=u_shape_concave, tris=1964, verts=984, parts=1 | 558 ms | 156 ms | 3.6x faster | 11% |
| `kernel.export.pipeline` | subject=crate (TemplateAuthor), tris=144, parts=12, materials=2 | 120 ms | 86.8 ms | 1.4x faster | 15% |
| `kernel.export.pipeline` | subject=barrel (TemplateAuthor), tris=320, parts=4, materials=2 | 79.9 ms | 76.1 ms | within noise | 11% |
| `kernel.qa.tier1` | subject=crate, tris=144, parts=12 | 2.0 ms | 1.8 ms | within noise | 10% |
| `kernel.qa.tier1` | subject=barrel, tris=320, parts=4 | 5.8 ms | 4.6 ms | within noise | 28% |
| `kernel.validate.usd` | subject=crate, tris=144, check=validate_usd(stage) | 2.3 ms | 2.1 ms | within noise | 33% |
| `kernel.validate.usd` | subject=crate, tris=144, check=check_engine_compliance(stage) | 0.707 ms | 0.797 ms | within noise | 13% |
| `kernel.validate.usd` | subject=crate, tris=144, check=validate_usd(path) incl. open | 2.8 ms | 2.4 ms | 1.1x faster | 13% |
| `kernel.validate.usd` | subject=barrel, tris=320, check=validate_usd(stage) | 1.1 ms | 1.2 ms | within noise | 33% |
| `kernel.validate.usd` | subject=barrel, tris=320, check=check_engine_compliance(stage) | 0.554 ms | 0.614 ms | within noise | 21% |
| `kernel.validate.usd` | subject=barrel, tris=320, check=validate_usd(path) incl. open | 1.7 ms | 1.7 ms | within noise | 11% |
| `kernel.package.write` | subject=crate, tris=144, textured=False, texture_px=None, texture_files=0 | 75.0 ms | 75.6 ms | within noise | 15% |
| `kernel.package.write` | subject=crate, tris=144, textured=True, texture_px=256, texture_files=6 | 129 ms | 134 ms | within noise | 14% |
| `kernel.package.write` | subject=barrel, tris=320, textured=False, texture_px=None, texture_files=0 | 50.5 ms | 52.4 ms | within noise | 12% |
| `kernel.package.write` | subject=barrel, tris=320, textured=True, texture_px=256, texture_files=6 | 95.4 ms | 103 ms | within noise | 17% |
| `kernel.package.write_large_textures` | subject=crate, tris=144, texture_px=1024, texture_files=6 | 806 ms | 787 ms | within noise | 15% |
| `kernel.package.validate_cold` | subject=crate, tris=144, files=15, package_bytes=5385624 | 18.9 ms | 18.0 ms | within noise | 13% |
| `kernel.package.validate_cold` | subject=barrel, tris=320, files=14, package_bytes=4636761 | 14.6 ms | 15.5 ms | within noise | 13% |
| `kernel.settle` | movable_props=10, static_meshes=1, tris=924 | 7.2 ms | 7.2 ms | within noise | 15% |
| `kernel.spatial.build` | boxes=10000, constructor=SpatialIndex(meshes) | 73.4 ms | 76.7 ms | within noise | 12% |
| `kernel.spatial.build` | boxes=10000, constructor=from_bounds | 52.7 ms | 51.5 ms | within noise | 12% |
| `kernel.spatial.build` | boxes=200, constructor=stage_first_query (StageManager.query_region after an edit) | 15.7 ms | 15.7 ms | within noise | 13% |
| `kernel.spatial.query_region` | boxes=10000, region=small, region_half_width_m=3.0 | 1.4 ms | 1.4 ms | within noise | 13% |
| `kernel.spatial.query_region` | boxes=10000, region=medium, region_half_width_m=22.5 | 8.6 ms | 9.3 ms | within noise | 14% |
| `kernel.spatial.query_region` | boxes=10000, region=whole, region_half_width_m=150.0 | 5.1 ms | 5.0 ms | within noise | 36% |
| `kernel.spatial.query_region` | boxes=200, region=6 m square, StageManager.query_region, index cached | 0.226 ms | 0.205 ms | within noise | 28% |
| `kernel.cache.hit` | subject=crate, tris=144, parts=12 | 9.0 ms | 9.1 ms | within noise | 12% |
| `kernel.cache.hit` | subject=barrel, tris=320, parts=4 | 3.0 ms | 3.3 ms | within noise | 13% |
| `kernel.cache.miss` | subject=crate, tris=144, parts=12 | 132 ms | 107 ms | 1.2x faster | 13% |
| `kernel.cache.miss` | subject=barrel, tris=320, parts=4 | 92.7 ms | 89.3 ms | within noise | 14% |
| `ipc.blender_pool.fake` | op=ping, workers=1, fake=True | 0.076 ms | 0.103 ms | 1.4x slower | 21% |
| `ipc.blender_pool.fake` | op=render_1024x1024x4_f32, payload_bytes=16777216, workers=1, fake=True | 30.2 ms | 32.2 ms | within noise | 15% |
| `mcp.roundtrip.apply_scene_ir` | nodes=5, tris=376, materials=3, transport=mcp stdio, offline server | 7.0 ms | 7.1 ms | within noise | 12% |
| `mcp.roundtrip.apply_scene_ir` | nodes=50, tris=4876, materials=3, transport=mcp stdio, offline server | 22.4 ms | 23.2 ms | within noise | 14% |
| `pipeline.prop_to_engine.offline` | prompt=wooden crate with iron brackets, mode=governed, cache=cold, engine=unreal_5, repeats=3 | 2920 ms | 2022 ms | 1.4x faster | 12% |
| `pipeline.prop_to_engine.offline` | prompt=wooden crate with iron brackets, mode=governed, cache=warm, engine=unreal_5, repeats=3 | 2543 ms | 1824 ms | 1.4x faster | 13% |
| `pipeline.prop_to_engine.offline` | prompt=wooden crate with iron brackets, mode=single_candidate, cache=cold, engine=unreal_5, repeats=3 | 2706 ms | 1939 ms | 1.4x faster | 13% |
| `pipeline.prop_to_engine.offline` | prompt=wooden crate with iron brackets, mode=single_candidate, cache=warm, engine=unreal_5, repeats=3 | 2571 ms | 1526 ms | 1.7x faster | 20% |
| `pipeline.prop_to_engine.offline` | prompt=rusty metal barrel, mode=governed, cache=cold, engine=unreal_5, repeats=3 | 3255 ms | 2279 ms | 1.4x faster | 15% |
| `pipeline.prop_to_engine.offline` | prompt=rusty metal barrel, mode=governed, cache=warm, engine=unreal_5, repeats=3 | 2782 ms | 1880 ms | 1.5x faster | 15% |
| `pipeline.prop_to_engine.offline` | prompt=rusty metal barrel, mode=single_candidate, cache=cold, engine=unreal_5, repeats=3 | 2872 ms | 1841 ms | 1.6x faster | 15% |
| `pipeline.prop_to_engine.offline` | prompt=rusty metal barrel, mode=single_candidate, cache=warm, engine=unreal_5, repeats=3 | 2642 ms | 1756 ms | 1.5x faster | 13% |
| `pipeline.prop_to_engine.offline` | prompt=oak dining table, mode=governed, cache=cold, engine=unreal_5, repeats=3 | 2450 ms | 1535 ms | 1.6x faster | 22% |
| `pipeline.prop_to_engine.offline` | prompt=oak dining table, mode=governed, cache=warm, engine=unreal_5, repeats=3 | 2093 ms | 1294 ms | 1.6x faster | 16% |
| `pipeline.prop_to_engine.offline` | prompt=oak dining table, mode=single_candidate, cache=cold, engine=unreal_5, repeats=3 | 2151 ms | 1359 ms | 1.6x faster | 12% |
| `pipeline.prop_to_engine.offline` | prompt=oak dining table, mode=single_candidate, cache=warm, engine=unreal_5, repeats=3 | 1876 ms | 1158 ms | 1.6x faster | 14% |
| `pipeline.prop_to_engine.offline` | prompt=iron street lamp post, mode=governed, cache=cold, engine=unreal_5, repeats=3 | 2712 ms | 1998 ms | 1.4x faster | 11% |
| `pipeline.prop_to_engine.offline` | prompt=iron street lamp post, mode=governed, cache=warm, engine=unreal_5, repeats=3 | 2446 ms | 1638 ms | 1.5x faster | 11% |
| `pipeline.prop_to_engine.offline` | prompt=iron street lamp post, mode=single_candidate, cache=cold, engine=unreal_5, repeats=3 | 2556 ms | 1797 ms | 1.4x faster | 17% |
| `pipeline.prop_to_engine.offline` | prompt=iron street lamp post, mode=single_candidate, cache=warm, engine=unreal_5, repeats=3 | 2364 ms | 1496 ms | 1.6x faster | 20% |
| `pipeline.prop_to_engine.offline` | prompt=wooden storage shelf, mode=governed, cache=cold, engine=unreal_5, repeats=3 | 2605 ms | 1627 ms | 1.6x faster | 24% |
| `pipeline.prop_to_engine.offline` | prompt=wooden storage shelf, mode=governed, cache=warm, engine=unreal_5, repeats=3 | 2215 ms | 1368 ms | 1.6x faster | 19% |
| `pipeline.prop_to_engine.offline` | prompt=wooden storage shelf, mode=single_candidate, cache=cold, engine=unreal_5, repeats=3 | 2307 ms | 1488 ms | 1.5x faster | 16% |
| `pipeline.prop_to_engine.offline` | prompt=wooden storage shelf, mode=single_candidate, cache=warm, engine=unreal_5, repeats=3 | 2071 ms | 1277 ms | 1.6x faster | 19% |
| `pipeline.prop.replayed_images` | prompt=wooden crate with iron brackets, mode=governed, engine=unreal_5, repeats=3 | 2752 ms | 1939 ms | 1.4x faster | 11% |
| `pipeline.prop.replayed_images` | prompt=wooden crate with iron brackets, mode=single_candidate, engine=unreal_5, repeats=3 | 2680 ms | 1703 ms | 1.6x faster | 15% |
| `pipeline.prop.replayed_images` | prompt=rusty metal barrel, mode=governed, engine=unreal_5, repeats=3 | 2979 ms | 2131 ms | 1.4x faster | 17% |
| `pipeline.prop.replayed_images` | prompt=rusty metal barrel, mode=single_candidate, engine=unreal_5, repeats=3 | 2818 ms | 1772 ms | 1.6x faster | 15% |
| `pipeline.prop.replayed_images` | prompt=oak dining table, mode=governed, engine=unreal_5, repeats=3 | 2176 ms | 1420 ms | 1.5x faster | 16% |
| `pipeline.prop.replayed_images` | prompt=oak dining table, mode=single_candidate, engine=unreal_5, repeats=3 | 1973 ms | 1301 ms | 1.5x faster | 12% |
| `pipeline.prop.replayed_images` | prompt=iron street lamp post, mode=governed, engine=unreal_5, repeats=3 | 2513 ms | 1900 ms | 1.3x faster | 10% |
| `pipeline.prop.replayed_images` | prompt=iron street lamp post, mode=single_candidate, engine=unreal_5, repeats=3 | 2569 ms | 1584 ms | 1.6x faster | 19% |
| `pipeline.prop.replayed_images` | prompt=wooden storage shelf, mode=governed, engine=unreal_5, repeats=3 | 2292 ms | 1454 ms | 1.6x faster | 16% |
| `pipeline.prop.replayed_images` | prompt=wooden storage shelf, mode=single_candidate, engine=unreal_5, repeats=3 | 2164 ms | 1400 ms | 1.5x faster | 15% |
| `pipeline.prop_to_engine.node_classification` | mode=governed, cache=cold, runs=15 | 2712 ms | 1998 ms | 1.4x faster | 11% |
| `pipeline.prop_to_engine.node_classification` | mode=single_candidate, cache=cold, runs=15 | 2556 ms | 1792 ms | 1.4x faster | 17% |
| `cli.prop.offline` | prompt=wooden crate with iron brackets, engine=unreal_5, runs=3 | 5215 ms | 4686 ms | within noise | 18% |
| `cli.models` | runs=5 | 654 ms | 654 ms | within noise | 18% |
| `import.usd_stage` | module=antigravity3d.kernel.usd_stage, runs=5 | 327 ms | 320 ms | within noise | 15% |
| `throughput.prop_offline` | jobs_K=12, worker_processes_P=1, start_method=spawn, threads_per_process=1 | 34.5 s | 24.7 s | 1.4x faster | 15% |
| `throughput.prop_offline` | jobs_K=12, worker_processes_P=2, start_method=spawn, threads_per_process=1 | 20.6 s | 15.2 s | 1.4x faster | 12% |
| `throughput.prop_offline` | jobs_K=12, worker_processes_P=4, start_method=spawn, threads_per_process=1 | 18.3 s | 12.4 s | 1.5x faster | 40% |
| `dag.batch_sanitize` | meshes=24, jobs=1, engine=unreal_5, max_tris=15000 | 37.1 s | 29.9 s | 1.2x faster | 13% |
| `dag.batch_sanitize` | meshes=24, jobs=2, engine=unreal_5, max_tris=15000 | 18.9 s | 15.5 s | 1.2x faster | 12% |
| `dag.batch_sanitize` | meshes=24, jobs=4, engine=unreal_5, max_tris=15000 | 14.6 s | 11.5 s | 1.3x faster | 24% |
| `web.verify` | subject=crate, tris=144, texture_px=0, texture_files=0 | 1416 ms | 736 ms | 1.9x faster | 21% |
| `web.verify` | subject=crate, tris=144, texture_px=1024, texture_files=6 | 18.4 s | 1463 ms | 12.6x faster | 20% |
