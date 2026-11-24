---
name: install-into-project
description: >-
  Installs a finished antigravity-3d package into the user's own game project
  as one scene to drag into a level (Godot 4 automatically: textures once with
  VRAM compression and mipmaps, levels of detail switched by distance, one
  collision body) and checks it there with the project's own engine; installs
  into a three.js or React Three Fiber project too (files where the dev server
  serves them, a loader and a component to import, checked in a browser with
  the project's own three.js); also removes an installed asset. Use when the user asks to put, add, install or
  drop an exported asset into their game, project or this workspace, says the
  import is a mess (duplicate textures, wrong LODs, many collision bodies), or
  wants an installed asset removed. Not for exporting or verifying a package on
  its own (use engine-truth-verify), for materials (use pbr-material-forge) or
  for cleaning a mesh file (use game-ready-sanitizer).
---

# Install into project

A package (the folder `export_engine_package` writes, or `agy3d prop --out`)
is files. This skill puts it into the game project so that dragging one scene
into a level is the whole job, and proves it there.

## Steps

1. **Find the package.** It is the directory with `manifest.json` that
   `export_engine_package` returned, or that the user names. `inspect_manifest`
   on the session shows the asset; the package's own `manifest.json` names the
   engine it was built for.
2. **Which case.** A project you are building in this session because the
   user asked for it (a website, a prototype): install directly, step 3 with
   `dry_run: false`; `deliver_package` ends its answer with that exact call.
   Never copy a package's files into a project by hand: in run 1 of the
   website task the agent did, and its page got no levels of detail and no
   compression. A project the user already had: dry run first.
   **Dry run.** Call `install_into_project` with `package_dir` and
   `dry_run: true` (the default); `install_into_project` takes `project_dir`
   from this workspace when it is not given. It detects the engine and version
   from the project's own files and refuses when they do not match the engine
   the package was built for (re-export for the project's engine) or when it
   cannot tell which engine the project is (ask the user for the project's
   root directory). Show the user the plan:
   the destination (default `agy3d/<asset>` inside the project) and the files
   it would create, replace or remove, and the warnings (an engine version that
   differs from the one the package was checked against).
3. **Install** (for a project the user already had, once they approve the
   plan). Call with `dry_run: false`.
   The plugin asks the user before anything is written, in both cases: for a
   project you are building the user approves the install itself instead of
   a plan. Pass `force: true`
   only when the user has said to replace files of an earlier install that
   they edited; the refusal names those files.
4. **Read the checks.** Godot: the project is imported headlessly in an
   isolated copy (the project's settings, none of its scripts) with the
   project's Godot binary, the installed scene is loaded, and each check is
   `passed`, `failed` or `skipped` with its reason: `import` (no error or
   warning naming the asset), `scale` (bounds equal the manifest's), `levels`
   (each level present, its triangle count and switch distance), `materials`
   (every map bound to the installed texture, compressed with mipmaps),
   `collision` (one StaticBody3D, the package's hulls within Jolt's 256-point
   limit) and "level_switching" (rendered at each switch distance, needs a
   display or xvfb-run). A skipped check is not a pass: say which and why.
5. **Report.** One line: `<asset> -> <project> (<engine> <version>): INSTALLED
   at <dest>, checks <n> passed / <n> failed / <n> skipped`, the scene to drag
   (`<dest>/<asset>.tscn`), the screenshot path when there is one, and every
   warning. For Unity, Unreal Engine and Roblox projects the tool writes
   nothing and says which file to import: pass that on and say plainly that
   automatic installation for that engine is not built yet.

## three.js and React Three Fiber

A project with `three` in its package.json takes a package built for the web
(`agy3d prop --engine web_three`; a script asset says `engine="web_three"` in its
`Asset(...)` line). The tool writes the asset's files to the served folder
(`public/agy3d/<asset>/`, or `static/`; the result names the rule that chose it),
a loader to `src/agy3d/<asset>/` (`index.js`, `index.d.ts`, plus `react.js` with
@react-three/fiber) and the record beside the loader, never in the served
folder. The developer's one step: `scene.add(await load<Asset>({ renderer }))`
imported from `./agy3d/<asset>`, or `<Asset />` in a Canvas. The result is a Group
holding a THREE.LOD that follows the live camera, with `userData.agy3d`: the
bounds, the collision hulls as plain arrays (for Rapier or cannon-es) and a
function that frees what the loader made.

A web package's GLB already carries its maps as WebP where that does not
change the picture (the manifest's web_textures record says which, with the bytes
and the measured difference); the install serves those, and keeps the PNG maps
out of the served folder. Compression (KTX2 textures, meshopt geometry) uses
packages the project already has and encodes from the package's PNG maps, never
from the WebP copies; without the packages the install ships the web GLB and the
loader with every level of detail all the same, and prints the one command
(`npm install -D @gltf-transform/core @gltf-transform/extensions
@gltf-transform/functions ktx2-encoder meshoptimizer sharp`). Nothing compressed
is written without a measurement: each encoding is rendered in the headless
browser against the package's own frame, and the first one inside the tolerance
is written. With `--no-check`, with no browser, or when no encoding is inside the
tolerance, the textures stay as the package has them (WebP when its GLB
carries WebP, else PNG) and the result says which. `--compress auto`
(default) then writes compressed files only if the page downloads no more than
before, `always` takes the GPU memory whatever the size, `never` skips it. For
procedurally painted textures PNG is already small: the gain is GPU memory
(computed from the format, not observed here), not download; say so.

Checks, in a headless browser with the project's own three.js: import, loader,
scene, scale, levels, materials, outline, collision (every hull point against the
package's colliders file), level_switching, shared_textures, loader_vs_package
(compression when compressed) and react_component. No browser or no
`node_modules/three`: skipped, never a pass.

## Removing it

`agy3d uninstall --project DIR --dest DIR_IN_PROJECT` removes exactly the
files the install wrote (its record is `<dest>/.agy3d/install.json`) and keeps
any file the user added there. It refuses when one of its files was edited
since, unless the user asks for `--force`.

## Check

The report line of step 5 is the check. A pass is `INSTALLED` with every
check `passed` and none `failed`; a `skipped` check (no Godot binary, no
display, a package with one level or no hulls) is listed with its reason and
is never counted as a pass. `agy3d install` run again on an unchanged package
answers `unchanged` and writes nothing.

## Gotchas

- **Symptom:** a package dropped into a Godot project by hand has twice the
  texture files (12 assets for 6 maps), textures that import lossless and the
  referenced ones without mipmaps, Godot's own levels of detail on top of the
  package's, one collision body per hull (8) and a part called rear_wheel2
  turned into a VehicleWheel3D named rear2. **Cause:** stock Godot 4.7.2 does
  this to a package as shipped (measured on a fresh project with a headless
  import; the switch to VRAM compression only happens in the editor GUI).
  **Remedy:** install it with this tool: one copy of each map, explicit import
  settings (VRAM compression, mipmaps, normal maps as normal maps, Godot's LOD
  generation and name hints off), one scene with the levels and one
  StaticBody3D for the hulls.
- **Symptom:** levels of detail pop or never switch in the installed scene.
  **Cause:** Godot measures the switch distance to the centre of each
  instance's bounding box, not its origin. **Remedy:** the scene is written
  with all levels sharing the asset's bounding box and a 2.5 % margin so that
  exactly one level is drawn at every distance; do not edit the switch
  distances by hand, rerun the install.
- **Symptom:** the install is refused. **Cause:** the project's engine does not
  match the package (re-export for it), the project root could not be found,
  or two engines' files sit in one directory (a monorepo root is not a
  project), or it is a Godot 3 project; a file in the destination was not
  written by this tool (never overwritten, with or without `force`), or one of
  the tool's own files was edited since. **Remedy:** read the refusal, it names
  the file or the engine; pass `force: true` only when the user says to replace
  their own edits to the tool's files.
- **Symptom:** the install failed halfway. **Cause:** any error while writing.
  **Remedy:** nothing to clean up: a failed install or update is rolled back
  (created files removed, replaced ones restored).
- **Symptom:** the project's scripts or autoloads seem to be missing from the
  check. **Cause:** by design the in-project check runs on an isolated copy
  without the project's autoloads or editor plugins, so the project's code
  never runs and its import cache is untouched. **Remedy:** none needed; say
  that is what was checked.
- **Symptom:** a Unity, Unreal Engine or Roblox project gets no install.
  **Cause:** automatic installation is built for Godot only; those projects are
  detected and refused with the file to import by hand. **Remedy:** pass on the
  named file and say plainly that nothing was written.

## Version notes

Last verified 2026-11-22 at the commit that added `agy3d install`.

- three.js: r152 to r187, the range the check was run on (0.152.2, 0.160.0,
  0.169.0, 0.170.0, 0.187.0); outside it the files are written and the result
  says "not checked on this version".
- Godot 4.7.2 is the version the install and its seven checks were measured
  and tested against, with a real Godot in the project's test set. Other 4.x
  versions are accepted with a warning that names the difference.
- Seven checks: import, scene, scale, levels, materials, collision and
  level switching (rendered from a moving camera; needs a display or
  `xvfb-run`).
