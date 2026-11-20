// three.js r160 GLTFLoader (vendored, unmodified; compiled by build.mjs into a factory) linked against the
// host's three.js namespace, and a GLB parse that works under the MCP App CSP.
import gltfFactory from "agy3d-gltf-factory";

export function gltfLoaderFor(THREE) {
  const { GLTFLoader } = gltfFactory((name) => {
    if (name === "three") return THREE;
    throw new Error("GLTFLoader requires unknown module " + name);
  });
  return GLTFLoader;
}

/**
 * Parse GLB bytes. Embedded images become blob: URLs inside GLTFLoader; its default ImageBitmapLoader reads
 * those with fetch(), which the frame's CSP (connect-src 'none') refuses. A parser plugin swaps in
 * THREE.TextureLoader, which reads them through an <img> (img-src allows blob:). Same textures, same settings.
 */
export function parseGlb(THREE, GLTFLoader, buffer) {
  const loader = new GLTFLoader();
  loader.register((parser) => {
    const tl = new THREE.TextureLoader(parser.options.manager);
    tl.setCrossOrigin(parser.options.crossOrigin);
    parser.textureLoader = tl;
    return { name: "agy3d_img_element_textures" };
  });
  return new Promise((resolve, reject) => loader.parse(buffer, "", resolve, reject));
}

export function base64ToArrayBuffer(s) {
  const bin = atob(s), n = bin.length, u8 = new Uint8Array(n);
  for (let i = 0; i < n; i++) u8[i] = bin.charCodeAt(i);
  return u8.buffer;
}
