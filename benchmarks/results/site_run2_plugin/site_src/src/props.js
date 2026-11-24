// props.js: the three bespoke 3D instruments along the Evidence Bench.
// Stand-ins hold exact positions and rough metres until install_into_project loaders are added.
import * as THREE from 'three';

export const PROPS = {
  capture_gate: {
    at: [0, 0, 0],
    turn: 0,
    size: [1.6, 1.8, 1.0],
    asset: 'capture_gate',
    module: () => import('./agy3d/capture_gate/index.js'),
  },
  artifact_vault: {
    at: [3.2, 0, -0.5],
    turn: -0.22,
    size: [1.5, 1.5, 1.2],
    asset: 'artifact_vault',
    module: () => import('./agy3d/artifact_vault/index.js'),
  },
  anchor_clamp: {
    at: [6.0, 0, 0.4],
    turn: -0.35,
    size: [1.4, 1.4, 1.1],
    asset: 'anchor_clamp',
    module: () => import('./agy3d/anchor_clamp/index.js'),
  },
};

const pascal = (s) =>
  s
    .split('_')
    .filter(Boolean)
    .map((p) => p[0].toUpperCase() + p.slice(1))
    .join('');

export function standIn([w, h, d]) {
  const group = new THREE.Group();
  const bodyGeo = new THREE.BoxGeometry(w, h, d);
  bodyGeo.translate(0, h / 2, 0);
  const bodyMat = new THREE.MeshStandardMaterial({
    color: 0x383e47,
    roughness: 0.62,
    metalness: 0.25,
  });
  const body = new THREE.Mesh(bodyGeo, bodyMat);
  body.name = 'stand-in';

  const bandGeo = new THREE.BoxGeometry(w + 0.02, 0.06, d + 0.02);
  bandGeo.translate(0, h * 0.78, 0);
  const bandMat = new THREE.MeshStandardMaterial({
    color: 0xf0b429,
    roughness: 0.4,
    metalness: 0.4,
  });
  const band = new THREE.Mesh(bandGeo, bandMat);
  band.name = 'stand-in-band';

  group.add(body, band);
  return group;
}

export async function loadProp(entry, renderer) {
  if (!entry.module) return standIn(entry.size);
  const mod = await entry.module();
  const load = mod['load' + pascal(entry.asset)];
  if (typeof load !== 'function') {
    throw new Error(`${entry.asset}: its loader has no export load${pascal(entry.asset)}`);
  }
  return load({
    renderer,
    baseUrl: new URL(`agy3d/${entry.asset}/`, document.baseURI).href,
    collision: false,
  });
}
