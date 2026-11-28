// kit.js: small helpers for building a set. A set is a few primitives; light, haze and shadow make the picture, and
// the print (print.js) turns it into the story's look, so a set needs shapes that catch light, not detail.
// Two ways to colour and light one: `paint` and `rig` take the theme's colours and light and follow a change of theme
// (a deck that uses only these can be shown in any theme); `mat` and `spot` take literal colours and a light the set
// places itself (a film whose look is its own).
import * as THREE from 'three';
import { PROPS, loadProp } from './props.js';
import { THEMES, DEFAULT_THEME } from './theme.js';

// A surface in a literal colour. Rough by default: in a print, a matt surface shows where the light falls, a shiny
// one shows highlights.
export function mat(colour, roughness = 0.85) {
  return new THREE.MeshStandardMaterial({ color: colour, roughness, metalness: 0 });
}

// ---- the theme: what paint and rig follow. film.js reads it from the page (theme.js) and hands it over before any
// scene is built, and again when the theme changes; until then the default theme's built-in colours stand.
let theme = { ...THEMES[DEFAULT_THEME], name: DEFAULT_THEME };
const painted = []; // every paint made: { material, token, roughness }
const rigs = [];    // every rig made
export const currentTheme = () => theme;
export function useTheme(next) {
  theme = next;
  for (const p of painted) tint(p);
  for (const r of rigs) r.apply();
}
function tint(p) {
  p.material.color.set(theme.colours[p.token]);
  if ('roughness' in p.material) p.material.roughness = p.roughness ?? theme.roughness;
}

// A surface in a theme colour: 'lit' (what sets are made of: in the light it is the theme's lit colour, out of it the
// shade), 'shade', 'accent' (the thing the story follows), 'accent-2', 'paper', 'paper-2', 'ink', 'muted'. It follows
// the theme: on a change its colour changes, and its roughness unless it names one. `flat: true` is a surface the
// light does not model (a wire, a mark, a tick of a ruler): the colour exactly, everywhere.
export function paint(token, { roughness = null, flat = false, ...rest } = {}) {
  if (!(token in theme.colours)) throw new Error(`no theme colour "${token}"`);
  const material = flat ? new THREE.MeshBasicMaterial(rest) : new THREE.MeshStandardMaterial({ metalness: 0, ...rest });
  const p = { material, token, roughness };
  painted.push(p);
  tint(p);
  return material;
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
// A prism with a triangular end, its ridge along x, standing on (x, y, z): a roof, a ramp, a wedge. `w` along the
// ridge, `h` to the ridge, `d` across.
export function prism(w, h, d, material, x = 0, y = 0, z = 0) {
  const a = [-w / 2, 0, -d / 2], b = [-w / 2, 0, d / 2], c = [-w / 2, h, 0], a2 = [w / 2, 0, -d / 2], b2 = [w / 2, 0, d / 2], c2 = [w / 2, h, 0];
  const faces = [a, b, c, a2, c2, b2, b, b2, c2, b, c2, c, a2, a, c, a2, c, c2, a, a2, b2, a, b2, b];
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(faces.flat(), 3));
  geometry.computeVertexNormals(); // no shared corners, so each face is flat
  return part(geometry, material, x, y, z);
}
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

// The sky of a rig: the theme's paper overhead, running into another colour at the horizon, and a third below it
// (the horizon's again in clear air, where the ground covers it; a darker one in thick air, which has no ground). It
// is drawn at the far plane wherever nothing else is, turns with the camera and never comes nearer, and writes no
// depth (the glyph print finds edges in the depth, and the sky has none).
function dome() {
  const material = new THREE.ShaderMaterial({
    uniforms: { top: { value: new THREE.Color() }, horizon: { value: new THREE.Color() }, below: { value: new THREE.Color() } },
    vertexShader: 'varying vec3 vDir; void main() { vDir = position; vec4 p = projectionMatrix * vec4(mat3(viewMatrix) * position, 1.0); gl_Position = p.xyww; }',
    fragmentShader: 'uniform vec3 top, horizon, below; varying vec3 vDir; void main() { float h = normalize(vDir).y; gl_FragColor = vec4(mix(horizon, h > 0.0 ? top : below, smoothstep(0.0, 0.45, abs(h))), 1.0); }',
    side: THREE.BackSide, depthWrite: false, fog: false,
  });
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 12), material);
  mesh.frustumCulled = false;
  mesh.renderOrder = 1000; // after everything solid, so it costs only the pixels that show it
  return mesh;
}

// How much of the fill reaches a surface that faces down: a little less, so the underside of a thing is its darkest.
const UNDER = 0.82;
// Thick air (rig.haze) takes this share of the theme's lit colour at most, and overhead OVERHEAD times as much; and
// never so much that the page's type, which lies over it, has less than READABLE to 1 against it at its lightest.
const AIR_LIT = 0.25, OVERHEAD = 2.2, READABLE = 4.5;
// WCAG's relative luminance of a colour (a THREE.Color holds linear values) and the contrast of two luminances.
const lum = (c) => 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b;
const contrast = (a, b) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
const share = (a, b) => (b > 1e-4 ? Math.min(1, Math.max(0, a / b)) : 1);
const Y = new THREE.Vector3(0, 1, 0);

// The theme's light for a scene: a key light that casts the shadows, a fill, the sky and the air. It follows the
// theme. The two lights are set so that a `lit` surface that faces the key squarely is exactly the theme's lit colour
// and one the key does not reach is its shade (times the theme's `fill`); every other paint keeps the same ratio.
// - shadow: the shadow map's size (0 for none). One light casts, so a frame draws the set twice at most.
// - from: the direction towards the light, where the set needs it somewhere (a sun that lays a shadow on a certain
//   spot); the theme's own direction when left out.
// - reach: the set's size in metres: where the air has taken the ground (the theme says how much), and the first
//   shadow box.
// - height: the height of the tallest thing that casts a shadow, so the box takes it in.
// It returns { key, fill, sky, fog, direction } and three things a scene's update may call, each absolute (a function
// of what it is given): cover(x, y, z, radius) puts the shadow box round a centre (the map's pixels are spent on
// 2 x radius metres: small round a close subject, large for a vista); haze(amount, within) thickens the air towards
// the page's paper until nothing shows beyond `within` metres (inside a cloud, smoke, deep water); plain(size) makes
// the ground.
export function rig(scene, { shadow = 2048, from = null, reach = 40, height = 0 } = {}) {
  if (light && shadow) shadow = Math.max(512, shadow >> 1); // drawn in software: a shadow map half the size (film.js)
  const key = new THREE.DirectionalLight(0xffffff, Math.PI), fill = new THREE.HemisphereLight(0xffffff, 0xffffff, Math.PI);
  const sky = dome(), fog = new THREE.FogExp2(0xffffff, 0);
  if (shadow) { key.castShadow = true; key.shadow.mapSize.set(shadow, shadow); }
  scene.fog = fog;
  scene.add(key, key.target, fill, sky);
  const direction = new THREE.Vector3(), u = new THREE.Vector3(), v = new THREE.Vector3();
  const top = sky.material.uniforms.top.value, horizon = sky.material.uniforms.horizon.value, below = sky.material.uniforms.below.value;
  const clearTop = new THREE.Color(), clearHorizon = new THREE.Color(), far = new THREE.Color(), c2 = new THREE.Color();
  const thick = new THREE.Color(), thickTop = new THREE.Color(), thickLow = new THREE.Color();
  const plains = [];
  const box = { x: 0, y: 0, z: 0, radius: reach }, air = { amount: 0, within: reach };
  let clear = 0; // the fog's density in clear air
  const self = {
    key, fill, sky, fog, direction,
    // Takes the current theme: called when the rig is made and on every change of theme.
    apply() {
      const c = theme.colours;
      // the fill is the share of the lit colour that the shade keeps, colour by colour, and the key is the rest
      clearTop.set(c.lit);
      c2.set(c.shade);
      fill.color.setRGB(share(c2.r, clearTop.r), share(c2.g, clearTop.g), share(c2.b, clearTop.b)).multiplyScalar(theme.fill);
      fill.groundColor.copy(fill.color).multiplyScalar(UNDER);
      key.color.setRGB(1 - fill.color.r, 1 - fill.color.g, 1 - fill.color.b);
      key.intensity = Math.PI * theme.key.intensity;
      direction.fromArray(from ?? theme.key.from).normalize();
      u.crossVectors(Y, direction);
      if (u.lengthSq() < 1e-6) u.set(1, 0, 0);
      u.normalize();
      v.crossVectors(direction, u);
      if (shadow) key.shadow.radius = theme.key.softness;
      // The sky is the page's paper overhead and, in a gradient, a quarter of the way to the ground's colour at the
      // horizon; the air takes the far ground half of the way to that. So the horizon is a soft line: the plain keeps
      // some of its own tone to its edge.
      clearTop.set(c.paper);
      clearHorizon.copy(clearTop);
      if (theme.background === 'gradient') clearHorizon.lerp(c2.set(c['paper-2']), 0.25);
      far.set(c['paper-2']).lerp(clearHorizon, 0.5);
      // Thick air. Where the background is a gradient it is the page's paper with some of the lit colour in it,
      // more of it overhead and some of the shade below: light comes down through it. A slide's words lie over it,
      // so it takes only as much of the lit colour as leaves the page's type readable where the air is lightest: a
      // quarter in a light theme, whose lit colour is close to its paper, and less in a dark one, whose lit colour
      // is far from it. Where the background is flat it is the paper itself: a flat print has no colour for "a
      // little lighter", and a print of characters would fill the air with them.
      thick.set(c.paper);
      thickTop.copy(thick);
      thickLow.copy(thick);
      if (theme.background === 'gradient') {
        const type = lum(c2.set(c.ink));
        c2.set(c.lit);
        let take = AIR_LIT;
        while (take > 0.01 && contrast(lum(thickTop.set(c.paper).lerp(c2, take * OVERHEAD)), type) < READABLE) take *= 0.9;
        thick.lerp(c2, take);
        thickTop.set(c.paper).lerp(c2, take * OVERHEAD);
        thickLow.copy(thick).lerp(c2.set(c.shade), 0.45);
      }
      clear = theme.fog / reach;
      // the ground is exactly its colour where the key falls on it: its paint is divided by the light it gets there
      const k = theme.key.intensity * Math.max(direction.y, 0);
      for (const p of plains) {
        c2.set(c[p.token]);
        p.material.color.setRGB(c2.r / (fill.color.r + key.color.r * k), c2.g / (fill.color.g + key.color.g * k), c2.b / (fill.color.b + key.color.b * k));
      }
      self.haze(air.amount, air.within);
      self.cover(box.x, box.y, box.z, box.radius);
    },
    cover(x, y, z, radius) {
      box.x = x; box.y = y; box.z = z; box.radius = radius;
      if (!shadow) return;
      // the centre moves in whole pixels of the map across the light, so a slow move does not make an edge crawl
      const texel = (2 * radius) / shadow, a = x * u.x + y * u.y + z * u.z, b = x * v.x + y * v.y + z * v.z;
      const da = Math.round(a / texel) * texel - a, db = Math.round(b / texel) * texel - b;
      x += u.x * da + v.x * db; y += u.y * da + v.y * db; z += u.z * da + v.z * db;
      const back = radius + height / Math.max(direction.y, 0.05), cam = key.shadow.camera;
      key.target.position.set(x, y, z);
      key.position.set(x + direction.x * back, y + direction.y * back, z + direction.z * back);
      cam.left = cam.bottom = -radius;
      cam.right = cam.top = radius;
      cam.near = 0.5;
      cam.far = back + radius;
      cam.updateProjectionMatrix();
      // a lit surface must not shadow itself: by two or three pixels of the map, whatever the box's size
      key.shadow.normalBias = texel * 2.5;
      key.shadow.bias = (-1.5 * texel) / (cam.far - cam.near);
    },
    haze(amount = 0, within = reach) {
      air.amount = amount = Math.min(1, Math.max(0, amount));
      air.within = within;
      fog.color.copy(far).lerp(thick, amount);
      top.copy(clearTop).lerp(thickTop, amount);
      horizon.copy(clearHorizon).lerp(thick, amount);
      below.copy(clearHorizon).lerp(thickLow, amount);
      const thin = Math.max(clear, 1e-7);
      fog.density = amount <= 0 ? clear : thin * Math.pow(1 / Math.max(within, 1e-3) / thin, amount);
    },
    // The ground: a plain `size` metres on a side in a theme colour, exactly that colour where the key falls on it
    // (the words of a slide lie on it), darker in a shadow by the same ratio as every paint.
    plain(size, token = 'paper-2') {
      const material = new THREE.MeshLambertMaterial();
      plains.push({ material, token });
      self.apply();
      // Small squares in the middle and squares twice as large with every ring outwards: a camera standing on a
      // plain of two triangles kilometres long sees it at the wrong depth (their corners are too far off for the
      // arithmetic), and what stands on it sinks into it.
      const rings = 12, geometry = new THREE.PlaneGeometry(1, 1, rings * 2, rings * 2), at = geometry.attributes.position;
      const spread = (u) => Math.sign(u) * (size / 2) * (Math.pow(2, Math.abs(u) * 2 * rings) - 1) / (Math.pow(2, rings) - 1);
      for (let i = 0; i < at.count; i++) at.setXY(i, spread(at.getX(i)), spread(at.getY(i)));
      const mesh = new THREE.Mesh(geometry, material);
      mesh.rotation.x = -Math.PI / 2;
      mesh.receiveShadow = true;
      return mesh;
    },
  };
  rigs.push(self);
  self.apply();
  return self;
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
let renderer = null, light = false;
export const useRenderer = (r, { light: l = false } = {}) => { renderer = r; light = l; };

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

// Stands the camera `distance` from the subject `at`, in the direction `from` (from the subject towards the camera),
// and puts the subject at (nx, ny) of the frame: -1..1 across and up, (0, 0) the centre. A subject at (0.35, 0.2)
// leaves the lower left of the frame to a slide's words. The camera is moved sideways, not turned, so the subject is
// seen from the same side wherever it stands in the frame. Call it after fit (it uses the field of view).
const _f = new THREE.Vector3(), _r = new THREE.Vector3(), _u = new THREE.Vector3();
export function look(camera, at, from, distance, nx = 0, ny = 0) {
  camera.position.copy(at).addScaledVector(_f.copy(from).normalize(), distance);
  camera.lookAt(at);
  const half = Math.tan((camera.fov * Math.PI) / 360) * distance;
  _r.set(1, 0, 0).applyQuaternion(camera.quaternion);
  _u.set(0, 1, 0).applyQuaternion(camera.quaternion);
  camera.position.addScaledVector(_r, -nx * half * camera.aspect).addScaledVector(_u, -ny * half);
}

// A view: where the camera stands (`eye`), what it looks at (`aim`), its field of view and its near plane. A scene
// that moves its camera makes two and goes between them with move().
export const view = () => ({ eye: new THREE.Vector3(), aim: new THREE.Vector3(), fov: 40, near: 0.5 });
// The camera at x (0..1, ease it yourself) of the way from view a to view b. The direction turns evenly, but the
// distance to the aim goes in proportion, so a move from a kilometre off to a few metres off is as even at its end
// as at its start (a straight line would spend it all in the last tenth); and the aim goes from one subject to the
// other as that distance closes, so the camera has turned to the new subject by the time it is near it.
const _a = new THREE.Vector3(), _b = new THREE.Vector3(), _aim = new THREE.Vector3();
export function move(camera, a, b, x) {
  const la = _a.subVectors(a.eye, a.aim).length(), lb = _b.subVectors(b.eye, b.aim).length();
  const l = la * Math.pow(lb / la, x);
  _a.divideScalar(la).lerp(_b.divideScalar(lb), x).normalize();
  _aim.lerpVectors(a.aim, b.aim, Math.abs(la - lb) > 1e-6 * (la + lb) ? (la - l) / (la - lb) : x);
  camera.position.copy(_aim).addScaledVector(_a, l);
  camera.fov = a.fov + (b.fov - a.fov) * x;
  camera.near = a.near * Math.pow(b.near / a.near, x);
  camera.lookAt(_aim);
}

// Smoothstep from 0 to 1, clamped: every move in a film starts and ends at rest.
export function ease(x) { x = Math.min(1, Math.max(0, x)); return x * x * (3 - 2 * x); }
// A seeded generator 0..1: the same set every load, and at update time a value that depends only on its seed.
export function rnd(seed) { let s = (Math.abs(Math.floor(seed)) % 2147483646) + 1; return () => (s = (s * 16807) % 2147483647, (s - 1) / 2147483646); }
