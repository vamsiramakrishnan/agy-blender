// occlusion.js: ambient occlusion, switched on rather than written. occlusion(renderer, scene, camera, { phone }) ->
// { pass, why }: pass draws the frame through postprocessing with an N8AO pass (render(), setSize(w, h), dispose()),
// or is null, and `why` says which and why (window.__site.info().occlusion).
//
// Ambient occlusion darkens where surfaces meet: slabs stacked on each other, a prop's foot on the floor. Without it
// run 3's stacked slabs showed no contact at all and read as toy blocks, although postprocessing and n8ao were
// installed. The packages are imported only here and only when the pass is wanted, so a project with three.js alone
// builds and runs (vite.config.js turns a missing one into an empty module), and a page that does not want the pass
// never downloads it.
import * as THREE from 'three';

// On by default where the GPU can afford a full-screen pass at device resolution: not on a phone, and not on a software
// renderer, where one frame takes seconds (the headless browser check_site uses is one). ?ao=1 in the address forces
// it on, for a look on a slow machine; ?ao=0 forces it off. A page that reads a GPU tier with detect-gpu (libraries.md)
// can pass it: tier 2 and up is a GPU that can.
export function wanted(renderer, { phone = false, tier = null } = {}) {
  const ask = new URLSearchParams(location.search).get('ao');
  if (ask === '1') return { on: true, why: 'on: forced by ?ao=1' };
  if (ask === '0') return { on: false, why: 'off: ?ao=0' };
  if (tier !== null) return tier >= 2 ? { on: true, why: `on: GPU tier ${tier}` } : { on: false, why: `off: GPU tier ${tier}` };
  if (phone) return { on: false, why: 'off: a phone' };
  const gl = renderer.getContext();
  const debug = gl.getExtension('WEBGL_debug_renderer_info');
  const name = String(gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
  if (/SwiftShader|llvmpipe|softpipe|software|Basic Render/i.test(name)) return { on: false, why: `off: a software renderer (${name}); ?ao=1 in the address shows it` };
  return { on: true, why: 'on' };
}

export async function occlusion(renderer, scene, camera, options = {}) {
  const want = wanted(renderer, options);
  if (!want.on) return { pass: null, why: want.why };
  let pp, n8;
  try {
    [pp, n8] = await Promise.all([import('postprocessing'), import('n8ao')]);
  } catch {
    pp = { missing: true }; // without a bundler, a bare name nothing maps fails to resolve
  }
  if (pp.missing || n8.missing) return { pass: null, why: 'off: postprocessing and n8ao are not both installed' };

  // The composer draws into buffers of its own: multisampled, since the canvas's own antialiasing no longer applies,
  // and half float, so light above 1 survives until the tone mapping at the end.
  const composer = new pp.EffectComposer(renderer, {
    frameBufferType: THREE.HalfFloatType, multisampling: Math.min(4, renderer.capabilities.maxSamples) });
  composer.addPass(new pp.RenderPass(scene, camera));
  const ao = new n8.N8AOPostPass(scene, camera, 1, 1);
  // In metres: contact within about half a metre darkens, which is the scale of the props and of slabs on a floor.
  // Half resolution, upsampled along depth edges: a quarter of the cost, and the occlusion is soft anyway.
  Object.assign(ao.configuration, { aoRadius: 0.5, distanceFalloff: 0.5, intensity: 2.5, halfRes: true,
    gammaCorrection: false }); // the effect pass after it writes sRGB
  composer.addPass(ao);
  composer.addPass(new pp.EffectPass(camera, new pp.ToneMappingEffect({ mode: pp.ToneMappingMode.NEUTRAL })));
  return {
    why: want.why,
    pass: { render: () => composer.render(), setSize: (w, h) => composer.setSize(w, h, false), dispose: () => composer.dispose() },
  };
}
