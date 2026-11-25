// stage.js: Three.js WebGLRenderer, RoomEnvironment, PCFShadowMap, detect-gpu quality tiering,
// CustomShaderMaterial metrology deck, InstancedMesh byte slabs, troika-three-text 3D labels,
// and antigravity-3d prop loader with stand-in fallback.
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { getGPUTier } from 'detect-gpu';
import { Text } from 'troika-three-text';
import { PROPS, loadProp, standIn } from './props.js';
import { DESIGN_ASPECT } from './shots.js';
import { createEvidenceLayer } from './evidence.js';

const KEY_DIR = new THREE.Vector3(-0.45, 1.0, 0.55).normalize();
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

function contactShadow() {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, 'rgba(0,0,0,0.6)');
  grad.addColorStop(0.55, 'rgba(0,0,0,0.24)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const map = new THREE.CanvasTexture(c);
  map.colorSpace = THREE.SRGBColorSpace;
  const geo = new THREE.PlaneGeometry(1, 1);
  geo.rotateX(-Math.PI / 2);
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map, transparent: true, depthWrite: false }));
  mesh.position.y = 0.003;
  mesh.renderOrder = 1;
  return mesh;
}

function makeLabel(text, colorHex, yHeight) {
  const t = new Text();
  t.text = text;
  t.font = new URL('fonts/jetbrains-mono-600.woff', document.baseURI).href;
  t.fontSize = 0.095;
  t.letterSpacing = 0.06;
  t.anchorX = 'center';
  t.anchorY = 'bottom';
  t.color = colorHex;
  t.outlineWidth = 0.012;
  t.outlineColor = '#0D0F12';
  t.position.set(0, yHeight, 0);
  return t;
}

export function createStage(canvas, { phone = false, onChange = () => {}, onLost = () => {} } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;

  const scene = new THREE.Scene();
  const bg = new THREE.Color(css('--bg', '#0D0F12'));
  scene.background = bg;
  scene.fog = new THREE.Fog(bg, 15, 42);

  function environment() {
    if (scene.environment) scene.environment.dispose();
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
  }
  environment();
  scene.environmentIntensity = 0.95;

  const key = new THREE.DirectionalLight(0xffffff, 2.7);
  key.castShadow = true;
  key.shadow.mapSize.set(phone ? 1024 : 2048, phone ? 1024 : 2048);
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.02;
  key.shadow.radius = 2.5;
  key.shadow.camera.near = 1;
  key.shadow.camera.far = 60;
  scene.add(key, key.target);

  // Subtle rim + front-right fill lights (no shadows) to separate dark machined steel bevels from #0D0F12
  const rim = new THREE.DirectionalLight('#b7c8e8', 0.75);
  rim.position.set(6, 5, -8);
  scene.add(rim);
  const fill = new THREE.DirectionalLight('#dbe4f5', 0.65);
  fill.position.set(5, 3, 7);
  scene.add(fill);

  // Metrology coordinate floor + instanced byte slabs
  const evidence = createEvidenceLayer(scene);

  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);

  // GPU tier check in parallel (offline benchmarksURL, never disables 3D)
  let maxDpr = phone ? 1.5 : 2;
  const gpuReady = Promise.race([
    getGPUTier({
      glContext: renderer.getContext(),
      benchmarksURL: new URL('detect-gpu', document.baseURI).href,
    }),
    new Promise((resolve) => setTimeout(() => resolve({ tier: 1, type: 'TIMEOUT' }), 1500)),
  ]).then((gpu) => {
    const tier = gpu && typeof gpu.tier === 'number' ? gpu.tier : 1;
    maxDpr = phone ? Math.min(1.5, tier >= 2 ? 1.5 : 1.25) : (tier >= 2 ? 2 : 1.5);
    document.documentElement.dataset.gpuTier = String(tier);
    document.documentElement.dataset.gpuType = gpu?.type || 'UNKNOWN';
    resize();
  }).catch(() => {});

  // Props & stand-ins
  const props = {};
  const loaded = [];
  const failed = [];
  const box = new THREE.Box3(), size = new THREE.Vector3();
  const labels = {};

  function seat(name, object) {
    const p = props[name];
    if (p.object) { p.holder.remove(p.object); }
    object.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    p.holder.add(object);
    p.object = object;
    box.setFromObject(object).getSize(size);
    p.height = Math.max(size.y, 0.01);
    p.shadow.scale.set(size.x * 1.6 + 0.25, 1, size.z * 1.6 + 0.25);
    const lbl = labels[`${name}-label`];
    if (lbl) lbl.position.y = p.height + 0.18;
  }

  for (const [name, entry] of Object.entries(PROPS)) {
    const holder = new THREE.Group();
    holder.position.fromArray(entry.at);
    holder.rotation.y = entry.turn || 0;
    const shadow = contactShadow();
    holder.add(shadow);
    scene.add(holder);
    props[name] = { holder, shadow, object: null, height: entry.size[1] };
    seat(name, standIn(entry.size));
  }

  function label(name, object, prop) {
    if (prop && !props[prop]) throw new Error(`label ${name}: no prop "${prop}" in props.js`);
    (prop ? props[prop].holder : scene).add(object);
    labels[name] = object;
    if (typeof object.sync === 'function') object.sync(onChange);
    onChange();
    return object;
  }

  label('gate-label', makeLabel('CONTAINMENT GATE', '#6F96FF', PROPS.gate.size[1] + 0.18), 'gate');
  label('vault-label', makeLabel('ARTIFACT STORE', '#F0B429', PROPS.vault.size[1] + 0.18), 'vault');
  label('anchor-label', makeLabel('CONTENT ANCHOR', '#63D3A4', PROPS.anchor.size[1] + 0.18), 'anchor');

  const propsReady = Promise.all(
    Object.entries(PROPS)
      .filter(([, e]) => e.module)
      .map(([name, entry]) =>
        loadProp(entry, renderer).then(
          (object) => { seat(name, object); loaded.push(name); onChange(); },
          (e) => {
            failed.push(name);
            console.error(`prop ${name} did not load, its stand-in stays:`, e && e.message ? e.message : e);
          },
        )),
  );

  const ready = Promise.all([propsReady, gpuReady]);

  canvas.addEventListener('webglcontextlost', () => onLost(true));
  canvas.addEventListener('webglcontextrestored', () => { environment(); onLost(false); onChange(); });

  let W = 1, H = 1, frame = { x: 0, y: 0, fit: 1 }, free = null;
  function resize() {
    W = Math.max(1, canvas.clientWidth);
    H = Math.max(1, canvas.clientHeight);
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, maxDpr));
    renderer.setSize(W, H, false);
    frame = {
      x: parseFloat(css('--frame-x', '0')) || 0,
      y: parseFloat(css('--frame-y', '0')) || 0,
      fit: parseFloat(css('--frame-fit', '1')) || 1,
    };
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
  if (document.fonts) document.fonts.ready.then(resize);

  const FILL = { w: 0.64, h: 0.56 }, LOOSE = { w: 0.82, h: 0.55 };
  const rest = new THREE.Box3(), corner2 = new THREE.Vector3();
  function restBox(names) {
    box.makeEmpty();
    for (const name of names) {
      const p = props[name];
      if (!p) continue;
      const y = p.object.position.y;
      p.object.position.y = 0;
      p.object.updateWorldMatrix(true, true);
      box.union(rest.setFromObject(p.object));
      p.object.position.y = y;
    }
    return box;
  }

  function fitted({ props: names, loose }) {
    const b = restBox(names);
    if (b.isEmpty()) return null;
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    for (let k = 0; k < 8; k++) {
      corner2.set(k & 1 ? b.max.x : b.min.x, k & 2 ? b.max.y : b.min.y, k & 4 ? b.max.z : b.min.z)
        .applyMatrix4(camera.matrixWorldInverse);
      if (corner2.z > -0.05) return null;
      const u = corner2.x / -corner2.z, v = corner2.y / -corner2.z;
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
    }
    const share = loose ? LOOSE : FILL, fh = free.bottom - free.top;
    const sc = Math.min((share.w * W) / (u1 - u0), (share.h * fh) / (v1 - v0));
    return { s: sc, cx: W / 2 - (sc * (u0 + u1)) / 2, cy: (free.top + free.bottom) / 2 + (sc * (v0 + v1)) / 2 };
  }

  const target = new THREE.Vector3(), focus = new THREE.Vector3(), lx = new THREE.Vector3(), ly = new THREE.Vector3();
  const UP = new THREE.Vector3(0, 1, 0);
  const lblWorld = new THREE.Vector3();
  let subject = null;

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

    const from = free && state.frame && fitted(state.frame.from);
    const to = free && state.frame && fitted(state.frame.to);
    if (from || to) {
      const m = from && to ? state.frame.mix : from ? 0 : 1;
      const A = from || to, B = to || from;
      const zoomOut = B.s < A.s;
      const mw = zoomOut ? Math.min(1, m * 1.6) : m;
      const sc = 1 / ((1 - mw) / A.s + mw / B.s);
      const cx = A.cx + (B.cx - A.cx) * mw;
      const cy = A.cy + (B.cy - A.cy) * mw;
      camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(H / (2 * sc)));
      camera.setViewOffset(W, H, W / 2 - cx, H / 2 - cy, W, H);
    }
    camera.updateProjectionMatrix();

    // Key light shadow frustum snaps to texels
    const span = state.focus.span, sc = key.shadow.camera, texel = span / key.shadow.mapSize.x;
    lx.crossVectors(UP, KEY_DIR).normalize();
    ly.crossVectors(KEY_DIR, lx).normalize();
    focus.fromArray(state.focus.at);
    const a = Math.round(focus.dot(lx) / texel) * texel;
    const b = Math.round(focus.dot(ly) / texel) * texel;
    const n = focus.dot(KEY_DIR);
    focus.copy(lx).multiplyScalar(a).addScaledVector(ly, b).addScaledVector(KEY_DIR, n);
    key.target.position.copy(focus);
    key.position.copy(focus).addScaledVector(KEY_DIR, 25);
    key.target.updateMatrixWorld();
    if (sc.right !== span / 2) {
      sc.left = -span / 2; sc.right = span / 2; sc.top = span / 2; sc.bottom = -span / 2;
      sc.updateProjectionMatrix();
    }

    // Update props
    for (const [name, p] of Object.entries(props)) {
      const v = state.show[name] ?? 0;
      p.holder.visible = v > 0.001;
      p.object.position.y = -(1 - v) * p.height * 1.05;
      p.shadow.material.opacity = v;
    }

    // Show each upright 3D label only on the chapter(s) where its prop is the active subject (or handoff)
    // and billboard horizontally toward the camera so it never turns edge-on.
    const labelRules = {
      'gate-label': !subject || subject.includes('gate'),
      'vault-label': !subject || subject.includes('vault'),
      'anchor-label': !subject || subject.includes('anchor'),
    };
    for (const [name, obj] of Object.entries(labels)) {
      const showLbl = labelRules[name] ?? true;
      obj.visible = showLbl;
      if (showLbl) {
        obj.getWorldPosition(lblWorld);
        const angleY = Math.atan2(camera.position.x - lblWorld.x, camera.position.z - lblWorld.z);
        const parentY = obj.parent ? obj.parent.rotation.y : 0;
        obj.rotation.set(0, angleY - parentY, 0);
      }
    }

    // Update shader metrology deck & instanced byte slabs strictly from story state
    evidence.update(state);

    renderer.render(scene, camera);
  }

  const corner = new THREE.Vector3();
  const visible = (o) => { for (; o; o = o.parent) if (!o.visible) return false; return true; };
  function measured(object, kind) {
    box.setFromObject(object);
    if (box.isEmpty()) return null;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let k = 0; k < 8; k++) {
      corner.set(k & 1 ? box.max.x : box.min.x, k & 2 ? box.max.y : box.min.y, k & 4 ? box.max.z : box.min.z).project(camera);
      if (corner.z > 1) continue;
      x0 = Math.min(x0, corner.x); x1 = Math.max(x1, corner.x); y0 = Math.min(y0, corner.y); y1 = Math.max(y1, corner.y);
    }
    const cx0 = Math.max(x0, -1), cx1 = Math.min(x1, 1), cy0 = Math.max(y0, -1), cy1 = Math.min(y1, 1);
    if (cx1 <= cx0 || cy1 <= cy0) return null;
    return {
      share: ((cx1 - cx0) * (cy1 - cy0)) / 4,
      x: (cx0 + cx1 + 2) / 4,
      y: 1 - (cy0 + cy1 + 2) / 4,
      box: [(x0 + 1) / 2, (1 - y1) / 2, (x1 + 1) / 2, (1 - y0) / 2],
      kind,
    };
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
    if (subject) {
      for (const [name, m] of Object.entries(out)) {
        const base = name.endsWith('-label') ? name.slice(0, -6) : name;
        m.subject = subject.includes(base);
      }
    }
    return out;
  }

  return {
    render,
    label,
    ready,
    info: () => ({
      frames: renderer.info.render.frame,
      calls: renderer.info.render.calls,
      triangles: renderer.info.render.triangles,
      textures: renderer.info.memory.textures,
      geometries: renderer.info.memory.geometries,
      loaded: [...loaded],
      failed: [...failed],
      onScreen: onScreen(),
    }),
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
