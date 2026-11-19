"""Drop-folder watcher: new DCC files start a batch-sanitize conversation.

Run as the ``depot_watcher`` plugin sidecar::

    python -m antigravity3d.runtime.depot_watcher [--depot DIR] [--interval 2]

Stdlib only (polling, no ``watchdog``), so it runs anywhere the sidecar can.

- **Debounce**: a file is ready once its (size, mtime) is identical in two
  consecutive polls and it is non-empty, so half-copied files are never sent.
- **Batching**: ready files are held until nothing else in the depot is still
  being written and ``--quiet`` seconds passed since the last file became ready
  (bounded by ``--max-wait``), then they go out together in one conversation.
- **Persisted seen-set**: every triggered file is recorded as
  ``relpath -> [size, mtime_ns]`` in a JSON state file (default
  ``$ANTIGRAVITY_EXECUTABLE_DATA_DIR/depot_seen.json``, the sidecar's persistent
  data dir per the sidecar docs), written atomically *after* a successful
  trigger, so a restart never re-triggers and a failed trigger is retried.
  A file that is later replaced with different content triggers again.
- **Trigger**: injectable ``trigger(files)``; the default runs
  ``agentapi new-conversation --title=... -- "<prompt>"``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

ASSET_EXTENSIONS = frozenset({".fbx", ".glb", ".gltf", ".obj", ".usd", ".usda", ".usdc", ".usdz"})
_TEMP_SUFFIXES = (".part", ".tmp", ".crdownload", ".download", "~")

Trigger = Callable[[list[Path]], None]


def build_prompt(files: list[Path], depot: Path) -> str:
    listing = "\n".join(f"- {f}" for f in files)
    return (
        f"New asset files landed in the antigravity-3d depot ({depot}). Run the batch-sanitize "
        f"pipeline on exactly these {len(files)} file(s):\n{listing}\n\n"
        "Use the game-ready-sanitizer skill for each file (UV check, LODs and colliders with "
        "generate_lods_and_colliders, run_usd_validator, then Tier-1 / Tier-1.5 QA via the usd_verifier "
        "subagent). The reusable DAG is plugin workflow workflows/batch_sanitize.py "
        "(run_workflow with Args {\"files\": [...]}). Do not export anything whose QA gate fails; "
        "finish with a one-line verdict per file."
    )


def agentapi_trigger(model: str | None = None, title_prefix: str = "Batch sanitize") -> Trigger:
    """Default trigger: ``agentapi new-conversation`` (documented sidecar Agent API CLI)."""

    def _trigger(files: list[Path]) -> None:
        depot = files[0].parent if files else Path(".")
        cmd = ["agentapi", "new-conversation", f"--title={title_prefix}: {len(files)} file(s)"]
        if model:
            cmd.append(f"--model={model}")
        cmd += ["--", build_prompt(files, depot)]
        subprocess.run(cmd, check=True, timeout=120, stdin=subprocess.DEVNULL)

    return _trigger


@dataclass
class _Obs:
    size: int
    mtime_ns: int
    stable: int  # consecutive polls with an unchanged (size, mtime)


def _atomic_write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class DepotWatcher:
    def __init__(
        self,
        depot: Path,
        state_path: Path,
        trigger: Trigger | None = None,
        *,
        extensions: Iterable[str] = ASSET_EXTENSIONS,
        settle_polls: int = 2,
        quiet: float = 0.0,
        max_wait: float = 60.0,
        max_batch: int = 50,
        retry_after: float = 60.0,
        log: Callable[[str], None] | None = None,
    ):
        if settle_polls < 2:
            raise ValueError("settle_polls must be >= 2 (size has to be seen stable across two polls)")
        self.depot = Path(depot)
        self.state_path = Path(state_path)
        self.trigger = trigger or agentapi_trigger()
        self.extensions = frozenset(e.lower() for e in extensions)
        self.settle_polls, self.quiet, self.max_wait = settle_polls, quiet, max_wait
        self.max_batch, self.retry_after = max_batch, retry_after
        self.log = log or (lambda m: print(f"[depot_watcher] {m}", flush=True))
        self._obs: dict[str, _Obs] = {}
        self._ready_since: dict[str, float] = {}
        self._seen: dict[str, list[int]] = self._load_seen()
        self._retry_at = 0.0

    # ---- persistence
    def _load_seen(self) -> dict[str, list[int]]:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            seen = d.get("seen", {}) if isinstance(d, dict) else {}
            return {k: list(v) for k, v in seen.items() if isinstance(v, list) and len(v) == 2}
        except (OSError, ValueError):
            return {}

    def _save_seen(self) -> None:
        _atomic_write_json(self.state_path, {"schema": "agy3d.depot_seen/1", "depot": str(self.depot),
                                             "seen": self._seen})

    # ---- scanning
    def _wanted(self, p: Path) -> bool:
        name = p.name
        if name.startswith(".") or name.endswith(_TEMP_SUFFIXES):
            return False
        return p.suffix.lower() in self.extensions

    def _scan(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        if not self.depot.is_dir():
            return out
        for root, dirs, files in os.walk(self.depot):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                p = Path(root, fn)
                if not self._wanted(p):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[p.relative_to(self.depot).as_posix()] = (st.st_size, st.st_mtime_ns)
        return out

    def _is_seen(self, rel: str, sig: tuple[int, int]) -> bool:
        return self._seen.get(rel) == [sig[0], sig[1]]

    def poll_once(self, now: float | None = None) -> list[Path] | None:
        """One poll. Returns the batch it triggered (absolute paths) or None."""
        now = time.monotonic() if now is None else now
        current = self._scan()
        for rel in list(self._obs):
            if rel not in current:
                del self._obs[rel]
                self._ready_since.pop(rel, None)
        unstable = 0
        for rel, (size, mtime) in current.items():
            if self._is_seen(rel, (size, mtime)):
                self._obs.pop(rel, None)
                self._ready_since.pop(rel, None)
                continue
            prev = self._obs.get(rel)
            if prev is not None and prev.size == size and prev.mtime_ns == mtime:
                prev.stable += 1
            else:
                self._obs[rel] = prev = _Obs(size, mtime, 0)
                self._ready_since.pop(rel, None)
            if prev.stable >= self.settle_polls - 1 and size > 0:
                self._ready_since.setdefault(rel, now)
            else:
                unstable += 1
        if not self._ready_since or now < self._retry_at:
            return None
        newest = max(self._ready_since.values())
        oldest = min(self._ready_since.values())
        waited_out = now - oldest >= self.max_wait
        if not waited_out and (unstable or now - newest < self.quiet):
            return None
        rels = sorted(self._ready_since, key=lambda r: (self._ready_since[r], r))[: self.max_batch]
        files = [self.depot / r for r in rels]
        try:
            self.trigger(files)
        except Exception as e:  # noqa: BLE001 - retry later, never crash the sidecar
            self._retry_at = now + self.retry_after
            self.log(f"trigger failed for {len(files)} file(s), retrying in {self.retry_after:.0f}s: {e}")
            return None
        for r in rels:
            o = self._obs.pop(r)
            self._ready_since.pop(r, None)
            self._seen[r] = [o.size, o.mtime_ns]
        self._save_seen()
        self.log(f"triggered batch-sanitize for {len(files)} file(s): {', '.join(rels)}")
        return files

    def run(self, stop: threading.Event, interval: float = 2.0) -> None:
        self.log(f"watching {self.depot} every {interval}s (state {self.state_path})")
        while not stop.is_set():
            self.poll_once()
            stop.wait(interval)


def _default_state_path() -> Path:
    data = os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR")
    base = Path(data) if data else Path.home() / ".agy3d"
    return base / "depot_seen.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="antigravity3d.runtime.depot_watcher")
    ap.add_argument("--depot", type=Path,
                    default=Path(os.environ.get("AGY3D_DEPOT_DIR") or Path.home() / "agy3d_depot"))
    ap.add_argument("--state", type=Path, default=_default_state_path())
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--quiet", type=float, default=4.0, help="seconds without new ready files before a batch fires")
    ap.add_argument("--max-wait", type=float, default=120.0)
    ap.add_argument("--model", default=os.environ.get("AGY3D_SANITIZE_MODEL"),
                    help="agentapi --model value (tier or model id); default: agentapi's default")
    ns = ap.parse_args(argv)
    ns.depot.mkdir(parents=True, exist_ok=True)
    watcher = DepotWatcher(ns.depot.resolve(), ns.state, agentapi_trigger(ns.model),
                           quiet=ns.quiet, max_wait=ns.max_wait)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    watcher.run(stop, ns.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
