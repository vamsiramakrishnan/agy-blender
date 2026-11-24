// props.js: the three Straitjacket instruments built with antigravity-3d and installed via install_into_project.
// Stand-ins render immediately on frame 1 and swap cleanly once the installed LOD models load.
import * as THREE from 'three';

export const PROPS = {
  gate: {
    at: [0, 0, 0],
    turn: 0,
    size: [1.63, 1.615, 0.94],
    asset: 'containment_gate',
    module: () => import('./agy3d/containment_gate/index.js'),
  },
  vault: {
    at: [3.0, 0, -0.25],
    turn: -0.18,
    size: [1.24, 1.596, 0.934],
    asset: 'artifact_vault',
    module: () => import('./agy3d/artifact_vault/index.js'),
  },
  anchor: {
    at: [5.9, 0, 0.2],
    turn: -0.15,
    size: [1.218, 1.482, 0.84],
    asset: 'anchor_clamp',
    module: () => import('./agy3d/anchor_clamp/index.js'),
  },
};

const pascal = (s) => s.split('_').filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join('');

export function standIn([w, h, d]) {
  const geo = new THREE.BoxGeometry(w, h, d);
  geo.translate(0, h / 2, 0);
  const mesh = new THREE.Mesh(
    geo,
    new THREE.MeshStandardMaterial({ color: 0x3a404a, roughness: 0.6, metalness: 0.25 }),
  );
  mesh.name = 'stand-in';
  const group = new THREE.Group();
  group.add(mesh);
  return group;
}

export async function loadProp(entry, renderer) {
  if (!entry.module) return standIn(entry.size);
  const mod = await entry.module();
  const load = mod['load' + pascal(entry.asset)];
  if (typeof load !== 'function') throw new Error(`${entry.asset}: its loader has no export load${pascal(entry.asset)}`);
  return load({ renderer, baseUrl: new URL(`agy3d/${entry.asset}/`, document.baseURI).href, collision: false });
}
