"""A stencil typeface as a table of strokes, and the text it draws on a surface: no font file, no image library.

Each glyph is a few polylines on a 4 x 6 grid (y up). A stencil plate cannot hold a free-floating island, so its
letters are cut from separate strokes with gaps where strokes meet; here every polyline is shortened at both ends by
``GAP`` grid units, which opens a closed bowl (O, D, 0) where its polyline closes and detaches the bars of E, F, H
from their stems: the look of sprayed stencil lettering, from the same table on every machine.

Text is evaluated at points given in the face's own metres (``draw_text``), anti-aliased over one texel, so it is as
sharp as the atlas allows and has no pixel staircase of its own.
"""

from __future__ import annotations

import numpy as np

#: Grid units (the cap height is 6): stroke width, the gap at each end of a stroke, and the advance between glyphs.
STROKE = 0.95
GAP = 0.45
ADVANCE = 5.6
WORD_SPACE = 3.6

_O = [[(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1.2, 0)]]
_P = [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0.6, 3)]]

GLYPHS: dict[str, list[list[tuple[float, float]]]] = {
    # a pointed A: with a flat top it read as R at the size a label has on a 330 px tile ("LOT R-458")
    "A": [[(0, 0), (2, 6), (4, 0)], [(0.9, 2.2), (3.1, 2.2)]],
    "B": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0.6, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (0.6, 0)]],
    "C": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1)]],
    "D": [[(0, 0), (0, 6), (2.5, 6), (4, 4.5), (4, 1.5), (2.5, 0), (0.6, 0)]],
    "E": [[(4, 6), (0, 6), (0, 0), (4, 0)], [(0.6, 3), (3, 3)]],
    "F": [[(4, 6), (0, 6), (0, 0)], [(0.6, 3), (3, 3)]],
    "G": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0.6, 3), (3.4, 3)]],
    "I": [[(1, 6), (3, 6)], [(2, 5.4), (2, 0.6)], [(1, 0), (3, 0)]],
    "J": [[(4, 6), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0.6, 2.6)], [(1.6, 3.4), (4, 0)]],
    "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]],
    "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": _O,
    "P": _P,
    "Q": _O + [[(2.4, 1.6), (4, 0)]],
    "R": _P + [[(2, 3), (4, 0)]],
    "S": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "T": [[(0, 6), (4, 6)], [(2, 5.4), (2, 0)]],
    "U": [[(0, 6), (0, 1), (1, 0), (3, 0), (4, 1), (4, 6)]],
    "V": [[(0, 6), (2, 0), (4, 6)]],
    "W": [[(0, 6), (1, 0), (2, 3.5), (3, 0), (4, 6)]],
    "X": [[(0, 6), (4, 0)], [(4, 6), (0, 0)]],
    "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 2.6), (2, 0)]],
    "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    "0": _O,
    "1": [[(0.8, 4.8), (2, 6), (2, 0)], [(0.8, 0), (3.2, 0)]],
    "2": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)]],
    "3": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (1.4, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "4": [[(3, 0), (3, 6), (0, 2), (4, 2)]],
    "5": [[(4, 6), (0, 6), (0, 3.4), (3, 3.4), (4, 2.4), (4, 1), (3, 0), (0, 0)]],
    "6": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2.4), (3, 3.4), (0.6, 3.4)]],
    "7": [[(0, 6), (4, 6), (1.4, 0)]],
    "8": [[(1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (1, 3), (0, 2), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3)]],
    "9": [[(3.4, 2.6), (1, 2.6), (0, 3.6), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (0, 0)]],
    "-": [[(0.6, 3), (3.4, 3)]],
    "/": [[(0, 0), (4, 6)]],
    ".": [[(1.7, 0), (2.3, 0)]],
    ":": [[(2, 0.4), (2, 1.2)], [(2, 4.2), (2, 5.0)]],
}


def _segments(polylines: list[list[tuple[float, float]]]) -> np.ndarray:
    """(S, 2, 2) segments of a glyph, every polyline shortened by ``GAP`` at both ends (the stencil bridges)."""
    out = []
    for pl in polylines:
        p = np.asarray(pl, np.float64)
        seg = np.stack([p[:-1], p[1:]], 1)
        L = np.linalg.norm(seg[:, 1] - seg[:, 0], axis=1)
        total = L.sum()
        if total <= 2 * GAP:  # a dot or a short tick: kept whole
            out.extend(seg.tolist())
            continue
        # cut GAP off the start of the first segments and off the end of the last ones
        cum = np.concatenate([[0.0], np.cumsum(L)])
        keep = []
        for k in range(len(seg)):
            a0, a1 = max(cum[k], GAP), min(cum[k + 1], total - GAP)
            if a1 <= a0:
                continue
            d = (seg[k, 1] - seg[k, 0]) / max(L[k], 1e-12)
            keep.append([seg[k, 0] + d * (a0 - cum[k]), seg[k, 0] + d * (a1 - cum[k])])
        out.extend(keep)
    return np.asarray(out, np.float64).reshape(-1, 2, 2)


_SEGMENTS = {ch: _segments(pl) for ch, pl in GLYPHS.items()}


def supported(text: str) -> bool:
    return all(ch == " " or ch in GLYPHS for ch in text)


def text_width(text: str, cap: float) -> float:
    """Width (same unit as ``cap``, the cap height) of ``text`` set in this face."""
    units = 0.0
    for i, ch in enumerate(text):
        units += WORD_SPACE if ch == " " else (4.0 + STROKE if i == len(text) - 1 else ADVANCE)
    return units * cap / 6.0


def draw_text(s: np.ndarray, t: np.ndarray, text: str, x0: float, y0: float, cap: float, texel: float) -> np.ndarray:
    """Coverage in [0, 1] at face points (s right, t up, metres) of ``text`` with its baseline-left corner at
    (x0, y0) and cap height ``cap``; edges anti-aliased over one ``texel``."""
    out = np.zeros(len(s), np.float32)
    k = cap / 6.0
    half = STROKE / 2.0
    aa = max(texel / k, 1e-6)
    gx = (s - x0) / k
    gy = (t - y0) / k
    pen = STROKE / 2.0       # the first stroke's ink starts at x0, as text_width counts it
    for ch in text:
        if ch == " ":
            pen += WORD_SPACE
            continue
        seg = _SEGMENTS.get(ch)
        if seg is None or not len(seg):
            pen += ADVANCE
            continue
        lx = gx - pen
        sel = np.flatnonzero((lx > -half - aa) & (lx < 4 + half + aa) & (gy > -half - aa) & (gy < 6 + half + aa))
        if len(sel):
            px, py = lx[sel], gy[sel]
            d = np.full(len(sel), np.inf)
            for (ax, ay), (bx, by) in seg:
                ex, ey = bx - ax, by - ay
                den = max(ex * ex + ey * ey, 1e-12)
                u = np.clip(((px - ax) * ex + (py - ay) * ey) / den, 0.0, 1.0)
                dx, dy = px - (ax + u * ex), py - (ay + u * ey)
                d = np.minimum(d, np.sqrt(dx * dx + dy * dy))
            cov = np.clip((half - d) / aa + 0.5, 0.0, 1.0).astype(np.float32)
            out[sel] = np.maximum(out[sel], cov)
        pen += ADVANCE
    return out
