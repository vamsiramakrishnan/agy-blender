#!/usr/bin/env python
"""Containment for the stock-agent arms (V, A, B and N) of ``compare_agents.py``.

A stock Antigravity agent runs shell commands and Python a model wrote. It runs here inside Linux namespaces, set up
by this file in three steps (the file is self-contained: stdlib plus the SDK, because it is copied into the sandbox):

1. ``command(spec)`` (host side): the ``unshare`` command line and the scrubbed environment that start step 2.
   New mount, network, PID, IPC and UTS namespaces; ``/proc`` remounted for the new PID namespace.
2. ``bootstrap`` (PID 1 of the new namespaces, still root): hides the host and puts back only what the run needs,
   brings up loopback (the only network interface), drops to ``nobody`` with every capability gone and
   ``no_new_privs`` set, and execs step 3.

   ===================  ===========================================================  ======
   path in the sandbox  what it is                                                    mode
   ===================  ===========================================================  ======
   /home/agent          the run's HOME (empty)                                        rw
   /home/agent/project  the agent's workspace (the run's ``work/``; output goes here)  rw
   /opt/blender         Blender 5.3 (``blender`` on PATH, ``$BLENDER_EXE``)            ro
   /opt/pydeps          the SDK's site-packages (only the SDK process uses it)        ro
   /opt/pydeps/antigravity3d  an empty read-only tmpfs: hides this project's plugin  ro
   /opt/agyctl          this file and the run config                                 ro
   /run/agy             the model relay's unix socket                                rw
   /tmp                 private tmpfs                                                 rw
   ===================  ===========================================================  ======

   Arm V: ``/opt/blender`` is not on PATH and ``$BLENDER_EXE`` is unset; its MCP server (``/opt/agyctl/
   compare_vanilla_mcp.py``) starts Blender under an Xvfb display inside the sandbox. Arm N: this project's src,
   plugin and engines are bound read-only (``plugin_attachment``).

   ``/home``, ``/root``, ``/tmp``, ``/opt``, ``/mnt``, ``/run``, ``/srv``, ``/media`` and ``/old_root`` are replaced
   by empty tmpfs mounts first, so the repository, the upstream clones, the worktrees, git credentials and the
   session's files are not in the sandbox's file tree at all. ``/usr``, ``/etc``, ``/var``, ``/nix`` stay visible
   read-only to ``nobody`` (system software).
3. ``runner`` (as ``nobody``): a byte pump from ``127.0.0.1:<port>`` to the relay's unix socket (the network
   namespace has nothing else), then one ``Agent.chat`` with the stock configuration, and a JSON-lines event stream
   to the host on an inherited pipe (fd 3) that the harness subprocess does not inherit.

The API key never enters the sandbox: the agent's endpoint is the local relay with a placeholder key, and the
relay (host side, ``compare_agents.Relay``) adds the real key, counts every call against the run's share and logs
every model response, including each function call (every command the model asked for), outside the sandbox.
"""
from __future__ import annotations

import json
import os
import sys

NOBODY = 65534
SANDBOX_PATH = "/opt/blender:/usr/local/bin:/usr/bin:/bin"
HIDE = ("/home", "/root", "/tmp", "/opt", "/mnt", "/run", "/srv", "/media", "/old_root")
STAGE = "/mnt"  # staging tmpfs (itself one of the hidden paths)
WORKSPACE = "/home/agent/project"
HOME = "/home/agent"
CTL = "/opt/agyctl"
SOCK_DIR = "/run/agy"
PYDEPS = "/opt/pydeps"
BLENDER_DIR = "/opt/blender"
EVENT_FD = 3
# Arm C only (the plugin arm): the project's source and the engines its MCP server drives, all read-only.
PROJECT_SRC = "/opt/agy3d/src"
PROJECT_PLUGIN = "/opt/agy3d/plugin"  # the tree's plugin bundle beside src: the server reads its styles from there
PLUGINS_DIR = f"{WORKSPACE}/.agents/plugins"  # the plugin's documented workspace location (bound read-only)
GODOT_DIR = "/opt/godot"
BROWSERS_DIR = "/opt/pw-browsers"
THREE_DIR = "/opt/three"
POOL_SOCKET = "/tmp/agy3d/pool.sock"


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The whole environment of everything in the sandbox: an allow-list, nothing inherited from the host."""
    env = {"PATH": SANDBOX_PATH, "HOME": HOME, "TMPDIR": "/tmp", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "BLENDER_EXE": f"{BLENDER_DIR}/blender", "LIBGL_ALWAYS_SOFTWARE": "1", "OMP_NUM_THREADS": "2",
           "OPENBLAS_NUM_THREADS": "2", "PYTHONDONTWRITEBYTECODE": "1", "TERM": "dumb"}
    env.update(extra or {})
    return env


# ----------------------------------------------------------------------------------------------- step 1 (host)


def command(spec: dict, python: str = "/usr/local/bin/python3") -> tuple[list[str], dict[str, str]]:
    """``unshare`` argv and env for a run described by ``spec`` (see ``compare_agents.stock_spec``)."""
    argv = ["unshare", "--mount", "--net", "--pid", "--ipc", "--uts", "--fork", "--mount-proc",
            "--propagation", "private", python, "-I", spec["host_sandbox_py"], "bootstrap", spec["host_config"]]
    return argv, scrubbed_env()


# ----------------------------------------------------------------------------------------------- step 2 (root, PID 1)


def _mount(*args: str) -> None:
    import subprocess

    subprocess.run(["mount", *args], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)


def _loopback_up() -> None:
    import fcntl
    import socket
    import struct

    siocgifflags, siocsifflags, iff_up = 0x8913, 0x8914, 0x1
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        flags = struct.unpack("16sH", fcntl.ioctl(s, siocgifflags, struct.pack("16sH", b"lo", 0)))[1]
        fcntl.ioctl(s, siocsifflags, struct.pack("16sH", b"lo", flags | iff_up))


def bootstrap(config_path: str) -> None:
    cfg = json.loads(open(config_path).read())
    h = cfg["host"]
    binds = {  # staging name -> (host source, final path, read-only)
        "pydeps": (h["pydeps"], PYDEPS, True), "blender": (h["blender_dir"], BLENDER_DIR, True),
        "ctl": (h["ctl"], CTL, True), "home": (h["home"], HOME, False), "work": (h["work"], WORKSPACE, False),
        "sock": (h["sock_dir"], SOCK_DIR, False),
    }
    if h.get("venv"):  # the interpreter the plugin's installer wrote into its launchers, at the same path
        binds["venv"] = (h["venv"], h["venv"], True)
    for name, dst in (("project_src", PROJECT_SRC), ("project_plugin", PROJECT_PLUGIN), ("godot_dir", GODOT_DIR), ("browsers_dir", BROWSERS_DIR),
                      ("three_dir", THREE_DIR), ("plugins", PLUGINS_DIR)):  # plugins: inside the workspace, so last
        if h.get(name):
            binds[name] = (h[name], dst, True)
    _mount("-t", "tmpfs", "-o", "mode=0755,size=1m", "tmpfs", STAGE)
    for name, (src, _, _) in binds.items():
        os.makedirs(f"{STAGE}/{name}")
        _mount("--rbind", src, f"{STAGE}/{name}")
    for path in HIDE:
        if path != STAGE and os.path.isdir(path):
            _mount("-t", "tmpfs", "-o", "mode=1777,size=4g" if path == "/tmp" else "mode=0755,size=16m", "tmpfs", path)
    for name, (_, dst, ro) in binds.items():
        os.makedirs(dst, exist_ok=True)
        _mount("--move", f"{STAGE}/{name}", dst)
        if ro:
            _mount("-o", "remount,bind,ro", dst)
    if os.path.isdir(f"{PYDEPS}/antigravity3d"):  # the project's plugin bundle ships inside its installed package
        _mount("-t", "tmpfs", "-o", "ro,mode=0555,size=16k", "tmpfs", f"{PYDEPS}/antigravity3d")
    _loopback_up()
    import resource

    resource.setrlimit(resource.RLIMIT_NPROC, (1024, 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 << 30, 4 << 30))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.chdir(WORKSPACE)
    argv = ["setpriv", f"--reuid={NOBODY}", f"--regid={NOBODY}", "--clear-groups", "--inh-caps=-all",
            "--bounding-set=-all", "--no-new-privs", "--", "/usr/local/bin/python3", "-I", f"{CTL}/compare_sandbox.py",
            "runner", f"{CTL}/config.json"]
    os.execvpe("setpriv", argv, sandbox_env(cfg))


def sandbox_env(cfg: dict) -> dict[str, str]:
    """The environment of everything in the sandbox. Arm V (``cfg["vanilla"]``): Blender is not on PATH and not named
    in the environment; its MCP server is how it reaches Blender (the binary stays mounted for the server to start)."""
    env = scrubbed_env(cfg.get("extra_env"))
    if cfg.get("vanilla"):
        env.pop("BLENDER_EXE", None)
        env["PATH"] = ":".join(p for p in env["PATH"].split(":") if p != BLENDER_DIR)
    return env


# ----------------------------------------------------------------------------------------------- step 3 (nobody)


class Events:
    def __init__(self, fd: int = EVENT_FD) -> None:
        try:
            self.f = os.fdopen(fd, "w", buffering=1)
        except OSError:
            self.f = sys.stdout

    def __call__(self, kind: str, **data: object) -> None:
        import time

        try:
            self.f.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **data}, default=str) + "\n")
        except (OSError, ValueError):
            pass


def _pump(a, b) -> None:
    try:
        while chunk := a.recv(65536):
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(2)
            except OSError:
                pass


def start_forwarder(sock_path: str) -> str:
    """127.0.0.1:<port> -> the relay's unix socket. Returns the base URL."""
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)

    def accept() -> None:
        while True:
            conn, _ = srv.accept()
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                up.connect(sock_path)
            except OSError:
                conn.close()
                continue
            threading.Thread(target=_pump, args=(conn, up), daemon=True).start()
            threading.Thread(target=_pump, args=(up, conn), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}"


def _short(v: object, n: int = 4000) -> object:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"...[{len(v) - n} more chars]"
    if isinstance(v, dict):
        return {k: _short(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_short(x, n) for x in v]
    return v


# Commands naming these are refused by policy. This is discouragement only (a string test is easy to evade); what
# prevents access is that none of these exist in the sandbox's file tree or environment.
DISCOURAGED = ("/home/user", "agy-blender", "el4cteo", "arjun988", "/root", "GEMINI_API_KEY", ".git-credentials",
               "/proc/1/environ", "claude-0")


# ----------------------------------------------------------------------------------------------- the plugin arm
#
# The headless harness discovers the plugin from none of its documented install locations (measured: see
# compare_agents.PLUGIN_DISCOVERY), so each part is attached here through its SDK equivalent, from the plugin's own
# files, unmodified: rules -> system instruction text appended to the stock template; skills -> skills_paths;
# mcp_config.json -> mcp_servers; hooks.json -> SDK hooks that run the plugin's hook scripts with the documented JSON
# contract; agents/*.md -> subagents; sidecars/blender_pool -> started here before the agent.


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    head, _, body = text[3:].partition("\n---")
    meta: dict = {}
    key = None
    for line in head.splitlines():
        if not line.strip():
            continue
        if line[:1] in " \t" and key:  # folded continuation (">-" blocks)
            meta[key] = (str(meta.get(key) or "") + " " + line.strip()).strip()
            continue
        k, _, v = line.partition(":")
        key, v = k.strip(), v.strip()
        meta[key] = "" if v in (">-", ">", "|") else v.strip('"')
    return meta, body.lstrip("\n")


def plugin_rules_text(plugin: str) -> tuple[str, list[str]]:
    """Every rule file, unmodified, under a header saying when the IDE would apply it."""
    import glob

    parts, names = [], []
    for path in sorted(glob.glob(f"{plugin}/rules/*.md")):
        meta, body = _frontmatter(open(path).read())
        trigger = meta.get("trigger") or "always_on"
        when = {"always_on": "always on", "glob": f"applies to files matching {meta.get('globs', '')}",
                "model_decision": f"apply when relevant: {meta.get('description', '')}", "manual": "manual"}.get(trigger, trigger)
        parts.append(f"<rule name=\"{os.path.basename(path)}\" plugin=\"antigravity-3d\" when=\"{when}\">\n{body.strip()}\n</rule>")
        names.append(f"{os.path.basename(path)} ({trigger})")
    return "Rules from the enabled plugin antigravity-3d:\n\n" + "\n\n".join(parts), names


def _module_cmd(command: str, args: list[str]) -> list[str]:
    """The launcher as installed; only ``uv run ... python -m mod`` (the uninstalled form) becomes the sandbox's python
    running the same module, since there is no uv in the sandbox."""
    if command == "uv" and "-m" in args:
        i = args.index("-m")
        return ["/usr/local/bin/python3", "-m", *args[i + 1:]]
    return [command, *args]


def start_pool_sidecar(plugin: str, env: dict, emit: Events) -> object | None:
    import subprocess
    import time

    path = f"{plugin}/sidecars/blender_pool/sidecar.json"
    if not os.path.exists(path):
        return None
    sc = json.loads(open(path).read())
    cmd = _module_cmd(sc["command"], sc.get("args", []))
    full = env | {k: v for k, v in (sc.get("env") or {}).items()}
    os.makedirs(os.path.dirname(full.get("AGY3D_POOL_SOCKET", POOL_SOCKET)), exist_ok=True)
    proc = subprocess.Popen(cmd, env=full, stdin=subprocess.DEVNULL, stdout=open(f"{HOME}/pool_sidecar.log", "w"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    sock = full.get("AGY3D_POOL_SOCKET", POOL_SOCKET)
    t0 = time.monotonic()
    while not os.path.exists(sock) and proc.poll() is None and time.monotonic() - t0 < 180:
        time.sleep(0.5)
    emit("sidecar", name="blender_pool", cmd=cmd, socket=sock, up=os.path.exists(sock), exit=proc.poll(),
         wait_s=round(time.monotonic() - t0, 1))
    return proc


class HookScripts:
    """Runs the plugin's hooks.json scripts as the harness would: JSON on stdin, JSON on stdout, the plugin as cwd."""

    def __init__(self, plugin: str, conversation_id: str, env: dict, emit: Events) -> None:
        self.plugin, self.conv, self.env, self.emit = plugin, conversation_id, env, emit
        cfg = json.loads(open(f"{plugin}/hooks.json").read())
        self.by_event: dict[str, list[dict]] = {}
        for group in cfg.values():
            for event, entries in group.items():
                for e in entries:
                    if "hooks" in e:  # grouped: {matcher, hooks: [...]}
                        for h in e["hooks"]:
                            self.by_event.setdefault(event, []).append({"matcher": e.get("matcher", "*"), **h})
                    else:
                        self.by_event.setdefault(event, []).append({"matcher": "*", **e})

    def run(self, event: str, payload: dict, tool_name: str = "") -> list[dict]:
        import re
        import subprocess

        outs = []
        for h in self.by_event.get(event, []):
            m = h.get("matcher", "*")
            if m not in ("*", "") and not re.search(m, tool_name or ""):
                continue
            body = {"conversationId": self.conv, "workspacePaths": [WORKSPACE], "cwd": WORKSPACE,
                    "hookEventName": event, **payload}
            try:
                r = subprocess.run(h["command"], shell=True, cwd=self.plugin, input=json.dumps(body), capture_output=True,
                                   text=True, timeout=h.get("timeout", 10), env=self.env)
                out = json.loads(r.stdout or "{}")
            except Exception as e:  # noqa: BLE001 - the harness treats a failed hook as no decision
                out = {"_error": f"{type(e).__name__}: {e}"[:300]}
            self.emit("plugin_hook", event=event, command=h["command"], tool=tool_name, out=_short(out, 1000))
            outs.append(out)
        return outs


def _int(v: object) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def plugin_attachment(cfg: dict, url: str, emit: Events, conversation_id: str) -> dict:
    """LocalAgentConfig keyword arguments, a prompt prefix and background processes for the plugin arm."""
    import glob
    import subprocess

    from google.antigravity import types
    from google.antigravity.hooks import hooks

    pc = cfg["plugin"]
    plugin = pc["dir"]
    env = dict(os.environ) | {"PYTHONPATH": f"{PROJECT_SRC}:{PYDEPS}", "GEMINI_API_KEY": "via-relay",
                              "GOOGLE_GEMINI_BASE_URL": url, "DISPLAY": ":99"}
    procs = []
    if os.path.exists("/usr/bin/Xvfb"):  # the engines' renderers need a display; the host's X socket is not in here
        procs.append(subprocess.Popen(["/usr/bin/Xvfb", ":99", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True))
    os.environ["DISPLAY"] = ":99"
    if (pool := start_pool_sidecar(plugin, env, emit)) is not None:
        procs.append(pool)
    mcp = json.loads(open(f"{plugin}/mcp_config.json").read())["mcpServers"]
    servers = []
    for name, sc in mcp.items():
        cmd = _module_cmd(sc.get("command", ""), sc.get("args", []))
        servers.append(types.McpStdioServer(name=f"antigravity-3d_{name}", command=cmd[0], args=cmd[1:],
                                            env=env | (sc.get("env") or {})))
    rules, rule_names = plugin_rules_text(plugin)
    subagents = []
    for path in sorted(glob.glob(f"{plugin}/agents/*.md")):
        meta, body = _frontmatter(open(path).read())
        model = (meta.get("model") or "").replace("-low", "").replace("-medium", "").replace("-high", "") or None
        subagents.append(types.SubagentConfig(name=meta["name"], description=meta.get("description", ""),
                                              system_instructions=body, model=model))
    scripts = HookScripts(plugin, conversation_id, env, emit)
    limits = pc["limits"]

    def mcp_tool(tc) -> str:  # noqa: ANN001
        a = tc.args or {}
        return str(a.get("ToolName") or a.get("tool_name") or "")

    def hook_call(tc) -> tuple[dict, str]:  # noqa: ANN001
        """The tool call as the hooks contract documents it: an MCP call is the step ``mcp_tool`` with ServerName,
        ToolName and Arguments (the SDK hands it over under the MCP tool's own name)."""
        if mcp_tool(tc):
            return {"id": tc.id, "name": "mcp_tool", "args": dict(tc.args or {})}, "mcp_tool"
        return {"id": tc.id, "name": str(tc.name), "args": dict(tc.args or {})}, str(tc.name)

    class PluginPreTool(hooks.PreToolCallDecideHook):
        async def run(self, context, data):  # noqa: ANN001
            tool = mcp_tool(data)
            modified = None
            if tool in ("open_asset_session", "start_preview"):
                # Spending is not the agent's choice. Missing limits are filled with the run's share (what the person
                # running this pilot would answer); limits other than the run's share are refused with that answer.
                args = dict(data.args.get("Arguments") or {})
                wants = tool == "start_preview" or args.get("preview") in (True, "true", "True", 1) or "token_ceiling" in args
                if tool == "open_asset_session" and not wants:
                    args["preview"] = True
                given = (args.get("token_ceiling"), args.get("max_images"))
                if given != (None, None) and (_int(given[0]), _int(given[1])) != (limits["tokens"], limits["images"]):
                    emit("ask_answered", tool=tool, asked=given, answer="refused: not this run's limits")
                    return hooks.HookResult(allow=False, message=(
                        f"The user's limits for this preview are {limits['tokens']} tokens and {limits['images']} images; "
                        "use exactly those."))
                args.update(token_ceiling=limits["tokens"], max_images=limits["images"])
                if tool == "open_asset_session":
                    args["preview"] = True
                emit("limits_injected", tool=tool, arguments=args)
                modified = {"Arguments": args}
                data = data.model_copy(update={"args": {**data.args, **modified}})
            call, matched = hook_call(data)
            outs = scripts.run("PreToolUse", {"toolCall": call}, tool_name=matched)
            for o in outs:
                if o.get("decision") == "deny":
                    return hooks.HookResult(allow=False, message=o.get("reason") or "denied by the plugin's hook")
                if o.get("decision") == "ask":
                    a_ = (data.args.get("Arguments") or {})
                    ok = tool in ("open_asset_session", "start_preview") and \
                        (_int(a_.get("token_ceiling")), _int(a_.get("max_images"))) == (limits["tokens"], limits["images"])
                    # A person would be asked here. Nobody can answer a headless run, so the harness does, and approves
                    # only the run's own share.
                    emit("ask_answered", tool=tool or str(data.name), question=o.get("reason"), answer="approved" if ok else "refused")
                    if not ok:
                        return hooks.HookResult(allow=False, message=(o.get("reason") or "") +
                                                " [asked the user: refused]")
            return hooks.HookResult(allow=True, modified_args=modified) if modified else hooks.HookResult(allow=True)

    class PluginPostTool(hooks.PostToolCallHook):
        async def run(self, context, data):  # noqa: ANN001
            mcp = bool(data.server_name)
            call = ({"id": data.id, "name": "mcp_tool", "args": {"ServerName": data.server_name, "ToolName": str(data.name)}}
                    if mcp else {"id": data.id, "name": str(data.name), "args": {}})
            scripts.run("PostToolUse", {"toolCall": call, "error": data.error}, tool_name=call["name"])

    class PluginStop(hooks.StopHook):
        async def run(self, context, data):  # noqa: ANN001
            reason = "NO_TOOL_CALL" if str(data.stop_reason).endswith("UNSPECIFIED") and not data.error_message else str(data.stop_reason)
            for o in scripts.run("Stop", {"terminationReason": reason, "continuationActive": data.continuation_count > 0}):
                if o.get("decision") == "continue" and o.get("reason"):
                    return types.StopHookResult(decision=types.StopDecision.CONTINUE, reason=o["reason"])
            return types.StopHookResult()

    start = scripts.run("SessionStart", {})
    injected = [st.get("systemMessage", {}).get("systemMessage", "") for o in start for st in o.get("injectSteps", [])
                if isinstance(st, dict)]
    emit("plugin_attached", servers=[s_.name for s_ in servers], rules=rule_names, subagents=[a.name for a in subagents],
         hooks={k: len(v) for k, v in scripts.by_event.items()}, skills=f"{plugin}/skills", limits=limits,
         session_start=injected)
    return {"kwargs": {"mcp_servers": servers, "skills_paths": [f"{plugin}/skills"], "system_instructions": rules,
                       "subagents": subagents},
            "hooks": [PluginPreTool(), PluginPostTool(), PluginStop()],
            "prompt_prefix": "".join(f"[SessionStart hook] {m}\n\n" for m in injected if m), "procs": procs}


# ----------------------------------------------------------------------------------------------- arm V


VANILLA_SOCKET = "/tmp/vanilla_blender.sock"


def vanilla_attachment(cfg: dict, emit: Events) -> dict:
    """LocalAgentConfig keyword arguments and background processes for arm V: one MCP server (``compare_vanilla_mcp.py
    serve``, stdlib only) whose Blender runs in this sandbox under a virtual display. Nothing of this project."""
    import subprocess

    from google.antigravity import types

    vc = cfg["vanilla"]
    procs = []
    if os.path.exists("/usr/bin/Xvfb"):  # Workbench (the solid-viewport screenshot) needs OpenGL, so a display
        procs.append(subprocess.Popen(["/usr/bin/Xvfb", ":99", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True))
    env = dict(os.environ) | {"DISPLAY": ":99", "VANILLA_BLENDER": vc["blender"], "VANILLA_SOCKET": VANILLA_SOCKET}
    server = types.McpStdioServer(name="blender", command="/usr/local/bin/python3", args=["-I", vc["server"], "serve"],
                                  env=env)
    emit("vanilla_attached", server="blender", command=["/usr/local/bin/python3", "-I", vc["server"], "serve"],
         blender=vc["blender"], display=bool(procs), path=os.environ.get("PATH"))
    return {"kwargs": {"mcp_servers": [server]}, "procs": procs}


# Tool calls approved but not yet finished: a command the harness put in the background stays here until it ends
# (the post-tool hook fires when the command completes, not when it is backgrounded).
OPEN_CALLS: set = set()


def _background_state(history: list) -> tuple[set, set]:
    """Calls still running, and task ids the harness has sent a message about."""
    import re

    for step in history:  # a call that was refused or failed never reaches the post-tool hook
        if step.error or step.status.name == "ERROR":
            for c in step.tool_calls:
                OPEN_CALLS.discard(c.id)
    started, heard = set(OPEN_CALLS), set()
    for step in history:
        text = json.dumps(step.model_dump(mode="json"), default=str)
        started |= set(re.findall(r"running as a background task with task id: ([\w./-]+?task-\d+)", text))
        heard |= set(re.findall(r"sender=([\w./-]+?task-\d+)", text))
    return started, heard


async def wait_for_background(agent, emit: Events, *, deadline: float, idle_s: float = 20.0,  # noqa: ANN001
                              cap_s: float = 900.0) -> list[str]:
    """After a turn: wait while a background task has not reported back or the conversation is still moving (the harness
    resumes the agent itself when a task finishes). Returns the text of any later model replies."""
    import asyncio
    import time

    hist = agent.conversation.history
    seen, quiet, texts = len(hist), 0.0, []
    t_start = time.monotonic()
    deadline = min(deadline, t_start + cap_s)  # a command's own timeout is 10 minutes; never wait longer than this
    emit("background_wait_start", open_calls=sorted(OPEN_CALLS))
    while time.monotonic() < deadline:
        started, heard = _background_state(agent.conversation.history)
        pending = started  # open calls: a backgrounded command has not finished yet
        if not pending and quiet >= idle_s:
            break
        await asyncio.sleep(2)
        n = len(agent.conversation.history)
        if n != seen:
            for step in agent.conversation.history[seen:n]:
                if step.depth == 0 and step.type.name == "TEXT_RESPONSE" and step.content and step.is_complete_response:
                    texts.append(step.content)
            seen, quiet = n, 0.0
        else:
            quiet += 2
    started, heard = _background_state(agent.conversation.history)
    emit("background_wait", still_open=sorted(started), reported=sorted(heard), later_replies=len(texts),
         waited_s=round(time.monotonic() - t_start, 1))
    return texts


async def run_agent(cfg: dict, emit: Events) -> None:
    import asyncio
    import time

    sys.path.insert(0, PYDEPS)
    from google.antigravity import Agent, types
    from google.antigravity.connections.local.local_connection_config import LocalAgentConfig
    from google.antigravity.hooks import hooks, policy
    from google.antigravity.models import (DEFAULT_IMAGE_GENERATION_MODEL, DEFAULT_MODEL, GeminiAPIEndpoint,
                                           GeminiModelOptions, ModelTarget, ModelType)

    url = start_forwarder(f"{SOCK_DIR}/relay.sock")
    opts = GeminiModelOptions(max_output_tokens=cfg["max_output_tokens"]) if cfg.get("max_output_tokens") else None
    endpoint = GeminiAPIEndpoint(base_url=url, api_key="via-relay", options=opts)
    image_endpoint = GeminiAPIEndpoint(base_url=url, api_key="via-relay")
    text_model = cfg.get("model") or DEFAULT_MODEL
    emit("config", model=text_model, image_model=DEFAULT_IMAGE_GENERATION_MODEL, skills=bool(cfg.get("skills_path")),
         max_output_tokens=cfg.get("max_output_tokens"), budget=cfg.get("budget"))

    class LogCalls(hooks.PreToolCallDecideHook):
        async def run(self, context, data):  # noqa: ANN001 - SDK hook signature
            emit("tool_call", name=str(data.name), args=_short(data.args))
            if data.id and str(data.name) == "run_command":  # the one tool the harness can send to the background
                OPEN_CALLS.add(data.id)
            return hooks.HookResult(allow=True)

    class LogResults(hooks.PostToolCallHook):
        async def run(self, context, data):  # noqa: ANN001
            emit("tool_result", name=str(data.name), error=data.error, result=_short(data.result, 2000))
            OPEN_CALLS.discard(data.id)

    def discouraged(args: dict) -> bool:
        line = str(args.get("CommandLine") or args.get("command_line") or "")
        return any(s in line for s in DISCOURAGED)

    b = cfg.get("budget") or {}
    kwargs = {"skills_paths": [cfg["skills_path"]]} if cfg.get("skills_path") else {}
    import uuid

    conversation_id = str(uuid.uuid4())
    extra_hooks, prompt_prefix, procs = [], "", []
    if cfg.get("plugin"):
        att = plugin_attachment(cfg, url, emit, conversation_id)
        kwargs |= att["kwargs"]
        extra_hooks, prompt_prefix, procs = att["hooks"], att["prompt_prefix"], att["procs"]
    if cfg.get("vanilla"):
        att = vanilla_attachment(cfg, emit)
        kwargs |= att["kwargs"]
        procs = att["procs"]
    agent_cfg = LocalAgentConfig(
        models=[ModelTarget(name=text_model, types=[ModelType.TEXT], endpoint=endpoint),
                ModelTarget(name=DEFAULT_IMAGE_GENERATION_MODEL, types=[ModelType.IMAGE], endpoint=image_endpoint)],
        policies=[policy.workspace_only([WORKSPACE]),
                  policy.deny("run_command", when=discouraged, name="contained",
                              reason="This path or variable is outside the project and not available."),
                  policy.allow_all()],
        capabilities=types.CapabilitiesConfig(
            disabled_tools=[types.BuiltinTools.SEARCH_WEB, types.BuiltinTools.READ_URL_CONTENT]),
        hooks=[LogCalls(), LogResults(), *extra_hooks],
        conversation_id=conversation_id, session_continuation_mode=types.SessionContinuationMode.CREATE_OR_RESUME,
        workspaces=[WORKSPACE], app_data_dir=f"{HOME}/.antigravity", save_dir=f"{HOME}/.antigravity/sessions",
        budget_config=types.BudgetConfig(max_model_calls=b.get("max_model_calls"), max_tool_calls=b.get("max_tool_calls")),
        **kwargs,
    )
    t0 = time.monotonic()
    status, text, stop, usage, err = "finished", "", None, None, None
    try:
        async with Agent(agent_cfg) as agent:
            emit("started", sandbox_status=str(agent.sandbox_status))
            try:
                resp = await asyncio.wait_for(agent.chat(prompt_prefix + cfg["task"]), cfg["timeout_s"])
                text = await asyncio.wait_for(resp.text(), cfg["timeout_s"])
                stop = str(getattr(resp, "_stop_reason", None))
                usage = resp.usage_metadata or agent.conversation.total_usage
                # The turn has ended, but a command the agent sent to the background may still be running; the harness
                # wakes the agent when it finishes, as the app would. Keep the session open while that can happen.
                waited = await wait_for_background(agent, emit, deadline=t0 + cfg["timeout_s"])
                if waited:
                    text = text + "\n" + "\n".join(waited)
            except asyncio.TimeoutError:
                status, err = "timeout", f"no final answer within {cfg['timeout_s']} s"
            finally:
                try:
                    hist = agent.conversation.history
                    emit("history", steps=len(hist), tool_calls=[{"name": str(c.name), "args": _short(c.args, 600)}
                                                              for s in hist if s.depth == 0 for c in s.tool_calls
                                                              if s.type.name == "TOOL_CALL"][:400],
                         errors=[s.error for s in hist if s.error][:50])
                    total = agent.conversation.total_usage
                    usage = usage or total
                except Exception as e:  # noqa: BLE001
                    emit("history_error", error=repr(e)[:300])
    except Exception as e:  # noqa: BLE001
        status, err = "crashed", f"{type(e).__name__}: {e}"[:2000]
    emit("end", status=status, text=_short(text, 8000), stop_reason=stop, error=err,
         usage=usage.model_dump(mode="json", exclude_none=True) if usage is not None else None,
         wall_s=round(time.monotonic() - t0, 2))


def runner(config_path: str) -> None:
    import asyncio

    cfg = json.loads(open(config_path).read())
    emit = Events(int(cfg.get("event_fd", EVENT_FD)))
    emit("identity", uid=os.getuid(), gid=os.getgid(), cwd=os.getcwd(), pid=os.getpid())
    asyncio.run(run_agent(cfg, emit))


if __name__ == "__main__":
    step, path = sys.argv[1], sys.argv[2]
    if step == "bootstrap":
        bootstrap(path)
    elif step == "runner":
        runner(path)
    else:
        raise SystemExit(f"unknown step {step!r}")
