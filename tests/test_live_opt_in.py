"""Tests marked ``live`` spend model quota, so they run only when asked for: a key in the environment is not a request."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE = '''
import pytest

@pytest.mark.live
def test_would_spend():
    raise AssertionError("a live test ran without being asked for")
'''


def run(tmp_path: Path, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """A throwaway live-marked test under this suite's own conftest, in a child pytest, with a key present."""
    import os

    (tmp_path / "conftest.py").write_text((REPO / "tests" / "conftest.py").read_text())
    (tmp_path / "test_probe.py").write_text(PROBE)
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    live: spends quota\n")
    env = {k: v for k, v in os.environ.items() if k != "AGY3D_RUN_LIVE"}
    env.update({"GEMINI_API_KEY": "not-a-real-key", **(env_extra or {})})
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", str(tmp_path), *args],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)


def test_a_key_in_the_environment_does_not_run_live_tests(tmp_path: Path) -> None:
    r = run(tmp_path)
    assert r.returncode == 0 and "1 skipped" in r.stdout and "--run-live" in r.stdout, r.stdout + r.stderr


def test_live_tests_run_when_asked_for_by_flag_or_variable(tmp_path: Path) -> None:
    for args, extra in ((("--run-live",), None), ((), {"AGY3D_RUN_LIVE": "1"})):
        r = run(tmp_path, *args, env_extra=extra)
        assert r.returncode == 1 and "a live test ran without being asked for" in r.stdout, r.stdout + r.stderr
