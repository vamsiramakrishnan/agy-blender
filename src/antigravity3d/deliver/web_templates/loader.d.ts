// %%ASSET%%: written by `agy3d install`.
import type { Group, LOD, WebGLRenderer } from "three";
%%KTX2_TYPE_IMPORT%%
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
  levels: { file: string; triangles: number; distance: number }[];
  dispose(): void;
}

export interface LoadOptions {
  /** Needed when the asset is compressed (KTX2): the renderer that will draw it. */
  renderer?: WebGLRenderer;
  /** Where the files are served from. Default: the site root (Vite's `base`) + `%%ASSETS_URL%%/`. */
  baseUrl?: string;
  /** Where the KTX2 transcoder is served from. Default: the site root + `%%BASIS_URL%%/`. */
  transcoderPath?: string;
%%KTX2_OPTION%%  /** false: do not fetch the collision data (a page without physics). */
  collision?: boolean;
}

export const bounds: Agy3dBounds;
export function %%LOAD%%(options?: LoadOptions): Promise<Group & { userData: { agy3d: Agy3dData } }>;
