// field.js: for a count, or a thing made of many. "A hundred thousand million million drops", "one page becomes fifty
// thousand", "a crowd": the claim is that many things, so the picture is that many things (or a few thousand that
// stand for them: say in the scene how many each one stands for).
//
// One InstancedMesh, so the whole count is one draw. A layout maps an index to a place and a size; show(x) shows the
// first share x of them; morph(from, to, x) moves every one from one layout to another. All three are absolute (the
// same call gives the same picture), so a scene's update can call them with functions of its state.
import * as THREE from 'three';

// What one of the many is: a round drop (smooth enough to come close to), a dot (cheaper, for thousands far off), a
// block. Each is one metre across: a layout's `size` is then metres.
const SHAPES = {
  drop: () => new THREE.IcosahedronGeometry(0.5, 3),
  dot: () => new THREE.IcosahedronGeometry(0.5, 1),
  block: () => new THREE.BoxGeometry(1, 1, 1),
};

// field(kit, { count, seed, shape, paint, stagger, order, shadow })
// - shape: 'drop', 'dot', 'block', or a geometry of your own;
// - paint: a theme colour for kit.paint ('accent': the thing the story follows), or a material;
// - stagger: in a morph, how far apart in time the first and the last set off (0: all together; 1: the last sets off
//   when the first arrives), and `order`: 'index' (the first of the layout goes first: lay a pile out from the bottom
//   up and it fills from the bottom) or 'random' (seeded);
// - shadow: true if they cast shadows (thousands of small shadows are dear: leave it off).
export function field(kit, { count, seed = 1, shape = 'drop', paint = 'accent', stagger = 0.5, order = 'index', shadow = false } = {}) {
  const geometry = typeof shape === 'string' ? SHAPES[shape]() : shape;
  const mesh = new THREE.InstancedMesh(geometry, typeof paint === 'string' ? kit.paint(paint) : paint, count);
  mesh.frustumCulled = false; // its instances go wherever the layouts send them
  mesh.castShadow = shadow;
  const m = mesh.instanceMatrix.array, layouts = {};
  for (let i = 0; i < count; i++) m[i * 16 + 15] = 1;
  const random = kit.rnd(seed), delay = new Float32Array(count);
  for (let i = 0; i < count; i++) delay[i] = order === 'random' ? random() : i / Math.max(1, count - 1);
  const place = (i, x, y, z, size) => {
    const o = i * 16;
    m[o] = m[o + 5] = m[o + 10] = size;
    m[o + 12] = x; m[o + 13] = y; m[o + 14] = z;
  };

  const self = {
    mesh, count,
    // A layout: fn(i, p, rnd) sets p.x, p.y, p.z (metres) and p.size (one thing's width; 0 hides it) for index i;
    // rnd() is a seeded 0..1. It is run once, here: a layout is a table, and a morph only reads tables.
    layout(name, fn) {
      const table = new Float32Array(count * 4), p = { x: 0, y: 0, z: 0, size: 1 }, rnd = kit.rnd(seed * 7919 + name.length * 104729 + name.charCodeAt(0));
      for (let i = 0; i < count; i++) {
        p.x = p.y = p.z = 0; p.size = 1;
        fn(i, p, rnd);
        table.set([p.x, p.y, p.z, p.size], i * 4);
      }
      layouts[name] = table;
      return self;
    },
    // Shows the first share x (0..1) of them, in the layout's order.
    show(x) {
      mesh.count = Math.round(Math.min(1, Math.max(0, x)) * count);
      return mesh.count;
    },
    // Every one between its place in layout `from` and its place in layout `to`: all at `from` at x = 0, all at `to`
    // at x = 1, each easing along a straight line in its own stretch of x (the field's stagger, or `apart` for this
    // morph).
    morph(from, to, x, apart = stagger) {
      const a = layouts[from], b = layouts[to];
      if (!a || !b) throw new Error(`field: no layout "${a ? to : from}"`);
      for (let i = 0; i < count; i++) {
        let u = Math.min(1, Math.max(0, x * (1 + apart) - delay[i] * apart));
        u = u * u * (3 - 2 * u);
        const o = i * 4;
        place(i, a[o] + (b[o] - a[o]) * u, a[o + 1] + (b[o + 1] - a[o + 1]) * u, a[o + 2] + (b[o + 2] - a[o + 2]) * u, a[o + 3] + (b[o + 3] - a[o + 3]) * u);
      }
      mesh.instanceMatrix.needsUpdate = true;
    },
    // The share of them that have arrived at x of a morph: what fills as they come (a level, a tally).
    arrived: (x, apart = stagger) => (apart > 0 ? Math.min(1, Math.max(0, (x * (1 + apart) - 1) / apart)) : x >= 1 ? 1 : 0),
    // Where one of them is now, and how large: for a camera that follows one, and for the tests.
    at(i, into = new THREE.Vector3()) { return into.set(m[i * 16 + 12], m[i * 16 + 13], m[i * 16 + 14]); },
    sizeOf(i) { return m[i * 16]; },
  };
  return self;
}
