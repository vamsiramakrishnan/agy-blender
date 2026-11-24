// stage.js: the three.js side. createStage(canvas) -> stage; stage.render(state) draws one state of shots.evaluate.
// It keeps nothing between frames that changes the picture: the same state always gives the same frame.
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { PROPS, loadProp, standIn } from './props.js';
import { DESIGN_ASPECT, DESIGN_FREE } from './shots.js';
import { occlusion } from './occlusion.js';

// From the upper left and in front: the shadows fall back and to the right, away from the reader.
const KEY_DIR = new THREE.Vector3(-0.45, 1.0, 0.55).normalize();
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

// The linear colour that Neutral tone mapping (three's and postprocessing's, Khronos PBR Neutral) turns into `c`: it
// subtracts from every channel x - 6.25 x^2 of the darkest channel x when x is below 0.08, else 0.04, and leaves a
// colour alone that stays below 0.76 (a page colour lighter than that is compressed and cannot be matched this way).
function untoned(c) {
  const lo = Math.min(c.r, c.g, c.b), k = lo < 0.04 ? Math.sqrt(lo / 6.25) - lo : 0.04;
  return c.setRGB(c.r + k, c.g + k, c.b + k, THREE.LinearSRGBColorSpace);
}

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

  // Light: an environment for reflections and soft fill, one key light that casts the shadow, nothing else, within the
  // budget of the skill's libraries.md: on a face turned up, the key gives 0.28 of its intensity and this environment
  // 1.84 of its own, so 1.8 and 0.25 light it at about 1 and a colour shows as its hex. Run 3 lit at about 2.5 (a rim
  // and a fill light added, the environment at 0.95): its floor came out 2.5 times its colour and its amber washed out.
  function environment() {
    if (scene.environment) scene.environment.dispose();
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
  }
  environment();
  scene.environmentIntensity = 0.25;
  const key = new THREE.DirectionalLight(0xffffff, 1.8);
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
    if (post) post.setSize(W, H);
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
  // Ambient occlusion: switched on by occlusion.js when the project has postprocessing and n8ao and the GPU can afford
  // it; until then, and without them, the scene is drawn directly. info().occlusion says which and why.
  let post = null, occluded = 'off: starting';
  const aoReady = occlusion(renderer, scene, camera, { phone }).then(({ pass, why }) => {
    occluded = why;
    if (!pass) return;
    post = pass;
    renderer.toneMapping = THREE.NoToneMapping; // the effect pass tone maps; twice would flatten the picture
    // Drawn directly, the background is the clear colour and never tone mapped; through the effect pass it is, and
    // Neutral tone mapping takes the darkest channel out of a dark colour (#101215 came out #010308 in the starter's
    // test). The background and the fog get the colour that the tone mapping turns into the page's colour.
    const page = bg.clone();
    scene.background = scene.fog.color = untoned(page);
    renderer.info.autoReset = false; // several passes per frame: count the frame's draw calls, not the last pass's
    post.setSize(W, H);
    onChange();
  }, (e) => { occluded = `off: it failed to start (${e && e.message ? e.message : e})`; console.error('ambient occlusion:', e); });

  resize();
  new ResizeObserver(resize).observe(canvas);
  // A still (check_site sets data-agy3d-still on <html> while it draws the article's stills) frames the subject in the
  // middle of the whole canvas: the still sits in the article, with no panel beside it (run 3's kept the desktop's
  // panel offset, and its left part was empty floor).
  const root = document.documentElement;
  new MutationObserver(onChange).observe(root, { attributes: true, attributeFilter: ['data-agy3d-still'] });
  if (document.fonts) document.fonts.ready.then(resize); // the panels' heights, and so the free area, wait for fonts

  // Framing: CSS decides where the text sits (style.css, --frame-x / --frame-y / --frame-fit), the camera moves the
  // picture's centre into the free part of the window and widens the view by --frame-fit so the shot still fits there.
  // Below the shots' design shape (DESIGN_ASPECT) the view widens further, to hold the horizontal coverage the shot was
  // designed for. That is right beside a panel, where the free area keeps most of the window's width; in the stacked
  // layout (portrait phone, panel below: --frame-stack: 1) it left the subject a speck in a landscape picture
  // letterboxed into a portrait window. There, and in a still, the camera keeps the shot's position and direction and
  // only the view (its width and offset) is fitted, once per shot: seen from the pose the shot arrives at, its subject
  // (props and layers) is centred in the free area and fills the share of it that it fills of the free part of the
  // design window (DESIGN_FREE), or the shot's own `fill`, so a close shot stays close and a wide one wide. The hold's
  // travel then shows as it does on a desktop. Run 3's phone refitted every frame to a fixed share: the push was
  // cancelled, every chapter had one scale, and its box stood still for five steps. A shot without a subject fits the
  // props on stage in its chapter. MIN_FILL keeps a wide shot from leaving a speck (22 % of the width in run 2's
  // phone), MAX_FILL leaves the hold room to close in without cutting the subject.
  const MIN_FILL = 0.3, MAX_FILL = 0.85;
  const rest = new THREE.Box3(), corner2 = new THREE.Vector3(), aim = new THREE.Vector3();
  const ref = new THREE.PerspectiveCamera();
  function subjectBox(names) {
    box.makeEmpty();
    for (const name of names) {
      const p = props[name];
      if (p) {
        // the box at full height, so the framing does not follow a prop as it rises or sinks
        const y = p.object.position.y;
        p.object.position.y = 0;
        p.object.updateWorldMatrix(true, true);
        box.union(rest.setFromObject(p.object));
        p.object.position.y = y;
        p.object.updateWorldMatrix(false, true);
      } else if (layers[name]) {
        const b = layerBox(layers[name]);
        if (b) box.union(b);
      }
    }
    return box;
  }
  // The view, as pixels per unit of tangent (s) and the window point (cx, cy) the camera's axis passes through.
  function fitted({ names, pose, fill }, area) {
    const b = subjectBox(names);
    if (b.isEmpty()) return null;
    ref.position.fromArray(pose.pos);
    ref.lookAt(aim.fromArray(pose.target));
    ref.updateMatrixWorld();
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    for (let k = 0; k < 8; k++) {
      corner2.set(k & 1 ? b.max.x : b.min.x, k & 2 ? b.max.y : b.min.y, k & 4 ? b.max.z : b.min.z).applyMatrix4(ref.matrixWorldInverse);
      if (corner2.z > -0.05) return null; // part of it behind the camera: no sensible fit
      const u = corner2.x / -corner2.z, v = corner2.y / -corner2.z;
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
    }
    const t0 = Math.tan(THREE.MathUtils.degToRad(pose.fov / 2));
    const share = fill ?? THREE.MathUtils.clamp(Math.max((u1 - u0) / (2 * t0 * DESIGN_ASPECT * DESIGN_FREE), (v1 - v0) / (2 * t0)), MIN_FILL, MAX_FILL);
    const fh = area.bottom - area.top;
    const sc = share * Math.min(W / (u1 - u0), fh / (v1 - v0));
    return { s: sc, cx: W / 2 - (sc * (u0 + u1)) / 2, cy: (area.top + area.bottom) / 2 + (sc * (v0 + v1)) / 2 };
  }

  const target = new THREE.Vector3(), focus = new THREE.Vector3(), lx = new THREE.Vector3(), ly = new THREE.Vector3();
  const UP = new THREE.Vector3(0, 1, 0);
  let subject = null; // the drawn chapter's subject (shots.js), for onScreen()
  function render(state) {
    const { cam } = state;
    subject = state.subject || null;
    // Layers follow story time first: their bounds may be part of the framing.
    for (const l of Object.values(layers)) if (l.update) l.update(state);
    // A prop rises out of the ground as it comes on stage and sinks back as it leaves: no pop, and its shadow agrees.
    for (const [name, p] of Object.entries(props)) {
      const v = state.show[name] ?? 0;
      p.holder.visible = v > 0.001;
      p.object.position.y = -(1 - v) * p.height * 1.05;
      p.shadow.material.opacity = v;
    }
    scene.updateMatrixWorld();
    for (const l of Object.values(layers)) {
      if (l.object && !l.box) l.object.traverse((o) => { if (o.isInstancedMesh) o.computeBoundingBox(); });
    }

    const still = root.hasAttribute('data-agy3d-still');
    const fit = still ? 1 : frame.fit, shift = still ? 0 : frame.x, lift = still ? 0 : frame.y;
    const t0 = Math.tan(THREE.MathUtils.degToRad(cam.fov / 2));
    camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(t0 * fit * Math.max(1, DESIGN_ASPECT / (W / H))));
    camera.aspect = W / H;
    camera.position.fromArray(cam.pos);
    camera.lookAt(target.fromArray(cam.target));
    camera.setViewOffset(W, H, -shift * W, lift * H, W, H);
    camera.updateMatrixWorld();
    const area = still ? { top: 0, bottom: H } : free;
    const from = area && state.frame && fitted(state.frame.from, area), to = area && state.frame && fitted(state.frame.to, area);
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

    // A label smaller on screen than MIN_LABEL_PX is not drawn (see label() below).
    for (const l of Object.values(labels)) {
      const m = measuredBox(box.setFromObject(l.object), 'label');
      l.holder.visible = !m || (m.box[3] - m.box[1]) * H >= MIN_LABEL_PX;
    }

    if (post) { renderer.info.reset(); post.render(); } else renderer.render(scene, camera);
  }

  // Labels: text or marks in the scene (a troika Text, a sprite) that the reader must be able to read. Register each
  // with label(name, object, prop): it is added to that prop's holder (so it is shown only while the prop is on stage)
  // or to the scene, and onScreen() reports it like a prop, with kind "label". Run 2's labels lay cut by the frame edge
  // and under the panel, and nothing measured them. A label whose box is less than MIN_LABEL_PX CSS pixels high on
  // screen is not drawn: run 3's finale showed its labels 5 to 7 px high on a phone, specks nobody can read. Hidden,
  // not held at a minimum size, because a label scaled up as the camera backs away grows over what it names in a wide
  // shot, where the panel's text names things anyway; it comes back when a closer shot makes it readable.
  const MIN_LABEL_PX = 16; // the height under which check_site calls a label of the subject too small to read
  const labels = {};
  // A label belongs to its prop (or a registered layer): it is flagged subject: true in onScreen() when that is the
  // shot's subject, so check_site can name a label of what the shot is about that is too small to read.
  function label(name, object, prop) {
    if (prop && !props[prop] && !layers[prop]) throw new Error(`label ${name}: no prop "${prop}" in props.js and no layer of that name`);
    const holder = new THREE.Group(); // at the origin of its parent, so the label keeps the place its own position gives it
    holder.add(object);
    (props[prop] ? props[prop].holder : layers[prop]?.object || scene).add(holder);
    labels[name] = { object, holder, owner: prop || null };
    if (typeof object.sync === 'function') object.sync(onChange); // a troika Text: its glyphs arrive later
    onChange();
    return object;
  }

  // Layers: what a shot is about that is not a prop, such as an InstancedMesh of slabs or a group of generated
  // geometry. Register each with layer(name, object, { box, update }) and list its name in LAYERS (shots.js); a shot
  // may then name it as its subject: the stacked phone layout and the stills fit it into the frame, and onScreen()
  // reports it with kind "layer". Its box is the object's bounds, taken at each frame (an InstancedMesh's from its
  // instances), or `box`, a fixed world box [[x0, y0, z0], [x1, y1, z1]], when the framing must not follow its motion.
  // update(state), when given, is called with shots.evaluate's state before each frame is drawn: drive the layer from
  // state.T there, and it stays a function of story time. The object is added to the scene unless it has a parent.
  const layers = {};
  function layer(name, object, { box: fixed, update } = {}) {
    if (object && !object.parent) scene.add(object);
    layers[name] = { object, update, box: fixed ? new THREE.Box3(new THREE.Vector3(...fixed[0]), new THREE.Vector3(...fixed[1])) : null };
    onChange();
    return object;
  }
  const lbox = new THREE.Box3();
  const layerBox = (l) => l.box || (l.object ? lbox.setFromObject(l.object) : null);

  // What the last frame showed of each prop above ground, each layer and each label: { share, x, y, box, kind }. share:
  // the part of the window the visible part of its box covers; x, y: that visible part's centre; box: [x0, y0, x1, y1],
  // the whole projected box, not clamped, so a value below 0 or above 1 says how far it runs out of the frame. All are
  // fractions of the window from its top left. When the drawn chapter's shot names a subject, each entry also has
  // subject: true or false. A step where no prop covers anything is a dead frame; a subject's box under the text
  // panel or past an edge is a framing to fix. check_site reads this hook.
  const corner = new THREE.Vector3();
  const visible = (o) => { for (; o; o = o.parent) if (!o.visible) return false; return true; };
  function measuredBox(b, kind) {
    if (!b || b.isEmpty()) return null; // a troika Text before its glyphs are ready
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let k = 0; k < 8; k++) {
      corner.set(k & 1 ? b.max.x : b.min.x, k & 2 ? b.max.y : b.min.y, k & 4 ? b.max.z : b.min.z).project(camera);
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
      const m = measuredBox(box.setFromObject(p.object), 'prop');
      if (m) out[name] = m;
    }
    for (const [name, l] of Object.entries(layers)) {
      const m = (!l.object || visible(l.object)) && measuredBox(layerBox(l), 'layer');
      if (m) out[name] = m;
    }
    for (const [name, l] of Object.entries(labels)) {
      const m = visible(l.object) && measuredBox(box.setFromObject(l.object), 'label');
      if (m) out[name] = m;
    }
    // A shot that names no subject flags nothing: its whole scene is the subject.
    if (subject) {
      for (const [name, m] of Object.entries(out)) {
        m.subject = subject.includes(name) || (m.kind === 'label' && subject.includes(labels[name].owner));
      }
    }
    return out;
  }

  return {
    render,
    label,
    layer,
    ready: Promise.all([ready, aoReady]),
    info: () => ({ frames: renderer.info.render.frame, calls: renderer.info.render.calls, triangles: renderer.info.render.triangles,
      textures: renderer.info.memory.textures, geometries: renderer.info.memory.geometries, loaded: [...loaded], failed: [...failed],
      occlusion: occluded, onScreen: onScreen() }),
    dispose() {
      scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) [].concat(o.material).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
      });
      if (scene.environment) scene.environment.dispose();
      if (post) post.dispose();
      renderer.dispose();
    },
  };
}
