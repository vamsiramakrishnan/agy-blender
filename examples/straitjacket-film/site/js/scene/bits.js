// bits.js: one InstancedMesh of unit boxes for the few hundred small pieces whose state is cheap to recompute on the
// CPU each frame (rail, corridor blocks, digest pages, tags, plates, brackets, markers, floor path). One draw call.
import * as THREE from '../../vendor/three.module.min.js';

export function createBits(capacity, { basic = false, shadows = true } = {}) {
  const geo = new THREE.BoxGeometry(1, 1, 1);
  const material = basic
    ? new THREE.MeshBasicMaterial({ color: 0xffffff })
    : new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.8, metalness: 0.0 });
  const mesh = new THREE.InstancedMesh(geo, material, capacity);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(capacity * 3), 3);
  mesh.instanceColor.setUsage(THREE.DynamicDrawUsage);
  mesh.frustumCulled = false;
  mesh.castShadow = shadows; mesh.receiveShadow = shadows;
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), e = new THREE.Euler(), p = new THREE.Vector3(), s = new THREE.Vector3();
  let n = 0;
  return {
    mesh,
    begin() { n = 0; },
    // A box centred at (x, y, z) of size (sx, sy, sz), turned by ry about Y; colour is a THREE.Color.
    box(x, y, z, sx, sy, sz, color, ry = 0) {
      if (n >= capacity || sx <= 0 || sy <= 0 || sz <= 0) return;
      p.set(x, y, z); s.set(sx, sy, sz); q.setFromEuler(e.set(0, ry, 0));
      m.compose(p, q, s);
      mesh.setMatrixAt(n, m); mesh.setColorAt(n, color);
      n++;
    },
    // A box between two corners.
    span(x0, x1, y0, y1, z0, z1, color) { this.box((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2, x1 - x0, y1 - y0, z1 - z0, color); },
    end() {
      mesh.count = n; mesh.visible = n > 0;
      mesh.instanceMatrix.needsUpdate = true; mesh.instanceColor.needsUpdate = true;
    },
    get used() { return n; },
  };
}
