"""benchmarks/site_task.py and the sandbox options it relies on: the workspace of the website task, the arms' equal
tools, the result folder and its paths. No model call: the offline path is the comparison harness's own stand-in model
(``FakeUpstream``), in the real sandbox where the machine allows one."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmarks"


def _load(name: str):
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    spec = importlib.util.spec_from_file_location(name, BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mods():
    ca = _load("compare_agents")
    st = _load("site_task")
    sb = _load("compare_sandbox")
    return ca, st, sb


@pytest.fixture(autouse=True)
def _budget_restored(mods):
    ca = mods[0]
    saved = copy.deepcopy(ca.BUDGET)
    yield
    ca.BUDGET.clear()
    ca.BUDGET.update(saved)


def _can_sandbox() -> str | None:
    if os.geteuid() != 0:
        return "needs root for the namespaces"
    if not shutil.which("unshare") or not shutil.which("setpriv"):
        return "needs unshare and setpriv"
    if subprocess.run(["unshare", "--net", "--mount", "--pid", "--fork", "true"], capture_output=True).returncode != 0:
        return "namespaces not permitted here"
    if not Path("/usr/local/bin/python3").exists():
        return "sandbox expects /usr/local/bin/python3"
    if not Path("/opt/node22/bin/node").exists() or not Path("/opt/pw-browsers/chromium").exists():
        return "needs /opt/node22 and /opt/pw-browsers"
    return None


# ----------------------------------------------------------------------------------------------- a small seed


def make_seed(base: Path, *, with_modules: bool = True) -> dict[str, Path]:
    project = base / "project"
    (project / "docs").mkdir(parents=True)
    (project / "README.md").write_text("# Demo\n\nA project.\n")
    (project / "docs" / "a.md").write_text("alpha\n")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    notes = base / "notes"
    notes.mkdir()
    (notes / "README.md").write_text("reference notes\n")
    pk = base / "packages"
    pk.mkdir()
    (pk / "package.json").write_text(json.dumps({"name": "seed", "dependencies": {"three": "0.187.0"}}))
    lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/three": {"version": "0.187.0"},
                                                "node_modules/fsevents": {"version": "2.3.3", "optional": True, "os": ["darwin"]}}}
    (pk / "package-lock.json").write_text(json.dumps(lock))
    if with_modules:
        (pk / "node_modules" / "three").mkdir(parents=True)
        (pk / "node_modules" / "three" / "package.json").write_text(json.dumps({"name": "three", "version": "0.187.0"}))
        (pk / "node_modules" / ".bin").mkdir()
        (pk / "node_modules" / ".bin" / "three").symlink_to("../three/package.json")
    task = base / "task.md"
    task.write_text("Intro.\n<!-- plugin -->\nUse the plugin.\n<!-- /plugin -->\n<!-- stock -->\nUse Blender yourself.\n<!-- /stock -->\nEnd.\n")
    return {"project": project, "notes": notes, "packages": pk, "task": task}


# ----------------------------------------------------------------------------------------------- the task text


def test_the_two_arms_read_the_same_task_but_for_what_it_says_about_the_plugin(mods, tmp_path):
    st = mods[1]
    text = make_seed(tmp_path)["task"].read_text()
    assert st.render_task(text, plugin=True) == "Intro.\nUse the plugin.\nEnd.\n"
    assert st.render_task(text, plugin=False) == "Intro.\nUse Blender yourself.\nEnd.\n"
    with pytest.raises(ValueError):
        st.render_task("a <!-- plugin --> b", plugin=True)
    shipped = (BENCH / "site_task_seed" / "task.md").read_text()
    both = [st.render_task(shipped, plugin=p) for p in (True, False)]
    assert "<!--" not in both[0] + both[1] and "antigravity-3d" in both[0] and "antigravity-3d" not in both[1]
    assert "plugin" not in both[1]  # nothing of the plugin reaches the stock arm's text


# ----------------------------------------------------------------------------------------------- the workspace


def test_a_workspace_is_a_copy_of_the_seed_and_never_touches_the_checkout(mods, tmp_path):
    st = mods[1]
    s = make_seed(tmp_path)
    before = st.tree_digest(s["project"])
    work = tmp_path / "ws"
    seed = st.prepare_workspace(work, project=s["project"], notes=s["notes"], packages=s["packages"], task="t")
    assert sorted(p.name for p in work.iterdir()) == ["node_modules", "package-lock.json", "package.json", "project", "reference"]
    assert not (work / "project" / ".git").exists()  # the agent gets the files, not the history
    assert (work / "project" / "docs" / "a.md").read_text() == "alpha\n"
    assert not (work / "project").is_symlink() and not any(p.is_symlink() and not str(p.resolve()).startswith(str(work.resolve()))
                                                          for p in work.rglob("*"))
    (work / "project" / "README.md").write_text("changed by the agent\n")  # a copy: the checkout does not follow
    assert (s["project"] / "README.md").read_text().startswith("# Demo") and st.tree_digest(s["project"]) == before
    assert seed["project_digest"] == before and seed["packages_installed"] == 2 and seed["packages_how"].startswith("copied")
    assert (work / "node_modules" / ".bin" / "three").is_symlink()  # a link inside the copy is kept: .bin needs them
    with pytest.raises(SystemExit, match="exists"):
        st.prepare_workspace(work, project=s["project"], notes=s["notes"], packages=s["packages"], task="t")


def test_a_workspace_refuses_a_link_out_of_the_seed_and_packages_that_do_not_match_the_lockfile(mods, tmp_path):
    st = mods[1]
    s = make_seed(tmp_path)
    (s["project"] / "escape").symlink_to("/etc/hostname")
    with pytest.raises(SystemExit, match="outside"):
        st.prepare_workspace(tmp_path / "w1", project=s["project"], notes=s["notes"], packages=s["packages"], task="t")
    (s["project"] / "escape").unlink()
    (s["packages"] / "node_modules" / "three" / "package.json").write_text(json.dumps({"name": "three", "version": "0.170.0"}))
    with pytest.raises(SystemExit, match=r"three is 0\.170\.0, the lockfile says 0\.187\.0"):
        st.prepare_workspace(tmp_path / "w2", project=s["project"], notes=s["notes"], packages=s["packages"], task="t")
    shutil.rmtree(s["packages"] / "node_modules")  # nothing installed: npm ci is the way, here a stand-in that installs nothing
    stub = tmp_path / "npm"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    with pytest.raises(SystemExit, match="three is missing"):
        st.prepare_workspace(tmp_path / "w3", project=s["project"], notes=s["notes"], packages=s["packages"], task="t", npm=str(stub))
    stub.write_text("#!/bin/sh\nmkdir -p node_modules/three && echo '{\"name\":\"three\",\"version\":\"0.187.0\"}' > node_modules/three/package.json\n")
    seed = st.prepare_workspace(tmp_path / "w4", project=s["project"], notes=s["notes"], packages=s["packages"], task="t", npm=str(stub))
    assert seed["packages_how"] == "npm ci"
    with pytest.raises(SystemExit, match="package-lock.json"):
        (s["packages"] / "package-lock.json").unlink()
        st.prepare_workspace(tmp_path / "w5", project=s["project"], notes=s["notes"], packages=s["packages"], task="t")


# ----------------------------------------------------------------------------------------------- local paths


def test_the_scrubber_labels_this_machines_paths_and_keeps_the_sandboxs(mods, tmp_path):
    st = mods[1]
    scrub = st.Scrubber({str(tmp_path / "ws"): "<workspace>", str(tmp_path): "<run>"}, secrets=("sk-abcdefghijkl",))
    raw = (f'{{"cwd": "/home/agent/project", "p": "{tmp_path}/ws/site/index.html", "q": "{tmp_path}/x", "py": "/usr/local/bin/python3", '
           '"r": "/root/.cache/x", "h": "/home/someone/proj/a.py", "t": "/tmp/claude-0/-home-user/scratch", "k": "sk-abcdefghijkl", '
           '"o": "/opt/blender/blender"}')
    out = scrub.text(raw)
    assert '"/home/agent/project"' in out and "/usr/local/bin/python3" in out and "/opt/blender/blender" in out
    assert "<workspace>/site/index.html" in out and "<run>/x" in out and "<api-key>" in out
    assert out.count("<local-path>") == 3 and not scrub.leaks(out)
    assert scrub.leaks("/home/someone/else")


# ----------------------------------------------------------------------------------------------- declared options


def test_the_sandbox_puts_node_and_the_browser_on_the_path_only_when_the_run_declares_them(mods):
    sb = mods[2]
    plain = sb.sandbox_env({"host": {}})
    assert plain["PATH"] == sb.SANDBOX_PATH and "CHROMIUM_BIN" not in plain  # a run that declares nothing is unchanged
    both = sb.sandbox_env({"host": {"node_dir": "/x/node", "browsers_dir": "/x/pw"}})
    assert both["PATH"].split(":")[0] == f"{sb.NODE_DIR}/bin" and both["PATH"].endswith(sb.BROWSERS_DIR)
    assert both["CHROMIUM_BIN"] == f"{sb.BROWSERS_DIR}/chromium" and both["BLENDER_EXE"] == f"{sb.BLENDER_DIR}/blender"
    assert sb.BLENDER_DIR in both["PATH"].split(":")  # Blender stays on PATH for every arm


@pytest.mark.skipif(os.geteuid() != 0, reason="the run folder is handed to the sandbox's user")
def test_a_stock_arm_gets_the_same_machine_as_the_plugin_arm_and_nothing_of_the_plugin(mods, tmp_path):
    ca, st, sb = mods
    if not Path("/opt/pw-browsers").is_dir() or not Path("/opt/node22/bin/node").exists():
        pytest.skip("needs /opt/pw-browsers and /opt/node22")
    os.chmod(tmp_path, 0o755)
    brief = ca.Brief("site", "a site", 0, (0.0, 0.0), False, "x")
    opts = ca.RunOptions(model="m-1", sdk_output_cap=True, limits={"max_model_calls": 7, "max_tool_calls": 9}, node_dir="/opt/node22",
                         browsers=True, deliverable="site/index.html")
    cfgs = {}
    for arm, plugin in (("A", False), ("N", True)):
        work = tmp_path / f"w{arm}"
        work.mkdir()
        spec = ca.stock_spec(tmp_path / f"r{arm}", arm, brief, plugin=plugin, share_tokens=1_000_000, workspace=work, task="T", opts=opts)
        cfgs[arm] = json.loads(Path(spec["host_config"]).read_text())
    a, n = cfgs["A"], cfgs["N"]
    for c in (a, n):
        assert c["model"] == "m-1" and c["max_output_tokens"] is None and c["budget"] == {"max_model_calls": 7, "max_tool_calls": 9}
        assert c["host"]["node_dir"] == "/opt/node22" and c["host"]["browsers_dir"] == "/opt/pw-browsers"
        assert c["extra_env"]["CHROMIUM_BIN"] == f"{sb.BROWSERS_DIR}/chromium" and c["task"] == "T"
    shared = {"pydeps", "blender_dir", "ctl", "home", "work", "sock_dir", "node_dir", "browsers_dir"}
    assert set(a["host"]) == shared  # the stock arm: no plugin folders, no interpreter of this project
    assert {"project_src", "project_plugin", "plugins", "venv"} <= set(n["host"]) and "plugin" not in a
    assert "GODOT_BIN" not in a["extra_env"] and not any(k.startswith("AGY3D") for k in a["extra_env"])
    assert not (tmp_path / "wA" / ".agents").exists()
    # no options: the comparison's own defaults, as before this change
    work = tmp_path / "wD"
    work.mkdir()
    d = json.loads(Path(ca.stock_spec(tmp_path / "rD", "A", brief, workspace=work, task="T")["host_config"]).read_text())
    assert "model" not in d and d["max_output_tokens"] == ca.STOCK_MAX_OUTPUT and "extra_env" not in d
    assert "node_dir" not in d["host"] and "browsers_dir" not in d["host"]


# ----------------------------------------------------------------------------------------------- the command


def test_a_run_says_live_or_offline_and_a_live_run_needs_the_key(mods, tmp_path, monkeypatch):
    st = mods[1]
    s = make_seed(tmp_path)
    args = ["run", "--arm", "stock", "--project", str(s["project"]), "--notes", str(s["notes"]), "--packages", str(s["packages"]),
            "--task", str(s["task"]), "--work-root", str(tmp_path / "wr"), "--out", str(tmp_path / "out"), "--share-tokens", "1000000"]
    with pytest.raises(SystemExit, match="--live"):
        st.main(args)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        st.main([*args, "--live"])
    assert not (tmp_path / "wr").exists()  # nothing was built before the refusal
    (tmp_path / "wr").mkdir()
    (tmp_path / "wr" / "old").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        st.main([*args, "--offline"])


def _probes(run_dir: Path) -> dict:
    for line in (run_dir / "transcript.jsonl").read_text().splitlines():
        for part in json.loads(line)["sent_last"].get("parts", []):
            out = ((part.get("functionResponse") or {}).get("response") or {}).get("output", "")
            if "SITE_PROBES {" in out:
                return json.loads(out.split("SITE_PROBES ", 1)[1].splitlines()[0])
    raise AssertionError("no probe output in the transcript")


def test_both_arms_run_the_same_seed_offline_with_the_same_tools_and_a_result_folder_without_local_paths(mods, tmp_path, monkeypatch):
    if (why := _can_sandbox()) is not None:
        pytest.skip(why)
    ca, st, _ = mods
    if not os.environ.get("BLENDER_BIN"):
        stand_in = tmp_path / "no-blender"
        stand_in.mkdir()
        (stand_in / "blender").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("BLENDER_BIN", str(stand_in / "blender"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    os.chmod(tmp_path, 0o755)
    s = make_seed(tmp_path)
    digest = st.tree_digest(s["project"])
    results = {}
    for arm in ("stock", "plugin"):
        out, wr = tmp_path / f"out_{arm}", tmp_path / f"wr_{arm}"
        rc = st.main(["run", "--arm", arm, "--project", str(s["project"]), "--notes", str(s["notes"]), "--packages", str(s["packages"]),
                      "--task", str(s["task"]), "--work-root", str(wr), "--out", str(out), "--share-tokens", "30000000",
                      "--wall-s", "400", "--offline", "--project-name", "demo"])
        assert rc == 0
        results[arm] = (out, wr)
    assert st.tree_digest(s["project"]) == digest
    for arm, (out, wr) in results.items():
        assert {p.name for p in out.iterdir()} == {"task.md", "record.json", "events.jsonl", "report.md", "site_src", "run.json", "site_check.txt"}
        run = json.loads((out / "run.json").read_text())
        assert run["status"] == "finished" and run["model"] == "gemini-4-neon" and run["share_tokens"] == 30_000_000
        assert run["command"][:3] == ["site_task.py", "run", "--arm"] and run["command"][3] == arm
        assert run["share_images"] == 0 and run["seed"]["packages_installed"] == 2 and run["site"]["files"] == 1
        assert run["site_check"] == "ok" and (out / "site_check.txt").read_text().strip()  # the tree's own site check ran on the staged copy
        assert sorted(str(p.relative_to(out / "site_src")) for p in (out / "site_src").rglob("*") if p.is_file()) == ["index.html", "src/main.js"]
        assert run["links_not_followed"] == [] and run["site_src_files"] == 2 and run["options"]["approve_installs"] is True
        assert "offline stand-in model" in (out / "report.md").read_text()
        text = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
        assert str(tmp_path) not in text and "/tmp/pytest" not in text and "<workspace>" in text
        ledger = json.loads((wr / "ledger.json").read_text())["accounts"][f"site:{arm}"]
        assert ledger["share_tokens"] == 30_000_000 and ledger["share_images"] == 0 and ledger["images"] == 0
        assert ("Use the plugin." in (out / "task.md").read_text()) == (arm == "plugin")
    sp = _probes(results["stock"][1] / "runs" / "site_stock")
    pp = _probes(results["plugin"][1] / "runs" / "site_plugin")
    for p in (sp, pp):  # the same machine
        assert p["node"].startswith("v22") and p["which_node"] and p["which_npm"] and p["which_blender"] and p["which_chromium"]
        assert p["three"] == "0.187.0" and "Chrome" in p["chromium_runs"] or "Chromium" in p["chromium_runs"]
        assert p["chromium_bin"] == "/opt/pw-browsers/chromium" and p["secret_env"] == []
        assert {"package.json", "package-lock.json", "node_modules", "reference", "demo"} <= set(p["workspace"])
    assert sp["plugins_dir"] in (None, []) and sp["agy3d_env"] == [] and not any("agy3d" in o for o in sp["opt"])  # nothing of the plugin
    assert pp["plugins_dir"] and any("agy3d" in o for o in pp["opt"])
    ev = [json.loads(x) for x in (results["stock"][1] / "runs" / "site_stock" / "events.jsonl").read_text().splitlines()]
    assert not any(e["kind"].startswith("plugin") or e["kind"] == "limits_injected" for e in ev)
    evp = [json.loads(x) for x in (results["plugin"][1] / "runs" / "site_plugin" / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "plugin_attached" for e in evp)
    assert json.loads((results["plugin"][0] / "record.json").read_text())["pipeline"]["mcp_tools"] == {"session_status": 1}
    assert "mcp_tools" not in json.loads((results["stock"][0] / "record.json").read_text())["pipeline"]


def test_collect_can_be_run_again_and_status_reads_a_run(mods, tmp_path, monkeypatch, capsys):
    """After a run whose collection failed (or a changed scrubber), ``collect`` rebuilds the folder from the run alone."""
    if (why := _can_sandbox()) is not None:
        pytest.skip(why)
    st = mods[1]
    if not os.environ.get("BLENDER_BIN"):
        stand_in = tmp_path / "no-blender"
        stand_in.mkdir()
        (stand_in / "blender").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("BLENDER_BIN", str(stand_in / "blender"))
    os.chmod(tmp_path, 0o755)
    s = make_seed(tmp_path)
    args = ["--project", str(s["project"]), "--notes", str(s["notes"]), "--packages", str(s["packages"]), "--task", str(s["task"])]
    assert st.main(["run", "--arm", "stock", *args, "--work-root", str(tmp_path / "wr"), "--out", str(tmp_path / "o1"),
                    "--share-tokens", "2000000", "--wall-s", "400", "--offline"]) == 0
    assert st.main(["collect", "--work-root", str(tmp_path / "wr"), "--out", str(tmp_path / "o2")]) == 0
    assert (tmp_path / "o1" / "events.jsonl").read_text() == (tmp_path / "o2" / "events.jsonl").read_text()
    capsys.readouterr()
    assert st.main(["status", "--work-root", str(tmp_path / "wr")]) == 0
    assert "stock |" in capsys.readouterr().out


# ----------------------------------------------------------------------------------------------- links the agent left


def fake_run(mods, base: Path):
    """A finished run's folders without a sandbox: the seed's workspace, its manifest and a record, so that what the
    agent did to the workspace afterwards can be written by the test."""
    ca, st, _ = mods
    s = make_seed(base)
    wr = base / "wr"
    work = wr / "work"
    wr.mkdir()
    seed = st.prepare_workspace(work, project=s["project"], notes=s["notes"], packages=s["packages"], task="t", project_name="demo")
    (wr / "seed_manifest.json").write_text(json.dumps(seed.pop("manifest")))
    run = wr / "runs" / "site_stock"
    run.mkdir(parents=True)
    rec = ca.RunRecord("site", "A", status="finished", account="site:stock")
    (run / "record.json").write_text(json.dumps(__import__("dataclasses").asdict(rec)))
    meta = {"workspace": str(work), "account": "site:stock", "arm": "A", "arm_name": "stock", "model": "m", "share_tokens": 1,
            "share_images": 0, "wall_s_limit": 1, "limits": {}, "offline": True, "seed": seed, "options": {}, "command": [],
            "task_as_given": "t", "site_src": None, "site_built": "site", "project_name": "demo", "project_src": str(s["project"]),
            "notes_src": str(s["notes"]), "packages_src": str(s["packages"])}
    (wr / "site_task.json").write_text(json.dumps(meta))
    return wr, work, meta


def test_a_link_to_the_hosts_folders_is_never_followed_and_a_link_into_the_sandbox_workspace_is_translated(mods, tmp_path):
    ca, st, _ = mods
    secret = tmp_path / "hostroot"
    secret.mkdir()
    (secret / "history.txt").write_text("SECRET-TOKEN-1234\n")
    wr, work, meta = fake_run(mods, tmp_path)
    (work / "src_link").symlink_to(secret)  # the agent: ln -s /root src
    (work / "dist").mkdir()
    (work / "dist" / "index.html").write_text("<h1>built</h1>")
    (work / "site").symlink_to("/home/agent/project/dist")  # the sandbox's own view of the workspace
    (work / "src").mkdir()
    (work / "src" / "main.js").write_text("export {}\n")
    (work / "src" / "inner").symlink_to(secret)  # a link inside a folder
    (work / "demo" / "docs" / "leak").symlink_to(secret)  # inside the sources folder of the facts
    out = tmp_path / "out"
    r = st.collect(wr, out)
    text = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert "SECRET-TOKEN" not in text
    run = json.loads((out / "run.json").read_text())
    assert run["site"]["files"] == 1 and r["site_check"] != "no site/index.html to check"  # the translated link was followed
    assert "node_modules/.bin/three -> ../three/package.json" not in run["links_not_followed"]  # installed packages are not walked
    assert any(x.startswith("src_link -> ") for x in run["links_not_followed"]) and any("src/inner -> " in x for x in run["links_not_followed"])
    assert (out / "site_src" / "src" / "main.js").is_file() and not (out / "site_src" / "src_link").exists()
    staged = wr / "check_in"
    assert (staged / "site" / "index.html").is_file() and not any(p.is_symlink() for p in staged.rglob("*"))
    assert not (staged / "sources" / "docs" / "leak").exists() and (staged / "sources" / "docs" / "a.md").is_file()


def test_a_site_link_that_leaves_the_workspace_is_not_checked_and_is_named(mods, tmp_path):
    ca, st, _ = mods
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "index.html").write_text("<h1>not the agent's</h1>")
    wr, work, meta = fake_run(mods, tmp_path)
    (work / "site").symlink_to(outside)
    r = st.collect(wr, tmp_path / "out")
    run = json.loads((tmp_path / "out" / "run.json").read_text())
    assert run["site"] is None and r["site_check"] == "no site/index.html to check" and run["site_check"] == r["site_check"]
    assert any(x.startswith("site -> ") for x in run["links_not_followed"])
    for climb in ("../" * 12 + "etc", "../elsewhere"):
        (work / "up").symlink_to(climb)
        assert ca.resolve_in_workspace(work, "up/index.html") is None
        (work / "up").unlink()
    (work / "a").symlink_to("b")
    (work / "b").symlink_to("a")
    assert ca.resolve_in_workspace(work, "a") is None  # a loop is not followed forever
    (work / "d").mkdir()
    (work / "e").symlink_to("d")
    assert ca.resolve_in_workspace(work, "e/x.txt") == (work / "d" / "x.txt").resolve()


def test_the_deliverable_of_a_run_is_found_through_the_sandbox_view_and_never_through_the_hosts(mods, tmp_path):
    ca = mods[0]
    work = tmp_path / "work"
    (work / "dist").mkdir(parents=True)
    (work / "dist" / "index.html").write_text("x")
    (work / "site").symlink_to("/home/agent/project/dist")
    got = ca.resolve_in_workspace(work, "site/index.html")
    assert got == (work / "dist" / "index.html").resolve() and got.is_file()
    (work / "site").unlink()
    (work / "site").symlink_to(tmp_path)  # the host's folder
    links: list[str] = []
    assert ca.resolve_in_workspace(work, "site/index.html", links) is None and links == [f"site -> {tmp_path}"]


def test_the_agents_source_is_what_it_made_or_changed_wherever_it_put_it(mods, tmp_path):
    ca, st, _ = mods
    wr, work, meta = fake_run(mods, tmp_path)
    manifest = json.loads((wr / "seed_manifest.json").read_text())
    for rel, text in {"index.html": "<h1>", "src/main.js": "1", "vite.config.js": "2", "crate.py": "from agy3d import Asset",
                      "public/models/a.glb": "x" * (600 * 1024), "package.json": '{"name": "changed"}',
                      ".agy3d/state.json": "p", "agy3d_assets/s/a.txt": "p", "dist/index.html": "b", "site/index.html": "b",
                      "node_modules/zz/index.js": "n", ".vite/c.json": "c", ".agents/plugins/p.md": "p"}.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    (work / "public" / "icon.bin").write_bytes(b"\xff\xfe\x00\x80")
    links: list[str] = []
    files = sorted(str(r) for _, r in st.agent_files(work, manifest, "site", None, links))
    assert files == ["crate.py", "index.html", "package.json", "public/icon.bin", "public/models/a.glb", "src/main.js", "vite.config.js"]
    narrowed = sorted(str(r) for _, r in st.agent_files(work, manifest, "site", work / "src", links))
    assert narrowed == ["src/main.js"]
    out = tmp_path / "o"
    out.mkdir()
    omitted = st.copy_site_src(st.agent_files(work, manifest, "site", None, links), out, st.scrubber_for(work, wr))
    assert [o.split(" (")[0] for o in omitted] == ["public/icon.bin", "public/models/a.glb"] and (out / "crate.py").is_file()
    assert "binary" in omitted[0] and "614400 bytes" in omitted[1]


def test_source_text_that_still_holds_a_path_of_this_machine_is_left_out_and_named(mods, tmp_path):
    st = mods[1]
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.js").write_text("// fine\n")
    (work / "b.js").write_text("// MARK\n")

    class Strict(st.Scrubber):
        def leaks(self, s):
            return ["/x"] if "MARK" in s else []

    out = tmp_path / "o"
    out.mkdir()
    omitted = st.copy_site_src([(work / "a.js", Path("a.js")), (work / "b.js", Path("b.js"))], out, Strict({}))
    assert omitted == ["b.js (holds a path of this machine, not published)"] and (out / "a.js").is_file() and not (out / "b.js").exists()


def test_the_paths_a_run_was_given_are_replaced_whatever_their_prefix_and_the_check_is_not_an_assert(mods, tmp_path):
    st = mods[1]
    work, root = Path("/var/tmp/site1/work"), Path("/var/tmp/site1")
    scrub = st.scrubber_for(work, root, {"/opt/checkouts/straitjacket": "<project checkout>"})
    out = scrub.text('{"w": "/var/tmp/site1/work/site", "r": "/var/tmp/site1/runs", "p": "/opt/checkouts/straitjacket/docs", '
                     '"sandbox": "/opt/blender/blender", "py": "/usr/local/bin/python3"}')
    assert "<workspace>/site" in out and "<run>/runs" in out and "<project checkout>/docs" in out
    assert "/opt/blender/blender" in out and "/usr/local/bin/python3" in out and not scrub.leaks(out)
    with pytest.raises(st.LeakError):
        scrub.check("x", "see /var/tmp/site1/work/x")
    assert "assert" not in __import__("inspect").getsource(st.Scrubber.check)


# ----------------------------------------------------------------------------------------------- installs


def test_the_stand_in_approves_an_install_into_the_runs_workspace_and_refuses_every_other_ask(mods):
    sb = mods[2]
    ok = sb.install_into_workspace
    base = {"package_dir": "/home/agent/project/agy3d_assets/x/pkg", "dry_run": False}
    assert ok(base) and ok(base | {"project_dir": "/home/agent/project"}) and ok(base | {"project_dir": "app"})
    assert ok(base | {"dest": "src/agy3d/gate", "assets": "public/models"})
    assert not ok(base | {"project_dir": "/home/agent"}) and not ok(base | {"project_dir": "/etc"})
    assert not ok(base | {"project_dir": "../other"}) and not ok(base | {"project_dir": "/home/agent/project/../x"})
    assert not ok(base | {"dest": "/home/agent/elsewhere"}) and not ok(base | {"dest": "../../x"})
    assert not ok(base | {"assets": "/tmp/pub"}) and not ok(base | {"assets": "a/../../b"})
    assert not ok(base | {"force": True}) and not ok(base | {"force": "true"})
