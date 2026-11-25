// shots.js: the whole scene as a pure function of story time T.
// Seven chapters, one continuous architectural evidence bench, varied shot vocabulary framed from installed prop bounds.

export const MOVE = 0.35;
export const REST = 0.7;
export const DESIGN_ASPECT = 1.6;
export const DESIGN_FREE = 0.6;

export const SHOT = {
  wide: { fill: 0.44, elevation: 16, fov: 34, push: 0.1, orbit: 8 },
  medium: { fill: 0.62, elevation: 22, fov: 30, push: 0.12, orbit: 6 },
  close: { fill: 0.78, elevation: 26, fov: 22, push: 0.1, orbit: 5 },
  top: { fill: 0.74, elevation: 66, fov: 30, push: 0.08, orbit: 10 },
  low: { fill: 0.64, elevation: 6, fov: 40, push: 0.08, orbit: 12 },
};

export function frame({ center, radius }, type = 'medium', { azimuth = 35, aspect = 1, ...options } = {}) {
  if (!SHOT[type]) throw new Error(`frame(): no shot type "${type}" (${Object.keys(SHOT).join(', ')})`);
  const { fill, elevation, fov, push, orbit } = { ...SHOT[type], ...options };
  const t = Math.tan((fov * Math.PI) / 360);
  const d = radius / (t * Math.min(1, aspect) * fill);
  const az = (azimuth * Math.PI) / 180, el = (elevation * Math.PI) / 180;
  const pos = [
    center[0] + d * Math.cos(el) * Math.sin(az),
    center[1] + d * Math.sin(el),
    center[2] + d * Math.cos(el) * Math.cos(az),
  ];
  return { pos, target: [...center], fov, push, orbit };
}

export function around(min, max) {
  const center = min.map((v, j) => (v + max[j]) / 2);
  return { center, radius: Math.hypot(...min.map((v, j) => max[j] - v)) / 2 };
}

// World-space bounds computed from the three installed props (props.js) and layer zones.
const FLOOD_ZONE = around([-3.35, 0, -0.75], [0.85, 1.72, 0.55]);
const DIGEST_ZONE = around([-0.9, 0, -0.75], [1.75, 1.72, 0.55]);
const VAULT_ZONE = around([1.92, 0, -1.95], [3.58, 1.5, -0.35]);
const CALIPER_ZONE = around([4.25, 0, -0.5], [6.3, 1.25, 0.75]);
const LANES_ZONE = around([0.4, 0, 1.05], [4.9, 0.42, 2.7]);
const INTRO_BENCH = around([-1.15, 0, -1.6], [3.85, 1.72, 0.55]);
const ALL_STATIONS = around([-0.75, 0, -1.7], [6.05, 1.72, 0.65]);

export const SHOTS = {
  intro: {
    ...frame(INTRO_BENCH, 'wide', { azimuth: 22, elevation: 19, fov: 32, fill: 0.94, push: 0.12, orbit: 7 }),
    fill: 0.52,
    span: 12,
    subject: ['capture_press', 'artifact_vault'],
  },
  flood: {
    ...frame(FLOOD_ZONE, 'low', { azimuth: -44, elevation: 7, fov: 40, fill: 0.72 }),
    fill: 0.76,
    span: 10,
    via: { pos: [-1.8, 2.3, 4.8], target: [-1.2, 0.7, 0] },
    subject: ['log_stream', 'capture_press'],
  },
  digest: {
    ...frame(DIGEST_ZONE, 'medium', { azimuth: 36, elevation: 23, fov: 30, fill: 0.68 }),
    fill: 0.76,
    span: 7,
    via: { pos: [0.3, 2.5, 4.3], target: [0.35, 0.78, 0] },
    subject: ['capture_press', 'digest_output'],
  },
  retrieve: {
    ...frame(VAULT_ZONE, 'close', { azimuth: -20, elevation: 24, fov: 22, fill: 0.76 }),
    fill: 0.86,
    span: 6,
    via: { pos: [1.9, 2.2, 1.9], target: [2.45, 0.78, -1.05] },
    subject: ['artifact_vault', 'vault_excerpts'],
  },
  anchors: {
    ...frame(CALIPER_ZONE, 'medium', { azimuth: 18, elevation: 21, fov: 28, fill: 0.66 }),
    fill: 0.72,
    span: 6,
    via: { pos: [5.4, 2.5, 3.2], target: [4.9, 0.65, -0.2] },
    subject: ['anchor_caliper', 'anchor_spans'],
  },
  harnesses: {
    ...frame(LANES_ZONE, 'top', { azimuth: 4, elevation: 58, fov: 28, fill: 0.86, orbit: 8 }),
    fill: 0.84,
    span: 10,
    via: { pos: [4.2, 4.9, 3.8], target: [3.2, 0.3, 1.4] },
    subject: ['host_lanes'],
  },
  start: {
    ...frame(ALL_STATIONS, 'wide', { azimuth: 16, elevation: 12, fov: 28, fill: 1.12, push: 0.08, orbit: 8 }),
    fill: 0.56,
    span: 14,
    via: { pos: [3.4, 2.8, 6.2], target: [2.65, 0.78, -0.5] },
    subject: ['capture_press', 'artifact_vault', 'anchor_caliper'],
  },
};

export const SHOW = {
  capture_press: ['intro', 'flood', 'digest', 'retrieve', 'anchors', 'harnesses', 'start'],
  artifact_vault: ['intro', 'flood', 'digest', 'retrieve', 'anchors', 'harnesses', 'start'],
  anchor_caliper: ['intro', 'flood', 'digest', 'retrieve', 'anchors', 'harnesses', 'start'],
};

export const LAYERS = ['log_stream', 'digest_output', 'vault_excerpts', 'anchor_spans', 'host_lanes'];

const DEFAULT_PUSH = 0.12, DEFAULT_ORBIT = 6;
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
    return (2 * t3 - 3 * t2 + 1) * ys[i]
      + (t3 - 2 * t2 + t) * h * m[i]
      + (-2 * t3 + 3 * t2) * ys[i + 1]
      + (t3 - t2) * h * m[i + 1];
  };
}

const held = (s) => {
  const k = 1 - (s.push ?? DEFAULT_PUSH), a = ((s.orbit ?? DEFAULT_ORBIT) * Math.PI) / 180;
  const dx = s.pos[0] - s.target[0], dy = s.pos[1] - s.target[1], dz = s.pos[2] - s.target[2];
  const c = Math.cos(a), sn = Math.sin(a);
  return {
    ...s,
    pos: [s.target[0] + k * (dx * c + dz * sn), s.target[1] + k * dy, s.target[2] + k * (dz * c - dx * sn)],
  };
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
  const k = c.shot(T), lo = Math.floor(k + 1e-9), hi = Math.min(n - 1, Math.ceil(k - 1e-9));
  const about = (j) => {
    const s = SHOTS[known[j]];
    return {
      names: s.subject ? [].concat(s.subject) : Object.keys(SHOW).filter((name) => SHOW[name].includes(known[j])),
      pose: { pos: s.pos, target: s.target, fov: s.fov },
      fill: s.fill ?? null,
    };
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
  };
}

export const restingT = (T, n) => Math.min(Math.floor(T), Math.max(0, n - 1)) + REST;
