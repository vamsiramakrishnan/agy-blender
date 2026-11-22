"""Format checks for the plugin's skills, after the reference repository's docs/SKILL-FORMAT.md (awesome-gamedev-agent-skills,
Apache-2.0, commit c6ca6c4): name, description, body shape, Gotchas, Version notes, links.

Lives beside the tests, not in the product package: it only scores the plugin's own documents. ``problems(skill_dir,
names, roots)`` returns what is wrong with one skill (an empty list: valid); ``roots`` are where a file named in the
text may live besides the skill's own folder.
"""

from __future__ import annotations

import re
from pathlib import Path

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_NAME, MAX_DESCRIPTION, MAX_LINES = 64, 1024, 500
MIN_DESCRIPTION = 300  # the format aims for about 300 to 700; a shorter one has left out the words users type
PACKAGE_FILES = frozenset({"manifest.json", "report.json", "inspector.html"})  # files of a shipped package, not of the plugin
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_FILE = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|json|txt|py|yaml))`")
_FENCE = re.compile(r"```.*?```", re.S)


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """The flat ``name`` / ``description`` fields (folded ``>-`` scalars joined by spaces) and the body."""
    m = _FRONT.match(text)
    if m is None:
        return {}, text
    fields: dict[str, str] = {}
    lines = m.group(1).split("\n")
    i = 0
    while i < len(lines):
        km = re.match(r"([A-Za-z0-9_-]+):\s*(.*)$", lines[i])
        i += 1
        if not km:
            continue
        key, value = km.group(1), km.group(2).strip()
        if value in {">", ">-", "|", "|-"}:
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in " \t"):
                block.append(lines[i].strip())
                i += 1
            value = " ".join(b for b in block if b)
        fields[key] = value.strip("\"'")
    return fields, text[m.end():]


def sections(body: str, heading: str) -> list[str]:
    """The text of every ``## <heading>`` section (to the next ``## `` heading)."""
    body = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), body)  # a fence may hold '## ' lines of its own
    parts = re.split(r"(?m)^## ", body)[1:]
    return [p.split("\n", 1)[1] if "\n" in p else "" for p in parts if p.split("\n", 1)[0].strip() == heading]


def headings(body: str) -> list[str]:
    return [h.strip() for h in re.findall(r"(?m)^## (.+)$", _FENCE.sub("", body))]


def not_for_targets(description: str) -> list[str]:
    """The skill names a closing 'Not for ... use X' clause points at; [] when the description has no such clause
    (the last sentence must start with 'Not for')."""
    flat = " ".join(description.split())
    last = re.split(r"(?<=[.])\s+", flat)[-1]
    return re.findall(r"\buse ([a-z0-9-]+)", last) if last.startswith("Not for") else []


def entries(gotchas: str) -> list[str]:
    return [e.strip() for e in re.split(r"(?m)^- ", gotchas)[1:]]


def problems(skill_dir: Path, names: set[str], roots: tuple[Path, ...] = ()) -> list[str]:
    out: list[str] = []
    path = skill_dir / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    fields, body = frontmatter(text)
    name, description = fields.get("name", ""), fields.get("description", "")
    if set(fields) != {"name", "description"}:
        out.append(f"frontmatter must hold exactly name and description, has {sorted(fields)}")
    if not (1 <= len(name) <= MAX_NAME and NAME.fullmatch(name)):
        out.append(f"name {name!r}: 1 to {MAX_NAME} characters, lowercase letters, digits and single hyphens")
    if name != skill_dir.name:
        out.append(f"name {name!r} must equal its directory {skill_dir.name!r}")
    if not description:
        out.append("description is empty")
    if len(description) > MAX_DESCRIPTION:
        out.append(f"description is {len(description)} characters, the limit is {MAX_DESCRIPTION}")
    if 0 < len(description) < MIN_DESCRIPTION:
        out.append(f"description is {len(description)} characters; under {MIN_DESCRIPTION} it has not room for the user's words")
    if "<" in description or ">" in description:
        out.append("description must not contain angle brackets")
    if not re.search(r"\bUse when\b", description):
        out.append("description must say when to use the skill ('Use when ...')")
    targets = not_for_targets(description)
    if not description.rstrip().endswith(".") or not targets:
        out.append("description must end with a 'Not for ... (use <skill>)' clause naming the neighbour that owns that work")
    for t in targets:
        if t not in names:
            out.append(f"'Not for' clause names {t!r}, which is not a skill")
        if t == name:
            out.append("'Not for' clause names the skill itself")
    if text.count("\n") + 1 > MAX_LINES:
        out.append(f"SKILL.md has {text.count(chr(10)) + 1} lines, the limit is {MAX_LINES}")
    hs = headings(body)
    for required in ("Gotchas", "Version notes"):
        if hs.count(required) != 1:
            out.append(f"needs exactly one '## {required}' section, has {hs.count(required)}")
    if not any(h.startswith("Check") for h in hs):
        out.append("needs a '## Check...' section: the check the agent runs and what a pass looks like")
    for g in sections(body, "Gotchas"):
        found = entries(g)
        if not found:
            out.append("Gotchas has no entries")
        for e in found:
            flat = " ".join(e.split())
            if not all(k in flat for k in ("**Symptom:**", "**Cause:**", "**Remedy:**")) or not (
                    flat.index("**Symptom:**") < flat.index("**Cause:**") < flat.index("**Remedy:**")):
                out.append(f"Gotchas entry is not symptom, cause, remedy: {flat[:70]!r}")
    for v in sections(body, "Version notes"):
        if not re.search(r"Last verified \d{4}-\d{2}-\d{2}", v):
            out.append("Version notes must start from 'Last verified YYYY-MM-DD'")
    prose = _FENCE.sub("", body)
    roots = (skill_dir, *roots)  # the skill, then the places the text may point at (plugin, source package, repository)
    for link in _LINK.findall(prose):
        if not re.match(r"[a-z]+:", link) and not link.startswith("#") and not (skill_dir / link.split("#")[0]).exists():
            out.append(f"link {link!r} does not resolve")
    for ref in _FILE.findall(prose):
        if ref in PACKAGE_FILES:
            continue
        if not any((r / ref).exists() for r in roots):
            out.append(f"file `{ref}` named in the text does not exist beside the skill")
    return out
