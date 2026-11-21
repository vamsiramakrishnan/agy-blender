"""CoACD in a child process that can be stopped.

A decomposition runs in native code, so a thread cannot be interrupted inside it. Where its running time is not known to be
bounded, it runs here instead: the parent waits for the child up to a deadline, kills the child's whole process group when the
deadline passes, and reports a crash of the child (CoACD has segfaulted on odd input) as a result rather than dying with it.

The module is also the child's script. It imports nothing of this package: it is started as a plain file with ``-I``, so that
the sibling ``types.py`` of this directory cannot shadow the standard library, and a start costs an interpreter, numpy and CoACD.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

#: marks every child, so that a test (or an operator) can find one that outlived its parent
CHILD_ENV = "AGY3D_COLLIDER_CHILD"
_SCRIPT = Path(__file__).resolve()      # a test points this at a script that never ends


@dataclass(frozen=True)
class Outcome:
    parts: list[tuple] | None          # [(vertices, triangles)] as CoACD returned them; None when the child did not finish
    reason: str = ""                   # "timeout", "crash (signal 11)", "error: ..." when parts is None
    seconds: float = 0.0


def run_in_child(points, triangles, params: dict, timeout_s: float) -> Outcome:
    """CoACD on one mesh in a child process, stopped after ``timeout_s`` seconds."""
    import numpy as np

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="agy3d-coacd-") as tmp:
        src, dst = Path(tmp, "in.npz"), Path(tmp, "out.npz")
        np.savez(src, points=np.asarray(points, np.float64), triangles=np.asarray(triangles, np.int32),
                 params=np.array(repr(sorted(params.items()))))
        env = dict(os.environ, **{CHILD_ENV: "1"})
        proc = subprocess.Popen([sys.executable, "-I", str(_SCRIPT), str(src), str(dst)], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)
        try:
            _, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill(proc)
            return Outcome(None, f"timeout after {timeout_s:g} s", time.perf_counter() - t0)
        except BaseException:
            _kill(proc)
            raise
        finally:
            _kill(proc)
        dt = time.perf_counter() - t0
        if proc.returncode < 0:
            return Outcome(None, f"crash (signal {-proc.returncode})", dt)
        if proc.returncode != 0 or not dst.exists():
            tail = (err or b"").decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            return Outcome(None, f"error: {tail[0][:120]}", dt)
        with np.load(dst) as z:
            n = int(z["count"])
            return Outcome([(z[f"v{i}"], z[f"f{i}"]) for i in range(n)], "", dt)


def _kill(proc: subprocess.Popen) -> None:
    """The child and anything it started, then reap it."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not deferrable
        pass


def _child_main(src: str, dst: str) -> None:
    import ast

    import coacd
    import numpy as np

    with np.load(src) as z:
        points, triangles = z["points"], z["triangles"]
        params = dict(ast.literal_eval(str(z["params"])))
    coacd.set_log_level("error")
    parts = coacd.run_coacd(coacd.Mesh(points, triangles), **params)
    out = {"count": np.array(len(parts))}
    for i, (v, f) in enumerate(parts):
        out[f"v{i}"], out[f"f{i}"] = np.asarray(v), np.asarray(f)
    np.savez(dst, **out)


if __name__ == "__main__":
    _child_main(sys.argv[1], sys.argv[2])
