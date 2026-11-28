// Morning on the shore: the lighthouse from the beach, its light out; a small boat pulled up on the sand; a low sun
// behind the tower laying its shadow towards us. Beat 2: the camera rises until the footprints show, from the boat to
// the tower's door. The boat is a prop (props.js): until one is built and installed, the stand-in below is the boat.
import * as THREE from 'three';
import { lighthouse } from './tower.js';

const TOWER = new THREE.Vector3(-6, 0, -26);
const BOAT = new THREE.Vector3(3.0, 0, -1.5);
const lerp = THREE.MathUtils.lerp;

// The boat from primitives: a turned hull, flattened and half sunk in the sand, a thwart and an oar beside it. Its
// origin is at its base, like an installed prop's, and it lies along z.
function boatFromPrimitives(kit) {
  const hull = kit.lathe([[0, 0], [0.42, 0.06], [0.62, 0.6], [0.66, 1.6], [0.6, 2.6], [0.38, 3.05], [0, 3.2]], kit.mat('#5b4b3d', 0.75), 24);
  hull.rotation.x = Math.PI / 2;
  hull.position.set(0, 0.12, -1.6);
  const squash = new THREE.Group();
  squash.scale.y = 0.55;
  squash.add(hull);
  const boat = new THREE.Group();
  boat.add(squash, kit.box(1.05, 0.05, 0.16, kit.mat('#7b6a58'), 0, 0.42, 0.1), kit.box(0.09, 0.04, 2.4, kit.mat('#8a7963'), 0.95, 0.03, 0.2));
  return boat;
}

// The footprints: pairs of prints from the boat to the door, one instanced mesh (one draw for forty prints). Pressed
// into the sand: a darker colour a sliver above it, which only a raking light or a high camera picks out.
function footprints(kit) {
  const n = 44, prints = new THREE.InstancedMesh(new THREE.BoxGeometry(0.13, 0.012, 0.28), kit.mat('#6f604c', 1), n);
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), one = new THREE.Vector3(1, 1, 1);
  const start = BOAT.clone().add(new THREE.Vector3(-0.9, 0, -0.6)), end = TOWER.clone().add(new THREE.Vector3(0, 0, 2.1));
  for (let i = 0; i < n; i++) {
    const f = i / (n - 1), bend = Math.sin(f * Math.PI) * 2.2;
    p.lerpVectors(start, end, f).add(new THREE.Vector3(bend, 0.006, 0));
    const heading = Math.atan2(end.x - start.x + Math.cos(f * Math.PI) * 2.2 * Math.PI, end.z - start.z); // along the path
    const side = i % 2 ? 0.14 : -0.14; // left foot, right foot
    p.x += Math.cos(heading) * side;
    p.z -= Math.sin(heading) * side;
    q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), heading);
    prints.setMatrixAt(i, m.compose(p, q, one));
  }
  prints.receiveShadow = true;
  return prints;
}

export default {
  id: 'shore',
  enterWith: 'fall',
  ambient: true,
  beats: [{ dur: 6, hold: 4 }, { dur: 7, hold: 6 }],
  async create(kit) {
    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#cdd1cf');
    scene.fog = new THREE.Fog('#cdd1cf', 45, 220);
    const camera = new THREE.PerspectiveCamera(36, 1, 0.3, 400);
    const sea = kit.plane(400, 200, kit.mat('#5e7080', 0.22));
    sea.position.set(0, 0.02, -134);
    const foam = kit.box(400, 0.03, 0.6, kit.mat('#eef0ee', 1), 0, 0.03, -34);
    foam.castShadow = false;
    const tower = lighthouse(kit);
    tower.group.position.copy(TOWER);
    tower.glass.emissiveIntensity = 0.05; // morning: the light is out
    const boat = await kit.prop('boat', { standIn: () => boatFromPrimitives(kit) });
    boat.position.copy(BOAT);
    boat.rotation.y = 0.55;
    scene.add(kit.plane(400, 400, kit.mat('#cdbc98', 1)), sea, foam, tower.group, boat, footprints(kit));

    // A low sun behind the tower: long shadows towards the camera, and the sand lit at a grazing angle.
    const sun = new THREE.DirectionalLight('#ffd9ae', 2.4);
    sun.position.set(-70, 14, -48);
    sun.target.position.set(0, 0, -8);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -26, right: 26, top: 26, bottom: -26, near: 1, far: 160 });
    sun.shadow.bias = -0.0005;
    scene.add(sun, sun.target, new THREE.HemisphereLight('#d5dde6', '#8a7a5e', 1.5));

    const target = new THREE.Vector3(), from = new THREE.Vector3();
    return {
      scene,
      camera,
      update(s) {
        // Beat 1: low on the sand, the boat near, the tower far, drifting a little sideways. Beat 2: up and over, until
        // the line of prints from the boat to the door reads.
        const rise = s.k(1, 0, 6);
        const d = lerp(kit.fit(camera, s.aspect, 24, 15), kit.fit(camera, s.aspect, 18, 30), rise);
        target.set(lerp(-0.6, -2.0, rise), lerp(1.7, 0, rise), lerp(-13, -12, rise));
        from.set(lerp(0.1 + 0.08 * s.k(0, 0, 6), 0.2, rise) + s.pointer.x * 0.03, lerp(0.015, 0.8, rise) - s.pointer.y * 0.02, 1).normalize();
        camera.position.copy(target).addScaledVector(from, d);
        camera.lookAt(target);
        foam.position.z = -34 + Math.sin(s.t * 0.7) * 0.7; // the sea's edge comes and goes
      },
    };
  },
};
