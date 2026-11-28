// "So why is it up there?": the cube's water goes back up, dot by dot, and the cloud closes over its drops again,
// seen from far off (beat 1: the pour of the last scene, reversed). Beat 2: inside the cloud, one drop sinking past
// a ruler marked in centimetres, at its true speed: a little more than one mark a second. Beat 3: a step back, and
// the air beside it, rising a hundred times as fast: two streams driven by one clock.
import * as THREE from 'three';
import { OVER, FALL } from '../worlds/sky.js';

const lerp = THREE.MathUtils.lerp;
// The water goes back up while the camera travels out of the yard, and on through the first beat: one clock for
// both, the seconds since the entrance began. ENTER is the entrance's length and BEATS[0].dur the first beat's; the
// last dot is back in its place 5.2 s into that beat.
const ENTER = 1.6, BEATS = [{ dur: 7, hold: 4 }, { dur: 5, hold: 6 }, { dur: 5, hold: 7 }];

export default {
  id: 'floats',
  world: 'sky',
  travel: ENTER,
  ambient: true,
  beats: BEATS,
  create(kit, w) {
    const yard = kit.view(), far = kit.view(), mark = kit.view(), streams = kit.view();
    return {
      update(s) {
        // The pour of the last scene, reversed: the dots leave the cube at a steady rate, the last to arrive first
        // (each eases along its own way). Then the cloud closes over its drops, and is whole.
        const pour = 1 - Math.min(1, (ENTER * s.enter + BEATS[0].dur * s.p[0]) / (ENTER + 5.2)), whole = s.k(0, 4.4, 6.6);
        w.cloudTo(OVER, 0, 0);
        w.solid(whole);
        w.km.mesh.visible = true;
        w.km.grow(1);
        w.drops.mesh.visible = whole < 1;
        w.drops.show(1);
        w.drops.morph('cloud', 'cube', pour);
        w.water(w.drops.arrived(pour));
        w.tape.mesh.visible = s.enter < 1; // the last scene's ruler goes as this one comes
        w.tape.grow(1 - s.enter);

        const out = kit.ease(s.enter), into = s.k(1, 0, 3.4), back = s.k(2, 0, 2.6);
        w.views.yard(yard, s.aspect);
        w.views.far(far, s.aspect);
        w.views.mark(mark, s.aspect);
        w.views.streams(streams, s.aspect);
        // The entrance is from the yard to far off, a hundred times as far from the house: on the engine's straight
        // way between the two the yard is a speck within the first tenth of the time. So during its entrance this
        // scene's own place is already on the even way out (kit.move, by s.enter), and the travel takes the camera
        // towards that: the house and the cube stay in the picture while they shrink and the water leaves.
        if (s.enter < 1) kit.move(w.camera, yard, far, out);
        else if (s.beat < 2) kit.move(w.camera, far, mark, into);
        else kit.move(w.camera, mark, streams, back);
        // the pointer's parallax: across the view in the yard and far off (as in every scene that stands there), and
        // across it inside the cloud
        w.camera.position.x += s.pointer.x * lerp(0.4, 30, out) * (1 - into);
        w.camera.position.z += s.pointer.x * 0.004 * into;
        w.mist(1, lerp(1.2, 3, back));
        // the shadow box opens from the yard (where the last scene left it) as the camera leaves
        w.rig.cover(lerp(8, -500, out), 0, lerp(4.5, 0, out), 14 * Math.pow(1600 / 14, out));

        // The ruler, and one drop beside it. It sinks FALL metres a second, and comes round again at the top after
        // the ruler's ten centimetres; at the ends of its way it is small, so it does not pop.
        w.scale.mesh.visible = s.beat >= 1;
        w.scale.grow(s.k(1, 1.2, 3.4));
        const fallen = (FALL * s.t + 0.042) % 0.1, end = Math.min(1, Math.min(fallen, 0.1 - fallen) / 0.008);
        w.drop.visible = s.beat >= 1;
        w.drop.position.set(w.MARK.x, w.MARK.y + 0.1 - fallen, w.MARK.z - 0.012);
        w.drop.scale.setScalar(Math.max(0.004 * end * s.k(1, 2.6, 3.6), 1e-6));
        // The two streams: the drops' fall and the air's rise.
        w.air.group.visible = s.beat >= 2;
        w.air.flow(s.t, s.k(2, 1.0, 3));
      },
    };
  },
};
