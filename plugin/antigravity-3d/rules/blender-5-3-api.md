---
trigger: glob
globs: "**/*.py, **/*.blend"
description: Blender 5.3 Python API rules for antigravity-3d raw bpy scripts and add-on code.
---

# Blender 5.3 API rules

Apply these whenever you write or review Python that imports `bpy`, `bmesh`,
`gpu` or `mathutils`, including script text passed to the raw-script escape
hatch. The linter (`antigravity3d.adapters.blender_5_3.linter`) rejects every
name in the sections below that is marked with a code, so the PreToolUse hook
denies such a script. Each rule was checked
on Blender 5.3.0. The idea of the table comes from EL4CTEO/blender-skills (MIT,
`api-changes-5x.md`); the rules and messages are this project's own, and four
rows of that table do not hold in 5.3 (last section).

The worker starts from an empty scene: no default Cube, Light or Camera.
Create what the script needs.

## GPU

Call `gpu.init()` before any other `gpu` call (repeating it is safe): in the
background worker `gpu.texture.from_image` without it crashes Blender (AG023).
`bgl` (AG010) and `Image.bindcode` (AG014) are gone: `gpu.init()`, then
`gpu.texture.from_image(image)`.

## Geometry Nodes modifier inputs

`mod["Socket_1"]` raises since 5.2 (AG016). Use
`mod.properties.inputs.Socket_1.value`, or, to find the socket by name,
`getattr(mod.properties.inputs, socket.identifier).value` with `socket` an item
of `node_group.interface.items_tree` (the only dynamic `getattr` allowed, AG005;
the `Socket_N` number depends on creation order). The modifier feeds the object
into the first input, so the Geometry input must come first.

## Removed or renamed

| Old | Use instead | Code |
| --- | --- | --- |
| `render.engine = "BLENDER_EEVEE_NEXT"` | `"BLENDER_EEVEE"` | AG011 |
| Boolean `solver = "FAST"` | `"FLOAT"` (or `"EXACT"`) | AG012 |
| `bpy.types.AssetHandle` | `bpy.types.AssetRepresentation` | AG013 |
| `Mesh.use_auto_smooth`, `Mesh.auto_smooth_angle` | `bpy.ops.object.shade_smooth_by_angle` | AG015 |
| `Mesh.calc_normals_split`, `Mesh.face_maps` | `Mesh.corner_normals`; a boolean face attribute in `Mesh.attributes` | AG015 |
| `Material.shadow_method` | `Material.surface_render_method` | AG015 |
| `eevee.use_gtao`, `eevee.gtao_distance` | `view_layer.eevee.ambient_occlusion_distance` | AG015 |
| `Brush.sculpt_tool`; `Brush.use_airbrush`, `Brush.use_anchor`, `Brush.use_space` | `Brush.sculpt_brush_type`; `Brush.stroke_method` | AG015 |
| `tool_settings.unified_paint_settings` | `tool_settings.sculpt.unified_paint_settings` | AG015 |
| File Output `node.base_path`, `node.file_slots`, `node.layer_slots` | `node.directory`, `node.file_name`, `node.file_output_items` | AG015 |
| `Action.id_root` | `ActionSlot.target_id_type` | AG015 |
| `bpy.data.grease_pencils_v3`, `GreasePencilv3`, `GPencilStroke` | `bpy.data.grease_pencils`, `bpy.types.GreasePencil`, `bpy.types.AnnotationStroke` | AG015 |
| Principled `Subsurface`, `Specular`, `Transmission`, `Clearcoat`, `Clearcoat Roughness`, `Sheen`, `Emission` inputs | `Subsurface Weight`, `Specular IOR Level`, `Transmission Weight`, `Coat Weight`, `Coat Roughness`, `Sheen Weight`, `Emission Color` | AG017 |
| Compare / Random Value `A_INT`, `A_VEC3`, `B_COL`, `Min_001`... | one socket per name: `A`, `B`, `Min`, `Max` (set `node.data_type` first) | AG017 |
| Render Layers pass `Z`, `DiffCol`, `DiffDir`, `GlossCol`, `Emit`, `AO`, `IndexOB`... | `Depth`, `Diffuse Color`, `Diffuse Direct`, `Glossy Color`, `Emission`, `Ambient Occlusion`, `Object Index`... (the message names each) | AG018 |
| `sky_type = "NISHITA"`; `file_format = "AVI_RAW"` or `"AVI_JPEG"` | `"MULTIPLE_SCATTERING"` or `"SINGLE_SCATTERING"`; `media_type = "VIDEO"` then `"FFMPEG"` | AG019 |
| `CompositorNodeGamma`, `CompositorNodeMixRGB`, `CompositorNodeMath`, `ShaderNodeTexPointDensity` | `ShaderNodeGamma`, `ShaderNodeMix`, `ShaderNodeMath` in the compositor tree; the Point Density node is removed | AG020 |
| `wm.usd_export(export_textures=, visible_objects_only=)` | `wm.usd_export(export_textures_mode=, selected_objects_only=)` | AG021 |
| `export_scene.gltf(export_image_add_webp=, export_image_webp_fallback=, at_collection_center=)` | `export_scene.gltf(export_image_add_compressed_images=, export_compressed_images_type=, export_compressed_images_fallback=, collection_center_mode=)` | AG021 |
| `image_settings.file_format = "OPEN_EXR_MULTILAYER"` or `"FFMPEG"` first | set `image_settings.media_type` (`"MULTI_LAYER_IMAGE"`, `"VIDEO"`) before it | AG022 |
| `node_group.inputs.new()`, `node_group.outputs.new()` | `node_group.interface.new_socket(name=, in_out=, socket_type=)` | AG024 |
| `scene.node_tree` | `scene.compositing_node_group` | AG025 |
| `Action.fcurves`, `Action.groups` | the channelbag's `fcurves` and `groups` (slotted actions) | AG026 |

Still work in 5.3, so leave them: `Material.blend_method`, `Strip.frame_final_*`,
`bpy.ops.pose.convert_rotation`, `bpy.ops.anim.convert_legacy_action`.
`Material.use_nodes = False` does nothing; new materials and worlds always have
a node tree.

## Script hygiene

- Allowed imports only: `bpy`, `bmesh`, `mathutils`, `bpy_extras`, `gpu`,
  `gpu_extras`, `bl_math`, `idprop` and pure stdlib maths and data modules. No
  `os`, `sys`, `subprocess`, `socket`, `pathlib`, `pickle`, `importlib`.
- No `eval`, `exec`, `compile`, dynamic `__import__`, `getattr` on computed
  names (except the one above), or dunder access.
- No `bpy.ops.wm.save_*`, `open_mainfile`, `quit_blender`, `bpy.ops.script.*`,
  add-on installation, or `bpy.app.handlers`.
- Return data by assigning a JSON-serialisable value to `result`; never write
  files from a script. Persist through the USD stage via Scene IR.
- Prefer data API (`bpy.data`, `bmesh`) over `bpy.ops`; operators depend on
  UI context and are slow in a headless worker.
- Work in metres and Z-up; do not apply engine axis conversion in scripts.
