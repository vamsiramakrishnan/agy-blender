// world.js: the fixed geography of the stage (metres; Y up; the evidence path runs along +X; the reader stands at +Z).
// Plain numbers only; shared by the line system, the props and the labels.

export const FX = 2.4;                       // context frame (turn 1) centre x
export const TURN = 3.3;                     // spacing of the turns 2..7
export const SX = -1.4, SZ = -3.6;           // evidence store centre
export const CONSOLE = [-5.0, 0, 0];
export const OUTLET = [-4.42, 1.30, 0];
export const GATE = [-1.6, 1.30, 0];
export const LEDGER = [4.05, 0, 1.7];
export const SOURCE = [-9.0, 0, -0.5];

// Line slabs.
export const SLAB_H = 0.0105, SLAB_D = 0.058;
export const slabLen = (chars) => Math.min(chars * 0.0019, 0.094);

// Frame cells: 25 columns x 100 rows x 8 layers.
export const F_COLS = 25, F_ROWS = 100, F_LAYERS = 8;
export const F_PITCH = [0.1, 0.0155, 0.07];
export const F_Y0 = 0.195 + 0.00775;
export const frameCell = (col, row, layer, len) => [
  FX + (col - (F_COLS - 1) / 2) * F_PITCH[0] - 0.047 + len / 2,
  F_Y0 + row * F_PITCH[1],
  (layer - (F_LAYERS - 1) / 2) * F_PITCH[2],
];

// Store cells: bays k = 0..7, 4 columns x 110 rows x 6 layers.
export const bayX = (k) => SX - 1.95 + 0.46 * k;
export const NINTH_X = SX + 1.84;
export const S_COLS = 4, S_ROWS = 110, S_LAYERS = 6;
export const S_Y0 = 0.16 + 0.00775;
export const SHELVES = [0.45, 0.95, 1.45];   // tops of the ninth bay's shelves (line 5000, the needle, line 17650)
export const storeCell = (k, col, row, layer, len) => [
  bayX(k) + (col - (S_COLS - 1) / 2) * F_PITCH[0] - 0.047 + len / 2,
  S_Y0 + row * F_PITCH[1],
  SZ + (layer - (S_LAYERS - 1) / 2) * F_PITCH[2],
];
export const shelfCell = (s, len) => [NINTH_X - 0.12 + len / 2, SHELVES[s] + SLAB_H / 2, SZ + 0.21 - SLAB_D / 2 - 0.004];

// The needle's front cell in the frame (row 71 by log order; column chosen so it reads in the three-quarter view).
export const NEEDLE_INDEX = 14237, LOUD = [4999, 17649];
export const NEEDLE_COL = 15;

// Digest page in a frame: front, low left (relative to the frame centre x).
export const PAGE = { x0: -1.2, y0: 0.215, z: 0.245, pitch: 0.0155, perChar: 0.0019 };

// Seeded random (mulberry32).
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0; let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
