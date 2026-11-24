"""Install and validate the antigravity-3d Antigravity plugin bundle.

The bundle lives in the repo at ``plugin/antigravity-3d`` (or, in a wheel, at
``antigravity3d/_plugin_bundle/antigravity-3d`` if packaged there). Per the
harness Plugins docs, a plugin is a sub-directory of a ``plugins/`` folder in a
customization root such as ``<workspace>/.agents/`` (workspace scope) or
``~/.gemini/config/`` (global), and is picked up automatically.

    python -m antigravity3d.plugin_install                 # copy into ./.agents/plugins/
    python -m antigravity3d.plugin_install --symlink       # live-link the repo copy
    python -m antigravity3d.plugin_install --validate plugin/antigravity-3d

``validate`` checks what the harness docs define: every JSON file parses,
``sidecar.json`` files match the vendored schema (``runtime/sidecar.schema.json``),
``mcp_config.json`` (documented stdio / Streamable HTTP fields, ``mcp(<plugin>_<server>/<tool>)`` grants),
``SKILL.md`` / agent / rule frontmatter, ``hooks.json`` structure and that the
scripts it runs exist, that workflow scripts pass
``google.antigravity.workflows.validate_workflow_source``, and that the MCP App
metadata the studio server declares (``ui://`` resources, tool ``_meta.ui`` and
entrypoints) follows the rules the host enforces ("# Developing MCP Apps").
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

PLUGIN_NAME = "antigravity-3d"
SCHEMA_PATH = Path(__file__).with_name("runtime") / "sidecar.schema.json"

PLUGIN_JSON_FIELDS = {"name", "description", "logo", "suggestedPrompts", "displayName", "version", "author",
                      "homepage", "repository", "disabled"}
HOOK_EVENTS_GROUPED = {"PreToolUse", "PostToolUse"}
HOOK_EVENTS_FLAT = {"SessionStart", "PreInvocation", "PostInvocation", "Stop"}
RULE_TRIGGERS = {"always_on", "glob", "model_decision", "manual"}
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CRON_FIELDS = re.compile(r"^(CRON_TZ=\S+ |TZ=\S+ )?(@\w+|@every \S+|(\S+ ){4}\S+)$")


# --------------------------------------------------------------------------- locating the bundle


def bundle_dir() -> Path:
    """The source bundle: the repo checkout first, then packaged data."""
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "plugin" / PLUGIN_NAME, here.parent / "_plugin_bundle" / PLUGIN_NAME):
        if (cand / "plugin.json").is_file():
            return cand
    raise FileNotFoundError(
        "antigravity-3d plugin bundle not found (expected plugin/antigravity-3d in the repo, or "
        "antigravity3d/_plugin_bundle/antigravity-3d in an installed wheel)")


# --------------------------------------------------------------------------- install


def _is_our_plugin(path: Path) -> bool:
    try:
        return json.loads((path / "plugin.json").read_text(encoding="utf-8")).get("name") == PLUGIN_NAME
    except (OSError, ValueError, AttributeError):
        return False


def _bind_python_launchers(dest: Path) -> None:
    """Bind copied launchers to the interpreter that owns the installed runtime."""
    # Resolving the symlink would escape a venv and lose its installed packages.
    python = str(Path(sys.executable).absolute())
    mcp_file = dest / "mcp_config.json"
    config = json.loads(mcp_file.read_text())
    studio = config["mcpServers"]["studio"]
    studio.update(command=python, args=["-m", "antigravity3d.runtime.mcp_server"])
    mcp_file.write_text(json.dumps(config, indent=2) + "\n")

    for path in sorted((dest / "sidecars").glob("*/sidecar.json")):
        sidecar = json.loads(path.read_text())
        args = sidecar.get("args", [])
        if sidecar.get("command") == "uv" and args[:3] == ["run", "python", "-m"]:
            sidecar.update(command=python, args=args[2:])
        elif path.parent.name == "weekly_distill":
            old = "uv run python -m antigravity3d.runtime.distill_job"
            command = shlex.join([python, "-m", "antigravity3d.runtime.distill_job"])
            sidecar["args"] = [arg.replace(old, command) for arg in args]
            grants = sidecar["agent_permissions"]["access_grants"]
            sidecar["agent_permissions"]["access_grants"] = [g.replace(old, command) for g in grants]
        else:
            continue
        path.write_text(json.dumps(sidecar, indent=2) + "\n")

    hook_file = dest / "hooks.json"
    hooks = json.loads(hook_file.read_text())
    for group in hooks.values():
        for event, items in group.items():
            if event == "enabled":
                continue
            for item in items:
                for handler in item.get("hooks", [item]):
                    argv = shlex.split(handler["command"])
                    if argv[0] == "python3":
                        handler["command"] = shlex.join([python, *argv[1:]])
    hook_file.write_text(json.dumps(hooks, indent=2) + "\n")


def _personalize(dest: Path, workspace: Path | None, depot: Path | None) -> None:
    """Fills install-time absolute paths into sidecar permissions (narrowest documented grants)."""
    for sc in sorted((dest / "sidecars").glob("*/sidecar.json")):
        cfg = json.loads(sc.read_text(encoding="utf-8"))
        spawns_agents = cfg.get("builtin") == "schedule" or sc.parent.name == "depot_watcher"
        if not spawns_agents:
            continue
        perms = cfg.setdefault("agent_permissions", {})
        if workspace is not None:
            perms["workspace_uris"] = [workspace.resolve().as_uri()]
        if depot is not None and sc.parent.name == "depot_watcher":
            cfg.setdefault("env", {})["AGY3D_DEPOT_DIR"] = str(depot.resolve())
            grants = perms.setdefault("access_grants", [])
            g = f"read_file({depot.resolve()})"
            if g not in grants:
                grants.append(g)
        sc.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


AUTHORING = ("ir", "script")
# Script mode's texts (S/r6/CONTRACT.md section 5), shipped as package data and not inside the default bundle: an
# installed bundle in script mode has them in place of rules/AGENTS.md and the parametric-kitbash skill.
SCRIPT_MODE_DIR = Path(__file__).with_name("build") / "plugin_script_mode"
AUTHORING_FILE = "authoring"  # one line, "script", read by the hook scripts (hooks/scripts/_common.authoring)
# Skills whose steps are tools script mode does not serve (forge_pbr_material, apply_shader_diff, run_paintover_loop,
# render_lookdev; set_stage, export_engine_package, run_usd_validator; open_asset_session, run_visual_qa): an agent that
# read one would be sent to tools that do not exist. The script-mode kitbash page covers materials (a.material),
# export (deliver_package) and a mesh file the user already has (a.load; the sanitizer skill's per-file steps are
# apply_scene_ir, set_stage and generate_lods_and_colliders).
# scroll-3d-website stays: its own steps are deliver_package, install_into_project and check_site, which both modes
# serve, and it sends the modelling to parametric-kitbash, which this mode replaces with the script page.
SCRIPT_MODE_DROPPED_SKILLS = ("pbr-material-forge", "engine-truth-verify", "concept-to-blueprint", "game-ready-sanitizer")


def _script_mode(dest: Path) -> None:
    """Turn a copied bundle into script mode: the server's env, the two texts the agent reads, the hooks' marker."""
    mcp_file = dest / "mcp_config.json"
    config = json.loads(mcp_file.read_text())
    config["mcpServers"]["studio"].setdefault("env", {})["AGY3D_AUTHORING"] = "script"
    mcp_file.write_text(json.dumps(config, indent=2) + "\n")
    shutil.copyfile(SCRIPT_MODE_DIR / "AGENTS.md", dest / "rules" / "AGENTS.md")
    shutil.copyfile(SCRIPT_MODE_DIR / "SKILL.md", dest / "skills" / "parametric-kitbash" / "SKILL.md")
    for name in SCRIPT_MODE_DROPPED_SKILLS:
        shutil.rmtree(dest / "skills" / name, ignore_errors=True)
    # The two sub-agents (usd_verifier, lookdev_critic) call inspect_manifest, run_usd_validator and qa_probe, which
    # script mode does not serve; the harness globs agents/*.md, so an absent folder is no error.
    shutil.rmtree(dest / "agents", ignore_errors=True)
    # The glob-scoped rule on budgets named export_engine_package, open_asset_session, engine-truth-verify and
    # generate_lods_and_colliders: the script-mode copy says the same for a script (the Asset line, deliver_package).
    shutil.copyfile(SCRIPT_MODE_DIR / "engine-budgets.md", dest / "rules" / "engine-budgets.md")
    (dest / AUTHORING_FILE).write_text("script\n", encoding="utf-8")


def install(target_root: Path | None = None, *, symlink: bool = False,
            workspace: Path | None = None, depot: Path | None = None, authoring: str = "ir") -> Path:
    """Copies (or symlinks) the bundle to ``<target_root>/plugins/antigravity-3d`` and returns that path.

    ``target_root`` is a customization root; default ``<cwd>/.agents`` (workspace scope).
    Use ``Path.home() / ".gemini/config"`` for a global install. ``workspace`` / ``depot``
    write absolute ``workspace_uris`` and the depot ``read_file`` grant into the copied
    sidecars (copy mode only; a symlinked bundle is the repo itself). ``authoring="script"``: script mode (an asset is a
    Python script run by ``run_build``; copy mode only); "ir" (the default) installs the bundle as it is.
    """
    src = bundle_dir()
    root = Path(target_root) if target_root is not None else Path.cwd() / ".agents"
    dest = root / "plugins" / PLUGIN_NAME
    if symlink and (workspace is not None or depot is not None):
        raise ValueError("workspace/depot personalisation needs a copy install, not symlink=True")
    if authoring not in AUTHORING:
        raise ValueError(f"authoring must be one of {', '.join(AUTHORING)}")
    if symlink and authoring != "ir":
        raise ValueError("script authoring changes files of the bundle: it needs a copy install, not symlink=True")
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        if not _is_our_plugin(dest):
            raise FileExistsError(f"{dest} exists and is not an antigravity-3d plugin; refusing to replace it")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if symlink:
        dest.symlink_to(src.resolve(), target_is_directory=True)
    else:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        _bind_python_launchers(dest)
        _personalize(dest, workspace, depot)
        if authoring == "script":
            _script_mode(dest)
    return dest


def register(workspace: Path, plugins_parent: Path | None = None) -> Path:
    """Alternative to install(): adds the bundle's parent dir to ``<workspace>/.agents/plugins.json``.

    Per the JSON Configurations docs, each ``entries[].path`` is scanned one level deep.
    """
    parent = (plugins_parent or bundle_dir().parent).resolve()
    cfg_path = Path(workspace) / ".agents" / "plugins.json"
    cfg: dict[str, Any] = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    entries = cfg.setdefault("entries", [])
    if not any(isinstance(e, dict) and Path(str(e.get("path", ""))).expanduser() == parent for e in entries):
        entries.append({"path": str(parent), "include_only": [PLUGIN_NAME]})
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg_path


# --------------------------------------------------------------------------- schema check (stdlib subset)


def load_sidecar_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


_TYPES: dict[str, Any] = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float),
                          "integer": int}


def schema_errors(inst: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validates the JSON-Schema subset the vendored schema uses (type, enum, properties,
    required, additionalProperties, items, oneOf, not)."""
    errs: list[str] = []
    t = schema.get("type")
    if t:
        ok = isinstance(inst, _TYPES[t]) and not (t in {"number", "integer"} and isinstance(inst, bool))
        if not ok:
            return [f"{path}: expected {t}, got {type(inst).__name__}"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in {schema['enum']}")
    if isinstance(inst, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in inst:
                errs.append(f"{path}: missing required '{req}'")
        addl = schema.get("additionalProperties", True)
        for k, v in inst.items():
            if k in props:
                errs += schema_errors(v, props[k], f"{path}.{k}")
            elif addl is False:
                errs.append(f"{path}: unknown property '{k}'")
            elif isinstance(addl, dict):
                errs += schema_errors(v, addl, f"{path}.{k}")
    if isinstance(inst, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(inst):
            errs += schema_errors(v, schema["items"], f"{path}[{i}]")
    if "oneOf" in schema:
        n = sum(1 for s in schema["oneOf"] if not schema_errors(inst, s, path))
        if n != 1:
            errs.append(f"{path}: must match exactly one of oneOf (matched {n}); command and builtin are exclusive")
    if "not" in schema and not schema_errors(inst, schema["not"], path):
        errs.append(f"{path}: matches a forbidden schema")
    return errs


# --------------------------------------------------------------------------- validate


def _frontmatter(text: str) -> dict[str, Any] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return None
    data = yaml.safe_load(parts[0][3:]) or {}
    return data if isinstance(data, dict) else None


def _check_sidecar(path: Path, cfg: Any, schema: dict[str, Any], rel: str) -> list[str]:
    errs = [f"{rel}: {e}" for e in schema_errors(cfg, schema)]
    if not isinstance(cfg, dict):
        return errs
    if cfg.get("builtin") == "schedule":
        args = cfg.get("args") or []
        if not args or not isinstance(args[0], str) or not _CRON_FIELDS.match(args[0]):
            errs.append(f"{rel}: schedule args[0] must be the cron schedule string")
        if cfg.get("restart_policy", "always") != "always":
            errs.append(f"{rel}: scheduled sidecars must use restart_policy 'always'")
        if args[1:3] == ["agentapi", "new-conversation"] and "--" not in args[3:-1]:
            errs.append(f"{rel}: put '--' before the prompt of agentapi new-conversation")
    if cfg.get("has_web_ui"):
        views = (cfg.get("ui_config") or {}).get("views") or []
        if not views:
            errs.append(f"{rel}: has_web_ui sidecars need ui_config.views")
    for g in (cfg.get("agent_permissions") or {}).get("access_grants", []):
        if re.fullmatch(r"(command|read_file|write_file|mcp|read_url|execute_url)\((\*|/)?\)", str(g)):
            errs.append(f"{rel}: over-broad access grant {g!r}")
    return errs


def _check_hooks(plugin_dir: Path, cfg: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(cfg, dict):
        return ["hooks.json: top level must be an object of named hooks"]
    for hook_name, spec in cfg.items():
        if not isinstance(spec, dict):
            errs.append(f"hooks.json: {hook_name} must be an object")
            continue
        for event, handlers in spec.items():
            where = f"hooks.json: {hook_name}.{event}"
            if event == "enabled":
                if not isinstance(handlers, bool):
                    errs.append(f"{where} must be a boolean")
                continue
            if event not in HOOK_EVENTS_GROUPED | HOOK_EVENTS_FLAT:
                errs.append(f"{where}: unknown event (documented: {sorted(HOOK_EVENTS_GROUPED | HOOK_EVENTS_FLAT)})")
                continue
            if not isinstance(handlers, list):
                errs.append(f"{where} must be a list")
                continue
            flat: list[Any] = []
            for item in handlers:
                if event in HOOK_EVENTS_GROUPED:
                    if not isinstance(item, dict) or "matcher" not in item or not isinstance(item.get("hooks"), list):
                        errs.append(f"{where}: tool events need {{matcher, hooks: [...]}} groups")
                        continue
                    if item["matcher"] not in ("*", ""):
                        try:
                            re.compile(item["matcher"])
                        except re.error as e:
                            errs.append(f"{where}: bad matcher regex: {e}")
                    flat += item["hooks"]
                else:
                    flat.append(item)
            for h in flat:
                if not isinstance(h, dict) or not isinstance(h.get("command"), str):
                    errs.append(f"{where}: handler needs a string 'command'")
                    continue
                if h.get("type", "command") != "command":
                    errs.append(f"{where}: only type 'command' is supported")
                if "timeout" in h and not (isinstance(h["timeout"], int) and h["timeout"] > 0):
                    errs.append(f"{where}: timeout must be a positive integer (seconds)")
                for tok in shlex.split(h["command"]):
                    if tok.startswith("./") or (not tok.startswith("-") and tok.endswith((".py", ".sh"))):
                        if not (plugin_dir / tok).is_file():
                            errs.append(f"{where}: command references missing file {tok}")
    return errs


MCP_SERVER_FIELDS = {"command", "args", "env", "url", "serverUrl"}  # "# MCP Servers (`mcp_config.json`)" in the docs
_MCP_GRANT = re.compile(r"^mcp\((?P<server>[^/()]+)/(?P<tool>[^()]+)\)$")


def _check_mcp_config(plugin_dir: Path, cfg: Any, plugin_name: str) -> list[str]:
    """Checks ``mcp_config.json`` against the documented schema (stdio: command/args/env; remote: url)."""
    import importlib.util

    errs: list[str] = []
    servers = cfg.get("mcpServers") if isinstance(cfg, dict) else None
    if not isinstance(servers, dict) or not servers:
        return ["mcp_config.json: needs a non-empty 'mcpServers' object (server id -> config)"]
    for name, sc in servers.items():
        where = f"mcp_config.json: mcpServers.{name}"
        if re.search(r"[\s/()]", name):
            errs.append(f"{where}: server id must not contain whitespace, '/' or parentheses (mcp(<server>/<tool>) grants)")
        if name.startswith(plugin_name):
            errs.append(f"{where}: plugin servers are namespaced <plugin>_<server> by the harness; do not repeat the plugin name")
        if not isinstance(sc, dict):
            errs.append(f"{where} must be an object")
            continue
        for k in sorted(set(sc) - MCP_SERVER_FIELDS):
            errs.append(f"{where}: unknown field '{k}' (documented: {sorted(MCP_SERVER_FIELDS)})")
        stdio, remote = "command" in sc, bool({"url", "serverUrl"} & set(sc))
        if stdio == remote:
            errs.append(f"{where}: needs exactly one transport: stdio 'command' or Streamable HTTP 'url'")
        if stdio:
            if not isinstance(sc["command"], str) or not sc["command"]:
                errs.append(f"{where}: command must be a non-empty string")
            args = sc.get("args", [])
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                errs.append(f"{where}: args must be a list of strings")
            else:
                for flag, mod in zip(args, args[1:]):
                    if flag != "-m":
                        continue
                    try:
                        found = importlib.util.find_spec(mod) is not None
                    except (ImportError, ValueError):  # missing parent package
                        found = False
                    if not found:
                        errs.append(f"{where}: module {mod} is not importable (package not installed?)")
            env = sc.get("env", {})
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                errs.append(f"{where}: env must map strings to strings")
            if isinstance(args, list) and STUDIO_SERVER_MODULE in args:
                errs += [f"{where}: {e}" for e in _check_mcp_apps()]
        elif remote and not str(sc.get("url") or sc.get("serverUrl") or "").startswith(("http://", "https://")):
            errs.append(f"{where}: url must be an http(s) URL")
    namespaced = {f"{plugin_name}_{n}" for n in servers}
    for sc_file in sorted((plugin_dir / "sidecars").glob("*/sidecar.json")):
        try:
            grants = (json.loads(sc_file.read_text(encoding="utf-8")).get("agent_permissions") or {}).get("access_grants", [])
        except ValueError:
            continue
        for g in grants:
            if str(g).startswith("mcp("):
                m = _MCP_GRANT.match(str(g))
                if not m or m["server"] not in namespaced:
                    errs.append(f"{sc_file.relative_to(plugin_dir)}: grant {g!r} must be mcp(<plugin>_<server>/<tool>) "
                                f"with a server from mcp_config.json ({sorted(namespaced)})")
    return errs


STUDIO_SERVER_MODULE = "antigravity3d.runtime.mcp_server"


def _check_mcp_apps() -> list[str]:
    """Builds the studio MCP server in-process and checks its MCP App ``_meta`` and ``ui://`` resources."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from mcp.server.mcpserver import MCPServer

    from antigravity3d.runtime.inspector_server import ui_meta_problems
    from antigravity3d.runtime.mcp_server import build_server

    async def listing() -> tuple[list[Any], list[Any]]:
        server = build_server()
        return await MCPServer.list_tools(server), await server.list_resources()  # every tool, not the stage view

    with ThreadPoolExecutor(1) as pool:  # validate() may run inside an event loop
        tools, resources = pool.submit(asyncio.run, listing()).result()
    return ui_meta_problems([(t.name, t.meta) for t in tools], [(str(r.uri), r.mime_type) for r in resources])


def validate(plugin_dir: Path) -> list[str]:
    """Returns a list of problems; empty means the bundle is valid."""
    plugin_dir = Path(plugin_dir)
    errs: list[str] = []
    if not (plugin_dir / "plugin.json").is_file():
        return [f"{plugin_dir}: plugin.json missing"]
    parsed: dict[Path, Any] = {}
    for jf in sorted(plugin_dir.rglob("*.json")):
        try:
            parsed[jf] = json.loads(jf.read_text(encoding="utf-8"))
        except ValueError as e:
            errs.append(f"{jf.relative_to(plugin_dir)}: invalid JSON: {e}")

    manifest = parsed.get(plugin_dir / "plugin.json")
    if isinstance(manifest, dict):
        unknown = set(manifest) - PLUGIN_JSON_FIELDS
        if unknown:
            errs.append(f"plugin.json: unknown top-level fields are silently dropped: {sorted(unknown)}")
        if "name" in manifest and not _KEBAB.match(str(manifest["name"])):
            errs.append("plugin.json: name must be lowercase kebab-case")
        for k in ("homepage",):
            if k in manifest and not str(manifest[k]).startswith("https://"):
                errs.append(f"plugin.json: {k} must be an https:// URL")
        if len(manifest.get("suggestedPrompts", [])) > 3:
            errs.append("plugin.json: at most 3 suggestedPrompts are used")
    elif manifest is not None:
        errs.append("plugin.json: must be an object")

    if (plugin_dir / "hooks" / "hooks.json").exists():
        errs.append("hooks/hooks.json: native plugins must put hooks.json at the plugin root")
    if (plugin_dir / "hooks.json") in parsed:
        errs += _check_hooks(plugin_dir, parsed[plugin_dir / "hooks.json"])

    if (plugin_dir / "mcp_config.json") in parsed:
        plugin_name = manifest.get("name", plugin_dir.name) if isinstance(manifest, dict) else plugin_dir.name
        errs += _check_mcp_config(plugin_dir, parsed[plugin_dir / "mcp_config.json"], str(plugin_name))

    schema = load_sidecar_schema()
    for jf, cfg in parsed.items():
        if jf.name == "sidecar.json":
            errs += _check_sidecar(jf, cfg, schema, str(jf.relative_to(plugin_dir)))

    for skill_dir in sorted(p for p in (plugin_dir / "skills").glob("*") if p.is_dir()):
        rel = f"skills/{skill_dir.name}/SKILL.md"
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            errs.append(f"{rel}: missing")
            continue
        fm = _frontmatter(md.read_text(encoding="utf-8"))
        if fm is None:
            errs.append(f"{rel}: missing YAML frontmatter")
            continue
        if fm.get("name") != skill_dir.name:
            errs.append(f"{rel}: frontmatter name must equal the folder name '{skill_dir.name}'")
        if not str(fm.get("description") or "").strip():
            errs.append(f"{rel}: description is required for skill discovery")

    for agent_md in sorted((plugin_dir / "agents").glob("*.md")):
        rel = f"agents/{agent_md.name}"
        fm = _frontmatter(agent_md.read_text(encoding="utf-8"))
        if fm is None:
            errs.append(f"{rel}: missing YAML frontmatter")
            continue
        for key in ("name", "description"):
            if not str(fm.get(key) or "").strip():
                errs.append(f"{rel}: '{key}' is required")
        if "tools" in fm and not isinstance(fm["tools"], list):
            errs.append(f"{rel}: tools must be a list")

    for rule in sorted((plugin_dir / "rules").glob("*.md")):
        rel = f"rules/{rule.name}"
        text = rule.read_text(encoding="utf-8")
        if len(text.encode()) > 24 * 1024:
            errs.append(f"{rel}: rule files are capped at 24 KB")
        fm = _frontmatter(text)
        if rule.name in {"AGENTS.md", "GEMINI.md"}:
            if fm is not None:
                errs.append(f"{rel}: plugin AGENTS.md is plain markdown without frontmatter")
            continue
        if fm is None:
            continue
        trig = fm.get("trigger")
        if trig not in RULE_TRIGGERS:
            errs.append(f"{rel}: trigger must be one of {sorted(RULE_TRIGGERS)}")
        if trig == "glob" and not (isinstance(fm.get("globs"), str) and fm["globs"].strip()):
            errs.append(f"{rel}: trigger glob needs 'globs' as one comma-separated string")
        if trig == "model_decision" and not str(fm.get("description") or "").strip():
            errs.append(f"{rel}: trigger model_decision needs a description")

    wf_files = sorted((plugin_dir / "workflows").glob("*.py"))
    if wf_files:
        try:
            from google.antigravity import workflows
        except ImportError:  # pragma: no cover - SDK is a hard dependency
            errs.append("workflows: google.antigravity not importable; cannot validate scripts")
        else:
            for wf in wf_files:
                try:
                    workflows.validate_workflow_source(wf.read_text(encoding="utf-8"))
                except workflows.WorkflowError as e:
                    errs.append(f"workflows/{wf.name}: {e}")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="antigravity3d.plugin_install", description="Install the antigravity-3d plugin.")
    ap.add_argument("--validate", type=Path, metavar="PLUGIN_DIR", help="only validate a bundle and exit")
    ap.add_argument("--target", type=Path, default=None, help="customization root (default ./.agents)")
    ap.add_argument("--symlink", action="store_true")
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--depot", type=Path, default=None)
    ap.add_argument("--authoring", choices=AUTHORING, default="ir",
                    help="script: an asset is a Python script run by run_build (default ir: today's scene tools)")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["install"]:  # `agy3d-plugin install ...` reads as the plain command
        argv = argv[1:]
    ns = ap.parse_args(argv)
    if ns.validate:
        problems = validate(ns.validate)
        for p in problems:
            print(p)
        print("OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    dest = install(ns.target, symlink=ns.symlink, workspace=ns.workspace, depot=ns.depot, authoring=ns.authoring)
    problems = validate(dest)
    print(dest)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
