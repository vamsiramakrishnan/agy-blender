from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import trimesh
import yaml
from pxr import Usd

from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.state import EngineBudget
from antigravity3d.tactics.authoring import TemplateAuthor


def agy(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "antigravity3d.cli", *args], capture_output=True, text=True, timeout=timeout)


def test_models_prints_every_tier() -> None:
    r = agy("models")
    assert r.returncode == 0
    for role in ("director", "builder", "worker", "bulk", "decision", "image"):
        assert role in r.stdout
    assert "gemini-decision-1" in r.stdout


def test_prop_offline_end_to_end(tmp_path: Path) -> None:
    r = agy("prop", "wooden crate with iron brackets", "--offline", "--engine", "unreal_5", "--out", str(tmp_path / "o"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "preview gates: PASS" in r.stdout and "publication: unverified" in r.stdout and "cost:" in r.stdout
    report = json.loads((tmp_path / "o" / "report.json").read_text())
    assert report["preview_gates_ok"] and not report["verified"] and not report["gates_ok"]
    assert not (tmp_path / "o" / "package").exists()
    pkg = Path(report["outputs"]["inspector"]).parent
    (usd,) = pkg.glob("*.usd")
    assert Usd.Stage.Open(str(usd)).GetDefaultPrim()
    assert (pkg / "inspector.html").exists()


def test_sanitize_and_nonzero_exit_on_failed_gate(tmp_path: Path) -> None:
    trimesh.creation.cylinder(radius=0.3, height=1.0).export(tmp_path / "cyl.glb")
    ok = agy("sanitize", str(tmp_path / "cyl.glb"), "--engine", "godot_4", "--out", str(tmp_path / "ok"))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "cyl.glb" in ok.stdout and "gates: PASS" in ok.stdout
    bad = agy("sanitize", str(tmp_path / "cyl.glb"), "--max-tris", "10", "--out", str(tmp_path / "bad"))
    assert bad.returncode == 1 and "gates: FAIL" in bad.stdout and "tri_budget" in bad.stdout
    missing = agy("sanitize", str(tmp_path / "nope.glb"), "--out", str(tmp_path / "m"))
    assert missing.returncode == 1 and "FAILED" in missing.stdout  # a missing input is a failed row, not a crash


def test_kit_variants_calibrate_and_plugin(tmp_path: Path) -> None:
    k = agy("kit", "stone temple", "--pieces", "2", "--offline", "--out", str(tmp_path / "kit"))
    assert k.returncode == 0 and "vram:" in k.stdout, k.stdout + k.stderr
    sm = StageManager.create(tmp_path / "t.usda", "T")
    sm.apply_ir(asyncio.run(TemplateAuthor().propose("oak table", budget=EngineBudget("unreal_5"), n=1, context={"asset_name": "T"}))[0])
    sm.commit_all()
    v = agy("variants", str(tmp_path / "t.usda"), "--offline", "--out", str(tmp_path / "v"))
    assert v.returncode == 0 and "pristine" in v.stdout, v.stdout + v.stderr
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([[f"a{i}", "defect_floating", 0.9 if i % 2 else 0.05, bool(i % 2 == 0)] for i in range(40)]))
    c = agy("calibrate", str(labels), "--out", str(tmp_path / "policy.yaml"))
    assert c.returncode == 0, c.stdout + c.stderr
    assert "thresholds" in yaml.safe_load((tmp_path / "policy.yaml").read_text())
    (tmp_path / "empty").mkdir()
    p = agy("plugin", "validate", str(tmp_path / "empty"))
    assert p.returncode == 1 and "problem" in p.stdout


def test_usage_errors_exit_2() -> None:
    r = subprocess.run([sys.executable, "-m", "antigravity3d.cli", "prop", "x", "--out", "/tmp/agy3d-nokey"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}, timeout=60)
    assert r.returncode == 2 and "GEMINI_API_KEY" in r.stderr
