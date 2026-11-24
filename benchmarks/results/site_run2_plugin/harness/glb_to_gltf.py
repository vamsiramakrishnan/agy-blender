"""glb_to_gltf.py IN.glb OUT_DIR: rewrite a binary glTF as the standard text form of the same asset, losslessly.

OUT_DIR/<name>.gltf   the JSON, with the geometry buffer embedded as a base64 data URI
OUT_DIR/<name>_<image>.png|jpg|webp   every embedded image, byte for byte, referenced by relative URI

Nothing is re-encoded: image bytes and geometry bytes are copied; only the container changes. For hosts that serve
JSON and images but not .glb files."""
import base64
import json
import struct
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
data = src.read_bytes()
magic, version, length = struct.unpack_from("<III", data, 0)
assert magic == 0x46546C67 and version == 2 and length == len(data), "not a glTF 2.0 binary"
offset, doc, binary = 12, None, b""
while offset < length:
    chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
    body = data[offset + 8: offset + 8 + chunk_len]
    if chunk_type == 0x4E4F534A:
        doc = json.loads(body.decode("utf-8"))
    elif chunk_type == 0x004E4942:
        binary = body
    offset += 8 + chunk_len
assert doc is not None and len(doc.get("buffers", [])) == 1 and "uri" not in doc["buffers"][0], "expected one embedded buffer"
views = doc.get("bufferViews", [])
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
image_views = {}
out.mkdir(parents=True, exist_ok=True)
stem = src.stem
for i, image in enumerate(doc.get("images", [])):
    if "bufferView" not in image:
        continue
    view = views[image["bufferView"]]
    start = view.get("byteOffset", 0)
    name = f"{stem}_{image.get('name') or i}{EXT[image['mimeType']]}".replace(f"{stem}_{stem}_", f"{stem}_")
    (out / name).write_bytes(binary[start: start + view["byteLength"]])
    image_views[image.pop("bufferView")] = name
    image["uri"] = name
kept, remap, packed = [], {}, bytearray()
for index, view in enumerate(views):
    if index in image_views:
        continue
    start = view.get("byteOffset", 0)
    while len(packed) % 4:
        packed.append(0)
    remap[index] = len(kept)
    kept.append({**view, "byteOffset": len(packed)})
    packed += binary[start: start + view["byteLength"]]
doc["bufferViews"] = kept
for accessor in doc.get("accessors", []):
    if "bufferView" in accessor:
        accessor["bufferView"] = remap[accessor["bufferView"]]
    for part in ("indices", "values"):
        sparse = accessor.get("sparse", {}).get(part)
        if sparse:
            sparse["bufferView"] = remap[sparse["bufferView"]]
doc["buffers"] = [{"byteLength": len(packed), "uri": "data:application/octet-stream;base64," + base64.b64encode(bytes(packed)).decode("ascii")}]
target = out / f"{stem}.gltf"
target.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
print(f"{src.name} {len(data):,} bytes -> {target.name} {target.stat().st_size:,} bytes + "
      + ", ".join(f"{n} {(out / n).stat().st_size:,}" for n in image_views.values())
      + f" | used extensions: {doc.get('extensionsUsed', [])} required: {doc.get('extensionsRequired', [])}")
