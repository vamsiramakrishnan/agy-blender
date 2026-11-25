// props.js: the three installed precision instrument stations along the Straitjacket evidence bench.
import * as THREE from 'three';

export const PROPS = {
  capture_press: {
    at: [0, 0, 0],
    turn: 0,
    size: [1.66, 1.725, 1.314],
    asset: 'capture_press',
    module: () => import('./agy3d/capture_press/index.js'),
  },
  artifact_vault: {
    at: [2.75, 0, -1.3],
    turn: -0.22,
    size: [1.56, 1.495, 1.331],
    asset: 'artifact_vault',
    module: () => import('./agy3d/artifact_vault/index.js'),
  },
  anchor_caliper: {
    at: [5.35, 0, 0.1],
    turn: -0.28,
    size: [2.012, 1.215, 0.958],
    asset: 'anchor_caliper',
    module: () => import('./agy3d/anchor_caliper/index.js'),
  },
};

const pascal = (s) => s.split('_').filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join('');

export function standIn([w, h, d]) {
  const geo = new THREE.BoxGeometry(w, h, d);
  geo.translate(0, h / 2, 0);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x8f97a0, roughness: 0.55, metalness: 0.25 }));
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
  return load({
    renderer,
    baseUrl: new URL(`agy3d/${entry.asset}/`, document.baseURI).href,
    basisUrl: new URL('agy3d/_basis/', document.baseURI).href,
    collision: false,
  });
}
