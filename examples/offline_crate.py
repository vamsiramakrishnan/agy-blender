"""Smallest end-to-end run: offline prop -> draft engine package + inspector HTML.

Uses the fakes (FakeDecisionsClient, FakeImageBackend) and the deterministic TemplateAuthor, so it needs no
network or API key. Run:  python examples/offline_crate.py [OUT_DIR]
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from antigravity3d.dags.prop_to_engine import PropToEngineDAG
from antigravity3d.decisions.engine import DecisionControlPlane
from antigravity3d.decisions.fakes import FakeDecisionsClient
from antigravity3d.genmedia.nano_banana import FakeImageBackend, ImageForge
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import TemplateAuthor


async def main(out: Path) -> int:
    plane = DecisionControlPlane(FakeDecisionsClient(answers={"pipeline_dag": "prop_to_engine", "needs_ortho_blueprint": 0.8, "complexity_tier": 2.0}))
    dag = PropToEngineDAG(plane=plane, forge=ImageForge(FakeImageBackend()), author=TemplateAuthor(), workdir=out, budget=EngineBudget("unreal_5"))
    rep = await dag.run("wooden crate with iron brackets")
    print(rep.summary())
    for k, v in rep.extras["outputs"].items():
        print(f"{k}: {v}")
    print("preview gates:", "PASS" if rep.extras["preview_gates_ok"] else "FAIL")
    return 0 if rep.extras["preview_gates_ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="agy3d-crate-")))))
