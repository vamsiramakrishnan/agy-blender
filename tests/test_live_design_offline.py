"""A real worker-tier answer that uses the new shapes and the mirror / repeat shortcuts runs through the whole offline
pipeline (``tests/no_decisions_key.py`` ``live_designs``): the shortcuts expand before anything downstream reads the
design, and every copy reaches the package as a part of its own. The answer is used as written: bevels, insets, lathes
and extrusions included."""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

from antigravity3d import cli, ir_schema

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey, live_design  # noqa: E402

BRIEF = "a rugged military supply crate kit-bashed from panels, corner guards, latches and two side handles"


def test_a_live_design_with_shortcuts_builds_a_package(tmp_path, monkeypatch, capsys):
    with NoDecisionsKey(live_designs=True) as api:
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        monkeypatch.delenv("AGY3D_THREE_JS", raising=False)
        rc = cli.main(["prop", BRIEF, "--engine", "godot_4", "--max-tris", "3000", "--out", str(tmp_path / "out"), "--preview", "--token-ceiling", "150000",
                       "--max-images", "4", "--single-candidate"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert api.kinds().count("author:propose") == 1
    manifest = json.loads(Path(glob.glob(str(tmp_path / "out" / "runs" / "*" / "package" / "manifest.json"))[0]).read_text())
    text = json.dumps(manifest)
    expanded = ir_schema.parse_scene(live_design(f"asset_name='X' Build design 1/1 for {BRIEF}. Return one JSON"))
    copies = [n.id for n in expanded.nodes if "__m" in n.id or "__r" in n.id]
    assert len(copies) >= 15 and all(c in text for c in copies[:5]), copies[:5]  # the copies are parts of the package
