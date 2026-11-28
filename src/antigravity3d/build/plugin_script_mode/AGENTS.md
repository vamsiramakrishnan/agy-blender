# antigravity-3d studio invariants

These rules are always on while the antigravity-3d plugin is enabled. Two
glob-scoped rule files add detail when matching files are in play:
`blender-5-3-api.md` (Python / Blender scripts) and `engine-budgets.md`
(USD stages and engine export folders).

## Source of truth and editing

- An asset's source is its build script: one Python file in the workspace
  (`<asset_name>.py`, written with `from agy3d import Asset`). The USD stage,
  the `.blend`, FBX, glTF and engine packages are derived from it; never
  hand-edit them and never treat them as canonical.
- Change an asset only by editing its script and running it with `run_build`.
  The skill `parametric-kitbash` is the script's reference: read it once before
  writing a script.
- A material's texture is generated from words that name its surface only:
  the material and its finish ("olive drab painted steel, scuffed and chipped"),
  never the object, its parts or layout, because the texture tiles over every
  face. Name the substance, not what is made of it ("tarnished brass", not
  "brass hardware"). That is a material's `surface`.
- A Blender snippet (`a.blender(id, code)`) is for shapes nothing else makes.
  It must pass the Blender 5.3 linter: no `os`, `subprocess`, file writes,
  `eval`/`exec` or `bpy.app.handlers`.

## A website is not an asset

A request for a website, web page, landing page or scroll-driven 3D explainer
(three.js, WebGL, shaders, "scrolls beautifully") goes to the skill
`scroll-3d-website` first, before any script: it says which props the story
needs, when to build them and how they reach the page. Each prop is then one
script, as below.

A request for slides, a deck, a talk, a pitch or a presentation that plays in
the browser, or for a story, a short film, an animated explainer or a title
sequence told scene by scene in three.js or WebGL (a fixed frame the viewer
steps through, not a page that scrolls) goes to the skill `story-slides-3d`
first, before any script: its slides are HTML over a stage of boxes and light,
and it says when one prop is worth building.

## How the tools reach you

- The studio tools are served by the plugin's MCP server (`studio`, which the
  harness namespaces `antigravity-3d_studio`). Two of them make an asset:
  `run_build(path)` runs the script, checks the design and shows it to you
  (findings and five colour views); `deliver_package(dest)` reviews, packages,
  exports and imports it into the engine.
- The first `run_build` of a conversation opens the asset's session from the
  script's `Asset(...)` line: pass `conversation_id` (the SessionStart header
  gives it) and put the user's request, in their words, in `brief`, which the
  review checks the asset against.
- The refusals are the gates working, not errors to route around.

## Units and axes

- Author in metres, Z-up, right-handed; the asset stands on z = 0, centred on
  the origin, its front facing -y.
- Axis conversion happens only at export, per engine (UE5 keeps Z-up; Unity,
  Godot and Roblox get Y-up). Units stay in metres for every engine. Never
  pre-rotate or pre-scale geometry to suit an engine.

## PBR

- Colours are sRGB as a colour picker shows them (`"#rrggbb"`). No pure black,
  no blown-out white.
- Metallic is 0 or 1 per material.
- Do not paint or generate texture files yourself: the package writer makes the
  maps (base colour, normal, occlusion-roughness-metallic) from each material's
  colour and `surface` words, or from an image model where the session's image
  limit allows it.

## Checks and delivery

- `run_build` checks every design (Tier-1: floating parts, open meshes, the
  triangle budget, flicker between flush faces). A design with a critical fault
  is not applied; the reply says what to change in the script.
- `deliver_package` refuses what the engine would reject, and says exactly what
  to fix. Its answer starts with `DELIVERED`, `NOT DELIVERED: a person must
  decide` (reply to the user with the reason and stop), `NOT DELIVERED` (fix
  the script, `run_build`, deliver again) or `STILL WORKING` (nothing failed:
  the work goes on; make the call the answer spells out, with the same
  arguments and the session it names, for the result; `run_build` and
  `install_into_project` answer so too when they take long).
- A verified export needs an approval from the native Decisions API. When the
  key has no access to it, `run_build` says so. Do not work around that. Ask
  the user whether they want an UNVERIFIED preview, and ask for their two
  limits: a token ceiling and an image limit. Never choose those numbers
  yourself. Then call `start_preview` with them (the design is kept). The user
  is shown both numbers and approves before the preview opens. A preview is
  judged by an uncalibrated judge and its package says `verified: false`; it is
  never verified and must not be presented as such. Its review is a list of
  findings for the user, not a gate.
- To change an asset that already exists, edit its script (the workspace's
  `<asset_name>.py`, or `source/build.py` in its package) and run it again;
  never write it anew.

## Engine budgets (summary)

Default studio budget per asset: at most 15,000 triangles, 4 draw calls,
1024 px/m texel density, UV stretch at most 0.05, 8 collision hulls,
manifold geometry. Per-engine limits are in `engine-budgets.md`. The session's
budget comes from the script's `Asset(engine=..., max_tris=...)`.

## Blender 5.3 (summary)

Use `BLENDER_EEVEE` (not `BLENDER_EEVEE_NEXT`), boolean solver `FLOAT` (not
`FAST`), the `gpu` module (no `bgl`), and `AssetRepresentation` (not
`AssetHandle`). Details in `blender-5-3-api.md`.
