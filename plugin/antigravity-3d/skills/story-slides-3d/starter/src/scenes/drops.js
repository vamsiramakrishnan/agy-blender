// "It is water": into the cloud. Beat 1: white air, and drops hanging a few centimetres from the lens (each drawn
// 4 mm across: two hundred times a drop's width). Beat 2: the camera steps back from them to a frame of one cubic
// metre; the air round it clears, and the drops inside it run together into one bead the size of a pea, which is all
// the water there is in it. Beat 3: back out, far off; the cloud's white shape thins away and the field's dots are
// what is left, filling its kilometre: each dot stands for forty million million drops.
import * as THREE from 'three';
import { OVER, BEAD, DOTS } from '../worlds/sky.js';

const lerp = THREE.MathUtils.lerp;

export default {
  id: 'drops',
  world: 'sky',
  ambient: true,
  beats: [{ dur: 5, hold: 6 }, { dur: 7, hold: 6 }, { dur: 8, hold: 6 }],
  create(kit, w) {
    const within = kit.view(), metre = kit.view(), far = kit.view();
    return {
      update(s) {
        const toFrame = s.k(1, 0, 3.4), alone = s.k(1, 2.2, 4.2), gather = s.k(1, 4.2, 6.4), out = s.k(2, 0, 5.5);
        // The cloud stands where the last scene left it: the dots are laid out for a cloud that stands there.
        w.cloudTo(OVER, 0, 0);
        w.km.mesh.visible = true; // the frame of the last scene is still round it: the air hides it from inside
        w.km.grow(1);

        w.views.within(within, s.aspect);
        w.views.metre(metre, s.aspect);
        w.views.far(far, s.aspect);
        if (s.beat < 2) kit.move(w.camera, within, metre, toFrame); else kit.move(w.camera, metre, far, out);
        // the pointer's parallax: across the view inside the cloud, and across it far off (as in every scene that
        // stands there, so the next scene opens on this one's last picture)
        w.camera.position.z += s.pointer.x * 0.004 * (1 - out);
        w.camera.position.x += s.pointer.x * 30 * out;
        // The air: thick once the travel in has nearly arrived, and for as long as the camera is inside the cloud.
        w.mist(kit.ease((s.enter - 0.7) / 0.3), lerp(1.6, 9, toFrame));

        // The drops by the lens, and behind them the nearest of the field's dots as specks. Then only the specks
        // inside the cubic metre are left, and they run together. Then all the dots, each in its place in the cloud.
        w.beads.mesh.visible = w.drops.mesh.visible = s.enter > 0.6;
        w.beads.morph('near', 'gone', alone);
        if (s.beat < 2) w.drops.show(w.NEAR / DOTS); else w.drops.show(1);
        if (s.beat === 2) w.drops.morph('bead', 'cloud', s.k(2, 2.6, 7.4));
        else if (gather > 0) w.drops.morph('boxed', 'bead', gather, 0.4);
        else w.drops.morph('near', 'boxed', alone, 0);
        // The cubic metre and the bead its drops make.
        w.metre.mesh.visible = s.beat >= 1 && out < 0.25;
        w.metre.grow(s.k(1, 0.8, 3));
        w.bead.visible = gather > 0 && out < 0.25;
        w.bead.scale.setScalar(BEAD * Math.max(kit.ease((gather - 0.3) / 0.7), 0.001));
        // The cloud's white shape thins into grain once the camera is out, and leaves the dots.
        w.solid(1 - s.k(2, 2.4, 7));
        w.rig.cover(-500, 0, 0, 1600);
      },
    };
  },
};
