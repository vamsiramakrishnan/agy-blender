// "It looks like nothing at all": from the grass by the house, looking up the wind as the cloud comes over and its
// shadow crosses the house (beat 1). Then the camera goes back out to where the whole cloud is small in the frame,
// and a frame a kilometre each way comes up round it; the house is a speck at the tip of the shadow (beat 2).
import * as THREE from 'three';
import { OVER } from '../worlds/sky.js';

const lerp = THREE.MathUtils.lerp;

export default {
  id: 'looks',
  world: 'sky',
  ambient: true,
  beats: [{ dur: 6, hold: 5 }, { dur: 7, hold: 6 }],
  create(kit, w) {
    const near = kit.view(), far = kit.view();
    return {
      update(s) {
        const back = s.k(1, 0, 5.5), come = s.k(0, 0.8, 5.2);
        // Beat 1: the cloud comes down the wind until the tip of its shadow has gone over the house. It stops
        // drifting as it comes: from here to the last scene it stands exactly where the talk takes it apart.
        w.cloudTo(OVER * come, s.t, 1 - come);
        // Beat 2: the frame is drawn once the camera is most of the way back.
        w.km.mesh.visible = s.beat >= 1;
        w.km.grow(s.k(1, 2.4, 6.6));
        w.views.grass(near, s.aspect);
        w.views.far(far, s.aspect);
        kit.move(w.camera, near, far, back);
        w.camera.position.x += s.pointer.x * lerp(0.3, 30, back);
        // the shadow box: round the house while the camera is by it, round the whole shadow when it is far off
        w.rig.cover(lerp(-120, -500, back), 0, 0, 520 * Math.pow(1600 / 520, back));
      },
    };
  },
};
