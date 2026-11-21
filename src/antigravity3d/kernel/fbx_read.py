"""A small, dependency-free reader of binary FBX files: what ``validate_package`` needs to know about the FBX of a package.

Only the node tree and the string / raw properties are read (arrays are skipped without being decompressed), which is
enough to list the file's texture references and whether each carries embedded bytes. It is not an importer and does not
interpret geometry. The same facts are parsed with Blender's own FBX parser when the file is written (``fbx_export``);
this reader exists so that the package validator, which runs without Blender, can re-check a shipped file.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"
_FIXED = {b"Y": 2, b"C": 1, b"I": 4, b"F": 4, b"D": 8, b"L": 8}
_ARRAY = {b"f", b"d", b"l", b"i", b"b"}
MAX_NODES = 2_000_000  # a package FBX has hundreds; refuse a file that would make the walk unbounded
MAX_DEPTH = 64  # Blender writes a handful of levels; a deeper file is refused, not walked until the interpreter gives up


class FbxFormatError(ValueError):
    """The file is not a binary FBX this reader understands."""


def _name(raw: Any) -> str:
    return raw.split(b"\x00\x01")[0].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def read_nodes(data: bytes) -> tuple[int, list[dict[str, Any]]]:
    """(file version, top-level nodes). A node is ``{"id": bytes, "props": [...], "children": [...]}``; a property is a
    ``bytes``/``int``/``float`` value for strings, raw data and scalars, and ``None`` for an array."""
    if not data.startswith(MAGIC) or len(data) < 27:
        raise FbxFormatError("not a binary FBX (missing the 'Kaydara FBX Binary' header)")
    version = struct.unpack_from("<I", data, 23)[0]
    wide = version >= 7500
    head = 25 if wide else 13
    seen = 0

    def read_children(pos: int, end: int, depth: int = 0) -> tuple[list[dict[str, Any]], int]:
        nonlocal seen
        if depth > MAX_DEPTH:
            raise FbxFormatError("nodes nested too deeply")
        nodes: list[dict[str, Any]] = []
        while pos + head <= end:
            if wide:
                node_end, n_props, props_len, name_len = struct.unpack_from("<QQQB", data, pos)
            else:
                node_end, n_props, props_len, name_len = struct.unpack_from("<IIIB", data, pos)
            if node_end == 0:  # the null record that closes a list of children
                return nodes, pos + head
            seen += 1
            if seen > MAX_NODES or node_end > len(data) or node_end <= pos:
                raise FbxFormatError("corrupt node record")
            cursor = pos + head
            node_id = data[cursor:cursor + name_len]
            cursor += name_len
            props: list[Any] = []
            props_end = cursor + props_len
            if props_end > node_end:
                raise FbxFormatError("property list overruns its node")
            for _ in range(n_props):
                code = data[cursor:cursor + 1]
                cursor += 1
                if code in _FIXED:
                    size = _FIXED[code]
                    fmt = {b"Y": "<h", b"C": "<B", b"I": "<i", b"F": "<f", b"D": "<d", b"L": "<q"}[code]
                    props.append(struct.unpack_from(fmt, data, cursor)[0])
                    cursor += size
                elif code in _ARRAY:
                    _, _, comp_len = struct.unpack_from("<III", data, cursor)
                    cursor += 12 + comp_len
                    props.append(None)
                elif code in (b"S", b"R"):
                    (n,) = struct.unpack_from("<I", data, cursor)
                    props.append(bytes(data[cursor + 4:cursor + 4 + n]))
                    cursor += 4 + n
                else:
                    raise FbxFormatError(f"unknown property type {code!r}")
                if cursor > props_end:
                    raise FbxFormatError("property overruns its list")
            children: list[dict[str, Any]] = []
            if props_end < node_end:
                children, _ = read_children(props_end, node_end, depth + 1)
            nodes.append({"id": node_id, "props": props, "children": children})
            pos = node_end
        return nodes, pos

    nodes, _ = read_children(27, len(data))
    return version, nodes


def texture_references(path: str | Path) -> list[dict[str, Any]]:
    """Every texture the file refers to: ``{"name", "relative", "filename", "embedded_bytes"}`` (from its ``Video`` nodes,
    which hold the file name and, when the image is embedded, its bytes)."""
    _, nodes = read_nodes(Path(path).read_bytes())
    refs: list[dict[str, Any]] = []
    for top in nodes:
        if top["id"] != b"Objects":
            continue
        for obj in top["children"]:
            if obj["id"] != b"Video":
                continue
            rec: dict[str, Any] = {"name": _name(obj["props"][1]) if len(obj["props"]) > 1 else "", "relative": None, "filename": None, "embedded_bytes": 0}
            for child in obj["children"]:
                value = child["props"][0] if child["props"] else None
                if child["id"] == b"RelativeFilename" and isinstance(value, bytes):
                    rec["relative"] = value.decode("utf-8", "replace")
                elif child["id"] == b"Filename" and isinstance(value, bytes):
                    rec["filename"] = value.decode("utf-8", "replace")
                elif child["id"] == b"Content" and isinstance(value, bytes):
                    rec["embedded_bytes"] = len(value)
            refs.append(rec)
    return refs
