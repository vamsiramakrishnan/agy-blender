// shots.js: the whole scene as a pure function of story time T. No three.js and no DOM here, so the page, a test and
// a screenshot tool can all call it, and any scroll position can be drawn, reversed or photographed alone.
//
//   evaluate(T, ids) -> { T, chapter, index, local, cam: { pos, target, fov }, focus: { at, span }, show: { prop: 0..1 },
//                         subject: [names] or null, frame: { from, to, mix }, moving }
//   timeline(ids) -> at(id, local): the story time of a moment inside a chapter
//   frame(bounds, type, options) -> a shot of one type of the vocabulary (SHOT) around a sphere
//
// Everything is data: a shot per chapter and the chapters each prop is on stage. Tune by editing the tables.
// Lifted from examples/straitjacket-film/site/js/scene/tracks.js (monotone curves through camera keys, so a move never
// overshoots).

// The first MOVE of each chapter travels from the last shot to this chapter's shot (the first chapter's from the page's
// opening pose, OPENING below); the rest of the chapter is its hold, during which the camera keeps travelling slowly
// (push and orbit, below). The chapter's text is shown only
// during the hold (main.js), so MOVE is also the share of every chapter after the first that has no text on screen:
// at 0.35 and the starter's chapters of 220 % of the window, 77 % of a window's height of scroll per chapter is picture
// alone. A larger MOVE gives a slower, calmer move and a longer gap in the reading; a smaller one the reverse.
export const MOVE = 0.35;
// The page opens this many degrees back from its first shot, round the shot's target, and the first MOVE of the first
// chapter swings the camera round to the shot, so the first wheel notch changes the picture. The title stays up
// through it: the opening is the first shot's own subject seen from further round, where a move between two chapters
// passes one subject for another. Without it the first chapter was all hold, and a hold travels slowly and started
// from rest: one notch (120 px of scroll, 1440 x 900) at the top of run 5's page moved its picture by 0.15 px, as
// check_site reads it, and a visitor's first scroll showed nothing happening. Rebuilt with nothing else changed, the
// notch moves it 2.5 px with a curve that starts at speed (monotone(), below) and 8.8 px with this opening as well.
// 0 opens on the shot itself.
export const OPENING = 24;
// Where a chapter rests when the visitor asked for reduced motion: the scene shows only resting pictures.
export const REST = 0.7;

// The window shape the shots are designed for: a shot's fov is its vertical view in a window this wide for its height
// (1440 x 900). A narrower window (a phone) gets a wider view that keeps the shot's horizontal coverage, so what was in
// frame at the design shape stays in frame (stage.js); a fixed widening factor cut run 2's props at both phone edges.
export const DESIGN_ASPECT = 1.6;
// The share of that window's width the text panel leaves free (style.css: the panel takes the left 40 %). frame() fits
// a subject into this part, and the stacked phone layout gives the subject the share of its free area that the subject
// has of this one, so a close shot stays close and a wide one wide on a phone too (stage.js).
export const DESIGN_FREE = 0.6;

// The shot vocabulary. A chapter's shot differs from the one before it in scale (another row) or in angle (30 degrees
// or more of azimuth or elevation): run 3's six shots all stood 4.9 to 5.8 m off, 13 to 14 degrees up, on a 30 to 32
// degree lens, and its first two pictures were nearly the same. Per type: fill, the share of the free part of the
// window (its height, or its width when that binds) the subject's bounding sphere covers; elevation, degrees above the
// horizon; fov, the lens (vertical degrees at DESIGN_ASPECT; a narrower lens flattens and isolates, a wider one gives
// depth); push and orbit, the hold's travel (below).
export const SHOT = {
  wide: { fill: 0.4, elevation: 16, fov: 34, push: 0.1, orbit: 8 },     // establishing: where things are; the opening
  medium: { fill: 0.62, elevation: 22, fov: 30, push: 0.12, orbit: 6 }, // one object and what touches it
  close: { fill: 0.8, elevation: 30, fov: 22, push: 0.1, orbit: 5 },    // one part: a detail, a label, a mechanism
  top: { fill: 0.7, elevation: 72, fov: 30, push: 0.1, orbit: 14 },     // a layout, a flow, a count seen from above
  low: { fill: 0.6, elevation: 5, fov: 40, push: 0.06, orbit: 16 },     // scale and weight; orbiting low it tracks along
};

// The camera pose of a shot of `type` around a sphere (`center`, `radius` in metres: the loader's `bounds`, or around()
// of a box), seen from `azimuth` degrees round the vertical (0: from +Z, 90: from +X). Any field of SHOT can be
// overridden in the options; `aspect` is the free part's width over its height at the design shape (about 1 beside the
// starter's panel). frame(bounds, 'close', { azimuth: 60 }) is a whole shot but for its span and subject.
export function frame({ center, radius }, type = 'medium', { azimuth = 35, aspect = 1, ...options } = {}) {
  if (!SHOT[type]) throw new Error(`frame(): no shot type "${type}" (${Object.keys(SHOT).join(', ')})`);
  const { fill, elevation, fov, push, orbit } = { ...SHOT[type], ...options };
  const t = Math.tan((fov * Math.PI) / 360);
  const d = radius / (t * Math.min(1, aspect) * fill);
  const az = (azimuth * Math.PI) / 180, el = (elevation * Math.PI) / 180;
  const pos = [center[0] + d * Math.cos(el) * Math.sin(az), center[1] + d * Math.sin(el), center[2] + d * Math.cos(el) * Math.cos(az)];
  return { pos, target: [...center], fov, push, orbit };
}

// The bounding sphere of a box given by two corners, for frame(): around([x0, y0, z0], [x1, y1, z1]).
export function around(min, max) {
  const center = min.map((v, j) => (v + max[j]) / 2);
  return { center, radius: Math.hypot(...min.map((v, j) => max[j] - v)) / 2 };
}

// The stand-ins of props.js as onScreen() measures them, in world axes: the hero, the part (turned by half a radian,
// so its box is wider than its 0.5 m), and both together.
const HERO = around([-0.7, 0, -0.45], [0.7, 1.5, 0.45]);
const PART = around([1.36, 0, 0.36], [2.04, 0.45, 1.04]);
const BOTH = around([-0.7, 0, -0.45], [2.04, 1.5, 1.04]);

// One shot per chapter id of the page (index.html): pos and target in metres (+Y up), fov in degrees (vertical, at
// DESIGN_ASPECT); span: metres across the key light's shadow box at this shot; push: the share of its distance the
// camera closes during the hold; orbit: degrees it turns round the target during the hold; via: an optional pose
// halfway through the move (to swing round a prop instead of through it); subject: the props (names from SHOW) or
// layers (names from LAYERS) the shot is about; fill: on a portrait phone and in a still, the share of the free area
// the subject fills at the start of the hold, when the share it has at the design shape is not what you want. With a
// subject named, onScreen() marks each entry subject: true or false, and check_site reports a subject under the panel
// or cut by the frame without calling a prop that is only in the background a fault; without one, every prop counts as
// a subject.
// These are the starter's placeholder shots, three scales and a low group shot: replace them with the storyboard's.
export const SHOTS = {
  intro: { ...frame(HERO, 'wide', { azimuth: 30 }), span: 12 }, // no subject: an establishing shot of what is on stage
  how: { ...frame(HERO, 'medium', { azimuth: 55 }), span: 8, via: { pos: [-0.7, 4.3, 7.7], target: [0, 0.75, 0] },
    subject: 'hero' },
  detail: { ...frame(PART, 'close', { azimuth: 115, elevation: 38 }), span: 4, subject: 'part' },
  // The last picture is the one a visitor leaves with: both props, framed from the union of their bounds, from low.
  outro: { ...frame(BOTH, 'medium', { azimuth: 15, elevation: 9 }), span: 10, subject: ['hero', 'part'] },
};

// Which chapters each prop is on stage for (a prop rises in during the move into its first chapter and sinks out
// during the move after its last). Names are those of props.js.
export const SHOW = {
  hero: ['intro', 'how', 'detail', 'outro'],
  part: ['how', 'detail', 'outro'],
};

// The layers a shot may name as its subject: things that are not props (an InstancedMesh of slabs, generated
// geometry), registered by the page with layer(name, object) (stage.js). Run 3's chapters were about its slabs, and
// a subject could only be a prop, so on the phone the slabs fell out of the frame.
export const LAYERS = [];

// A shot written by hand without push or orbit travels this much during its hold. A hold that stands still is a
// portrait: run 3 closed in by 5 to 11 % over 1.4 window heights of scroll and the review found the picture hardly
// travelled. A push of a quarter in its place takes a subject framed at 0.6 of the free area to 0.8 by the end, against
// the panel and the frame's edges; turning round the target moves near and far things against each other, which is
// what reads as the camera moving.
const DEFAULT_PUSH = 0.12, DEFAULT_ORBIT = 6;
const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
// A moment of the story named by chapter: at('how', 0.5) is halfway through "how" wherever the page puts it. Key every
// timing of a layer of your own on this (ramp(T, at('how', 0.2), at('how', 0.6))) and a share of the whole story on
// T / ids.length: run 2 wrote ramp(T, 2.0, 2.85) and T / 7, which go wrong as soon as a chapter is added or moved.
export function timeline(ids) {
  return (id, local = 0) => {
    const i = ids.indexOf(id);
    if (i < 0) throw new Error(`at("${id}"): no chapter of the page has that id`);
    return i + local;
  };
}

// Smootherstep from a to b: 0 before a, 1 after b, no jerk at either end.
export const ramp = (T, a, b) => { const t = clamp01((T - a) / (b - a)); return t * t * t * (t * (t * 6 - 15) + 10); };

// Monotone cubic Hermite (Fritsch-Carlson) through (ts, ys): C1-smooth, no overshoot, flat between equal keys.
function monotone(ts, ys) {
  const n = ts.length, d = [], m = new Array(n).fill(0);
  if (n === 1) return () => ys[0];
  for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / (ts[i + 1] - ts[i]));
  for (let i = 1; i < n - 1; i++) {
    if (d[i - 1] * d[i] <= 0) continue;
    const w1 = 2 * (ts[i + 1] - ts[i]) + (ts[i] - ts[i - 1]), w2 = (ts[i + 1] - ts[i]) + 2 * (ts[i] - ts[i - 1]);
    m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
  }
  // The curve starts at speed: its first key takes the one-sided slope of the first two spans (the end rule of the
  // same shape-preserving scheme), so the first pixel of scroll already moves the camera. Starting from rest, the
  // page's first wheel notch covered under 2 % of the first span's travel. The last key keeps 0: the story comes to
  // rest at the end of the page.
  if (n > 2) {
    const h0 = ts[1] - ts[0], h1 = ts[2] - ts[1];
    const m0 = ((2 * h0 + h1) * d[0] - h0 * d[1]) / (h0 + h1);
    if (m0 * d[0] > 0) m[0] = d[0] * d[1] <= 0 && Math.abs(m0) > 3 * Math.abs(d[0]) ? 3 * d[0] : m0;
  } else m[0] = d[0];
  return (T) => {
    if (T <= ts[0]) return ys[0];
    if (T >= ts[n - 1]) return ys[n - 1];
    let i = 0;
    while (T > ts[i + 1]) i++;
    const h = ts[i + 1] - ts[i], t = (T - ts[i]) / h, t2 = t * t, t3 = t2 * t;
    return (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * m[i] + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * m[i + 1];
  };
}

// The pose at the end of a shot's hold: the camera has turned `orbit` degrees round the target's vertical and closed
// `push` of its distance to it. The target stays: the shot is still about the same thing.
const held = (s) => {
  const k = 1 - (s.push ?? DEFAULT_PUSH), a = ((s.orbit ?? DEFAULT_ORBIT) * Math.PI) / 180;
  const dx = s.pos[0] - s.target[0], dy = s.pos[1] - s.target[1], dz = s.pos[2] - s.target[2];
  const c = Math.cos(a), sn = Math.sin(a);
  return { ...s, pos: [s.target[0] + k * (dx * c + dz * sn), s.target[1] + k * dy, s.target[2] + k * (dz * c - dx * sn)] };
};

// Camera keys for the chapters in page order: [T, pose]. Chapter i arrives at i + MOVE and travels until i + 1; the
// first chapter arrives from its opening pose (OPENING), which is its own shot and so not a move between two shots.
function keysFor(ids) {
  const keys = [];
  ids.forEach((id, i) => {
    const s = SHOTS[id];
    // the third value: which shot the camera is on, i - 1 to i during the move into chapter i (see `frame` below)
    if (i === 0) {
      if (OPENING) keys.push([0, held({ ...s, push: 0, orbit: -OPENING }), 0]);
      keys.push([OPENING ? MOVE : 0, s, 0]);
    } else {
      if (s.via) keys.push([i + MOVE / 2, { ...s, ...s.via }, i - 0.5]);
      keys.push([i + MOVE, s, i]);
    }
    keys.push([i + 1, held(s), i]);
  });
  return keys;
}

let cached = null;
function curves(ids) {
  const sig = ids.join('|');
  if (cached && cached.sig === sig) return cached;
  const keys = keysFor(ids), ts = keys.map((k) => k[0]);
  const chan = (f) => monotone(ts, keys.map(f));
  cached = {
    sig,
    pos: [0, 1, 2].map((j) => chan((k) => k[1].pos[j])),
    target: [0, 1, 2].map((j) => chan((k) => k[1].target[j])),
    fov: chan((k) => k[1].fov),
    span: chan((k) => k[1].span),
    shot: chan((k) => k[2]),
    show: Object.fromEntries(Object.entries(SHOW).map(([name, on]) => {
      const here = ids.filter((id) => on.includes(id)); // in page order
      return [name, here.length ? [here[0], here[here.length - 1]] : null];
    })),
  };
  return cached;
}

// The chapter ids of the page that have no shot, the SHOW names that are no chapter and the subjects that are neither a
// prop nor a layer: the page calls this once and reports each as an error, because a chapter without a shot would draw
// the previous one's picture.
export function problems(ids) {
  const out = ids.filter((id) => !SHOTS[id]).map((id) => `chapter "${id}" has no shot in shots.js`);
  for (const [name, on] of Object.entries(SHOW)) {
    for (const id of on) if (!ids.includes(id)) out.push(`SHOW.${name} names "${id}", which is no chapter of the page`);
  }
  for (const id of ids) {
    for (const name of [].concat(SHOTS[id]?.subject ?? [])) {
      if (!SHOW[name] && !LAYERS.includes(name)) out.push(`SHOTS.${id}.subject names "${name}", which is no prop in SHOW and no layer in LAYERS`);
    }
  }
  return out;
}

export function evaluate(T, ids) {
  const known = ids.filter((id) => SHOTS[id]);
  const n = known.length;
  T = Math.max(0, Math.min(n, Number(T) || 0));
  const c = curves(known);
  const at = timeline(known);
  const index = Math.min(n - 1, Math.floor(T));
  const show = {};
  for (const [name, range] of Object.entries(c.show)) {
    if (!range) { show[name] = 0; continue; }
    const [first, last] = range;
    const rise = first === known[0] ? 1 : ramp(T, at(first), at(first, MOVE));
    const sink = last === known[n - 1] ? 0 : ramp(T, at(last, 1), at(last, 1 + MOVE));
    show[name] = rise * (1 - sink);
  }
  const target = c.target.map((f) => f(T));
  // What the stacked (phone) layout and a still fit into the free area: the shot's subject, or, for a shot that names
  // none, the props on stage in its chapter, seen from the pose the shot arrives at (stage.js). During a move it blends
  // the last shot's framing into the next one's on the camera's own curve, so the picture never jumps.
  const k = c.shot(T), lo = Math.floor(k + 1e-9), hi = Math.min(n - 1, Math.ceil(k - 1e-9));
  const about = (j) => {
    const s = SHOTS[known[j]];
    return { names: s.subject ? [].concat(s.subject) : Object.keys(SHOW).filter((name) => SHOW[name].includes(known[j])),
      pose: { pos: s.pos, target: s.target, fov: s.fov }, fill: s.fill ?? null };
  };
  return {
    T,
    chapter: known[index],
    index,
    local: T - index,
    cam: { pos: c.pos.map((f) => f(T)), target, fov: c.fov(T) },
    focus: { at: [target[0], 0, target[2]], span: c.span(T) },
    show,
    subject: SHOTS[known[index]].subject ? [].concat(SHOTS[known[index]].subject) : null,
    frame: { from: about(lo), to: about(hi), mix: hi > lo ? k - lo : 0 },
    // The camera has not yet arrived at this chapter's shot: frame.mix between 0 and 1, and also the chapter's very
    // first scroll position, where the move has not begun (mix is still 0 on the last shot). stage.js reports it as
    // info().moving and main.js hides the text panels while it is true, so the two never disagree.
    moving: k < index - 1e-9,
  };
}

// Reduced motion: the scene rests at one picture per chapter.
export const restingT = (T, n) => Math.min(Math.floor(T), Math.max(0, n - 1)) + REST;
