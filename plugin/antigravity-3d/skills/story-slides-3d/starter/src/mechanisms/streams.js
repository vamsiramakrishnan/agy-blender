// streams.js: for a rate, or two rates. "A centimetre a second", "twice as fast", "a hundred times faster": the claim
// is a speed, so the picture is things going past at that speed, in the set's metres and real seconds; two streams
// side by side, driven by the same clock, show a ratio without a word.
//
// Each stream is specks that run through a stretch of space along a direction and come round again: one
// InstancedMesh (one draw). flow(t, amount) places them for a time t in seconds (the scene's ambient time, s.t), at
// `amount` of their size (0..1, to bring them in; 1 when left out): absolute, so the same call is the same picture.
// A scene that has streams says `ambient: true`.
import * as THREE from 'three';

// streams(kit, list): one stream's options or a list of them, each
// { speed, direction, length, across, count, size, streak, seed, paint, at }:
// - speed: metres a second along `direction` ([x, y, z]; a negative speed goes the other way);
// - length: metres of the stretch they run through, centred on `at` ([x, y, z], the origin when left out);
// - across: [width, depth] of the stretch, in which they are scattered;
// - size: a speck's width in metres; streak: how many times longer than wide it is along the direction (1: round).
//   Give a fast stream the length it travels in a tenth of a second and a slow one a round speck, and a still frame
//   shows the two speeds;
// - paint: a theme colour ('accent', 'accent-2', 'ink' ...) or a material.
// It returns { group, each, flow(t, amount) }; each stream has { mesh, speed, length, gone(t), at(i, into) } and a
// flow of its own.
export function streams(kit, list) {
  const group = new THREE.Group();
  const each = (Array.isArray(list) ? list : [list]).map((o, n) => {
    const { speed = 1, direction = [0, 1, 0], length = 2, across = [0.5, 0.5], count = 24, size = 0.03, streak = 1, seed = n + 1, paint = 'accent', at = [0, 0, 0] } = o;
    // A round speck, or for a streak a cone with its blunt end leading and its point trailing, so a still shows
    // which way it goes.
    const shape = streak > 1 ? new THREE.ConeGeometry(0.5, 1, 14).rotateX(Math.PI) : new THREE.IcosahedronGeometry(0.5, 2);
    const mesh = new THREE.InstancedMesh(shape, typeof paint === 'string' ? kit.paint(paint) : paint, count);
    mesh.frustumCulled = false;
    mesh.castShadow = false;
    const dir = new THREE.Vector3(...direction).normalize();
    const ex = new THREE.Vector3(0, 0, 1).cross(dir);
    if (ex.lengthSq() < 1e-6) ex.set(1, 0, 0);
    ex.normalize();
    const ez = new THREE.Vector3().crossVectors(ex, dir);
    // each speck: where along the stretch it is at t = 0, and its place across it
    const rnd = kit.rnd(seed * 31 + 7), starts = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) starts.set([(i + rnd()) / count * length, (rnd() - 0.5) * across[0], (rnd() - 0.5) * across[1]], i * 3);
    const m = mesh.instanceMatrix.array;
    for (let i = 0; i < count; i++) m[i * 16 + 15] = 1;
    const stream = {
      mesh, speed, length,
      // How far a speck has gone after t seconds, in metres: the number the picture shows.
      gone: (t) => speed * t,
      at: (i, into = new THREE.Vector3()) => into.set(m[i * 16 + 12], m[i * 16 + 13], m[i * 16 + 14]),
      flow(t, amount = 1) {
        for (let i = 0; i < count; i++) {
          const o = i * 16, along = ((((starts[i * 3] + speed * t) % length) + length) % length) - length / 2;
          // a speck comes in and goes out small at the two ends of the stretch, so none pops
          const e = Math.min(1, (length / 2 - Math.abs(along)) / (0.12 * length)), f = size * amount * e * e * (3 - 2 * e);
          const a = starts[i * 3 + 1], b = starts[i * 3 + 2];
          m[o] = ex.x * f; m[o + 1] = ex.y * f; m[o + 2] = ex.z * f;
          m[o + 4] = dir.x * f * streak; m[o + 5] = dir.y * f * streak; m[o + 6] = dir.z * f * streak;
          m[o + 8] = ez.x * f; m[o + 9] = ez.y * f; m[o + 10] = ez.z * f;
          m[o + 12] = at[0] + dir.x * along + ex.x * a + ez.x * b;
          m[o + 13] = at[1] + dir.y * along + ex.y * a + ez.y * b;
          m[o + 14] = at[2] + dir.z * along + ex.z * a + ez.z * b;
        }
        mesh.instanceMatrix.needsUpdate = true;
      },
    };
    stream.flow(0);
    group.add(mesh);
    return stream;
  });
  return {
    group, each,
    // Every stream at time t (seconds); `amount` 0..1 is the specks' size, for bringing the streams in.
    flow(t, amount = 1) { for (let k = 0; k < each.length; k++) each[k].flow(t, amount); },
  };
}
