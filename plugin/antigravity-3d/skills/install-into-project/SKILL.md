---
name: install-into-project
description: >-
  Installs a finished antigravity-3d package into the user's own game project
  (Godot 4 automatically) and checks it inside that project with the project's
  engine. Use when the user asks to put, add, install or drop an exported
  asset into their game, their project or this workspace, or to remove one
  that was installed. Not for exporting or verifying a package on its own
  (that is engine-truth-verify).
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
2. **Dry run.** Call `install_into_project` with `package_dir` and
   `dry_run: true` (the default); `install_into_project` takes `project_dir`
   from this workspace when it is not given. It detects the engine and version
   from the project's own files and refuses when they do not match the engine
   the package was built for (re-export for the project's engine) or when it cannot tell which engine the project is
   (ask the user for the project's root directory). Show the user the plan:
   the destination (default `agy3d/<asset>` inside the project) and the files
   it would create, replace or remove, and the warnings (an engine version that
   differs from the one the package was checked against).
3. **Install, with the user's approval.** Call again with `dry_run: false`.
   The plugin asks the user before anything is written. Pass `force: true`
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

## Removing it

`agy3d uninstall --project DIR --dest DIR_IN_PROJECT` removes exactly the
files the install wrote (its record is `<dest>/.agy3d/install.json`) and keeps
any file the user added there. It refuses when one of its files was edited
since, unless the user asks for `--force`.
