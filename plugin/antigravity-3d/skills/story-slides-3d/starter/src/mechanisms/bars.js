// bars.js: for a few numbers compared. "Four times last year's", "the smallest of the five", "this one stands out":
// the claim is that one number is larger than another, so the picture is things whose heights are those numbers,
// standing on one ground under one light. A handful only: past seven or so it is a chart, and a chart belongs in
// the slide's HTML.
//
// A row of bars from the origin to +x, standing on the ground, each as tall as its value (the largest is `height`
// metres): two InstancedMeshes (two draws: the plain ones and the marked ones), lit, casting shadows. grow(x) raises
// them one after another; absolute.
import * as THREE from 'three';

// bars(kit, { values, height, width, gap, depth, marked, paint, mark }): `marked` is the indices shown in the accent;
// `gap` is the space between two bars as a share of a bar's width; `depth` is a bar's depth (its width when left out).
export function bars(kit, { values, height = 1, width = 0.3, gap = 0.5, depth = null, marked = [], paint = 'lit', mark = 'accent' } = {}) {
  const most = Math.max(...values), heights = values.map((v) => (v / most) * height), deep = depth ?? width;
  const isMarked = values.map((_, i) => marked.includes(i)), slot = [];
  let nMarks = 0, nPlain = 0;
  for (let i = 0; i < values.length; i++) slot.push(isMarked[i] ? nMarks++ : nPlain++);
  const make = (n, colour) => {
    const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), kit.paint(colour), Math.max(n, 1));
    mesh.count = n;
    mesh.frustumCulled = false;
    mesh.castShadow = mesh.receiveShadow = true;
    return mesh;
  };
  const marks = make(nMarks, mark), plain = make(nPlain, paint);
  const group = new THREE.Group();
  group.add(marks, plain);
  const m = new THREE.Matrix4(), p = new THREE.Vector3(), s = new THREE.Vector3(), q = new THREE.Quaternion();
  const n = values.length, tops = values.map(() => 0);

  const self = {
    group, heights,
    tops, // each bar's height now, in metres
    // How far the bars have risen, 0..1: the first starts first and each takes half of x to reach its height, so
    // they finish in order and the eye compares them as they come.
    grow(x) {
      for (let i = 0; i < n; i++) {
        let u = Math.min(1, Math.max(0, (x - (n > 1 ? (i / (n - 1)) * 0.5 : 0)) / 0.5));
        u = u * u * (3 - 2 * u);
        const tall = heights[i] * u;
        tops[i] = tall;
        m.compose(p.set(i * width * (1 + gap) + width / 2, tall / 2, 0), q, s.set(u > 0 ? width : 0, tall, u > 0 ? deep : 0));
        (isMarked[i] ? marks : plain).setMatrixAt(slot[i], m);
      }
      marks.instanceMatrix.needsUpdate = plain.instanceMatrix.needsUpdate = true;
      return tops;
    },
  };
  self.grow(1);
  return self;
}
