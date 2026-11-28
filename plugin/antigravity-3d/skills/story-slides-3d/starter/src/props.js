// props.js: the props of the story that the plugin builds, one entry each; a scene asks for one with kit.prop(name).
//
// Until a prop is built, the scene's own stand-in (made of primitives) holds its place, so the story can be shot and
// watched before any modelling. Once install_into_project has installed the prop, add two fields: `asset` (the
// asset's name) and `module` (an import of the loader file the install wrote, src/agy3d/<asset>/index.js). `size`
// (metres) is the grey block shown when a scene gives no stand-in. The scene places and turns what it gets.
export const PROPS = {
  house: { size: [9, 7.6, 7] },
  // An installed prop looks like this (the loader's export is "load" + the asset name in PascalCase):
  // house: { size: [9, 7.6, 7], asset: 'farm_house', module: () => import('./agy3d/farm_house/index.js') },
};

const pascal = (s) => s.split('_').filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join('');

// Loads one prop through the installer's loader. The base URL is given relative to the page because the loader's own
// default outside Vite is the site root, and a story published under a sub-path would request its models from the
// wrong folder.
export async function loadProp(entry, renderer) {
  const mod = await entry.module();
  const load = mod['load' + pascal(entry.asset)];
  if (typeof load !== 'function') throw new Error(`${entry.asset}: its loader has no export load${pascal(entry.asset)}`);
  return load({ renderer, baseUrl: new URL(`agy3d/${entry.asset}/`, document.baseURI).href, collision: false });
}
