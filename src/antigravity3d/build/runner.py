"""Run an asset's build script: the guard, then a bare subprocess, then what its author needs to know.

``run_script(script, out)`` reads the script ONCE: that text is compiled (a syntax error is reported as one), read by
``antigravity3d.build.guard`` (a guard against accidents, not a sandbox), run (sent to the process on stdin and compiled
there under the script's own path, so its line numbers and frames are the file's) and returned for the record
(``RunResult.source``): a script that rewrites its own file while it runs cannot change what was checked, run or
shipped (found by review: a debug dump written over the script shipped as zeros). It runs with this interpreter and this process's import path in a new process group, in the script's
folder, with a bare environment: ``PATH``, ``HOME``, ``LANG``, ``TMPDIR``, ``BLENDER_BIN``, ``AGY3D_BUILD_OUT`` (where
``Asset.save()`` writes the hand-off folder) and ``AGY3D_BUILD_CACHE``, nothing else (no API key, no proxy). The import
path travels as an argument of the bootstrap (``python -I``), not as ``PYTHONPATH``, so nothing from the server's
environment and nothing in the script's folder shadows a library. At ``timeout_s`` the whole process group is killed
(a Blender started by ``a.blender`` with it). Its output goes to files, read once the process is gone: a child that
left the group (``setsid``) cannot hold the answer past the time limit by holding a pipe open (found by review: a
5 s limit answered after 30 s, and a script that ended at once was reported as timed out). Such a child is not killed.

A failure is reported as the author reads it: the innermost frame inside the script (its line number and that line's
text) and the exception's message; for a call with a wrong keyword also the signature of what was called. Never a
traceback, never a frame of this package.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from antigravity3d.build import guard

OUT_ENV = "AGY3D_BUILD_OUT"
CACHE_ENV = "AGY3D_BUILD_CACHE"
# The variables the script's process gets from this one, when set (CONTRACT section 3). Everything else is left out:
# a key in the server's environment must not be readable by a script.
PASSED_ENV = ("PATH", "HOME", "LANG", "TMPDIR", "BLENDER_BIN")
ERROR_MARK = "\x00AGY3D_BUILD_ERROR "
STDOUT_TAIL = 800

_BOOT = ("import sys, json; sys.path[:0] = json.loads(sys.argv[2]); "
         "from antigravity3d.build.runner import _child; _child(sys.argv[1], sys.stdin.read())")


@dataclass(frozen=True)
class RunResult:
    ok: bool
    out: Path | None        # the hand-off folder when ok
    error: str              # one to three lines when not ok: where (file, line, the line's text) and why
    stdout_tail: str        # last lines the script printed (at most 800 characters)
    seconds: float
    refused: bool = False   # the guard refused it (it never ran)
    timed_out: bool = False
    no_save: bool = False   # it ran to the end without writing a design (no a.save())
    syntax: bool = False    # it does not compile: ``error`` is "line N: <text> (<why>)"
    source: str = ""        # the text that was checked and run (the one to record and ship)


def _tail(text: str, limit: int = STDOUT_TAIL) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    return cut[cut.find("\n") + 1:] if "\n" in cut else cut


def bare_env(out: Path, cache: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASSED_ENV if os.environ.get(k)}
    env.setdefault("PATH", "/usr/bin:/bin")
    env[OUT_ENV] = str(out)
    env[CACHE_ENV] = str(cache)
    return env


def import_path() -> list[str]:
    """This interpreter's import path, absolute folders only ('' and the cwd are left out: the script's folder is not
    on its path, so a file there cannot shadow numpy)."""
    return [p for p in sys.path if p and os.path.isabs(p) and os.path.isdir(p)]


def run_script(script: Path, out: Path, *, timeout_s: float = 180.0, cache: Path | None = None) -> RunResult:
    """Guard, run and report one build script. ``out`` must not exist yet (it is the run's own folder); ``cache``:
    ``AGY3D_BUILD_CACHE`` for the script (default: this environment's, else ``<out>/../.build-cache``)."""
    t0 = time.perf_counter()
    script = Path(script).resolve()
    name = script.name
    try:
        source = script.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return RunResult(False, None, f"{name} could not be read: {e}", "", 0.0, refused=True)
    try:
        compile(source, str(script), "exec", dont_inherit=True)
    except SyntaxError as e:
        line = (e.text or "").strip() or (source.splitlines()[e.lineno - 1].strip() if e.lineno and e.lineno <= len(source.splitlines()) else "")
        return RunResult(False, None, f"line {e.lineno}: {line} ({e.msg})", "", time.perf_counter() - t0, syntax=True,
                         source=source)
    except ValueError as e:  # a null byte
        return RunResult(False, None, f"line 1: ({e})", "", time.perf_counter() - t0, syntax=True, source=source)
    if (why := guard.check(source, name)) is not None:
        return RunResult(False, None, why, "", time.perf_counter() - t0, refused=True, source=source)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cache = Path(cache or os.environ.get(CACHE_ENV) or out.parent / ".build-cache")
    with tempfile.TemporaryDirectory(prefix="agy3d-run-") as logs:
        with open(Path(logs) / "out", "w+b") as fout, open(Path(logs) / "err", "w+b") as ferr:
            proc = subprocess.Popen([sys.executable, "-I", "-c", _BOOT, str(script), json.dumps(import_path())],
                                    cwd=str(script.parent), env=bare_env(out, cache), stdin=subprocess.PIPE,
                                    stdout=fout, stderr=ferr, start_new_session=True)
            timed_out = False
            try:
                proc.communicate(input=source.encode("utf-8"), timeout=timeout_s)  # waits for the process, not for EOF
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
            finally:
                if proc.poll() is None:  # an exception on our side: never leave the group running
                    _kill_group(proc)
            fout.seek(0)
            ferr.seek(0)
            stdout = fout.read().decode("utf-8", "replace")
            stderr = ferr.read().decode("utf-8", "replace")
    seconds = time.perf_counter() - t0
    tail = _tail(stdout)
    if timed_out:
        last = tail.splitlines()[-1] if tail else ""
        return RunResult(False, None, f"{name} ran longer than {timeout_s:.0f} s and was stopped"
                         + (f" (the last line it printed: {last})" if last else " (it printed nothing)"),
                         tail, seconds, timed_out=True, source=source)
    if proc.returncode != 0:
        return RunResult(False, None, _error_text(stderr, name, proc.returncode), tail, seconds, source=source)
    if not (out / "design.json").is_file():
        return RunResult(False, None, f"{name} ran to the end but wrote no design: a.save() must be its last line",
                         tail, seconds, no_save=True, source=source)
    return RunResult(True, out, "", tail, seconds, source=source)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _error_text(stderr: str, name: str, code: int) -> str:
    for line in reversed(stderr.splitlines()):
        if line.startswith(ERROR_MARK):
            try:
                e = json.loads(line[len(ERROR_MARK):])
            except ValueError:
                break
            where = f"{name} line {e['line']}: {e['text']}\n" if e.get("line") else ""
            return (where + e["message"] + (f"\n{e['hint']}" if e.get("hint") else "")).strip()
    if code < 0:
        return f"{name} was killed by signal {-code} (out of memory?)"
    last = next((x for x in reversed(stderr.splitlines()) if x.strip()), "")
    return f"{name} stopped with exit code {code}" + (f": {last[-300:]}" if last else "")


# --------------------------------------------------------------------------- the child side


def _child(script: str, source: str) -> None:
    """Runs in the script's process: run ``source`` (the text the parent checked, compiled under the script's path) as
    ``__main__``; on an exception, write the author's error (see ``_report``) as one marked line on stderr and exit 1."""
    import builtins

    path = os.path.abspath(script)
    sys.argv = [path]
    namespace = {"__name__": "__main__", "__file__": path, "__builtins__": builtins}
    try:
        exec(compile(source, path, "exec", dont_inherit=True), namespace)  # noqa: S102 - the checked script itself
    except SystemExit as e:
        if e.code in (None, 0):
            return
        _report(path, e, source)
        sys.exit(1)
    except BaseException as e:  # noqa: BLE001 - every failure is the author's to read
        _report(path, e, source)
        sys.exit(1)


def _report(path: str, exc: BaseException, source: str) -> None:
    sys.stdout.flush()
    frame, line = None, 0
    tb = exc.__traceback__
    while tb is not None:  # the innermost frame inside the script
        if os.path.abspath(tb.tb_frame.f_code.co_filename) == path:
            frame, line = tb.tb_frame, tb.tb_lineno
        tb = tb.tb_next
    if line == 0 and isinstance(exc, SyntaxError) and exc.filename and os.path.abspath(exc.filename) == path:
        line = exc.lineno or 0
    text = ""
    if line:
        lines = source.splitlines()
        text = lines[line - 1].strip() if line <= len(lines) else ""
    message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if type(exc).__name__ == "BuildError":  # the builder's own error already names the part and what was expected
        message = str(exc)
    hint = _signature_hint(exc, frame) if isinstance(exc, TypeError) and frame is not None else ""
    sys.stderr.write(ERROR_MARK + json.dumps({"line": line, "text": text[:300], "message": message[:600],
                                               "hint": hint[:400]}) + "\n")
    sys.stderr.flush()


def _signature_hint(exc: TypeError, frame) -> str:
    """For a call with a wrong keyword or argument count ("Asset.box() got an unexpected keyword argument 'sise'"): the
    signature of what was called, found among the script's own names. '' when it cannot be had cheaply."""
    import inspect

    msg = str(exc)
    if "()" not in msg or not any(w in msg for w in ("keyword argument", "positional argument", "required")):
        return ""
    qual = msg.split("()", 1)[0].strip().split(" ")[-1]
    owner, _, attr = qual.rpartition(".")
    for value in [*frame.f_locals.values(), *frame.f_globals.values()]:
        fn = None
        if owner and type(value).__qualname__ == owner and hasattr(type(value), attr):
            fn = getattr(value, attr)
        elif not owner and callable(value) and getattr(value, "__qualname__", "") == qual:
            fn = value
        elif owner and isinstance(value, type) and value.__qualname__ == owner and attr == "__init__":
            fn = value
        if fn is not None:
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                return ""
            # names and defaults only: the annotations (str | None, -> Part) double the line and tell the author nothing
            params = [p.name if p.default is p.empty else f"{p.name}={p.default!r}"
                      for p in sig.parameters.values() if p.name != "self"]
            return f"expected: {qual.replace('.__init__', '')}({', '.join(params)})"
    return ""
