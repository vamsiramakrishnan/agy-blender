// props.js: the three open-mechanism instrument stations of Straitjacket along one machined bench.
// Stand-ins hold their places while modelling runs; once install_into_project installs each prop,
// `asset` and `module` load the real delivered GLB with LODs and compressed textures.
import * as THREE from 'three';

export const PROPS = {
  containment_press: {
    asset: 'containment_press',
    module: () => import('./agy3d/containment_press/index.js'),
    at: [0.0, 0, 0.0],
    turn: 0,
    size: [1.84, 1.84, 1.19],
  },
  evidence_vault: {
    asset: 'evidence_vault',
    module: () => import('./agy3d/evidence_vault/index.js'),
    at: [2.75, 0, -0.05],
    turn: 0,
    size: [1.38, 1.50, 1.39],
  },
  anchor_manifold: {
    asset: 'anchor_manifold',
    module: () => import('./agy3d/anchor_manifold/index.js'),
    at: [5.55, 0, 0.0],
    turn: 0,
    size: [2.22, 1.08, 1.28],
  },
};

const pascal = (s) => s.split('_').filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join('');

export function standIn([w, h, d]) {
  const geo = new THREE.BoxGeometry(w, h, d);
  geo.translate(0, h / 2, 0);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x3b414c, roughness: 0.65, metalness: 0.25 }));
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
    transcoderPath: new URL('agy3d/_basis/', document.baseURI).href,
    collision: false,
  });
}
