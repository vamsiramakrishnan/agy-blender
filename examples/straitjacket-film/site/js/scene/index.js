// index.js: the scroll film's three.js scene. createFilm(...) -> film; film.render(T) draws film time T.
// Everything visible is a pure function of T (tracks.js); `seconds` only adds a slow drift at rest.
import * as THREE from '../../vendor/three.module.min.js';
import { RoomEnvironment } from '../../vendor/RoomEnvironment.js';
import { evaluate } from './tracks.js';
import { createLines } from './lines.js';
import { createProps } from './props.js';
import { createExtras, revealK, FRAME_DROP } from './extras.js';
import { createLabels } from './labels.js';
import { FX, TURN, SX, SZ, CONSOLE, LEDGER, NEEDLE_INDEX, bayX } from './world.js';

const QUALITY = { hi: { dpr: 2, shadow: 2048 }, mid: { dpr: 1.5, shadow: 1024 }, lo: { dpr: 1, shadow: 0 } };
const KEY_DIR = new THREE.Vector3(-0.5, 1.0, 0.34).normalize();   // from the upper left, in front of the reader

function makeFloor() {
  const geo = new THREE.PlaneGeometry(160, 160); geo.rotateX(-Math.PI / 2);
  const uniforms = { uGrid: { value: new THREE.Color() }, uGridAmt: { value: 1 }, uPool: { value: new THREE.Color() } };
  const base = new THREE.MeshBasicMaterial({ toneMapped: false, fog: true });
  base.onBeforeCompile = (sh) => {
    Object.assign(sh.uniforms, uniforms);
    sh.vertexShader = 'varying vec3 vSjW;\n' + sh.vertexShader.replace('#include <project_vertex>', '#include <project_vertex>\n  vSjW = (modelMatrix * vec4(transformed, 1.0)).xyz;');
    sh.fragmentShader = 'varying vec3 vSjW;\nuniform vec3 uGrid;\nuniform float uGridAmt;\nuniform vec3 uPool;\n' + sh.fragmentShader.replace('#include <color_fragment>', `#include <color_fragment>
  {
    // The pool of the key light on the table: lighter around the set, the ground colour far away.
    vec2 pc = (vSjW.xz - vec2(1.0, -1.2)) / vec2(15.0, 9.5);
    float pool = 1.0 - smoothstep(0.15, 1.0, length(pc));
    diffuseColor.rgb = mix(diffuseColor.rgb, uPool, pool);
    vec2 g = vSjW.xz / 0.64;
    vec2 fw = fwidth(g);
    vec2 d = abs(fract(g - 0.5) - 0.5) / max(fw, 1e-4);
    float l = 1.0 - min(min(d.x, d.y), 1.0);
    // fade with distance (and where the cells get too small to draw cleanly)
    float dist = length(vSjW - cameraPosition);
    float fade = 0.55 * (1.0 - smoothstep(10.0, 30.0, dist)) * (1.0 - smoothstep(0.18, 0.45, max(fw.x, fw.y)));
    diffuseColor.rgb = mix(diffuseColor.rgb, uGrid, l * fade * uGridAmt);
  }`);
  };
  base.customProgramCacheKey = () => 'sj-floor';
  const floor = new THREE.Mesh(geo, base);
  floor.renderOrder = -2;
  const catcherMat = new THREE.ShadowMaterial({ color: 0x000000, opacity: 0.5, depthWrite: false });
  const catcher = new THREE.Mesh(geo, catcherMat);
  catcher.receiveShadow = true; catcher.renderOrder = -1;
  return { floor, catcher, base, catcherMat, uniforms };
}

export async function createFilm({ canvas, labels: labelRoot, data, lines: bytes, palette, quality = 'hi', reducedMotion = false }) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 120);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  pmrem.dispose();

  // Light: one key with a soft shadow, a hemisphere fill, the room at low intensity for the props' metal.
  const key = new THREE.DirectionalLight(0xffffff, 2.6);
  key.castShadow = true;
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.012;
  key.shadow.radius = 3;
  key.shadow.camera.near = 1; key.shadow.camera.far = 60;
  scene.add(key, key.target);
  const hemi = new THREE.HemisphereLight(0xffffff, 0x000000, 1.0);
  scene.add(hemi);
  // A cooler rim from behind-right, no shadow: every prop gets a lit edge against the ground.
  const rim = new THREE.DirectionalLight(0xc8d8ff, 1.6);
  rim.position.set(6, 5, -9);
  scene.add(rim);

  const stage = makeFloor();
  scene.add(stage.floor, stage.catcher);

  const lines = createLines({ bytes, palette });
  scene.add(lines.mesh);
  const props = createProps({ palette, onLoad: () => { dirty = true; } });
  for (const gs of Object.values(props.groups)) gs.forEach((g) => scene.add(g));
  const extras = createExtras({ data, lines, palette });
  extras.meshes.forEach((m) => scene.add(m));
  const labels = createLabels(labelRoot);

  let W = 1, H = 1, layout = 'wide', q = QUALITY[quality] || QUALITY.hi, lastMs = 0, dirty = true, theme = 'dark';
  const pal = { ...palette };

  function setPalette(p) {
    Object.assign(pal, p);
    const bg = new THREE.Color(pal.bg);
    theme = bg.getHSL({}).l > 0.5 ? 'light' : 'dark';
    scene.background = bg;
    scene.fog = new THREE.Fog(bg, theme === 'light' ? 20 : 18, theme === 'light' ? 52 : 48);
    stage.base.color.set(pal.bg);
    stage.uniforms.uGrid.value.set(pal.grid);
    stage.uniforms.uPool.value.set(theme === 'light' ? pal.surface : '#1B1F25');
    if (theme === 'dark') stage.uniforms.uGrid.value.set('#2E343D');
    stage.uniforms.uGridAmt.value = theme === 'light' ? 0.9 : 1.0;
    stage.catcherMat.opacity = theme === 'light' ? 0.2 : 0.55;
    key.intensity = theme === 'light' ? 3.6 : 3.4;
    hemi.intensity = theme === 'light' ? 1.5 : 1.35;
    hemi.color.set(0xffffff); hemi.groundColor.set(theme === 'light' ? pal.surface2 : '#30343a');
    rim.intensity = theme === 'light' ? 1.2 : 3.0;
    scene.environmentIntensity = theme === 'light' ? 0.3 : 0.22;
    lines.setPalette(pal); props.setPalette(pal); extras.setPalette(pal);
    dirty = true;
  }
  function setQuality(name) {
    q = QUALITY[name] || QUALITY.hi;
    const on = q.shadow > 0;
    if (key.castShadow !== on) { key.castShadow = on; stage.catcher.visible = on; }
    if (on && key.shadow.mapSize.x !== q.shadow) {
      key.shadow.mapSize.set(q.shadow, q.shadow);
      if (key.shadow.map) { key.shadow.map.dispose(); key.shadow.map = null; }
    }
    resize(W, H, devicePixelRatio || 1, layout);
  }
  let dprIn = 1;
  function resize(w, h, dpr, lay) {
    W = Math.max(1, w); H = Math.max(1, h); dprIn = dpr || dprIn; layout = lay || layout;
    renderer.setPixelRatio(Math.min(dprIn, q.dpr));
    renderer.setSize(W, H, false);
    camera.aspect = W / H;
    labels.resize(W, H, layout);
    dirty = true;
  }

  // Frame the key pose for the current layout: subject in the right two thirds (wide) or upper 55 % (narrow).
  const tmpP = new THREE.Vector3(), tmpT = new THREE.Vector3();
  function placeCamera(s, seconds) {
    const aspect = W / H;
    const regionAspect = layout === 'wide' ? aspect * (2 / 3) : aspect;
    const regionV = layout === 'wide' ? 1 : 0.55;
    const need = Math.max(1, 1 / regionV, 1.0667 / regionAspect);
    const fk = Math.min(need, 1.22), dolly = need / fk;
    const t0 = Math.tan(THREE.MathUtils.degToRad(s.cam.fov / 2));
    camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(t0 * fk));
    tmpT.set(...s.cam.target);
    tmpP.set(...s.cam.pos).sub(tmpT).multiplyScalar(dolly).add(tmpT);
    if (!reducedMotion && seconds && s.p.drift > 0) {
      const d = s.p.drift;
      tmpP.x += Math.sin(seconds * 0.19) * 0.09 * d; tmpP.y += Math.sin(seconds * 0.13 + 1.3) * 0.035 * d;
    }
    camera.position.copy(tmpP);
    camera.up.set(0, 1, 0);
    camera.lookAt(tmpT);
    if (layout === 'wide') camera.setViewOffset(W, H, -W / 6, 0, W, H);
    else camera.setViewOffset(W, H, 0, H * 0.225, W, H);
    camera.updateProjectionMatrix();
  }

  // The key light's shadow box follows the point of interest, snapped to whole texels so edges do not crawl.
  const lx = new THREE.Vector3(), ly = new THREE.Vector3();
  function placeKey(s) {
    if (!key.castShadow) { key.position.copy(KEY_DIR).multiplyScalar(30); key.target.position.set(0, 0, 0); return; }
    const span = s.focus.span, c = key.shadow.camera;
    lx.crossVectors(new THREE.Vector3(0, 1, 0), KEY_DIR).normalize(); ly.crossVectors(KEY_DIR, lx).normalize();
    const f = new THREE.Vector3(...s.focus.at);
    const texel = span / key.shadow.mapSize.x;
    const a = Math.round(f.dot(lx) / texel) * texel, b = Math.round(f.dot(ly) / texel) * texel, n = f.dot(KEY_DIR);
    f.copy(lx).multiplyScalar(a).addScaledVector(ly, b).addScaledVector(KEY_DIR, n);
    key.target.position.copy(f);
    key.position.copy(f).addScaledVector(KEY_DIR, 30);
    if (c.right !== span / 2) { c.left = -span / 2; c.right = span / 2; c.top = span / 2; c.bottom = -span / 2; c.updateProjectionMatrix(); }
    key.target.updateMatrixWorld();
  }

  // Frames 2..7 (corridor, contained, workers B and C) rise out of the table; the ledger stand too.
  function placeProps(s) {
    const p = s.p;
    props.groups.context_frame.forEach((g, k) => {
      if (k === 0) return;
      const r = Math.max(revealK(p.corridor, k), revealK(p.contained, k), k <= 2 ? p.workers : 0);
      g.visible = r > 0.001; g.position.y = -(1 - r) * FRAME_DROP;
    });
    const lg = props.groups.ledger_stand[0];
    lg.visible = p.ledger > 0.001; lg.position.y = -(1 - p.ledger) * 1.2;
    props.placeContacts();
  }

  // Where the needle is (for its label and its outline on the floor).
  const needleCell = lines.at(NEEDLE_INDEX, 'frame');
  const needleFloor = [needleCell[0], 0.005, 0.86];

  const fam = data.log.families;
  function placeLabels(s, T) {
    const L = s.labels, p = s.p;
    labels.begin();
    labels.put(camera, 'cmd', '$ cat corpus.log', 'mono', [CONSOLE[0] - 0.45, 1.98, 0.2], L.cmd);
    labels.put(camera, 'ctxcmd', '$ ctx run -- cat corpus.log', 'mono', [CONSOLE[0] - 0.45, 1.98, 0.2], L.ctxcmd);
    labels.put(camera, 'context', 'CONTEXT', 'turn', [FX - 1.4, 2.06, 0.4], L.context);
    for (let k = 0; k < 7; k++) {
      const dy = k === 0 ? 0 : -(1 - Math.max(revealK(p.corridor, k), revealK(p.contained, k))) * FRAME_DROP;
      labels.put(camera, 'T' + (k + 1), 'T' + (k + 1), 'turn', [FX + TURN * k - 1.4, 2.06 + dy, 0.4], L.turns);
    }
    let nat = needleCell, no = L.needle;
    if (T > 3.2 && T < 4) nat = needleFloor;
    else if (T > 7 && T < 8) nat = extras.anchors.copyAt(FX);
    else if (T > 10 && T < 11) { nat = extras.anchors.copyAt(FX + TURN); no *= p.handoff > 0.95 ? 1 : 0; }
    labels.put(camera, 'needle', 'L14238', 'tag', [nat[0] + 0.06, nat[1] + 0.01, nat[2]], no);
    fam.forEach((f, k) => labels.put(camera, 'fam' + k, `${f.verb} ${f.count.toLocaleString('en-US')}`, 'mono', [bayX(k), 1.965, SZ + 0.27], L.families, 'above', true));
    labels.put(camera, 'run', 'run:8956c843e556', 'tag', extras.anchors.run, L.run);
    labels.put(camera, 'srctag', '@07407f1c', 'tag', extras.anchors.srcTag(s), L.source, 'left');
    ['WORKER A', 'WORKER B', 'WORKER C'].forEach((t, k) => labels.put(camera, 'w' + k, t, 'turn', [FX + TURN * k - 1.4, 2.06, 0.4], L.workers));
    labels.put(camera, 'ledger', 'LEDGER', 'turn', [LEDGER[0] + 0.3, 1.2, LEDGER[2]], L.workers);
    ['BIRTH', 'ENTRY', 'RESIDENCE', 'EMISSION'].forEach((t, i) => {
      const m = extras.anchors.markers[i];
      const lit = Math.min(1, Math.max(0, (p.gates - 0.14 - i * 0.2) / 0.08));
      labels.put(camera, 'g' + i, t, 'gate', [m.at[0], 0.02, m.at[1] + 0.3], L.gates * lit, 'center');
    });
    labels.end();
  }

  let lastT = -1;
  function render(T, seconds = 0) {
    const t0 = performance.now();
    const s = evaluate(T);
    placeCamera(s, reducedMotion ? 0 : seconds);
    placeKey(s);
    lines.update(s.p);
    placeProps(s);
    extras.update(s, T, needleFloor);
    renderer.render(scene, camera);
    placeLabels(s, T);
    lastT = T; dirty = false;
    lastMs = performance.now() - t0;
  }

  setPalette(palette);
  setQuality(quality);
  resize(canvas.clientWidth || innerWidth, canvas.clientHeight || innerHeight, devicePixelRatio || 1, 'wide');

  return {
    render,
    resize,
    setPalette,
    setQuality,
    info: () => ({ calls: renderer.info.render.calls, triangles: renderer.info.render.triangles, frameMs: Math.round(lastMs * 10) / 10, propsLoaded: props.loaded(), labels: labels.count() }),
    get needsRender() { return dirty; },
    dispose() {
      labels.dispose();
      scene.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) [].concat(o.material).forEach((m) => m.dispose()); });
      if (scene.environment) scene.environment.dispose();
      renderer.dispose();
    },
  };
}
