// extras.js: everything in the film that is not the 20,001 lines or a prop, as a pure function of the parameters:
// rail, the corridor's resident blocks, the digest pages, the fetched copies of the needle, the threads, tags, the
// ledger's plates, the source file of the anchors chapter, the gate plan's markers and floor path, the gate's lamp.
import * as THREE from '../../vendor/three.module.min.js';
import { createBits } from './bits.js';
import {
  FX, TURN, SX, SZ, OUTLET, GATE, LEDGER, SOURCE, SLAB_H, SLAB_D, F_ROWS, F_Y0, F_PITCH, PAGE, NINTH_X, SHELVES, rng,
} from './world.js';

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const ease = (t) => { t = clamp01(t); return t * t * (3 - 2 * t); };
const easeOut = (t) => { t = clamp01(t); return 1 - (1 - t) * (1 - t) * (1 - t); };
const lerp = (a, b, t) => a + (b - a) * t;
const bez = (a, b, c, d, t) => { const s = 1 - t; return [0, 1, 2].map((i) => s * s * s * a[i] + 3 * s * s * t * b[i] + 3 * s * t * t * c[i] + t * t * t * d[i]); };

// Reveal of frame k (1..6) for a corridor parameter p: staggered so the row rises from near to far.
export const revealK = (p, k) => ease((p - (k - 1) * 0.08) / 0.6);
export const FRAME_DROP = 2.05;   // how far below the table a hidden frame waits

export function createExtras({ data, lines, palette }) {
  const bits = createBits(1400);
  const lamp = createBits(4, { basic: true, shadows: false });
  const C = {};
  const derived = () => {
    C.resident = C.faint.clone().lerp(C.red, 0.65);
    C.residentDeep = C.resident.clone().multiplyScalar(0.62);
    C.grid2 = C.grid.clone();
    C.prop = new THREE.Color(C.bg.getHSL({}).l > 0.5 ? C.surface2 : new THREE.Color('#2B3038'));
  };
  const setPalette = (pal) => {
    for (const [k, v] of Object.entries(pal)) { if (!C[k]) C[k] = new THREE.Color(); C[k].set(v); }
    derived();
    threadMat.color.copy(C.blue);
  };

  // Digest page: one slab per digest line, width from its real length, the line that quotes L14238 in amber.
  const digestLines = data.digest.text.split('\n').filter((s) => s.length);
  const pageLines = digestLines.map((s, j) => ({ len: Math.max(s.length, 3) * PAGE.perChar, amber: /^\s*L14238:/.test(s), j }));
  const PAGE_N = pageLines.length;
  const pageW = Math.max(...pageLines.map((l) => l.len));
  const amberRow = pageLines.findIndex((l) => l.amber);
  const pageY = (j) => PAGE.y0 + (PAGE_N - 1 - j) * PAGE.pitch;
  // Where the fetched copy of the needle settles: beside the page, level with the amber digest line.
  const copyAt = (fx) => [fx + PAGE.x0 + pageW + 0.07 + lines.needleLen / 2, pageY(amberRow), PAGE.z];
  const needleShelf = lines.at(14237, 'store');

  // Threads: thin blue tubes along cubic Beziers, drawn progressively; the copies travel along the same curves.
  const curve = (from, to, lift, pull) => new THREE.CubicBezierCurve3(
    new THREE.Vector3(...from), new THREE.Vector3(from[0] + pull[0], from[1] + lift, from[2] + pull[1]),
    new THREE.Vector3(to[0] + pull[2], to[1] + lift, to[2] + pull[3]), new THREE.Vector3(...to));
  const threadMat = new THREE.MeshStandardMaterial({ roughness: 0.6, metalness: 0.0 });
  const shelfFront = [needleShelf[0] - lines.needleLen / 2 - 0.02, needleShelf[1] + 0.01, needleShelf[2] + 0.02];
  const curveA = curve([copyAt(FX)[0] - lines.needleLen / 2 - 0.02, copyAt(FX)[1], PAGE.z + 0.04], shelfFront, 0.95, [-0.3, -0.4, 0.5, 1.4]);
  const curveB = curve([copyAt(FX + TURN)[0] - lines.needleLen / 2 - 0.02, copyAt(FX + TURN)[1], PAGE.z + 0.04], shelfFront, 1.7, [-0.8, -0.6, 1.2, 1.6]);
  const tube = (c) => {
    const g = new THREE.TubeGeometry(c, 160, 0.0045, 6, false);
    const m = new THREE.Mesh(g, threadMat); m.castShadow = true; m.frustumCulled = false;
    m.userData.full = g.index.count; return m;
  };
  const threadA = tube(curveA), threadB = tube(curveB);
  const showThread = (m, p) => {
    m.visible = p > 0.001;
    const per = 6 * 6; m.geometry.setDrawRange(0, Math.round((m.userData.full / per) * p) * per);
  };

  // Source file (anchors): 64 lines of code-like lengths, indentation, one blank every so often.
  const src = [];
  { const r = rng(7407); let indent = 0;
    for (let i = 0; i < 64; i++) {
      if (r() < 0.18) indent = Math.max(0, Math.min(3, indent + (r() < 0.5 ? -1 : 1)));
      const blank = r() < 0.08 && i > 2;
      src.push({ indent: blank ? 0 : indent, len: blank ? 0 : 0.12 + r() * 0.5 });
    } }
  const inserted = [0, 1, 2, 3, 4, 5].map((k) => ({ indent: 1 + (k % 2), len: 0.2 + 0.07 * ((k * 5) % 4) }));
  const SRC = { left: SOURCE[0] - 0.48, top: 1.98, pitch: 0.0275, h: 0.0115, d: 0.024, z: SOURCE[2] + 0.04, insertAt: 20 };

  // The floor path of the gate plan: console -> gate -> store, store ⇢ frame (dashed), frame -> beyond.
  const P = (x, z) => [x, z];
  const STORE_FRONT = P(SX, SZ + 0.62);
  const path = [
    { a: P(OUTLET[0] + 0.02, 0), b: P(GATE[0], 0), dashed: false },
    { a: P(GATE[0], 0), b: STORE_FRONT, dashed: false },
    { a: STORE_FRONT, b: P(FX - 1.55, -0.55), dashed: true },
    { a: P(FX + 1.5, 0), b: P(FX + 2.35, 0), dashed: true },
  ];
  path.forEach((s) => { s.len = Math.hypot(s.b[0] - s.a[0], s.b[1] - s.a[1]); });
  const pathLen = path.reduce((a, s) => a + s.len, 0);
  const MARKERS = [
    { id: 'birth', at: [-3.0, 0.0] },
    { id: 'entry', at: [FX - 1.55, -0.55] },
    { id: 'residence', at: [FX, 0.72] },
    { id: 'emission', at: [FX + 2.35, 0] },
  ];

  // ---------------------------------------------------------------------------------------------------------------
  function update(s, T, needleFloor) {
    const p = s.p;
    bits.begin();
    const b = bits;

    // Rail: two thin bars from the outlet to the gate, one slim stanchion with a cross-arm.
    for (const z of [-0.17, 0.17]) b.span(OUTLET[0], GATE[0] - 0.06, 1.293, 1.307, z - 0.007, z + 0.007, C.prop);
    b.span(-3.02, -2.98, 0, 1.285, -0.22, -0.18, C.prop);
    b.span(-3.03, -2.97, 1.275, 1.29, -0.22, 0.2, C.prop);
    b.span(-3.12, -2.88, 0, 0.02, -0.28, -0.12, C.prop);

    // Corridor: turns 2..7 each hold the same resident block (row plates read as the stacked lines).
    if (p.corridor > 0) for (let k = 1; k < 7; k++) {
      const r = revealK(p.corridor, k); if (r <= 0) continue;
      const x = FX + TURN * k, dy = -(1 - r) * FRAME_DROP;
      for (let row = 0; row < F_ROWS; row++) {
        const y = F_Y0 + row * F_PITCH[1] + dy;
        if (y < -0.05) continue;
        b.box(x - 0.003, y, 0.0, 2.494, SLAB_H, 0.548, row % 2 ? C.resident : C.resident);
      }
    }

    // Digest page in turn 1: rises out of the store and settles in the frame, line by line.
    const pageAt = (fx, dy, t0, tFly, scale = 1) => {
      // backing sheet
      const pt = clamp01(tFly);
      for (const l of pageLines) {
        const j = l.j;
        const u = easeOut((pt - (j / PAGE_N) * 0.35) / 0.65);
        if (u <= 0) continue;
        const end = [fx + PAGE.x0 + l.len / 2, pageY(j) + dy, PAGE.z];
        let pos = end;
        if (u < 1) {
          const start = [SX + 0.4 + l.len / 2, 1.98 + (PAGE_N - j) * 0.004, SZ + 0.05];
          pos = bez(start, [start[0] + 0.3, 2.9, SZ + 0.6], [end[0] - 0.2, end[1] + 1.4, end[2] + 1.2], end, u);
        }
        const sc = Math.min(1, u * 4) * scale;
        b.box(pos[0], pos[1], pos[2], l.len * sc, SLAB_H * sc, SLAB_D * sc, l.amber ? C.amber : C.blue);
      }
      if (pt >= 1) b.span(fx + PAGE.x0 - 0.02, fx + PAGE.x0 + pageW + 0.02, PAGE.y0 - 0.02 + dy, pageY(0) + 0.02 + dy, PAGE.z - SLAB_D / 2 - 0.012, PAGE.z - SLAB_D / 2 - 0.004, C.blue2);
    };
    if (p.digest > 0) pageAt(FX, 0, 0, p.digest);
    // Pages in turns 2..7 for the contained corridor; the fetched lines at turn 4.
    if (p.contained > 0) for (let k = 1; k < 7; k++) {
      const r = revealK(p.contained, k); if (r <= 0) continue;
      const dy = -(1 - r) * FRAME_DROP;
      pageAt(FX + TURN * k, dy, 0, 1);
      if (k === 3) { const c = copyAt(FX + TURN * k); b.box(c[0], c[1] + dy, c[2], lines.needleLen, SLAB_H, SLAB_D, C.amber); }
    }

    // The fetched copy of the needle: from its shelf along the thread into turn 1, beside the page.
    showThread(threadA, p.thread);
    if (p.copyA > 0) {
      const e = ease(p.retrieve);
      const c = e < 1 ? curveA.getPoint(1 - e) : null;
      const at = c ? [c.x + lines.needleLen / 2 + 0.02, c.y, c.z - 0.04] : copyAt(FX);
      if (c && e > 0.92) { const f = (e - 0.92) / 0.08, d = copyAt(FX); for (let i = 0; i < 3; i++) at[i] = lerp(at[i], d[i], ease(f)); }
      b.box(at[0], at[1], at[2], lines.needleLen, SLAB_H, SLAB_D, C.amber);
    }

    // Truncation: the quiet line on the floor, outlined in red.
    if (needleFloor) {
      const o = ease((p.trunc - 0.62) / 0.2);
      if (o > 0) {
        const [x, , z] = needleFloor, hw = lines.needleLen / 2 + 0.03, hd = SLAB_D / 2 + 0.03, t = 0.007, h = 0.004;
        const gx = hw * o, gz = hd * o;
        b.box(x, h / 2, z + hd, 2 * gx, h, t, C.red); b.box(x, h / 2, z - hd, 2 * gx, h, t, C.red);
        b.box(x + hw, h / 2, z, t, h, 2 * gz, C.red); b.box(x - hw, h / 2, z, t, h, 2 * gz, C.red);
      }
    }

    // The run's tag on the store: an amber plate on the top front edge.
    { const o = ease((T - 5.72) / 0.18);
      if (o > 0) b.span(SX - 2.2, SX - 2.2 + 0.5 * o, 1.92, 1.965, SZ + 0.27, SZ + 0.285, C.amber); }

    // Ledger: three plates already there; in the handoff an amber tag leaves A, a plate is added, the tag reaches B.
    if (p.ledger > 0) {
      const dy = -(1 - p.ledger) * 1.2;
      const top = 1.06 + dy;
      for (let i = 0; i < 3; i++) b.span(LEDGER[0] - 0.2, LEDGER[0] + 0.2, top + i * 0.012, top + i * 0.012 + 0.008, LEDGER[2] - 0.15, LEDGER[2] + 0.13, C.faint);
      const h = p.handoff;
      const drop = ease((h - 0.3) / 0.14);
      if (h > 0.3) b.box(LEDGER[0], lerp(top + 0.36 + 0.04, top + 3 * 0.012 + 0.004, drop), LEDGER[2] - 0.01, 0.4, 0.008, 0.28, C.amber);
      // the tag (an address, not a payload)
      if (h > 0 && p.workers > 0) {
        const A = [FX + PAGE.x0 + 0.06, pageY(0) + 0.05, PAGE.z + 0.06], L = [LEDGER[0], top + 0.1, LEDGER[2]];
        const Bp = [FX + TURN + PAGE.x0 + 0.06, PAGE.y0 + 0.01, PAGE.z + 0.02];
        let at;
        if (h < 0.32) at = bez(A, [A[0], A[1] + 0.8, A[2] + 0.4], [L[0], L[1] + 0.8, L[2]], L, ease(h / 0.32));
        else if (h < 0.42) at = L;
        else at = bez(L, [L[0], L[1] + 0.8, L[2]], [Bp[0], Bp[1] + 0.9, Bp[2] + 0.3], Bp, ease((h - 0.42) / 0.24));
        b.box(at[0], at[1], at[2], 0.1, 0.012, 0.05, C.amber);
      }
    }
    // Handoff, B fetches by the address: a thread from B to the store, one amber slab arrives.
    showThread(threadB, p.workers * ease((p.handoff - 0.62) / 0.14));
    if (p.workers > 0 && p.handoff > 0.74) {
      const e = ease((p.handoff - 0.74) / 0.22);
      const c = curveB.getPoint(1 - e), d = copyAt(FX + TURN);
      const at = e < 1 ? [c.x + lines.needleLen / 2 + 0.02, c.y, c.z - 0.04] : d;
      b.box(at[0], at[1], at[2], lines.needleLen, SLAB_H, SLAB_D, C.amber);
    }

    // Source file (anchors): a slim stand and a column of line slabs; six inserted above the anchored range.
    if (p.source > 0) {
      const dy = -(1 - p.source) * 2.4;
      const ins = ease(p.insert), res = ease(p.resolve);
      const x0 = SRC.left, z = SRC.z;
      const slotY = (k) => SRC.top - k * SRC.pitch + dy;
      b.span(SOURCE[0] - 0.56, SOURCE[0] + 0.56, 0, 2.06 + dy, SOURCE[2] - 0.02, SOURCE[2] + 0.012, C.prop);
      b.span(SOURCE[0] - 0.3, SOURCE[0] + 0.3, 0, 0.03, SOURCE[2] - 0.2, SOURCE[2] + 0.2, C.prop);
      src.forEach((l, i) => {
        if (!l.len) return;
        const slot = i + (i >= SRC.insertAt ? 6 * ins : 0);
        const blue = i >= 39 && i <= 51;
        b.box(x0 + 0.06 * l.indent + l.len / 2, slotY(slot), z, l.len, SRC.h, SRC.d, blue ? C.blue : C.faint);
      });
      inserted.forEach((l, k) => {
        const u = ease((ins - k * 0.08) / 0.6); if (u <= 0) return;
        const x = x0 + 0.06 * l.indent + l.len / 2 - (1 - u) * 0.9, y = slotY(SRC.insertAt + k);
        b.box(x, y, z, l.len, SRC.h, SRC.d, C.faint);
        b.box(x, y, z - 0.004, l.len + 0.012, SRC.h + 0.009, SRC.d - 0.006, C.amber);   // amber rim behind
      });
      // Brackets on the left: blue = the anchored range (moves to the content), red = where a bare number points.
      const bracket = (k0, k1, color, depth) => {
        const xb = x0 - 0.05, y0 = slotY(k1) - SRC.pitch / 2 + 0.003, y1 = slotY(k0) + SRC.pitch / 2 - 0.003;
        b.span(xb - 0.006, xb, y0, y1, z - 0.01 + depth, z + 0.002 + depth, color);
        b.span(xb - 0.006, xb + 0.03, y1 - 0.005, y1, z - 0.01 + depth, z + 0.002 + depth, color);
        b.span(xb - 0.006, xb + 0.03, y0, y0 + 0.005, z - 0.01 + depth, z + 0.002 + depth, color);
      };
      const k0 = 39 + 6 * res;
      bracket(k0, k0 + 12, C.blue, 0);
      if (res > 0.02) { const o = res; const xb = x0 - 0.09; const y0 = slotY(51) - SRC.pitch / 2, y1 = slotY(39) + SRC.pitch / 2;
        const mid = (y0 + y1) / 2, hh = (y1 - y0) / 2 * o;
        b.span(xb - 0.006, xb, mid - hh, mid + hh, z - 0.01, z + 0.002, C.red);
        b.span(xb - 0.006, xb + 0.025, mid + hh - 0.005, mid + hh, z - 0.01, z + 0.002, C.red);
        b.span(xb - 0.006, xb + 0.025, mid - hh, mid - hh + 0.005, z - 0.01, z + 0.002, C.red); }
      // the anchor's tag rides with the blue bracket
      const ty = slotY(k0) + SRC.pitch / 2 + 0.03;
      b.span(x0 - 0.06, x0 + 0.08, ty - 0.012, ty + 0.012, z - 0.006, z + 0.006, C.amber);
    }

    // Gate plan: a blue path on the floor and four markers that light in order.
    if (p.plan > 0) {
      let left = p.plan * pathLen;
      for (const sg of path) {
        if (left <= 0) break;
        const L = Math.min(sg.len, left); left -= sg.len;
        const dx = (sg.b[0] - sg.a[0]) / sg.len, dz = (sg.b[1] - sg.a[1]) / sg.len, ry = -Math.atan2(dz, dx);
        if (!sg.dashed) b.box(sg.a[0] + dx * L / 2, 0.0025, sg.a[1] + dz * L / 2, L, 0.005, 0.03, C.blue, ry);
        else for (let d = 0; d < L; d += 0.16) { const l = Math.min(0.09, L - d); b.box(sg.a[0] + dx * (d + l / 2), 0.0025, sg.a[1] + dz * (d + l / 2), l, 0.005, 0.03, C.blue, ry); }
      }
      MARKERS.forEach((m, i) => {
        const lit = ease((p.gates - 0.14 - i * 0.2) / 0.08);
        const col = C.grid.clone().lerp(C.blue, lit);
        const o = ease(p.plan * 3 - i * 0.2);
        const r = 0.17 * o;
        b.box(m.at[0], 0.004, m.at[1], 2 * r, 0.008, 2 * r, C.prop);
        b.box(m.at[0], 0.009, m.at[1], 1.3 * r, 0.006, 1.3 * r, col);
      });
    }
    bits.end();

    // The gate's status lamp: two plain strips, neutral when idle, blue when capturing.
    lamp.begin();
    const lc = C.grid.clone().lerp(C.blue, p.armed);
    lamp.box(GATE[0], 1.30, 0.665, 0.04, 0.6, 0.008, lc);
    lamp.box(-1.655, 1.79, 0, 0.008, 0.04, 0.6, lc);
    lamp.end();
  }

  setPalette(palette);
  return {
    meshes: [bits.mesh, lamp.mesh, threadA, threadB],
    update, setPalette,
    anchors: {
      copyAt, needleShelf, pageTop: (fx) => [fx + PAGE.x0, pageY(0) + 0.06, PAGE.z],
      markers: MARKERS, srcTag: (s) => [SRC.left - 0.06, SRC.top - (39 + 6 * s.p.resolve) * SRC.pitch + SRC.pitch / 2 + 0.03 - (1 - s.p.source) * 2.4, SRC.z],
      run: [SX - 2.2, 1.95, SZ + 0.28],
    },
    used: () => bits.used,
  };
}
