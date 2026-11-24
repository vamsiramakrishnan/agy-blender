"""to_artifact.py SITE_DIR OUT_HTML: turn a static site's index.html into the page fragment a sandboxed page host
publishes, changing nothing that is not required by that host, and say exactly what was changed.

The host wraps the page in its own <!doctype>/<html>/<head>/<body>, so those four tags cannot be in the file. This:
  - drops the doctype and the html/head/body tags, keeping everything inside them in the same order;
  - drops the charset and viewport metas (the host supplies its own);
  - re-applies attributes that were on <html> and <body> (classes, data attributes, lang) with one inline script;
  - leaves every other byte alone.
It also prints the list of the site's other files with their media types, and every URL on another host the page or
its scripts and styles refer to, so that what the host's content policy would block is known before publishing."""
import json
import re
import sys
from pathlib import Path

site, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
src = (site / "index.html").read_text(encoding="utf-8")
changes = []


def attrs(tag: str) -> dict:
    return {m.group(1): (m.group(3) if m.group(3) is not None else m.group(4) or "") for m in
            re.finditer(r'([\w:-]+)(\s*=\s*(?:"([^"]*)"|\'([^\']*)\'))?', re.sub(r"^<\w+", "", tag).rstrip(">"))}


html_tag = re.search(r"<html\b[^>]*>", src, re.I)
body_tag = re.search(r"<body\b[^>]*>", src, re.I)
head = re.search(r"<head\b[^>]*>(.*?)</head>", src, re.I | re.S)
body = re.search(r"<body\b[^>]*>(.*)</body>", src, re.I | re.S)
if not (head and body):
    sys.exit("index.html has no <head> or no <body>: convert by hand")
head_inner, body_inner = head.group(1), body.group(1)
for pattern, what in ((r"<meta\s+charset=[^>]*>\s*", "charset meta"), (r'<meta\s+name=["\']viewport["\'][^>]*>\s*', "viewport meta")):
    head_inner, n = re.subn(pattern, "", head_inner, flags=re.I)
    if n:
        changes.append(f"removed the {what} (the host sets its own)")
changes.append("removed <!doctype>, <html>, <head> and <body> tags (the host adds them); their contents are kept in order")
apply = []
for name, tag in (("documentElement", html_tag), ("body", body_tag)):
    for key, value in (attrs(tag.group(0)) if tag else {}).items():
        if key == "class" and value.strip():
            apply.append(f"document.{name}.classList.add({', '.join(json.dumps(c) for c in value.split())});")
        elif key not in ("class",) and (name == "body" or key != "lang"):
            apply.append(f"document.{name}.setAttribute({json.dumps(key)}, {json.dumps(value)});")
script = ""
if apply:
    script = "<script>" + "".join(apply) + "</script>\n"
    changes.append("re-applied the attributes of <html> and <body> with one inline script: " + " ".join(apply))
title = re.search(r"<title>.*?</title>", head_inner, re.I | re.S)
if title:  # the host reads the title from the top of the file
    head_inner = title.group(0) + "\n" + head_inner.replace(title.group(0), "", 1)
fragment = head_inner.strip("\n") + "\n" + script + body_inner.strip("\n") + "\n"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(fragment, encoding="utf-8")

TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
         ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".bin": "application/octet-stream", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".webp": "image/webp", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".html": "text/html",
         ".txt": "text/plain", ".ico": "image/x-icon", ".ktx2": "image/ktx2", ".wasm": "application/wasm", ".hdr": "application/octet-stream",
         ".mp4": "video/mp4", ".webm": "video/webm", ".map": "application/json", ".md": "text/markdown"}
files, unknown, total = {}, [], 0
for path in sorted(p for p in site.rglob("*") if p.is_file()):
    rel = path.relative_to(site).as_posix()
    if rel == "index.html":
        continue
    total += path.stat().st_size
    kind = TYPES.get(path.suffix.lower())
    if kind is None:
        unknown.append(rel)
        continue
    files[rel] = {"from": str(path), "contentType": kind}
(out.parent / (out.stem + "_files.json")).write_text(json.dumps(files, indent=1))
hosts = {}
texts = [("index.html", src)] + [(p.relative_to(site).as_posix(), p.read_text(encoding="utf-8", errors="replace"))
                                 for p in site.rglob("*") if p.is_file() and p.suffix in (".js", ".mjs", ".css") and p.stat().st_size < 3_000_000]
for name, text in texts:
    for url in re.findall(r"""https?://[^\s'"<>)\\]+""", text):
        host = url.split("/")[2]
        hosts.setdefault(host, set()).add(name)
print(json.dumps({"fragment": str(out), "fragment_bytes": len(fragment.encode()), "files": len(files), "files_bytes": total,
                  "files_without_a_known_type": unknown, "changes": changes,
                  "other_hosts_named": {h: sorted(v)[:6] for h, v in sorted(hosts.items())}}, indent=1))
