"""The story-slides skill says true things about its starter.

The staging guide (``skills/story-slides-3d/staging.md``) teaches by quoting the starter: every fenced ``js`` block in
it starts with ``// from starter/<path>`` and holds lines of that file. The starter's own test
(``tests/test_story_slides_starter.py``) runs those files in a browser, so code in the guide is code that ran; this test
keeps the two from drifting apart, and holds the names the skill's tables give (treatments, cuts, fields of the state)
to the starter's source. No browser is needed.
"""

from __future__ import annotations

import re
from pathlib import Path

from antigravity3d.plugin_install import bundle_dir

SKILL = bundle_dir() / "skills" / "story-slides-3d"
STARTER = SKILL / "starter"
_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.S)
_FROM = re.compile(r"// from (starter/[\w./-]+)")


def _blocks(doc: str) -> list[tuple[str, list[str]]]:
    return [(m.group(1), m.group(2).splitlines()) for m in _BLOCK.finditer((SKILL / doc).read_text(encoding="utf-8"))]


def _keys(path: Path, const: str) -> list[str]:
    """The keys of ``export const <const> = { ... }`` written one per line at two spaces of indent."""
    body = path.read_text(encoding="utf-8").split(f"export const {const} = {{", 1)[1]
    body = body[: re.search(r"(?m)^\};", body).start()]
    return re.findall(r"(?m)^  ([a-z][A-Za-z0-9]*): \{", body)


def test_every_block_of_code_in_the_guide_is_an_excerpt_of_the_starter() -> None:
    blocks = _blocks("staging.md")
    assert len(blocks) >= 8, "the guide teaches with the starter's code"
    for lang, lines in blocks:
        assert lang == "js" and lines and _FROM.fullmatch(lines[0]), f"a block that names no file of the starter: {lines[:2]}"


def test_every_line_of_an_excerpt_is_a_line_of_the_file_it_names() -> None:
    for _lang, lines in _blocks("staging.md"):
        rel = _FROM.fullmatch(lines[0]).group(1)
        source = SKILL / rel
        assert source.is_file(), rel
        have = {line.strip() for line in source.read_text(encoding="utf-8").splitlines()}
        have |= {line[:-2] if line.endswith("`,") else line[:-1] for line in have if line.endswith(("`,", "`"))}  # GLSL in a template
        gone = [line for line in lines[1:] if line.strip() and line.strip() not in have]
        assert not gone, f"{rel} no longer holds: {gone}"


def test_the_treatments_and_cuts_the_skill_names_are_the_starters() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    treatments, cuts = _keys(STARTER / "src" / "print.js", "TREATMENTS"), _keys(STARTER / "src" / "cuts.js", "CUTS")
    assert treatments == ["grade", "halftone", "glyph"] and cuts == ["fade", "iris", "wipe", "fall", "flip"]
    rows = re.findall(r"(?m)^\s*\| `([a-z]+)` \|", skill)  # the first cell of a table row, when it is one plain name
    assert [r for r in rows if r in treatments] == treatments, "the treatments table names each, in the starter's order"
    assert [r for r in rows if r in cuts] == cuts, "the cuts table names each, in the starter's order"
    assert "three print treatments, five cuts" in skill


def test_the_fields_of_the_state_the_skill_lists_are_the_engines() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"(?m)^\| `s\.([a-z]+)", skill))
    film = (STARTER / "src" / "film.js").read_text(encoding="utf-8")
    state = film.split("function state(", 1)[1].split("\n  }\n", 1)[0]
    made = set(re.findall(r"\b(beat|p|t|enter|exit|still|aspect|pointer|k)\b(?=[,:])", state.split("return {", 1)[1]))
    assert listed == {"beat", "p", "k", "t", "enter", "pointer", "aspect", "still"}, listed
    assert listed | {"exit"} == made, (sorted(listed), sorted(made))


def test_the_starter_holds_the_files_the_skill_and_the_guide_name() -> None:
    for rel in ("index.html", "vite.config.js", "src/story.js", "src/style.css", "src/props.js", "src/print.js", "src/cuts.js",
                "src/kit.js", "src/film.js", "src/main.js", "src/caption.js"):
        assert (STARTER / rel).is_file(), rel
    assert sorted(p.stem for p in (STARTER / "src" / "scenes").glob("*.js")) == ["end", "room", "shore", "title", "tower"]
    kit = (STARTER / "src" / "kit.js").read_text(encoding="utf-8")
    for name in ("mat", "spot", "gobo", "haze", "windows", "card", "figure", "prop", "fit", "ease", "rnd"):
        assert re.search(rf"export (?:async )?function {name}\(", kit), f"the kit has no {name}"
