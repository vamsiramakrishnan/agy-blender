"""run_build (S/r6/CONTRACT.md section 4): an asset script run in a bare subprocess, its design applied to the asset session
as one checked transaction, the reply with findings and colour views; every NOT BUILT cause leaves the sessions as they
were. Driven through ``StudioHost`` offline (the fake judge), as the MCP server dispatches it."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from antigravity3d.harness import tools as T
from antigravity3d.harness.session import session
from antigravity3d.runtime import mcp_server as M

# A closed tetrahedron, faces outward: a free-form part (a mesh_ref the builder writes) that needs no repair.
TETRA = "[(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)], [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]"


def crate(name: str = "crate", engine: str = "godot_4", max_tris: int = 4000, brief: str = "a red crate with a blue lid",
          lid_colour: str = "#2040d0", extra: str = "") -> str:
    return textwrap.dedent(f"""\
        from agy3d import Asset
        a = Asset("{name}", engine="{engine}", max_tris={max_tris}, brief="{brief}")
        red = a.material("red", "#c02020", surface="red enamel paint")
        blue = a.material("blue", "{lid_colour}", surface="blue enamel paint")
        body = a.box("body", size=(1.0, 0.8, 0.6), bottom=(0, 0, 0), material=red)
        lid = a.box("lid", size=(1.05, 0.85, 0.08), bottom=(0, 0, body.top), material=blue)
        a.mesh("knob", {TETRA}, at=(-0.05, -0.05, lid.top), material=red)
        """) + extra + "a.save()\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY3D_OFFLINE", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("AGY3D_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.delenv("AGY3D_POOL_SOCKET", raising=False)
    root = tmp_path / "ws"
    root.mkdir()
    return root


def write(root: Path, text: str, name: str = "crate.py") -> str:
    (root / name).write_text(text)
    return name


def snapshot(host: M.StudioHost) -> dict:
    """Everything a failed run_build must leave as it was: each session's design, transaction stack, geometry version,
    budget, brief and script record; the active session."""
    out = {"active": host.active, "sessions": sorted(host.gates)}
    for sid in host.gates:
        s = session(host.registry, sid)
        out[sid] = (s.stage.base_ir().model_dump_json(), list(s.stage.tx_stack), s.geom_version, s.state.budget,
                    s.state.brief, json.dumps(s._res(T.R_SCRIPT), default=str, sort_keys=True))
    return out


async def build(host: M.StudioHost, root: Path, name: str = "crate.py", **kw):
    return await host.run_build(name, str(root), "conv-rb-" + "0" * 32, **kw)


async def test_built_reply_opens_a_session_and_shows_five_colour_views(ws):
    host = M.StudioHost(script=True)
    r = await build(host, ws, write(ws, crate()))
    head = r.split("\n")[0]
    assert head.startswith("BUILT crate.py -> session crate-") and "(godot_4, tris " in head and " of 4000, 3 parts, 2 materials, draws=" in head
    assert r.rstrip().endswith(M.NEXT_STEP)
    assert "VIEWS #1: 5 renders" in r and "in its material's base colour" in r and "[02] knob" in r
    assert [v for v, _ in r.images] == ["front", "right", "top", "persp", "persp_rear"]
    s = session(host.registry, host.active)
    assert s.state.budget.max_tris == 4000 and s.state.brief == "a red crate with a blue lid"
    assert [n.id for n in s.stage.base_ir().nodes] == ["body", "lid", "knob"]
    # the session reads its own copy of the hand-off folder, not the run's temporary one
    knob = next(n for n in s.stage.base_ir().nodes if n.id == "knob")
    assert Path(knob.params["path"]).is_relative_to(s.workdir / "builds")
    assert not list((ws / "agy3d_assets").glob(".run-*"))
    rec = s._res(T.R_SCRIPT)
    assert rec["path"] == "crate.py" and rec["text"] == (ws / "crate.py").read_text() and len(rec["sha256"]) == 64
    # colour: the lid's pixels in the top view are blue, the body's in the front view red
    top = np.asarray(Image.open(dict(r.images)["top"]).convert("RGB")).reshape(-1, 3).astype(int)
    assert ((top[:, 2] > top[:, 0] + 60) & (top[:, 2] > 120)).sum() > 5000
    front = np.asarray(Image.open(dict(r.images)["front"]).convert("RGB")).reshape(-1, 3).astype(int)
    assert ((front[:, 0] > front[:, 2] + 60) & (front[:, 0] > 120)).sum() > 5000


async def test_an_unchanged_rerun_answers_unchanged_and_a_changed_one_replaces_the_scene_in_one_transaction(ws):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate()))
    s = session(host.registry, host.active)
    before = snapshot(host)
    r = await build(host, ws)
    assert r.startswith("UNCHANGED: crate.py builds exactly the design session ") and "deliver_package" in r
    assert not getattr(r, "images", ()) and set(host.last_timings) == {"script"}  # nothing applied or rendered
    assert snapshot(host) == before
    # a comment-only edit is still UNCHANGED, and the session keeps the new text (it ships in the package)
    write(ws, "# tidy\n" + crate())
    assert (await build(host, ws)).startswith("UNCHANGED")
    assert s._res(T.R_SCRIPT)["text"].startswith("# tidy")
    tx = len(s.stage.tx_stack)
    write(ws, crate(lid_colour="#20a040", extra='a.box("foot", size=(0.1, 0.1, 0.05), bottom=(0.4, 0.3, -0.05), material=red)\n'))
    r = await build(host, ws)
    assert r.startswith(f"BUILT crate.py -> session {s.sid} ") and ", 4 parts," in r
    assert len(s.stage.tx_stack) == tx + 1  # one transaction replaced the scene
    assert {n.id for n in s.stage.base_ir().nodes} == {"body", "lid", "knob", "foot"}


async def test_max_tris_and_brief_change_in_place_and_an_engine_change_opens_a_new_session(ws):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate()), preview=True, token_ceiling=50_000, max_images=0)
    sid = host.active
    assert session(host.registry, sid).state.preview
    write(ws, crate(max_tris=6000, brief="a red crate, blue lid, one knob"))
    r = await build(host, ws)
    assert r.startswith(f"BUILT crate.py -> session {sid} ") and "tris " in r and "of 6000" in r
    assert "budget now 6,000 triangles (was 4,000)" in r and "brief updated" in r
    s = session(host.registry, sid)
    assert s.state.preview and s.state.budget.max_tris == 6000 and s.state.brief == "a red crate, blue lid, one knob"
    write(ws, crate(engine="unity_urp"))
    r = await build(host, ws)
    assert host.active != sid and r.startswith(f"BUILT crate.py -> session {host.active} (unity_urp")
    assert (f"NEW SESSION {host.active}: the script's engine changed from godot_4 to unity_urp, so this is a new session; "
            f"the earlier session {sid} keeps its design. This session is NOT a preview (the earlier one was): if "
            "deliver_package needs one, ask the user for their two limits and call start_preview.") in r
    assert session(host.registry, sid).state.budget.target_engine == "godot_4"  # left as it was


async def test_a_refused_budget_change_is_put_back(ws):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate()))
    before = snapshot(host)
    write(ws, crate(max_tris=10, brief="other words"))
    r = await build(host, ws)
    # refused from the design's own count, before anything is applied (seq/b11: 31,756 triangles cost 25.9 s and 1 GB)
    assert r.startswith("NOT BUILT: crate.py makes 28 triangles and the budget is 10. The heaviest parts: body 12, lid 12, knob 4.")
    assert "raise max_tris in the script's Asset line; nothing was applied; session " in r and set(host.last_timings) == {"script"}
    assert "TIER1" not in r and "warn:" not in r
    assert snapshot(host) == before
    # a fault the count cannot see (more draw calls than the engine allows) is refused after the check, in script words
    five = crate(extra="".join(f'm{k} = a.material("m{k}", "#{k}0{k}0{k}0")\na.box("b{k}", size=(0.1, 0.1, 0.1), '
                               f'bottom=({k * 0.15 - 0.3}, 0, 0.6), material=m{k})\n' for k in range(1, 4)))
    r = await build(host, ws, write(ws, five))
    assert r.startswith("NOT BUILT: crate.py makes a design the engine would reject: it needs 5 draw calls and the engine "
                        "allows 4: use fewer materials. Change those parts") and "its earlier design stays" in r, r
    assert "TIER1" not in r and snapshot(host) == before


SCRIPT_ERRORS = [
    ("import os\n", "NOT BUILT: the script was refused before it ran: crate.py line 1: import os: import of os is not allowed"),
    ("data = open('x.txt').read()\n", "NOT BUILT: the script was refused before it ran: crate.py line 1: data = open('x.txt').read(): "
                                      "open() is not allowed in a build script: a build script reads no file and writes none: "
                                      "a mesh file comes in with a.load(path)"),
    ("x = 1\nfor i in range(3)\n    x += i\n", "NOT BUILT: crate.py has a syntax error at line 2: for i in range(3) ("),
    ("from agy3d import Asset\nx = 1 / 0\n", "NOT BUILT: the script stopped with an error:\ncrate.py line 2: x = 1 / 0\nZeroDivisionError: division by zero"),
    ("from agy3d import Asset\na = Asset('c', engine='godot_4')\n", "NOT BUILT: crate.py ran to the end but wrote no design: a.save() must be its last line"),
]


@pytest.mark.parametrize("text,says", SCRIPT_ERRORS)
async def test_each_not_built_cause_leaves_every_session_as_it_was(ws, text, says):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate()))
    before = snapshot(host)
    r = await build(host, ws, write(ws, text))
    assert r.startswith(says), r
    assert f"session {host.active} is unchanged" in r and not getattr(r, "images", ())
    assert "Traceback" not in r and "runner.py" not in r and "site-packages" not in r
    assert snapshot(host) == before


async def test_a_wrong_keyword_names_the_line_and_the_signature(ws):
    host = M.StudioHost(script=True)
    r = await build(host, ws, write(ws, crate(extra='a.box("strap", sise=(1, 1, 1))\n')))
    lines = r.split("\n")
    assert lines[0] == "NOT BUILT: the script stopped with an error:"
    assert lines[1] == 'crate.py line 8: a.box("strap", sise=(1, 1, 1))'
    assert "unexpected keyword argument 'sise'" in lines[2]
    assert lines[3].startswith("expected: Asset.box(id, ") and "size" in lines[3]
    assert "no session was opened" in r and host.active is None


async def test_a_script_that_runs_too_long_is_stopped(ws):
    host = M.StudioHost(script=True)
    r = await host.run_build(write(ws, "x = 0\nwhile True:\n    x += 1\n"), str(ws), timeout_s=2)
    assert r.startswith("NOT BUILT: crate.py ran longer than 2 s and was stopped") and "no session was opened" in r


async def test_a_path_outside_the_workspace_is_refused_in_one_sentence(ws, tmp_path):
    (tmp_path / "elsewhere.py").write_text(crate())
    host = M.StudioHost(script=True)
    for path in (str(tmp_path / "elsewhere.py"), "../elsewhere.py", "missing.py"):
        r = await host.run_build(path, str(ws))
        assert r.startswith("NOT BUILT: ") and "\n" not in r and host.active is None, r


async def test_a_malformed_design_is_not_built(ws, monkeypatch):
    from antigravity3d.build import runner

    def fake(script, out, *, timeout_s=180.0, cache=None):
        out.mkdir(parents=True)
        (out / "design.json").write_text(json.dumps({"version": 1, "asset": {"name": "crate", "engine": "godot_4", "max_tris": 4000,
                                                                              "brief": "", "art_direction": ""},
                                                     "materials": [], "nodes": [{"id": "x", "kind": "blob", "params": {},
                                                                                 "transform": {"translate": [0, 0, 0]}}],
                                                     "notes": [], "stats": {}}))
        return runner.RunResult(True, out, "", "", 0.1)

    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate()))
    before = snapshot(host)
    monkeypatch.setattr(runner, "run_script", fake)
    write(ws, crate(lid_colour="#000080"))
    r = await build(host, ws)
    assert r.startswith("NOT BUILT: the design ") and "Fix the script and run_build again" in r, r
    assert snapshot(host) == before


async def test_preview_rules_apply_only_to_the_opening_call(ws):
    host = M.StudioHost(script=True)
    r = await build(host, ws, write(ws, crate()), preview=True)
    assert r.startswith("NOT BUILT: the session could not be opened: ") and "token_ceiling and max_images" in r and host.active is None
    r = await build(host, ws, preview=True, token_ceiling=40_000, max_images=0)
    assert r.startswith("BUILT") and "PREVIEW (UNVERIFIED" in r
    s = session(host.registry, host.active)
    write(ws, crate(lid_colour="#203090"))
    r = await build(host, ws, preview=True, token_ceiling=1, max_images=9)  # a later call does not reopen the preview
    assert r.startswith(f"BUILT crate.py -> session {s.sid}") and "PREVIEW (UNVERIFIED" not in r
    assert s.governor.summary() == session(host.registry, s.sid).governor.summary()


async def test_deliver_after_run_build_ships_the_script_and_says_so_in_the_provenance(ws):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate()), preview=True, token_ceiling=100_000, max_images=0)
    r = await host.call_tool("deliver_package", T.deliver_package, "", {"dest": "output"})
    assert r.startswith("DELIVERED"), r
    pkg = session(host.registry, host.active).last_export
    man = json.loads((pkg / "manifest.json").read_text())
    assert (pkg / "source" / "build.py").read_text() == (ws / "crate.py").read_text()
    # the folder the request named gets the script too, and the reply says so
    assert (ws / "output" / "source" / "build.py").read_text() == (ws / "crate.py").read_text()
    assert "source/build.py" in next(x for x in r.split("\n") if x.startswith("placed: "))
    assert "source/build.py" in man["files"] and man["file_hashes"]["source/build.py"]["bytes"] == len((ws / "crate.py").read_bytes())
    design = man["provenance"]["sources"]["design"]
    assert design["kind"] == "script" and design["path"] == "crate.py" and design["sha256"] == session(host.registry, host.active)._res(T.R_SCRIPT)["sha256"]
    made = [k for k in man["provenance"]["sources"] if k.startswith(("supplied:", "script_output:"))]
    assert made and all(k.startswith("script_output:knob-") and man["provenance"]["sources"][k]["kind"] == "script_output" for k in made)
    assert man["provenance"]["files"]["source/build.py"]["derived_from"] == ["design"]
    from antigravity3d.kernel import provenance
    from antigravity3d.kernel.validator import validate_package

    assert provenance.problems(man) == []
    assert not [i for i in validate_package(pkg) if i.severity == "critical"]
    # reopening the package in script mode points at its script; in the default mode the reply names it too
    r = await host.open_existing_asset(str(pkg), str(ws))
    assert r.startswith(f"SCRIPT {pkg / 'source' / 'build.py'} built this asset (the workspace's crate.py is the same script)")
    host.script = False
    assert f"built by the script {pkg / 'source' / 'build.py'}" in await host.open_existing_asset(str(pkg), str(ws))


async def test_a_session_built_by_run_build_is_told_to_fix_the_script(ws):
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, crate(extra='a.box("loose", size=(0.1, 0.1, 0.1), at=(2, 0, 1), material=red)\n')),
                preview=True, token_ceiling=100_000, max_images=0)
    r = await host.call_tool("deliver_package", T.deliver_package, "", {})
    assert r.startswith("NOT DELIVERED: loose is detached") and "Fix what it names in the script, then run_build and call deliver_package again." in r
    assert "apply_scene_ir" not in r


async def test_the_script_mode_server_lists_run_build_and_says_so_and_the_default_does_not(monkeypatch):
    from mcp.server.mcpserver import MCPServer

    monkeypatch.setenv(M.AUTHORING_ENV, "script")
    server = M.build_server()
    names = {t.name for t in await server.list_tools()}
    assert names == set(M.SCRIPT_MANAGEMENT_TOOLS) | set(M.inspector_app.TOOL_NAMES) | {"deliver_package"}
    assert {t.name for t in await MCPServer.list_tools(server)} == names  # the stage tools are not served at all
    assert server.instructions == M.SCRIPT_INSTRUCTIONS and M.SCRIPT_INSTRUCTIONS.startswith("antigravity-3d asset studio. An asset is one Python script")
    assert M.SCRIPT_INSTRUCTIONS.endswith(M.ir_schema.colour_note())
    schema = next(t for t in await server.list_tools() if t.name == "run_build").input_schema
    assert schema["required"] == ["path"] and set(schema["properties"]) == {"path", "conversation_id", "preview", "token_ceiling",
                                                                           "max_images", "session_id"}
    monkeypatch.delenv(M.AUTHORING_ENV)
    default = M.build_server()
    assert default.instructions == M.INSTRUCTIONS
    assert "run_build" not in {t.name for t in await MCPServer.list_tools(default)}
    assert "open_asset_session" in {t.name for t in await default.list_tools()}


# A flush stack as the reference page teaches it (``bottom=(0, 0, below.top)``): the gallery, wider than the collar under
# it and the chimney on it, has none of its own vertices within 2 mm of another part's shipped surface (the engine
# check's rule, fast_ops._PartGap), so Tier-1 reads it as floating. Found by the integrator driving the script API.
LANTERN = """\
from agy3d import Asset
a = Asset("lamp", engine="godot_4", max_tris=4000)
brass = a.material("brass", "#b08d3c", metallic=1)
glass = a.material("glass", "#c8d8d0")
tank = a.lathe("tank", profile=[(0.0, 0.085), (0.055, 0.085), (0.075, 0.07), (0.085, 0.045), (0.085, 0.02), (0.075, 0.005), (0.07, 0.0), (0.0, 0.0)], bottom=(0, 0, 0), material=brass)
collar = a.cylinder("collar", radius=0.035, height=0.03, bevel=0.003, bottom=(0, 0, tank.top), material=brass)
gallery = a.lathe("gallery", profile=[(0.0, 0.02), (0.05, 0.02), (0.056, 0.012), (0.056, 0.0), (0.0, 0.0)], bottom=(0, 0, collar.top), material=brass)
a.lathe("chimney", profile=[(0.0, 0.17), (0.028, 0.17), (0.03, 0.12), (0.048, 0.07), (0.05, 0.04), (0.045, 0.0), (0.0, 0.0)], bottom=(0, 0, gallery.top), material=glass)
"""


async def test_a_flush_stack_is_attached_in_the_same_transaction_and_a_far_part_is_left(ws, tmp_path):
    from antigravity3d.kernel.fast_ops import tier1_qa

    host = M.StudioHost(script=True)
    r = await build(host, ws, write(ws, LANTERN + "a.save()\n", "lamp.py"))
    assert r.startswith("BUILT lamp.py"), r
    moved = [x for x in r.split("\n") if x.startswith("joined: ")]
    assert moved and all(x.endswith("(it lay flush)") and len(x) < 80 for x in moved), moved
    assert "detached:" not in r
    s = session(host.registry, host.active)
    assert len(s.stage.tx_stack) == 1  # the design and its moves are one transaction
    assert not [i for i in tier1_qa(s.stage.extract_meshes(), s.state.budget).issues if i.code.startswith("floating")]
    # the same script in a fresh session makes the same moves: the same stage
    other = M.StudioHost(script=True)
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    await build(other, ws2, write(ws2, LANTERN + "a.save()\n", "lamp.py"))
    assert session(other.registry, other.active).stage.base_ir() == s.stage.base_ir()
    # a part 30 cm from everything is out of reach: left as it is and reported with its gap
    write(ws, LANTERN + 'a.cylinder("lost", radius=0.02, height=0.02, bottom=(0.4, 0, 0.3), material=brass)\na.save()\n', "lamp.py")
    r = await build(host, ws, "lamp.py")
    assert r.startswith("BUILT lamp.py") and "detached: lost (" in r and "deliver_package will refuse delivery" in r
    lost = next(n for n in s.stage.base_ir().nodes if n.id == "lost")
    assert abs(lost.transform.translate[0] - 0.4) < 1e-9


async def test_apply_scene_ir_does_not_attach(ws):
    """The default path is the baseline: apply_scene_ir with the same full scene leaves the floating part to the author."""
    host = M.StudioHost(script=True)
    await build(host, ws, write(ws, LANTERN + "a.save()\n", "lamp.py"))
    folder = next((session(host.registry, host.active).workdir / "builds").iterdir())
    design = json.loads((folder / "design.json").read_text())
    plain = M.StudioHost(script=False)
    sid = await plain._open("lamp", "godot_4", 4000, "", str(ws), "", "", False, False, None, None, "")
    r = await plain.call_tool("apply_scene_ir", T.apply_scene_ir, sid, {"ops": {"nodes": design["nodes"], "materials": design["materials"]}})
    assert r.startswith("OK") and "detached: gallery" in r and "attached" not in r.replace("detached", "")


async def test_a_joint_is_not_a_finding_and_a_buried_part_is(ws):
    """A script joins parts by setting one into the other, so Tier-1's interpenetration warning (any crossing at all) is
    left out of the reply, even for a joint set deep (a neck in a tank, a boss in a wall: 53 and 71 % live, 2026-11-23);
    a part that lies almost entirely inside another is reported, once, as a fact."""
    host = M.StudioHost(script=True)
    extra = ('a.cylinder("peg", radius=0.05, height=0.3, bottom=(0.3, 0, lid.top - 0.05), material=red)\n'   # 17 % in the lid
             'a.box("boss", size=(0.2, 0.2, 0.2), at=(-0.3, 0, 0.56), material=blue)\n'                     # 70 % in the body
             'a.sphere("nub", radius=0.1, at=(0.405, 0, 0.3), material=blue)\n')                              # pokes 5 mm out
    r = await build(host, ws, write(ws, crate(extra=extra)))
    assert r.startswith("BUILT ")
    assert "interpenetration" not in r and "probe points" not in r
    deep = [x for x in r.split("\n") if "almost entirely inside" in x]
    assert len(deep) == 1 and "nub" in deep[0] and deep[0].startswith("warning: ") and deep[0].endswith("it will not be seen")
    assert not any(p in x for x in r.split("\n") if x.startswith("warning") for p in ("peg", "boss"))


def test_script_warning_words_each_kind():
    class Issue:
        def __init__(self, code, node_id, detail):
            self.code, self.node_id, self.detail = code, node_id, detail

    assert T.script_warning(Issue("interpenetration", "a", "a/b: ~34% of probe points inside")) is None
    assert T.script_warning(Issue("interpenetration", "a", "a/b: ~71% of probe points inside")) is None
    assert T.script_warning(Issue("interpenetration", "a", "a/b: ~89% of probe points inside")) is None
    deep = T.script_warning(Issue("interpenetration", "a", "a/b: ~90% of probe points inside"))
    assert deep == "warning: a lies almost entirely inside b (about 90 %): it will not be seen"
    assert T.script_warning(Issue("not_watertight", "fin", "3 boundary edges")) == "warning: not_watertight on fin: 3 boundary edges"
    assert T.script_warning(Issue("z_fighting", "", "")) == "warning: z_fighting"


async def test_the_opening_reply_of_a_preview_without_images_speaks_of_the_script_and_attached_views_carry_no_path(ws, monkeypatch):
    monkeypatch.setenv("AGY3D_GENERATED_SURFACES", "1")  # the suite's default is the flat path (tests/conftest.py)
    host = M.StudioHost(script=True)
    r = await build(host, ws, write(ws, crate()), preview=True, token_ceiling=150_000, max_images=0)
    note = next(x for x in r.split("\n") if x.startswith("PREVIEW"))
    assert "NO IMAGES in this session (image limit 0)" in note and "in the script" in note
    # nothing a script session does not have: no stage, no manifest reader, no shader tool
    for word in ("LOOKDEV", "inspect_manifest", "apply_shader_diff", "apply_scene_ir"):
        assert word not in r, word
    # every view attached: one line names the five, without paths (review: about 170 characters a view)
    views = [x for x in r.split("\n") if x.startswith("attached, in this order: ")]
    assert len(views) == 1 and "/" not in views[0] and views[0].startswith("attached, in this order: front (front (orthographic)), right")
    # the default path's reply keeps its paths (tests/test_native_views.py pins render_preview's text)
    assert all(p.stat().st_size <= T.INLINE_IMAGE_BYTES for _, p in r.images)


FIVE_MATERIALS = "".join(f'm{k} = a.material("m{k}", "#{k}0{k}0{k}0")\na.box("b{k}", size=(0.1, 0.1, 0.1), bottom=({k * 0.15 - 0.3}, 0, 0.6), material=m{k})\n'
                         for k in range(1, 4))


async def test_the_judge_notice_comes_with_the_first_reply_after_the_session_opens_whatever_its_first_word(ws, monkeypatch):
    """Found by review (seq/b4.py): an opening build that was refused opened the session silently, the next build said
    nothing, and the agent first heard of the missing judge from deliver_package. The notice is in script words."""
    from antigravity3d.decisions.engine import DecisionControlPlane

    monkeypatch.setattr(DecisionControlPlane, "calibrated", property(lambda self: False))
    host = M.StudioHost(script=True)
    r = await build(host, ws, write(ws, crate(extra=FIVE_MATERIALS)))
    assert r.startswith("NOT BUILT: crate.py makes a design the engine would reject") and host.active is not None
    notice = ("WARNING: this key has no calibrated judge, so deliver_package will be refused until the user allows an "
              "unverified preview: ask the user whether they want one and for their two limits (a token ceiling and an "
              "image limit), then call start_preview with them; the design is kept.")
    assert r.endswith("\n" + notice)
    assert "run_visual_qa" not in r and "export_engine_package" not in r
    r = await build(host, ws, write(ws, crate()))
    assert r.startswith("BUILT") and notice not in r  # said once
    # start_preview in script mode: no visual QA to re-run
    r = await host.start_preview(100_000, 0)
    assert r.startswith(f"SESSION {host.active} is now an UNVERIFIED preview: PREVIEW (UNVERIFIED") and "visual QA" not in r
    assert "deliver_package(dest=...)" in r
    # deliver_package on a script session that holds no design names the script, not apply_scene_ir (seq/b3.py)
    other = M.StudioHost(script=True)
    await build(other, ws, write(ws, crate(extra=FIVE_MATERIALS)), preview=True, token_ceiling=100_000, max_images=0)
    r = await other.call_tool("deliver_package", T.deliver_package, "", {})
    assert "nothing to deliver: the scene is empty (write the script and run_build it)" in r and "apply_scene_ir" not in r, r


async def test_a_compact_legend_at_forty_parts(ws):
    host = M.StudioHost(script=True)
    many = "".join(f'a.box("slat_{k:02d}", size=(0.02, 0.8, 0.05), bottom=({k * 0.025 - 0.45}, 0, 0.68), material=red)\n'
                   for k in range(36))
    r = await build(host, ws, write(ws, crate(extra=many)))
    assert r.startswith("BUILT") and ", 39 parts," in r
    legend = r.split("Legend (badge, part):\n", 1)[1].split("\nNEXT:", 1)[0]
    assert "[01] body" in legend and "views:" not in legend and len(legend) < 900
    assert all(len(x) <= 110 for x in legend.split("\n"))


async def test_open_existing_asset_in_script_mode_on_a_package_no_script_built(ws, tmp_path):
    from test_harness_tools import CRATE

    plain = M.StudioHost(script=False)
    sid = await plain._open("crate", "godot_4", 4000, "", str(ws), "", "", False, True, 100_000, 0, "")
    await plain.call_tool("apply_scene_ir", T.apply_scene_ir, sid, {"ops": CRATE["ops"]})
    r = await plain.call_tool("deliver_package", T.deliver_package, sid, {})
    assert r.startswith("DELIVERED"), r
    pkg = session(plain.registry, sid).last_export
    host = M.StudioHost(script=True)
    r = await host.open_existing_asset(str(pkg), str(ws))
    assert r.startswith(f"NOT A SCRIPT ASSET: {pkg} was not built by a script") and "\n" not in r and host.active is None
    for word in ("apply_scene_ir", "TWEAK", "inspect_manifest", "render_preview", "stage"):
        assert word not in r, word


async def test_script_mode_descriptions_use_no_stage_vocabulary(monkeypatch):
    monkeypatch.setenv(M.AUTHORING_ENV, "script")
    tools = {t.name: t.description for t in await M.build_server().list_tools()}
    for name in ("open_existing_asset", "session_status", "run_build", "start_preview"):
        for word in ("stage", "TWEAK", "BLOCKOUT", "tier15", "apply_scene_ir", "open_asset_session"):
            assert word not in tools[name], (name, word)


STOOL = textwrap.dedent("""\
    from agy3d import Asset
    a = Asset("stool", engine="godot_4", max_tris=3000, brief="a three-legged wooden stool with a bent foot ring")
    oak = a.material("oak", "#8a5a2b", surface="oiled oak, satin, worn at the edges", roughness=0.55)
    iron = a.material("iron", "#2b2b2e", surface="blackened wrought iron", metallic=1, roughness=0.6)
    seat = a.cylinder("seat", radius=0.16, height=0.04, bevel=0.008, bevel_segments=2, bottom=(0, 0, 0.42), material=oak)
    feet = [(0.17, 0.0), (-0.085, 0.147), (-0.085, -0.147)]
    for k, (x, y) in enumerate(feet):
        a.tube(f"leg_{k}", path=[(x * 0.6, y * 0.6, seat.bottom + 0.012), (x, y, 0)], radius=[0.019, 0.013], sides=8, material=oak)
    a.tube("ring", path=[(0.6 * x * 0.62 + 0.4 * x, 0.6 * y * 0.62 + 0.4 * y, 0.17) for x, y in feet], radius=0.006, sides=6,
           closed=True, smooth=True, material=iron)
    a.loft("pad", [[(-0.1, -0.1, seat.top - 0.002), (0.1, -0.1, seat.top - 0.002), (0.1, 0.1, seat.top - 0.002), (-0.1, 0.1, seat.top - 0.002)],
                   [(-0.08, -0.08, seat.top + 0.015), (0.08, -0.08, seat.top + 0.015), (0.08, 0.08, seat.top + 0.015), (-0.08, 0.08, seat.top + 0.015)]],
           material=iron)
    a.save()
    """)


@pytest.mark.blender
async def test_a_script_with_free_form_parts_is_delivered_past_the_real_blender_check(ws, monkeypatch):
    """The script path end to end with real engines: a stool whose legs, foot ring and pad are free-form parts (tube,
    closed smooth tube, loft) is built by run_build and delivered by deliver_package with a real Blender worker: the
    package's own Blender check passes and, where Godot is installed, the engine imports it. The generated surfaces are
    on, as in a preview without images."""
    import os

    from antigravity3d.adapters.blender_5_3.pool import BlenderPool, find_blender

    if not find_blender():
        pytest.skip("no Blender binary ($BLENDER_BIN or PATH)")
    monkeypatch.setenv("AGY3D_GENERATED_SURFACES", "1")
    host = M.StudioHost(script=True)
    async with BlenderPool(1, startup_timeout=180) as pool:
        await host.backends.ensure()
        host.backends.pool = pool
        r = await build(host, ws, write(ws, STOOL, "stool.py"), preview=True, token_ceiling=200_000, max_images=0)
        assert r.startswith("BUILT stool.py -> session stool-"), r
        assert "6 parts, 2 materials" in r.split("\n")[0] and not [x for x in r.split("\n") if x.startswith(("detached", "warning"))], r
        d = await host.call_tool("deliver_package", T.deliver_package, "", {"dest": "output"})
        assert d.startswith("DELIVERED"), d
        assert "blender_asset_qa passed" in d, d
        if os.environ.get("GODOT_BIN"):
            assert "engine_import ok" in d, d
        assert (ws / "output" / "stool.glb").is_file() and (ws / "output" / "source" / "build.py").read_text() == STOOL
