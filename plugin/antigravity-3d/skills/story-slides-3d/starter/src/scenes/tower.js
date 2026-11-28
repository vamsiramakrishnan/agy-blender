// The tower at night: a dark sea under fog, the lighthouse on its rock, two beams turning once every nine seconds,
// made visible by haze cones. Beat 2: a small lantern far out on the water appears and blinks, and the camera turns
// to take it in. shore.js stands the same lighthouse on its beach.
import * as THREE from 'three';
import { TURN } from './title.js';

// The lighthouse, its door facing +z: rocks, a turned tower, gallery, lantern room and roof. Eight parts. The lantern
// glass glows (emissive); `glass` is returned so a scene can dim it by day. The lamp is at LAMP_Y.
export const LAMP_Y = 9.85;
export function lighthouse(kit) {
  const group = new THREE.Group();
  const stone = kit.mat('#4b4d52', 0.95), white = kit.mat('#ddd7c9', 0.8), dark = kit.mat('#23262d', 0.6);
  const glass = new THREE.MeshStandardMaterial({ color: '#2b2a25', emissive: '#ffe4a8', emissiveIntensity: 1.4, roughness: 0.3 });
  const rock = (w, h, d, x, z, turn) => { const r = kit.box(w, h, d, stone, x, h / 2 - 0.2, z); r.rotation.y = turn; return r; };
  group.add(
    rock(4.6, 1.1, 3.8, 0.3, 0.2, 0.4), rock(3.2, 0.8, 3, -1.6, 1.4, -0.3), rock(2.6, 0.7, 2.4, 1.9, -1.5, 1.1),
    kit.lathe([[1.7, 0], [1.6, 0.6], [1.15, 9], [1.3, 9.02], [1.3, 9.06], [0, 9.06]], white),
    kit.cyl(1.55, 1.5, 0.18, dark, 0, 9.12, 0),
    new THREE.Mesh(new THREE.CylinderGeometry(0.82, 0.82, 1.3, 20), glass).translateY(LAMP_Y),
    kit.cyl(0.08, 1.0, 0.8, dark, 0, 10.9, 0),
    kit.box(0.75, 1.6, 0.2, dark, 0, 1.4, 1.52),
  );
  group.children[5].castShadow = false; // the lamp's own glass must not shadow the lamp's beams
  return { group, glass };
}

// A soft round glow for a light too small for the print's cells: an additive sprite.
function glow(size) {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d'), grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, 'rgba(255,236,190,1)');
  grad.addColorStop(0.25, 'rgba(255,220,160,0.45)');
  grad.addColorStop(1, 'rgba(255,220,160,0)');
  g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
  const map = new THREE.CanvasTexture(c);
  map.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map, blending: THREE.AdditiveBlending, depthWrite: false, fog: false }));
  sprite.scale.setScalar(size);
  return sprite;
}

const DOWN = new THREE.Vector3(0, -1, 0);
const LANTERN = new THREE.Vector3(-30, 0.5, -34);

export default {
  id: 'tower',
  enterWith: 'iris',
  ambient: true,
  beats: [{ dur: 6, hold: 4 }, { dur: 6, hold: 5 }],
  create(kit) {
    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#04060a');
    scene.fog = new THREE.FogExp2('#04060a', 0.011);
    const camera = new THREE.PerspectiveCamera(36, 1, 0.5, 400);
    scene.add(kit.plane(700, 700, kit.mat('#0e1726', 0.3)), lighthouse(kit).group);
    // One key with a source in the world, the moon (low on the left, out of frame), and a fill of a few hundredths:
    // the tower's lit side and the beams are the picture, the rest is night.
    const moon = new THREE.DirectionalLight('#b9c7e2', 1.1);
    moon.position.set(-30, 14, 16);
    scene.add(moon, new THREE.HemisphereLight('#26324a', '#000000', 0.04));

    // Two beams back to back, each a spot light (what it lights) and a haze cone (the beam itself, seen in the air).
    const beams = [0, Math.PI].map((offset) => {
      const light = kit.spot(scene, [0, LAMP_Y, 0], [0, 0, 60], { intensity: 26, angle: 0.08, penumbra: 0.6, distance: 160, colour: '#fff0cf' });
      const cone = kit.haze(0.3, 4.5, 95, 0.42);
      cone.position.set(0, LAMP_Y, 0);
      scene.add(cone);
      return { offset, light, cone };
    });

    // The town across the bay: a strip of lit windows on the horizon, not fogged, so it stays as a row of points.
    const town = kit.windows(5, 64, 3);
    const strip = new THREE.Mesh(new THREE.PlaneGeometry(140, 2.6), new THREE.MeshBasicMaterial({ map: town, fog: false, color: '#ffffff' }));
    strip.position.set(-30, 1.3, -190);
    scene.add(strip);

    // The answer: a point of light far out, with a short streak of its reflection on the water below it. Small and
    // unfogged, so it reads as a light "out there" and not as a second lamp.
    const lantern = new THREE.Group();
    const bulb = new THREE.Mesh(new THREE.SphereGeometry(0.22, 10, 6), new THREE.MeshBasicMaterial({ color: '#fff1d0', fog: false }));
    const streak = new THREE.Mesh(new THREE.PlaneGeometry(0.22, 4), new THREE.MeshBasicMaterial({ color: '#c9a86a', fog: false, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
    streak.rotation.x = -Math.PI / 2;
    streak.position.set(0, -0.48, 2.2);
    lantern.add(bulb, glow(1.6), streak);
    lantern.position.copy(LANTERN);
    scene.add(lantern);

    const target = new THREE.Vector3(), from = new THREE.Vector3(), dir = new THREE.Vector3();
    return {
      scene,
      camera,
      update(s) {
        // Beat 1 drifts in on the tower; beat 2 turns and draws back to hold the tower and the lantern together.
        const turn = s.k(1, 0, 5);
        const d = THREE.MathUtils.lerp(kit.fit(camera, s.aspect, 11, 14.5) * (1.2 - 0.15 * s.k(0, 0, 6)), kit.fit(camera, s.aspect, 34, 16), turn);
        target.set(THREE.MathUtils.lerp(0, -10, turn), THREE.MathUtils.lerp(5.6, 3.5, turn), THREE.MathUtils.lerp(0, 4, turn));
        from.set(0.32 + s.pointer.x * 0.03, 0.02 - s.pointer.y * 0.02, 1).normalize();
        camera.position.copy(target).addScaledVector(from, d);
        camera.lookAt(target);

        // The beams: one turn every TURN seconds, the first pointing left and a little towards the camera at t = 0.
        const a = (s.t / TURN) * Math.PI * 2 - 1.15;
        for (const b of beams) {
          dir.set(Math.sin(a + b.offset), -0.07, Math.cos(a + b.offset)).normalize();
          b.light.target.position.set(0, LAMP_Y, 0).addScaledVector(dir, 60);
          b.light.target.updateMatrixWorld();
          b.cone.quaternion.setFromUnitVectors(DOWN, dir);
        }

        // The lantern appears early in beat 2, then blinks: a second and a half on, one and a half off.
        const shown = s.k(1, 0.6, 1.8), on = ((s.t % 3) + 3) % 3 < 1.6 ? 1 : 0.12;
        lantern.scale.setScalar(Math.max(shown, 0.001));
        bulb.material.color.setScalar(on);
        lantern.children[1].material.opacity = on;
        streak.material.opacity = on;
      },
    };
  },
};
