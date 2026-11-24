"""shrink_glb.py IN.glb OUT.glb [SIDE]: resample the PNG textures embedded in a GLB to SIDE x SIDE (default 1024).

The plugin's own texture control (the `realistic` art direction) stops at 2048 px; the film's three.js page has no KTX2 or
meshopt decoders, so the delivered GLB is shrunk here. Only the image bytes change: the atlas UVs are normalised, so no
vertex data is touched. Needs Pillow only."""
import io, json, struct, sys
from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
side = int(sys.argv[3]) if len(sys.argv) > 3 else 1024
data = open(src, "rb").read()
magic, ver, _ = struct.unpack_from("<4sII", data, 0)
off, js, binc = 12, None, None
while off < len(data):
    ln, typ = struct.unpack_from("<II", data, off)
    chunk = data[off + 8: off + 8 + ln]
    if typ == 0x4E4F534A: js = json.loads(chunk)
    elif typ == 0x004E4942: binc = chunk
    off += 8 + ln
views = js["bufferViews"]
img_views = {im["bufferView"] for im in js.get("images", [])}
new_bin, new_views = bytearray(), []
for i, v in enumerate(views):
    raw = binc[v.get("byteOffset", 0): v.get("byteOffset", 0) + v["byteLength"]]
    if i in img_views:
        im = Image.open(io.BytesIO(raw))
        if max(im.size) > side:
            im = im.resize((side, side * im.size[1] // im.size[0]), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, "PNG", optimize=True); raw = buf.getvalue()
    while len(new_bin) % 4: new_bin.append(0)
    nv = {k: val for k, val in v.items() if k != "byteOffset"}
    nv["byteOffset"] = len(new_bin); nv["byteLength"] = len(raw)
    new_views.append(nv); new_bin += raw
while len(new_bin) % 4: new_bin.append(0)
js["bufferViews"] = new_views
js["buffers"] = [{"byteLength": len(new_bin)}]
jb = json.dumps(js, separators=(",", ":")).encode()
while len(jb) % 4: jb += b" "
out = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(jb) + 8 + len(new_bin))
out += struct.pack("<II", len(jb), 0x4E4F534A) + jb + struct.pack("<II", len(new_bin), 0x004E4942) + bytes(new_bin)
open(dst, "wb").write(out)
print(src, len(data), "->", dst, len(out))
