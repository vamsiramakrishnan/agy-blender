# Libraries for a scroll-driven 3D site

What each library is for, when not to use it, the version this guide was checked against, and one example that ran.
Use only what the project already has installed: a sandbox without network cannot add a package, so read the
project's `package.json` first and pick from what is there.

Last verified 2026-11-24. Versions are those of the round-eight seed workspace (read from the npm registry that day).
Every example below was built with Vite 8.3.8 and run in headless Chromium (software WebGL) against exactly these
versions, with no error and no warning in the console; the Node example ran on Node 22. Each example is a whole file
that replaces the starter's `src/main.js` in the starter's page (`starter/index.html`: a `<canvas id="stage">` and
`<section data-chapter>` chapters), so it may import the starter's own modules. The line after each example says what
the run checked. To run them again: the repository's `tests/test_scroll_site_libraries.py`, with
`AGY3D_SITE_SEED` pointing at a workspace that has these packages installed.

| Library | Version | Use it for | Not for |
|---|---|---|---|
| three | 0.187.0 | the scene: `WebGLRenderer` for a site | a renderer you write yourself |
| gsap + ScrollTrigger | 3.15.1 | page effects tied to scroll: panels, pinning, counters | the scene's state (see the starter) |
| lenis | 1.3.28 | smooth wheel scrolling on a desktop | touch (it leaves touch native by default; keep it so) |
| postprocessing + n8ao | 6.39.6, 2.1.0 | ambient occlusion, a little bloom, tone mapping, SMAA | a phone at full resolution; effects that hide a weak scene |
| troika-three-text | 0.53.1 | sharp text placed in the 3D scene (labels on parts) | paragraphs: those stay HTML |
| three-custom-shader-material | 6.4.0 | changing a standard material in a shader and keeping its light and shadows | a full-screen effect |
| three-mesh-bvh | 0.9.18 | fast picking and hover on dense models | a scene of a few boxes |
| detect-gpu | 5.0.70 | choosing a quality level before the first frame | deciding whether to show 3D at all |
| @react-three/fiber + drei | 9.8.2, 10.7.9 | a project that is already React | a plain page (adds React for nothing) |
| glTF-Transform, meshoptimizer, sharp, ktx2-encoder | 4.5.1, 1.3.0, 0.35.5, 0.6.0 | shrinking a model that did not come from this plugin | the plugin's own props: `install_into_project` already does it |

## three.js 0.187: the renderer

Use `WebGLRenderer`. Everything else in this guide (postprocessing, n8ao, troika, custom shader material, drei's
helpers) is written for it. What moved since 0.170, the version many examples online and older workspaces use:

- `PCFSoftShadowMap` is removed: the renderer warns and uses `PCFShadowMap`, whose `shadow.radius` now softens.
- `THREE.Clock` is deprecated (it warns): use `THREE.Timer` (in 0.170 it was an add-on). A scroll site rarely
  needs either: the scene is a function of scroll, not of time.
- WebGL 1 is gone (since 0.163): check for `webgl2`, not `webgl`, before loading the scene.
- Add-ons are imported from `three/addons/...` (the same files as `three/examples/jsm/...`).

Many small things (slabs, tokens, rows, particles) are one `InstancedMesh`: one geometry, one material, a matrix per
copy, one draw call. Two thousand separate meshes are two thousand draw calls and a slow phone.

```js
// example: three-webgl
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
renderer.toneMapping = THREE.NeutralToneMapping;
renderer.shadowMap.enabled = true; // PCFShadowMap, the default

const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
pmrem.dispose();

const key = new THREE.DirectionalLight('#ffffff', 2.4);
key.position.set(-4, 8, 5);
key.castShadow = true;
key.shadow.radius = 3;
key.shadow.camera.left = key.shadow.camera.bottom = -6;
key.shadow.camera.right = key.shadow.camera.top = 6;
scene.add(key);

const ground = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.MeshStandardMaterial({ color: '#1a1d22' }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

// 2,000 slabs, one draw call (and one more for the shadow map)
const COUNT = 2000;
const slabs = new THREE.InstancedMesh(new THREE.BoxGeometry(0.08, 0.04, 0.3),
  new THREE.MeshStandardMaterial({ color: '#c9c4b8', roughness: 0.5 }), COUNT);
const m = new THREE.Matrix4();
for (let i = 0; i < COUNT; i++) {
  m.makeTranslation((i % 50) * 0.12 - 3, 0.02, Math.floor(i / 50) * 0.35 - 7);
  slabs.setMatrixAt(i, m);
}
slabs.castShadow = true;
scene.add(slabs);

const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 100);
camera.position.set(5, 4, 7);
camera.lookAt(0, 0, -1);
renderer.render(scene, camera); // once: draw again only when something changes
```
<!-- check: picture -->
<!-- check: screen-draws 3 -->

### WebGPURenderer and TSL: when they are worth it

`WebGPURenderer` (import from `three/webgpu`) runs on WebGPU where the browser has it and on WebGL 2 where it does
not; its materials are written in TSL (`three/tsl`), node functions instead of GLSL strings, and its post-processing
is `RenderPipeline` (renamed from `PostProcessing` in 0.183; the old name warns). It is worth it for compute work:
a hundred thousand particles moved on the GPU, a simulation. It is not worth it for a site of a few props, because
pmndrs postprocessing, n8ao, troika, custom shader material and drei's shadow helpers do not work with it.
Call `await renderer.init()` before the first `render` (`renderAsync` is deprecated since 0.181). Where the browser
has no WebGPU (the headless check's Chromium has none) it warns once that it runs on WebGL 2: that is expected.

```js
// example: three-webgpu-tsl
import * as THREE from 'three/webgpu';
import { color, mix, normalLocal, positionLocal, sin, uniform } from 'three/tsl';

const renderer = new THREE.WebGPURenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
await renderer.init();

const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 2));

// A uniform the page sets from scroll: the material's look is a function of it, never of time.
const progress = uniform(0.35);
const material = new THREE.MeshStandardNodeMaterial({ roughness: 0.4 });
material.colorNode = mix(color('#2b6cb0'), color('#e0a33a'), progress);
material.positionNode = positionLocal.add(normalLocal.mul(sin(positionLocal.y.mul(10).add(progress.mul(6.28))).mul(0.05)));
scene.add(new THREE.Mesh(new THREE.IcosahedronGeometry(1, 24), material));

const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(0, 0.5, 5);
renderer.render(scene, camera);
```
<!-- check: picture -->
<!-- check: warning WebGPU is not available, running under WebGL2 backend -->

## GSAP 3.15 with ScrollTrigger: page effects tied to scroll

For the HTML: a panel that fades in as its chapter arrives, a section pinned while its numbers count, a progress bar.
`scrub: true` ties a tween to the scroll position, so it plays backwards when the visitor scrolls back. Do not drive
the 3D scene with GSAP tweens of its own (`gsap.to(camera.position, ...)` started by a trigger): such a tween depends
on when it started, so the scene stops being a function of scroll and cannot be scrubbed or photographed at a given
position. Let the scene read the same scroll position (the starter does); with Lenis, tell ScrollTrigger on every
Lenis scroll and drive Lenis from GSAP's ticker so both move on the same frame.

```js
// example: gsap-scrolltrigger
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import Lenis from 'lenis';

gsap.registerPlugin(ScrollTrigger);
const lenis = new Lenis(); // smoothing is off by itself under reduced motion (respectReducedMotion)
lenis.on('scroll', ScrollTrigger.update);
gsap.ticker.add((seconds) => lenis.raf(seconds * 1000));
gsap.ticker.lagSmoothing(0);

// Each chapter's panel rises in as the chapter arrives, scrubbed to the scroll.
for (const chapter of document.querySelectorAll('[data-chapter]')) {
  gsap.fromTo(chapter.querySelector('.panel'), { opacity: 0.15, y: 40 }, {
    opacity: 1, y: 0, ease: 'none',
    scrollTrigger: { trigger: chapter, start: 'top 85%', end: 'top 35%', scrub: true },
  });
}
// One number for the whole page (0 at the top, 1 at the bottom), for a progress bar.
ScrollTrigger.create({
  trigger: '#story', start: 'top top', end: 'bottom bottom',
  onUpdate: (self) => document.documentElement.style.setProperty('--progress', self.progress.toFixed(4)),
});
```
<!-- check: js (async () => { scrollTo(0, document.documentElement.scrollHeight); await new Promise((r) => setTimeout(r, 1500)); return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--progress')) > 0.95; })() -->

## Lenis 1.3: smooth scrolling

Lenis smooths the scroll position itself, so a page that reads `scrollY` every frame (the starter) gets smooth motion
without easing its own state. Add it on top of the starter as below and change nothing else. In 1.3.28
`respectReducedMotion` is on by default (no smoothing under reduced motion) and touch scrolls natively unless
`syncTouch` is set; leave both so. A panel that scrolls inside itself needs `data-lenis-prevent`, or Lenis takes its
wheel events. Do not add Lenis to a page whose scroll is already smoothed by another library (locomotive-scroll 5 is
Lenis inside).

```js
// example: lenis
import Lenis from 'lenis';

const lenis = new Lenis({ autoRaf: true, anchors: true });
// panels that scroll inside themselves (the starter's phone layout) keep their own wheel
for (const panel of document.querySelectorAll('.panel')) panel.setAttribute('data-lenis-prevent', '');
// a chapter link scrolls smoothly too: lenis.scrollTo('#how')
await import('./src/main.js'); // the starter, unchanged: it reads scrollY, which Lenis now moves smoothly
```
<!-- check: picture -->
<!-- check: js document.documentElement.classList.contains('lenis') && window.__site.mode === 'webgl' -->

## pmndrs postprocessing 6.39 and n8ao 2.1: effects, used sparingly and measured

Ambient occlusion (n8ao) gives contact and depth where a shadow map does not; a little bloom makes emissive parts
read; tone mapping and SMAA finish the picture. Each pass is a full-screen draw at device resolution: measure the draw
calls and the frame on a phone size before keeping one, and drop to fewer passes or a lower resolution there.
postprocessing 6.39.6 declares three `>= 0.168 < 0.188`: check its peer range before moving three past 0.187. With
postprocessing, create the renderer with `antialias: false, stencil: false, depth: false` (the composer has its own
buffers), leave the renderer's tone mapping off and tone map in the effect pass, and turn n8ao's own gamma correction
off because the effect pass already outputs sRGB.

```js
// example: postprocessing-n8ao
import * as THREE from 'three';
import { BloomEffect, EffectComposer, EffectPass, RenderPass, SMAAEffect, ToneMappingEffect, ToneMappingMode } from 'postprocessing';
import { N8AOPostPass } from 'n8ao';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: false, stencil: false, depth: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
renderer.setSize(innerWidth, innerHeight, false);
const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 1.5));
const sun = new THREE.DirectionalLight('#ffffff', 2);
sun.position.set(-3, 6, 4);
scene.add(sun);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(20, 20), new THREE.MeshStandardMaterial({ color: '#2a2e35' }));
ground.rotation.x = -Math.PI / 2;
const block = new THREE.Mesh(new THREE.BoxGeometry(1.2, 1.2, 1.2), new THREE.MeshStandardMaterial({ color: '#8a8f96' }));
block.position.y = 0.6;
const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.15, 32, 16), new THREE.MeshStandardMaterial({ emissive: '#ffb347', emissiveIntensity: 6 }));
lamp.position.set(0.9, 0.15, 0.9);
scene.add(ground, block, lamp);
const camera = new THREE.PerspectiveCamera(35, innerWidth / innerHeight, 0.1, 50);
camera.position.set(3.5, 2.5, 4);
camera.lookAt(0, 0.5, 0);

const composer = new EffectComposer(renderer, { frameBufferType: THREE.HalfFloatType });
composer.addPass(new RenderPass(scene, camera));
const ao = new N8AOPostPass(scene, camera, innerWidth, innerHeight);
ao.configuration.aoRadius = 1.0;
ao.configuration.intensity = 2.5;
ao.configuration.gammaCorrection = false;
composer.addPass(ao);
composer.addPass(new EffectPass(camera,
  new BloomEffect({ luminanceThreshold: 1.0, intensity: 0.6, mipmapBlur: true }),
  new ToneMappingEffect({ mode: ToneMappingMode.NEUTRAL })));
composer.addPass(new EffectPass(camera, new SMAAEffect())); // after tone mapping: SMAA wants the final colours
composer.setSize(innerWidth, innerHeight);
composer.render();
```
<!-- check: picture -->

## troika-three-text 0.53: text in the scene

A label on a part, a number that sits on the object it counts: troika draws it from the font file as signed distance
fields, sharp at any size. Give it a font file the site serves: without `font` it fetches fonts from a CDN, which a
sandbox cannot reach and a strict host forbids. It reads `.ttf`, `.otf` and `.woff` (not `.woff2`). Paragraphs stay
HTML: they must be selectable, translatable and readable without WebGL.

```js
// example: troika-text
import * as THREE from 'three';
import { Text } from 'troika-three-text';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(0, 0.6, 4);

const label = new Text();
label.text = 'sharp at any size';
label.font = './fonts/label.ttf'; // served beside index.html (Vite: public/fonts/label.ttf)
label.fontSize = 0.3;
label.anchorX = 'center';
label.anchorY = 'middle';
label.color = '#e9e7e2';
label.position.set(0, 0.6, 0);
scene.add(label);
label.sync(() => renderer.render(scene, camera)); // draw once the glyphs are ready
```
<!-- check: picture -->

## three-custom-shader-material 6.4: a standard material with a shader inside

To change how a `MeshStandardMaterial` moves or colours its surface (a wave, a reveal, a heat colour) and keep its
lighting, environment and shadows: write only the part that changes, into `csm_Position`, `csm_DiffuseColor`,
`csm_Roughness` and the like. Import the plain three.js build from `three-custom-shader-material/vanilla`. A surface
that moves needs the same change in its depth material, or its shadow stays where the undisplaced surface was.

```js
// example: custom-shader-material
import * as THREE from 'three';
import CustomShaderMaterial from 'three-custom-shader-material/vanilla';

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
renderer.shadowMap.enabled = true;
const scene = new THREE.Scene();
scene.background = new THREE.Color('#101215');
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 1));
const sun = new THREE.DirectionalLight('#ffffff', 2.5);
sun.position.set(-3, 6, 4);
sun.castShadow = true;
scene.add(sun);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(20, 20), new THREE.MeshStandardMaterial({ color: '#2a2e35' }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

const uniforms = { uProgress: { value: 0.4 } }; // set from scroll
const wave = /* glsl */ `
  uniform float uProgress;
  void main() { csm_Position = position + normal * 0.08 * sin(position.y * 10.0 + uProgress * 6.2832); }`;
const material = new CustomShaderMaterial({
  baseMaterial: THREE.MeshStandardMaterial,
  vertexShader: wave,
  fragmentShader: /* glsl */ `
    uniform float uProgress;
    void main() { csm_DiffuseColor = vec4(mix(vec3(0.17, 0.42, 0.69), vec3(0.88, 0.64, 0.23), uProgress), 1.0); }`,
  uniforms,
  roughness: 0.4,
});
const blob = new THREE.Mesh(new THREE.IcosahedronGeometry(0.8, 32), material);
blob.position.y = 1;
blob.castShadow = true;
// the shadow follows the wave
blob.customDepthMaterial = new CustomShaderMaterial({
  baseMaterial: THREE.MeshDepthMaterial, vertexShader: wave, uniforms, depthPacking: THREE.RGBADepthPacking,
});
scene.add(blob);

const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(3, 2.4, 4.5);
camera.lookAt(0, 0.8, 0);
renderer.render(scene, camera);
```
<!-- check: picture -->

## three-mesh-bvh 0.9: picking on dense models

Hover labels and clicks on parts cast rays; on a model of tens of thousands of triangles three tests every triangle.
A bounds tree, built once per geometry, makes each ray a few dozen tests. Set `firstHitOnly` when the nearest hit is
all you need.

```js
// example: three-mesh-bvh
import * as THREE from 'three';
import { acceleratedRaycast, computeBoundsTree, disposeBoundsTree } from 'three-mesh-bvh';

THREE.BufferGeometry.prototype.computeBoundsTree = computeBoundsTree;
THREE.BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree;
THREE.Mesh.prototype.raycast = acceleratedRaycast;

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('stage'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
const scene = new THREE.Scene();
scene.add(new THREE.HemisphereLight('#ffffff', '#30343a', 2));
const geometry = new THREE.IcosahedronGeometry(1, 40); // 33,620 triangles
geometry.computeBoundsTree();
const material = new THREE.MeshStandardMaterial({ color: '#8a8f96' });
const model = new THREE.Mesh(geometry, material);
scene.add(model);
const camera = new THREE.PerspectiveCamera(32, innerWidth / innerHeight, 0.1, 50);
camera.position.set(0, 0, 5);
renderer.render(scene, camera);

const raycaster = new THREE.Raycaster();
raycaster.firstHitOnly = true;
const pointer = new THREE.Vector2();
addEventListener('pointermove', (e) => {
  pointer.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObject(model)[0];
  material.color.set(hit ? '#e0a33a' : '#8a8f96');
  document.body.dataset.hover = hit ? String(hit.faceIndex) : '';
  renderer.render(scene, camera);
});
```
<!-- check: picture -->
<!-- check: js (async () => { dispatchEvent(new PointerEvent('pointermove', { clientX: innerWidth / 2, clientY: innerHeight / 2 })); return document.body.dataset.hover !== '' && document.body.dataset.hover !== undefined; })() -->

## detect-gpu 5.0: choosing a quality level

Read the tier before the first frame and pick the pixel ratio, the shadow map size and the passes from it. Two
rules. Serve its benchmark files yourself (copy `node_modules/detect-gpu/dist/benchmarks` into the served folder and
pass `benchmarksURL`): by default it fetches them from unpkg.com. And never switch the 3D off at a low tier: a
software renderer is in none of its benchmarks, so the headless Chromium that every check uses (SwiftShader) gets
tier 1 with type `FALLBACK`, and a renderer on its block list gets tier 0. A page that hides the scene at a low tier
shows its checks the fallback and not the site. A low tier means a lower quality, not no scene.

```js
// example: detect-gpu
import { getGPUTier } from 'detect-gpu';

const gpu = await getGPUTier({ benchmarksURL: './detect-gpu' }); // the copied benchmarks folder, beside index.html
const QUALITY = { 0: { dpr: 1, shadow: 1024, passes: 0 }, 1: { dpr: 1.5, shadow: 1024, passes: 1 },
  2: { dpr: 2, shadow: 2048, passes: 2 }, 3: { dpr: 2, shadow: 2048, passes: 2 } };
const quality = QUALITY[gpu.tier] ?? QUALITY[0];
document.documentElement.dataset.gpuTier = String(gpu.tier);
document.documentElement.dataset.maxDpr = String(quality.dpr);
```
<!-- check: js document.documentElement.dataset.gpuTier === '1' && document.documentElement.dataset.maxDpr === '1.5' -->

## React Three Fiber 9.8 and drei 10.7: when the project is React

Only for a project that already uses React (React 19 for these versions; fiber 9.8 declares react `>= 19 < 19.4`).
The rules do not change: the scene is a function of scroll, so read the scroll position and set the scene from it,
and draw on demand (`frameloop="demand"` and `invalidate`), not every frame. drei's `ScrollControls` scrolls its
own box instead of the page, which breaks the page's own scroll, links and the article fallback: read the page's
scroll instead, as below (the starter's `shots.js` and `chapters.js` work unchanged). drei's `Environment` presets
download HDR files from a CDN: build the environment from `Lightformer`s, or serve your own file. With three 0.187,
`shadows` alone asks for the removed soft shadow type (a warning): write `shadows="percentage"`. fiber 9.8 creates a
`THREE.Clock`, which three 0.187 warns about once; that warning is fiber's and harmless.

```jsx
// example: r3f-drei
import { createRoot } from 'react-dom/client';
import { useEffect } from 'react';
import { Canvas, useThree } from '@react-three/fiber';
import { ContactShadows, Environment, Lightformer } from '@react-three/drei';
import { measure, timeAt } from './src/chapters.js';
import { evaluate } from './src/shots.js';

function ScrollCamera() {
  const camera = useThree((s) => s.camera);
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    let boxes = measure();
    const ids = boxes.map((b) => b.id);
    const update = () => {
      const s = evaluate(timeAt(boxes, scrollY), ids);
      camera.position.fromArray(s.cam.pos);
      camera.fov = s.cam.fov;
      camera.lookAt(...s.cam.target);
      camera.updateProjectionMatrix();
      invalidate();
    };
    const relayout = () => { boxes = measure(); update(); };
    addEventListener('scroll', update, { passive: true });
    addEventListener('resize', relayout);
    update();
    return () => { removeEventListener('scroll', update); removeEventListener('resize', relayout); };
  }, [camera, invalidate]);
  return null;
}

const host = document.createElement('div');
host.style.cssText = 'position:fixed;inset:0;z-index:0';
document.body.prepend(host);
createRoot(host).render(
  <Canvas frameloop="demand" shadows="percentage" dpr={[1, 2]} camera={{ fov: 30 }}>
    <color attach="background" args={['#101215']} />
    <Environment resolution={128}>
      <Lightformer intensity={2} position={[0, 4, -6]} scale={[10, 4, 1]} />
    </Environment>
    <directionalLight castShadow position={[-4, 8, 5]} intensity={2} />
    <mesh castShadow position={[0, 0.75, 0]}>
      <boxGeometry args={[1.4, 1.5, 0.9]} />
      <meshStandardMaterial color="#8a8f96" />
    </mesh>
    <ContactShadows opacity={0.6} scale={8} blur={2.5} far={2} frames={1} />
    <ScrollCamera />
  </Canvas>,
);
```
<!-- check: picture -->
<!-- check: warning THREE.Clock: This module has been deprecated -->

## glTF-Transform 4.5: shrinking a model that did not come from the plugin

The plugin's props need none of this: deliver them for web_three, install them with `install_into_project`, and the
installer writes the web-sized files and a loader with levels of detail (and compresses with KTX2 and meshopt when the
project has these packages). Use glTF-Transform for a model from elsewhere. WebP textures (through sharp) need no
decoder in the page; meshopt geometry and KTX2 textures do, and both decoders are WebAssembly: on a host that may
not run WebAssembly, use WebP alone.

```js
// example: gltf-transform (node)
import fs from 'node:fs';
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { dedup, meshopt, prune, textureCompress } from '@gltf-transform/functions';
import { MeshoptEncoder } from 'meshoptimizer';
import sharp from 'sharp';

const [input, output] = process.argv.slice(2);
await MeshoptEncoder.ready;
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.encoder': MeshoptEncoder });
const doc = await io.read(input);
await doc.transform(
  dedup(),
  prune(),
  textureCompress({ encoder: sharp, targetFormat: 'webp', resize: [2048, 2048] }), // no decoder needed in the page
  meshopt({ encoder: MeshoptEncoder, level: 'medium' }), // needs MeshoptDecoder (WebAssembly) in the page
);
await io.write(output, doc);
console.log(JSON.stringify({ before: fs.statSync(input).size, after: fs.statSync(output).size }));
```
<!-- check: node out.after < out.before -->

In the page, a meshopt file needs `loader.setMeshoptDecoder(MeshoptDecoder)` with `MeshoptDecoder` from
`three/addons/libs/meshopt_decoder.module.js`; a KTX2 file needs a `KTX2Loader` with its transcoder files served by the
site and `detectSupport(renderer)`. The installer's loader does both for the plugin's props.

## Left out, and why

- **Theatre.js** (`@theatre/core` 0.7.2): no release since May 2024; its timeline is keyed to time, and the scene here
  is keyed to scroll.
- **curtains.js** (8.1.6): no release since May 2024, and it draws only planes over the page.
- **locomotive-scroll** (5.1.1): version 5 is Lenis inside; use Lenis directly. **@studio-freight/lenis** is Lenis's
  old name, deprecated on npm.
- **ScrollMagic**: ScrollTrigger does the same and works with Lenis.
- **@14islands/r3f-scroll-rig**: last release December 2024, and it moves the scene's state into its own scroll
  proxy.
- **camera-controls** (in the seed, 3.1.2): for a camera the visitor drags; a scroll site's camera follows the
  scroll. Use it only for an explicit "turn the model" moment, and give back the scroll's camera after.
- **maath** (in the seed): its `damp` eases values per frame, which makes the picture depend on the path taken; use its
  maths helpers, not the damping, for the scene's state.
- **motion** (in the seed, 14.14): good for React interface animation; for scroll-linked page effects this guide uses
  GSAP, and one library for one job is enough.
- **lygia** (in the seed): a library of shader functions (noise, colour, lighting) to include in your own GLSL or
  TSL. Copy the one function you need rather than its whole tree.
- **lil-gui, stats-gl** (in the seed): development tools. Keep them out of the published page.
