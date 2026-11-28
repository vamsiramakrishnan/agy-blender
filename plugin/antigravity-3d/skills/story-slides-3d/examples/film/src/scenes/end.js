// The end card: the title's wall again, the beam come to rest on the card, the camera drawing back. Nothing moves
// once the beat has played, so the film stops drawing (film.js draws only while something moves).
import { cardSet, frameCard } from './title.js';

export default {
  id: 'end',
  enterWith: 'fade',
  beats: [{ dur: 5, hold: 6 }],
  create(kit) {
    const set = cardSet(kit, ['END']);
    set.beam.target.position.set(0.4, 0.2, 0);
    return {
      scene: set.scene,
      camera: set.camera,
      update(s) {
        frameCard(kit, set, s, 1.0 + 0.14 * s.k(0, 0, 5));
        set.face.emissiveIntensity = 0.04 + 0.2 * s.k(0, 0.5, 3.5);
        set.beam.intensity = 11 * (0.35 + 0.65 * s.k(0, 0, 2.5)); // the light settles on the card
      },
    };
  },
};
