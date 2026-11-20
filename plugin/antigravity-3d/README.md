# antigravity-3d: Antigravity plugin

This plugin packages the antigravity-3d studio for Google Antigravity. It uses
the platform's own building blocks instead of hand-written supervision, cron
jobs or orchestration:

- always-on and file-scoped rules
- skills (step-by-step playbooks the agent loads when relevant)
- lifecycle hooks
- custom subagents
- an MCP server that serves the studio tools to native conversations
- managed background processes ("sidecars")
- scheduled automations
- a side-pane panel served by a sidecar
- a reusable `run_workflow` script

Every format below was checked against the docs and schemas built into the
Antigravity harness that ships with `google-antigravity` 0.1.24. The table at
the end separates what those docs confirm from what they don't.

## Install

```bash
# Copy into the workspace's customization root (<cwd>/.agents/plugins/antigravity-3d)
python -m antigravity3d.plugin_install
# Live-link the repo copy instead (edits take effect immediately)
python -m antigravity3d.plugin_install --symlink
# Global install, with absolute workspace and depot paths written into the sidecar permissions
python -m antigravity3d.plugin_install --target ~/.gemini/config --workspace /path/to/project --depot /path/to/depot
# Validate a bundle (JSON, sidecar schema, frontmatter, hooks, workflow script)
python -m antigravity3d.plugin_install --validate plugin/antigravity-3d
```

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
| `rules/blender-5-3-api.md` | Rule scoped by `trigger: glob` to `**/*.py, **/*.blend`: Blender 5.3 API rules (`BLENDER_EEVEE`, solver `FLOAT`, no `bgl`, `AssetRepresentation`). The linter enforces them too. |
| `rules/engine-budgets.md` | Rule scoped by `trigger: glob` to USD files and `*_exports/` folders: budgets and conventions for UE5, Unity URP, Godot 4 and Roblox R15. |
| `skills/concept-to-blueprint` | Best-of-4 orthographic-view tournament that picks one winner. |
| `skills/parametric-kitbash` | Blockout built from Scene IR patches, plus a loop that locks the silhouette to the orthos. |
| `skills/pbr-material-forge` | Albedo Best-of-6, de-lighting, ORM and normal derivation, then a policy gate. |
| `skills/game-ready-sanitizer` | Makes existing files game-ready: UVs, LODs, colliders, validator. |
| `skills/engine-truth-verify` | Export, headless engine import, then a parity check against the stage. |
| `agents/usd_verifier.md` | Isolated, read-only subagent that runs the Tier-1 and Tier-1.5 QA gate and returns a two-line verdict. |
| `agents/lookdev_critic.md` | Isolated visual critic that returns a two-line audit delta. |
| `mcp_config.json` | Registers the stdio MCP server `studio` (`python -m antigravity3d.runtime.mcp_server`, launched with `uv run --project ${PLUGIN_ROOT}`). The harness namespaces plugin servers `<plugin>_<server>`, so it appears as `antigravity-3d_studio`. See "MCP server" below. |
| `hooks.json` | Wires the four hook scripts in `hooks/scripts/`. It sits at the plugin root because native plugins must not use `hooks/hooks.json`. |
| `hooks/scripts/pre_tool_gate.py` | PreToolUse hook. Blocks `export_engine_package` (also when it arrives as an `mcp_tool` step) unless Tier-1 passed, Tier-1.5 is at least 3.4, and nothing changed since QA; on any error it blocks. Blocks raw bpy scripts that fail `linter.assert_safe`. |
| `hooks/scripts/post_tool_telemetry.py` | PostToolUse hook. Appends to `<state>/telemetry/tool_calls.jsonl` (tool, ok, error, session, duration) and marks the scene as changed after any editing tool. |
| `hooks/scripts/session_start.py` | SessionStart hook. Adds one short system message with the budget, stage and last QA verdict. |
| `hooks/scripts/stop_gate.py` | Stop hook. While this conversation has changed an asset and the QA gate is unmet, it refuses to let the agent finish and gives a one-line reason. It refuses at most once per turn. |
| `sidecars/blender_pool` | Keeps a warm pool of Blender workers running (`worker_daemon --workers 4`, `restart_policy: always`), reached through `$AGY3D_POOL_SOCKET`. |
| `sidecars/depot_watcher` | Watches the drop folder (polling, with debounce and batching, remembering files it has already seen) and starts a batch-sanitize run through `agentapi new-conversation`. |
| `sidecars/inspector` | Side-pane panel (`has_web_ui`, view entrypoint `SIDECAR_UI_ENTRYPOINT_AUX_PANE`, path `/`) that serves the generated viewer pages on `127.0.0.1:$ANTIGRAVITY_SIDECAR_WEB_PORT`. |
| `sidecars/nightly_variant_factory` | Scheduled automation (`builtin: schedule`), daily at 02:17 UTC. |
| `sidecars/weekly_distill` | Scheduled automation (`builtin: schedule`), Sundays at 03:43 UTC. Its only command grant is `distill_job`. |
| `workflows/batch_sanitize.py` | Workflow script: sanitize, gate with `usd_verifier`, then optionally export. Run it with `run_workflow(ScriptPath=<abs path>, Args={"files": [...]})`. |

### State shared between the tools and the hooks

Hooks run as separate short-lived processes, so they communicate through
files under `$AGY3D_STATE_DIR` (default `<workspace>/.agy3d`):

- `qa/latest.json` and `qa/<conversation or session id>.json`: written by the
  Python session (`harness/session.py`, `Session.persist_qa`, which calls
  `antigravity3d.runtime.qa_state.write_qa_state`) every time `last_tier1_ok`
  or `last_tier15_score` changes: Tier-1 pass, a visual-QA score, and the reset
  to 0 when geometry or materials change. It is the single writer; a failed
  write is logged once and never fails the tool. Opening a session also writes
  a blank verdict, so a new session never inherits an older session's pass.
  The state root is `$AGY3D_STATE_DIR`, else `<workspace>/.agy3d`, resolved the
  same way as in the hook scripts (SDK stage agents use their workdir as the
  workspace; the MCP server uses the client's first root, else its cwd). A
  native conversation has no conversation id on the MCP side, so its hooks fall
  back to `qa/latest.json`; run one asset session at a time per workspace there.
- `hooks/dirty/<conversation>.json`: written by the PostToolUse hook after an
  editing tool runs. `generate_lods_and_colliders` does not count as editing:
  it only derives data, so QA -> LODs -> export stays open.
- `telemetry/tool_calls.jsonl`: input to the weekly distillation.
- `budget.json` (optional): target engine and budget overrides that the
  SessionStart hook reads.

## MCP server

Conversations started by the app or CLI, by `agentapi new-conversation` (depot
watcher, scheduled automations) and by `run_workflow` agents do not get Python
SDK custom tools. `antigravity3d.runtime.mcp_server` serves them over MCP
instead: every tool in `harness/tools.py` with its name, docstring and a schema
generated from its signature (no `ctx`; an optional `session_id` is added), plus
`open_asset_session`, `set_stage`, `session_status` and `list_sessions`.

- **Enforcement.** There is no SDK hook runner on this path, so the server calls
  `InvariantAndSafetyGate.decide` (the gate's decision logic, shared with the
  SDK hook) before every dispatch: stage tool-set scoping, the QA export gate,
  the raw-script linter and the image policy gate. A denial is one
  `REFUSED ...` line. The hook scripts in this plugin check export and raw
  scripts again when the call is an `mcp_tool` step; both read the same QA file.
- **Stages.** `tools/list` always lists every tool. Narrowing it to the active
  stage would need `notifications/tools/list_changed` and a client that
  re-lists, which the docs do not promise, so scoping happens at call time.
- **Backends.** `GEMINI_API_KEY` set: `make_control_plane` (Decisions API,
  generative fallback) and a real `ImageForge`. `AGY3D_OFFLINE=1`, or no key: the
  fakes. The Blender pool is attached through `PoolClient` when
  `$AGY3D_POOL_SOCKET` exists.
- **Ordering.** The MCP runner dispatches requests concurrently, so every tool
  call takes one FIFO lock as its first await: calls run in arrival order (a
  client may pipeline `open_asset_session` and `apply_scene_ir`), at the cost of
  serial execution per server process. On stdin EOF the server answers every
  request it already received before it exits (`_DrainingStreams`).
- **Sessions.** The active session is the last one opened or selected with
  `set_stage`; pass `session_id` to act on another.
- The two schedule sidecars that start conversations carry explicit
  `mcp(antigravity-3d_studio/<tool>)` grants, never `mcp(*)`.

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
| Working directory and workspace of a plugin's stdio MCP server | **unverified**: `uv run --project ${PLUGIN_ROOT}` finds the project only when the plugin is installed inside the workspace tree. The server asks the client for `roots/list` and uses the first root as the workspace for `.agy3d`; if the client has no roots it uses its cwd, and `$AGY3D_STATE_DIR` overrides both. The Antigravity client's `roots/list` behaviour is unverified (a roots-workspace listener exists in the binary, 170395). | |
| Whether the client re-lists tools on `list_changed` | **unverified**, so the server lists all tools and scopes at call time | |
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
| Whether the browser security policy (CSP) of the side pane allows the inspector page's three.js CDN import | **unverified**: generative-UI HTML artifacts block CDNs. Building with `three_js_source` puts the library inline in the page and avoids this. | 310213-310240 |

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
- Python SDK tools registered on a `LocalAgent` are only available to agents
  created through the SDK. Conversations that `agentapi` starts (the depot
  watcher, automations, workflow agents) get the same tools from the plugin's
  MCP server (`mcp_config.json`).
