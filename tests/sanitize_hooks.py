"""Test-only hooks for ``BatchSanitizeDAG(worker_hook="sanitize_hooks:misbehave")``: they run inside the worker process."""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def misbehave(path: Path) -> None:
    """Behave according to the file name: ``segv``/``exit`` kill the worker, ``hang`` blocks, ``slow`` sleeps a little."""
    name = path.name
    if "segv" in name:
        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(5)
    if "exit" in name:
        os._exit(3)
    if "hang" in name:
        time.sleep(600)
    if "slow" in name:
        time.sleep(2.0)
