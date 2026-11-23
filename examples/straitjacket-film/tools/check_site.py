"""check_site.py: what must be true of site/ before it is published. Exit status 1 with the reasons if not.

1. Every number marked `data-fact="path.in.film.json"` in film.html is that value of data/film.json, written the way
   the page writes numbers (thousands separators). The page cannot drift from the receipts unnoticed.
2. The digest printed in the page is tests/fixtures/field_needle_main_digest.txt, line for line (from film.json).
3. index.html is the current build of film.html.
4. Every file the page refers to exists; nothing is loaded from another host except the font stylesheet and the
   links a reader follows; no file is larger than its budget.
"""
import html
import json
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
site = root / "site"
page = (site / "film.html").read_text(encoding="utf-8")
film = json.loads((site / "data" / "film.json").read_text(encoding="utf-8"))
bad: list[str] = []


def lookup(path: str):
    value = film
    for key in path.split("."):
        value = value[key]
    return value


def shown(value) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


facts = re.findall(r'data-fact="([^"]+)"[^>]*>([^<]*)<', page)
for path, text in facts:
    try:
        want = shown(lookup(path))
    except (KeyError, TypeError):
        bad.append(f"data-fact {path}: not in film.json")
        continue
    if text.strip() != want:
        bad.append(f"data-fact {path}: the page says {text.strip()!r}, the receipt says {want!r}")

block = re.search(r'id="digest-specimen".*?<code>(.*?)</code>', page, re.S)
printed = html.unescape(re.sub(r"<[^>]+>", "", block.group(1))) if block else ""
if printed.rstrip("\n") != film["digest"]["text"].rstrip("\n"):
    want, got = film["digest"]["text"].rstrip("\n").split("\n"), printed.rstrip("\n").split("\n")
    first = next((i for i, (a, b) in enumerate(zip(want, got)) if a != b), min(len(want), len(got)))
    bad.append(f"the digest in the page differs from the fixture at line {first + 1}: "
               f"{(got[first] if first < len(got) else '<missing>')!r} against {(want[first] if first < len(want) else '<none>')!r}")

if subprocess.run([sys.executable, str(root / "tools" / "build_page.py"), "--check"], capture_output=True).returncode:
    bad.append("site/index.html is stale: run tools/build_page.py")

ALLOWED_HOSTS = ("https://fonts.googleapis.com/",)
for kind, ref in re.findall(r'<(?:link|script|img|source)\b[^>]*?\b(href|src|srcset)="([^"]+)"', page):
    if ref.startswith("http"):
        if not ref.startswith(ALLOWED_HOSTS):
            bad.append(f"loads from another host: {ref}")
    elif not (site / ref.split("#")[0].split("?")[0]).is_file():
        bad.append(f"missing file: {ref}")
for source in list((site / "js").rglob("*.js")) + list((site / "css").rglob("*.css")):
    text = source.read_text(encoding="utf-8")
    for ref in re.findall(r"""(?:from\s+|import\s*\(\s*|url\(\s*)['"]?(https?://[^'")\s]+)""", text):
        bad.append(f"{source.relative_to(site)} loads from another host: {ref}")
    for ref in re.findall(r"""(?:from\s+|import\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]""", text):
        if not (source.parent / ref).resolve().is_file():
            bad.append(f"{source.relative_to(site)} imports a missing file: {ref}")

BUDGET = {".glb": 2_000_000, ".js": 800_000, ".mjs": 200_000, ".css": 120_000, ".svg": 120_000, ".json": 60_000,
          ".bin": 60_000, ".html": 80_000, ".webp": 600_000, ".jpg": 600_000, ".png": 900_000}
total = 0
for file in sorted(p for p in site.rglob("*") if p.is_file()):
    size = file.stat().st_size
    total += size
    limit = BUDGET.get(file.suffix)
    if limit is not None and size > limit:
        bad.append(f"{file.relative_to(site)} is {size:,} bytes (budget {limit:,})")
if total > 14_000_000:
    bad.append(f"the site is {total:,} bytes in all (budget 14,000,000)")

print(f"{len(facts)} marked numbers checked against film.json; digest {'matches' if printed else 'not found'}; "
      f"site {total:,} bytes in {sum(1 for p in site.rglob('*') if p.is_file())} files")
for line in bad:
    print("FAIL", line)
sys.exit(1 if bad else 0)
