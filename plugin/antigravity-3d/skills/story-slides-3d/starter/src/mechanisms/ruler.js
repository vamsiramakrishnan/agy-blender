// ruler.js: for a size. "Eight metres on a side", "a centimetre a second", "as tall as the house": the claim is a
// length, so the picture is a measure with ticks standing beside the thing, its length a number of the set's metres,
// with something familiar in the same frame.
//
// A spine from the origin up (+y) and a tick every `step` metres to one side (+x): one InstancedMesh (one draw), in
// the theme's ink and not lit. Every `major`-th tick and both ends are twice as long, so the eye can count. Lay it
// along the ground or turn it to the camera with the mesh's rotation. grow(x) draws it from the origin; absolute.
import * as THREE from 'three';

// ruler(kit, { length, step, major, thickness, tick, paint }): metres. `thickness` of the lines (1/120 of the length
// when left out) and `tick`, a short tick's length (1/16 of the length).
export function ruler(kit, { length = 1, step = 1, major = 5, thickness = null, tick = null, paint = 'ink' } = {}) {
  const t = thickness ?? length / 120, short = tick ?? length / 16;
  const count = Math.floor(length / step + 1e-6) + 1; // ticks at 0, step, 2 x step ... up to the length
  const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), kit.paint(paint, { flat: true }), count + 1);
  mesh.frustumCulled = false;
  mesh.castShadow = mesh.receiveShadow = false;
  const m = new THREE.Matrix4(), p = new THREE.Vector3(), s = new THREE.Vector3(), q = new THREE.Quaternion();

  const self = {
    mesh, length, step, ticks: count,
    reached: 0, // metres of it drawn
    shown: 0,   // ticks drawn in full
    // How much of it is drawn, 0..1: the spine grows from the origin and each tick comes out as the spine reaches it.
    grow(x) {
      const u = Math.min(1, Math.max(0, x)), reached = length * u * u * (3 - 2 * u);
      const thick = t * (kit.currentTheme().line ?? 1); // a cell print needs its lines thicker (theme.js)
      mesh.setMatrixAt(0, m.compose(p.set(0, reached / 2, 0), q, s.set(reached > 0 ? thick : 0, reached, reached > 0 ? thick : 0)));
      let shown = 0;
      for (let k = 0; k < count; k++) {
        const at = k * step, out = Math.min(1, Math.max(0, (reached - at) / (0.35 * step) + 1)) * (reached > 0 ? 1 : 0);
        const long = k % major === 0 || k === count - 1 ? 2 : 1, run = short * long * out;
        if (out >= 1) shown++;
        mesh.setMatrixAt(k + 1, m.compose(p.set(run / 2, at, 0), q, s.set(run, out > 0 ? thick : 0, out > 0 ? thick : 0)));
      }
      mesh.instanceMatrix.needsUpdate = true;
      self.reached = reached;
      self.shown = shown;
      return reached;
    },
  };
  self.grow(1);
  return self;
}
