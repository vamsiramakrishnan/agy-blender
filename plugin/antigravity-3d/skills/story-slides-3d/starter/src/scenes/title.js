// The title card: the film's name on a card on a wall, a lighthouse beam passing over it through a window's blinds
// once every nine seconds, as it will all film. The card is lit by the scene and printed like any set, so the title
// is already in the film's look. end.js uses the same set.
import * as THREE from 'three';

// The period of the beam, the film's one clock: "The light went round every nine seconds."
export const TURN = 9;

// A wall, a card of large type on it, a spot light behind slanted blinds aimed at the wall, a little fill so the
// card reads when the beam is away.
export function cardSet(kit, words) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#05070b');
  const camera = new THREE.PerspectiveCamera(36, 1, 0.3, 60);
  const wall = kit.plane(22, 14, kit.mat('#50545d', 0.95), true);
  wall.position.z = -0.05;
  const map = kit.card(words);
  const face = new THREE.MeshStandardMaterial({ map, emissive: '#ffffff', emissiveMap: map, roughness: 0.9 });
  const cardMesh = kit.plane(6.4, 3.2, face, true);
  scene.add(wall, cardMesh, new THREE.AmbientLight('#9fb0c8', 0.04));
  const beam = kit.spot(scene, [-2.5, 2.2, 9.5], [0, 0.3, 0], { intensity: 11, angle: 0.34, penumbra: 0.35, distance: 30, shadow: 1024, colour: '#fff1d6' });
  const blinds = kit.gobo(beam, { count: 9, gap: 0.13, thickness: 0.065, width: 2.4, turn: 0.32, at: 1.4 });
  return { scene, camera, face, beam, blinds };
}

// The camera a little below the card's centre so a headline over the picture's lower left leaves the card clear. In
// an upright frame the card's type fills the width but for a small margin: a phone's cells are few.
export function frameCard(kit, set, s, distance) {
  const d = kit.fit(set.camera, s.aspect, s.aspect < 1 ? 6.3 : 7.4, 5.6) * distance;
  set.camera.position.set(s.pointer.x * 0.3, -0.55 - s.pointer.y * 0.15, d);
  set.camera.lookAt(0, -0.55, 0);
}

export default {
  id: 'title',
  enterWith: 'fade',
  ambient: true, // the beam keeps turning while the title holds
  beats: [{ dur: 4, hold: 4 }],
  create(kit) {
    const set = cardSet(kit, ['KEEPER']);
    return {
      scene: set.scene,
      camera: set.camera,
      update(s) {
        frameCard(kit, set, s, 1.12 - 0.1 * s.k(0, 0, 4)); // one slow push in
        set.face.emissiveIntensity = 0.05 + 0.22 * s.k(0, 0.3, 3); // the card comes up out of the dark
        // The beam crosses the wall from left to right once a turn, and is on the card at t = 0 (the still).
        const x = (((s.t / TURN + 0.5) % 1) + 1) % 1 * 30 - 15;
        set.beam.target.position.set(x, 0.3, 0);
        set.beam.target.updateMatrixWorld();
        set.blinds.slide(Math.sin(s.t * 0.4) * 0.01); // the blinds stir
      },
    };
  },
};
