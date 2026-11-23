"""publish_list.py: print the supporting files of site/ as JSON for a host that publishes film.html as the page and
every other file beside it at the same relative path (index.html is the standalone build of the same page and is
left out). Each entry carries the media type a browser needs for it."""
import json
from pathlib import Path

site = Path(__file__).resolve().parent.parent / "site"
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
         ".glb": "model/gltf-binary", ".bin": "application/octet-stream", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
SKIP = {"index.html", "film.html"}
files = {}
for path in sorted(p for p in site.rglob("*") if p.is_file()):
    rel = path.relative_to(site).as_posix()
    if rel in SKIP or path.suffix == ".LICENSE":
        continue
    files[rel] = {"from": rel, "contentType": TYPES[path.suffix]}
print(json.dumps(files, indent=1))
