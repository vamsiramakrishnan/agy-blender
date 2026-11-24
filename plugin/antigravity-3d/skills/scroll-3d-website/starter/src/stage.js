// stage.js: the three.js side. createStage(canvas) -> stage; stage.render(state) draws one state of shots.evaluate.
// It keeps nothing between frames that changes the picture: the same state always gives the same frame.
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { PROPS, loadProp, standIn } from './props.js';
import { DESIGN_ASPECT } from './shots.js';

// From the upper left and in front: the shadows fall back and to the right, away from the reader.
const KEY_DIR = new THREE.Vector3(-0.45, 1.0, 0.55).normalize();
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

// The soft dark seat under an object that a shadow map alone does not give: a radial gradient on the ground.
function contactShadow() {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, 'rgba(0,0,0,0.55)');
  grad.addColorStop(0.55, 'rgba(0,0,0,0.22)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const map = new THREE.CanvasTexture(c);
  map.colorSpace = THREE.SRGBColorSpace;
  const geo = new THREE.PlaneGeometry(1, 1);
  geo.rotateX(-Math.PI / 2);
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map, transparent: true, depthWrite: false }));
  mesh.position.y = 0.002;
  mesh.renderOrder = 1;
  return mesh;
}

export function createStage(canvas, { phone = false, onChange = () => {}, onLost = () => {} } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap; // PCFSoftShadowMap was removed in r18x: it warns and falls back

  const scene = new THREE.Scene();
  const bg = new THREE.Color(css('--bg', '#101215'));
  scene.background = bg;
  scene.fog = new THREE.Fog(bg, 14, 40); // the ground fades into the page colour: no horizon line, no grid to the edge

  // Light: an environment for reflections and soft fill, one key light that casts the shadow, nothing else.
  function environment() {
    if (scene.environment) scene.environment.dispose();
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
  }
  environment();
  scene.environmentIntensity = 0.55;
  const key = new THREE.DirectionalLight(0xffffff, 2.4);
  key.castShadow = true;
  key.shadow.mapSize.set(phone ? 1024 : 2048, phone ? 1024 : 2048);
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.02;
  key.shadow.radius = 3;
  key.shadow.camera.near = 1;
  key.shadow.camera.far = 60;
  scene.add(key, key.target);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.MeshStandardMaterial({ color: css('--ground', '#1a1d22'), roughness: 0.95 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);

  // The props: each starts as its stand-in and is swapped for the installed model when that has loaded.
  const props = {};
  const loaded = [];
  const failed = [];
  const box = new THREE.Box3(), size = new THREE.Vector3();
  function seat(name, object) {
    const p = props[name];
    if (p.object) { p.holder.remove(p.object); }
    object.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    p.holder.add(object);
    p.object = object;
    box.setFromObject(object).getSize(size);
    p.height = Math.max(size.y, 0.01);
    p.shadow.scale.set(size.x * 1.6 + 0.2, 1, size.z * 1.6 + 0.2);
  }
  for (const [name, entry] of Object.entries(PROPS)) {
    const holder = new THREE.Group();
    holder.position.fromArray(entry.at);
    holder.rotation.y = entry.turn || 0;
    const shadow = contactShadow();
    holder.add(shadow);
    scene.add(holder);
    props[name] = { holder, shadow, object: null, height: 1 };
    seat(name, standIn(entry.size));
  }
  const ready = Promise.all(Object.entries(PROPS).filter(([, e]) => e.module).map(([name, entry]) =>
    loadProp(entry, renderer).then((object) => { seat(name, object); loaded.push(name); onChange(); }, (e) => {
      failed.push(name);
      console.error(`prop ${name} did not load, its stand-in stays:`, e && e.message ? e.message : e);
    })));

  // A lost context (a phone reclaiming GPU memory, a driver reset) is restored by the browser because three calls
  // preventDefault; three then re-creates what it holds in JavaScript, but not the environment map, which was rendered
  // on the GPU and comes back black, and nothing redraws a page that draws on demand. If it does not come back, the
  // page tells main.js, which turns it into the article.
  canvas.addEventListener('webglcontextlost', () => onLost(true));
  canvas.addEventListener('webglcontextrestored', () => { environment(); onLost(false); onChange(); });

  // Size: from the canvas's own box, which CSS sets (100lvh tall), not from innerHeight, which a phone's address bar
  // changes as it slides: run 2 drew at innerHeight and the browser stretched that to the taller box.
  let W = 1, H = 1, frame = { x: 0, y: 0, fit: 1 }, free = null;
  function resize() {
    W = Math.max(1, canvas.clientWidth); H = Math.max(1, canvas.clientHeight);
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    renderer.setSize(W, H, false);
    frame = { x: parseFloat(css('--frame-x', '0')) || 0, y: parseFloat(css('--frame-y', '0')) || 0, fit: parseFloat(css('--frame-fit', '1')) || 1 };
    // The stacked layout's free area: below any fixed bar marked data-bar, above the highest panel of the panel layer
    // (all panels, so it does not change from chapter to chapter and the picture does not jump when the text does).
    free = null;
    if (css('--frame-stack', '0') === '1') {
      const panels = [...document.querySelectorAll('.panel')].map((el) => el.getBoundingClientRect().top);
      const bars = [...document.querySelectorAll('[data-bar]')].map((el) => el.getBoundingClientRect().bottom);
      free = { top: Math.max(0, ...bars), bottom: Math.min(H, ...panels) };
      if (free.bottom - free.top < 40) free = null;
    }
    onChange();
  }
  resize();
  new ResizeObserver(resize).observe(canvas);
  if (document.fonts) document.fonts.ready.then(resize); // the panels' heights, and so the free area, wait for fonts

  // Framing: CSS decides where the text sits (style.css, --frame-x / --frame-y / --frame-fit), the camera moves the
  // picture's centre into the free part of the window and widens the view by --frame-fit so the shot still fits there.
  // Below the shots' design shape (DESIGN_ASPECT) the view widens further, to hold the horizontal coverage the shot
  // was designed for. That is right beside a panel, where the free area keeps most of the window's width; in the
  // stacked layout (portrait phone, panel below: --frame-stack: 1) it left the subject a speck in a landscape picture
  // letterboxed into a portrait window. There the camera keeps the shot's position and direction and only the view
  // (its width and offset) is fitted: the subject's box fills FILL of the free area's width or height, whichever binds,
  // centred in it. A shot that names no subject is an establishing shot: its chapter's props fill LOOSE, so it still
  // reads as wide. The shares are what looked right on the starter's stand-ins in check_site's phone sheet: 80 % left
  // a box-shaped prop wall to wall (a box's projected corners overstate the prop) and a wide shot read as a close-up.
  const FILL = { w: 0.62, h: 0.6 }, LOOSE = { w: 0.38, h: 0.35 };
  const rest = new THREE.Box3(), corner2 = new THREE.Vector3();
  function restBox(names) {
    box.makeEmpty();
    for (const name of names) {
      const p = props[name];
      if (!p) continue;
      // the box at full height, so the framing does not follow a prop as it rises or sinks
      const y = p.object.position.y;
      p.object.position.y = 0;
      p.object.updateWorldMatrix(true, true);
      box.union(rest.setFromObject(p.object));
      p.object.position.y = y;
    }
    return box;
  }
  // The view, as pixels per unit of tangent (s) and the window point (cx, cy) the camera's axis passes through.
  function fitted({ props: names, loose }) {
    const b = restBox(names);
    if (b.isEmpty()) return null;
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    for (let k = 0; k < 8; k++) {
      corner2.set(k & 1 ? b.max.x : b.min.x, k & 2 ? b.max.y : b.min.y, k & 4 ? b.max.z : b.min.z).applyMatrix4(camera.matrixWorldInverse);
      if (corner2.z > -0.05) return null; // part of it behind the camera: no sensible fit
      const u = corner2.x / -corner2.z, v = corner2.y / -corner2.z;
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
    }
    const share = loose ? LOOSE : FILL, fh = free.bottom - free.top;
    const sc = Math.min((share.w * W) / (u1 - u0), (share.h * fh) / (v1 - v0));
    return { s: sc, cx: W / 2 - (sc * (u0 + u1)) / 2, cy: (free.top + free.bottom) / 2 + (sc * (v0 + v1)) / 2 };
  }

  const target = new THREE.Vector3(), focus = new THREE.Vector3(), lx = new THREE.Vector3(), ly = new THREE.Vector3();
  const UP = new THREE.Vector3(0, 1, 0);
  let subject = null; // the drawn chapter's subject (shots.js), for onScreen()
  function render(state) {
    const { cam } = state;
    subject = state.subject || null;
    const t0 = Math.tan(THREE.MathUtils.degToRad(cam.fov / 2));
    camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(t0 * frame.fit * Math.max(1, DESIGN_ASPECT / (W / H))));
    camera.aspect = W / H;
    camera.position.fromArray(cam.pos);
    camera.lookAt(target.fromArray(cam.target));
    camera.setViewOffset(W, H, -frame.x * W, frame.y * H, W, H);
    camera.updateMatrixWorld();
    const from = free && state.frame && fitted(state.frame.from), to = free && state.frame && fitted(state.frame.to);
    if (from || to) {
      const m = from && to ? state.frame.mix : from ? 0 : 1, A = from || to, B = to || from;
      const sc = A.s + (B.s - A.s) * m, cx = A.cx + (B.cx - A.cx) * m, cy = A.cy + (B.cy - A.cy) * m;
      camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(H / (2 * sc)));
      camera.setViewOffset(W, H, W / 2 - cx, H / 2 - cy, W, H);
    }
    camera.updateProjectionMatrix();

    // The key light's shadow box follows the shot, snapped to whole texels so shadow edges do not crawl as it moves.
    const span = state.focus.span, sc = key.shadow.camera, texel = span / key.shadow.mapSize.x;
    lx.crossVectors(UP, KEY_DIR).normalize();
    ly.crossVectors(KEY_DIR, lx).normalize();
    focus.fromArray(state.focus.at);
    const a = Math.round(focus.dot(lx) / texel) * texel, b = Math.round(focus.dot(ly) / texel) * texel, n = focus.dot(KEY_DIR);
    focus.copy(lx).multiplyScalar(a).addScaledVector(ly, b).addScaledVector(KEY_DIR, n);
    key.target.position.copy(focus);
    key.position.copy(focus).addScaledVector(KEY_DIR, 25);
    key.target.updateMatrixWorld();
    if (sc.right !== span / 2) { sc.left = -span / 2; sc.right = span / 2; sc.top = span / 2; sc.bottom = -span / 2; sc.updateProjectionMatrix(); }

    // A prop rises out of the ground as it comes on stage and sinks back as it leaves: no pop, and its shadow agrees.
    for (const [name, p] of Object.entries(props)) {
      const v = state.show[name] ?? 0;
      p.holder.visible = v > 0.001;
      p.object.position.y = -(1 - v) * p.height * 1.05;
      p.shadow.material.opacity = v;
    }
    renderer.render(scene, camera);
  }

  // Labels: text or marks in the scene (a troika Text, a sprite) that the reader must be able to read. Register each
  // with label(name, object, prop): it is added to that prop's holder (so it is shown only while the prop is on stage)
  // or to the scene, and onScreen() reports it like a prop, with kind "label". Run 2's labels lay cut by the frame edge
  // and under the panel, and nothing measured them.
  const labels = {};
  function label(name, object, prop) {
    if (prop && !props[prop]) throw new Error(`label ${name}: no prop "${prop}" in props.js`);
    (prop ? props[prop].holder : scene).add(object);
    labels[name] = object;
    if (typeof object.sync === 'function') object.sync(onChange); // a troika Text: its glyphs arrive later
    onChange();
    return object;
  }

  // What the last frame showed of each prop above ground and each label: { share, x, y, box, kind }. share: the part
  // of the window the visible part of its box covers; x, y: that visible part's centre; box: [x0, y0, x1, y1], the
  // whole projected box, not clamped, so a value below 0 or above 1 says how far it runs out of the frame. All are
  // fractions of the window from its top left. When the drawn chapter's shot names a subject, each entry also has
  // subject: true or false. A step where no prop covers anything is a dead frame; a subject's box under the text
  // panel or past an edge is a framing to fix. check_site reads this hook.
  const corner = new THREE.Vector3();
  const visible = (o) => { for (; o; o = o.parent) if (!o.visible) return false; return true; };
  function measured(object, kind) {
    box.setFromObject(object);
    if (box.isEmpty()) return null; // a troika Text before its glyphs are ready
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let k = 0; k < 8; k++) {
      corner.set(k & 1 ? box.max.x : box.min.x, k & 2 ? box.max.y : box.min.y, k & 4 ? box.max.z : box.min.z).project(camera);
      if (corner.z > 1) continue; // behind the camera
      x0 = Math.min(x0, corner.x); x1 = Math.max(x1, corner.x); y0 = Math.min(y0, corner.y); y1 = Math.max(y1, corner.y);
    }
    const cx0 = Math.max(x0, -1), cx1 = Math.min(x1, 1), cy0 = Math.max(y0, -1), cy1 = Math.min(y1, 1);
    if (cx1 <= cx0 || cy1 <= cy0) return null;
    return { share: ((cx1 - cx0) * (cy1 - cy0)) / 4, x: (cx0 + cx1 + 2) / 4, y: 1 - (cy0 + cy1 + 2) / 4,
      box: [(x0 + 1) / 2, (1 - y1) / 2, (x1 + 1) / 2, (1 - y0) / 2], kind };
  }
  function onScreen() {
    const out = {};
    for (const [name, p] of Object.entries(props)) {
      if (!p.holder.visible || p.object.position.y < -0.5 * p.height) continue;
      const m = measured(p.object, 'prop');
      if (m) out[name] = m;
    }
    for (const [name, object] of Object.entries(labels)) {
      const m = visible(object) && measured(object, 'label');
      if (m) out[name] = m;
    }
    if (subject) for (const [name, m] of Object.entries(out)) m.subject = subject.includes(name);
    return out;
  }

  return {
    render,
    label,
    ready,
    info: () => ({ frames: renderer.info.render.frame, calls: renderer.info.render.calls, triangles: renderer.info.render.triangles,
      textures: renderer.info.memory.textures, geometries: renderer.info.memory.geometries, loaded: [...loaded], failed: [...failed],
      onScreen: onScreen() }),
    dispose() {
      scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) [].concat(o.material).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
      });
      if (scene.environment) scene.environment.dispose();
      renderer.dispose();
    },
  };
}
