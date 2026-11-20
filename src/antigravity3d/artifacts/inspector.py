"""Interactive WebGL inspector for one asset: one front end, two hosts.

The front end (``apps/inspector/src``, built by ``apps/inspector/build.mjs`` into ``web/``) runs

- **standalone**: :func:`build_inspector` writes ONE self-contained ``.html`` -- geometry (base64
  Float32/Uint32 buffers in a JSON ``<script>`` payload), textures (PNG data URIs) and the viewer
  code are embedded. The only external fetch is the pinned three.js module from cdnjs, unless
  ``three_js_source`` is given, in which case the library is inlined and the page needs no network
  at all. Notes are exported as a JSON file (:func:`read_notes`).
- **as an MCP App** inside Antigravity: :func:`app_html` is the document the studio MCP server
  serves as a ``ui://`` resource (three.js and the MCP Apps SDK bundled in); the same data model
  (:func:`inspector_payload`) arrives through app-only tools and notes are stored server-side.

Summary panels (budget gauges, node table, QA) are also rendered server-side as plain HTML in the
standalone file, so they stay readable even if three.js or WebGL fails to load.
"""

from __future__ import annotations

import base64
import colorsys
import gzip
import hashlib
import html
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

import numpy as np

from antigravity3d.kernel.types import MeshData
from antigravity3d.state import EXPORT_MIN_TIER15_SCORE, EngineBudget

THREE_JS_VERSION = "0.160.0"
THREE_JS_URL = f"https://cdnjs.cloudflare.com/ajax/libs/three.js/{THREE_JS_VERSION}/three.module.min.js"
NOTES_SCHEMA = "antigravity3d.review_notes/1"
PAYLOAD_SCHEMA = "antigravity3d.inspector_payload/1"


@lru_cache(maxsize=1)
def bundled_three_js() -> str:
    vendor = resources.files("antigravity3d.artifacts").joinpath("vendor")
    license_text = vendor.joinpath("THREE-LICENSE.txt").read_text("utf-8")
    source = gzip.decompress(vendor.joinpath("three-r160.module.min.js.gz").read_bytes())
    return f"/*{license_text}*/\n" + source.decode("utf-8")


@dataclass(frozen=True)
class InspectorTextures:
    """PNG bytes for one material id. ORM = occlusion (R), roughness (G), metallic (B)."""

    albedo_png: bytes | None = None
    orm_png: bytes | None = None
    normal_png: bytes | None = None


@dataclass(frozen=True)
class InspectorLod:
    """A reduced mesh, world-space Z-up like MeshData. Shown as LOD1..n (LOD0 = ``meshes``)."""

    label: str
    points: np.ndarray
    triangles: np.ndarray


@dataclass(frozen=True)
class InspectorCollider:
    name: str
    points: np.ndarray
    triangles: np.ndarray


@dataclass(frozen=True)
class InspectorQA:
    tier1_ok: bool
    tier1_line: str = ""
    tier15_score: float | None = None
    defects: dict[str, float] = field(default_factory=dict)  # probability 0..1
    audit_deltas: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewNote:
    asset_name: str
    node_id: str | None  # None = note about the whole asset
    text: str


# --------------------------------------------------------------------------- geometry


def zup_to_yup(points: np.ndarray) -> np.ndarray:
    """World Z-up (pipeline/USD) -> three.js Y-up. The ONLY place axes are swapped.

    (x, y, z)_zup -> (x, z, -y)_yup: a proper rotation (det = +1), so triangle
    winding and normals are preserved. Contact-sheet "front" (camera on -Y in
    Z-up) becomes camera on +Z in Y-up.
    """
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    return np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1)


def _b64(arr: np.ndarray, dtype: str) -> str:
    return base64.b64encode(np.ascontiguousarray(arr, dtype=dtype).tobytes()).decode("ascii")


def _geom(points: np.ndarray, triangles: np.ndarray, what: str) -> tuple[str, str, int]:
    pts = np.asarray(points, dtype=np.float64)
    tri = np.asarray(triangles)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"{what}: points must be (N, 3), got {pts.shape}")
    if tri.ndim != 2 or tri.shape[1] != 3:
        raise ValueError(f"{what}: triangles must be (M, 3), got {tri.shape}")
    if tri.size and (tri.min() < 0 or tri.max() >= len(pts)):
        raise ValueError(f"{what}: triangle index out of range")
    return _b64(zup_to_yup(pts), "<f4"), _b64(tri, "<u4"), int(len(tri))


def _prim_colors(ids: list[str]) -> list[str]:
    """Deterministic, distinct colours: hue from a hash of the id, nudged apart on collision."""
    hues: list[float] = []
    out: list[str] = []
    for i, nid in enumerate(ids):
        h = int.from_bytes(hashlib.sha1(nid.encode()).digest()[:4], "big") % 360
        for _ in range(360):
            if all(min(abs(h - o), 360 - abs(h - o)) >= 24 for o in hues):
                break
            h = (h + 37) % 360
        hues.append(h)
        r, g, b = colorsys.hls_to_rgb(h / 360, 0.52 if i % 2 == 0 else 0.40, 0.65)
        out.append(f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}")
    return out


# --------------------------------------------------------------------------- html fragments

_e = html.escape


def _gauge(key: str, label: str, value: int, limit: int) -> str:
    over = value > limit
    pct = min(100.0, value / limit * 100) if limit > 0 else 100.0
    state = f"OVER BUDGET by {value - limit:,}" if over else f"Within budget ({limit - value:,} spare)"
    return (
        f'<div class="gauge" data-key="{key}" data-state="{"over" if over else "ok"}">'
        f'<div class="gauge-head"><span>{_e(label)}</span><span class="gauge-num">{value:,} / {limit:,}</span></div>'
        f'<div class="bar"><i style="width:{pct:.1f}%"></i></div>'
        f'<div class="gauge-state">{state}</div></div>'
    )


def _qa_html(qa: InspectorQA | None) -> str:
    if qa is None:
        return '<p class="muted">No QA results for this asset yet.</p>'
    parts = [
        '<p class="qa-line"><strong>Tier 1:</strong> '
        + (
            '<span class="pill pass">PASS</span>' if qa.tier1_ok else '<span class="pill fail">FAIL</span>'
        )
        + f" {_e(qa.tier1_line)}</p>"
    ]
    thr = EXPORT_MIN_TIER15_SCORE
    if qa.tier15_score is None:
        parts.append(f'<p class="qa-line"><strong>Tier 1.5:</strong> not scored (export threshold {thr:.1f})</p>')
    else:
        ok = qa.tier15_score >= thr
        scale = max(4.0, qa.tier15_score)  # rubrics are 0-4
        parts.append(
            f'<div class="gauge" data-key="tier15" data-state="{"ok" if ok else "over"}">'
            f'<div class="gauge-head"><span><strong>Tier 1.5 score</strong></span>'
            f'<span class="gauge-num">{qa.tier15_score:.2f} (export threshold {thr:.1f})</span></div>'
            f'<div class="bar"><i style="width:{min(100.0, qa.tier15_score / scale * 100):.1f}%"></i>'
            f'<span class="tick" style="left:{thr / scale * 100:.1f}%"></span></div>'
            f'<div class="gauge-state">{"Meets export threshold" if ok else "BELOW export threshold by " + format(thr - qa.tier15_score, ".2f")}</div></div>'
        )
    if qa.defects:
        parts.append('<h2 style="margin-top:10px">Defect probability</h2>')
        for name, p in sorted(qa.defects.items(), key=lambda kv: kv[1], reverse=True):
            pc = max(0.0, min(1.0, float(p))) * 100
            parts.append(
                f'<div class="defect"><span>{_e(name)}</span><div class="bar"><i style="width:{pc:.0f}%"></i></div>'
                f'<span class="pct">{pc:.0f}%</span></div>'
            )
    if qa.audit_deltas:
        parts.append('<h2 style="margin-top:10px">Audit deltas</h2><ul class="deltas">')
        parts += [f"<li>{_e(d)}</li>" for d in qa.audit_deltas]
        parts.append("</ul>")
    return "".join(parts)


def _json_for_script(obj: object) -> str:
    """JSON safe inside <script>: '<', '>' and '&' (so '</script>' and '<!--' cannot occur) and U+2028/9 are escaped."""
    s = json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# --------------------------------------------------------------------------- data model


def _png_bytes(data: bytes) -> bytes:
    """PNG as-is; any other image format Pillow reads is converted (the viewer uses PNG data URIs)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def _qa_dict(qa: InspectorQA | dict | None) -> dict | None:
    if qa is None or isinstance(qa, dict):
        return qa
    return {"tier1_ok": bool(qa.tier1_ok), "tier1_line": qa.tier1_line, "tier15_score": qa.tier15_score,
            "defects": {str(k): float(v) for k, v in qa.defects.items()}, "audit_deltas": list(qa.audit_deltas),
            "threshold": EXPORT_MIN_TIER15_SCORE}


def inspector_payload(
    meshes: list[MeshData],
    *,
    asset_name: str,
    budget: EngineBudget,
    textures: dict[str, InspectorTextures] | None = None,
    lods: list[InspectorLod | tuple[str, np.ndarray, np.ndarray]] | None = None,
    colliders: list[InspectorCollider | tuple[str, np.ndarray, np.ndarray]] | None = None,
    qa: InspectorQA | dict | None = None,
    uvs: dict[str, np.ndarray] | None = None,
    draw_calls: int | None = None,
    split_textures: bool = False,
) -> tuple[dict, dict[str, str]]:
    """The inspector's data model (what the front end renders) and its separately fetched parts.

    Returns ``(payload, parts)``. With ``split_textures`` every texture in ``payload["textures"]`` is
    a reference ``{"part": "tex/<material>/<kind>", "mime", "bytes"}`` and ``parts`` maps the part
    name to the base64 PNG (the MCP App fetches them through an app-only tool); otherwise textures
    are embedded as data URIs and ``parts`` is empty.

    ``payload["revision"]`` is a content hash of everything shown (geometry, UVs, LODs, colliders,
    texture bytes, budget): the same asset gives the same revision, any visible change a new one.
    Review notes are tied to it.
    """
    textures = textures or {}
    uvs = uvs or {}
    lod_list = [InspectorLod(*x) if isinstance(x, tuple) else x for x in (lods or [])]
    col_list = [InspectorCollider(*x) if isinstance(x, tuple) else x for x in (colliders or [])]

    ids = [m.node_id for m in meshes]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate node_id in meshes")
    colors = _prim_colors(ids)

    mesh_payload = []
    for m, color in zip(meshes, colors, strict=True):
        pos, idx, ntri = _geom(m.points, m.triangles, f"mesh {m.node_id}")
        uv = uvs.get(m.node_id)
        uv_b64 = None
        if uv is not None:
            uv = np.asarray(uv)
            if uv.shape != (len(m.points), 2):
                raise ValueError(f"uvs[{m.node_id!r}] must be ({len(m.points)}, 2), got {uv.shape}")
            uv_b64 = _b64(uv, "<f4")
        mesh_payload.append(
            {"id": m.node_id, "prim": m.prim_path, "mat": m.material_id or "", "color": color, "tris": ntri,
             "pos": pos, "idx": idx, "uv": uv_b64}
        )

    digest = hashlib.sha256()
    tex_payload: dict[str, dict] = {}
    parts: dict[str, str] = {}
    for mat in sorted(textures):
        t = textures[mat]
        entry: dict = {}
        for kind, raw in (("albedo", t.albedo_png), ("orm", t.orm_png), ("normal", t.normal_png)):
            if not raw:
                continue
            png = _png_bytes(raw)
            digest.update(f"tex:{mat}:{kind}:".encode() + hashlib.sha256(png).digest())
            if split_textures:
                name = f"tex/{mat}/{kind}"
                parts[name] = base64.b64encode(png).decode("ascii")
                entry[kind] = {"part": name, "mime": "image/png", "bytes": len(png)}
            else:
                entry[kind] = _data_uri(png)
        if entry:
            tex_payload[mat] = entry

    lod_payload = []
    for lod in lod_list:
        pos, idx, ntri = _geom(lod.points, lod.triangles, f"lod {lod.label}")
        lod_payload.append({"label": lod.label, "tris": ntri, "pos": pos, "idx": idx})
    col_payload = []
    for c in col_list:
        pos, idx, _ = _geom(c.points, c.triangles, f"collider {c.name}")
        col_payload.append({"name": c.name, "pos": pos, "idx": idx})

    if draw_calls is None:
        draw_calls = len({m.material_id or "" for m in meshes})
    hulls = len(col_list) if colliders is not None else sum(1 for m in meshes if m.collider)
    payload = {
        "schema": PAYLOAD_SCHEMA,
        "asset_name": asset_name,
        "three_url": None,
        "budget": {
            "target_engine": budget.target_engine,
            "max_tris": budget.max_tris,
            "max_draw_calls": budget.max_draw_calls,
            "max_collision_hulls": budget.max_collision_hulls,
        },
        "summary": {"tris": sum(m["tris"] for m in mesh_payload), "draw_calls": int(draw_calls), "hulls": int(hulls)},
        "meshes": mesh_payload,
        "lods": lod_payload,
        "colliders": col_payload,
        "textures": tex_payload,
        "qa": _qa_dict(qa),
    }
    shown = {k: payload[k] for k in ("asset_name", "budget", "summary", "meshes", "lods", "colliders")}
    digest.update(json.dumps(shown, sort_keys=True, separators=(",", ":")).encode())
    payload["revision"] = digest.hexdigest()[:16]
    return payload, parts


@lru_cache(maxsize=4)
def _web_text(name: str) -> str:
    return resources.files("antigravity3d.artifacts").joinpath("web", name).read_text("utf-8")


def app_html() -> str:
    """The inspector MCP App document (``web/inspector_app.html``; build: ``apps/inspector/build.mjs``).

    ``$AGY3D_INSPECTOR_APP_HTML`` names another file to serve instead, for fast UI iteration: reopening
    the view picks up a rebuild without restarting the server (the "Packaging and iterating" advice in
    the Antigravity MCP Apps docs).
    """
    override = os.environ.get("AGY3D_INSPECTOR_APP_HTML")
    if override:
        return Path(override).read_text(encoding="utf-8")
    return _web_text("inspector_app.html")


# --------------------------------------------------------------------------- public API


def build_inspector(
    meshes: list[MeshData],
    *,
    asset_name: str,
    budget: EngineBudget,
    textures: dict[str, InspectorTextures] | None = None,
    lods: list[InspectorLod | tuple[str, np.ndarray, np.ndarray]] | None = None,
    colliders: list[InspectorCollider | tuple[str, np.ndarray, np.ndarray]] | None = None,
    qa: InspectorQA | None = None,
    uvs: dict[str, np.ndarray] | None = None,
    three_js_source: str | None = None,
    draw_calls: int | None = None,
    out_path: Path,
) -> Path:
    """Write the standalone inspector for one asset to ``out_path`` and return it.

    meshes   LOD0, one entry per node (world-space Z-up, as MeshData).
    textures keyed by ``MeshData.material_id``; used only for meshes that also have UVs.
    uvs      keyed by ``node_id``, (N, 2) per-vertex, bottom-left origin (USD/OpenGL).
    lods     extra levels shown as LOD1..n; each is a single merged mesh.
    colliders  hull meshes for the overlay. Hull gauge = len(colliders), or the count
             of ``collider=True`` meshes when no hulls are given.
    three_js_source  ES-module build of three.js (e.g. ``three.module.min.js`` text) to
             inline for fully offline use; otherwise the pinned cdnjs URL is loaded.
    """
    payload, _ = inspector_payload(meshes, asset_name=asset_name, budget=budget, textures=textures, lods=lods,
                                   colliders=colliders, qa=qa, uvs=uvs, draw_calls=draw_calls)
    payload["three_url"] = None if three_js_source is not None else THREE_JS_URL

    rows = []
    legend = ["<ul>"]
    for m in payload["meshes"]:
        sw = f'<span class="sw" style="background:{m["color"]}"></span>'
        rows.append(
            f'<tr data-node="{_e(m["id"], quote=True)}" aria-selected="false" title="{_e(m["prim"], quote=True)}">'
            f'<td>{sw}<button type="button" class="rowbtn">{_e(m["id"])}</button></td>'
            f'<td class="num">{m["tris"]:,}</td><td>{_e(m["mat"] or "default")}</td></tr>'
        )
        legend.append(f'<li><span class="sw" style="background:{m["color"]}"></span>{_e(m["id"])}</li>')
    legend.append("</ul>")
    s = payload["summary"]
    gauges = "".join(
        [
            _gauge("tris", "Triangles", s["tris"], budget.max_tris),
            _gauge("draws", "Draw calls", s["draw_calls"], budget.max_draw_calls),
            _gauge("hulls", "Collision hulls", s["hulls"], budget.max_collision_hulls),
        ]
    )

    if three_js_source is not None:
        inline = (
            '<script type="text/plain" id="three-src">'
            + three_js_source.replace("</script", "<\\/script")
            + "</script>"
        )
    else:
        inline = ""

    template = resources.files("antigravity3d.artifacts").joinpath("inspector_template.html").read_text("utf-8")
    # Single pass so substituted content (asset names, three.js) is never re-scanned for tokens.
    subs = {
        "TITLE": _e(f"{asset_name} - 3D inspector"),
        "ASSET_NAME": _e(asset_name),
        "ENGINE": _e(budget.target_engine),
        "GAUGES": gauges,
        "TABLE_ROWS": "".join(rows),
        "QA": _qa_html(qa),
        "LEGEND": "".join(legend),
        "PAYLOAD": _json_for_script(payload),
        "THREE_INLINE": inline,
        "APP_JS": _web_text("inspector_standalone.js"),
    }
    out = re.sub(r"@@([A-Z_]+)@@", lambda m: subs[m.group(1)], template)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    return out_path


def read_notes(path: Path) -> list[ReviewNote]:
    """Parse a notes file exported from the inspector's "Export notes as JSON" button."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != NOTES_SCHEMA:
        raise ValueError(f"not an inspector notes file (expected schema {NOTES_SCHEMA!r})")
    asset = str(doc.get("asset_name", ""))
    notes = []
    for n in doc.get("notes", []):
        text = str(n.get("text", "")).strip()
        if text:
            nid = n.get("node_id")
            notes.append(ReviewNote(asset_name=asset, node_id=None if nid is None else str(nid), text=text))
    return notes
