"""The story-slides skill says true things about its starter.

The staging guide (``skills/story-slides-3d/staging.md``) teaches by quoting the starter: every fenced ``js`` block in
it starts with ``// from starter/<path>`` (or ``// from examples/film/<path>``) and holds lines of that file. The
starter's own tests run those files in a browser, so code in the guide is code that ran; this test keeps the two from
drifting apart, and holds the names the skill's tables give (themes, layouts, mechanisms, cuts, fields of the state)
to the starter's source. No browser is needed.
"""

from __future__ import annotations

import re
from pathlib import Path

from antigravity3d.plugin_install import bundle_dir

SKILL = bundle_dir() / "skills" / "story-slides-3d"
STARTER = SKILL / "starter"
_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.S)
_FROM = re.compile(r"// from ((?:starter|examples/film)/[\w./-]+)")


def _blocks(doc: str) -> list[tuple[str, list[str]]]:
    return [(m.group(1), m.group(2).splitlines()) for m in _BLOCK.finditer((SKILL / doc).read_text(encoding="utf-8"))]


def _keys(path: Path, const: str) -> list[str]:
    """The keys of ``export const <const> = { ... }`` written one per line at two spaces of indent."""
    body = path.read_text(encoding="utf-8").split(f"export const {const} = {{", 1)[1]
    body = body[: re.search(r"(?m)^\};", body).start()]
    return re.findall(r"(?m)^  ([a-z][A-Za-z0-9]*): \{", body)


def _table(skill: str, heading_cell: str) -> list[str]:
    """Every plain name in the first cell of the rows of the table whose header's first cell is ``heading_cell``
    (a cell may hold two names: "`left`, `right`")."""
    lines = skill.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"| {heading_cell} |"))
    names: list[str] = []
    for line in lines[start + 2:]:
        if not line.strip().startswith("|"):
            break
        names += re.findall(r"`([a-z][a-z0-9-]*)`", line.strip().split("|")[1])
    return names


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


def test_the_themes_layouts_mechanisms_and_cuts_the_skill_names_are_the_starters() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    themes = _keys(STARTER / "src" / "theme.js", "THEMES")
    assert themes == ["paper", "ink", "poster", "halftone", "glyph"] and _table(skill, "theme") == themes
    css = (STARTER / "src" / "theme.css").read_text(encoding="utf-8")
    assert all(f'[data-story-theme="{t}"]' in css for t in themes[1:]), "every theme but the default has its block of tokens"
    cuts = _keys(STARTER / "src" / "cuts.js", "CUTS")
    assert [c for c in cuts if c != "travel"] == ["fade", "iris", "wipe", "fall", "flip"] == _table(skill, "cut")
    layouts = _table(skill, "layout")
    assert layouts == ["title", "statement", "left", "right", "number", "end", "caption"]
    page = (STARTER / "src" / "style.css").read_text(encoding="utf-8") + (STARTER / "src" / "caption.js").read_text(encoding="utf-8")
    assert all(name in page for name in layouts), [name for name in layouts if name not in page]
    files = sorted(p.stem for p in (STARTER / "src" / "mechanisms").glob("*.js"))
    named = sorted(set(re.findall(r"`([a-z]+)`", " ".join(line.split("|")[3] for line in skill.splitlines()
                                                          if line.count("|") == 4 and "| show it as |" not in line and "---" not in line
                                                          and line.strip().startswith("| a ")))))
    assert set(named) <= set(files) and {"field", "stack", "ruler", "frame", "streams", "bars"} <= set(files), (named, files)
    assert "five themes" in skill and "seven layouts" in skill and "five cuts" in skill


def test_the_fields_of_the_state_the_skill_lists_are_the_engines() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"(?m)^\| `s\.([a-z]+)", skill))
    film = (STARTER / "src" / "film.js").read_text(encoding="utf-8")
    state = film.split("function state(", 1)[1].split("\n  }\n", 1)[0]
    made = set(re.findall(r"\b(beat|p|t|enter|exit|still|aspect|pointer|k)\b(?=[,:])", state.split("return {", 1)[1]))
    assert listed == {"beat", "p", "k", "t", "enter", "pointer", "aspect", "still"}, listed
    assert listed | {"exit"} == made, (sorted(listed), sorted(made))


def test_the_starter_and_the_film_example_hold_the_files_the_skill_and_the_guide_name() -> None:
    for rel in ("index.html", "vite.config.js", "facts.json", "src/story.js", "src/style.css", "src/theme.css", "src/theme.js",
                "src/props.js", "src/print.js", "src/cuts.js", "src/kit.js", "src/film.js", "src/main.js", "src/caption.js"):
        assert (STARTER / rel).is_file(), rel
    assert list((STARTER / "src" / "worlds").glob("*.js")) and list((STARTER / "src" / "scenes").glob("*.js"))
    film = SKILL / "examples" / "film"
    assert (film / "index.html").is_file() and list(film.rglob("scenes/*.js")), "the film example is whole"
    kit = (STARTER / "src" / "kit.js").read_text(encoding="utf-8")
    for name in ("mat", "paint", "rig", "spot", "gobo", "haze", "card", "figure", "prop", "fit", "ease", "rnd"):
        assert re.search(rf"export (?:async )?function {name}\(", kit) or re.search(rf"export const {name}\b", kit), f"the kit has no {name}"


def test_the_stage_and_the_page_have_one_palette() -> None:
    """A theme's colours are written twice: in ``theme.css``, which styles the slides and which the stage reads at run
    time, and in ``theme.js``, which the stage falls back on where a page's CSS has no theme. Two palettes for one
    theme would show a deck in different colours depending on which of the two was read."""
    css = (STARTER / "src" / "theme.css").read_text(encoding="utf-8")
    js = (STARTER / "src" / "theme.js").read_text(encoding="utf-8")
    tokens = re.findall(r"'([a-z0-9-]+)'", re.search(r"export const TOKENS = \[(.*?)\];", js).group(1))
    assert tokens == ["paper", "paper-2", "ink", "muted", "accent", "accent-2", "lit", "shade"]
    blocks = {m.group(1): m.group(2) for m in re.finditer(r'(?ms)^\[data-story-theme="([a-z]+)"\] \{\n(.*?)^\}', css)}
    blocks["paper"] = next(b for b in re.findall(r"(?ms)^:root \{\n(.*?)^\}", css) if "--paper:" in b)
    for name in _keys(STARTER / "src" / "theme.js", "THEMES"):
        line = re.search(rf"(?m)^  {name}: \{{\n    colours: \{{(.*?)\}},", js).group(1)
        built_in = dict(re.findall(r"'?([a-z0-9-]+)'?: '(#[0-9a-f]{6})'", line))
        on_page = {t: re.search(rf"(?m)^\s*--{re.escape(t)}: (#[0-9a-f]{{6}});", blocks[name]).group(1) for t in tokens}
        assert built_in == on_page, f"theme {name}: theme.js has {built_in}, theme.css has {on_page}"
