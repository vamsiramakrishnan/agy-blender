---
trigger: glob
globs: "**/*.py, **/*.blend"
description: Blender 5.3 Python API rules for antigravity-3d raw bpy scripts and add-on code.
---

# Blender 5.3 API rules

Apply these whenever you write or review Python that imports `bpy`, `bmesh`,
`gpu` or `mathutils`, including script text passed to the raw-script escape
hatch. The 5.3 linter (`antigravity3d.adapters.blender_5_3.linter`) rejects
the removed symbols below, so code that ignores them is denied by the
PreToolUse hook.

| Removed or renamed in 5.x | Use instead | Linter code |
| --- | --- | --- |
| `scene.render.engine = "BLENDER_EEVEE_NEXT"` | `"BLENDER_EEVEE"` | AG011 |
| Boolean modifier `solver = "FAST"` | `solver = "FLOAT"` (or `"EXACT"`) | AG012 |
| `import bgl`, any `bgl.*` call | the `gpu` module, e.g. `gpu.texture.from_image(image)` | AG010 |
| `bpy.types.AssetHandle` | `bpy.types.AssetRepresentation` | AG013 |
| `Image.bindcode` | `gpu.texture.from_image(image)` | AG014 |

Script hygiene the linter also enforces:

- Allowed imports only: `bpy`, `bmesh`, `mathutils`, `bpy_extras`, `gpu`,
  `gpu_extras`, `bl_math`, `idprop` and pure stdlib maths and data modules. No
  `os`, `sys`, `subprocess`, `socket`, `pathlib`, `pickle`, `importlib`.
- No `eval`, `exec`, `compile`, dynamic `__import__`, `getattr` on
  computed names, or dunder access.
- No `bpy.ops.wm.save_*`, `open_mainfile`, `quit_blender`, `bpy.ops.script.*`,
  add-on installation, or `bpy.app.handlers`.
- Return data by assigning a JSON-serialisable value to `result`; never write
  files from a script. Persist through the USD stage via Scene IR.
- Prefer data API (`bpy.data`, `bmesh`) over `bpy.ops`; operators depend on
  UI context and are slow in a headless worker.
- Work in metres and Z-up; do not apply engine axis conversion in scripts.
