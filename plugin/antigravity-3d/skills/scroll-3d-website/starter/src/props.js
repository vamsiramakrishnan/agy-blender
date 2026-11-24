// props.js: the props the story needs, one entry each.
//
// Until a prop is built, its stand-in (a grey block of `size` metres) holds its place, so the camera work and the
// chapters can be done and looked at before any modelling. Once install_into_project has installed the prop, add the
// two fields its answer gives: `asset` (the asset's name) and `module` (an import of the loader it wrote, by default
// src/agy3d/<asset>/index.js). Keep `size`: it is the stand-in while the model loads or if it fails.
import * as THREE from 'three';

export const PROPS = {
  hero: { at: [0, 0, 0], turn: 0, size: [1.4, 1.5, 0.9] },
  part: { at: [1.7, 0, 0.7], turn: -0.5, size: [0.5, 0.45, 0.5] },
  // An installed prop looks like this (the loader's export is "load" + the asset name in PascalCase):
  // hero: { at: [0, 0, 0], turn: 0, size: [1.4, 1.5, 0.9], asset: 'containment_gate',
  //         module: () => import('./agy3d/containment_gate/index.js') },
};

const pascal = (s) => s.split('_').filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join('');

export function standIn([w, h, d]) {
  const geo = new THREE.BoxGeometry(w, h, d);
  geo.translate(0, h / 2, 0); // stands on the ground, like an installed asset (its origin is at its base)
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x8a8f96, roughness: 0.6 }));
  mesh.name = 'stand-in';
  const group = new THREE.Group();
  group.add(mesh);
  return group;
}

// Loads one prop through the installer's loader. The base URL is given relative to the page because the loader's own
// default outside Vite is the site root, and a site published under a sub-path then requests its models from the
// wrong folder (check_site serves the site under a sub-path to catch exactly this).
export async function loadProp(entry, renderer) {
  if (!entry.module) return standIn(entry.size);
  const mod = await entry.module();
  const load = mod['load' + pascal(entry.asset)];
  if (typeof load !== 'function') throw new Error(`${entry.asset}: its loader has no export load${pascal(entry.asset)}`);
  return load({ renderer, baseUrl: new URL(`agy3d/${entry.asset}/`, document.baseURI).href, collision: false });
}
