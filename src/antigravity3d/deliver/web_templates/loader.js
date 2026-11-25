// %%ASSET%%: written by `agy3d install` (antigravity-3d). Edit freely; `agy3d install` keeps an edited file and says so.
//
//   import { %%LOAD%% } from "./agy3d/%%ASSET%%";
//   scene.add(await %%LOAD%%({ renderer }));
//
// Returns a THREE.Group (its origin is the asset's own origin, metres, +Y up) holding one THREE.LOD whose levels
// switch by distance. It resolves once the nearest level is loaded; each farther level is fetched the first time the
// camera comes near the distance where it appears (`{ levels: "all" }` fetches every level before resolving).
// Everything else is in `group.userData.agy3d`: `bounds`, `hulls` (collision hulls as plain arrays), `levels` (each
// with `loaded`), `loadLevels()` (a promise that every level is loaded), `dispose()`.
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
%%IMPORTS%%
const DATA = %%DATA%%;

// Where the files are served from: the site root, or Vite's `base`. `import.meta.env` does not exist outside Vite.
const SITE = (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.BASE_URL) || "/";
const join = (a, b) => (a.endsWith("/") ? a : a + "/") + b;

/** The asset's bounding box in metres, available without loading anything. */
export const bounds = DATA.bounds;

// A far level is fetched the first time the camera comes within REACH of the distance where it appears. At 0.8 the
// request starts while the asset still looks a quarter larger than at the switch (1 / 0.8), nearer than the
// hysteresis band (95 to 100 % of that distance), and a camera moving out has the last fifth of it for the file to
// arrive
// (the third website's far levels were 92 to 213 kB each); until it has, three.js draws the nearest loaded level.
// Fetching all of them up front cost run 3 of the website task 856 kB of its 1.56 MB of models, for levels its camera
// never reached.
const REACH = %%REACH%%;
const _camera = new THREE.Vector3(), _centre = new THREE.Vector3();

// three measures a LOD's distance to the LOD object's origin; the levels were made for the bounding sphere's centre,
// so the LOD sits there (the group around it keeps the asset's own origin). The distances were computed for the
// vertical field of view DATA.fov; the live camera's field of view and zoom are applied on every update, so the
// switch is right for any camera. The levels not requested yet are requested from here, with the same distance the
// switch compares: no timer, no polling, only the updates the renderer makes anyway.
class AssetLOD extends THREE.LOD {
  constructor() {
    super();
    this.waiting = []; // { reach, request } for each far level not requested yet
  }

  update(camera) {
    let view = camera;
    if (camera && camera.isPerspectiveCamera && camera.fov) {
      const k = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) / Math.tan(THREE.MathUtils.degToRad(DATA.fov) / 2);
      // distance / zoom is what LOD.update compares; distance * k is what the recorded distances are for
      view = { matrixWorld: camera.matrixWorld, zoom: (camera.zoom || 1) / k };
    }
    if (this.waiting.length && view && view.matrixWorld) {
      _camera.setFromMatrixPosition(view.matrixWorld);
      const distance = _camera.distanceTo(_centre.setFromMatrixPosition(this.matrixWorld)) / view.zoom;
      const reached = this.waiting.filter((w) => distance >= w.reach);
      if (reached.length) {
        this.waiting = this.waiting.filter((w) => distance < w.reach);
        reached.forEach((w) => w.request());
      }
    }
    super.update(view);
  }
}
%%KTX2%%
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

export async function %%LOAD%%(options = {}) {
  if (options.levels !== undefined && options.levels !== "all") {
    throw new Error(`${DATA.asset}: levels is "all" or left out, not ${JSON.stringify(options.levels)}`);
  }
  const all = options.levels === "all";
  const base = options.baseUrl ?? join(SITE, DATA.assetsUrl);
  // The far levels name no image (they draw the nearest level's materials), so they need no KTX2 loader: they have a
  // loader of their own, which still works after the KTX2 pool below is released.
  const loader = new GLTFLoader(), farLoader = new GLTFLoader();
%%LOADER_SETUP%%
  // With levels: "all" every far level is requested now, beside the nearest one; otherwise when the camera comes near.
  const early = DATA.levels.map((level, i) => (all && i ? farLoader.loadAsync(join(base, level.file)) : null));
  early.forEach((p) => p && p.catch(() => {})); // looked at below; a failure is not an unhandled rejection meanwhile
  // The nearest level and the collision data at once. Both are waited for before the KTX2 loader is released, also
  // when one fails; then what loaded is freed and the first failure is thrown.
  const settled = await Promise.allSettled([
    loader.loadAsync(join(base, DATA.levels[0].file)),
    DATA.hulls && options.collision !== false
      ? fetch(join(base, "collision.json")).then((r) => { if (!r.ok) throw new Error(`${DATA.asset}: collision.json ${r.status}`); return r.json(); })
      : Promise.resolve({}),
  ]);
  ktx2.release();
  const failed = settled.find((r) => r.status === "rejected");
  if (failed) {
    if (settled[0].status === "fulfilled") freeScene(settled[0].value.scene);
    early.forEach((p) => p && p.then((gltf) => freeScene(gltf.scene), () => {}));
    throw failed.reason;
  }
  const near = settled[0].value;
  const hulls = settled[1].value;
  // One set of materials (and so of textures) for every level: a far level names the highest level's materials.
  const materials = new Map();
  near.scene.traverse((o) => { if (o.isMesh) materials.set(o.material.name, o.material); });
  // What this loader made, collected as it arrives: dispose() frees these and nothing the developer added or assigned
  // later (an environment map, a texture shared with other objects, children).
  const made = { geometries: new Set(), materials: new Set(), textures: new Set() };
  const collect = (scene) => scene.traverse((o) => {
    if (!o.isMesh) return;
    made.geometries.add(o.geometry);
    for (const m of [].concat(o.material)) {
      made.materials.add(m);
      for (const v of Object.values(m)) if (v && v.isTexture) made.textures.add(v);
    }
  });
  collect(near.scene);
  const root = new THREE.Group();
  root.name = DATA.asset;
  const lod = new AssetLOD();
  lod.name = DATA.asset + "_LOD";
  lod.position.fromArray(DATA.bounds.center);
  const holderOf = (gltf, i) => {
    const holder = new THREE.Group();
    holder.name = `LOD${i}`;
    holder.position.fromArray(DATA.bounds.center).multiplyScalar(-1);
    holder.add(gltf.scene);
    return holder;
  };
  lod.addLevel(holderOf(near, 0), DATA.levels[0].distance, DATA.levels[0].hysteresis);
  root.add(lod);
  const levels = DATA.levels.map((l, i) => ({
    file: l.file, triangles: l.triangles, distance: l.distance, loaded: i === 0, failed: false,
  }));
  const arrivals = [], errors = [];
  let disposed = false;
  // A far level, once: it settles to nothing either way (a failure is remembered for loadLevels), so a level that
  // did not load is not asked for again and nothing rejects unwatched.
  const fetchLevel = (i) => early[i] || farLoader.loadAsync(join(base, DATA.levels[i].file));
  const request = (i) => arrivals[i] ??= fetchLevel(i).then((gltf) => {
    if (disposed) { freeScene(gltf.scene); return; } // arrived after dispose(): freed, never shown
    gltf.scene.traverse((o) => {
      if (!o.isMesh) return;
      const shared = materials.get(o.material.name);
      if (shared) { o.material.dispose(); o.material = shared; }
    });
    collect(gltf.scene);
    const holder = holderOf(gltf, i);
    holder.visible = false; // shown from the LOD's next update, when the camera is far enough
    lod.addLevel(holder, DATA.levels[i].distance, DATA.levels[i].hysteresis);
    levels[i].loaded = true;
  }, (error) => {
    levels[i].failed = true;
    errors[i] = error;
    if (!all && !disposed) {
      const why = (error && error.message) || error;
      console.warn(`${DATA.asset}: the far level ${DATA.levels[i].file} did not load (${why}); `
                   + "the nearer level stays shown and it is not requested again");
    }
  });
  const loadLevels = () => {
    if (disposed) return Promise.reject(new Error(`${DATA.asset}: loadLevels() after dispose()`));
    lod.waiting = [];
    return Promise.all(DATA.levels.map((_, i) => i && request(i))).then(() => {
      const error = errors.find(Boolean);
      if (error) throw error;
      return root;
    });
  };
  lod.waiting = DATA.levels.slice(1).map((l, j) => ({ reach: REACH * l.distance, request: () => request(j + 1) }));
  root.userData.agy3d = {
    asset: DATA.asset, bounds: DATA.bounds, hulls, lod, levels, loadLevels,
    dispose() {
      disposed = true;
      lod.waiting = [];
      made.geometries.forEach((g) => g.dispose());
      made.textures.forEach((t) => t.dispose());
      made.materials.forEach((m) => m.dispose());
    },
  };
  if (all) {
    try {
      await loadLevels();
    } catch (error) {
      root.userData.agy3d.dispose();
      throw error;
    }
  }
  return root;
}
