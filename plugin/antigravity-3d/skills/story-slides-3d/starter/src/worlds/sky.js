// sky.js: the deck's one world. A plain to the horizon, one cloud a kilometre each way, its shadow on the ground, one
// small house; and the things the talk measures the cloud with (a frame, a field of drops, a cube of water, two
// rulers, two streams), each built once here and posed by the scenes. Everything is at its true size in metres, and
// every number the talk states is read from facts.json, so the words and the picture cannot disagree.
//
// The world is built once; a scene's update poses it. The engine calls rest() before each update, so a part a scene
// does not pose is put away, whichever scene posed the world last.
import * as THREE from 'three';
import facts from '../../facts.json';
import { field } from '../mechanisms/field.js';
import { frame } from '../mechanisms/frame.js';
import { ruler } from '../mechanisms/ruler.js';
import { streams } from '../mechanisms/streams.js';

// ---- the talk's numbers (facts.json)
export const SIDE = facts.premises.side.value;          // the cloud: a kilometre long, wide and tall
export const CUBE = facts.derived.cube.value;           // its water poured together: a cube 7.94 m on a side
export const FALL = facts.derived.fall.value / 100;     // a drop's fall, metres a second
export const RISE = facts.premises.rise.value;          // the air's rise under the cloud, metres a second
// All the drops of one cubic metre as one bead: half a gram of water is half a cubic centimetre, a sphere this wide.
export const BEAD = Math.cbrt((6 * facts.premises.water.value * 1e-6) / Math.PI);
// The field's dots: each stands for this many of the cloud's drops (about forty million million).
export const DOTS = 3000;
export const DROPS_A_DOT = facts.derived.drops.value / DOTS;

// ---- where things are, in metres. The house stands on the origin. The wind blows along +x, and the sun stands in
// the west, up the wind, 58 degrees high: a thing's shadow lies 1 / SLOPE of its height down the wind from it. So
// the cloud's shadow runs ahead of the cloud, and its tip is the first of it to reach the house.
const SLOPE = 1.6;
export const SUN = new THREE.Vector3(-1, SLOPE, 0).normalize();
export const BASE = 600; // the cloud's flat base above the plain
// The middle of the cloud's base when the cloud has come `along` metres down the wind since the talk began. At 0 the
// tip of its shadow is 150 m short of the house; from 150 to about 1400 the house is in the shadow; then the cloud
// is over the house and its shadow has gone on.
const START = 1300;
export const cloudAt = (along, into) => into.set(along - START, BASE, 0);
// Where the cloud stands while the talk takes it apart: the tip of its shadow lies over the house, and ends some
// 30 m beyond it.
export const OVER = 170;
// A place inside the cloud, from the middle of its base: where the camera goes to look at drops.
const INSIDE = new THREE.Vector3(-80, 240, 120);

// The cloud, from the middle of its base. Its floor is a ring of low domes, [x, z, radius, height], each half an
// ellipsoid standing on the base, so the base is flat (a cumulus has one); on them stands a pile of balls,
// [x, y, z, radius]. Together they fill the kilometre each way.
const DOMES = [
  [0, 0, 420, 300], [-310, -70, 200, 220], [310, 70, 210, 230], [-70, 300, 200, 210], [90, -290, 210, 220],
  [-400, 140, 100, 130], [410, -160, 90, 120], [340, 310, 120, 150], [-320, -310, 140, 170], [160, 400, 100, 120],
  [-180, 400, 100, 110], [280, -360, 120, 140], [-450, -50, 50, 70], [455, 60, 45, 60],
];
const BALLS = [
  [-230, 330, 60, 250], [250, 310, -40, 240], [0, 350, 240, 230], [30, 330, -250, 240], [0, 400, 0, 330], // the middle
  [-150, 580, -60, 230], [190, 560, 80, 220], [-20, 600, 170, 190], [80, 620, -170, 190],                 // the shoulders
  [40, 800, 10, 200], [-110, 740, 40, 150],                                                               // the head
];
// Whether a point (from the middle of the base) is inside the cloud, a little way under its surface.
function inCloud(x, y, z) {
  if (y < 8) return false;
  for (const [px, pz, r, h] of DOMES) if (((x - px) / r) ** 2 + (y / h) ** 2 + ((z - pz) / r) ** 2 < 0.86) return true;
  for (const [px, py, pz, r] of BALLS) if ((x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2 < 0.86 * r * r) return true;
  return false;
}

// The house until one is built and installed (props.js): two storeys of blocks with a ledge between them, under a
// roof; a door, six windows and a chimney. Nine metres long, 7.6 to the ridge. Its origin is the middle of its base
// and its door faces +z.
function houseOfBlocks(kit) {
  // Its blocks stand on one another, and each casts its shadow by the faces the sun sees (shadowSide): by the far
  // faces, which is the default, a line of light gets in where one block touches the next (under the eave).
  const sunward = { shadowSide: THREE.FrontSide };
  const wall = kit.paint('lit', sunward), roof = kit.paint('accent-2', sunward), dark = kit.paint('ink', { roughness: 0.6 });
  const g = new THREE.Group();
  g.add(
    kit.box(9, 2.7, 7, wall, 0, 1.35, 0), kit.box(9.24, 0.14, 7.24, wall, 0, 2.77, 0), kit.box(9, 2.36, 7, wall, 0, 4.02, 0),
    kit.prism(9.7, 2.4, 7.9, roof, 0, 5.2, 0), kit.box(0.8, 1.9, 0.8, wall, 2.6, 6.3, -1.2),
    kit.box(1.1, 2.1, 0.12, dark, -0.4, 1.05, 3.5),
  );
  for (const y of [1.55, 4.1]) for (const x of [-3, 2.2, 3.4]) g.add(kit.box(0.8, 1.1, 0.12, dark, x, y, 3.5));
  return g;
}

export async function create(kit) {
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(40, 1, 5, 90000);
  // The sun is the world's (the shadow must fall on the house); its colour, its softness, the air and the sky are the
  // theme's. The air has taken the plain by eleven kilometres, so the cloud a few kilometres off is nearly clear.
  const rig = kit.rig(scene, { shadow: 2048, from: SUN.toArray(), reach: 11000, height: BASE + SIDE + 100 });
  scene.add(rig.plain(180000));

  // The cloud: three draws (its domes, their floors, its balls). alphaHash lets it thin out into grain (solid())
  // without sorting.
  // The floors (a disc under each dome, facing down) all lie in one plane, and the feet of the domes that stand
  // inside one another touch that plane: left to the depth buffer, the rims of those feet show through the base as
  // hairlines. A floor can only be seen from below, and from below nothing of the cloud is ever in front of it. So
  // the cloud is drawn first of all (its domes and balls, then its floors), and a floor is drawn over whatever of
  // the cloud is there, at its own depth; everything else is drawn after, and is tested against that depth as usual.
  const cloud = new THREE.Group();
  const white = kit.paint('lit', { roughness: 1, alphaHash: true });
  const under = kit.paint('lit', { roughness: 1, alphaHash: true, depthFunc: THREE.AlwaysDepth });
  const domes = new THREE.InstancedMesh(new THREE.SphereGeometry(1, 48, 16, 0, Math.PI * 2, 0, Math.PI / 2), white, DOMES.length);
  const floors = new THREE.InstancedMesh(new THREE.CircleGeometry(1, 48).rotateX(Math.PI / 2), under, DOMES.length);
  const balls = new THREE.InstancedMesh(new THREE.SphereGeometry(1, 48, 24), white, BALLS.length);
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), v = new THREE.Vector3();
  DOMES.forEach(([x, z, r, h], i) => {
    domes.setMatrixAt(i, m.compose(p.set(x, 0, z), q, v.set(r, h, r)));
    floors.setMatrixAt(i, m);
  });
  BALLS.forEach(([x, y, z, r], i) => balls.setMatrixAt(i, m.compose(p.set(x, y, z), q, v.set(r, r, r))));
  for (const puffs of [domes, floors, balls]) {
    puffs.castShadow = puffs.receiveShadow = true;
    puffs.frustumCulled = false;
    puffs.renderOrder = puffs === floors ? -1 : -2;
  }
  // The frame round it: a kilometre each way, standing on the cloud's base.
  const km = frame(kit, { size: SIDE, thickness: 8 });
  cloud.add(domes, floors, balls, km.mesh);
  scene.add(cloud);

  const house = await kit.prop('house', { standIn: () => houseOfBlocks(kit) });
  house.rotation.y = 0.35;
  scene.add(house);

  // The water: a cube beside the house, filled from the bottom (water()), and a ruler as tall as it is in whole
  // metres.
  const CUBE_AT = new THREE.Vector3(12.5, 0, 8.5);
  const cube = kit.box(CUBE, CUBE, CUBE, kit.paint('accent', { roughness: 0.45 }));
  const tape = ruler(kit, { length: Math.round(CUBE), step: 1, major: 4, thickness: 0.09, tick: 0.55 });
  tape.mesh.position.set(5.5, 0, 4.8);  // between the house and the cube, as the camera in the yard sees them
  tape.mesh.rotation.y = -0.735;        // its ticks point at the cube, across that camera's view
  scene.add(cube, tape.mesh);

  // The drops: DOTS dots, one draw, each standing for DROPS_A_DOT drops. Five layouts:
  // - cloud: each in its place in the cloud as it stands at OVER, the nearest to the camera's place inside first;
  // - near: inside the cloud the camera stands at HERE and looks along +x. The nearest NEAR dots hang in the few
  //   metres before it, about fourteen to the cubic metre, each drawn 4 mm across (two hundred times a drop's width,
  //   and still specks). The rest wait at their places in the cloud with no size;
  // - boxed: the same, but only the dots inside the cubic metre at FRAME are left: the others have no size;
  // - bead: those have run together at the frame's middle (together they are about the bead's volume);
  // - cube: packed into the cube from the bottom up, so the cube fills as they arrive.
  const drops = field(kit, { count: DOTS, seed: 7, shape: 'dot', paint: 'accent', stagger: 1.6, order: 'index' });
  const home = cloudAt(OVER, new THREE.Vector3()), HERE = home.clone().add(INSIDE), rnd = kit.rnd(11), inside = [];
  while (inside.length < DOTS) {
    const x = (rnd() - 0.5) * SIDE, y = rnd() * SIDE, z = (rnd() - 0.5) * SIDE;
    if (inCloud(x, y, z)) inside.push([x, y, z, (x - INSIDE.x) ** 2 + (y - INSIDE.y) ** 2 + (z - INSIDE.z) ** 2]);
  }
  inside.sort((a, b) => a[3] - b[3]);
  drops.layout('cloud', (i, o) => { o.x = home.x + inside[i][0]; o.y = home.y + inside[i][1]; o.z = home.z + inside[i][2]; o.size = 11; });
  const FRAME = HERE.clone().add(new THREE.Vector3(2.6, 0, 0.45)), NEAR = 720, nearAt = [];
  const boxed = (o) => Math.max(Math.abs(o.x - FRAME.x), Math.abs(o.y - FRAME.y), Math.abs(o.z - FRAME.z)) < 0.47;
  drops.layout('near', (i, o, r) => {
    o.x = home.x + inside[i][0]; o.y = home.y + inside[i][1]; o.z = home.z + inside[i][2]; o.size = 0;
    if (i < NEAR) { o.x = HERE.x + 0.5 + r() * 5.2; o.y = HERE.y - 1.2 + r() * 2.4; o.z = HERE.z - 1.6 + r() * 4.1; o.size = 0.0035 + r() * 0.0015; }
    nearAt[i] = [o.x, o.y, o.z, o.size];
  });
  drops.layout('boxed', (i, o) => {
    [o.x, o.y, o.z, o.size] = nearAt[i];
    if (!boxed(o)) o.size = 0;
  });
  drops.layout('bead', (i, o) => {
    [o.x, o.y, o.z, o.size] = nearAt[i];
    if (boxed(o) && o.size > 0) { o.x = FRAME.x; o.y = FRAME.y; o.z = FRAME.z; o.size = 0.003; } else o.size = 0;
  });
  // And a dozen of them close to the lens, as smooth beads (the dots are too coarse to come that near): a few
  // centimetres off, to the right and above, where a slide's words are not. [ahead, up, right] of the camera, metres.
  const LENS = [[0.04, 0.004, 0.012], [0.065, 0.016, 0.004], [0.09, -0.007, 0.034], [0.13, 0.03, 0.046], [0.17, 0.012, -0.012],
    [0.23, 0.05, 0.09], [0.3, -0.03, 0.12], [0.36, 0.085, 0.03], [0.45, 0.02, 0.17], [0.52, 0.11, -0.05], [0.6, -0.06, 0.05], [0.75, 0.06, 0.26]];
  const beads = field(kit, { count: LENS.length, shape: new THREE.SphereGeometry(0.5, 40, 20), paint: kit.paint('accent', { roughness: 0.25 }), stagger: 0 });
  beads.layout('near', (i, o) => { o.x = HERE.x + LENS[i][0]; o.y = HERE.y + 0.1 + LENS[i][1]; o.z = HERE.z + 0.45 + LENS[i][2]; o.size = 0.004; });
  beads.layout('gone', (i, o) => { o.x = HERE.x + LENS[i][0]; o.y = HERE.y + 0.1 + LENS[i][1]; o.z = HERE.z + 0.45 + LENS[i][2]; o.size = 0; });
  const per = Math.ceil(Math.cbrt(DOTS)), cell = CUBE / per;
  drops.layout('cube', (i, o, r) => {
    const layer = Math.floor(i / (per * per)), k = i % (per * per);
    o.x = CUBE_AT.x - CUBE / 2 + ((k % per) + 0.2 + r() * 0.6) * cell;
    o.y = (Math.min(layer, per - 1) + 0.2 + r() * 0.6) * cell;
    o.z = CUBE_AT.z - CUBE / 2 + (Math.floor(k / per) + 0.2 + r() * 0.6) * cell;
    o.size = cell * 0.4; // small enough to be inside the water: the cube's faces stay flat
  });
  scene.add(drops.mesh, beads.mesh);

  // Inside the cloud: a frame of one cubic metre and the bead its drops make; a ruler in centimetres with one drop
  // beside it; and the two streams, the drops' fall and the air's rise, at their true speeds. The ruler stands across
  // the camera's line of sight (the camera looks along +x), its ticks to the right.
  const metre = frame(kit, { size: 1, thickness: 0.006 });
  metre.mesh.position.set(FRAME.x, FRAME.y - 0.5, FRAME.z);
  const bead = new THREE.Mesh(new THREE.SphereGeometry(0.5, 32, 16), kit.paint('accent', { roughness: 0.25 }));
  bead.position.copy(FRAME);
  const MARK = HERE.clone().add(new THREE.Vector3(1.1, -0.05, 0.5)); // the foot of the centimetre ruler
  const scale = ruler(kit, { length: 0.1, step: 0.01, major: 5, thickness: 0.0007, tick: 0.006 });
  scale.mesh.position.copy(MARK);
  scale.mesh.rotation.y = -Math.PI / 2;
  const drop = new THREE.Mesh(new THREE.SphereGeometry(0.5, 32, 16), kit.paint('accent', { roughness: 0.25 }));
  // The drops are round specks to the left of the ruler; the air is strokes to its right, each as long as the air
  // goes in a tenth of a second (ten centimetres: the ruler's length), blunt end leading. In that tenth of a second a
  // drop goes a little more than a millimetre: a still frame shows both speeds.
  const air = streams(kit, [
    { speed: FALL, direction: [0, -1, 0], length: 0.36, across: [0.02, 0.06], count: 6, size: 0.005, seed: 3, paint: 'accent', at: [MARK.x, MARK.y + 0.05, MARK.z - 0.055] },
    { speed: RISE, direction: [0, 1, 0], length: 0.56, across: [0.05, 0.15], count: 17, size: 0.0034, streak: (RISE * 0.1) / 0.0034, seed: 5, paint: 'accent-2', at: [MARK.x, MARK.y + 0.05, MARK.z + 0.115] },
  ]);
  scene.add(metre.mesh, bead, scale.mesh, drop, air.group);

  // The camera's places in this world: each fills a view (kit.view) for the frame's shape. Two scenes that share a
  // place share its numbers, so a travel between them has nowhere to go and the picture simply carries on.
  const e = new THREE.Vector3(), a = new THREE.Vector3(), c = new THREE.Vector3(), d3 = new THREE.Vector3();
  const set = (view, eye, aim, fov, near) => { view.eye.copy(eye); view.aim.copy(aim); view.fov = fov; view.near = near; return view; };
  // A view of a subject: a box `w` metres wide and `h` high about the point `at`, seen from the direction `from`
  // (from the subject towards the camera). kit.fit finds the distance at which the subject fills its part of the
  // frame, whatever the frame's shape: in a wide frame the right-hand `fx` and the upper `fy` of it, because a
  // slide's words lie in the lower left; in an upright frame the upper half, because the words take the lower one
  // (and the camera then stands `lift` higher, so that it is not put under the ground).
  const on = (view, aspect, at, w, h, from, near, fx = 0.62, fy = 0.88, fovWide = 40, fovTall = 50, lift = 0) => {
    const wide = aspect >= 1, sx = wide ? fx : 0.9, sy = wide ? fy : 0.5;
    const d = kit.fit(camera, aspect, w / sx, h / sy, fovWide, fovTall);
    d3.copy(from).normalize();
    if (!wide) d3.setY(d3.y + lift).normalize();
    kit.look(camera, at, d3, d, wide ? 1 - sx : 0, 1 - sy);
    return set(view, camera.position, e.copy(camera.position).addScaledVector(d3, -d), camera.fov, near);
  };
  // From the south, a little below the cloud's middle, with the sun behind the left shoulder: the cloud lit from the
  // left, its shadow to its right on the plain, and at the shadow's tip the house.
  const SOUTH = new THREE.Vector3(-0.208, -0.115, 0.978);
  const views = {
    // The title: the cloud where the talk begins, its shadow, and beyond the shadow's tip the house.
    vista: (view, aspect) => on(view, aspect, a.set(-880, 800, 0), 2150, 2150, SOUTH, 5, 0.68, 0.9, 40, 50, 0.27),
    // The cloud where it is taken apart, with room for the frame round it, and the house at the tip of its shadow.
    far: (view, aspect) => on(view, aspect, a.set(-790, 800, 0), 2100, 2200, SOUTH, 5, 0.64, 0.9, 40, 50, 0.27),
    // From the grass down the wind from the house, at a person's height, looking up the wind: the house on the right
    // in the tip of the shadow, the cloud's flat base over it.
    grass: (view, aspect) => (aspect >= 1 ? set(view, e.set(46, 1.7, 13), a.set(-300, -16, 46), 72, 0.5) : set(view, e.set(60, 1.7, 2), a.set(-300, -60, 4), 86, 0.5)),
    // Inside the cloud, looking along +x at the drops that hang a few centimetres before the lens.
    within: (view, aspect) => (aspect >= 1 ? set(view, e.copy(HERE).add(a.set(0, 0.1, 0.45)), a.copy(HERE).add(c.set(3, 0.02, 0.42)), 40, 0.004) : set(view, e.copy(HERE).add(a.set(0, 0.1, 0.45)), a.copy(HERE).add(c.set(3, -0.55, 1.2)), 56, 0.004)),
    // Before the cubic metre.
    metre: (view, aspect) => on(view, aspect, FRAME, 1.9, 1.75, c.set(-0.92, 0.17, 0.36), 0.05, 0.6, 0.92),
    // Before the centimetre ruler: fourteen centimetres of the frame's height.
    mark: (view, aspect) => on(view, aspect, a.copy(MARK).add(c.set(0, 0.05, 0)), 0.05, 0.125, c.set(-1, 0.03, 0), 0.01, 0.5, 0.9),
    // A step back from it: room for the two streams.
    streams: (view, aspect) => on(view, aspect, a.copy(MARK).add(c.set(0, 0.05, 0.03)), 0.34, 0.37, c.set(-1, 0.02, 0), 0.02, 0.62, 0.94),
    // In the yard, from the south-west: the house and the cube of water side by side, their lit sides to the left.
    yard: (view, aspect) => on(view, aspect, a.set(6.5, 4, 4), 31, 11, c.set(-0.66, 0.07, 0.74), 0.5, 0.64, 0.7, 36, 52, 0.3),
    // On the grass by the house's sunny side, looking up past its roof: the corner of the house in the lower right
    // of the frame, the cloud's base and its edge over it, and open sky between (where the words lie). The camera
    // looks high enough that the ground is out of the frame.
    step: (view, aspect) => (aspect >= 1 ? set(view, e.set(-12, 1.3, 8), a.set(-2, 14.5, -7), 70, 0.3) : set(view, e.set(-12, 1.3, 8), a.set(-2, 20, -7), 84, 0.3)),
  };

  const away = [km.mesh, cube, tape.mesh, drops.mesh, beads.mesh, metre.mesh, bead, scale.mesh, drop, air.group];
  const world = {
    scene, camera, rig, cloud, km, house, cube, tape, drops, beads, metre, bead, scale, drop, air, views,
    HERE, FRAME, MARK, CUBE_AT, NEAR,
    // Everything put away and the cloud whole at the start of its way: what a scene does not pose stays so.
    rest() {
      world.cloudTo(0, 0);
      world.solid(1);
      cloud.visible = true;
      for (let i = 0; i < away.length; i++) away[i].visible = false;
      rig.haze(0);
      rig.cover(-500, 0, 0, 1900);
      camera.near = 5;
      camera.far = 90000;
    },
    // The cloud `along` metres down the wind, and drifting on from there with the world's time t: at a walking pace
    // at first and ever more slowly, 140 m in all. So a slide held for a long while keeps its composition, and the
    // tip of the shadow, which begins 150 m short of the house, never reaches it by drifting. `drift` 0 holds the
    // cloud where `along` puts it (the drops are laid out for the cloud standing exactly at OVER).
    cloudTo(along, t, drift = 1) {
      cloudAt(along + drift * 140 * (1 - Math.exp(-t / 40)), cloud.position);
    },
    // The air inside the cloud, for a camera that is already placed: white beyond `within` metres while the camera
    // is near HERE, clear by the time it is 300 m off; `amount` 0..1 scales it (a travel in brings it on). Where the
    // air is at its thickest nothing far off is drawn at all: the cloud's own shell and the plain would be white
    // on white, but a print that draws edges from depth (glyph) would still outline them.
    mist(amount, within) {
      const thick = amount * (1 - kit.ease((camera.position.distanceTo(HERE) - 80) / 220));
      rig.haze(thick, within);
      if (thick > 0.98) { cloud.visible = false; camera.far = 40; }
    },
    // How much of the cloud is still a solid white shape, 1..0: it thins out into grain, and at 0 it is gone and
    // casts no shadow (its water is somewhere else). Its shadow thins with it: a shadow map knows nothing of grain,
    // so it is the light's shadows that are made fainter (all of them; a scene thins the cloud from far off, where
    // the cloud's is the only shadow large enough to see).
    solid(x) {
      white.opacity = under.opacity = x;
      domes.visible = floors.visible = balls.visible = x > 0.004;
      rig.key.shadow.intensity = x > 0.004 ? x : 1;
    },
    // The cube of water, filled to a share of its height.
    water(level) {
      cube.visible = level > 0.001;
      cube.scale.y = Math.max(level, 0.001);
      cube.position.set(CUBE_AT.x, (CUBE * level) / 2, CUBE_AT.z);
    },
  };
  return world;
}
