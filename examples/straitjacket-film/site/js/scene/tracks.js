// tracks.js: the whole film as a pure function of film time T. No three.js here: the page and tests call it alone.
//
// evaluate(T) -> { cam: { pos, target, fov }, focus: { at, span }, p: { named parameters 0..1 },
//                  labels: { id: opacity 0..1 }, readout: { value, note, tone } }
//
// Everything is data: camera keys, parameter ramps and label windows below. Tune by editing the tables.

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
// Smootherstep ramp from a to b (0 before a, 1 after b).
export const ramp = (T, a, b) => { const t = clamp01((T - a) / (b - a)); return t * t * t * (t * (t * 6 - 15) + 10); };
// Smoothstep ramp, a gentler shoulder (used for flights, where the window already softens the start).
const sramp = (T, a, b) => { const t = clamp01((T - a) / (b - a)); return t * t * (3 - 2 * t); };
// A window: rises over [a, a+f], holds, falls over [b-f, b].
const win = (T, a, b, f = 0.06) => ramp(T, a, a + f) * (1 - ramp(T, b - f, b));

// ---------------------------------------------------------------------------------------------------------------
// Camera keys: [T, position, target, vertical fov, shadow span (metres across the key light's shadow box)].
// Interpolated per component with monotone cubic Hermite curves: C1-smooth, no overshoot, holds are real holds.
const FX = 2.4;
const CAM = [
  [0.00, [0.9, 3.55, 11.6], [-0.9, 0.85, -1.2], 30, 15],
  [0.80, [0.5, 3.35, 11.0], [-0.9, 0.85, -1.2], 30, 15],
  [1.10, [-2.55, 2.05, 4.35], [-3.7, 1.15, -0.1], 32, 9],      // at the console as the command runs
  [1.45, [-0.6, 2.0, 4.6], [-0.3, 1.05, -0.3], 32, 10],        // along the stream
  [1.85, [5.7, 1.85, 4.9], [2.25, 0.95, -0.1], 31, 9],         // three-quarter on the full frame
  [2.05, [5.85, 1.85, 4.75], [2.25, 0.95, -0.1], 31, 9],
  [2.40, [0.15, 1.55, 2.55], [12.0, 0.85, -0.9], 34, 16],      // down the corridor of turns
  [2.88, [0.35, 1.5, 2.45], [12.0, 0.85, -0.9], 34, 16],
  [3.15, [FX + 0.25, 1.15, 5.0], [FX + 0.1, 0.8, 0.0], 30, 8],  // frontal on turn 1
  [3.95, [FX + 0.2, 1.15, 4.85], [FX + 0.1, 0.8, 0.0], 30, 8],
  [4.45, [1.7, 3.6, 8.3], [-0.6, 0.85, -1.6], 31, 13],          // back and up: the store comes in
  [4.90, [0.9, 3.4, 7.7], [-1.2, 0.95, -1.4], 31, 13],
  [5.20, [-1.2, 2.25, 4.0], [-2.4, 1.15, -0.9], 32, 10],        // console and gate
  [5.65, [-0.1, 2.15, 2.65], [-1.25, 1.0, -3.55], 32, 9],       // facing the store
  [6.02, [0.0, 2.1, 2.55], [-1.25, 1.0, -3.55], 32, 9],
  [6.45, [2.05, 0.82, 1.75], [1.48, 0.48, 0.15], 30, 6],        // close on the digest page
  [6.92, [2.1, 0.82, 1.68], [1.48, 0.48, 0.15], 30, 6],
  [7.25, [4.6, 2.2, 3.4], [0.6, 0.8, -1.7], 31, 11],            // frame in front, store behind, the thread between
  [7.92, [4.45, 2.15, 3.3], [0.6, 0.8, -1.7], 31, 11],
  [8.35, [0.15, 1.55, 2.55], [12.0, 0.85, -0.9], 34, 16],       // the corridor again
  [8.88, [0.35, 1.5, 2.45], [12.0, 0.85, -0.9], 34, 16],
  [9.20, [-8.05, 1.35, 2.75], [-9.0, 1.05, -0.5], 32, 6],       // the source file
  [9.92, [-8.1, 1.3, 2.65], [-9.0, 1.05, -0.5], 32, 6],
  [10.25, [6.3, 3.9, 9.6], [3.3, 0.7, -0.8], 31, 16],           // the handoff: workers, ledger, store
  [10.95, [6.0, 3.8, 9.3], [3.3, 0.7, -0.8], 31, 16],
  [11.35, [-0.4, 16.5, 3.0], [-0.4, 0.0, -0.9], 30, 15],        // plan view of the whole path
  [11.95, [-0.4, 16.5, 3.0], [-0.4, 0.0, -0.9], 30, 15],
  [12.45, [1.0, 3.6, 11.4], [-0.6, 0.85, -1.2], 30, 15],        // rest
  [14.00, [0.7, 3.5, 11.2], [-0.6, 0.85, -1.2], 30, 15],
];

// Monotone cubic Hermite (Fritsch-Carlson) over one scalar channel.
function monotone(ts, ys) {
  const n = ts.length, d = [], m = new Array(n).fill(0);
  for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / (ts[i + 1] - ts[i]));
  m[0] = 0; m[n - 1] = 0;
  for (let i = 1; i < n - 1; i++) {
    if (d[i - 1] * d[i] <= 0) { m[i] = 0; continue; }
    const w1 = 2 * (ts[i + 1] - ts[i]) + (ts[i] - ts[i - 1]), w2 = (ts[i + 1] - ts[i]) + 2 * (ts[i] - ts[i - 1]);
    m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
  }
  return (T) => {
    if (T <= ts[0]) return ys[0];
    if (T >= ts[n - 1]) return ys[n - 1];
    let i = 0; while (T > ts[i + 1]) i++;
    const h = ts[i + 1] - ts[i], t = (T - ts[i]) / h, t2 = t * t, t3 = t2 * t;
    return (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * m[i] + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * m[i + 1];
  };
}
const camT = CAM.map((k) => k[0]);
const chan = (f) => monotone(camT, CAM.map(f));
const POS = [0, 1, 2].map((j) => chan((k) => k[1][j]));
const TGT = [0, 1, 2].map((j) => chan((k) => k[2][j]));
const FOV = chan((k) => k[3]);
const SPAN = chan((k) => k[4]);

// ---------------------------------------------------------------------------------------------------------------
// Label windows: id -> list of [from, to] in film time.
const LABELS = {
  cmd: [[0.92, 1.62]],
  ctxcmd: [[4.9, 5.55]],
  context: [[0.1, 0.92], [1.62, 2.06], [3.02, 3.14], [3.96, 4.6], [12.4, 14]],
  turns: [[2.3, 2.92], [8.3, 8.92]],
  needle: [[1.7, 2.06], [3.4, 3.8], [7.62, 7.98], [10.15, 10.98]],
  families: [[5.62, 6.12]],
  run: [[5.75, 6.12], [7.2, 7.98], [12.4, 14]],
  source: [[9.12, 9.96]],
  workers: [[10.15, 10.98]],
  gates: [[11.3, 12.15]],
};

// ---------------------------------------------------------------------------------------------------------------
const fmt = (n) => Math.round(n).toLocaleString('en-US');
export const W = 0.1;            // flight window: about a tenth of the lines are in the air at once
const TOKENS = 302628;

export function evaluate(T) {
  T = Math.max(0, Math.min(14, Number(T) || 0));
  const cam = { pos: POS.map((f) => f(T)), target: TGT.map((f) => f(T)), fov: FOV(T) };
  const focus = { at: [cam.target[0], 0, cam.target[2]], span: SPAN(T) };

  const flood = sramp(T, 1.08, 1.85) * (1 - sramp(T, 4.1, 4.7));
  const corridor = ramp(T, 2.04, 2.42) * (1 - ramp(T, 2.9, 3.04));
  const contained = ramp(T, 8.04, 8.42) * (1 - ramp(T, 8.9, 9.04));
  const p = {
    flood,
    corridor,
    contained,
    trunc: sramp(T, 3.15, 3.6) * (1 - sramp(T, 3.8, 3.95)),
    armed: ramp(T, 4.55, 4.85),
    capture: sramp(T, 5.05, 5.85),
    digest: ramp(T, 6.1, 6.6),
    thread: ramp(T, 7.05, 7.35) * (1 - ramp(T, 7.94, 8.04)),
    retrieve: ramp(T, 7.35, 7.8),
    copyA: ramp(T, 7.35, 7.4) * (1 - ramp(T, 7.98, 8.04) * (1 - ramp(T, 9.96, 10.04))),
    source: ramp(T, 8.92, 9.06) * (1 - ramp(T, 9.96, 10.1)),
    insert: ramp(T, 9.12, 9.42),
    resolve: ramp(T, 9.5, 9.78),
    workers: ramp(T, 10.0, 10.14) * (1 - ramp(T, 10.94, 11.08)),
    ledger: ramp(T, 9.98, 10.12),
    handoff: clamp01((T - 10.14) / (10.9 - 10.14)),
    plan: ramp(T, 11.12, 11.82),
    gates: clamp01((T - 11.2) / (11.85 - 11.2)),
    drift: 1 - ramp(T, 0.85, 1.05) + ramp(T, 12.3, 12.6),
  };

  const labels = {};
  for (const [id, ws] of Object.entries(LABELS)) labels[id] = Math.max(...ws.map(([a, b]) => win(T, a, b)));

  // HUD readout ("Visible tokens"): agrees with the picture.
  let readout;
  const landed = clamp01((flood - W) / (1 - W));
  if (T < 4) {
    if (T < 1.08 || flood <= 0) readout = { value: '0', note: 'nothing has run yet', tone: 'idle' };
    else if (T < 2) readout = { value: fmt(TOKENS * landed), note: 'resident in the prompt', tone: 'loss' };
    else if (T < 3) readout = corridor < 0.999 || T < 2.55
      ? { value: '302,628 × 6', note: 'token-turns, modelled', tone: 'loss' }
      : { value: '1,815,768', note: 'token-turns, modelled', tone: 'loss' };
    else if (p.trunc > 0.5) readout = { value: '1,219', note: 'the quiet line is lost', tone: 'loss' };
    else readout = { value: '302,628', note: 'resident in the prompt', tone: 'loss' };
  } else if (T < 5) {
    const v = TOKENS * landed;
    readout = { value: fmt(v), note: 'the same command, about to run through ctx', tone: v > 0 ? 'loss' : 'idle' };
  } else if (T < 6) readout = { value: '0', note: '20,001 lines stored, none in the prompt', tone: 'bound' };
  else if (T < 7) readout = p.digest < 0.5
    ? { value: '0', note: '20,001 lines stored, none in the prompt', tone: 'bound' }
    : { value: '531', note: 'the digest', tone: 'bound' };
  else if (T < 8) readout = p.retrieve < 0.5
    ? { value: '531', note: 'the digest', tone: 'bound' }
    : { value: '531 + 1 line', note: 'fetched by address', tone: 'ok' };
  else if (T < 9) readout = { value: '531', note: 'per turn; 21 lines fetched at turn 4', tone: 'bound' };
  else if (T < 10) readout = p.resolve < 0.5
    ? { value: '--lines 40:52@07407f1c', note: 'verified', tone: 'ok' }
    : { value: 'moved → L46:58', note: 'verified', tone: 'ok' };
  else if (T < 11) readout = { value: '1 address', note: 'handed to the next worker', tone: 'bound' };
  else if (T < 12) readout = { value: '4 gates', note: 'one store', tone: 'bound' };
  else readout = { value: '531', note: 'of 302,628', tone: 'bound' };

  return { cam, focus, p, labels, readout };
}
