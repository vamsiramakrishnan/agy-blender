"""Which files of a free-form part (a scene's ``mesh_ref``) can travel inside a package, and the one check of it.

The package writer (``package._ship_parts``) asks it before it copies a file, and ``agy3d edit`` / ``open_existing_asset``
(``dags.edit_asset._part_file``) ask it again, on the bytes they are about to open, because a package is input from
anywhere: a name and a hash that are right say nothing about what the file inside refers to. Both ask this one function, so
they cannot disagree.

What it reads is the bytes of the file, never a layer's or a loader's dependencies: finding out whether a file names
another file must not open that file. The formats that travel are the ones whose names, points and faces
(``mesh_io.triangle_parts``) are in the one file and whose loaders open nothing but that file:

* ``.ply``, ``.stl``: a PLY header's ``comment TextureFile`` must be a bare file name;
* ``.glb``: no buffer or image URI that is not embedded (``data:``);
* ``.obj``: an ``mtllib`` line must name bare files (the writer ships no ``.mtl``; the compiler does not read one).

Not here, by the same rule: a ``.gltf`` (its buffers and images are other files; export one ``.glb``), and a USD file (its
layers, references and payloads are other files, and a package's USD part cannot be delivered anyway: ``glb_batch``)."""

from __future__ import annotations

import json
import re

PART_SUFFIXES = (".ply", ".stl", ".glb", ".obj")
TRAVELS = "the formats that travel are .ply, .stl, .glb and .obj"


def _bare(name: str) -> bool:
    """A file name that stays in the folder of the file that names it."""
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..") and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", name)


def problem(suffix: str, data: bytes) -> str | None:
    """Why a part file with this suffix and these bytes cannot travel as the one file it is (a sentence), or None."""
    ext = suffix.lower()
    if ext == ".gltf":
        return "a .gltf names its buffers and images as other files; export it as one .glb"
    if ext in (".usd", ".usda", ".usdc", ".usdz"):
        return f"a USD file names other layers, references and payloads, and a USD part cannot be delivered ({TRAVELS})"
    if ext not in PART_SUFFIXES:
        return f"{ext or 'a file without a suffix'} is not a part format ({TRAVELS})"
    if ext == ".glb":
        if data[:4] != b"glTF" or len(data) < 20:
            return "not a readable .glb"
        try:
            doc = json.loads(data[20:20 + int.from_bytes(data[12:16], "little")])
        except ValueError:
            return "not a readable .glb (its JSON chunk)"
        external = sorted({str(x["uri"]).split("/")[-1][:40] for k in ("buffers", "images") for x in doc.get(k, ())
                           if isinstance(x, dict) and x.get("uri") and not str(x["uri"]).startswith("data:")})
        return f"the .glb names other files ({', '.join(external[:3])}); embed them (export one .glb)" if external else None
    if ext == ".obj":
        for line in data.split(b"\n"):
            line = line.strip()  # a loader strips a line before it reads its keyword: so does this check
            if line.startswith(b"mtllib"):
                names = line[6:].decode("latin-1").split()
                if not names or not all(_bare(n) for n in names):
                    return f"the .obj's mtllib names a path ({' '.join(names)[:60] or 'none'}), not a bare file name"
        return None
    if ext == ".ply":
        for line in data[:4096].split(b"end_header", 1)[0].split(b"\n"):
            if line.lstrip().lower().startswith(b"comment texturefile") and not _bare(line.split(None, 2)[-1].decode("latin-1").strip()):
                return "the .ply's TextureFile comment names a path, not a bare file name"
    return None
