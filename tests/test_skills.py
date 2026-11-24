"""The plugin's skills are valid skills, say true things and can be chosen from their descriptions.

* format: every skill passes ``skill_check.problems`` (name, description with a closing "Not for", Gotchas, Version
  notes, check section, links), and the checker itself flags a bad skill;
* claims: the numbers, flags, files and manifest keys that the new sections state are compared with the code they
  describe (tests/test_plugin_docs.py checks tool and operation names);
* triggers: ``benchmarks/skill_triggers/triggers.json`` is a valid trigger set for these skills, and the runner and the
  Gemini judge command work against a stand-in (scoring itself needs a live model and is run by hand: see that
  folder's README).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import skill_check  # noqa: E402

from antigravity3d import art_direction, cli, state  # noqa: E402
from antigravity3d.authoring_contract import AuthoringIntent, PROP_CRAFT_GUIDANCE  # noqa: E402
from antigravity3d.dags import batch_sanitize  # noqa: E402
from antigravity3d.decisions.compose import Thresholds  # noqa: E402  (brief_depiction_min)
from antigravity3d.harness import agents  # noqa: E402
from antigravity3d.tactics import metered_author  # noqa: E402
from antigravity3d.deliver import godot as deliver_godot  # noqa: E402
from antigravity3d.ir import NodeIR  # noqa: E402
from antigravity3d.kernel.compile_ir import check_node, triangle_count  # noqa: E402
from antigravity3d.kernel import separate, validator  # noqa: E402
from antigravity3d.kernel import provenance  # noqa: E402
from antigravity3d.kernel.kit_metric import KitMetric  # noqa: E402
from antigravity3d.plugin_install import bundle_dir  # noqa: E402
from antigravity3d import service_answer  # noqa: E402

BUNDLE = bundle_dir()
REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "antigravity3d"
TRIGGERS = REPO / "benchmarks" / "skill_triggers"
SKILL_DIRS = sorted(p.parent for p in BUNDLE.glob("skills/*/SKILL.md"))
NAMES = {p.name for p in SKILL_DIRS}
TEXT = {p.name: (p / "SKILL.md").read_text(encoding="utf-8") for p in SKILL_DIRS}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TRIGGERS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


runner = _load("run_trigger_evals")
judge = _load("gemini_judge")


# --------------------------------------------------------------------------- format


def test_the_eight_skills_are_found() -> None:
    # scroll-3d-website (round eight): building a scroll-driven 3D site around the props, which no skill covered in run 1
    assert NAMES == {"art-direction", "concept-to-blueprint", "engine-truth-verify", "game-ready-sanitizer",
                     "install-into-project", "parametric-kitbash", "pbr-material-forge", "scroll-3d-website"}


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_is_valid(skill: Path) -> None:
    found = skill_check.problems(skill, NAMES, (BUNDLE, SRC, REPO))
    assert not found, "\n".join(found)


def test_checker_flags_a_bad_skill(tmp_path: Path) -> None:
    d = tmp_path / "wrong-dir"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: Does things <b>.\nextra: 1\n---\n# x\n\n## Gotchas\n- oops\n"
                                "\nSee [gone](missing.md) and `nowhere.json`.\n", encoding="utf-8")
    found = " | ".join(skill_check.problems(d, {"a-skill"}))
    for expect in ("exactly name and description", "name 'Bad_Name'", "must equal its directory", "angle brackets", "Use when",
                   "Not for", "Version notes", "'## Check", "not symptom, cause, remedy", "missing.md", "nowhere.json"):
        assert expect in found, expect


def test_not_for_clause_must_name_real_neighbours_and_close_the_description() -> None:
    ok = "Does a thing. Use when asked. Not for a (use pbr-material-forge) or b (use art-direction)."
    assert skill_check.not_for_targets(ok) == ["pbr-material-forge", "art-direction"]
    assert skill_check.not_for_targets("Not for x (use a-skill). Use when asked.") == []
    assert skill_check.not_for_targets("Does a thing, not for the faint.") == []


def test_every_skill_has_neighbours_that_point_back_somewhere() -> None:
    """A skill that is named by no other skill's 'Not for' clause is one nothing can confuse it with, or a gap."""
    named = {t for n in NAMES for t in skill_check.not_for_targets(skill_check.frontmatter(TEXT[n])[0]["description"])}
    assert named == NAMES, f"never named as the owner of nearby work: {sorted(NAMES - named)}"


def test_no_description_still_promises_a_blockout() -> None:
    """Round four changed the modelling stage to ask for a finished prop; a description that says blockout or greybox
    is what a blind judge held against the models."""
    for name, text in TEXT.items():
        desc = skill_check.frontmatter(text)[0]["description"].lower()
        assert "greybox" not in desc and not re.search(r"(?<!not a )blockout", desc), name


# --------------------------------------------------------------------------- claims against the code


def _all_text() -> str:
    return "\n".join(TEXT.values())


def test_cited_threshold_is_the_gate() -> None:
    cited = set(re.findall(r"(?:Tier-1\.5|tier15)[^.\n]{0,40}?(\d\.\d)", _all_text()))
    assert cited == {str(state.EXPORT_MIN_TIER15_SCORE)}


def test_cited_numbers_match_the_code() -> None:
    t = _all_text()
    assert state.EXPORT_MIN_TIER15_SCORE == 3.4
    assert f"{separate.MARGIN * 1000:g} mm apart" in re.sub(r"\s+", " ", t) or f"{separate.MARGIN * 1000:g} mm by arithmetic" in re.sub(r"\s+", " ", t)
    assert separate.MARGIN == pytest.approx(0.002)
    assert f"default {batch_sanitize.DEFAULT_TIMEOUT_S:g} s" in re.sub(r"\s+", " ", t)
    assert f"above {validator.SCALE_MAX_M:g} m or below {validator.SCALE_MIN_M * 100:g} cm" in re.sub(r"\s+", " ", t)
    assert f"{', '.join(f'{d:g}' for d in service_answer.DEFAULT_DELAYS_S[:-1])} and {service_answer.DEFAULT_DELAYS_S[-1]:g} seconds" in re.sub(r"\s+", " ", t)
    assert f"below {Thresholds().brief_depiction_min:g} of 4" in re.sub(r"\s+", " ", t)
    assert f"no larger than {KitMetric().max_face_m:g} m on their shorter side" in re.sub(r"\s+", " ", t)
    flat = re.sub(r"\s+", " ", t)
    production = agents._system("BLOCKOUT", authoring_intent=AuthoringIntent.PRODUCTION_PROP)
    assert metered_author.PROP_CRAFT_GUIDANCE == PROP_CRAFT_GUIDANCE and PROP_CRAFT_GUIDANCE in production
    assert "50 to 90 parts" in production and "50 to 90 parts when max_tris allows" in flat
    # A production target follows the budget and art direction (retro-lowres: 400 tris); intermediate stages omit it.
    assert "when max_tris allows it (50 plain boxes are 600 triangles)" in PROP_CRAFT_GUIDANCE
    assert "with a smaller max_tris use as many as fit, and fewer where an art direction asks for few" in PROP_CRAFT_GUIDANCE
    assert "50 to 90 parts" not in agents._system("BLOCKOUT")
    assert 50 * triangle_count(NodeIR(id="b", kind="box", params={"size": (1, 1, 1)})) == 600
    assert deliver_godot.HYSTERESIS == 0.025 and "2.5 % margin" in flat


def test_style_figures_are_the_styles_files() -> None:
    low = art_direction.all_styles()["retro-lowres"]
    assert (low.tri_budget_prop, low.texture_px_prop, low.texture_filter, low.palette_max_colors) == (400, 128, "nearest", 64)
    flat = re.sub(r"\s+", " ", TEXT["art-direction"])
    assert "400 triangles for a prop, 128 px maps, nearest filtering, 64 colours" in flat
    assert set(art_direction.ALIASES.values()) == {"retro-lowres"}  # the table of the skill shows the one alias
    shipped = {i for i, s in art_direction.all_styles().items() if s.tri_budget_prop or s.texture_px_prop or s.shading_crease_deg is not None}
    assert {"low-poly", "retro-lowres", "voxel", "realistic"} <= shipped


def test_cli_flags_named_in_skills_exist() -> None:
    flags = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", _all_text()))
    known = set(re.findall(r"""add_argument\(\s*"(--[a-z-]+)""", (SRC / "cli.py").read_text()))
    assert flags and flags <= known, sorted(flags - known)
    subcommands = set(re.findall(r"agy3d ([a-z]+)", _all_text()))
    assert subcommands <= set(re.findall(r'add_parser\("([a-z]+)"', (SRC / "cli.py").read_text())), subcommands


def test_files_and_manifest_keys_named_in_skills_exist() -> None:
    manifest_sources = "\n".join((SRC / p).read_text() for p in ("kernel/package.py", "dags/prop_to_engine.py",
                                                                  "adapters/blender_5_3/fbx_package.py", "art_direction.py",
                                                                  "kernel/provenance.py", "tactics/pyramid.py", "harness/tools.py"))
    for key in set(re.findall(r"`((?:validation|fbx|art_direction|provenance)\.[a-z_.]+)`", _all_text())):
        for part in key.split("."):
            assert f'"{part}"' in manifest_sources or f"'{part}'" in manifest_sources or part in manifest_sources, key


def test_node_kinds_and_stage_ids_named_in_skills_exist() -> None:
    from antigravity3d import ir
    import typing

    kinds = set(typing.get_args(ir.NodeIR.model_fields["kind"].annotation))
    # the kitbash skill lists every kind the schema has, and no other: a kind added to the format must reach the skill
    listed = set(re.findall(r"\b(box|cylinder|cone|sphere|capsule|lathe|extrude|group|mesh_ref)\b",
                            re.search(r"## Version notes.*?Node kinds: ([^.]*)\.", TEXT["parametric-kitbash"], re.S).group(1)))
    assert listed == kinds, (sorted(kinds ^ listed))
    from antigravity3d.harness import tools as T
    for stage in set(re.findall(r"\b(BLOCKOUT|VARIANTS|LOOKDEV|EXPORT)\b", _all_text())):
        assert stage in T.STAGE_TOOLS, stage


def test_shape_costs_and_refusals_quoted_by_the_kitbash_skill_are_the_kernels() -> None:
    flat = re.sub(r"\s+", " ", TEXT["parametric-kitbash"])
    box = lambda **p: NodeIR(id="a", kind="box", params={"size": (1, 1, 1), **p})  # noqa: E731
    assert (triangle_count(box()), triangle_count(box(bevel=0.02, bevel_segments=1)), triangle_count(box(bevel=0.02, bevel_segments=2))) == (12, 44, 108)
    assert "a box is 12 triangles, bevelled 44 (1 segment) or 108 (2), plus 16 per inset face" in flat
    assert triangle_count(box(inset_faces=["+x"], inset_margin=0.1, inset_depth=0.01)) == 12 + 16
    assert triangle_count(NodeIR(id="e", kind="extrude", params={"outline": [(0, 0), (1, 0), (1, 1), (0, 1)], "depth": 0.1})) == 4 * 4 - 4
    for params, quoted in [({"size": (0.2, 0.2, 0.2), "bevel": 0.5}, "bevel 0.5 must be below half the smallest extent (0.1)"),
                           ({"size": (1, 1, 1), "inset_faces": ["+x"], "inset_margin": 0.0, "inset_depth": 0.01},
                            "inset_margin must be > 0 when inset_faces is set (without a frame the recess cuts the edge away)"),
                           ({"size": (1, 1, 1), "inset_faces": ["+x"], "inset_margin": 0.6, "inset_depth": 0.01}, "inset_margin 0.6 leaves no panel on face +x")]:
        with pytest.raises(ValueError) as e:
            check_node(NodeIR(id="a", kind="box", params=params))
        assert quoted in str(e.value) and quoted.replace("bevel 0.5 must", "bevel 0.5 must") in flat.replace("\"", "")
    from antigravity3d import ir_shortcuts
    assert f"2 to {ir_shortcuts.MAX_REPEAT} with the original" in flat


def test_install_skill_names_the_checks_and_files_of_the_install() -> None:
    src = (SRC / "deliver" / "install.py").read_text()
    checks = set(re.findall(r'Check\("([a-z_]+)"', src)) - {"in_project"}
    text = re.sub(r"\s+", " ", TEXT["install-into-project"])
    assert {"import", "scene", "scale", "levels", "materials", "collision", "level_switching"} == checks
    for name in checks - {"scene"}:  # the loaded scene is part of the import check in the skill's wording
        assert name in text, name
    assert "Seven checks" in text and "agy3d/<asset>" in text and ".agy3d/install.json" in text
    from antigravity3d.deliver import install
    assert install.RECORD == ".agy3d/install.json"


def test_design_loop_numbers_follow_the_code() -> None:
    """Round five's loop settings are PROVISIONAL (plan settings chosen from the level texts until the live curve of
    score per round exists); the skills state them in words, and this test reads the settings from the code so that the
    text cannot drift from them."""
    from antigravity3d.tactics import governor, pyramid

    flat = re.sub(r"\s+", " ", TEXT["parametric-kitbash"])
    assert f"{pyramid.AUTHOR_VIEW_PX} px on a side" in flat
    assert f"stops after {'two' if pyramid.REPAIR_PATIENCE == 2 else pyramid.REPAIR_PATIENCE} undone repairs in a row" in flat
    rounds = [governor.PLANS[i].improve_rounds for i in sorted(governor.PLANS)]
    assert "improvement rounds per spend plan" in flat and rounds == sorted(rounds)  # clutter, simple, standard, hero: more for more
    assert all(governor.PLANS[i].improve_target == governor.IMPROVE_TARGET for i in governor.PLANS)


# --------------------------------------------------------------------------- triggers


def test_trigger_file_is_valid_for_these_skills(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--check"]) == 0
    assert "evals OK" in capsys.readouterr().out
    data = json.loads((TRIGGERS / "triggers.json").read_text())
    for skill in NAMES:
        assert sum(1 for i in data if i["id"].startswith(skill + ".s")) >= 3
        assert any(i["id"] == skill + ".nm" and i["kind"] == "near-miss" for i in data)
    assert {i["kind"] for i in data} == {"should", "near-miss", "combo", "none"}


def test_the_runner_scores_with_a_judge_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A stand-in judge that picks a skill by a word of the request: the runner builds the selector prompt from the
    plugin's own descriptions, parses the judge's list and reports."""
    judge_py = tmp_path / "judge.py"
    judge_py.write_text("import sys,json\nt=sys.stdin.read().split('----- request start -----')[1].lower()\n"
                        "print(json.dumps(['pbr-material-forge'] if 'texture' in t else []))\n")
    assert runner.main(["--judge-cmd", f"{sys.executable} {judge_py}", "--json", "--kind", "none"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["kinds"]["none"] == {"passed": 6, "total": 6, "rate": 1.0}
    assert "pbr-material-forge: Creates the surface" in runner.selector_prompt(runner.load_skills(REPO), "x")


def test_the_runner_reads_an_older_checkout_against_the_same_file(tmp_path: Path) -> None:
    old = tmp_path / "plugin" / "antigravity-3d" / "skills" / "art-direction"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("---\nname: art-direction\ndescription: >-\n  Old text.\n---\n")
    assert runner.load_skills(tmp_path) == {"art-direction": "Old text."}


class _Client:
    class models:  # noqa: N801 - mimics client.models
        seen: dict = {}

        @classmethod
        def generate_content(cls, *, model, contents, config):
            cls.seen = {"model": model, "contents": contents, "temperature": config.temperature}
            return type("R", (), {"text": '["art-direction"]'})()


def test_the_gemini_judge_sends_only_the_prompt_and_reads_the_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert judge.ask("PROMPT", "m", client=_Client) == '["art-direction"]'
    assert _Client.models.seen == {"model": "m", "contents": "PROMPT", "temperature": 0.0}
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        judge.ask("PROMPT", "m")
    assert judge.DEFAULT_MODEL == "gemini-4-hydrogen"
    src = (TRIGGERS / "gemini_judge.py").read_text()
    assert "--api-key" not in src and "--key" not in src


def test_the_provenance_keys_the_skill_quotes_are_in_a_real_record() -> None:
    """`provenance.generated_content`, `.models` and `.unrecorded` are the record's own keys (kernel/provenance.finalize)."""
    data = {"asset": "P", "inspector": "i.html", "files": ["P.glb"], "textures": []}
    prov = provenance.finalize(provenance.build(data, asset="P"))
    for key in set(re.findall(r"`provenance\.([a-z_]+)`", TEXT["engine-truth-verify"])):
        assert key in prov, key
    assert provenance.SCHEMA_VERSION == 4 and "schema version 4" in TEXT["engine-truth-verify"]
