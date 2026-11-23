// props.js: the five props. Each is a Group standing on y = 0 at its place, front toward +Z. Until assets/<name>.glb
// has loaded (or when it is missing) the group holds a stand-in built from boxes to the dimensions in SPEC section 6,
// merged into one geometry (one draw call) in the `surface2` material.
import * as THREE from '../../vendor/three.module.min.js';
import { GLTFLoader } from '../../vendor/GLTFLoader.js';
import { mergeGeometries } from '../../vendor/BufferGeometryUtils.js';
import { FX, TURN, SX, SZ, CONSOLE, LEDGER, GATE } from './world.js';

// A box from min/max corners, in the prop's local frame.
const B = (x0, x1, y0, y1, z0, z1) => {
  const g = new THREE.BoxGeometry(x1 - x0, y1 - y0, z1 - z0);
  g.translate((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
  return g;
};
const merged = (boxes) => mergeGeometries(boxes.map((g) => g.toNonIndexed()), false);

const STANDINS = {
  // Console: plinth, cabinet, desk, a raised back with a dark screen bezel, the outlet on +X at (0.58, 1.30).
  console: () => merged([
    B(-0.52, 0.52, 0, 0.05, -0.44, 0.44),
    B(-0.46, 0.46, 0.05, 1.16, -0.38, 0.38),
    B(-0.5, 0.5, 1.16, 1.21, -0.42, 0.42),
    B(-0.46, 0.46, 1.21, 1.76, -0.38, -0.3),
    B(-0.5, 0.5, 1.76, 1.8, -0.42, -0.26),
    B(0.46, 0.58, 1.19, 1.41, -0.13, 0.13),       // outlet housing; its face is the outlet O
  ]),
  // Capture gate: two posts at z = ±0.60, beam 1.72..1.86, clear opening 1.08 x 1.72; feet.
  capture_gate: () => merged([
    B(-0.05, 0.05, 0, 1.72, 0.54, 0.66), B(-0.05, 0.05, 0, 1.72, -0.66, -0.54),
    B(-0.05, 0.05, 1.72, 1.86, -0.66, 0.66),
    B(-0.22, 0.22, 0, 0.03, 0.52, 0.68), B(-0.22, 0.22, 0, 0.03, -0.68, -0.52),
  ]),
  // Context frame: inner volume x ±1.28, y 0.18..1.78, z ±0.33; open front and back.
  context_frame: () => merged([
    B(-1.4, 1.4, 0, 0.18, -0.4, 0.4),
    B(-1.4, 1.4, 1.78, 1.9, -0.4, 0.4),
    B(-1.4, -1.28, 0.18, 1.78, -0.4, -0.3), B(-1.4, -1.28, 0.18, 1.78, 0.3, 0.4),
    B(1.28, 1.4, 0.18, 1.78, -0.4, -0.3), B(1.28, 1.4, 0.18, 1.78, 0.3, 0.4),
    B(-1.37, -1.31, 0.18, 1.78, -0.3, 0.3), B(1.31, 1.37, 0.18, 1.78, -0.3, 0.3),
  ]),
  // Evidence store: 8 bays of 0.40 inner width at 0.46 pitch, a ninth bay with three shelves; base, top, back.
  evidence_store: () => {
    const g = [];
    const walls = []; for (let k = 0; k <= 8; k++) walls.push(-2.18 + 0.46 * k); walls.push(2.18);
    for (const w of walls) g.push(B(w - 0.03, w + 0.03, 0.16, 1.88, -0.27, 0.25));
    g.push(B(-2.24, 2.24, 0, 0.16, -0.29, 0.27));            // base
    g.push(B(-2.24, 2.24, 1.88, 1.96, -0.29, 0.27));         // top
    g.push(B(-2.21, 2.21, 0.16, 1.88, -0.29, -0.23));        // back
    for (const y of [0.45, 0.95, 1.45]) g.push(B(1.53, 2.15, y - 0.03, y, -0.23, 0.23));
    return merged(g);
  },
  // Ledger stand: foot, column, a tray on top.
  ledger_stand: () => merged([
    B(-0.24, 0.24, 0, 0.03, -0.18, 0.18),
    B(-0.03, 0.03, 0.03, 1.0, -0.03, 0.03),
    B(-0.26, 0.26, 1.0, 1.03, -0.19, 0.19),
    B(-0.26, 0.26, 1.03, 1.06, -0.19, -0.16),
  ]),
};

export const PLACES = {
  console: [CONSOLE],
  capture_gate: [[GATE[0], 0, 0]],
  context_frame: [0, 1, 2, 3, 4, 5, 6].map((k) => [FX + TURN * k, 0, 0]),
  evidence_store: [[SX, 0, SZ]],
  ledger_stand: [LEDGER],
};

export function createProps({ palette, onLoad }) {
  const material = new THREE.MeshStandardMaterial({ roughness: 0.78, metalness: 0.0 });
  material.color.set(palette.surface2);
  const groups = {};
  for (const [name, places] of Object.entries(PLACES)) {
    const geo = STANDINS[name]();
    groups[name] = places.map((pos) => {
      const g = new THREE.Group(); g.position.set(...pos); g.name = name;
      const m = new THREE.Mesh(geo, material); m.castShadow = true; m.receiveShadow = true;
      g.add(m);
      return g;
    });
  }
  let loaded = 0;
  const loader = new GLTFLoader();
  // Swap a stand-in for the real prop when assets/<name>.glb exists. A HEAD request first, so a missing file is quiet.
  const tryLoad = async (name) => {
    const url = new URL(`../../assets/${name}.glb`, import.meta.url).href;
    try {
      const head = await fetch(url, { method: 'HEAD', cache: 'no-store' });
      if (!head.ok) return;
      const gltf = await loader.loadAsync(url);
      gltf.scene.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
      groups[name].forEach((g, i) => {
        g.clear();
        g.add(i === 0 ? gltf.scene : gltf.scene.clone(true));
      });
      loaded++;
      if (onLoad) onLoad(name);
    } catch (e) { /* stay with the stand-in */ }
  };
  const ready = Promise.all(Object.keys(PLACES).map(tryLoad));
  return {
    groups, material, ready,
    loaded: () => loaded,
    setPalette: (pal) => material.color.set(pal.surface2),
  };
}
