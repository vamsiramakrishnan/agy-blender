// shots.js: the whole scene as a pure function of story time T. No three.js and no DOM here, so the page, a test and
// a screenshot tool can all call it, and any scroll position can be drawn, reversed or photographed alone.
//
//   evaluate(T, ids) -> { T, chapter, index, local, cam: { pos, target, fov }, focus: { at, span }, show: { prop: 0..1 },
//                         subject: [prop names] or null, frame: { from, to, mix } }
//   timeline(ids) -> at(id, local): the story time of a moment inside a chapter
//
// Everything is data: a shot per chapter and the chapters each prop is on stage. Tune by editing the two tables.
// Lifted from examples/straitjacket-film/site/js/scene/tracks.js (monotone curves through camera keys, so a hold is a
// real hold and a move never overshoots).

// The first MOVE of each chapter travels from the last shot to this chapter's shot; the rest of the chapter holds on it
// (a slow push-in, not a freeze). A reader reads during the hold, so the hold is the longer part.
export const MOVE = 0.35;
// Where a chapter rests when the visitor asked for reduced motion: the scene shows only resting pictures.
export const REST = 0.7;

// The window shape the shots are designed for: a shot's fov is its vertical view in a window this wide for its height
// (1440 x 900). A narrower window (a phone) gets a wider view that keeps the shot's horizontal coverage, so what was in
// frame at the design shape stays in frame (stage.js); a fixed widening factor cut run 2's props at both phone edges.
export const DESIGN_ASPECT = 1.6;

// One shot per chapter id of the page (index.html). pos and target in metres (+Y up), fov in degrees (vertical, at
// DESIGN_ASPECT),
// span: metres across the key light's shadow box at this shot, push: share of the distance the camera closes during
// the hold, via: an optional pose halfway through the move (to swing round a prop instead of through it), subject:
// the prop or props (names from SHOW) the shot is about. With a subject named, onScreen() marks each entry
// subject: true or false, and check_site reports a subject under the panel or cut by the frame without calling a prop
// that is only in the background a fault; without one, every prop counts as a subject.
// These are the starter's placeholder shots: replace them with the storyboard's.
export const SHOTS = {
  intro: { pos: [5.5, 3.0, 7.5], target: [0, 0.8, 0], fov: 30, span: 8, subject: 'hero' },
  how: { pos: [-4.5, 2.2, 4.5], target: [0.3, 0.8, 0.3], fov: 32, span: 7, via: { pos: [0.5, 3.4, 8.5], target: [0, 0.8, 0] },
    subject: ['hero', 'part'] },
  detail: { pos: [4.6, 2.0, 4.2], target: [1.1, 0.55, 0.4], fov: 30, span: 5, subject: 'part' },
  outro: { pos: [7.0, 6.0, 9.0], target: [0, 0.4, 0], fov: 30, span: 12, push: 0 }, // the whole scene: no one subject
};

// Which chapters each prop is on stage for (a prop rises in during the move into its first chapter and sinks out
// during the move after its last). Names are those of props.js.
export const SHOW = {
  hero: ['intro', 'how', 'detail', 'outro'],
  part: ['how', 'detail'],
};

const DEFAULT_PUSH = 0.05;
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
  return (T) => {
    if (T <= ts[0]) return ys[0];
    if (T >= ts[n - 1]) return ys[n - 1];
    let i = 0;
    while (T > ts[i + 1]) i++;
    const h = ts[i + 1] - ts[i], t = (T - ts[i]) / h, t2 = t * t, t3 = t2 * t;
    return (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * m[i] + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * m[i + 1];
  };
}

// The pose at the end of a shot's hold: the camera has closed `push` of its distance to the target.
const pushed = (s) => {
  const k = 1 - (s.push ?? DEFAULT_PUSH);
  return { ...s, pos: s.pos.map((p, j) => s.target[j] + (p - s.target[j]) * k) };
};

// Camera keys for the chapters in page order: [T, pose]. Chapter i arrives at i + MOVE and holds until i + 1.
function keysFor(ids) {
  const keys = [];
  ids.forEach((id, i) => {
    const s = SHOTS[id];
    // the third value: which shot the camera is on, i - 1 to i during the move into chapter i (see `frame` below)
    if (i === 0) keys.push([0, s, 0]);
    else {
      if (s.via) keys.push([i + MOVE / 2, { ...s, ...s.via }, i - 0.5]);
      keys.push([i + MOVE, s, i]);
    }
    keys.push([i + 1, pushed(s), i]);
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

// The chapter ids of the page that have no shot, and the SHOW names that are no chapter: the page calls this once and
// reports each as an error, because a chapter without a shot would draw the previous one's picture.
export function problems(ids) {
  const out = ids.filter((id) => !SHOTS[id]).map((id) => `chapter "${id}" has no shot in shots.js`);
  for (const [name, on] of Object.entries(SHOW)) {
    for (const id of on) if (!ids.includes(id)) out.push(`SHOW.${name} names "${id}", which is no chapter of the page`);
  }
  for (const id of ids) {
    for (const name of [].concat(SHOTS[id]?.subject ?? [])) {
      if (!SHOW[name]) out.push(`SHOTS.${id}.subject names "${name}", which is no prop in SHOW`);
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
  // What the stacked (phone) layout fits into the space above the panel: the shot's subject, or, for a shot that names
  // none, the props on stage in its chapter, framed loosely (stage.js). During a move it blends the last shot's
  // framing into the next one's on the camera's own curve, so the picture never jumps.
  const k = c.shot(T), lo = Math.floor(k + 1e-9), hi = Math.min(n - 1, Math.ceil(k - 1e-9));
  const about = (j) => (SHOTS[known[j]].subject
    ? { props: [].concat(SHOTS[known[j]].subject), loose: false }
    : { props: Object.keys(SHOW).filter((name) => SHOW[name].includes(known[j])), loose: true });
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
  };
}

// Reduced motion: the scene rests at one picture per chapter.
export const restingT = (T, n) => Math.min(Math.floor(T), Math.max(0, n - 1)) + REST;
