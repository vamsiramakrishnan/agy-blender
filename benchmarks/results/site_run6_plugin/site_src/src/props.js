// props.js: Straitjacket's three open mechanical instruments on the bench.
// Stand-ins hold exact footprints until each antigravity-3d asset is installed via install_into_project.
import * as THREE from 'three';

export const PROPS = {
  digest_press: {
    asset: 'digest_press',
    module: () => import('./agy3d/digest_press/index.js'),
    at: [0, 0, 0],
    turn: 0,
    size: [1.87, 1.48, 1.18],
  },
  anchor_rack: {
    asset: 'anchor_rack',
    module: () => import('./agy3d/anchor_rack/index.js'),
    at: [-3.4, 0, -0.5],
    turn: 0.25,
    size: [1.42, 1.64, 1.31],
  },
  host_manifold: {
    asset: 'host_manifold',
    module: () => import('./agy3d/host_manifold/index.js'),
    at: [-1.7, 0, -3.4],
    turn: 0.08,
    size: [2.48, 1.17, 1.16],
  },
};

const pascal = (s) => s.split('_').filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join('');

export function standIn([w, h, d]) {
  const geo = new THREE.BoxGeometry(w, h, d);
  geo.translate(0, h / 2, 0);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x242931, metalness: 0.55, roughness: 0.55 }));
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
