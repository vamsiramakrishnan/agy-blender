"""The two GLB operations an install needs: reference images instead of embedding them, and read collision hulls."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Mapping

import numpy as np


def _split(data: bytes) -> tuple[dict, bytes]:
    magic, version, size = struct.unpack_from("<4sII", data, 0)
    jlen, jtype = struct.unpack_from("<I4s", data, 12)
    if (magic, version, jtype) != (b"glTF", 2, b"JSON") or size != len(data):
        raise ValueError("not a glTF 2.0 binary")
    doc = json.loads(data[20:20 + jlen])
    off, blob = 20 + jlen, b""
    if off + 8 <= size:
        blen, btype = struct.unpack_from("<I4s", data, off)
        if btype == b"BIN\0":
            blob = data[off + 8:off + 8 + blen]
    return doc, blob


def _join(doc: dict, blob: bytes) -> bytes:
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    blob += b"\0" * (-len(blob) % 4)
    chunks = struct.pack("<I4s", len(js), b"JSON") + js
    if blob:
        chunks += struct.pack("<I4s", len(blob), b"BIN\0") + blob
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def reference_images(src: Path, uri_of_sha256: Mapping[str, str]) -> tuple[bytes, list[dict]]:
    """``src`` with every embedded image whose bytes hash to a key of ``uri_of_sha256`` replaced by a reference to
    that URI, and the binary chunk compacted (the image bytes leave the file). Returns the new GLB and, per image,
    what was done. An embedded image with no matching file stays embedded and is reported (``"embedded"``).

    Why: Godot's importer extracts a GLB's embedded images to new files beside it (gltf/embedded_image_handling=1,
    the default) and binds LOD0's materials to those, while far levels that reference ``textures/`` bind to the
    originals; measured with stock Godot 4.7.2 on the offline crate: 12 texture assets for 6 maps, both copies in
    memory at once. A LOD0 that references ``textures/`` (as the package's far levels already do) gives one."""
    return reference_images_bytes(src.read_bytes(), uri_of_sha256, src.name)


def reference_images_bytes(data: bytes, uri_of_sha256: Mapping[str, str], name: str = "glb") -> tuple[bytes, list[dict]]:
    """:func:`reference_images` on GLB bytes (``name`` only labels an error)."""
    doc, blob = _split(data)
    views = doc.get("bufferViews") or []
    drop: set[int] = set()
    report = []
    for i, image in enumerate(doc.get("images") or []):
        if "bufferView" not in image:
            report.append({"image": i, "storage": "uri", "uri": image.get("uri")})
            continue
        v = views[image["bufferView"]]
        start = v.get("byteOffset", 0)
        digest = hashlib.sha256(blob[start:start + v["byteLength"]]).hexdigest()
        uri = uri_of_sha256.get(digest)
        if uri is None:
            report.append({"image": i, "storage": "embedded", "sha256": digest})
            continue
        drop.add(image.pop("bufferView"))
        image["uri"] = uri
        report.append({"image": i, "storage": "uri", "uri": uri, "sha256": digest})
    if drop:
        users = [obj for key in ("accessors", "images") for obj in doc.get(key) or [] if "bufferView" in obj]
        if any(obj["bufferView"] in drop for obj in users):
            raise ValueError(f"{name}: an image buffer view is shared with other data")
        new_views, remap, out = [], {}, bytearray()
        for i, v in enumerate(views):
            if i in drop:
                continue
            nv = json.loads(json.dumps(v))
            # data of the binary chunk this view owns: the view itself, and a meshopt-compressed copy of it that the
            # EXT_meshopt_compression extension points at (its own buffer index; the view then names a data-less
            # fallback buffer). Both are moved: offsets inside the binary chunk change when image bytes leave it.
            ext = (nv.get("extensions") or {}).get("EXT_meshopt_compression")
            for holder in [nv, *([ext] if ext else [])]:
                if holder.get("buffer", 0) == 0 and not (holder is nv and ext and nv.get("buffer", 0) != 0):
                    start = holder.get("byteOffset", 0)
                    out += b"\0" * (-len(out) % 4)  # accessors need 4-byte aligned views
                    holder["byteOffset"] = len(out)
                    out += blob[start:start + holder["byteLength"]]
            remap[i] = len(new_views)
            new_views.append(nv)
        for obj in users:
            obj["bufferView"] = remap[obj["bufferView"]]
        doc["bufferViews"] = new_views
        blob = bytes(out)
        if doc.get("buffers"):
            doc["buffers"][0]["byteLength"] = len(blob)
    return _join(doc, blob), report


def _drop_texture_refs(node: object) -> None:
    """Remove every ``{"index": n, ...}`` texture reference (``*Texture`` keys, in a material or in its extensions)."""
    if isinstance(node, dict):
        for k in [k for k, v in node.items() if k.endswith("Texture") and isinstance(v, dict) and "index" in v]:
            del node[k]
        for v in node.values():
            _drop_texture_refs(v)
    elif isinstance(node, list):
        for v in node:
            _drop_texture_refs(v)


def strip_images(data: bytes) -> bytes:
    """``data`` without its images, textures, samplers and the materials' references to them (the materials keep their
    names and factors). For a far level whose materials the web loader replaces by the highest level's: the file then
    names no picture, so nothing fetches or decodes one twice."""
    out, _ = reference_images_bytes(data, {hashlib.sha256(b).hexdigest(): "x" for b, _m in embedded_images(data)})
    doc, blob = _split(out)
    for key in ("images", "textures", "samplers"):
        doc.pop(key, None)
    _drop_texture_refs(doc.get("materials") or [])
    gone = ("KHR_texture_basisu", WEBP_EXTENSION)  # texture extensions: no texture is left to use them
    doc["extensionsUsed"] = [e for e in doc.get("extensionsUsed") or [] if e not in gone]
    doc["extensionsRequired"] = [e for e in doc.get("extensionsRequired") or [] if e not in gone]
    for key in ("extensionsUsed", "extensionsRequired"):
        if not doc[key]:
            del doc[key]
    return _join(doc, blob)


WEBP_EXTENSION = "EXT_texture_webp"


def texture_source(texture: Mapping) -> int | None:
    """The image a glTF texture shows: its ``EXT_texture_webp`` source when it has one (a reader that supports the
    extension uses it, and a texture written by :func:`swap_images` has no other), else its ``source``."""
    ext = (texture.get("extensions") or {}).get(WEBP_EXTENSION) or {}
    return ext.get("source", texture.get("source"))


def swap_images(data: bytes, embedded: Mapping[str, tuple[bytes, str]] | None = None,
                uris: Mapping[str, tuple[str, str]] | None = None) -> bytes:
    """``data`` with some images replaced, everything else as it was.

    ``embedded``: {sha256 of an embedded image's bytes: (new bytes, MIME type)}; ``uris``: {URI an image references
    exactly as written: (new URI, MIME type)}. A texture showing a replaced image is written for its new type: a WebP
    image through ``EXT_texture_webp`` with no ``source`` fallback (a fallback would be the PNG again, in the same
    file), any other type through ``source``. The extension is listed as used and required exactly while a texture
    uses it. The binary chunk is laid out again, view by view in order, so image bytes of another length move the
    views after them; a view outside buffer 0 or a meshopt-compressed one is refused (the package writer makes none)."""
    doc, blob = _split(data)
    views = doc.get("bufferViews") or []
    if any(v.get("buffer", 0) != 0 or v.get("extensions") for v in views):
        raise ValueError("swap_images handles plain views of the binary chunk only")
    new_bytes: dict[int, bytes] = {}
    mime_of: dict[int, str] = {}
    for i, image in enumerate(doc.get("images") or []):
        if "bufferView" in image and embedded:
            v = views[image["bufferView"]]
            start = v.get("byteOffset", 0)
            got = embedded.get(hashlib.sha256(blob[start:start + v["byteLength"]]).hexdigest())
            if got is not None:
                if sum(1 for im in doc["images"] if im.get("bufferView") == image["bufferView"]) > 1:
                    raise ValueError(f"image {i} shares its buffer view")
                new_bytes[image["bufferView"]] = got[0]
                image["mimeType"] = mime_of[i] = got[1]
        elif isinstance(image.get("uri"), str) and uris and image["uri"] in uris:
            image["uri"], mime_of[i] = uris[image["uri"]]
            image["mimeType"] = mime_of[i]
    if new_bytes:
        out = bytearray()
        for i, v in enumerate(views):
            start = v.get("byteOffset", 0)
            chunk = new_bytes.get(i, blob[start:start + v["byteLength"]])
            out += b"\0" * (-len(out) % 4)  # accessors need 4-byte aligned views
            v["byteOffset"], v["byteLength"] = len(out), len(chunk)
            out += chunk
        blob = bytes(out)
        doc["buffers"][0]["byteLength"] = len(blob)
    for texture in doc.get("textures") or []:
        src = texture_source(texture)
        if src not in mime_of:
            continue
        ext = {k: v for k, v in (texture.get("extensions") or {}).items() if k != WEBP_EXTENSION}
        texture.pop("source", None)
        if mime_of[src] == "image/webp":
            ext[WEBP_EXTENSION] = {"source": src}
        else:
            texture["source"] = src
        if ext:
            texture["extensions"] = ext
        else:
            texture.pop("extensions", None)
    uses = any(WEBP_EXTENSION in (t.get("extensions") or {}) for t in doc.get("textures") or [])
    for key in ("extensionsUsed", "extensionsRequired"):
        names = [e for e in doc.get(key) or [] if e != WEBP_EXTENSION] + ([WEBP_EXTENSION] if uses else [])
        if names:
            doc[key] = names
        else:
            doc.pop(key, None)
    return _join(doc, blob)


def embedded_images(data: bytes) -> list[tuple[bytes, str]]:
    """(bytes, mime type) of each image a GLB embeds in its binary chunk, in image order (a referenced image: skipped)."""
    doc, blob = _split(data)
    views = doc.get("bufferViews") or []
    out = []
    for image in doc.get("images") or []:
        if "bufferView" in image:
            v = views[image["bufferView"]]
            start = v.get("byteOffset", 0)
            out.append((blob[start:start + v["byteLength"]], image.get("mimeType", "")))
    return out


def hull_points(colliders_glb: Path, names: list[str]) -> dict[str, np.ndarray]:
    """World-space vertices (glTF Y-up metres, the space Godot imports in) of each named collider node."""
    import trimesh

    scene = trimesh.load_scene(colliders_glb, process=False, allow_remote=False)
    out: dict[str, np.ndarray] = {}
    for node in scene.graph.nodes_geometry:
        transform, geom = scene.graph[node]
        if node in names:
            out[node] = np.asarray(trimesh.transform_points(scene.geometry[geom].vertices, transform), np.float64)
    missing = [n for n in names if n not in out]
    if missing:
        raise ValueError(f"{colliders_glb.name} has no node(s) {missing}")
    return out


def hull_meshes(colliders_glb: Path, names: list[str]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """World-space (vertices, triangle indices) of each named collider node (glTF Y-up metres): the same vertices as
    :func:`hull_points`, with the faces a physics library that wants a mesh needs."""
    import trimesh

    scene = trimesh.load_scene(colliders_glb, process=False, allow_remote=False)
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for node in scene.graph.nodes_geometry:
        transform, geom = scene.graph[node]
        if node in names:
            g = scene.geometry[geom]
            out[node] = (np.asarray(trimesh.transform_points(g.vertices, transform), np.float64),
                         np.asarray(g.faces, np.int64))
    missing = [n for n in names if n not in out]
    if missing:
        raise ValueError(f"{colliders_glb.name} has no node(s) {missing}")
    return out


def node_names(glb: Path) -> list[str]:
    """The names of a GLB's mesh nodes, in node order."""
    doc, _ = _split(glb.read_bytes())
    return [n.get("name", f"node_{i}") for i, n in enumerate(doc.get("nodes") or []) if "mesh" in n]
