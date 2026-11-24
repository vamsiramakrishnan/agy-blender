// containment_gate: written by `agy3d install` (antigravity-3d). Edit freely; `agy3d install` keeps an edited file and says so.
//
//   import { loadContainmentGate } from "./agy3d/containment_gate";
//   scene.add(await loadContainmentGate({ renderer }));
//
// Returns a THREE.Group (its origin is the asset's own origin, metres, +Y up) holding one THREE.LOD whose levels
// switch by distance. Everything else is in `group.userData.agy3d`: `bounds`, `hulls` (collision hulls as plain
// arrays), `levels`, `dispose()`.
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

const DATA = {
 "asset": "containment_gate",
 "assetsUrl": "agy3d/containment_gate",
 "basisUrl": "agy3d/_basis",
 "fov": 60.0,
 "compressed": false,
 "hulls": true,
 "bounds": {
  "min": [
   -0.78,
   0.0,
   -0.47
  ],
  "max": [
   0.85,
   1.615,
   0.47
  ],
  "size": [
   1.63,
   1.615,
   0.94
  ],
  "center": [
   0.035,
   0.8075,
   0.0
  ],
  "radius": 1.239831
 },
 "levels": [
  {
   "file": "containment_gate.glb",
   "triangles": 3546,
   "distance": 0.0,
   "hysteresis": 0.0
  },
  {
   "file": "containment_gate_LOD1.glb",
   "triangles": 2648,
   "distance": 9.019292,
   "hysteresis": 0.047619
  },
  {
   "file": "containment_gate_LOD2.glb",
   "triangles": 1395,
   "distance": 45.096461,
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

export async function loadContainmentGate(options = {}) {
  const base = options.baseUrl ?? join(SITE, DATA.assetsUrl);
  const loader = new GLTFLoader();
  const ktx2 = { release() {} };
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
