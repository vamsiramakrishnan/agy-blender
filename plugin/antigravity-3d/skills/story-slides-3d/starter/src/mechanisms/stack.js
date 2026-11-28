// stack.js: for a part of a whole. "Three in ten", "a fifth of the budget", "one hour of the day": the claim is a
// share, so the picture is the whole cut into equal slabs with that many of them in the accent. Counting slabs is
// reading the number.
//
// `parts` slabs in a column (axis 'y', standing on the origin) or a row (axis 'x', from the origin to +x, on the
// ground), the first `marked` of them in the accent: two InstancedMeshes (two draws), lit, casting shadows. grow(x)
// raises them one after another; absolute.
import * as THREE from 'three';

// stack(kit, { parts, marked, size, gap, axis, paint, mark }): size is the whole stack's [width, height, depth] in
// metres; gap is the space between two slabs as a share of a slab's thickness.
export function stack(kit, { parts = 10, marked = 1, size = [1, 2, 1], gap = 0.12, axis = 'y', paint = 'lit', mark = 'accent' } = {}) {
  const along = axis === 'x' ? 0 : 1, total = size[along];
  const slab = total / (parts + (parts - 1) * gap), pitch = slab * (1 + gap);
  const make = (n, colour) => {
    const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), kit.paint(colour), Math.max(n, 1));
    mesh.count = n;
    mesh.frustumCulled = false;
    mesh.castShadow = mesh.receiveShadow = true;
    return mesh;
  };
  const marks = make(marked, mark), plain = make(parts - marked, paint);
  const group = new THREE.Group();
  group.add(marks, plain);
  const m = new THREE.Matrix4(), p = new THREE.Vector3(), s = new THREE.Vector3(), q = new THREE.Quaternion();

  const self = {
    group, parts, marked, slab,
    raised: 0, // slabs standing in full
    // How many of the slabs stand, 0..1 of them: each comes up from its own base in its own stretch of x.
    grow(x) {
      let raised = 0;
      for (let k = 0; k < parts; k++) {
        let u = Math.min(1, Math.max(0, x * parts - k));
        u = u * u * (3 - 2 * u);
        if (u >= 1) raised++;
        const run = slab * u;
        if (along === 1) { p.set(0, k * pitch + run / 2, 0); s.set(u > 0 ? size[0] : 0, run, u > 0 ? size[2] : 0); }
        else { p.set(k * pitch + run / 2, size[1] / 2, 0); s.set(run, u > 0 ? size[1] : 0, u > 0 ? size[2] : 0); }
        m.compose(p, q, s);
        if (k < marked) marks.setMatrixAt(k, m); else plain.setMatrixAt(k - marked, m);
      }
      marks.instanceMatrix.needsUpdate = plain.instanceMatrix.needsUpdate = true;
      self.raised = raised;
      return raised;
    },
  };
  self.grow(1);
  return self;
}
