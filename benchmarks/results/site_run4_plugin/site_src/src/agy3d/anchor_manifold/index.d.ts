// anchor_manifold: written by `agy3d install`.
import type { Group, LOD, WebGLRenderer } from "three";
import type { KTX2Loader } from "three/examples/jsm/loaders/KTX2Loader.js";
export interface Agy3dBounds {
  /** Metres, +Y up, in the asset's own space. */
  min: [number, number, number];
  max: [number, number, number];
  size: [number, number, number];
  center: [number, number, number];
  /** Half the bounding box's diagonal. */
  radius: number;
}

/** A convex collision hull: `points` is x,y,z,x,y,z,... in metres; `index` lists triangles (three indices each). */
export interface Agy3dHull {
  points: number[];
  index: number[];
}

export interface Agy3dData {
  asset: string;
  bounds: Agy3dBounds;
  /** Empty when loaded with `collision: false`. */
  hulls: Record<string, Agy3dHull>;
  lod: LOD;
  /** Every level, nearest first. `distance`: where it appears, for the recorded field of view. `loaded`: in the LOD
   * now. `failed`: its file did not load (the nearer level stays shown; it is not requested again). */
  levels: { file: string; triangles: number; distance: number; loaded: boolean; failed: boolean }[];
  /** Fetches every level not loaded yet; resolves with the group once all are in the LOD, rejects if one failed. */
  loadLevels(): Promise<Group & { userData: { agy3d: Agy3dData } }>;
  /** Frees what the loader made; a level that arrives later is freed and not added. */
  dispose(): void;
}

export interface LoadOptions {
  /** Needed when the asset is compressed (KTX2): the renderer that will draw it. */
  renderer?: WebGLRenderer;
  /** Where the files are served from. Default: the site root (Vite's `base`) + `agy3d/anchor_manifold/`. */
  baseUrl?: string;
  /** Where the KTX2 transcoder is served from. Default: the site root + `agy3d/_basis/`. */
  transcoderPath?: string;
  /** The project's own KTX2Loader, instead of the one made for the duration of the load. */
  ktx2Loader?: KTX2Loader;
  /** false: do not fetch the collision data (a page without physics). */
  collision?: boolean;
  /** Left out: the loader resolves with the nearest level, and each farther level is fetched the first time the camera
   * comes within 80 % of the distance where it appears. "all": every level is fetched before the loader resolves, and
   * it rejects if one fails. */
  levels?: "all";
}

export const bounds: Agy3dBounds;
export function loadAnchorManifold(options?: LoadOptions): Promise<Group & { userData: { agy3d: Agy3dData } }>;
