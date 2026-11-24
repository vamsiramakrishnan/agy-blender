"""build_page.py: writes site/index.html, the standalone document, from site/film.html.

site/film.html is the page as it is authored: a fragment without <html>, <head> or <body>, which is the form the
hosted page is published in. A static host needs a whole document, so this wraps the fragment in the same skeleton
the hosted viewer uses (charset, viewport, a small reset) and lifts <title>, <meta> and <link> into <head>.
Run it after every change to film.html; tools/check_site.py fails when index.html is stale.
"""
import re
import sys
from pathlib import Path

site = Path(__file__).resolve().parent.parent / "site"
fragment = (site / "film.html").read_text(encoding="utf-8")
HEAD_TAG = re.compile(r"^(<title>.*?</title>|<meta [^>]*>|<link [^>]*>)\s*$", re.M)
head = [m.group(1) for m in HEAD_TAG.finditer(fragment)]
body = HEAD_TAG.sub("", fragment).lstrip("\n")
doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml">
<style>:root{{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}}body{{margin:0;font:14px system-ui,sans-serif}}img{{max-width:100%}}[hidden]{{display:none!important}}</style>
{chr(10).join(head)}
</head>
<body>
{body}</body>
</html>
"""
target = site / "index.html"
if "--check" in sys.argv:
    sys.exit(0 if target.exists() and target.read_text(encoding="utf-8") == doc else "site/index.html is stale: run tools/build_page.py")
target.write_text(doc, encoding="utf-8")
print("wrote", target, len(doc), "bytes")
