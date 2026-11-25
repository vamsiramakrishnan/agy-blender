// shots.js: pure function of story time T for Straitjacket's six-chapter visual journey.
// Every neighbouring chapter differs in shot scale (wide -> low -> medium -> close -> top -> low wide group)
// and by 44-86 degrees in viewing angle.

export const MOVE = 0.35;
export const REST = 0.7;

export const DESIGN_ASPECT = 1.6;
export const DESIGN_FREE = 0.6;

export const SHOT = {
  wide: { fill: 0.42, elevation: 16, fov: 34, push: 0.1, orbit: 8 },
  medium: { fill: 0.62, elevation: 22, fov: 30, push: 0.12, orbit: 7 },
  close: { fill: 0.76, elevation: 28, fov: 22, push: 0.08, orbit: 6 },
  top: { fill: 0.68, elevation: 72, fov: 30, push: 0.1, orbit: 12 },
  low: { fill: 0.62, elevation: 6, fov: 38, push: 0.08, orbit: 14 },
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

// World bounding volumes matching the exact installed prop bounds on the tightened bench.
const LINE = around([-0.92, 0.0, -0.64], [6.57, 1.84, 0.86]);
const FLOOD_ZONE = around([-2.12, 0.0, -0.55], [1.54, 1.84, 0.64]);
const VAULT_ZONE = around([2.06, 0.0, -0.54], [3.96, 1.56, 0.86]);
const ANCHOR_ZONE = around([3.84, 0.10, 0.66], [4.52, 1.24, 0.88]);
const MANIFOLD_ZONE = around([4.46, 0.0, -0.64], [6.60, 1.12, 0.64]);

export const SHOTS = {
  // 01: Bold oblique establishing panorama down the full three-station Straitjacket bench
  intro: {
    ...frame(LINE, 'wide', { azimuth: -54, elevation: 15, fov: 34, fill: 1.18, push: 0.10, orbit: 7 }),
    fill: 0.78,
    span: 13,
    subject: ['containment_press', 'evidence_vault', 'anchor_manifold'],
  },
  // 02: Dramatic low angle sweeping from the red intake flood across to the bounded blue digest & amber needle
  flood: {
    ...frame(FLOOD_ZONE, 'low', { azimuth: -36, elevation: 8, fov: 36, fill: 0.78, push: 0.09, orbit: 18 }),
    fill: 0.84,
    span: 7.5,
    via: { pos: [-3.4, 2.2, 5.2], target: [0.3, 0.85, 0.0] },
    subject: ['containment_press', 'token_stream'],
  },
  // 03: Unobstructed medium 3/4 view on the open-drawer Evidence Vault, overhead gantry & blue prompt prefix
  vault: {
    ...frame(VAULT_ZONE, 'medium', { azimuth: 14, elevation: 22, fov: 30, fill: 0.70, push: 0.10, orbit: 7 }),
    fill: 0.56,
    span: 6.0,
    via: { pos: [1.6, 2.0, 4.2], target: [1.7, 0.8, 0.1] },
    subject: ['evidence_vault', 'vault_slice'],
  },
  // 04: Tight telephoto macro close-up of the Anchor Caliper tracking relocated lines vs. unanchored red drift
  anchors: {
    ...frame(ANCHOR_ZONE, 'close', { azimuth: -26, elevation: 14, fov: 20, fill: 0.92, push: 0.07, orbit: 5 }),
    fill: 0.88,
    span: 4.5,
    via: { pos: [3.3, 1.45, 3.0], target: [3.85, 0.70, 0.55] },
    subject: ['anchor_track'],
  },
  // 05: High top-down plan view across all seven parallel harness lanes and Birth/Output gates
  harnesses: {
    ...frame(MANIFOLD_ZONE, 'top', { azimuth: 8, elevation: 74, fov: 30, fill: 0.76, push: 0.08, orbit: 10 }),
    fill: 0.64,
    span: 6.0,
    via: { pos: [4.9, 3.4, 2.3], target: [5.45, 0.65, 0.05] },
    subject: ['anchor_manifold', 'host_lanes'],
  },
  // 06: Strongest last picture — bold low right-to-left hero panorama framing all three lighted stations
  start: {
    ...frame(LINE, 'wide', { azimuth: 54, elevation: 8, fov: 34, fill: 1.22, push: 0.08, orbit: 8 }),
    fill: 0.80,
    span: 13,
    via: { pos: [8.2, 2.4, 5.2], target: [3.6, 0.82, 0.0] },
    subject: ['containment_press', 'evidence_vault', 'anchor_manifold'],
  },
};

// All three stations stay grounded on the unified bench throughout the journey (zero popping or empty frames).
const ALL = ['intro', 'flood', 'vault', 'anchors', 'harnesses', 'start'];
export const SHOW = {
  containment_press: ALL,
  evidence_vault: ALL,
  anchor_manifold: ALL,
};

export const LAYERS = ['token_stream', 'vault_slice', 'anchor_track', 'host_lanes'];

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
