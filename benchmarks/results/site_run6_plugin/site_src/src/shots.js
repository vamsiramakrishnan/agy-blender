// shots.js: Straitjacket's camera choreography as a pure function of story time T.
// Every shot differs from its neighbours in scale or >= 30 degrees of angle, and no chapter lands on empty floor.

export const MOVE = 0.35;
export const OPENING = 24;
export const REST = 0.7;

export const DESIGN_ASPECT = 1.6;
export const DESIGN_FREE = 0.6;

export const SHOT = {
  wide: { fill: 0.42, elevation: 16, fov: 34, push: 0.1, orbit: 8 },
  medium: { fill: 0.62, elevation: 22, fov: 30, push: 0.12, orbit: 7 },
  close: { fill: 0.78, elevation: 24, fov: 24, push: 0.1, orbit: 6 },
  top: { fill: 0.7, elevation: 68, fov: 30, push: 0.1, orbit: 12 },
  low: { fill: 0.62, elevation: 7, fov: 40, push: 0.08, orbit: 14 },
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

// World-space bounds of the three props and key active zones:
const PRESS = around([-0.95, 0, -0.7], [0.95, 1.5, 0.7]);
const PRESS_AND_FLOOD = around([-0.95, 0, -0.75], [3.65, 1.5, 0.75]);
const FLOOD_INTAKE = around([0.1, 0, -0.68], [3.65, 1.35, 0.68]);
const PRESS_AND_EXIT = around([-2.15, 0, -0.7], [0.95, 1.5, 0.7]);
const ANCHOR = around([-4.25, 0, -1.1], [-2.55, 1.66, 0.45]);
const HOSTS = around([-3.0, 0, -4.15], [-0.4, 1.2, -2.65]);
const ALL_THREE = around([-4.25, 0, -4.15], [1.1, 1.66, 0.75]);

export const SHOTS = {
  // 01: Establishing wide shot of the press & incoming flood stack (opening swings 24 deg on first wheel notch)
  intro: {
    ...frame(PRESS_AND_FLOOD, 'wide', { azimuth: 26, elevation: 16, fill: 0.56 }),
    span: 13,
    subject: ['digest_press', 'flood'],
  },
  // 02: Dramatic low angle looking along the towering 6,053-slab flood pressing into the open intake rollers
  flood: {
    ...frame(FLOOD_INTAKE, 'low', { azimuth: 66, elevation: 8, fill: 0.68 }),
    span: 9,
    subject: ['flood', 'digest_press'],
  },
  // 03: Medium exit-side shot into the open press platen & 11-slab blue+amber digest emerging on the rail
  digest: {
    ...frame(PRESS_AND_EXIT, 'medium', { azimuth: -36, elevation: 23, fill: 0.70 }),
    span: 8,
    via: { pos: [1.5, 3.2, 5.2], target: [-0.4, 0.7, 0] },
    subject: ['digest_press', 'needle'],
  },
  // 04: Close shot into the open Content Anchor Rack: open drawer + shifting code spans + amber caliper
  anchors: {
    ...frame(ANCHOR, 'close', { azimuth: 22, elevation: 18, fill: 0.80 }),
    span: 6,
    via: { pos: [-2.6, 2.3, 3.8], target: [-2.6, 0.75, -0.25] },
    subject: ['anchor_rack', 'anchors'],
  },
  // 05: Top-down analytical view of all seven host lanes and their birth/output gate bridges
  hosts: {
    ...frame(HOSTS, 'top', { azimuth: 6, elevation: 62, fill: 0.78 }),
    span: 8,
    via: { pos: [-3.2, 4.4, -0.4], target: [-2.2, 0.55, -2.2] },
    subject: ['host_manifold', 'lanes'],
  },
  // 06: Monumental low group finale framing all three instruments together against the dark horizon
  start: {
    ...frame(ALL_THREE, 'wide', { azimuth: 18, elevation: 11, fill: 0.86, push: 0.08, orbit: 10 }),
    span: 15,
    via: { pos: [0.5, 4.2, 3.4], target: [-1.7, 0.7, -1.6] },
    subject: ['digest_press', 'anchor_rack', 'host_manifold'],
  },
};

// All three instruments stand together as one architectural bench from the very first frame through the finale.
export const SHOW = {
  digest_press: ['intro', 'flood', 'digest', 'anchors', 'hosts', 'start'],
  anchor_rack: ['intro', 'flood', 'digest', 'anchors', 'hosts', 'start'],
  host_manifold: ['intro', 'flood', 'digest', 'anchors', 'hosts', 'start'],
};

export const LAYERS = ['flood', 'needle', 'anchors', 'lanes'];

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

const held = (s) => {
  const k = 1 - (s.push ?? DEFAULT_PUSH), a = ((s.orbit ?? DEFAULT_ORBIT) * Math.PI) / 180;
  const dx = s.pos[0] - s.target[0], dy = s.pos[1] - s.target[1], dz = s.pos[2] - s.target[2];
  const c = Math.cos(a), sn = Math.sin(a);
  return { ...s, pos: [s.target[0] + k * (dx * c + dz * sn), s.target[1] + k * dy, s.target[2] + k * (dz * c - dx * sn)] };
};

function keysFor(ids) {
  const keys = [];
  ids.forEach((id, i) => {
    const s = SHOTS[id];
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
    moving: k < index - 1e-9,
  };
}

export const restingT = (T, n) => Math.min(Math.floor(T), Math.max(0, n - 1)) + REST;
