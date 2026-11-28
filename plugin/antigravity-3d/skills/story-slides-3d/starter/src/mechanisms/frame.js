// frame.js: for a volume or an extent. "A kilometre long, wide and tall", "in every cubic metre", "this much of the
// room": the claim is a size in three directions, so the picture is that box drawn round the thing, at its true size
// in the set's metres.
//
// A thin wire box: twelve bars in one InstancedMesh (one draw), in the theme's ink and not lit (it is a mark on the
// picture, not a part of the set). Its origin is the middle of its base. grow(x) draws it: the base first, then the
// four uprights, then the top; absolute, so update can call it with a function of the state.
import * as THREE from 'three';

const clamp01 = (x) => Math.min(1, Math.max(0, x));
// A stretch of x, eased: 0 before `from`, 1 after `to`.
const span = (x, from, to) => { const u = clamp01((x - from) / (to - from)); return u * u * (3 - 2 * u); };

// frame(kit, { size, thickness, paint }): size in metres, one number or [width, height, depth]; thickness of a bar
// (1/250 of the largest side when left out: a line of two or three pixels when the box fills the frame).
export function frame(kit, { size = 1, thickness = null, paint = 'ink' } = {}) {
  const [w, h, d] = Array.isArray(size) ? size : [size, size, size];
  const t = thickness ?? Math.max(w, h, d) / 250;
  const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), kit.paint(paint, { flat: true }), 12);
  mesh.frustumCulled = false;
  mesh.castShadow = mesh.receiveShadow = false;
  // Each bar, six numbers: where it starts (x, y, z), the axis it runs along (0 x, 1 y, 2 z), its length along that
  // axis (signed) and its stage (0 base, 1 upright, 2 top). The base and the top run from corner to corner round the
  // rectangle, so each is drawn in one stroke.
  const bars = [];
  for (const [stage, y] of [[0, 0], [2, h]]) {
    bars.push(-w / 2, y, -d / 2, 0, w, stage, w / 2, y, -d / 2, 2, d, stage, w / 2, y, d / 2, 0, -w, stage, -w / 2, y, d / 2, 2, -d, stage);
  }
  for (const [x, z] of [[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]]) bars.push(x, 0, z, 1, h, 1);
  const m = new THREE.Matrix4(), p = new THREE.Vector3(), s = new THREE.Vector3(), q = new THREE.Quaternion();
  const grown = [0, 0, 0];

  const self = {
    mesh, size: [w, h, d], thickness: t,
    // How much of it is drawn, 0..1. Returns the shares drawn so far of a base bar, an upright and a top bar.
    grow(x) {
      grown[0] = span(x, 0, 0.45); grown[1] = span(x, 0.3, 0.8); grown[2] = span(x, 0.65, 1);
      const thick = t * (kit.currentTheme().line ?? 1); // a cell print needs its lines thicker (theme.js)
      for (let i = 0; i < 12; i++) {
        const o = i * 6, axis = bars[o + 3], g = grown[bars[o + 5]], run = bars[o + 4] * g;
        p.set(bars[o], bars[o + 1], bars[o + 2]);
        p.setComponent(axis, p.getComponent(axis) + run / 2);
        // a bar not yet begun has no size at all, so it does not show as a dot at its corner
        if (g > 0) { s.set(thick, thick, thick); s.setComponent(axis, Math.abs(run) + thick); } else s.set(0, 0, 0);
        mesh.setMatrixAt(i, m.compose(p, q, s));
      }
      mesh.instanceMatrix.needsUpdate = true;
      return grown;
    },
  };
  self.grow(1);
  return self;
}
