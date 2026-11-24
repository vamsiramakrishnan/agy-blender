// occlusion.js: ambient occlusion, switched on rather than written. occlusion(renderer, scene, camera, { phone }) ->
// { pass, why }: pass draws the frame through postprocessing with an N8AO pass (render(), setSize(w, h), dispose(),
// maxPixelRatio(w, h)), or is null, and `why` says which and why (window.__site.info().occlusion).
//
// Ambient occlusion darkens where surfaces meet: slabs stacked on each other, a prop's foot on the floor. Without it
// run 3's stacked slabs showed no contact at all and read as toy blocks, although postprocessing and n8ao were
// installed. The packages are imported only here and only when the pass is wanted, so a project with three.js alone
// builds and runs (vite.config.js turns a missing one into an empty module), and a page that does not want the pass
// never downloads it.
//
// What it costs, measured with check_site's own record on the starter at 1440 x 900 and a pixel ratio of 1: textures
// 38 MB without the pass (34 of it the 2048 px shadow map), 133 MB with it, plus 7 MB of render buffers; about 80
// bytes of graphics memory per pixel drawn. With the composer's multisampling (4 samples), as this module first had
// it, its two multisampled buffers held another 124 MB at that size, which is why it has none and smooths edges with
// SMAA instead: at 4x zoom a silhouette edge is as smooth either way.
import * as THREE from 'three';

// Set (empty) on <html> by check_site before any script of the page runs: this browser stands in for a visitor's,
// draw what a visitor with a GPU would see although it renders in software; time does not matter.
export const CAMERA_ATTR = 'data-agy3d-camera';

// On by default where the GPU can afford a full-screen pass at device resolution: not on a phone, and not on a software
// renderer, where every frame is drawn on the CPU (the whole desktop check of the starter takes about 30 s with the
// pass against 18 s without), unless check_site asks for it (CAMERA_ATTR): its frames and memory are then those of a
// visitor with a GPU. ?ao=1 in the address forces it on, for a look on a slow machine; ?ao=0 forces it off. A page that
// reads a GPU tier with detect-gpu (libraries.md) can pass it: tier 2 and up is a GPU that can.
export function wanted(renderer, { phone = false, tier = null } = {}) {
  const ask = new URLSearchParams(location.search).get('ao');
  if (ask === '1') return { on: true, why: 'on: forced by ?ao=1' };
  if (ask === '0') return { on: false, why: 'off: ?ao=0' };
  if (tier !== null) return tier >= 2 ? { on: true, why: `on: GPU tier ${tier}` } : { on: false, why: `off: GPU tier ${tier}` };
  if (phone) return { on: false, why: 'off: a phone' };
  const gl = renderer.getContext();
  const debug = gl.getExtension('WEBGL_debug_renderer_info');
  const name = String(gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
  if (/SwiftShader|llvmpipe|softpipe|software|Basic Render/i.test(name)) {
    if (document.documentElement.hasAttribute(CAMERA_ATTR)) {
      return { on: true, why: `on: ${CAMERA_ATTR} asks for what a visitor with a GPU sees (this renderer is ${name})` };
    }
    return { on: false, why: `off: a software renderer (${name}); ?ao=1 in the address shows it` };
  }
  return { on: true, why: 'on' };
}

// The drawing buffer while the pass is on: a pixel ratio of at most 1.5 and at most 3 million pixels. Each pixel drawn
// costs the pass's buffers about 80 bytes, so the starter's own cap of 2 would give a 1440 x 900 window at a device
// scale of 2 (5.2 million pixels) some 410 MB for the pass alone, and a 4K screen at a ratio of 1 (8.3 million) more
// than 650 MB. Measured with the cap at that window and scale (2160 x 1350 drawn): 252 MB of textures and 15 MB of
// render buffers in all. The occlusion is drawn at half resolution and is soft anyway; what a lower ratio loses is some
// sharpness of fine detail, and SMAA keeps its edges from stepping.
const PASS_RATIO = 1.5, PASS_PIXELS = 3e6;

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

  // The composer draws into buffers of its own, so the canvas's own antialiasing no longer applies. Half float, so
  // light above 1 survives until the tone mapping; no multisampling (see the head of this file for what it cost).
  const composer = new pp.EffectComposer(renderer, { frameBufferType: THREE.HalfFloatType, multisampling: 0 });
  composer.addPass(new pp.RenderPass(scene, camera));
  const ao = new n8.N8AOPostPass(scene, camera, 1, 1);
  // In metres: contact within about half a metre darkens, which is the scale of the props and of slabs on a floor.
  // Half resolution, upsampled along depth edges: a quarter of the cost, and the occlusion is soft anyway.
  Object.assign(ao.configuration, { aoRadius: 0.5, distanceFalloff: 0.5, intensity: 2.5, halfRes: true,
    gammaCorrection: false }); // the last effect pass writes sRGB
  composer.addPass(ao);
  composer.addPass(new pp.EffectPass(camera, new pp.ToneMappingEffect({ mode: pp.ToneMappingMode.NEUTRAL })));
  // Antialiasing in a pass of its own after the tone mapping, so it finds edges in the picture as it is shown (before
  // it, a bright edge above 1 would be smoothed against values the tone mapping then compresses). Its two buffers of 8
  // bits per channel cost 10 MB at 1440 x 900.
  composer.addPass(new pp.EffectPass(camera, new pp.SMAAEffect({ preset: pp.SMAAPreset.MEDIUM })));
  return {
    why: want.why,
    pass: {
      render: () => composer.render(),
      setSize: (w, h) => composer.setSize(w, h, false),
      dispose: () => composer.dispose(),
      maxPixelRatio: (w, h) => Math.min(PASS_RATIO, Math.sqrt(PASS_PIXELS / Math.max(1, w * h))),
    },
  };
}
