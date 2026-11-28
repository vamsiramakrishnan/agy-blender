// "Look up": by the house, looking up. The cloud has come on down the wind and stands overhead, its flat grey base
// filling the top of the frame; its shadow passes on, and the sun comes back onto the house.
import * as THREE from 'three';
import { OVER } from '../worlds/sky.js';

const lerp = THREE.MathUtils.lerp;

export default {
  id: 'up',
  world: 'sky',
  ambient: true,
  beats: [{ dur: 7, hold: 8 }],
  create(kit, w) {
    const step = kit.view(), higher = kit.view();
    return {
      update(s) {
        const wide = s.aspect >= 1, on = s.k(0, 0, 7);
        // The travel in brings the cloud most of the way; the beat brings it overhead, and its shadow off the house.
        w.cloudTo(lerp(OVER, 1220, kit.ease(s.enter)) + 270 * on, s.t, s.enter);
        w.km.mesh.visible = s.enter < 0.6; // the frame goes as the camera comes down to the house
        w.km.grow(1 - s.enter / 0.6);
        w.views.step(step, s.aspect);
        w.views.step(higher, s.aspect);
        higher.aim.y += wide ? 7 : 2.5; // the camera tilts up a little as the cloud comes over
        kit.move(w.camera, step, higher, on);
        w.camera.position.x += s.pointer.x * 0.3;
        // The shadow box: round the whole of the cloud's shadow while the camera is still high on its way in and the
        // plain is in the frame, and close round the house once it is down and looks up (the eave's shadow on the
        // wall is in the foreground then, and wants all of the map's pixels).
        const close = kit.ease((s.enter - 0.6) / 0.4);
        w.rig.cover(lerp(-500, 0, close), lerp(0, 3.8, close), 0, 1600 * Math.pow(9 / 1600, close));
      },
    };
  },
};
