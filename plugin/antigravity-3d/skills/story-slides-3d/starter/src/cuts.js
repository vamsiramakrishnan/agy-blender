// cuts.js: how one picture gives way to the next. A cut is a GLSL function the print runs for every cell:
//   Cut cut_<name>(vec2 q, vec2 grid, float p)
// q is the cell's position in cut cells (continuous, origin at the lower left), grid the picture's size in cut cells,
// p the cut's progress 0..1. It returns a Cut (print.js): `side` (0 the leaving picture, 1 the arriving one, between
// the two mixed), `src` (where in the chosen picture to look, in cut cells), `black` (1: the cell is paper) and `hot`
// (a brightness that leads the cut's edge). Cut cells are the treatment's own cells, so a cut looks made of the same
// stuff as the picture: dots fall as dots, glyphs as glyphs. The print shows exactly the leaving picture at p = 0 and
// exactly the arriving one at p = 1 whatever a cut computes there (print.js, cutAt).
// `dur` is in seconds; a film names a cut on the scene it enters (enterWith).
export const CUTS = {
  // A cross-fade. In a cell treatment each cell switches at its own random moment (a dissolve of whole cells, which
  // is what a fade can be in dots or glyphs); in grade (uSoft 1) the two pictures mix.
  fade: {
    dur: 1.2,
    glsl: /* glsl */ `
Cut cut_fade(vec2 q, vec2 grid, float p) {
  float at = hash(floor(q) + 11.0) * 0.8 + 0.1;
  return Cut(mix(step(at, p), p, uSoft), q, 0.0, 0.0);
}`,
  },
  // The picture closes to a point on its centre, the frame is black for a moment, and the next opens from there.
  iris: {
    dur: 2.0,
    glsl: /* glsl */ `
Cut cut_iris(vec2 q, vec2 grid, float p) {
  vec2 aspect = vec2(1.0, uCutPx.y / uCutPx.x);
  float r = length((floor(q) + 0.5 - grid * 0.5) * aspect);
  float R = length(grid * 0.5 * aspect) + 2.0;
  float k = abs(1.0 - 2.0 * p); k = k * k * (3.0 - 2.0 * k);
  float black = step(R * k, r);
  float hot = (1.0 - black) * step(R * k - 1.5, r) * 0.8;
  return Cut(step(0.5, p), q, black, hot);
}`,
  },
  // A typewriter: the new picture comes row by row from the top, a bright cursor running ahead along the row.
  wipe: {
    dur: 1.8,
    glsl: /* glsl */ `
Cut cut_wipe(vec2 q, vec2 grid, float p) {
  vec2 c = floor(q);
  float rows = ceil(grid.y), row = rows - 1.0 - c.y;
  float at = p * rows, done = floor(at), x = fract(at) * (ceil(grid.x) + 3.0);
  if (row < done || (row < done + 1.0 && c.x < x - 2.0)) return Cut(1.0, q, 0.0, 0.0);
  return Cut(0.0, q, 0.0, row < done + 1.0 && c.x < x ? 1.0 : 0.0);
}`,
  },
  // Columns of the old picture drop out of the frame, each after its own delay, and the new one is behind them.
  fall: {
    dur: 2.2,
    glsl: /* glsl */ `
Cut cut_fall(vec2 q, vec2 grid, float p) {
  vec2 c = floor(q);
  float rows = ceil(grid.y), row = rows - 1.0 - c.y;
  float delay = hash(vec2(c.x, 7.0)) * 0.45;
  float t = clamp((p - delay) / 0.55, 0.0, 1.0); t *= t;
  float drop = floor(t * (rows + 1.0));
  if (row < drop) return Cut(1.0, q, 0.0, row > drop - 3.0 && t < 1.0 ? 0.85 : 0.0);
  return Cut(0.0, q + vec2(0.0, drop), 0.0, 0.0);
}`,
  },
  // Horizontal bands turn over one after another from the top, the new picture on their backs.
  flip: {
    dur: 1.6,
    glsl: /* glsl */ `
Cut cut_flip(vec2 q, vec2 grid, float p) {
  float rows = ceil(grid.y), bh = 6.0, nb = ceil(rows / bh);
  float rowT = rows - q.y, band = floor(rowT / bh), v = fract(rowT / bh) - 0.5;
  float turn = clamp(p * 1.7 - band / nb * 0.7, 0.0, 1.0);
  float k = abs(cos(turn * 3.14159265));
  if (abs(v) > 0.5 * k) return Cut(0.0, q, 1.0, 0.0);
  float src = (band + v / max(k, 0.02) + 0.5) * bh;
  return Cut(step(0.5, turn), vec2(q.x, rows - src), 0.0, (1.0 - k) * 0.25);
}`,
  },
};

// Back, Home, End and jumps by hash: a short fade, so moving about the film is quick and never confused with a scene's
// own entrance.
export const QUICK = 0.45;

// The cuts' names in the order the print numbers them (uCut).
export const CUT_NAMES = Object.keys(CUTS);
