"""Sidecar entrypoints: warm pool front socket, depot watcher, distill job."""

from __future__ import annotations

import asyncio
import glob
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from antigravity3d.adapters.blender_5_3.pool import BlenderOpError
from antigravity3d.runtime import distill_job
from antigravity3d.runtime.depot_watcher import DepotWatcher, agentapi_trigger, build_prompt
from antigravity3d.runtime.worker_daemon import PoolClient

REPO = Path(__file__).resolve().parents[1]


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def children_of(pid: int) -> list[int]:
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if int((p / "stat").read_text().rsplit(")", 1)[1].split()[1]) == pid:
                    out.append(int(p.name))
            except (OSError, IndexError, ValueError):
                pass
    return out


@pytest.fixture()
def short_tmp():
    d = tempfile.mkdtemp(prefix="agy3dt-", dir="/tmp")  # AF_UNIX path length limit
    yield Path(d)
    subprocess.run(["rm", "-rf", d], check=False)


# --------------------------------------------------------------------------- worker_daemon


def _start_daemon(sock: Path, workers: int) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "antigravity3d.runtime.worker_daemon", "--workers", str(workers), "--fake",
         "--socket", str(sock)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert "READY" in line, line + (proc.stdout.read() if proc.poll() is not None else "")
    return proc


async def test_worker_daemon_serves_concurrent_clients_and_shuts_down_clean(short_tmp):
    sock = short_tmp / "pool.sock"
    proc = _start_daemon(sock, workers=3)
    try:
        assert stat.S_ISSOCK(sock.stat().st_mode) and stat.S_IMODE(sock.stat().st_mode) == 0o600
        async with PoolClient(str(sock)) as a, PoolClient(str(sock)) as b:
            info = await a.pool_info()
            assert info["workers"] == 3 and info["fake"] is True
            worker_pids = [p for p in info["pids"] if p]
            assert len(worker_pids) == 3 and all(alive(p) for p in worker_pids)

            delay = 0.4
            t0 = time.perf_counter()
            res = await asyncio.gather(*(c.call("echo", i=i, delay=delay) for i in range(6) for c in (a, b)))
            wall = time.perf_counter() - t0
            assert sorted(r["i"] for r in res) == sorted(list(range(6)) * 2)
            assert {r["pid"] for r in res} == set(worker_pids)
            assert wall < 12 * delay * 0.6, wall  # 12 calls x 0.4 s on 3 workers ~ 1.6 s, not 4.8 s

            r = await a.call("render", width=96, height=64, seed=7)
            img = r["image"]
            assert isinstance(img, np.ndarray) and img.shape == (64, 96, 4) and img.dtype == np.float32
            assert img[0, -1, 0] == pytest.approx(1.0) and img[0, 0, 2] == pytest.approx(7 / 255)
            imgs = await b.map("render", [{"width": 16, "height": 16, "seed": s} for s in range(3)])
            assert [round(float(x["image"][0, 0, 2]) * 255) for x in imgs] == [0, 1, 2]

            with pytest.raises(BlenderOpError) as ei:
                await a.call("fail", msg="boom")
            assert ei.value.cls == "ValueError" and "boom" in ei.value.msg
            await a.call("echo")
            await b.call("echo")  # each connection handles its __release__ before the next request
            assert glob.glob(f"/dev/shm/agy3dfront_{proc.pid}_*") == []
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
    assert rc == 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(alive(p) for p in worker_pids):
        await asyncio.sleep(0.05)
    assert not any(alive(p) for p in worker_pids)
    assert children_of(proc.pid) == []
    assert not sock.exists()
    assert glob.glob(f"/dev/shm/agy3dfront_{proc.pid}_*") == [] and glob.glob(f"/dev/shm/agy3d_{proc.pid}_*") == []


async def test_worker_daemon_refuses_second_instance_and_replaces_stale_socket(short_tmp):
    sock = short_tmp / "pool.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(sock))
    stale.close()  # leaves a dead socket file behind, like a crashed daemon
    proc = _start_daemon(sock, workers=1)
    try:
        env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
        second = subprocess.run([sys.executable, "-m", "antigravity3d.runtime.worker_daemon", "--workers", "1",
                                 "--fake", "--socket", str(sock)], capture_output=True, text=True, env=env, timeout=60)
        assert second.returncode == 1 and "already serving" in second.stderr
        async with PoolClient(str(sock)) as c:
            assert (await c.call("ping"))["pong"]
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0


# --------------------------------------------------------------------------- depot_watcher


class Recorder:
    def __init__(self, fail_times: int = 0):
        self.batches: list[list[Path]] = []
        self.fail_times = fail_times

    def __call__(self, files: list[Path]) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("agentapi unavailable")
        self.batches.append(list(files))


def test_depot_debounces_slow_writes_and_batches(tmp_path):
    depot, state = tmp_path / "depot", tmp_path / "state" / "seen.json"
    depot.mkdir()
    rec = Recorder()
    w = DepotWatcher(depot, state, rec, log=lambda m: None)

    slow = depot / "crate.fbx"
    slow.write_bytes(b"x" * 100)
    assert w.poll_once(now=0) is None  # first sighting
    with slow.open("ab") as fh:
        fh.write(b"y" * 100)  # still being copied
    assert w.poll_once(now=1) is None
    other = depot / "sub" / "lamp.GLB"
    other.parent.mkdir()
    other.write_bytes(b"z" * 10)
    (depot / "notes.txt").write_text("ignored")
    (depot / ".hidden.usd").write_text("ignored")
    (depot / "partial.obj.part").write_text("ignored")
    assert w.poll_once(now=2) is None  # crate is stable now, but lamp is still settling -> hold the batch
    assert rec.batches == []
    batch = w.poll_once(now=3)
    assert batch == [depot / "crate.fbx", depot / "sub" / "lamp.GLB"]
    assert rec.batches == [batch]
    assert w.poll_once(now=4) is None and len(rec.batches) == 1

    seen = json.loads(state.read_text())["seen"]
    assert set(seen) == {"crate.fbx", "sub/lamp.GLB"}

    # Restart: a fresh watcher with the same state never re-triggers.
    rec2 = Recorder()
    w2 = DepotWatcher(depot, state, rec2, log=lambda m: None)
    for t in range(5):
        assert w2.poll_once(now=t) is None
    assert rec2.batches == []

    # The same name with new content is a new asset.
    time.sleep(0.01)
    slow.write_bytes(b"new content" * 20)
    w2.poll_once(now=10)
    assert w2.poll_once(now=11) == [slow]


def test_depot_quiet_window_and_trigger_retry(tmp_path):
    depot = tmp_path / "depot"
    depot.mkdir()
    rec = Recorder(fail_times=1)
    w = DepotWatcher(depot, tmp_path / "seen.json", rec, quiet=5.0, retry_after=30.0, log=lambda m: None)
    (depot / "a.usdz").write_bytes(b"1")
    w.poll_once(now=0)
    assert w.poll_once(now=1) is None  # ready at t=1, waiting for the quiet window
    (depot / "b.usda").write_bytes(b"2")
    w.poll_once(now=3)
    assert w.poll_once(now=4) is None  # b became ready at t=4, window restarts
    assert w.poll_once(now=9) is None  # trigger raised -> not marked seen, retry later
    assert rec.batches == [] and not (tmp_path / "seen.json").exists()
    assert w.poll_once(now=20) is None  # still inside the retry back-off
    assert w.poll_once(now=40) == [depot / "a.usdz", depot / "b.usda"]


def test_default_trigger_invokes_agentapi(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    f = tmp_path / "x.fbx"
    agentapi_trigger(model="gemini-4-carbon-medium")([f])
    cmd, kw = calls[0]
    assert cmd[:2] == ["agentapi", "new-conversation"] and "--model=gemini-4-carbon-medium" in cmd
    assert cmd[-2] == "--" and str(f) in cmd[-1] and "batch-sanitize" in cmd[-1]
    assert kw["check"] is True
    assert "game-ready-sanitizer" in build_prompt([f], tmp_path)


def test_depot_watcher_process_end_to_end(tmp_path):
    bindir, depot, log = tmp_path / "bin", tmp_path / "depot", tmp_path / "agentapi.log"
    bindir.mkdir()
    fake = bindir / "agentapi"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {log}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.Popen([sys.executable, "-m", "antigravity3d.runtime.depot_watcher", "--depot", str(depot),
                             "--state", str(tmp_path / "seen.json"), "--interval", "0.1", "--quiet", "0"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 10
        while not depot.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        (depot / "robot.glb").write_bytes(b"glTF" * 50)
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert log.exists(), "agentapi was never called"
        args = log.read_text()
        assert "new-conversation" in args and "robot.glb" in args
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0


# --------------------------------------------------------------------------- inspector (now an MCP App)
# The localhost HTTP sidecar that served inspector pages to the side pane is retired: the studio MCP
# server serves the inspector as an MCP App (tests/test_mcp_server.py, tests/test_inspector.py).


def test_inspector_is_served_by_the_mcp_server_not_a_sidecar():
    from antigravity3d.plugin_install import bundle_dir
    from antigravity3d.runtime import inspector_server

    assert not (bundle_dir() / "sidecars" / "inspector").exists()
    assert not hasattr(inspector_server, "make_server")  # no HTTP server left
    assert inspector_server.APP_URI.startswith("ui://") and inspector_server.APP_MIME == "text/html;profile=mcp-app"


# --------------------------------------------------------------------------- distill_job


def test_distill_job_summarises_telemetry(tmp_path):
    tele = tmp_path / ".agy3d" / "telemetry" / "tool_calls.jsonl"
    tele.parent.mkdir(parents=True)
    now = time.time()
    rows = [{"ts": now, "tool": "apply_scene_ir", "ok": True, "duration_ms": 120},
            {"ts": now, "tool": "apply_scene_ir", "ok": False, "error": "non-manifold after boolean"},
            {"ts": now - 30 * 86400, "tool": "old_tool", "ok": False}]
    tele.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    out = distill_job.run(tmp_path, None, None)
    text = out.read_text()
    assert "| apply_scene_ir | 2 | 1 | 50 |" in text and "non-manifold" in text and "old_tool" not in text
    assert "Proposed guardrails" in text
