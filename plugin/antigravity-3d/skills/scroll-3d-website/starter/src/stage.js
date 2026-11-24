// stage.js: the three.js side. createStage(canvas) -> stage; stage.render(state) draws one state of shots.evaluate.
// It keeps nothing between frames that changes the picture: the same state always gives the same frame.
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { PROPS, loadProp, standIn } from './props.js';

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

export function createStage(canvas, { phone = false, onChange = () => {} } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap; // PCFSoftShadowMap was removed in r18x: it warns and falls back

  const scene = new THREE.Scene();
  const bg = new THREE.Color(css('--bg', '#101215'));
  scene.background = bg;
  scene.fog = new THREE.Fog(bg, 14, 40); // the ground fades into the page colour: no horizon line, no grid to the edge

  // Light: an environment for reflections and soft fill, one key light that casts the shadow, nothing else.
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.55;
  pmrem.dispose();
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

  // Framing: CSS decides where the text sits (style.css, --frame-x / --frame-y / --frame-fit), the camera moves the
  // picture's centre into the free part of the window and widens the view so the shot still fits there.
  let W = 1, H = 1, frame = { x: 0, y: 0, fit: 1 };
  function resize(w, h, dpr) {
    W = Math.max(1, w); H = Math.max(1, h);
    renderer.setPixelRatio(Math.min(dpr || 1, 2));
    renderer.setSize(W, H, false);
    frame = { x: parseFloat(css('--frame-x', '0')) || 0, y: parseFloat(css('--frame-y', '0')) || 0, fit: parseFloat(css('--frame-fit', '1')) || 1 };
  }

  const target = new THREE.Vector3(), focus = new THREE.Vector3(), lx = new THREE.Vector3(), ly = new THREE.Vector3();
  const UP = new THREE.Vector3(0, 1, 0);
  function render(state) {
    const { cam } = state;
    const t0 = Math.tan(THREE.MathUtils.degToRad(cam.fov / 2));
    camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(t0 * frame.fit));
    camera.aspect = W / H;
    camera.position.fromArray(cam.pos);
    camera.lookAt(target.fromArray(cam.target));
    camera.setViewOffset(W, H, -frame.x * W, frame.y * H, W, H);
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

  // What the last frame showed of each prop above ground: the share of the window its box covers and the box's
  // centre (0..1 from the window's top left). A step where no prop covers anything is a dead frame; a centre behind
  // the text panel is a framing to fix.
  const corner = new THREE.Vector3();
  function onScreen() {
    const out = {};
    for (const [name, p] of Object.entries(props)) {
      if (!p.holder.visible || p.object.position.y < -0.5 * p.height) continue;
      box.setFromObject(p.object);
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (let k = 0; k < 8; k++) {
        corner.set(k & 1 ? box.max.x : box.min.x, k & 2 ? box.max.y : box.min.y, k & 4 ? box.max.z : box.min.z).project(camera);
        if (corner.z > 1) continue; // behind the camera
        x0 = Math.min(x0, corner.x); x1 = Math.max(x1, corner.x); y0 = Math.min(y0, corner.y); y1 = Math.max(y1, corner.y);
      }
      const cx0 = Math.max(x0, -1), cx1 = Math.min(x1, 1), cy0 = Math.max(y0, -1), cy1 = Math.min(y1, 1);
      if (cx1 <= cx0 || cy1 <= cy0) continue;
      out[name] = { share: ((cx1 - cx0) * (cy1 - cy0)) / 4, x: (cx0 + cx1 + 2) / 4, y: 1 - (cy0 + cy1 + 2) / 4 };
    }
    return out;
  }

  return {
    render,
    resize,
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
