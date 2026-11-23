// props.js: the five props. Each is a Group standing on y = 0 at its place, front toward +Z. Until assets/<name>.glb
// has loaded (or when it is missing) the group holds a stand-in built from boxes to the dimensions in SPEC section 6,
// merged into one geometry (one draw call) in the `surface2` material.
import * as THREE from '../../vendor/three.module.min.js';
import { GLTFLoader } from '../../vendor/GLTFLoader.js';
import { mergeGeometries } from '../../vendor/BufferGeometryUtils.js';
import { FX, TURN, SX, SZ, CONSOLE, LEDGER, GATE } from './world.js';

// A box from min/max corners, in the prop's local frame.
const B = (x0, x1, y0, y1, z0, z1) => {
  const g = new THREE.BoxGeometry(x1 - x0, y1 - y0, z1 - z0);
  g.translate((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
  return g;
};
const merged = (boxes) => mergeGeometries(boxes.map((g) => g.toNonIndexed()), false);

const STANDINS = {
  // Console: plinth, cabinet, desk, a raised back with a dark screen bezel, the outlet on +X at (0.58, 1.30).
  console: () => merged([
    B(-0.52, 0.52, 0, 0.05, -0.44, 0.44),
    B(-0.46, 0.46, 0.05, 1.16, -0.38, 0.38),
    B(-0.5, 0.5, 1.16, 1.21, -0.42, 0.42),
    B(-0.46, 0.46, 1.21, 1.76, -0.38, -0.3),
    B(-0.5, 0.5, 1.76, 1.8, -0.42, -0.26),
    B(0.46, 0.58, 1.19, 1.41, -0.13, 0.13),       // outlet housing; its face is the outlet O
  ]),
  // Capture gate: two posts at z = ±0.60, beam 1.72..1.86, clear opening 1.08 x 1.72; feet.
  capture_gate: () => merged([
    B(-0.05, 0.05, 0, 1.72, 0.54, 0.66), B(-0.05, 0.05, 0, 1.72, -0.66, -0.54),
    B(-0.05, 0.05, 1.72, 1.86, -0.66, 0.66),
    B(-0.22, 0.22, 0, 0.03, 0.52, 0.68), B(-0.22, 0.22, 0, 0.03, -0.68, -0.52),
  ]),
  // Context frame: inner volume x ±1.28, y 0.18..1.78, z ±0.33; open front and back.
  context_frame: () => merged([
    B(-1.4, 1.4, 0, 0.18, -0.4, 0.4),
    B(-1.4, 1.4, 1.78, 1.9, -0.4, 0.4),
    B(-1.4, -1.28, 0.18, 1.78, -0.4, -0.3), B(-1.4, -1.28, 0.18, 1.78, 0.3, 0.4),
    B(1.28, 1.4, 0.18, 1.78, -0.4, -0.3), B(1.28, 1.4, 0.18, 1.78, 0.3, 0.4),
    B(-1.37, -1.31, 0.18, 1.78, -0.3, 0.3), B(1.31, 1.37, 0.18, 1.78, -0.3, 0.3),
  ]),
  // Evidence store: 8 bays of 0.40 inner width at 0.46 pitch, a ninth bay with three shelves; base, top, back.
  evidence_store: () => {
    const g = [];
    const walls = []; for (let k = 0; k <= 8; k++) walls.push(-2.18 + 0.46 * k); walls.push(2.18);
    for (const w of walls) g.push(B(w - 0.03, w + 0.03, 0.16, 1.88, -0.27, 0.25));
    g.push(B(-2.24, 2.24, 0, 0.16, -0.29, 0.27));            // base
    g.push(B(-2.24, 2.24, 1.88, 1.96, -0.29, 0.27));         // top
    g.push(B(-2.21, 2.21, 0.16, 1.88, -0.29, -0.23));        // back
    for (const y of [0.45, 0.95, 1.45]) g.push(B(1.53, 2.15, y - 0.03, y, -0.23, 0.23));
    return merged(g);
  },
  // Ledger stand: foot, column, a tray on top.
  ledger_stand: () => merged([
    B(-0.24, 0.24, 0, 0.03, -0.18, 0.18),
    B(-0.03, 0.03, 0.03, 1.0, -0.03, 0.03),
    B(-0.26, 0.26, 1.0, 1.03, -0.19, 0.19),
    B(-0.26, 0.26, 1.03, 1.06, -0.19, -0.16),
  ]),
};

export const PLACES = {
  console: [CONSOLE],
  capture_gate: [[GATE[0], 0, 0]],
  context_frame: [0, 1, 2, 3, 4, 5, 6].map((k) => [FX + TURN * k, 0, 0]),
  evidence_store: [[SX, 0, SZ]],
  ledger_stand: [LEDGER],
};

export function createProps({ palette, onLoad }) {
  const material = new THREE.MeshStandardMaterial({ roughness: 0.6, metalness: 0.0 });
  // Charcoal clearly lighter than the ground in the dark theme (the rim needs something to catch); surface2 in light.
  const standinColor = (pal) => (new THREE.Color(pal.bg).getHSL({}).l > 0.5 ? pal.surface2 : '#2B3038');
  material.color.set(standinColor(palette));
  const groups = {};
  for (const [name, places] of Object.entries(PLACES)) {
    const geo = STANDINS[name]();
    groups[name] = places.map((pos) => {
      const g = new THREE.Group(); g.position.set(...pos); g.name = name;
      const m = new THREE.Mesh(geo, material); m.castShadow = true; m.receiveShadow = true;
      g.add(m);
      return g;
    });
  }
  let loaded = 0;
  const loader = new GLTFLoader();
  const texLoader = new THREE.TextureLoader();
  const assetURL = (f) => new URL('assets/' + f, document.baseURI).href;

  // Contact shadow: a flat quad on the floor, the baked grey image multiplied onto whatever is below it, faded to
  // white over the last 12 % of each side so no edge shows. One per prop instance; a child of the instance's group,
  // so it is visible exactly when the instance is (placeContacts keeps it on the floor while the group rises).
  const contacts = [];
  const contactMaterial = (tex) => {
    const m = new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, fog: false, toneMapped: false, blending: THREE.MultiplyBlending, premultipliedAlpha: true });
    m.userData.fade = { value: 1 };
    m.onBeforeCompile = (sh) => {
      sh.uniforms.uFade = m.userData.fade;
      sh.fragmentShader = 'uniform float uFade;\n' + sh.fragmentShader.replace('#include <map_fragment>', `#include <map_fragment>
  { vec2 e = min(vMapUv, 1.0 - vMapUv) / 0.12; float f = clamp(min(e.x, e.y), 0.0, 1.0); f = f * f * (3.0 - 2.0 * f);
    diffuseColor.rgb = mix(vec3(1.0), diffuseColor.rgb, f * uFade); diffuseColor.a = 1.0; }`);
    };
    m.customProgramCacheKey = () => 'sj-contact';
    return m;
  };
  const addContact = async (name, c) => {
    const tex = await texLoader.loadAsync(assetURL(c.file));
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 4;
    const geo = new THREE.PlaneGeometry(c.size_x, c.size_z); geo.rotateX(-Math.PI / 2);
    for (const g of groups[name]) {
      const mat = contactMaterial(tex);
      const q = new THREE.Mesh(geo, mat);
      q.position.set(c.centre_x || 0, 0.004, c.centre_z || 0);
      q.renderOrder = 1; q.name = 'contact';
      g.add(q);
      contacts.push({ g, q, mat });
    }
  };

  // Swap a stand-in for the real prop when assets/manifest.json lists it (the manifest always exists, so a missing
  // prop costs no request and logs nothing).
  const tryLoad = async (name, entry) => {
    if (!entry || !entry.file) return;
    try {
      const gltf = await loader.loadAsync(assetURL(entry.file));
      gltf.scene.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
      groups[name].forEach((g, i) => {
        for (const ch of [...g.children]) if (ch.name !== 'contact') g.remove(ch);
        g.add(i === 0 ? gltf.scene : gltf.scene.clone(true));
      });
      loaded++;
      if (onLoad) onLoad(name);
    } catch (e) { /* stay with the stand-in */ }
    if (entry.contact) { try { await addContact(name, entry.contact); if (onLoad) onLoad(name); } catch (e) { /* no contact shadow */ } }
  };
  const ready = fetch(assetURL('manifest.json'), { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : {})).catch(() => ({}))
    .then((m) => Promise.all(Object.keys(PLACES).map((n) => tryLoad(n, (m.props || {})[n]))));
  return {
    groups, material, ready,
    loaded: () => loaded,
    // Keep each contact quad on the floor while its group is lowered below the table, fading it in as it rises.
    placeContacts() {
      for (const { g, q, mat } of contacts) {
        q.position.y = 0.004 - g.position.y;
        mat.userData.fade.value = Math.max(0, Math.min(1, 1 + g.position.y / 0.6));
      }
    },
    setPalette: (pal) => material.color.set(standinColor(pal)),
  };
}
