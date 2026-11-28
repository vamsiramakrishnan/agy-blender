// The title: the cloud over the plain, seen from far off across the wind, the sun behind the left shoulder. Its
// shadow lies on the plain to its right, and just beyond the shadow's tip stands the house, a dot with a roof. The
// cloud drifts, slowly, towards it.

export default {
  id: 'cloud',
  world: 'sky',
  ambient: true, // the cloud drifts while the title holds
  beats: [{ dur: 6, hold: 6 }],
  create(kit, w) {
    const from = kit.view(), to = kit.view();
    return {
      update(s) {
        w.cloudTo(0, s.t);
        w.rig.cover(-650, 0, 0, 1300); // the whole of the cloud's shadow, and the house at its tip
        // One slow push, a tenth of the way in.
        w.views.vista(from, s.aspect);
        w.views.vista(to, s.aspect);
        to.eye.lerp(to.aim, 0.1);
        kit.move(w.camera, from, to, s.k(0, 0, 6));
        w.camera.position.x += s.pointer.x * 30;
        w.camera.position.y -= s.pointer.y * 12;
      },
    };
  },
};
