"""Direct bindings to two reviewed EL4CTEO/blender-skills primitives.

Set AGY3D_BLENDER_SKILLS to a checkout of revision 9e7e04a603c125a0d17447ab97112fa70a865067.
Only these scripts and their shared helper are read. Source hashes also support
a read-only deployment without .git metadata.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


PINNED_SCRIPTS = {
    "mesh_stats": ("blender-game-optimization", "6f48790b87b90cd15d4c20e6884dcfedf9aa65292f4afcc7fad9fcf7eb8216bc"),
    "qa_check": ("blender-asset-qa", "37d01051f98fdafd3a9f13f7525804174c68a7bacb446d5bd5b2f43fc2785cca"),
}
HELPER_DIGEST = "90f44d70198b199cd922e4e57f7e55c56a3041d3a498ef7d444b019f9795d2ff"


def _load(path: Path, digest: str) -> ModuleType:
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != digest:
        raise ValueError(f"unexpected blender-skills source digest for {path.name}")
    module = ModuleType(path.stem)
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def load_reviewed(checkout: Path) -> dict[str, Callable[..., dict[str, Any]]]:
    original_path = sys.path.copy()
    previous_helper = sys.modules.get("_bpyx")
    try:
        scripts = checkout / "skills"
        sys.modules["_bpyx"] = _load(scripts / "blender-game-optimization/scripts/_bpyx.py", HELPER_DIGEST)
        return {name: _load(scripts / category / "scripts" / f"{name}.py", digest).run
                for name, (category, digest) in PINNED_SCRIPTS.items()}
    finally:
        # Upstream scripts prepend their own directory; keep worker imports isolated.
        sys.path[:] = original_path
        if previous_helper is None:
            sys.modules.pop("_bpyx", None)
        else:
            sys.modules["_bpyx"] = previous_helper
