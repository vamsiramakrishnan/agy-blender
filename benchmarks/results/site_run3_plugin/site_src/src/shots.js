// shots.js: pure function of story time T.
// Framed from the exact bounds of containment_gate, artifact_vault, and anchor_clamp.

export const MOVE = 0.35;
export const REST = 0.7;
export const DESIGN_ASPECT = 1.6;

export const SHOTS = {
  intro: {
    pos: [-2.1, 2.25, 5.2],
    target: [0.05, 0.84, 0.0],
    fov: 30,
    span: 7,
    push: 0.11,
    subject: 'gate',
  },
  flood: {
    pos: [-3.5, 2.05, 4.4],
    target: [-0.45, 0.82, 0.05],
    fov: 32,
    span: 8,
    via: { pos: [-3.1, 2.35, 5.1], target: [-0.22, 0.84, 0.05] },
    subject: 'gate',
  },
  digest: {
    pos: [1.3, 2.1, 5.4],
    target: [0.45, 0.84, -0.05],
    fov: 31,
    span: 8,
    via: { pos: [-0.6, 2.55, 5.9], target: [0.25, 0.84, -0.05] },
    subject: 'gate',
  },
  retrieve: {
    pos: [4.55, 2.1, 4.9],
    target: [3.0, 0.82, -0.15],
    fov: 30,
    span: 7,
    via: { pos: [3.85, 2.35, 5.75], target: [2.92, 0.82, -0.15] },
    subject: 'vault',
  },
  anchors: {
    pos: [7.55, 1.95, 4.75],
    target: [5.9, 0.78, 0.25],
    fov: 30,
    span: 7,
    via: { pos: [5.95, 2.4, 5.6], target: [4.7, 0.8, 0.1] },
    subject: 'anchor',
  },
  handoff: {
    pos: [2.95, 4.95, 15.4],
    target: [2.95, 0.82, 0.0],
    fov: 32,
    span: 14,
    push: 0.05,
    via: { pos: [3.45, 4.65, 15.0], target: [3.0, 0.82, 0.0] },
    // Wide establishing shot of all three instruments on the continuous bench
  },
};

export const SHOW = {
  gate: ['intro', 'flood', 'digest', 'retrieve', 'anchors', 'handoff'],
  vault: ['intro', 'flood', 'digest', 'retrieve', 'anchors', 'handoff'],
  anchor: ['intro', 'flood', 'digest', 'retrieve', 'anchors', 'handoff'],
};

const DEFAULT_PUSH = 0.06;
const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);

export function timeline(ids) {
  return (id, local = 0) => {
    const i = ids.indexOf(id);
    if (i < 0) throw new Error(`at("${id}"): no chapter of the page has that id`);
    return i + local;
  };
}

export const ramp = (T, a, b) => {
  const t = clamp01((T - a) / (b - a));
  return t * t * t * (t * (t * 6 - 15) + 10);
};

function monotone(ts, ys) {
  const n = ts.length, d = [], m = new Array(n).fill(0);
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
    const h = ts[i + 1] - ts[i], t = (T - ts[i]) / h, t2 = t * t, t3 = t2 * t;
    return (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * m[i]
      + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * m[i + 1];
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
      const here = ids.filter((id) => on.includes(id));
      return [name, here.length ? [here[0], here[here.length - 1]] : null];
    })),
  };
  return cached;
}

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
  const k = c.shot(T), lo = Math.floor(k + 1e-9), hi = Math.min(n - 1, Math.ceil(k - 1e-9));
  const about = (j) => (SHOTS[known[j]].subject
    ? { props: [].concat(SHOTS[known[j]].subject), loose: false }
    : { props: Object.keys(SHOW).filter((name) => SHOW[name].includes(known[j])), loose: true });

  const has = (id) => known.includes(id);
  const layer = {
    floodIn: has('flood') ? ramp(T, at('flood', 0.0), at('flood', 0.5)) : 0,
    floodDivert: has('digest') ? ramp(T, at('digest', 0.05), at('digest', 0.65)) : 0,
    digestOut: has('digest') ? ramp(T, at('digest', 0.2), at('digest', 0.72)) : 0,
    retrieveOut: has('retrieve') ? ramp(T, at('retrieve', 0.12), at('retrieve', 0.72)) : 0,
    anchorShift: has('anchors') ? ramp(T, at('anchors', 0.15), at('anchors', 0.78)) : 0,
    handoffFlow: has('handoff') ? ramp(T, at('handoff', 0.05), at('handoff', 0.95)) : 0,
    storyShare: n > 0 ? T / n : 0,
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
    layer,
  };
}

export const restingT = (T, n) => Math.min(Math.floor(T), Math.max(0, n - 1)) + REST;
