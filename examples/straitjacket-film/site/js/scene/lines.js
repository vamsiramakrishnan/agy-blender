// lines.js: the 20,001 log lines as one InstancedMesh of thin slabs. Every motion is computed in the vertex shader
// from a few uniforms (flood, capture, trunc); the per-line attributes are uploaded once and never touched again.
import * as THREE from '../../vendor/three.module.min.js';
import {
  FX, SLAB_H, SLAB_D, slabLen, F_COLS, F_ROWS, F_LAYERS, frameCell, S_COLS, S_LAYERS, storeCell, shelfCell,
  NEEDLE_INDEX, NEEDLE_COL, LOUD, rng, OUTLET, GATE, CONSOLE,
} from './world.js';
import { W } from './tracks.js';

// A unit slab with only the faces a reader standing in front of and above the stage can see: top, front, both ends.
// 8 triangles instead of 12; the shadow pass renders front faces (the tops) so nothing is lost there either.
function slabGeometry() {
  const P = [], N = [], I = [];
  const quad = (a, b, c, d, n) => { const o = P.length / 3; P.push(...a, ...b, ...c, ...d); for (let i = 0; i < 4; i++) N.push(...n); I.push(o, o + 1, o + 2, o, o + 2, o + 3); };
  const h = 0.5;
  quad([-h, h, h], [h, h, h], [h, h, -h], [-h, h, -h], [0, 1, 0]);      // top
  quad([-h, -h, h], [h, -h, h], [h, h, h], [-h, h, h], [0, 0, 1]);      // front
  quad([h, -h, h], [h, -h, -h], [h, h, -h], [h, h, h], [1, 0, 0]);      // +x end
  quad([-h, -h, -h], [-h, -h, h], [-h, h, h], [-h, h, -h], [-1, 0, 0]); // -x end
  const g = new THREE.InstancedBufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(P, 3));
  g.setAttribute('normal', new THREE.Float32BufferAttribute(N, 3));
  g.setIndex(I);
  return g;
}

// Per-line attributes from lines.bin ([family, chars] per line, in log order).
function buildAttributes(bytes) {
  const n = bytes.length / 2;
  const aF = new Float32Array(n * 3), aS = new Float32Array(n * 3), aD = new Float32Array(n * 4), aR = new Float32Array(n * 4);
  const rand = rng(14238);
  // Frame: rows fill bottom to top in log order; the 200 cells of a row are taken in a seeded shuffle.
  const perRow = F_COLS * F_LAYERS;
  const cells = new Int32Array(perRow);
  for (let row = 0; row * perRow < n; row++) {
    for (let c = 0; c < perRow; c++) cells[c] = c;
    for (let c = perRow - 1; c > 0; c--) { const j = Math.floor(rand() * (c + 1)); const t = cells[c]; cells[c] = cells[j]; cells[j] = t; }
    const start = row * perRow;
    // The needle sits on the front layer, at a fixed column.
    if (NEEDLE_INDEX >= start && NEEDLE_INDEX < start + perRow) {
      const want = (F_LAYERS - 1) * F_COLS + NEEDLE_COL, at = cells.indexOf(want), k = NEEDLE_INDEX - start;
      cells[at] = cells[k]; cells[k] = want;
    }
    for (let k = 0; k < perRow && start + k < n; k++) {
      const i = start + k, len = slabLen(bytes[i * 2 + 1]);
      let col = cells[k] % F_COLS, layer = Math.floor(cells[k] / F_COLS), r = row;
      if (row >= F_ROWS) { col = 12; layer = F_LAYERS - 1; r = F_ROWS; } // line 20,001 lies on top of the last row
      aF.set(frameCell(col, r, layer, len), i * 3);
    }
  }
  // Store: each family fills its bay bottom to top in log order; inside a row, back layer first.
  const seen = new Int32Array(11);
  const perBayRow = S_COLS * S_LAYERS;
  for (let i = 0; i < n; i++) {
    const fam = bytes[i * 2], len = slabLen(bytes[i * 2 + 1]);
    if (fam <= 7) {
      const j = seen[fam]++, row = Math.floor(j / perBayRow), m = j % perBayRow;
      const layer = Math.floor(m / S_COLS), col = m % S_COLS;
      aS.set(storeCell(fam, col, row, layer, len), i * 3);
    } else aS.set(shelfCell(fam - 8, len), i * 3);
    const kept = i < 40 || i >= n - 40 ? 1 : 0;
    aD.set([i / (n - 1), len, fam, kept], i * 4);
    aR.set([rand() * 2 - 1, rand() * 2 - 1, rand() * 2 - 1, rand()], i * 4);
  }
  return { n, aF, aS, aD, aR };
}

const v3 = (a) => `vec3(${a.map((x) => x.toFixed(4)).join(',')})`;

// The vertex code shared by the colour pass and the shadow (depth) pass.
const HEAD = /* glsl */`
attribute vec3 aF;
attribute vec3 aS;
attribute vec4 aD;
attribute vec4 aR;
uniform float uFlood;
uniform float uCapture;
uniform float uTrunc;
#ifdef SJ_COLOR
uniform vec3 uFaint;
uniform vec3 uInk;
uniform vec3 uRed;
uniform vec3 uAmber;
varying vec3 vSj;
#endif
vec3 sjBez(vec3 a, vec3 b, vec3 c, vec3 d, float t) {
  float s = 1.0 - t;
  return s * s * s * a + 3.0 * s * s * t * b + 3.0 * s * t * t * c + t * t * t * d;
}
// One flight: console -> along the rail -> through the gate -> a curve to just outside the destination cell -> in.
// toStore: the line turns at the gate toward the store and enters its cell from the front (-z);
// otherwise it swings behind the frame and enters from the back (+z).
vec3 sjRoute(float t, vec3 dst, float toStore, vec2 off, out float scl) {
  const vec3 O0 = vec3(${(CONSOLE[0] + 0.12).toFixed(3)}, ${OUTLET[1].toFixed(3)}, 0.0);
  const vec3 G = ${v3(GATE)};
  const float A = 0.36;
  const float B = 0.84;
  scl = smoothstep(0.0, 0.06, t);
  vec3 dz = vec3(0.0, 0.0, mix(-0.62, 0.72, toStore));
  vec3 Q = dst + dz;
  vec3 pos;
  if (t < A) {
    float u = t / A;
    pos = mix(O0, G, u);
    pos.yz += off * smoothstep(0.04, 0.3, u);
  } else if (t < B) {
    float u = (t - A) / (B - A);
    vec3 P1 = G + vec3(mix(1.7, 0.95, toStore), 0.0, 0.0);
    vec3 P2 = Q + dz * 1.6;
    pos = sjBez(G, P1, P2, Q, u);
    pos.yz += off * (1.0 - smoothstep(0.0, 0.85, u));
  } else {
    float u = (t - B) / (1.0 - B);
    u = 1.0 - (1.0 - u) * (1.0 - u) * (1.0 - u);
    pos = mix(Q, dst, u);
  }
  return pos;
}
float sjFly(float p, float r) { return clamp((p - r * (1.0 - ${W.toFixed(3)})) / ${W.toFixed(3)}, 0.0, 1.0); }
`;

const BODY = /* glsl */`
  float rank = aD.x;
  float len = aD.y;
  float fam = aD.z;
  float kept = aD.w;
  float isNeedle = 1.0 - step(0.5, abs(fam - 9.0));
  float isLoud = step(7.5, fam) * (1.0 - isNeedle);
  vec2 off = vec2(aR.y * 0.05, aR.x * 0.10);
  float tc = sjFly(uCapture, rank);
  float tf = sjFly(uFlood, rank);
  float scl = 0.0;
  vec3 c;
  float red = 0.0;
  float ao = 1.0;
  if (tc > 0.0) {
    c = sjRoute(tc, aS, 1.0, off, scl);
    float home = smoothstep(0.92, 1.0, tc);
    // Depth in the bay: the back layers sit in the shade of the ones in front.
    ao = mix(1.0, mix(0.45, 1.0, pow(clamp((aS.z + 3.775) / 0.35, 0.0, 1.0), 1.5)), home * (1.0 - step(7.5, fam)));
  } else {
    c = sjRoute(tf, aF, 0.0, off, scl);
    float land = smoothstep(0.9, 1.0, tf);
    red = 0.65 * land * (1.0 - isNeedle);
    ao = mix(1.0, mix(0.42, 1.0, pow(clamp((aF.z + 0.245) / 0.49, 0.0, 1.0), 1.5)), land);
    if (uTrunc > 0.0 && tf >= 1.0) {
      if (isNeedle > 0.5) {
        // The quiet line falls out of the front of the frame and lies on the floor.
        float u = clamp((uTrunc - 0.18) / 0.5, 0.0, 1.0);
        c.z = mix(aF.z, 0.86, u);
        c.y = mix(aF.y, ${(SLAB_H / 2).toFixed(5)}, u * u);
        ao = 1.0;
      } else if (kept < 0.5) {
        // Everything between the head and the tail leaves, top rows first: shrinks and drops.
        float tt = clamp((uTrunc - (1.0 - rank) * 0.62 - aR.w * 0.08) / 0.3, 0.0, 1.0);
        c.y -= 0.22 * tt * tt;
        scl *= 1.0 - tt;
      }
    }
  }
  vec3 transformed = c + position * vec3(len, ${SLAB_H}, ${SLAB_D}) * scl;
#ifdef SJ_COLOR
  vec3 col = mix(uFaint * (1.0 + (fam - 3.5) * 0.0172) * (1.0 + aR.z * 0.035), uInk, isLoud);
  col = mix(col, uRed, red);
  col = mix(col, uAmber, isNeedle);
  vSj = col * ao;
#endif
`;

function patchVertex(shader, uniforms, color) {
  Object.assign(shader.uniforms, uniforms);
  shader.vertexShader = (color ? '#define SJ_COLOR\n' : '') + HEAD + shader.vertexShader.replace('#include <begin_vertex>', BODY);
}

export function createLines({ bytes, palette }) {
  const A = buildAttributes(bytes);
  const geo = slabGeometry();
  geo.setAttribute('aF', new THREE.InstancedBufferAttribute(A.aF, 3));
  geo.setAttribute('aS', new THREE.InstancedBufferAttribute(A.aS, 3));
  geo.setAttribute('aD', new THREE.InstancedBufferAttribute(A.aD, 4));
  geo.setAttribute('aR', new THREE.InstancedBufferAttribute(A.aR, 4));

  const uniforms = {
    uFlood: { value: 0 }, uCapture: { value: 0 }, uTrunc: { value: 0 },
    uFaint: { value: new THREE.Color() }, uInk: { value: new THREE.Color() }, uRed: { value: new THREE.Color() }, uAmber: { value: new THREE.Color() },
  };
  const material = new THREE.MeshStandardMaterial({ roughness: 0.82, metalness: 0.0, shadowSide: THREE.FrontSide });
  material.onBeforeCompile = (shader) => {
    patchVertex(shader, uniforms, true);
    shader.fragmentShader = 'varying vec3 vSj;\n' + shader.fragmentShader.replace('#include <color_fragment>', '#include <color_fragment>\n  diffuseColor.rgb = vSj;');
  };
  material.customProgramCacheKey = () => 'sj-lines-color';
  const depth = new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking });
  depth.onBeforeCompile = (shader) => patchVertex(shader, uniforms, false);
  depth.customProgramCacheKey = () => 'sj-lines-depth';

  const mesh = new THREE.InstancedMesh(geo, material, A.n);
  mesh.frustumCulled = false;
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  mesh.customDepthMaterial = depth;
  mesh.instanceMatrix.setUsage(THREE.StaticDrawUsage);

  const setPalette = (pal) => {
    uniforms.uFaint.value.set(pal.faint); uniforms.uInk.value.set(pal.ink);
    uniforms.uRed.value.set(pal.red); uniforms.uAmber.value.set(pal.amber);
  };
  setPalette(palette);

  // Nothing is out when no flight has started (or after the drain); skip the 20,001 instances then.
  const update = (p) => {
    uniforms.uFlood.value = p.flood; uniforms.uCapture.value = p.capture; uniforms.uTrunc.value = p.trunc;
    mesh.visible = p.flood > 1e-5 || p.capture > 1e-5;
  };

  // Where a given line is when it rests (for labels): the needle's frame cell and shelf.
  const at = (i, which) => { const a = which === 'store' ? A.aS : A.aF; return [a[i * 3], a[i * 3 + 1], a[i * 3 + 2]]; };
  return { mesh, update, setPalette, at, count: A.n, needleLen: A.aD[NEEDLE_INDEX * 4 + 1], loud: LOUD, FX };
}
