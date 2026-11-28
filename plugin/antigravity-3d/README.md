# antigravity-3d: the Antigravity plugin

This plugin lets you ask the Antigravity agent for a game prop in your own words and get back a checked engine
package, installed into your Godot or three.js project if you want it there. It also changes an asset you made earlier and
cleans up meshes you already have. What the whole project does, and how it compares with a stock agent driving
Blender, is on the [front page](../../README.md).

## Install it in one command

From a clone of the repository, after `pip install -e ".[dev]"`, run this in your game's folder:

```bash
agy3d-plugin                       # the default: the agent drives the scene tools
agy3d-plugin --authoring script    # script mode: the agent writes each asset as one Python file
```

Then open the folder in Antigravity and ask. The plugin suggests these first requests:

- "Turn this concept sheet into a finished prop and lock the silhouette"
- "Forge a PBR material for weathered bronze and gate it against policy"
- "Make every file in the depot game-ready for Unreal 5"

## What it does for you, and with which Antigravity piece

| What happens for you | The piece |
|---|---|
| You ask in your own words and the right playbook loads: modelling, materials, art style, concept sheets, export, mesh cleanup, installing, a scroll-driven 3D website around the props, a story film told beat by beat | nine skills |
| The agent follows the studio's conventions (USD is the source of truth, metres and Z-up, PBR ranges, Blender 5.3 API names, engine budgets) | three rules, one always on, two scoped to files |
| Your export is held until the checks pass and nothing changed since; the agent is sent back once if it tries to stop with the checks unmet | PreToolUse and Stop hooks |
| You are asked before an install writes into your project, and you approve the token and image limits of an unverified preview | PreToolUse hook (`ask`) |
| The agent starts each session knowing the budget, the stage and the last check result | SessionStart hook |
| Conversations started by the app, the CLI, automations and workflows all get the same tools, with the same gates | MCP server `studio` |
| You review the asset in a 3D panel, inline or in the side pane; your notes per part reach the agent | MCP App (the inspector) |
| Structure and looks are checked by helpers that cannot edit your asset | two read-only subagents |
| Blender stays warm between calls; files dropped in a folder are cleaned up by themselves; queued variants are made at night | sidecars and scheduled automations |
| A whole folder of meshes is sanitized, gated by the read-only verifier and optionally exported in one call | a reusable `run_workflow` script, `workflows/batch_sanitize.py` |

It uses the platform's own building blocks instead of hand-written supervision, cron jobs or orchestration. Every
format below was checked against the docs and schemas built into the Antigravity harness that ships with
`google-antigravity` 0.1.24; the table at the end separates what those docs confirm from what they don't. The rest
of this page is the reference: install options, each file, the state the hooks share, the MCP server, script mode
and the inspector.

## Install options

```bash
# Copy into the workspace's customization root (<cwd>/.agents/plugins/antigravity-3d)
python -m antigravity3d.plugin_install
# Live-link the repo copy instead (edits take effect immediately)
python -m antigravity3d.plugin_install --symlink
# Global install, with absolute workspace and depot paths written into the sidecar permissions
python -m antigravity3d.plugin_install --target ~/.gemini/config --workspace /path/to/project --depot /path/to/depot
# Validate a bundle (JSON, sidecar schema, frontmatter, hooks, workflow script)
python -m antigravity3d.plugin_install --validate plugin/antigravity-3d
# Script mode (copy installs only)
python -m antigravity3d.plugin_install --authoring script
```

`agy3d-plugin` is the same installer. `agy3d plugin install [--target DIR]` and `agy3d plugin validate [PATH]` do the
copy install and the validation only.

A copy install binds Python hook commands, Python sidecars, and the studio MCP server
to the interpreter that installed the package. Reinstall after moving or replacing
the virtual environment. Developer symlinks use the source launchers and need the
project environment on the host's path.

There is a second way to install: `antigravity3d.plugin_install.register(workspace)`
adds this folder's parent to `<workspace>/.agents/plugins.json`. Each entry in
that file is scanned one level deep.

Plugins load by themselves. Automations (the two schedule sidecars) are the
exception: you have to turn them on yourself in the **Automations**
dashboard. Following the docs, the plugin never turns itself on.

## What each piece does

| Path | Purpose |
| --- | --- |
| `plugin.json` | Manifest. Uses only fields the docs allow, because the loader silently drops unknown ones. |
| `rules/AGENTS.md` | Always-on studio rules: USD is the source of truth; edit only through Scene IR, with raw bpy only through the linter-gated escape hatch; metres and Z-up, with axis conversion only at export; PBR ranges, ORM packing and normal-map convention; the QA gate; renders always go through the QA tools. |
| `rules/blender-5-3-api.md` | Rule scoped by `trigger: glob` to `**/*.py, **/*.blend`: Blender 5.3 API rules: `gpu.init()` before any `gpu` call, Geometry Nodes inputs through `properties.inputs`, `BLENDER_EEVEE`, solver `FLOAT`, and the other names removed in 5.x with their replacements. The linter enforces them too. |
| `rules/engine-budgets.md` | Rule scoped by `trigger: glob` to USD files and `*_exports/` folders: budgets and conventions for UE5, Unity URP, Godot 4, Roblox R15 and three.js (web). |
| `skills/art-direction` | Picks and applies a named art direction (low-poly, retro-lowres, stylized, realistic, voxel, isometric, anime, hand-painted): a lower triangle budget where the look needs one, the shipped maps' size, palette and filtering, the shading crease, and short briefs for the models. Eight style files in `styles/`. |
| `skills/concept-to-blueprint` | Optional concept art: a Best-of-4 orthographic-view tournament that picks one blueprint (a prop from a text brief needs none). |
| `skills/parametric-kitbash` | The prop's model built from Scene IR patches (a finished prop, not a blockout: bevels, inset panels, lathe and extrude parts, mirror and repeat), plus a loop that locks the silhouette. |
| `skills/pbr-material-forge` | Albedo Best-of-6, de-lighting, ORM and normal derivation, then a policy gate. |
| `skills/game-ready-sanitizer` | Makes existing files game-ready: UVs, LODs, colliders, validator. |
| `skills/engine-truth-verify` | Export, headless engine import, a parity check against the stage, and the manifest's provenance record of what made each file. |
| `skills/install-into-project` | Installs a package into the user's game project (`install_into_project`) and checks it there with the project's engine. Godot 4: one scene with levels, collision and materials wired. three.js or React Three Fiber (a package built with `engine="web_three"`): the asset's files where the dev server serves them and a loader module to import, with a component for React Three Fiber, checked in a headless browser with the project's own three.js. |
| `skills/scroll-3d-website` | A scroll-driven 3D website around the props: the order of work (storyboard, page and scene, props through the plugin and the installer, a look with `check_site`, revision), what makes a site scroll well, a guide to current web libraries whose examples ran against their versions (`libraries.md`), recipes, and `starter/`, the smallest Vite project with the plumbing right, run in a browser by `tests/test_scroll_site_starter.py`. Present in both authoring modes. |
| `skills/story-slides-3d` | A short film or a talk in the browser, told beat by beat in a fixed frame: the order of work (a beat sheet first, the film in blocks, light and one print treatment, motion and cuts, a look with `check_site` beat by beat), a staging guide (`staging.md`), and a starter that is the engine: scenes and beats, a state every picture is a function of, three treatments (a grade, halftone dots, text characters), five cuts, captions, keys, a link to every beat, auto-play, reduced motion and an article where WebGL is missing. |
| `agents/usd_verifier.md` | Isolated, read-only subagent that runs the Tier-1 structural check (`run_usd_validator`) and returns a two-line verdict. |
| `agents/lookdev_critic.md` | Isolated visual critic that returns a two-line audit delta. |
| `mcp_config.json` | Registers the stdio MCP server `studio` (`python -m antigravity3d.runtime.mcp_server`, launched with `uv run --project ${PLUGIN_ROOT}`). The harness namespaces plugin servers `<plugin>_<server>`, so it appears as `antigravity-3d_studio`. See "MCP server" below. |
| `hooks.json` | Wires the four hook scripts in `hooks/scripts/`. It sits at the plugin root because native plugins must not use `hooks/hooks.json`. |
| `hooks/scripts/pre_tool_gate.py` | PreToolUse hook. Blocks `export_engine_package` (also when it arrives as an `mcp_tool` step) unless Tier-1 passed, Tier-1.5 is at least 3.4, and nothing changed since QA; on any error it blocks. Blocks raw bpy scripts that fail `linter.assert_safe`. Asks the user before a preview or an install; see the list below the table. |
| `hooks/scripts/post_tool_telemetry.py` | PostToolUse hook. Appends to `<state>/telemetry/tool_calls.jsonl` (tool, ok, error, session, duration) and marks the scene as changed after any editing tool. |
| `hooks/scripts/session_start.py` | SessionStart hook. Adds one short system message with the budget, stage and last QA verdict. |
| `hooks/scripts/stop_gate.py` | Stop hook. While this conversation has changed an asset and the QA gate is unmet, it refuses to let the agent finish and gives a one-line reason. It refuses at most once per turn. |
| `sidecars/blender_pool` | Keeps a warm pool of Blender workers running (`worker_daemon --workers 4`, `restart_policy: always`), reached through `$AGY3D_POOL_SOCKET`. |
| `sidecars/depot_watcher` | Watches the drop folder (polling, with debounce and batching, remembering files it has already seen) and starts a batch-sanitize run through `agentapi new-conversation`. |
| `sidecars/nightly_variant_factory` | Scheduled automation (`builtin: schedule`), daily at 02:17 UTC. |
| `sidecars/weekly_distill` | Scheduled automation (`builtin: schedule`), Sundays at 03:43 UTC. Its only command grant is `distill_job`. |
| `workflows/batch_sanitize.py` | Workflow script: sanitize, gate with `usd_verifier`, then optionally export. Run it with `run_workflow(ScriptPath=<abs path>, Args={"files": [...]})`. |

What `pre_tool_gate.py` decides besides the export gate:

- **Asks the user** (`ask`) before an unverified preview, showing the token ceiling and image limit the model passed
  (`start_preview`, or `open_asset_session` / `run_build` with `preview=true`). Missing limits are denied.
- **Asks the user** before `install_into_project` writes into a project. A dry run is allowed.
- **Denies** `open_asset_session` and `run_build` without this conversation's id.
- **Asks the user** to review a raw bpy script when the linter cannot be imported.

### State shared between the tools and the hooks

Hooks run as separate short-lived processes, so they communicate through
files under `$AGY3D_STATE_DIR` (default `<workspace>/.agy3d`):

- `qa/latest.json` and `qa/<conversation or session id>.json`: the QA verdict.
  - **Writer.** The Python session (`harness/session.py`, `Session.persist_qa`, which calls
    `antigravity3d.runtime.qa_state.write_qa_state`) is the single writer. A failed write is logged once and never
    fails the tool.
  - **When.** Every time `last_tier1_ok` or `last_tier15_score` changes: a Tier-1 pass, a visual-QA score, and the
    reset to 0 when geometry or materials change.
  - **New sessions.** Opening a session writes a blank verdict, so a new session never inherits an older one's pass.
  - **Where.** The state root is `$AGY3D_STATE_DIR`, else `<workspace>/.agy3d`, resolved as in the hook scripts:
    SDK stage agents use their workdir as the workspace; the MCP server uses the client's first root, else its cwd.
  - **One session per workspace in a native conversation.** It has no conversation id on the MCP side, so its
    hooks fall back to `qa/latest.json`.
- `hooks/dirty/<conversation>.json`: written by the PostToolUse hook after an
  editing tool runs. `generate_lods_and_colliders` does not count as editing:
  it only derives data, so QA -> LODs -> export stays open.
- `telemetry/tool_calls.jsonl`: input to the weekly distillation.
- `budget.json` (optional): target engine and budget overrides that the
  SessionStart hook reads.

## MCP server

Conversations started by the app or CLI, by `agentapi new-conversation` (depot watcher, scheduled automations) and
by `run_workflow` agents do not get Python SDK custom tools. `antigravity3d.runtime.mcp_server` serves them over MCP
instead:

- every tool in `harness/tools.py`, with its name, docstring and a schema generated from its signature (no `ctx`;
  an optional `session_id` is added);
- eight session management tools:

| Tool | What it does |
|---|---|
| `open_asset_session` | open a new asset session |
| `open_existing_asset` | reopen an asset this studio made, to change it rather than author it again |
| `start_preview` | turn an open session into an unverified preview with the user's limits |
| `set_stage` | switch the active stage's tool set |
| `session_status` | stage, budget, QA verdict and newest audit lines |
| `list_sessions` | the open sessions |
| `install_into_project` | put a package into the user's game project and check it there; a dry run by default. Takes `package_dir`, `project_dir`, `dest`, `force` and, for three.js, `assets` and `compress` (`auto`, `always`, `never`) |
| `check_site` | look at a built website as a visitor does: photographs at even scroll steps at desktop and phone size (contact sheets), errors, failed requests, weight, what the 3D scene costs, reduce motion, no WebGL, and `data-src` facts against their source. Takes `target` (the built folder or a localhost URL), `sources`, `steps`, `sizes`, `other_hosts` |

- **Enforcement.** There is no SDK hook runner on this path, so the server calls `InvariantAndSafetyGate.decide`
  (the gate's decision logic, shared with the SDK hook) before every dispatch.
- It applies stage tool-set scoping, the QA export gate, the raw-script linter and the image policy gate. A denial
  is one `REFUSED ...` line.
- The hook scripts in this plugin check export and raw scripts again when the call is an `mcp_tool` step; both read
  the same QA file.
- **Stages.** A new connection lists the eight session management tools, the inspector's `open_inspector` and
  `review_notes`, and the six tools of the short path (`apply_scene_ir`, `render_preview`, `run_visual_qa`,
  `deliver_package`, `inspect_manifest`, `undo_last_edit`).
- Once an asset is open, discovery adds the active stage's tools. Clients receive a list-change notification when
  switching catalogs. Calls addressed to a different session still pass through that session's stage gate.
- **Backends.** `GEMINI_API_KEY` set: `make_control_plane` (Decisions API, generative fallback) and a real
  `ImageForge`. `AGY3D_OFFLINE=1`, or no key: the fakes.
- The Blender pool is attached through `PoolClient` when `$AGY3D_POOL_SOCKET` exists.
- **Ordering.** The MCP runner dispatches requests concurrently, so every tool call takes one FIFO lock as its
  first await: calls run in arrival order (a client may pipeline `open_asset_session` and `apply_scene_ir`), at the
  cost of serial execution per server process.
- On stdin EOF the server answers every request it already received before it exits (`_DrainingStreams`).
- **Long calls.** Antigravity ends any MCP tool call after 3 minutes, and has no setting for it. Every harness tool
  therefore runs as a task its session keeps, and `run_build` and `install_into_project` as tasks the server keeps
  (`harness/keeper.py`); each answers within 150 s: with its result, or with `STILL WORKING (nothing needs fixing, nothing failed)`, the step it
  is at and the call to repeat. The work goes on after that answer and after a client cancel; the same call with the
  same arguments joins it, so no step runs twice. A call that changes the design stops kept work of the design
  before (its folders are removed, nothing of it is delivered); `session_status` names a call that is running and its
  step.
- **Sessions.** The active session is the last one opened or selected with
  `set_stage`; pass `session_id` to act on another.
- The nightly variant factory carries explicit `mcp(antigravity-3d_studio/<tool>)`
  grants, never `mcp(*)`. The weekly distillation carries only its `command(...)` grant.

## Script mode

`agy3d-plugin --authoring script` (copy installs only) installs the same bundle in a second mode, in which the agent
writes an asset as one Python file (`from agy3d import Asset`) and one tool, `run_build`, checks the script, runs it
in a process without the key, applies the design and answers with findings and five colour views.
`deliver_package` delivers it as in the default mode, and the script travels with the package as
`source/build.py`.

What the install changes in the copied bundle (`plugin_install._script_mode`):

- `mcp_config.json`: the server gets `AGY3D_AUTHORING=script`. It then lists `run_build`, `open_existing_asset`,
  `start_preview`, `session_status`, `list_sessions`, `install_into_project`, `check_site`, `deliver_package` and the inspector's
  tools, the same list before and after a session opens; no stage tools.
- `rules/AGENTS.md`, `rules/engine-budgets.md` and `skills/parametric-kitbash/SKILL.md` are replaced by the
  script-mode texts in `src/antigravity3d/build/plugin_script_mode/`.
- The skills `pbr-material-forge`, `engine-truth-verify`, `concept-to-blueprint` and `game-ready-sanitizer` and the
  `agents/` folder are removed: their steps are tools script mode does not serve.
- A file `authoring` (one line, `script`) tells the hook scripts which mode they are in.

Measured against the default on three briefs it was no faster and no cheaper for a first build; its assets were
preferred in blind comparisons and a change request became an edit of the file
([`benchmarks/ROUND_SIX.md`](../../benchmarks/ROUND_SIX.md)). The check before a script runs is not a sandbox.

## 3D review inspector (MCP App)

The studio server also serves the inspector as an MCP App
(`antigravity3d/runtime/inspector_server.py`). Antigravity renders it in an
isolated frame, inline in the conversation or in the side pane (opened from the side pane's "+" menu), and talks to
it through the MCP Apps SDK (`@modelcontextprotocol/ext-apps`, bundled).

The front end lives in `apps/inspector`;
rebuild it with `npm ci && npm run build`. The built document is committed, and a test (`tests/test_inspector.py`)
fails when it is stale.

| Name | Kind | Visibility | Purpose |
| --- | --- | --- | --- |
| `ui://antigravity-3d/inspector.html` | resource, `text/html;profile=mcp-app`, `_meta.ui.prefersBorder` | | One self-contained document (three.js r160 + SDK + app, about 1.4 MB). Built from `apps/inspector` (`npm ci && npm run build`); `$AGY3D_INSPECTOR_APP_HTML` serves another file for UI iteration. |
| `open_inspector` | tool, `_meta.ui.resourceUri` + entrypoints `aux_pane` and `global` ("3D Inspector") | model and app | Opens the inspector for the active (or given) session. All arguments optional (`node`, `lod`, `channel`, `view`, `wireframe`, `colliders`); calling it again updates the open view. Returns one line for the model and a small summary as `structuredContent`. |
| `review_notes` | tool | model | The reviewer's notes, current and stale. |
| `inspector_payload` | tool, `visibility: ["app"]` | app only | Per revision, in 512 KiB chunks: part `scene` (review metadata: budget, node table, QA, which glTF node is which node / LOD / hull) and part `glb`, the review GLB. |
| `inspector_qa_state`, `inspector_list_notes`, `inspector_save_note` | tools, `visibility: ["app"]` | app only | QA gate state; notes listing and eager saving. |

The inspector tools are session-level, like the management tools: they are
listed in every stage, and they do not count against the stage sets (at most five tools each).

**How notes reach the agent.**

1. The reviewer writes a note per node (or for the whole asset).
2. Each keystroke burst is saved to the session through `inspector_save_note` into
   `<session workdir>/review_notes.json`, tied to the node and to the *revision*: a content hash of what the
   inspector showed. Frames are removed without teardown, so nothing waits for close.
3. After a selection or a note, the app calls `updateModelContext` with one text block (what is selected, what was
   just written, how many notes exist), which Antigravity attaches to the reviewer's next message.
4. The agent reads all notes with `review_notes`. After an edit that changes what is shown, earlier notes are kept
   and reported as stale, never dropped.
5. Where the host offers `message`, an "Ask agent to address notes" button sends a user message.

**What the app does not use.** No browser storage (the frame's origin is
fresh and storage is discarded), no network (the default CSP
`default-src 'none'` needs no extra domains), no hand-written `postMessage`
bridge.

**What the reviewer sees is what ships.**

- The asset reaches the page as one "review GLB" (`antigravity3d/artifacts/review_glb.py`), built by the package's
  glTF writer with the package's conversion: crease normals split at hard edges, glTF UVs, the package's GLB
  materials.
- It holds LOD0 and every further LOD level per node, and the collider hulls, as separate nodes sharing
  de-duplicated images.
- three.js r160's `GLTFLoader` (vendored unmodified, see `artifacts/vendor/three-r160-addons.provenance.json`)
  loads it.
- The debug channels (clay, albedo, ORM, occlusion, roughness, metallic, normal map, UV checker, prim ID),
  wireframe, colliders, LODs and picking are overrides on the loaded meshes.
- In a session, materials come from the composed stage (`review_glb.session_materials`).

The standalone `inspector.html` that `agy3d prop` writes into a package is the
same front end (`apps/inspector/src/viewer.js`) with the payload and the review
GLB embedded; it keeps notes in the page and exports them as a JSON file.

The inspector used to be a `has_web_ui` sidecar (`sidecars/inspector`) serving
HTML files from a directory. It was removed: nothing in the pipeline wrote into
that directory, it was a second way to show the same viewer, and in the side
pane its notes lived in browser storage that the platform discards.

## Verified against the harness docs, and what isn't

The harness binary embeds its own documentation. Line numbers below refer to
a `strings` dump of that binary.

| Item | Status | Source in the dump |
| --- | --- | --- |
| Plugin layout and `plugin.json` fields; `hooks.json` at the plugin root | verified | "# Plugins" 306249-306330; plugin skill 327604-327858; built-in swarm `plugin.json` 282729 |
| `agents/<agent>.md` inside a plugin | verified (layout) | plugin skill layout 327766 |
| Agent frontmatter `name`, `description`, `subagent`, `mainAgent`, `tools` | verified | migrate-agent-settings 315482-315496 |
| Agent `model:` set to an exact model id with a thinking-level suffix (`gemini-4-hydrogen-low`) | **partly verified**: the `model` field exists, and ids with a thinking-level suffix exist (281854 `gemini-4-hydrogen-low`). No doc shows a model in agent frontmatter, and there is no separate frontmatter field for thinking level. | model field 220755; model ids 281759-281922 |
| Plugin tool names (`inspect_manifest`, ...) in an agent's `tools` list | **unverified**: they are Python SDK tools, not harness tools | |
| Rule frontmatter `trigger: glob` with `globs` given as one comma-separated string, in a plugin's `rules/` folder | verified (migrate doc writes `rules/<name>.md` with frontmatter into a plugin) | 315497-315510; customization guide 308186-308193 |
| Hook events, payload fields (camelCase) and output formats | verified | "Lifecycle Hooks" 325489-325810 |
| PreToolUse decision values; an unrecognized decision blocks the tool call | verified | 280596, 282505 |
| Hook matcher target for Python SDK tools | **unverified**: the docs derive tool names from step types (`custom_tool` is the SDK custom-tool step). The matcher keeps the name-based alternatives, which also match if the harness reports the tool name itself. | 325575-325580; step names 320924-320935 |
| Hook matcher target for MCP tools | **partly verified**: tool name = lower-cased step type, and the MCP step type is `CORTEX_STEP_TYPE_MCP_TOOL`, so the name is `mcp_tool` (added to the matcher). **Unverified**: the key names inside `toolCall.args` for the server and tool (the action proto has `server_name`, `tool_name`, `arguments_json`). The gate script accepts `ToolName`/`toolName`/`tool_name`, `ServerName`/`serverName`/`server_name` and `Arguments`/`ArgumentsJson` variants, and a namespaced step name ending in the tool name. | 325575-325580; 320924, 422119; 105685-105687 |
| `mcp_config.json` in a plugin root; `mcpServers` map; stdio `command`/`args`/`env` | verified | "# MCP Servers" 300694-300760; plugin layout 306259 |
| Plugin MCP servers namespaced `<plugin>_<server>` (so `studio` becomes `antigravity-3d_studio`); `mcp(<server>/<tool>)` grants | verified for the server prefix (the packaging example gives `tasks_tasks`); how the harness shows the tool names to the model is **unverified**. The earlier "server names must be prefixed with the plugin name" is not in the docs: the harness adds the prefix itself, so the config key is the bare `studio`. | 319165; 300740-300745; 318662 |
| `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` expansion in `mcp_config.json` | verified | 319146-319160 |
| Working directory and workspace of a plugin's stdio MCP server | Copy installs bind Python launchers to the installing interpreter. The first client workspace root sets the default state location, with process cwd as a fallback. Host roots discovery remains unverified. | |
| Whether the client re-lists tools on `list_changed` | Verified with official MCP clients, pending native Antigravity UI qualification. | |
| MCP tool results as plain text lines | verified against the `mcp` 2.3.0 client in `tests/test_mcp_server.py`; the harness's own handling of `isError`/text content is unverified | |
| A matcher of `"*"` is passed through to every tool | verified | 325575 |
| `sidecar.json` schema | **reconstructed**: the docs say a schema applies, but the binary builds it at runtime from the Go struct tags (lines 257098-280837) and proto descriptor (about 415600-415690). The copy kept at `antigravity3d/runtime/sidecar.schema.json` assumes `additionalProperties: false`. | 298906-298930 |
| Plugin sidecars at `plugins/<plugin>/sidecars/<id>/`, with ids of the form `<plugin>/<folder>` | verified (UI plugin navigation doc) | 302705-302735 |
| `ANTIGRAVITY_SIDECAR_WEB_PORT` and `ANTIGRAVITY_EXECUTABLE_DATA_DIR` environment variables | verified | 298922, 298928 |
| `builtin: schedule` with `args = [cron, "agentapi", "new-conversation", flags..., "--", prompt]` and `CRON_TZ=` | verified | Scheduled Automations 318461-318640 (quick-start object form at 318481) |
| `agentapi new-conversation [--model=<flash_lite\|flash\|pro\|model_id>] [--title=] [--profile=]` | verified (usage text) | 282566, 282436, 282541 |
| `agent_permissions` fields `workspace_uris`, `access_grants`, `allows_user_interactions` | verified | 318565, 318640-318660; proto 415630 |
| Default sandbox for sidecars and the effect of `sandbox.read_write_paths` | **unverified** | tags 264374, 267396 |
| Python sidecar and MCP commands | Copy installs use the installing environment’s absolute interpreter path. | |
| Plugins shipping a `workflows/` folder | **not a documented plugin component**: it's a plain file run with `run_workflow(ScriptPath=...)` | run-workflow 314708-314880 |
| MCP App resource (`ui://`, MIME `text/html;profile=mcp-app`, 10 MiB limit, `_meta.ui.prefersBorder`), tool `_meta.ui.resourceUri`, `visibility`, `com.google.antigravity/ui` entrypoints `aux_pane`/`global` with `title`, entrypoint launches with `{}` | verified (docs); checked by `tests/test_mcp_server.py` through the real stdio server and by `plugin_install.validate` | "# Developing MCP Apps" 318990-319060 (rules 319031-319051) |
| 1 MiB cap on the stored `CallToolResult` of a model tool call; large data from an app-only tool | verified (docs). Whether results of app calls (`callServerTool`) are capped is **not stated**; chunks are 512 KiB to stay below 1 MiB either way | 319048-319050 |
| Host behaviour: SDK handshake, `updateModelContext` shown as a pill on the next user message, `callServerTool` limits (4 in flight), no `ui/resource-teardown`, side-pane auto-open and "calling the same tool again updates the open pane" | verified as documented; **not exercised against a running Antigravity**. `tests/test_inspector.py::test_mcp_app_in_a_browser_host` drives the built app with the SDK's own `AppBridge` in Chromium under the documented CSP | 319040-319046, 319082-319110 |
| Which surface sends `containerDimensions.maxHeight` | docs: "when inline". The app uses its presence to choose a fixed-height inline layout and fills the frame otherwise; **unverified** in the side pane and global view | 319112-319115 |
| Default frame CSP | docs give `default-src 'none'` with inline scripts/styles and `data:`/`blob:` images, fonts, media; the exact header is **unverified**. The test applies that policy as a response header | 319136-319138 |

### Where the docs contradict the original spec

- `working_dir` is not a sidecar field. A sidecar always runs with its own
  folder as the working directory.
- The Scheduled Automations quick-start shows `"args": {"schedule", "prompt"}`,
  but its detailed section and the base sidecar docs use an `args` array. The
  array form is used here.
- In the docs, the `run_workflow` `agent()` call takes a `model` tier
  (`flash_lite`, `flash`, `pro`). The SDK's typed placeholder doesn't. The
  script uses `type_name="usd_verifier"` instead of a model tier.
- The PostToolUse payload has no timestamps, so `duration_ms` is exact only
  for tools that also went through the PreToolUse gate. For other tools,
  `since_prev_ms` is an upper bound.
- Python SDK tools registered on a `LocalAgent` are only available to agents created through the SDK.
- Conversations that `agentapi` starts (the depot watcher, automations, workflow agents) get the same tools from
  the plugin's MCP server (`mcp_config.json`).
