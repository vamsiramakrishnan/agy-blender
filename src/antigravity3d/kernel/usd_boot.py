"""OpenUSD's first use in a process, made by one thread alone.

The first stage a process creates or opens sets up OpenUSD's plugin, file-format, resolver and schema registries.
Two threads doing that at the same moment can stop for good: with OpenUSD 0.26.11 and the environment kit, whose
pieces are built in worker threads, two answers that arrived together each created their first stage at once, and
2 of 40 runs never returned, both threads inside ``Usd.Stage.CreateNew``. (A prop run never showed it: its first
stage is made on the main thread before any worker thread exists.)

``ensure_ready`` makes that first use happen once, in whichever thread asks first, while any other caller waits on
an ordinary lock outside OpenUSD. Afterwards it is a flag test, and stages are created and opened from any number
of threads as before. ``StageManager.create`` and ``StageManager.open`` call it; call it before any other first
touch of OpenUSD that can happen in more than one thread.
"""

from __future__ import annotations

import os
import tempfile
import threading

_lock = threading.Lock()
_ready = False


def is_ready() -> bool:
    return _ready


def ensure_ready() -> None:
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        from pxr import Usd, UsdGeom, UsdShade

        # The real thing, thrown away: a stage file created, given a mesh and a material, saved and opened again, so
        # the registries the pipeline's own first stage would set up are set up here.
        with tempfile.TemporaryDirectory(prefix="agy3d-usd-") as tmp:
            path = os.path.join(tmp, "first.usda")
            stage = Usd.Stage.CreateNew(path)
            UsdGeom.Mesh.Define(stage, "/first/mesh")
            UsdShade.Material.Define(stage, "/first/material")
            stage.GetRootLayer().Save()
            del stage
            Usd.Stage.Open(path)
        _ready = True
