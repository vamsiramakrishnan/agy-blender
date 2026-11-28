// kit.js: small helpers for building a set. A set is a few primitives; light, haze and shadow make the picture, and
// the print (print.js) turns it into the film's look, so a set needs shapes that catch light, not detail.
import * as THREE from 'three';
import { PROPS, loadProp } from './props.js';

// A surface. Rough by default: in a print, a matt surface shows where the light falls, a shiny one shows highlights.
export function mat(colour, roughness = 0.85) {
  return new THREE.MeshStandardMaterial({ color: colour, roughness, metalness: 0 });
}

// Every part of a set casts and receives shadow unless told otherwise: shadow is half of the drawing.
function part(geometry, material, x, y, z) {
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.set(x, y, z);
  mesh.castShadow = mesh.receiveShadow = true;
  return mesh;
}
// A block, centred on (x, y, z): walls, desks, rocks, a book.
export const box = (w, h, d, material, x = 0, y = 0, z = 0) => part(new THREE.BoxGeometry(w, h, d), material, x, y, z);
// A cylinder or a cone (top and bottom radius), centred on (x, y, z): towers, posts, lamps, a pedestal.
export const cyl = (top, bottom, h, material, x = 0, y = 0, z = 0, open = false) =>
  part(new THREE.CylinderGeometry(top, bottom, h, 28, 1, open), material, x, y, z);
// A flat ground or wall, lying flat (facing up) unless `upright`; it only receives shadow.
export function plane(w, h, material, upright = false) {
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material);
  if (!upright) mesh.rotation.x = -Math.PI / 2;
  mesh.receiveShadow = true;
  return mesh;
}
// A turned shape from a profile [[radius, height], ...] from the bottom up: a tower, a lens, a bottle, a hull.
export const lathe = (profile, material, segments = 32) =>
  part(new THREE.LatheGeometry(profile.map(([r, y]) => new THREE.Vector2(r, y)), segments), material, 0, 0, 0);

// A spot light from `from` to `to`, added to the scene with its target. Decay 0: its intensity does not fall off with
// distance, only ends at `distance`, so a number means the same thing in every set. `shadow` is the shadow map's
// size (0 for none; at most 2048, and 1024 is plenty under a print's cells).
export function spot(scene, from, to, { intensity = 4, angle = 0.4, penumbra = 0.4, distance = 40, shadow = 0, colour = 0xffffff } = {}) {
  const light = new THREE.SpotLight(colour, intensity, distance, angle, penumbra, 0);
  light.position.set(...from);
  light.target.position.set(...to);
  if (shadow) {
    light.castShadow = true;
    light.shadow.mapSize.set(Math.min(shadow, 2048), Math.min(shadow, 2048));
    light.shadow.camera.near = 0.3;
    light.shadow.bias = -0.0005;
  }
  scene.add(light, light.target);
  return light;
}

// Slats in front of a shadow-casting light: venetian blinds, window bars, a fence. They stand `at` metres from the
// light towards its target, `count` of them `gap` apart, turned by `turn` radians about the beam. One instanced mesh,
// so a window of bars costs one draw. slide(d) moves them across the beam by d metres (for a breeze): absolute, so
// update(s) can call it with a function of s.
export function gobo(light, { count = 9, gap = 0.12, thickness = 0.05, width = 3, turn = 0, at = 1 } = {}) {
  const slats = new THREE.InstancedMesh(new THREE.BoxGeometry(width, thickness, 0.01), new THREE.MeshBasicMaterial({ color: 0 }), count);
  const m = new THREE.Matrix4();
  for (let i = 0; i < count; i++) slats.setMatrixAt(i, m.makeTranslation(0, (i - (count - 1) / 2) * gap, 0));
  slats.castShadow = true;
  const group = new THREE.Group();
  group.add(slats);
  const dir = light.target.position.clone().sub(light.position).normalize();
  const base = light.position.clone().addScaledVector(dir, at);
  group.position.copy(base);
  group.lookAt(light.target.position);
  group.rotateZ(turn);
  light.parent.add(group);
  const across = new THREE.Vector3(0, 1, 0).applyQuaternion(group.quaternion);
  return { group, slide: (d) => group.position.copy(base).addScaledVector(across, d) };
}

// A beam made visible: an additive cone, brightest near its apex and along its axis as seen, fading at its rim. The
// apex is at the origin and the cone opens along -y for `height`; turn it with quaternion.setFromUnitVectors. It
// writes no depth, so it adds light without hiding what is behind it. uniforms.opacity is what update(s) sets.
export function haze(top, bottom, height, opacity) {
  const geometry = new THREE.CylinderGeometry(top, bottom, height, 32, 1, true);
  geometry.translate(0, -height / 2, 0);
  const material = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.BackSide,
    uniforms: { opacity: { value: opacity } },
    vertexShader: 'varying vec2 vUv; varying vec3 vN; void main() { vUv = uv; vN = normalize(normalMatrix * normal); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: 'uniform float opacity; varying vec2 vUv; varying vec3 vN; void main() { float e = pow(abs(vN.z), 1.6); float along = mix(0.08, 1.0, pow(vUv.y, 1.8)); gl_FragColor = vec4(vec3(opacity * e * along), 1.0); }',
  });
  return new THREE.Mesh(geometry, material);
}

// A canvas texture of lit windows, for a far town or a building at night: a grid of windows a third of them lit, each
// at its own brightness, the same for the same seed. Nearest filtering keeps each window a hard square.
export function windows(seed, cols = 16, rows = 4) {
  const c = document.createElement('canvas');
  c.width = cols * 8; c.height = rows * 8;
  const g = c.getContext('2d'), r = rnd(seed);
  g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height);
  for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) {
    if (r() > 0.34) continue;
    const v = Math.floor(140 + r() * 115);
    g.fillStyle = `rgb(${v},${Math.floor(v * 0.86)},${Math.floor(v * 0.62)})`;
    g.fillRect(i * 8 + 2, j * 8 + 2, 4, 5);
  }
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.magFilter = THREE.NearestFilter;
  return t;
}

// A canvas texture of large type, for a title or an end card set in the scene, lit by its lights and printed like the
// rest. Light letters on a dark ground: the light that falls on the card is what draws the letters. Heavy by default
// (weight 900): to survive a cell print a stroke must be about two cells wide and a letter about ten cells high. On a
// phone in the glyph print (4 px cells, about 90 columns) six letters across the frame reach only eight or nine rows
// high, at the limit: a title for a phone is a short word, or one word a line.
export function card(lines, { width = 1024, height = 512, size = 220, weight = 900, ink = '#f2ede0', ground = '#2b2e35', font = 'Georgia, "Times New Roman", serif', spacing = 0.12 } = {}) {
  const c = document.createElement('canvas');
  c.width = width; c.height = height;
  const g = c.getContext('2d');
  g.fillStyle = ground; g.fillRect(0, 0, width, height);
  g.fillStyle = ink; g.textAlign = 'center'; g.textBaseline = 'middle';
  // The type shrinks until the widest line fits 92 % of the card: a long word must not run off the card's edges.
  const setSize = (px) => {
    g.font = `${weight} ${px}px ${font}`;
    if ('letterSpacing' in g) g.letterSpacing = `${Math.round(px * spacing)}px`;
  };
  setSize(size);
  const widest = Math.max(...lines.map((line) => g.measureText(line).width));
  if (widest > width * 0.92) { size = Math.floor((size * width * 0.92) / widest); setSize(size); }
  lines.forEach((line, i) => g.fillText(line, width / 2 + (size * spacing) / 2, height / 2 + (i - (lines.length - 1) / 2) * size * 1.05));
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

// A person about 1.8 m tall, standing on the origin and facing +z: a turned coat, legs, arms, a head and a cap. Seven
// parts: enough for a silhouette and a shadow, which is all a print shows of a person.
export function figure({ coat = '#4a5160', dark = '#1d2027', skin = '#c4b3a0' } = {}) {
  const g = new THREE.Group(), coatM = mat(coat), darkM = mat(dark, 0.7), skinM = mat(skin, 0.7);
  const body = lathe([[0.31, 0.42], [0.28, 0.8], [0.22, 1.12], [0.25, 1.32], [0.27, 1.45], [0.2, 1.53], [0.09, 1.57], [0, 1.57]], coatM, 24);
  body.scale.set(1.1, 1, 0.66);
  const arm = (x) => { const a = cyl(0.07, 0.065, 0.64, coatM, x, 1.12, 0.02); a.rotation.z = -Math.sign(x) * 0.1; return a; };
  const head = part(new THREE.SphereGeometry(0.11, 16, 12), skinM, 0, 1.69, 0.01);
  g.add(body, cyl(0.085, 0.075, 0.42, darkM, -0.11, 0.21, 0), cyl(0.085, 0.075, 0.42, darkM, 0.11, 0.21, 0), arm(-0.37), arm(0.37), head,
    cyl(0.12, 0.125, 0.08, darkM, 0, 1.79, 0));
  return g;
}

// A prop built by the plugin (props.js): a Promise of a Group whose origin is at the prop's base. Until it is
// installed, or if it fails to load, the scene's own stand-in (`standIn()`, made of primitives so the film looks
// finished) or else a grey block of the entry's size.
// `shown` records what each prop is on screen ('installed', 'stand-in' or 'failed'): info().props, for a look at
// whether an install took.
export const shown = {};
export async function prop(name, { standIn } = {}) {
  const entry = PROPS[name];
  if (!entry) throw new Error(`props.js has no prop "${name}"`);
  const fallback = () => {
    if (standIn) return standIn();
    const [w, h, d] = entry.size, g = new THREE.Group();
    g.add(box(w, h, d, mat('#8a8f96', 0.6), 0, h / 2, 0));
    return g;
  };
  if (!entry.module) { shown[name] = 'stand-in'; return fallback(); }
  try {
    const object = await loadProp(entry, renderer);
    object.traverse((o) => { if (o.isMesh) o.castShadow = o.receiveShadow = true; });
    shown[name] = 'installed';
    return object;
  } catch (e) {
    if (shown[name] !== 'failed') console.warn(`prop ${name} did not load, its stand-in shows:`, e && e.message ? e.message : e);
    shown[name] = 'failed';
    return fallback();
  }
}
// The renderer a loader may need (for compressed textures); film.js sets it before any scene is created.
let renderer = null;
export const useRenderer = (r) => { renderer = r; };

// Fits a subject `width` x `height` metres into the frame: sets the camera's field of view for the picture's shape
// (wider in portrait, so a phone does not crop the subject to a slit) and returns the distance at which the subject
// just fills the frame. Multiply the distance to leave room around it.
export function fit(camera, aspect, width, height, fovLandscape = 36, fovPortrait = 52) {
  camera.fov = aspect < 1 ? fovPortrait : fovLandscape;
  camera.aspect = aspect;
  camera.updateProjectionMatrix();
  const t = Math.tan((camera.fov * Math.PI) / 360);
  return Math.max(height / (2 * t), width / (2 * t * aspect));
}

// Smoothstep from 0 to 1, clamped: every move in a film starts and ends at rest.
export function ease(x) { x = Math.min(1, Math.max(0, x)); return x * x * (3 - 2 * x); }
// A seeded generator 0..1: the same set every load, and at update time a value that depends only on its seed.
export function rnd(seed) { let s = (Math.abs(Math.floor(seed)) % 2147483646) + 1; return () => (s = (s * 16807) % 2147483647, (s - 1) / 2147483646); }
