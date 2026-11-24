#!/usr/bin/env python
"""The website task as a repeatable run: one command prepares a workspace, runs one arm of the comparison harness in it
and writes the result folder; the same seed runs with this project's plugin or without it.

Run 1 (``results/site_run1/``) was driven by scratch scripts and an uncommitted edit of the sandbox, and had no stock
baseline. What this adds, each part with its reason:

* ``prepare``: the workspace the agent receives, built from a copy of the project the site is about (copied, never
  linked, and the checkout is not written to), a folder of reference notes, a ``package.json`` with its lockfile whose
  packages are installed into the workspace before the sandbox starts (the sandbox has no network), and the task.
* ``run``: ``prepare`` then one sandboxed session, then ``collect``. ``--arm plugin|script|stock`` is the same seed with
  the plugin (default or script authoring) or without it. Each arm has its own account with its own token share, no
  image may be made (the relay refuses), and the wall-clock limit is an argument. A run that can spend money needs
  ``--live`` and ``GEMINI_API_KEY``; ``--offline`` runs the same wiring against the harness's stand-in model
  (``compare_agents.FakeUpstream``) and spends nothing, which is how the wiring is tested.
* ``collect``: the result folder, from a finished run, so a three-hour run whose collection failed is not lost.
* ``status``: where a running session stands.

The result folder holds what ``results/site_run1/`` holds (task, record, events, the agent's report, the site's
source), written by the command, with every path of this machine replaced by a label (``<repository>``,
``<workspace>``, ...) and a check that none is left, plus ``run.json`` (the seed's digests, the options, the built
site's weight) and, when this tree has the site check, its answer for the finished site.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare_agents as ca  # noqa: E402

ARMS = {"plugin": "N", "script": "S", "stock": "A"}  # the comparison's own arm letters; A is the stock agent, no plugin
DEFAULT_MODEL = "gemini-4-neon"
# Run 1's backstops (a 69 minute session made 139 model calls and 181 tool calls): the SDK's own, not the comparison's
# ``harness_budget``, which sizes a 20-call asset task.
DEFAULT_LIMITS = {"max_model_calls": 450, "max_tool_calls": 1400}
DEFAULT_WALL_S = 3 * 3600
SITE_SRC_FILE_LIMIT = 512 * 1024  # a source file bigger than this is a model or a picture: listed, not copied


# ----------------------------------------------------------------------------------------------- the task text


def render_task(text: str, plugin: bool) -> str:
    """The task as one arm reads it. Blocks ``<!-- plugin -->...<!-- /plugin -->`` and ``<!-- stock -->...<!-- /stock -->``
    are kept only for that arm (markers removed); everything else is identical for both, so the arms differ in what
    the task says about the plugin and in nothing else."""
    for tag in ("plugin", "stock"):
        if text.count(f"<!-- {tag} -->") != text.count(f"<!-- /{tag} -->"):
            raise ValueError(f"unbalanced <!-- {tag} --> markers in the task file")
    keep, drop = ("plugin", "stock") if plugin else ("stock", "plugin")
    text = re.sub(rf"<!-- {drop} -->.*?<!-- /{drop} -->\n?", "", text, flags=re.S)
    text = re.sub(rf"<!-- /?{keep} -->\n?", "", text)
    return text


# ----------------------------------------------------------------------------------------------- the workspace


def tree_digest(root: Path, skip: tuple[str, ...] = (".git", "node_modules")) -> str:
    """One sha256 over the paths and contents of a tree: what a seed was, so that two runs can be shown to share one."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in skip for part in rel.parts):
            continue
        if p.is_symlink():
            h.update(f"L {rel} {os.readlink(p)}\n".encode())
        elif p.is_file():
            h.update(f"F {rel} {hashlib.sha256(p.read_bytes()).hexdigest()}\n".encode())
    return h.hexdigest()


def copy_tree(src: Path, dst: Path, *, skip: tuple[str, ...] = ()) -> None:
    """A copy the agent can change without touching ``src``: files are copied, and a symbolic link is kept only if it
    stays inside the copy (``node_modules/.bin`` needs them); one that leaves it is an error, since the sandbox would
    follow it to the host's file or find nothing."""
    if not src.is_dir():
        raise SystemExit(f"{src} is not a folder")
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*skip) if skip else None)
    root = dst.resolve()
    bad = [str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_symlink()
           and not (root == (t := Path(os.path.normpath(p.parent.resolve() / os.readlink(p)))) or root in t.parents)]
    if bad:
        raise SystemExit(f"{src} holds symbolic links that point outside it (first: {bad[:3]}); the workspace takes no links")


def verify_install(work: Path) -> list[str]:
    """Every package the lockfile names for this platform is in ``work/node_modules`` at the locked version (the
    agent cannot install anything, so a missing one would show up hours into a run)."""
    lock = json.loads((work / "package-lock.json").read_text())
    problems = []
    for key, meta in (lock.get("packages") or {}).items():
        if not key.startswith("node_modules/") or meta.get("optional") or meta.get("os") or meta.get("cpu") or meta.get("link"):
            continue  # an optional or platform-bound package is legitimately absent on a machine it does not suit
        pj = work / key / "package.json"
        if not pj.is_file():
            problems.append(f"{key} is missing")
        elif meta.get("version") and json.loads(pj.read_text()).get("version") != meta["version"]:
            problems.append(f"{key} is {json.loads(pj.read_text()).get('version')}, the lockfile says {meta['version']}")
    return problems


def prepare_workspace(work: Path, *, project: Path, notes: Path, packages: Path, task: str, npm: str = "npm",
                      project_name: str | None = None) -> dict[str, Any]:
    """Build the workspace at ``work`` (which must not exist). Returns what the seed was, for ``run.json``."""
    if work.exists():
        raise SystemExit(f"{work} exists: a workspace is built fresh so that two runs start from the same seed")
    for need in ("package.json", "package-lock.json"):
        if not (packages / need).is_file():
            raise SystemExit(f"{packages} has no {need}: the packages are installed from a package.json with its lockfile")
    name = project_name or project.resolve().name
    digest_before = tree_digest(project)
    work.mkdir(parents=True)
    copy_tree(project, work / name, skip=(".git", "node_modules"))
    copy_tree(notes, work / "reference")
    shutil.copyfile(packages / "package.json", work / "package.json")
    shutil.copyfile(packages / "package-lock.json", work / "package-lock.json")
    if (packages / "node_modules").is_dir():  # already installed: copied, so the run does not depend on the registry
        copy_tree(packages / "node_modules", work / "node_modules")
        how = "copied from " + "<packages>/node_modules"
    else:
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=work, check=True, stdin=subprocess.DEVNULL)
        how = "npm ci"
    problems = verify_install(work)
    if problems:
        raise SystemExit("the workspace's node_modules does not match its lockfile: " + "; ".join(problems[:5]))
    if tree_digest(project) != digest_before:
        raise SystemExit(f"{project} changed while it was copied")
    manifest = {str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(work.rglob("*")) if p.is_file() and not p.is_symlink() and "node_modules" not in p.relative_to(work).parts}
    git = lambda *a: subprocess.run(["git", "-C", str(project), *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    lock = json.loads((work / "package-lock.json").read_text())
    return {"project_name": name, "project_digest": digest_before, "project_git_commit": git("rev-parse", "HEAD") or None,
            "project_git_dirty": bool(git("status", "--porcelain")) if git("rev-parse", "HEAD") else None,
            "notes_digest": tree_digest(work / "reference"),
            "package_json_sha256": hashlib.sha256((work / "package.json").read_bytes()).hexdigest(),
            "lockfile_sha256": hashlib.sha256((work / "package-lock.json").read_bytes()).hexdigest(),
            "packages_installed": sum(1 for k in (lock.get("packages") or {}) if k.startswith("node_modules/")),
            "packages_how": how, "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
            # every file the seed put in the workspace (not the installed packages) with its digest: what ``collect``
            # compares against to find the agent's own files, wherever it put them
            "manifest": manifest}


# ----------------------------------------------------------------------------------------------- local paths


class LeakError(RuntimeError):
    """Text about to be published still holds a path of this machine (or a key). Not an assert: ``python -O`` removes those."""


class Scrubber:
    """Replace every path of this machine in published text with a label, as run 1's scratch step did, and then
    refuse to publish text that still holds one. ``pairs`` are the exact paths the run was given, whatever their prefix
    (``--work-root /var/tmp/x`` and a checkout under /opt are the machine's, not the sandbox's), longest first; the
    pattern catches the rest (a path a tool printed that no option named) and alone leaves system prefixes
    alone. Paths inside the sandbox (/home/agent, /opt/blender, /tmp/...) are not the machine's and stay."""

    HOST = re.compile(r"/(?:home/(?!agent\b)|root\b|Users/|tmp/(?:claude-|pytest-)|run/agy-pilot)[^\s\"'\\,;:)\]}>]*")

    def __init__(self, pairs: dict[str, str], secrets: tuple[str, ...] = ()) -> None:
        self.pairs = sorted(((p.rstrip("/"), label) for p, label in pairs.items() if p and len(p.rstrip("/")) > 1),
                            key=lambda x: -len(x[0]))
        self.secrets = tuple(s for s in secrets if s and len(s) > 8)

    def text(self, s: str) -> str:
        for secret in self.secrets:
            s = s.replace(secret, "<api-key>")
        for path, label in self.pairs:
            s = s.replace(path, label)
        return self.HOST.sub("<local-path>", s)

    def leaks(self, s: str) -> list[str]:
        return self.HOST.findall(s) + [p for p, _ in self.pairs if p in s] + [x for x in self.secrets if x in s]

    def check(self, name: str, s: str) -> None:
        if found := self.leaks(s):
            raise LeakError(f"{name} still holds {found[:3]}")


# The paths this process happens to run from are ambient: a system folder there (a venv-less Python at /usr) is the
# sandbox's own too, so it is not a label's path. The paths the run was given never go through this filter.
SYSTEM_ROOTS = ("/", "/usr", "/usr/local", "/opt", "/bin", "/etc", "/lib", "/var")


def scrubber_for(work: Path, root: Path, extra: dict[str, str] | None = None) -> Scrubber:
    ambient = {sys.prefix: "<venv>", str(Path.home()): "<home>"}
    pairs = {k: v for k, v in ambient.items() if k.rstrip("/") not in SYSTEM_ROOTS}
    pairs |= {str(work): "<workspace>", str(root): "<run>", str(ca.PROJECT_SRC.parent): "<repository>", "/run/agy-pilot": "<socket-dir>"}
    pairs.update(extra or {})
    return Scrubber(pairs, secrets=(os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GOOGLE_API_KEY", "")))


# ----------------------------------------------------------------------------------------------- the site check


# Run in a separate interpreter with -I: the folder checked was written by an agent, and nothing of it (nor the current
# directory) may end up on the import path. -I also ignores PYTHONPATH, so this tree's src is passed and put first.
SITE_CHECK_CODE = r"""
import sys
site, sources, out, src = sys.argv[1:5]
sys.path.insert(0, src)
try:
    from antigravity3d.deliver.site_check import check_site
except ImportError:
    print("NO_SITE_CHECK")
    sys.exit(3)
print(check_site(site, sources=sources, out_dir=out).text())
"""


def run_site_check(site: Path, sources: Path, out_dir: Path, timeout_s: int = 240) -> tuple[str | None, str]:
    """(the check's answer or None, why there is none). Runs on the host against the finished site, outside the sandbox."""
    if not (site / "index.html").is_file():
        return None, f"no {site.name}/index.html to check"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "DISPLAY", "CHROMIUM_BIN", "PLAYWRIGHT_BROWSERS_PATH",
                                                          "XAUTHORITY")}
    try:
        r = subprocess.run([sys.executable, "-I", "-c", SITE_CHECK_CODE, str(site), str(sources), str(out_dir),
                            str(ca.PROJECT_SRC)], env=env,
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, f"the site check did not answer in {timeout_s} s"
    if r.returncode == 3:
        return None, "this tree has no site check (antigravity3d.deliver has no check_site)"
    if r.returncode != 0:
        return None, "the site check failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:300]
    return r.stdout, "ok"


# ----------------------------------------------------------------------------------------------- the result folder


def walk_regular(root: Path, work: Path, links: list[str], prune: tuple[str, ...] = ()):
    """(path, path relative to the workspace) of every regular file under ``root``, without following a link: ``root`` is
    a folder the agent wrote, and a link in it can name any file of this machine. Each link met (to a file or to a
    folder) is appended to ``links`` and left alone."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in prune]
        for name in list(dirnames):
            if (here / name).is_symlink():
                dirnames.remove(name)
                links.append(f"{(here / name).relative_to(work)} -> {os.readlink(here / name)}")
        dirnames.sort()
        for name in sorted(filenames):
            p = here / name
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                links.append(f"{p.relative_to(work)} -> {os.readlink(p)}")
            elif stat.S_ISREG(st.st_mode):
                yield p, p.relative_to(work)


def site_weight(site: Path, work: Path, links: list[str]) -> dict[str, Any]:
    files = sorted(((p.stat().st_size, str(p.relative_to(site))) for p, _ in walk_regular(site, work, links)), reverse=True)
    return {"files": len(files), "bytes": sum(s for s, _ in files), "largest": [{"path": p, "bytes": s} for s, p in files[:5]]}


def stage_tree(src: Path, dst: Path, work: Path, links: list[str], skip: tuple[str, ...] = ()) -> None:
    """A copy of ``src`` made of its regular files only. The site check serves one folder and reads another, on this
    machine and outside the sandbox; both are copies, so that nothing it opens is reached through a link."""
    dst.mkdir(parents=True, exist_ok=True)
    for p, rel in walk_regular(src, work, links):
        inner = p.relative_to(src)
        if any(part in skip for part in inner.parts):
            continue
        (dst / inner).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dst / inner)


# Never the agent's source: installed packages, caches, the plugin's own folders (its packages and sessions are the
# plugin's output, kept in the raw run) and the build output that ``dist`` and the built site duplicate.
NOT_SOURCE_DIRS = ("node_modules", ".git", ".vite", ".cache", ".parcel-cache", ".turbo", ".next", "__pycache__", ".npm")
# ``agy3d_site_check`` holds the pictures the plugin's site check took for the agent: looked at, not written.
NOT_SOURCE_TOP = (".agents", ".agy3d", "agy3d_assets", "agy3d_site_check", "dist")
# The plugin's installer also writes its skills into the workspace's ``skills/`` before the session starts (seen in the
# offline plugin run); for the stock arm a ``skills/`` folder can only be the agent's own.
PLUGIN_TOP = ("skills",)


def agent_files(work: Path, manifest: dict[str, str], built_rel: str, narrow: Path | None, links: list[str],
                plugin: bool = False) -> list[tuple[Path, Path]]:
    """The files the agent made or changed: everything in the workspace that is not in the seed's manifest (or whose
    digest differs from it), except what ``NOT_SOURCE_*`` and the built site are. ``narrow`` limits it to one folder."""
    top_skip = {*NOT_SOURCE_TOP, *(PLUGIN_TOP if plugin else ()), Path(built_rel).parts[0]}
    out = []
    for p, rel in walk_regular(narrow or work, work, links, prune=NOT_SOURCE_DIRS):
        if rel.parts[0] in top_skip:
            continue
        if manifest.get(str(rel)) == hashlib.sha256(p.read_bytes()).hexdigest():
            continue
        out.append((p, rel))
    return out


def copy_site_src(files: list[tuple[Path, Path]], dst: Path, scrub: Scrubber) -> list[str]:
    """The site's source as the agent left it, paths labelled. Named in the returned list instead of copied: a file over
    ``SITE_SRC_FILE_LIMIT`` (a model or a picture), a binary, and a text file that still holds a path of this machine
    after labelling (not published)."""
    omitted = []
    for p, rel in files:
        size = p.stat().st_size
        if size > SITE_SRC_FILE_LIMIT:
            omitted.append(f"{rel} ({size} bytes)")
            continue
        try:
            text = scrub.text(p.read_text())
        except UnicodeDecodeError:
            omitted.append(f"{rel} (binary, {size} bytes)")
            continue
        if scrub.leaks(text):
            omitted.append(f"{rel} (holds a path of this machine, not published)")
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text(text)
    return omitted


CLIPPED = re.compile(r"(.*)\.\.\.\[(\d+) more chars\]", re.S)  # how the run's event log ends a text it cut


def whole_final_text(final: str, transcript: Path) -> str:
    """The agent's final report, whole. The event log of a run keeps the start of a long text and says how many
    characters it left out; the relay's transcript holds the model's last reply. Where that reply continues the kept
    start exactly, the report is put together from the two; where it does not, the cut text is returned as it is."""
    m = CLIPPED.fullmatch(final or "")
    if not m or not transcript.is_file():
        return final
    kept, more = m.group(1), int(m.group(2))
    last = ""
    for line in transcript.read_text().splitlines():
        try:
            last = json.loads(line).get("reply_text") or last
        except ValueError:
            continue
    head = last[:len(last) - more] if 0 < more < len(last) else None
    return kept + last[len(last) - more:] if head and kept.endswith(head) else final


def collect(work_root: Path, out: Path) -> dict[str, Any]:
    """Write the result folder ``out`` from the run in ``work_root``. Safe to run again. The workspace is what the agent
    left on a host folder: nothing here follows a link out of it (see ``resolve_in_workspace``), and what is named
    in ``run.json`` as ``links_not_followed`` was left alone."""
    meta = json.loads((work_root / "site_task.json").read_text())
    work, account = Path(meta["workspace"]), meta["account"]
    run_dir = work_root / "runs" / account.replace(":", "_")
    rec = ca.load_record(run_dir / "record.json")
    manifest = json.loads((work_root / "seed_manifest.json").read_text())
    scrub = scrubber_for(work, work_root, {meta["project_src"]: "<project checkout>", meta["notes_src"]: "<notes>",
                                           meta["packages_src"]: "<packages>"})
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    links: list[str] = []

    def put(name: str, text: str) -> None:
        text = scrub.text(text)
        scrub.check(name, text)
        (out / name).write_text(text)
        written[name] = hashlib.sha256(text.encode()).hexdigest()[:12]

    put("task.md", meta["task_as_given"])
    put("record.json", json.dumps(asdict(rec), indent=1, default=str))
    events = run_dir / "events.jsonl"
    put("events.jsonl", events.read_text() if events.exists() else "")
    put("report.md", whole_final_text(rec.final_text, run_dir / "transcript.jsonl") or "(the agent wrote no final report)\n")
    narrow = None
    if meta.get("site_src"):
        narrow = ca.resolve_in_workspace(work, meta["site_src"], links)
    files = [] if meta.get("site_src") and narrow is None else agent_files(work, manifest, meta["site_built"], narrow, links, plugin=meta["arm_name"] != "stock")
    shutil.rmtree(out / "site_src", ignore_errors=True)
    (out / "site_src").mkdir()
    omitted = copy_site_src(files, out / "site_src", scrub)
    built = ca.resolve_in_workspace(work, meta["site_built"], links)
    has_site = built is not None and (built / "index.html").is_file() and not (built / "index.html").is_symlink()
    check, why = None, "no site/index.html to check" if built is None or not has_site else "ok"
    if has_site:
        stage = work_root / "check_in"
        shutil.rmtree(stage, ignore_errors=True)
        stage_tree(built, stage / "site", work, links)
        src_real = ca.resolve_in_workspace(work, meta["project_name"], links)
        stage_tree(src_real, stage / "sources", work, links, skip=(".git", "node_modules")) if src_real else (stage / "sources").mkdir(parents=True)
        check, why = run_site_check(stage / "site", stage / "sources", work_root / "site_check", timeout_s=240)
    if check is not None:
        put("site_check.txt", check)
    run = {k: meta[k] for k in ("arm_name", "arm", "model", "share_tokens", "share_images", "wall_s_limit", "limits", "offline",
                                "seed", "options", "command")} | {
        "status": rec.status, "wall_s": rec.wall_s, "model_calls": rec.model_calls, "tool_calls": rec.tool_calls,
        "tokens": rec.tokens, "images": rec.images, "refused": rec.refused,
        "site": site_weight(built, work, links) if has_site else None,
        "site_src_files": len(files) - len(omitted), "site_src_omitted": omitted, "site_check": why,
        "links_not_followed": sorted(set(links))}
    put("run.json", json.dumps(run, indent=1, default=str))
    return {"out": str(out), "files": written, "status": rec.status, "site_check": why}


# ----------------------------------------------------------------------------------------------- the offline path


SITE_PROBE = r"""python3 - <<'EOF'
import json, os, shutil, subprocess
def run(c):
    p = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=90)
    return (p.stdout + p.stderr).strip()[:300]
r = {"path": os.environ["PATH"].split(":"), "cwd": os.getcwd()}
for t in ("node", "npm", "blender", "chromium", "xvfb-run"):
    r["which_" + t] = shutil.which(t)
r["node"] = run("node --version")
r["chromium_bin"] = os.environ.get("CHROMIUM_BIN")
r["chromium_runs"] = run((os.environ.get("CHROMIUM_BIN") or "chromium") + " --headless=new --no-sandbox --version")
r["three"] = run("node -e \"console.log(require('three/package.json').version)\"")
r["blender_exe"] = os.environ.get("BLENDER_EXE")
r["workspace"] = sorted(os.listdir("."))
r["plugins_dir"] = sorted(os.listdir(".agents/plugins")) if os.path.isdir(".agents/plugins") else None
r["agy3d_env"] = sorted(k for k in os.environ if k.startswith("AGY3D"))
r["opt"] = sorted(os.listdir("/opt"))
r["secret_env"] = sorted(k for k in os.environ if any(s in k for s in ("KEY", "TOKEN", "SECRET")))
print("SITE_PROBES " + json.dumps(r))
EOF"""

# The source at the workspace root (index.html and src/), the way the plugin's installer and starter lay a project out,
# and the built site in site/.
SITE_WRITE = r"""mkdir -p src site && node -e "
const fs = require('fs');
const html = '<!doctype html><meta charset=utf-8><title>offline</title><h1 data-src=\"%s/README.md#L1\">site</h1>';
fs.writeFileSync('index.html', html);
fs.writeFileSync('src/main.js', 'console.log(1);');
fs.writeFileSync('site/index.html', html);
" && ls site"""


def scripted_site_agent(project_name: str, plugin: bool) -> list[dict[str, Any]]:
    """The stand-in model of an offline run: look at the machine, write a one-page site with node (source at the root, built
    into site/), and report. The plugin arms also ask the plugin's server for its status, so that the same script shows
    whether the plugin is there."""
    turns = [ca._fc("run_command", CommandLine=SITE_PROBE, Cwd="/home/agent/project"),
             ca._fc("run_command", CommandLine=SITE_WRITE.replace("%s", project_name), Cwd="/home/agent/project")]
    if plugin:
        turns.insert(1, ca._fc("call_mcp_tool", ServerName="antigravity-3d_studio", ToolName="session_status", Arguments={},
                               toolSummary="Studio call", toolAction="Calling studio"))
    return [*turns, {"parts": [{"text": "Built site/index.html (offline stand-in model; nothing was designed)."}]}]


# ----------------------------------------------------------------------------------------------- commands


def add_seed_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--project", type=Path, required=True, help="a checkout of the project the site is about (copied)")
    p.add_argument("--notes", type=Path, required=True, help="a folder of reference notes (copied to ./reference)")
    p.add_argument("--packages", type=Path, required=True,
                   help="a folder with package.json and package-lock.json, and node_modules if already installed")
    p.add_argument("--task", type=Path, required=True, help="the task text (<!-- plugin --> / <!-- stock --> blocks allowed)")
    p.add_argument("--project-name", help="the copy's folder name in the workspace (default: the checkout's)")
    p.add_argument("--npm", default="npm", help="used for `npm ci` when --packages has no node_modules")


def cmd_prepare(a: argparse.Namespace) -> int:
    task = render_task(a.task.read_text(), plugin=a.arm != "stock")
    seed = prepare_workspace(a.workspace, project=a.project, notes=a.notes, packages=a.packages, task=task, npm=a.npm,
                             project_name=a.project_name)
    seed.pop("manifest")
    print(json.dumps(seed, indent=1))
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    if a.live == a.offline:
        raise SystemExit("say --live (real model calls, spends the share, needs GEMINI_API_KEY) or --offline (the stand-in model)")
    if a.live and not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("--live needs GEMINI_API_KEY in the environment")
    if a.work_root.exists() and any(a.work_root.iterdir()):
        raise SystemExit(f"{a.work_root} is not empty: every run has a fresh work root, so that nothing of another run mixes in")
    arm = ARMS[a.arm]
    plugin = a.arm != "stock"
    task = render_task(a.task.read_text(), plugin=plugin)
    work_root = a.work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    work = work_root / "work"
    seed = prepare_workspace(work, project=a.project, notes=a.notes, packages=a.packages, task=task, npm=a.npm,
                             project_name=a.project_name)
    (work_root / "seed_manifest.json").write_text(json.dumps(seed.pop("manifest")))
    account = f"site:{a.arm}"
    ca.BUDGET.update(ceiling_tokens=a.share_tokens + 5_000_000, ceiling_images=0, round="site")
    opts = ca.RunOptions(model=a.model, sdk_output_cap=True, limits={"max_model_calls": a.max_model_calls, "max_tool_calls": a.max_tool_calls},
                         node_dir=str(a.node_dir), browsers=True, deliverable=f"{a.site_built}/index.html",
                         approve_installs=True)
    meta = {"workspace": str(work), "account": account, "arm": arm, "arm_name": a.arm, "model": a.model, "share_tokens": a.share_tokens,
            "share_images": 0, "wall_s_limit": a.wall_s, "limits": opts.limits, "offline": a.offline, "seed": seed,
            "options": asdict(opts), "command": ["site_task.py", *a.argv], "task_as_given": task,
            "site_src": a.site_src, "site_built": a.site_built, "project_name": seed["project_name"],
            "project_src": str(a.project.resolve()), "notes_src": str(a.notes.resolve()), "packages_src": str(a.packages.resolve())}
    (work_root / "site_task.json").write_text(json.dumps(meta, indent=1))
    scripts = None if a.live else {account: scripted_site_agent(seed["project_name"], plugin)}
    relay, fake = ca.make_relay(work_root, live=a.live, allow_new_ceilings=True, use_blender=False, scripts=scripts)
    brief = ca.Brief("site", f"a website for {seed['project_name']}", 0, (0.0, 0.0), False, "not a comparison brief")
    print(f"arm {a.arm} ({arm}) | model {a.model} | share {a.share_tokens:,} tokens, 0 images | limits {opts.limits} | "
          f"wall {a.wall_s} s | {'LIVE' if a.live else 'offline'} | workspace {work}", flush=True)
    try:
        rec = ca.run_stock(relay, work_root, brief, arm, wall_s=a.wall_s, share_tokens=a.share_tokens, share_images=0, plugin=plugin,
                           account=account, workspace=work, task=task, opts=opts)
    finally:
        if fake is not None:
            fake.close()
    print(json.dumps({k: getattr(rec, k) for k in ("status", "wall_s", "model_calls", "tool_calls", "tokens", "refused")}, default=str))
    print(json.dumps(collect(work_root, a.out), indent=1))
    return 0 if rec.status == "finished" else 2


def cmd_collect(a: argparse.Namespace) -> int:
    print(json.dumps(collect(a.work_root.resolve(), a.out), indent=1))
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    meta = json.loads((a.work_root / "site_task.json").read_text())
    run = a.work_root / "runs" / meta["account"].replace(":", "_")
    ev = [json.loads(x) for x in (run / "events.jsonl").read_text().splitlines() if x.strip()] if (run / "events.jsonl").exists() else []
    rows = [json.loads(x) for x in (run / "relay_calls.jsonl").read_text().splitlines() if x.strip()] if (run / "relay_calls.jsonl").exists() else []
    ok = [r for r in rows if r.get("status") == 200 and r.get("usage")]
    tot: dict[str, int] = {}
    for r in ok:
        for k, v in r["usage"].items():
            if isinstance(v, (int, float)):
                tot[k] = tot.get(k, 0) + int(v)
    t0 = ev[0].get("t", time.time()) if ev else time.time()
    print(f"{meta['arm_name']} | {(time.time() - t0) / 60:.0f} min | model calls {len(ok)} | usage {tot}")
    for e in [e for e in ev if e.get("kind") == "tool_call"][-a.last:]:
        print(" ", e.get("name"), json.dumps(e.get("args"))[:140])
    site = Path(meta["workspace"]) / meta["site_built"]
    if site.is_dir():
        print("site/:", sum(1 for p in site.rglob("*") if p.is_file()), "files")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="build the workspace only")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--arm", choices=sorted(ARMS), default="plugin", help="which arm's reading of the task text to write")
    add_seed_args(p)
    p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("run", help="prepare, run one arm, write the result folder")
    p.add_argument("--arm", choices=sorted(ARMS), required=True,
                   help="plugin: the plugin attached; script: the plugin in script authoring; stock: no plugin")
    add_seed_args(p)
    p.add_argument("--work-root", type=Path, required=True, help="a new folder for the run (ledger, workspace, raw logs; not for the repository)")
    p.add_argument("--out", type=Path, required=True, help="the result folder")
    p.add_argument("--share-tokens", type=int, required=True, help="this arm's token share; the relay refuses any call that could pass it")
    p.add_argument("--wall-s", type=int, default=DEFAULT_WALL_S)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-model-calls", type=int, default=DEFAULT_LIMITS["max_model_calls"])
    p.add_argument("--max-tool-calls", type=int, default=DEFAULT_LIMITS["max_tool_calls"])
    p.add_argument("--node-dir", type=Path, default=Path("/opt/node22"), help="a Node install, bound read-only (bin/node, bin/npm)")
    p.add_argument("--site-src", default=None,
                   help="narrow the source to one folder of the workspace (default: every file the agent made or changed)")
    p.add_argument("--site-built", default="site", help="the built site, relative to the workspace (its index.html is the delivery)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--offline", action="store_true")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("collect", help="write the result folder from a finished run")
    p.add_argument("--work-root", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("status", help="where a running session stands")
    p.add_argument("--work-root", type=Path, required=True)
    p.add_argument("--last", type=int, default=12)
    p.set_defaults(fn=cmd_status)
    a = ap.parse_args(argv)
    a.argv = list(sys.argv[1:] if argv is None else argv)
    return a.fn(a)


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
