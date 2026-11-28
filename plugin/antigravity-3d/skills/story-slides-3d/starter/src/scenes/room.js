// The lamp room: a round room, the great lens on its pedestal, a desk with the log book under a shaded lamp, the
// keeper. Each beat has one bright, simple subject and the keeper dark against light: beat 1 the open log in the
// lamp's pool, he beside it against the window bars' stripes on the wall; beat 2 the lit lens in the middle of the
// frame, he before it. The lens's beam sweeps the walls all the while.
import * as THREE from 'three';
import { TURN } from './title.js';

const LENS = new THREE.Vector3(1.5, 0, -1.3);
const DESK = new THREE.Vector3(-1.7, 0, -1.55);
// The lens's rings: a beehive of glass, ridged so each ring catches the light.
const LENS_PROFILE = [[0, 0], [0.45, 0], [0.62, 0.1], [0.74, 0.3], [0.8, 0.45], [0.73, 0.52], [0.8, 0.66], [0.73, 0.73],
  [0.8, 0.87], [0.73, 0.94], [0.8, 1.08], [0.72, 1.16], [0.76, 1.28], [0.6, 1.5], [0.3, 1.64], [0, 1.68]];
const lerp = THREE.MathUtils.lerp;

// The lens's glow, ring by ring: a band of the texture per segment of the profile (a lathe's v runs along it), bright
// where the glass faces out and up, dim in the grooves, so the rings read even in a two-ink print.
function rings() {
  const c = document.createElement('canvas');
  c.width = 1; c.height = LENS_PROFILE.length - 1;
  const g = c.getContext('2d');
  for (let i = 0; i < c.height; i++) {
    const out = LENS_PROFILE[i + 1][0] >= LENS_PROFILE[i][0];
    g.fillStyle = out ? '#ffffff' : '#3a3a3a';
    g.fillRect(0, c.height - 1 - i, 1, 1);
  }
  const t = new THREE.CanvasTexture(c);
  t.magFilter = THREE.NearestFilter;
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export default {
  id: 'room',
  enterWith: 'wipe',
  ambient: true,
  beats: [{ dur: 7, hold: 4 }, { dur: 6, hold: 5 }],
  create(kit) {
    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#05070b');
    const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 60);
    const wallM = kit.mat('#77756f', 0.95);
    wallM.side = THREE.BackSide;
    const wall = kit.cyl(4.2, 4.2, 3.4, wallM, 0, 1.7, 0, true);
    wall.castShadow = false; // the window's light comes through it
    const wood = kit.mat('#4a3c30', 0.7), dark = kit.mat('#1d1f24', 0.6), page = kit.mat('#f4eedf', 0.9);
    // The lens glows from within (emissive) and is flat shaded, so each ring is its own band: the brightest, simplest
    // shape in beat 2, with the keeper dark against it.
    const lensM = new THREE.MeshStandardMaterial({ color: '#3a4244', roughness: 0.3, emissive: '#ffe3a6', emissiveIntensity: 0.9, emissiveMap: rings(), flatShading: true });
    const lens = kit.lathe(LENS_PROFILE, lensM, 40);
    lens.position.set(LENS.x, 1.0, LENS.z);
    lens.scale.setScalar(0.7);
    lens.castShadow = false;
    const shadeM = kit.mat('#2a2a26', 0.5);
    const pageL = kit.box(0.34, 0.025, 0.44, page, DESK.x - 0.17, 0.805, DESK.z + 0.1), pageR = kit.box(0.34, 0.025, 0.44, page, DESK.x + 0.17, 0.805, DESK.z + 0.1);
    pageL.rotation.z = 0.06; pageR.rotation.z = -0.06;
    scene.add(
      wall, kit.plane(14, 14, kit.mat('#3a322b', 0.8)), lens, kit.cyl(0.36, 0.46, 1.0, dark, LENS.x, 0.5, LENS.z),
      kit.box(1.5, 0.06, 0.75, wood, DESK.x, 0.76, DESK.z), kit.box(0.06, 0.73, 0.7, wood, DESK.x - 0.7, 0.365, DESK.z),
      kit.box(0.06, 0.73, 0.7, wood, DESK.x + 0.7, 0.365, DESK.z), pageL, pageR,
      kit.cyl(0.07, 0.17, 0.16, shadeM, DESK.x - 0.45, 1.26, DESK.z - 0.15, true), kit.cyl(0.015, 0.015, 0.45, shadeM, DESK.x - 0.55, 1.02, DESK.z - 0.2),
      new THREE.HemisphereLight('#3b4660', '#120e0a', 0.03), // fill: a few hundredths
    );
    // The desk lamp, under its shade: a pool of light on the open log, and nothing else.
    const lamp = kit.spot(scene, [DESK.x - 0.45, 1.24, DESK.z - 0.15], [DESK.x + 0.05, 0.8, DESK.z + 0.1], { intensity: 7, angle: 0.7, penumbra: 0.7, distance: 2.4, colour: '#ffc46e' });
    // Moonlight through a window on the right, out of frame: it grazes the back wall and lays the bars on it, behind
    // the keeper, so in beat 1 he stands dark against stripes of light.
    const moon = kit.spot(scene, [3.0, 2.9, -1.6], [-1.6, 1.5, -3.8], { intensity: 12, angle: 0.3, penumbra: 0.2, distance: 20, shadow: 1024, colour: '#cbd8f2' });
    kit.gobo(moon, { count: 6, gap: 0.12, thickness: 0.035, width: 2, turn: Math.PI / 2, at: 1.0 });
    kit.gobo(moon, { count: 3, gap: 0.3, thickness: 0.05, width: 2, turn: 0, at: 1.02 });
    // The lens's beam, turning with the tower's: it sweeps the walls; at t = 0 it points behind the camera.
    const beam = kit.spot(scene, [LENS.x, 1.6, LENS.z], [LENS.x, 1.6, 0], { intensity: 9, angle: 0.2, penumbra: 0.5, distance: 14, colour: '#fff0cf' });

    const keeper = kit.figure({ coat: '#2a2f38' });
    scene.add(keeper);
    const target = new THREE.Vector3(), from = new THREE.Vector3(), dir = new THREE.Vector3();
    return {
      scene,
      camera,
      update(s) {
        // Beat 1 he stands at the desk's end, facing the log; early in beat 2 he goes to the lens and faces it.
        const go = s.k(1, 0.3, 3.5);
        keeper.position.set(lerp(-0.7, 0.95, go), 0, lerp(DESK.z - 0.1, -0.55, go));
        keeper.rotation.y = lerp(-Math.PI / 2, -3.85, go);

        // Beat 1: near desk height from the side, pushing in slowly on the log. Beat 2: low and close, the lens large in
        // the middle of the frame, the keeper's head and shoulders dark against it.
        const shift = s.k(1, 0, 4);
        const near = kit.fit(camera, s.aspect, 3.0, 1.9) * (1.25 - 0.2 * s.k(0, 0, 7));
        const d = lerp(near, kit.fit(camera, s.aspect, 1.9, 1.7), shift);
        target.set(lerp(DESK.x + 0.45, LENS.x - 0.15, shift), lerp(1.05, 1.55, shift), lerp(DESK.z, LENS.z + 0.1, shift));
        from.set(lerp(0.3, -0.5, shift) + s.pointer.x * 0.04, lerp(0.1, 0.0, shift) - s.pointer.y * 0.03, 1).normalize();
        camera.position.copy(target).addScaledVector(from, d);
        camera.lookAt(target);

        const a = (s.t / TURN) * Math.PI * 2;
        dir.set(Math.sin(a), -0.06, Math.cos(a));
        beam.target.position.copy(beam.position).add(dir);
        beam.target.updateMatrixWorld();
        lamp.intensity = 7 * (0.94 + 0.06 * Math.sin(s.t * 7.3) * Math.sin(s.t * 2.9)); // the wick
      },
    };
  },
};
