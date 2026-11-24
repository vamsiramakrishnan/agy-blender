// anchor_clamp: written by `agy3d install` (antigravity-3d). Edit freely; `agy3d install` keeps an edited file and says so.
//
//   import { loadAnchorClamp } from "./agy3d/anchor_clamp";
//   scene.add(await loadAnchorClamp({ renderer }));
//
// Returns a THREE.Group (its origin is the asset's own origin, metres, +Y up) holding one THREE.LOD whose levels
// switch by distance. Everything else is in `group.userData.agy3d`: `bounds`, `hulls` (collision hulls as plain
// arrays), `levels`, `dispose()`.
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { KTX2Loader } from "three/examples/jsm/loaders/KTX2Loader.js";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";

const DATA = {
 "asset": "anchor_clamp",
 "assetsUrl": "agy3d/anchor_clamp",
 "basisUrl": "agy3d/_basis",
 "fov": 60.0,
 "compressed": true,
 "hulls": true,
 "bounds": {
  "min": [
   -0.66,
   -0.0,
   -0.47
  ],
  "max": [
   0.75,
   1.7,
   0.49
  ],
  "size": [
   1.41,
   1.7,
   0.96
  ],
  "center": [
   0.045,
   0.85,
   0.01
  ],
  "radius": 1.204128
 },
 "levels": [
  {
   "file": "anchor_clamp.glb",
   "triangles": 3582,
   "distance": 0.0,
   "hysteresis": 0.0
  },
  {
   "file": "anchor_clamp_LOD1.glb",
   "triangles": 1700,
   "distance": 43.79784,
   "hysteresis": 0.047619
  }
 ]
};

// Where the files are served from: the site root, or Vite's `base`. `import.meta.env` does not exist outside Vite.
const SITE = (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.BASE_URL) || "/";
const join = (a, b) => (a.endsWith("/") ? a : a + "/") + b;

/** The asset's bounding box in metres, available without loading anything. */
export const bounds = DATA.bounds;

// three measures a LOD's distance to the LOD object's origin; the levels were made for the bounding sphere's centre,
// so the LOD sits there (the group around it keeps the asset's own origin). The distances were computed for the
// vertical field of view DATA.fov; the live camera's field of view and zoom are applied on every update, so the
// switch is right for any camera.
class AssetLOD extends THREE.LOD {
  update(camera) {
    if (camera && camera.isPerspectiveCamera && camera.fov) {
      const k = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) / Math.tan(THREE.MathUtils.degToRad(DATA.fov) / 2);
      // distance / zoom is what LOD.update compares; distance * k is what the recorded distances are for
      super.update({ matrixWorld: camera.matrixWorld, zoom: (camera.zoom || 1) / k });
    } else {
      super.update(camera);
    }
  }
}

// A KTX2Loader owns a pool of transcoder workers. It lives only while loads that use it are in flight (a count per
// renderer, shared by assets loading at the same time through a well-known key): every React Three Fiber Canvas remount
// makes a new renderer, and a loader kept for ever per renderer would pile up worker pools (three.js warns about several
// active KTX2Loaders). The transcoder files stay in the browser's cache, so the next load starts a pool again cheaply.
// Pass `ktx2Loader` to use the project's own: it is never disposed here.
const KTX2_KEY = Symbol.for("agy3d.ktx2Loaders");
function leaseKtx2(options) {
  if (options.ktx2Loader) return { loader: options.ktx2Loader, release() {} };
  const renderer = options.renderer;
  if (!renderer) throw new Error(`${DATA.asset} is compressed (KTX2): pass { renderer } so the right GPU format is chosen`);
  const registry = (globalThis[KTX2_KEY] ??= new WeakMap());
  let entry = registry.get(renderer);
  if (!entry) {
    const path = options.transcoderPath ?? join(SITE, DATA.basisUrl);
    entry = { loader: new KTX2Loader().setTranscoderPath(path.endsWith("/") ? path : path + "/").detectSupport(renderer), uses: 0 };
    registry.set(renderer, entry);
  }
  entry.uses++;
  let released = false;
  return {
    loader: entry.loader,
    release() {
      if (released) return;
      released = true;
      if (--entry.uses === 0) { entry.loader.dispose(); if (registry.get(renderer) === entry) registry.delete(renderer); }
    },
  };
}

function freeScene(scene) {
  scene.traverse((o) => {
    if (!o.isMesh) return;
    o.geometry.dispose();
    for (const m of [].concat(o.material)) {
      for (const v of Object.values(m)) if (v && v.isTexture) v.dispose();
      m.dispose();
    }
  });
}

export async function loadAnchorClamp(options = {}) {
  const base = options.baseUrl ?? join(SITE, DATA.assetsUrl);
  const loader = new GLTFLoader();
  const ktx2 = leaseKtx2(options);
  loader.setKTX2Loader(ktx2.loader);
  loader.setMeshoptDecoder(MeshoptDecoder);
  // Every level and the collision data at once: the far levels name no image, so none waits for another. All of them
  // are waited for before the KTX2 loader is released, also when one fails; then the first failure is thrown.
  const settled = await Promise.allSettled([
    ...DATA.levels.map((level) => loader.loadAsync(join(base, level.file))),
    DATA.hulls && options.collision !== false
      ? fetch(join(base, "collision.json")).then((r) => { if (!r.ok) throw new Error(`${DATA.asset}: collision.json ${r.status}`); return r.json(); })
      : Promise.resolve({}),
  ]);
  ktx2.release();
  const failed = settled.find((r) => r.status === "rejected");
  if (failed) {
    for (const r of settled) if (r.status === "fulfilled" && r.value && r.value.scene) freeScene(r.value.scene);
    throw failed.reason;
  }
  const gltfs = settled.slice(0, DATA.levels.length).map((r) => r.value);
  const hulls = settled[DATA.levels.length].value;
  // One set of materials (and so of textures) for every level: a far level names the highest level's materials.
  const materials = new Map();
  gltfs[0].scene.traverse((o) => { if (o.isMesh) materials.set(o.material.name, o.material); });
  for (const gltf of gltfs.slice(1)) {
    gltf.scene.traverse((o) => {
      if (!o.isMesh) return;
      const shared = materials.get(o.material.name);
      if (shared) { o.material.dispose(); o.material = shared; }
    });
  }
  // What this loader made, collected now: dispose() frees these and nothing the developer added or assigned later
  // (an environment map, a texture shared with other objects, children).
  const made = { geometries: new Set(), materials: new Set(), textures: new Set() };
  for (const gltf of gltfs) {
    gltf.scene.traverse((o) => {
      if (!o.isMesh) return;
      made.geometries.add(o.geometry);
      for (const m of [].concat(o.material)) {
        made.materials.add(m);
        for (const v of Object.values(m)) if (v && v.isTexture) made.textures.add(v);
      }
    });
  }
  const root = new THREE.Group();
  root.name = DATA.asset;
  const lod = new AssetLOD();
  lod.name = DATA.asset + "_LOD";
  lod.position.fromArray(DATA.bounds.center);
  gltfs.forEach((gltf, i) => {
    const holder = new THREE.Group();
    holder.name = `LOD${i}`;
    holder.position.fromArray(DATA.bounds.center).multiplyScalar(-1);
    holder.add(gltf.scene);
    lod.addLevel(holder, DATA.levels[i].distance, DATA.levels[i].hysteresis);
  });
  root.add(lod);
  root.userData.agy3d = {
    asset: DATA.asset, bounds: DATA.bounds, hulls, lod, levels: DATA.levels.map((l) => ({ file: l.file, triangles: l.triangles, distance: l.distance })),
    dispose() {
      made.geometries.forEach((g) => g.dispose());
      made.textures.forEach((t) => t.dispose());
      made.materials.forEach((m) => m.dispose());
    },
  };
  return root;
}
