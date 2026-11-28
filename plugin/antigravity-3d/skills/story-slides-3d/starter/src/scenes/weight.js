// "Pour them together": the field's dots leave their places in the cloud and run down into a cube beside the house,
// which fills from the bottom as they arrive; the camera follows them down from far off into the yard (beat 1). A
// ruler comes up between the house and the cube, eight metres, as the camera comes a step nearer (beat 2). Then the
// picture holds for the number (beat 3): only the camera's slow sway is left.
import * as THREE from 'three';
import { OVER } from '../worlds/sky.js';

const lerp = THREE.MathUtils.lerp;
// The scene begins where the last one ended (the same place, the same picture), so its entrance has nowhere to take
// the camera. The pour begins with the entrance instead and runs on through the first beat, on one clock: the seconds
// since the entrance began. ENTER is the entrance's length and BEATS[0].dur the first beat's; the pour ends 7.6 s
// into that beat.
const ENTER = 1.6, BEATS = [{ dur: 8, hold: 4 }, { dur: 5, hold: 5 }, { dur: 2, hold: 8 }];

export default {
  id: 'weight',
  world: 'sky',
  travel: ENTER,
  ambient: true,
  beats: BEATS,
  create(kit, w) {
    const far = kit.view(), yard = kit.view();
    return {
      update(s) {
        const pour = Math.min(1, (ENTER * s.enter + BEATS[0].dur * s.p[0]) / (ENTER + 7.6)), down = s.k(0, 1.2, 7.6);
        w.cloudTo(OVER, 0, 0);
        w.solid(0); // the cloud is its drops now
        w.km.mesh.visible = true; // the frame they came out of stays where it was
        w.km.grow(1);
        // The dots set off one after another at a steady rate (the field's stagger; each eases along its own way),
        // so the cube fills as fast as they arrive: the share that has arrived is the water's level.
        w.drops.mesh.visible = pour < 1;
        w.drops.show(1);
        w.drops.morph('cloud', 'cube', pour);
        w.water(w.drops.arrived(pour));
        w.tape.mesh.visible = s.beat >= 1;
        w.tape.grow(s.k(1, 0.5, 3.8));

        w.views.far(far, s.aspect);
        w.views.yard(yard, s.aspect);
        yard.eye.lerp(yard.aim, 0.07 * s.k(1, 0, 5)); // beat 2: a step nearer
        kit.move(w.camera, far, yard, down);
        // In the yard the camera sways a hand's breadth, slowly, so the held picture is not a photograph.
        w.camera.position.x += s.pointer.x * lerp(30, 0.4, down) + down * 0.25 * Math.sin(s.t * 0.35);
        w.camera.position.y += down * 0.1 * Math.sin(s.t * 0.27);
        // the shadow box closes in with the camera, onto the house, the cube and their shadows on the grass
        w.rig.cover(lerp(-500, 8, down), 0, lerp(0, 4.5, down), 1600 * Math.pow(14 / 1600, down));
      },
    };
  },
};
