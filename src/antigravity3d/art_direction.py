"""Named art directions: one file per style, read by the pipeline and by the agent.

Each style is ``plugin/antigravity-3d/skills/art-direction/styles/<id>.md`` (packaged with the plugin bundle). Its YAML
front matter holds what the pipeline applies (``pipeline``) and the text added to model calls (``briefs``); the Markdown
body is the agent's reference for the same style and states the same values in its "What the pipeline applies" table.
There is no second copy: this module parses the file, and ``tests/test_art_direction.py`` fails when the table or any
number in the prose disagrees with a field.

What a style can change, and where it is applied (every value is optional; ``None`` keeps the pipeline's own):

* ``tri_budget_prop`` / ``tri_budget_kit_piece``: lowers ``EngineBudget.max_tris`` for a prop / a kit piece. Never raises
  it: the effective budget is the smaller of the engine's (or ``--max-tris``) and the style's (:meth:`ArtDirection.budget`).
  Tier-1 and the engine compliance check enforce it as they enforce the engine budget.
* ``shading_crease_deg``: the crease angle of the shipped normals (``compile_ir.crease_normals``; the project default is
  40 degrees). 0 gives one normal per face (faceted). It reaches the USD, every GLB level, the inspector and the LOD pop
  check, so the gate judges what ships.
* ``texture_px_prop`` / ``texture_px_kit``: side of the generated maps (prop PBR maps / the kit trim sheet). Capped at
  the engine's ``max_texture_px``. Larger generated images are downscaled to it before they are written.
* ``texture_filter``: ``"nearest"`` writes a glTF sampler with NEAREST magnification and NEAREST_MIPMAP_NEAREST
  minification on every texture of every package GLB. USD's ``UsdUVTexture`` has no filter input, so the USD cannot carry
  it; the manifest records it.
* ``palette_max_colors``: the shipped albedo map of each material is quantised to at most this many colours (median cut,
  no dither), and the package check counts them.

Briefs: ``author`` is appended to the scene author's request (and the BLOCKOUT stage agent's system text), ``concept`` to
the concept-sheet prompt, ``texture`` to the texture and material prompts. Without an art direction nothing is appended
and no setting changes, so every output is what it was before styles existed.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

AssetClass = Literal["prop", "kit"]
STYLES_REL = "skills/art-direction/styles"
FILTERS = (None, "nearest")
GL_NEAREST, GL_NEAREST_MIPMAP_NEAREST = 9728, 9984  # glTF sampler filter enums
PIPELINE_FIELDS = ("tri_budget_prop", "tri_budget_kit_piece", "shading_crease_deg", "texture_px_prop", "texture_px_kit",
                   "texture_filter", "palette_max_colors")
BRIEFS = ("author", "concept", "texture")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


# Names a user may type for a style, resolved to the canonical id before anything else sees them: the manifest, the
# briefs and the session header carry only the canonical id. ``psx`` is how the owner asked for the retro low-res look;
# as a console nickname it stays out of every style file, the skill's prose and every model-facing brief
# (tests/test_art_direction.py exempts exactly these alias tokens).
ALIASES: dict[str, str] = {"psx": "retro-lowres"}


class UnknownArtDirection(ValueError):
    """An art direction id that has no style file. The message lists the valid ids."""


@dataclass(frozen=True)
class ArtDirection:
    id: str
    title: str
    tri_budget_prop: int | None = None
    tri_budget_kit_piece: int | None = None
    shading_crease_deg: float | None = None
    texture_px_prop: int | None = None
    texture_px_kit: int | None = None
    texture_filter: str | None = None
    palette_max_colors: int | None = None
    briefs: dict[str, str] = field(default_factory=dict, compare=False, hash=False)
    body: str = field(default="", repr=False, compare=False, hash=False)
    source: str = ""  # bundle-relative path of the style file
    sha256: str = ""  # of the style file's bytes: what was applied is pinned by content

    # ---- what the pipeline applies -----------------------------------------------------------------------------

    def budget(self, budget: Any, asset_class: AssetClass = "prop") -> Any:
        """The effective ``EngineBudget``: the style's triangle budget only ever lowers ``max_tris``."""
        cap = self.tri_budget_prop if asset_class == "prop" else self.tri_budget_kit_piece
        return budget if cap is None or cap >= budget.max_tris else replace(budget, max_tris=int(cap))

    def crease_deg(self) -> float:
        from antigravity3d.kernel.compile_ir import CREASE_DEG

        return CREASE_DEG if self.shading_crease_deg is None else float(self.shading_crease_deg)

    def texture_px(self, engine: str, asset_class: AssetClass, default: int) -> int:
        """Side of the generated maps: the style's size (else ``default``), never above the engine's ``max_texture_px``."""
        from antigravity3d.kernel.validator import ENGINE_RULES

        want = self.texture_px_prop if asset_class == "prop" else self.texture_px_kit
        size = default if want is None else int(want)
        cap = ENGINE_RULES[engine].max_texture_px  # type: ignore[index]
        return min(size, cap) if cap else size

    def sampler(self) -> Any:
        """The glTF sampler every package texture gets, or None (no sampler: the engine's default filtering)."""
        if self.texture_filter != "nearest":
            return None
        from antigravity3d.kernel.gltf_writer import Sampler

        return Sampler(mag_filter=GL_NEAREST, min_filter=GL_NEAREST_MIPMAP_NEAREST)

    def finish_map(self, image: Any, role: str, size: int | None) -> Any:
        """A generated map as it ships: downscaled to ``size`` when larger, and (albedo only) quantised to the
        palette limit. Deterministic; an image already within both limits is returned unchanged."""
        img = image
        if size is not None and max(img.size) > size:
            img = _resize(img, size, normal=role == "normal")
        if role == "albedo" and self.palette_max_colors is not None and count_colors(img) > self.palette_max_colors:
            img = quantize(img, self.palette_max_colors)
        return img

    # ---- briefs ---------------------------------------------------------------------------------------------------

    def brief(self, kind: str) -> str:
        return " ".join(str(self.briefs.get(kind, "")).split())

    def manifest_record(self, *, budget: Any, asset_class: AssetClass, texture_px: int | None) -> dict[str, Any]:
        """What was applied, for the package manifest / run report: the style, its file hash and the effective values."""
        return {
            "id": self.id, "source": self.source, "sha256": self.sha256,
            "effective": {
                "max_tris": int(budget.max_tris), "shading_crease_deg": self.crease_deg(), "texture_px": texture_px,
                "texture_filter": self.texture_filter, "palette_max_colors": self.palette_max_colors,
            },
        }


# --------------------------------------------------------------------------- loading


def styles_dir() -> Path:
    from antigravity3d.plugin_install import bundle_dir

    return bundle_dir() / STYLES_REL


def parse_style(path: Path, *, root: Path | None = None) -> ArtDirection:
    raw = path.read_bytes()
    m = _FRONT.match(raw.decode("utf-8"))
    if m is None:
        raise ValueError(f"{path.name}: expected YAML front matter between '---' lines")
    meta = yaml.safe_load(m.group(1)) or {}
    pipe = meta.get("pipeline") or {}
    briefs = meta.get("briefs") or {}
    unknown = set(pipe) - set(PIPELINE_FIELDS)
    if unknown:
        raise ValueError(f"{path.name}: unknown pipeline field(s) {sorted(unknown)}; known: {PIPELINE_FIELDS}")
    if set(briefs) - set(BRIEFS):
        raise ValueError(f"{path.name}: unknown brief(s) {sorted(set(briefs) - set(BRIEFS))}; known: {BRIEFS}")
    if meta.get("id") != path.stem:
        raise ValueError(f"{path.name}: id {meta.get('id')!r} must equal the file name {path.stem!r}")
    for key in ("tri_budget_prop", "tri_budget_kit_piece", "texture_px_prop", "texture_px_kit", "palette_max_colors"):
        v = pipe.get(key)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1):
            raise ValueError(f"{path.name}: {key} must be a positive integer or null, got {v!r}")
    for key in ("texture_px_prop", "texture_px_kit"):
        v = pipe.get(key)
        if v is not None and v & (v - 1):
            raise ValueError(f"{path.name}: {key} must be a power of two (engines mip and compress pow2 maps), got {v}")
    from antigravity3d.kernel.compile_ir import CREASE_DEG

    crease = pipe.get("shading_crease_deg")
    # At most the project default. The export decides which hidden contact faces can be left out without changing a
    # shipped corner normal at that default (fast_ops._crease_safe); a narrower crease smooths across fewer faces, so
    # that decision stays safe for it, while a wider one would smooth across faces the decision never looked at.
    if crease is not None and (isinstance(crease, bool) or not isinstance(crease, (int, float)) or not 0 <= crease <= CREASE_DEG):
        raise ValueError(f"{path.name}: shading_crease_deg must be within [0, {CREASE_DEG:g}] or null, got {crease!r}")
    if pipe.get("texture_filter") not in FILTERS:
        raise ValueError(f"{path.name}: texture_filter must be one of {FILTERS}, got {pipe.get('texture_filter')!r}")
    base = root if root is not None else path.parents[3]
    return ArtDirection(
        id=meta["id"], title=str(meta.get("title") or meta["id"]),
        **{k: (float(pipe[k]) if k == "shading_crease_deg" and pipe.get(k) is not None else pipe.get(k)) for k in PIPELINE_FIELDS},
        briefs={k: " ".join(str(v).split()) for k, v in briefs.items()}, body=m.group(2),
        source=path.relative_to(base).as_posix() if path.is_relative_to(base) else path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


@lru_cache(maxsize=4)
def _load(directory: str) -> dict[str, ArtDirection]:
    d = Path(directory)
    root = d.parents[2]  # the plugin bundle
    return {p.stem: parse_style(p, root=root) for p in sorted(d.glob("*.md"))}


def all_styles(directory: Path | None = None) -> dict[str, ArtDirection]:
    return dict(_load(str(directory or styles_dir())))


def style_ids() -> list[str]:
    return sorted(all_styles())


def load(style_id: str | None) -> ArtDirection | None:
    """The art direction named ``style_id`` or one of its ``ALIASES`` (None/"" -> None). An unknown id raises, listing
    the valid ones."""
    if not style_id:
        return None
    styles = all_styles()
    canonical = ALIASES.get(style_id.strip().lower(), style_id)
    if canonical not in styles:
        aliases = ", ".join(f"{a} = {c}" for a, c in sorted(ALIASES.items()))
        raise UnknownArtDirection(f"unknown art direction {style_id!r}; valid ids: {', '.join(sorted(styles))} (aliases: {aliases})")
    return styles[canonical]


# --------------------------------------------------------------------------- model-call text (identity without a style)


def author_text(art: ArtDirection | None) -> str:
    """The line appended to a scene-author request ('' without a style)."""
    return f"Art direction {art.id}: {art.brief('author')}" if art is not None and art.brief("author") else ""


def concept_prompt(prompt: str, art: ArtDirection | None) -> str:
    return f"{prompt}. Art direction: {art.brief('concept')}" if art is not None and art.brief("concept") else prompt


def texture_brief(art: ArtDirection | None) -> str:
    return art.brief("texture") if art is not None else ""


def estimate_tokens(text: str) -> int:
    """Offline input-token estimate: 3 bytes per token, the rule ``MeteredAuthor.cost_profile`` sizes requests with."""
    return -(-len(text.encode("utf-8")) // 3) if text else 0


# --------------------------------------------------------------------------- image helpers


def count_colors(image: Any) -> int:
    """Distinct RGB colours of an image."""
    import numpy as np

    a = np.asarray(image.convert("RGB"), dtype=np.uint32).reshape(-1, 3)
    return int(len(np.unique((a[:, 0] << 16) | (a[:, 1] << 8) | a[:, 2])))


def quantize(image: Any, colors: int) -> Any:
    """At most ``colors`` RGB colours (median cut, no dithering: flat colour areas, deterministic)."""
    from PIL import Image

    rgb = image.convert("RGB")
    out = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).convert("RGB")
    if image.mode == "RGBA":  # keep a cut-out / blend alpha channel as it was
        out.putalpha(image.getchannel("A"))
    return out


def _resize(image: Any, size: int, *, normal: bool) -> Any:
    import numpy as np
    from PIL import Image

    w, h = image.size
    k = size / max(w, h)
    target = (max(1, round(w * k)), max(1, round(h * k)))
    out = image.resize(target, Image.Resampling.BOX)  # area average: no ringing, exact for integer factors
    if not normal:
        return out
    a = np.asarray(out.convert("RGB"), np.float64) / 127.5 - 1.0  # re-normalise averaged tangent-space normals
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    a = np.where(n > 1e-6, a / np.maximum(n, 1e-6), [0.0, 0.0, 1.0])
    return Image.fromarray(np.clip(np.rint((a + 1.0) * 127.5), 0, 255).astype(np.uint8), "RGB")


def png_bytes(image: Any) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


__all__ = ["ArtDirection", "UnknownArtDirection", "all_styles", "author_text", "concept_prompt", "count_colors", "estimate_tokens",
           "load", "parse_style", "quantize", "style_ids", "styles_dir", "texture_brief"]
