---
name: parametric-kitbash
description: >-
  Builds the 3D model of a prop as one Python script (`from agy3d import Asset`):
  ready shapes with rounded edges and recessed panels, rods and rails along any
  path, skins through cross-sections, any triangle mesh, and whatever a Blender
  snippet builds; `run_build` runs the script and answers with the checks'
  findings and colour views, and `deliver_package` exports the engine package.
  Changes an asset that already exists ("make the two side handles 30 % larger")
  by editing its script, never by modelling it again. Use when the user asks to
  model, build, make or kitbash an asset, to change an existing one, or to fix
  its shape, parts or gaps, or to make a mesh file they already have
  game-ready. Not for putting a package into a game project (use
  install-into-project).
---

# Build an asset as a script

An asset is one Python file. Three steps, and nothing else is required:

1. **Write `<asset_name>.py`** in the workspace root, the whole asset in one file
   write. Keep the file: it is the asset's source, and a later change edits it.
2. **`run_build(path="<asset_name>.py", conversation_id=...)`** runs it, checks the
   design and shows it to you: findings that name the part and the fix, and five
   colour views. Look at every view, compare it with the request, edit the lines
   that are wrong (edit, do not rewrite the file) and run it again. Once or twice
   is normal.
3. **`deliver_package(dest="output")`** reviews it, builds levels of detail and
   collision shapes, textures it, validates, exports and imports it into the
   engine. `dest` is the folder the request names for the result. Its answer
   starts with `DELIVERED` (finished: reply to the user with the path and the
   findings, do not inspect or copy anything), `NOT DELIVERED: a person must
   decide` (reply with the reason and stop) or `NOT DELIVERED` (fix what it names
   in the script, `run_build`, deliver again).

Every model call sends the whole conversation again, so do not explore folders,
do not read other skills for a build, and do not run the script yourself: only
`run_build` checks it and shows it.

## A complete script

```python
from agy3d import Asset

a = Asset("wheelbarrow", engine="godot_4", max_tris=4000,
          brief="a weathered garden wheelbarrow with a steel tray and wooden handles")

green = a.material("enamel", "#4f6b4a", surface="green enamelled steel, chipped, rusty at the rim", metallic=1, roughness=0.55)
wood = a.material("ash", "#a8794a", surface="weathered ash wood, grey at the ends", roughness=0.7)
black = a.material("rubber", "#1c1c1c", surface="black rubber, dusty", roughness=0.9)

def rect(w, d, z, y=0.0):                       # a ring of points: one cross-section
    return [(-w / 2, y - d / 2, z), (w / 2, y - d / 2, z), (w / 2, y + d / 2, z), (-w / 2, y + d / 2, z)]

# tray: a skin out along the outside from the floor to the rim, then back down the inside: a hollow tray
tray = a.loft("tray", [rect(0.34, 0.50, 0.36), rect(0.60, 0.86, 0.60, y=0.04),
                       rect(0.57, 0.83, 0.60, y=0.04), rect(0.31, 0.47, 0.38)], material=green)

# wheel at the front (-y), axle along x
wheel = a.cylinder("wheel", radius=0.19, height=0.08, axis="x", bevel=0.025, at=(0, -0.50, 0.19), material=black)
a.cylinder("axle", radius=0.012, height=0.34, axis="x", at=wheel.center, material=green)

# one handle rail per side: from the axle, under the tray, back to the grip. mirror="x" makes the other side
rail = a.tube("rail", path=[(0.16, -0.50, 0.19), (0.15, -0.20, 0.343), (0.17, 0.25, 0.343), (0.27, 0.95, 0.52)],
              radius=0.017, sides=8, material=wood, mirror="x")
a.capsule("grip", radius=0.021, height=0.11, axis=(0.03, 0.50, 0.16), at=rail.along(0.97), material=black, mirror="x")

# a leg under each rail, tapered, ending on the ground; a brace between the two legs
foot = (0.23, 0.38, 0.0)
leg = a.tube("leg", path=[rail.along(0.62), foot], radius=[0.014, 0.011], sides=6, material=green, mirror="x")
a.cylinder("brace", radius=0.009, height=2 * leg.along(0.6)[0], axis="x",
           at=(0, leg.along(0.6)[1], leg.along(0.6)[2]), material=green)

a.save()
```

Units are metres, Z is up, the asset stands on z = 0 centred on the origin, its
front faces -y.

## Reference

`Asset(name, engine, max_tris=15000, brief="", art_direction="", max_texture_px=None)`:
`engine` is `unreal_5`, `unity_urp`, `godot_4`, `roblox_r15` or `web_three`
(three.js); `brief` is the user's request in their words (the review checks the
asset against it); `art_direction` is a named style, only when the user asks for
one (skill art-direction); `max_texture_px` (256, 512, 1024, 2048 or 4096) caps
the side of the texture atlas: the lever for a web page's download and texture
memory (never scale the object for it). `deliver_package` says for a web asset
up to how many pixels on screen its textures stay sharp.

`a.material(id, color, surface="", metallic=0.0, roughness=0.5)`: `color` is
`"#rrggbb"` as a colour picker shows it. `surface` names the substance and its
finish in a few words ("oiled oak, satin, worn at the edges"), never the object:
the texture is generated from these words and tiles over every face. Metallic is
0 or 1. Use at most three materials unless the request needs more.

Every shape takes an id (letters, digits, underscore; unique), `material=`, one
position, an orientation, `mirror=`, and returns the part:

| shape | makes |
|---|---|
| `a.box(id, size=(x, y, z), bevel=0, bevel_segments=1, inset_faces=(), inset_margin=0, inset_depth=0)` | a box; `bevel` cuts all edges back by that much (a chamfer; `bevel_segments=2` to 4 rounds them); a recessed panel on each listed face (`"+x"`, `"-y"`, ...) |
| `a.cylinder(id, radius, height, segments=24, bevel=0, bevel_segments=1)` | a cylinder along its axis; `bevel` chamfers both rims (`bevel_segments=2` to 4 rounds them) |
| `a.cone(id, radius, height, segments=24)` | base to tip along its axis |
| `a.sphere(id, radius, segments=24, rings=12)` | |
| `a.capsule(id, radius, height, segments=16)` | `height` is the straight part |
| `a.lathe(id, profile=[(r, z), ...], segments=24, closed=False)` | an outline turned about its axis, listed top to bottom: bottles, knobs, bells, wheels. Start and end on the axis (`r = 0`) for a solid; `closed=True` when the outline is a loop that never touches the axis (a ring, a tyre) |
| `a.extrude(id, outline=[(x, y), ...], depth)` | a flat outline (counter-clockwise) given thickness along its axis: brackets, plates, arches |
| `a.tube(id, path=[p0, p1, ...], radius, sides=10, closed=False, smooth=False)` | a rod along any 3D path: legs, rails, pipes, handles, hoops, cables. `radius` is a number, or a list spread evenly along the length (a taper, a turned leg). `closed=True` joins the ends (a ring, a chain link). `smooth=True` rounds the path through its points. Cost: `2 x sides` triangles per step of the path (`smooth` makes 8 steps between two points) |
| `a.loft(id, sections=[[p, ...], ...])` | a skin through cross-sections, each a ring of 3D points with the same count, in order; a section of one point is a tip: seats, hulls, blades, funnels, tapered or twisted bodies. List every section in the same direction round. For a hollow shape go up the outside and back down the inside, as the tray above does. A loft is not shaded round: a round rail is a `tube` |
| `a.mesh(id, vertices, faces)` | any mesh you compute (numpy arrays or lists; a `trimesh.Trimesh` works too); a face of more than three points must be convex |
| `a.blender(id, code)` | everything the mesh objects of a Blender Python snippet add up to, run in an empty scene with modifiers applied: booleans, subdivision, bevel modifiers, curves, text. Hidden objects (a boolean's cutter) are left out |
| `a.load(id, path)` | a mesh file that already exists (glb, gltf, obj, ply, stl) |

Position, one of: `at=(x, y, z)` (where the shape's own origin goes: the centre of
a box, cylinder, cone, sphere or capsule, the zero point of a lathe's profile or
an extrusion's outline; for `tube`, `loft`, `mesh`, `blender` and `load` an
offset added to their own coordinates), `bottom=(x, y, z)` (where the middle of
its lowest face goes) or `top=(x, y, z)`.

Orientation, one of: `axis="x" | "y" | "z"` or a vector (where the shape's own
length points: a cylinder's height, a lathe's axis, an extrusion's depth; an
extrusion's outline then stands upright, its y pointing up) or
`rot=(rx, ry, rz)` in degrees.

`mirror="x" | "y" | "z"` also makes the reflected copy on the other side of the
origin, named `<id>__mx` (`__my`, `__mz` on the other axes; `p.mirrored` is that
copy). The part must lie on one side of that plane.

`grain="x" | "y" | "z"` or a vector: the way the wood runs in a part (default: its
longest side; a tube's grain follows its path).

A part knows where it is: `p.min`, `p.max`, `p.center`, `p.size` (its bounding
box), `p.top`, `p.bottom` (heights), and for a tube `p.along(t)`, the point at
fraction `t` of its path. Take a joint's coordinates from the parts it joins
(`bottom=(0, 0, body.top)`, `path=[rail.along(0.5), foot]`) instead of working
them out again.

`a.save()` is the last line. The script may import `math`, `random`, `numpy`,
`scipy`, `trimesh` and `agy3d`; it cannot read or write files itself.

## What the checks expect

- **Every part touches another part or the ground** (within 2 mm). A part that
  hangs free is refused at delivery; the finding names it, the nearest part and
  the move in millimetres. End a rod inside the part it joins.
- **Inside the triangle budget.** `run_build` reports the count. Lower `sides`
  and `segments` on small parts before removing parts.
- **A closed mesh** for `mesh`, `loft` and `blender` parts: no holes, faces
  outward.

## What makes it a finished prop, not a blockout

- Every part the request names is there, built from the pieces a real one has: a
  latch is a plate, a hook and a lever, not one box.
- The part count is in the tens and the design uses most of the triangle budget
  where it shows: silhouette first, then what catches the light.
- Nothing manufactured has a razor edge: bevel boxes and cylinders by 3 to 5 %
  of their smallest size (`bevel_segments=2` where a large rounded edge shows).
  A panel is an inset face, not a thin box stacked on.
- Round, bent and tapered things are made round, bent and tapered: `lathe`,
  `tube` and `loft`, not stacks of boxes.
- Colours as the real materials look in daylight; say wear and finish in
  `surface`.

## Changing an asset that already exists

A request about an asset that is already made is an edit of its script, not a
new model. Find the script (`<asset_name>.py` in the workspace, or
`source/build.py` in its package folder), change only the lines of the parts the
request names, keep every other line as it is, then `run_build`, check the views
and `deliver_package(dest=...)` to the same folder.

## A mesh file that already exists

"Make this OBJ game-ready" is a script of three lines: `Asset(...)`,
`a.load("model", "path/to/file.obj", material=...)` (glb, gltf, obj, ply or stl,
in metres, Z up) and `a.save()`. `run_build` checks it, and `deliver_package`
unwraps it, builds the levels of detail and collision shapes and exports. A mesh
the check calls open or inside out is repaired first on the command line
(`agy3d sanitize FILE --engine ENGINE --out OUT`) and the repaired file loaded.

## Previews without the calibrated judge

When `run_build` says the key has no calibrated judge, only an UNVERIFIED preview
can be delivered: ask the user whether they want one and for their two limits (a
token ceiling and an image limit), never choose them, and call `start_preview`.
With an image limit of 0 surfaces are generated from the geometry and each
material's `surface` words; do not paint textures yourself.
