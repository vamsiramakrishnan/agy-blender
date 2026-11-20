"""BatchSanitizeDAG worker processes: real parallelism, deterministic rows, crash / hang isolation, no stray processes."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import trimesh

from antigravity3d.dags.batch_sanitize import BatchSanitizeDAG
from antigravity3d.state import EngineBudget

UE = EngineBudget("unreal_5")
HOOK = "sanitize_hooks:misbehave"
posix_only = pytest.mark.skipif(os.name != "posix", reason="worker processes are POSIX only")


def meshes(tmp: Path, *names: str) -> list[Path]:
    out = []
    for i, n in enumerate(names):
        p = tmp / n
        trimesh.creation.icosphere(subdivisions=1 + i % 2, radius=0.3 + 0.01 * i).export(p)
        out.append(p)
    return out


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_gone(pids: list[int], seconds: float = 10) -> list[int]:
    end = time.monotonic() + seconds
    left = [p for p in pids if alive(p)]
    while left and time.monotonic() < end:
        time.sleep(0.1)
        left = [p for p in left if alive(p)]
    return left


@posix_only
async def test_jobs_means_worker_processes_not_threads(tmp_path: Path) -> None:
    files = meshes(tmp_path, *[f"m{i}.ply" for i in range(6)])
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, max_parallel=3).run(files)
    rows = rep.extras["rows"]
    pids = {r["worker_pid"] for r in rows}
    assert all(r["status"] == "ok" for r in rows)
    assert len(pids) > 1 and os.getpid() not in pids  # the first three files are dispatched together, to three workers
    w = rep.extras["workers"]
    assert w["mode"] == "processes" and w["jobs"] == 3 and w["distinct_pids"] == len(pids) and w["restarted"] == 0
    assert wait_gone(w["pids_spawned"]) == []  # every worker was reaped on success


@posix_only
async def test_rows_follow_input_order_whatever_the_completion_order(tmp_path: Path) -> None:
    files = meshes(tmp_path, "a_slow.ply", "b.ply", "c.ply", "d.ply")
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, max_parallel=4, worker_hook=HOOK).run(files)
    assert [Path(r["file"]).name for r in rep.extras["rows"]] == [f.name for f in files]
    assert rep.nodes["file:a_slow_ply"].end_ms > rep.nodes["file:d_ply"].end_ms  # it really finished last
    assert [json.loads((tmp_path / "o" / "report.json").read_text())["rows"][i]["file"] for i in range(4)] == [str(f) for f in files]


@posix_only
async def test_largest_files_start_first_but_rows_stay_in_input_order(tmp_path: Path) -> None:
    sizes = {"s.ply": 0, "m.ply": 2, "l.ply": 3}  # input order: small, medium, large
    for n, sub in sizes.items():
        trimesh.creation.icosphere(subdivisions=sub).export(tmp_path / n)
    files = [tmp_path / n for n in sizes]
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, max_parallel=1).run(files)
    start = {n: rep.nodes[f"file:{n.replace('.', '_')}"].start_ms for n in sizes}
    assert start["l.ply"] < start["m.ply"] < start["s.ply"]
    assert [Path(r["file"]).name for r in rep.extras["rows"]] == list(sizes) and all(r["status"] == "ok" for r in rep.extras["rows"])


@posix_only
@pytest.mark.parametrize("which, how", [("segv", "SIGSEGV"), ("exit", "exit code 3")])
async def test_a_worker_crash_fails_only_that_file_and_the_pool_recovers(tmp_path: Path, which: str, how: str) -> None:
    files = meshes(tmp_path, "a.ply", f"b_{which}.ply", "c.ply", "d.ply")
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, max_parallel=1, worker_hook=HOOK).run(files)
    st = {Path(r["file"]).name: r for r in rep.extras["rows"]}
    bad = st[f"b_{which}.ply"]
    assert bad["status"] == "failed" and bad["failure"] == "crash" and how in bad["error"] and "pid" in bad["error"]
    assert [st[n]["status"] for n in ("a.ply", "c.ply", "d.ply")] == ["ok"] * 3  # one worker, replaced after the crash
    assert rep.extras["workers"]["restarted"] == 1 and not rep.extras["gates_ok"]
    assert wait_gone(rep.extras["workers"]["pids_spawned"]) == []


@posix_only
async def test_a_hung_worker_is_reported_after_its_timeout(tmp_path: Path) -> None:
    files = meshes(tmp_path, "a.ply", "b_hang.ply", "c.ply")
    t0 = time.monotonic()
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, max_parallel=2, timeout_s=4, worker_hook=HOOK).run(files)
    st = {Path(r["file"]).name: r for r in rep.extras["rows"]}
    hung = st["b_hang.ply"]
    assert hung["status"] == "failed" and hung["failure"] == "timeout" and hung["timeout_s"] == 4 and "timed out after 4 s" in hung["error"]
    assert st["a.ply"]["status"] == "ok" and st["c.ply"]["status"] == "ok"
    assert time.monotonic() - t0 < 60 and rep.extras["workers"]["timeout_s"] == 4
    assert wait_gone(rep.extras["workers"]["pids_spawned"]) == []


@posix_only
async def test_an_ordinary_error_keeps_its_exception_name_and_the_worker_lives(tmp_path: Path) -> None:
    (tmp_path / "garbage.stl").write_text("this is not a mesh")
    files = [tmp_path / "garbage.stl", *meshes(tmp_path, "ok.ply")]
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, max_parallel=1).run(files)
    bad, good = rep.extras["rows"]
    assert bad["status"] == "failed" and "failure" not in bad and bad["error"]
    assert good["status"] == "ok" and rep.extras["workers"]["restarted"] == 0  # an exception is not a crash


async def test_jobs_below_one_never_means_unbounded(tmp_path: Path) -> None:
    files = meshes(tmp_path, "a.ply", "b.ply")
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, max_parallel=-3).run(files)
    assert rep.extras["workers"]["jobs"] >= 1 and rep.extras["gates_ok"]


@posix_only
async def test_worker_output_files_are_identical_to_the_in_process_path(tmp_path: Path) -> None:
    files = meshes(tmp_path, "a.ply", "b.ply", "c.ply")

    def digest(out: Path) -> dict[str, str]:
        return {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out.rglob("*")) if p.is_file() and p.name not in ("report.json", "spend.sqlite3")}

    inproc = await BatchSanitizeDAG(workdir=tmp_path / "t", budget=UE, max_parallel=2, isolate=False).run(files)
    procs = await BatchSanitizeDAG(workdir=tmp_path / "p", budget=UE, max_parallel=2).run(files)
    assert inproc.extras["workers"]["mode"] == "threads" and procs.extras["workers"]["mode"] == "processes"
    assert digest(tmp_path / "t") == digest(tmp_path / "p") and len(digest(tmp_path / "p")) >= 9

    def content(rep):  # everything but timings, paths and pids
        return [{k: v for k, v in r.items() if k not in ("ms", "file", "outputs", "worker_pid")} for r in rep.extras["rows"]]
    assert content(inproc) == content(procs)


@posix_only
async def test_visual_check_runs_with_workers(tmp_path: Path) -> None:
    from antigravity3d.decisions.engine import DecisionControlPlane
    from antigravity3d.decisions.fakes import FakeDecisionsClient

    plane = DecisionControlPlane(FakeDecisionsClient(answers={}))
    rep = await BatchSanitizeDAG(workdir=tmp_path / "o", budget=UE, plane=plane, max_parallel=2).run(meshes(tmp_path, "a.ply", "b.ply"))
    for r in rep.extras["rows"]:
        assert r["status"] == "ok" and "tier15" in r["visual"] and r["ms"]["visual_check"] > 0 and r["decision_receipts"]
    assert rep.extras["skipped"] == []


SCRIPT = textwrap.dedent("""
    import asyncio, sys
    from pathlib import Path
    from antigravity3d.dags.batch_sanitize import BatchSanitizeDAG
    from antigravity3d.state import EngineBudget
    async def main():
        dag = BatchSanitizeDAG(workdir=Path(sys.argv[1]) / "o", budget=EngineBudget("unreal_5"), max_parallel=2, worker_hook="sanitize_hooks:misbehave")
        t = asyncio.create_task(dag.run([Path(sys.argv[1]) / "a_hang.ply", Path(sys.argv[1]) / "b_hang.ply"]))
        await asyncio.sleep(4)
        print(" ".join(map(str, dag._pool.pids)), flush=True)
        await t
    asyncio.run(main())
""")


@posix_only
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGKILL])
def test_no_worker_outlives_an_interrupted_or_killed_parent(tmp_path: Path, sig: signal.Signals) -> None:
    meshes(tmp_path, "a_hang.ply", "b_hang.ply")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in [str(Path(__file__).parent), *sys.path] if p)}
    p = subprocess.Popen([sys.executable, "-c", SCRIPT, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    try:
        pids = [int(x) for x in p.stdout.readline().split()]
        assert len(pids) == 2 and all(alive(x) for x in pids)  # both are busy on a hang
        p.send_signal(sig)
        p.wait(timeout=30)
        assert wait_gone(pids) == [], f"workers left behind after {sig.name}"
    finally:
        p.kill()
        p.wait()
