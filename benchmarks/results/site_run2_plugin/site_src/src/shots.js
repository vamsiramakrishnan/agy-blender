// shots.js: pure deterministic function of story time T (no three.js, no DOM).
// Every scroll position maps to one composed frame along the continuous Evidence Bench:
//   capture_gate [0, 0, 0] -> artifact_vault [3.2, 0, -0.5] -> anchor_clamp [6.0, 0, 0.4]

export const MOVE = 0.35;
export const REST = 0.7;

export const SHOTS = {
  // 01: Three-quarter establishing view of capture_gate with intake column and bench clear of panel.
  intro: {
    pos: [-1.75, 2.25, 5.55],
    target: [0.1, 0.88, -0.05],
    fov: 31,
    span: 9.5,
    push: 0.05,
  },
  // 02: Push left-front into the red 302,628-token flood approaching capture_gate's intake.
  flood: {
    pos: [-3.75, 1.9, 3.8],
    target: [-0.95, 0.9, 0.0],
    fov: 30,
    span: 7.5,
    push: 0.06,
  },
  // 03: Swing smoothly around the front of capture_gate to see the bounded 531-token blue digest exit toward the vault.
  digest: {
    pos: [2.2, 1.75, 4.15],
    target: [0.85, 0.85, -0.1],
    fov: 30,
    span: 7.5,
    via: { pos: [-0.6, 2.55, 5.4], target: [0.2, 0.9, 0.0] },
    push: 0.05,
  },
  // 04: Close three-quarter shot of artifact_vault with the single blue bounded slice extracting on its front rail.
  retrieve: {
    pos: [4.65, 1.75, 3.45],
    target: [3.25, 0.75, -0.2],
    fov: 29,
    span: 6.5,
    push: 0.05,
  },
  // 05: Glide across to anchor_clamp showing the source stack shifting and relocating cleanly inside the jaws.
  anchors: {
    pos: [7.65, 1.7, 3.9],
    target: [6.0, 0.75, 0.4],
    fov: 29,
    span: 6.5,
    via: { pos: [5.5, 2.2, 4.3], target: [4.6, 0.75, 0.1] },
    push: 0.05,
  },
  // 06: Elevated angled view of the seven host lanes aligned beside artifact_vault and anchor_clamp.
  harnesses: {
    pos: [4.65, 4.65, 6.05],
    target: [4.55, 0.55, 1.35],
    fov: 31,
    span: 11.5,
    push: 0.04,
  },
  // 07: Wide establishing pull-back encompassing all three instruments along the Evidence Bench.
  start: {
    pos: [3.05, 3.65, 10.6],
    target: [3.0, 0.75, 0.15],
    fov: 31,
    span: 15,
    push: 0.02,
  },
};

// All three instruments remain grounded on the bench throughout the scroll so transitions never show an empty frame.
const ALL_CHAPTERS = ['intro', 'flood', 'digest', 'retrieve', 'anchors', 'harnesses', 'start'];
export const SHOW = {
  capture_gate: ALL_CHAPTERS,
  artifact_vault: ALL_CHAPTERS,
  anchor_clamp: ALL_CHAPTERS,
};

const DEFAULT_PUSH = 0.05;
const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
export const ramp = (T, a, b) => {
  const t = clamp01((T - a) / (b - a));
  return t * t * t * (t * (t * 6 - 15) + 10);
};

function monotone(ts, ys) {
  const n = ts.length;
  const d = [];
  const m = new Array(n).fill(0);
  if (n === 1) return () => ys[0];
  for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / (ts[i + 1] - ts[i]));
  for (let i = 1; i < n - 1; i++) {
    if (d[i - 1] * d[i] <= 0) continue;
    const w1 = 2 * (ts[i + 1] - ts[i]) + (ts[i] - ts[i - 1]);
    const w2 = (ts[i + 1] - ts[i]) + 2 * (ts[i] - ts[i - 1]);
    m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
  }
  return (T) => {
    if (T <= ts[0]) return ys[0];
    if (T >= ts[n - 1]) return ys[n - 1];
    let i = 0;
    while (T > ts[i + 1]) i++;
    const h = ts[i + 1] - ts[i];
    const t = (T - ts[i]) / h;
    const t2 = t * t;
    const t3 = t2 * t;
    return (
      (2 * t3 - 3 * t2 + 1) * ys[i] +
      (t3 - 2 * t2 + t) * h * m[i] +
      (-2 * t3 + 3 * t2) * ys[i + 1] +
      (t3 - t2) * h * m[i + 1]
    );
  };
}

const pushed = (s) => {
  const k = 1 - (s.push ?? DEFAULT_PUSH);
  return { ...s, pos: s.pos.map((p, j) => s.target[j] + (p - s.target[j]) * k) };
};

function keysFor(ids) {
  const keys = [];
  ids.forEach((id, i) => {
    const s = SHOTS[id];
    if (i === 0) keys.push([0, s]);
    else {
      if (s.via) keys.push([i + MOVE / 2, { ...s, ...s.via }]);
      keys.push([i + MOVE, s]);
    }
    keys.push([i + 1, pushed(s)]);
  });
  return keys;
}

let cached = null;
function curves(ids) {
  const sig = ids.join('|');
  if (cached && cached.sig === sig) return cached;
  const keys = keysFor(ids);
  const ts = keys.map((k) => k[0]);
  const chan = (f) => monotone(ts, keys.map(f));
  cached = {
    sig,
    pos: [0, 1, 2].map((j) => chan((k) => k[1].pos[j])),
    target: [0, 1, 2].map((j) => chan((k) => k[1].target[j])),
    fov: chan((k) => k[1].fov),
    span: chan((k) => k[1].span),
    show: Object.fromEntries(
      Object.entries(SHOW).map(([name, on]) => {
        const idx = on.map((id) => ids.indexOf(id)).filter((i) => i >= 0);
        return [name, idx.length ? [Math.min(...idx), Math.max(...idx)] : null];
      }),
    ),
  };
  return cached;
}

export function problems(ids) {
  const out = ids.filter((id) => !SHOTS[id]).map((id) => `chapter "${id}" has no shot in shots.js`);
  for (const [name, on] of Object.entries(SHOW)) {
    for (const id of on) {
      if (!ids.includes(id)) out.push(`SHOW.${name} names "${id}", which is no chapter of the page`);
    }
  }
  return out;
}

export function evaluate(T, ids) {
  const known = ids.filter((id) => SHOTS[id]);
  const n = known.length;
  T = Math.max(0, Math.min(n, Number(T) || 0));
  const c = curves(known);
  const index = Math.min(n - 1, Math.floor(T));
  const show = {};
  for (const [name, range] of Object.entries(c.show)) {
    if (!range) {
      show[name] = 0;
      continue;
    }
    const [a, b] = range;
    const rise = a === 0 ? 1 : ramp(T, a, a + MOVE);
    const sink = b === n - 1 ? 0 : ramp(T, b + 1, b + 1 + MOVE);
    show[name] = rise * (1 - sink);
  }
  const target = c.target.map((f) => f(T));
  return {
    T,
    chapter: known[index],
    index,
    local: T - index,
    cam: { pos: c.pos.map((f) => f(T)), target, fov: c.fov(T) },
    focus: { at: [target[0], 0, target[2]], span: c.span(T) },
    show,
  };
}

export const restingT = (T, n) => Math.min(Math.floor(T), Math.max(0, n - 1)) + REST;
