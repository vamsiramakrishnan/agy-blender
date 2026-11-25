// evidence.js: custom-shader metrology coordinate deck + single-draw-call InstancedMesh byte slabs.
// Aligned cleanly to containment_gate [0, 0, 0], artifact_vault [3.0, 0, -0.25], anchor_clamp [5.9, 0, 0.2].
import * as THREE from 'three';
import CustomShaderMaterial from 'three-custom-shader-material/vanilla';

const PALETTE = {
  ground: '#15181D',
  grid: '#272C33',
  gridMajor: '#343B45',
  railOff: '#1F242C',
  blue: '#6F96FF',
  amber: '#F0B429',
  red: '#FF7768',
  green: '#63D3A4',
  neutral: '#535B66',
  neutralDark: '#2E343D',
};

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const mix = (a, b, t) => a + (b - a) * t;

export function createEvidenceLayer(scene) {
  const deckUniforms = {
    uBase: { value: new THREE.Color(PALETTE.ground) },
    uGrid: { value: new THREE.Color(PALETTE.grid) },
    uGridMajor: { value: new THREE.Color(PALETTE.gridMajor) },
    uRailOff: { value: new THREE.Color(PALETTE.railOff) },
    uBlue: { value: new THREE.Color(PALETTE.blue) },
    uAmber: { value: new THREE.Color(PALETTE.amber) },
    uRed: { value: new THREE.Color(PALETTE.red) },
    uGreen: { value: new THREE.Color(PALETTE.green) },
    uFlood: { value: 0 },
    uDigest: { value: 0 },
    uRetrieve: { value: 0 },
    uAnchor: { value: 0 },
    uHandoff: { value: 0 },
  };

  const deckMaterial = new CustomShaderMaterial({
    baseMaterial: THREE.MeshStandardMaterial,
    roughness: 0.92,
    metalness: 0.08,
    uniforms: deckUniforms,
    vertexShader: /* glsl */ `
      varying vec3 vWorld;
      void main() {
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWorld = wp.xyz;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uBase;
      uniform vec3 uGrid;
      uniform vec3 uGridMajor;
      uniform vec3 uRailOff;
      uniform vec3 uBlue;
      uniform vec3 uAmber;
      uniform vec3 uRed;
      uniform vec3 uGreen;
      uniform float uFlood;
      uniform float uDigest;
      uniform float uRetrieve;
      uniform float uAnchor;
      uniform float uHandoff;
      varying vec3 vWorld;

      float gridLine(vec2 p, float stepSize, float halfWidth) {
        vec2 q = abs(fract(p / stepSize - 0.5) - 0.5) * stepSize;
        float d = min(q.x, q.y) - halfWidth;
        float w = max(fwidth(d), 0.001);
        return 1.0 - smoothstep(-w, w, d);
      }

      float segMask(vec2 p, vec2 a, vec2 b, float halfWidth) {
        vec2 pa = p - a, ba = b - a;
        float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-5), 0.0, 1.0);
        float d = length(pa - ba * h) - halfWidth;
        float w = max(fwidth(d), 0.001);
        return 1.0 - smoothstep(-w, w, d);
      }

      void main() {
        vec2 p = vWorld.xz;
        vec3 col = uBase;

        float minor = gridLine(p, 0.5, 0.006);
        float major = gridLine(p, 2.0, 0.011);
        float benchFade = 1.0 - smoothstep(10.0, 24.0, length(p - vec2(2.95, 0.0)));
        col = mix(col, uGrid, minor * 0.55 * benchFade);
        col = mix(col, uGridMajor, major * 0.8 * benchFade);

        // Main physical evidence bus: intake -> gate [0,0] -> vault [3.0,-0.25] -> anchor [5.9,0.2] -> exit
        float intakeRail = segMask(p, vec2(-3.0, 0.0), vec2(-0.5, 0.0), 0.055);
        float digestRail = segMask(p, vec2(0.55, 0.0), vec2(2.55, -0.25), 0.045);
        float retrieveRail = segMask(p, vec2(3.0, 0.15), vec2(3.0, 1.55), 0.045);
        float anchorRail = segMask(p, vec2(3.45, -0.2), vec2(5.45, 0.2), 0.045);
        float exitRail = segMask(p, vec2(6.35, 0.2), vec2(7.9, 0.2), 0.045);

        float allRails = max(max(intakeRail, digestRail), max(retrieveRail, max(anchorRail, exitRail)));
        col = mix(col, uRailOff, allRails * 0.85);

        float intakeSignal = segMask(p, vec2(-2.9, 0.0), vec2(-0.55, 0.0), 0.02);
        vec3 intakeCol = mix(uBlue, uRed, clamp(uFlood * (1.0 - uDigest * 0.4), 0.0, 1.0));
        col = mix(col, intakeCol, intakeSignal * (0.35 + 0.65 * max(uFlood, uHandoff)));

        float digestSignal = segMask(p, vec2(0.6, 0.0), vec2(2.5, -0.25), 0.02);
        col = mix(col, uBlue, digestSignal * (0.25 + 0.75 * max(uDigest, uHandoff)));

        float retrieveSignal = segMask(p, vec2(3.0, 0.2), vec2(3.0, 1.48), 0.02);
        col = mix(col, uAmber, retrieveSignal * (0.2 + 0.8 * max(uRetrieve, uHandoff)));

        float anchorSignal = segMask(p, vec2(3.5, -0.19), vec2(5.4, 0.19), 0.02);
        col = mix(col, uGreen, anchorSignal * (0.2 + 0.8 * max(uAnchor, uHandoff)));

        float exitSignal = segMask(p, vec2(6.4, 0.2), vec2(7.8, 0.2), 0.02);
        col = mix(col, uBlue, exitSignal * (0.25 + 0.75 * uHandoff));

        // Calibration pads under the three instruments
        vec2 padGate = abs(p - vec2(0.03, 0.0)) - vec2(1.02, 0.68);
        float dPadGate = abs(max(padGate.x, padGate.y));
        float rPadGate = 1.0 - smoothstep(-fwidth(dPadGate), fwidth(dPadGate), dPadGate - 0.012);
        col = mix(col, uBlue, rPadGate * 0.58);

        vec2 padVault = abs(p - vec2(3.0, -0.25)) - vec2(0.84, 0.68);
        float dPadVault = abs(max(padVault.x, padVault.y));
        float rPadVault = 1.0 - smoothstep(-fwidth(dPadVault), fwidth(dPadVault), dPadVault - 0.012);
        col = mix(col, uAmber, rPadVault * 0.58);

        vec2 padAnchor = abs(p - vec2(5.9, 0.2)) - vec2(0.82, 0.64);
        float dPadAnchor = abs(max(padAnchor.x, padAnchor.y));
        float rPadAnchor = 1.0 - smoothstep(-fwidth(dPadAnchor), fwidth(dPadAnchor), dPadAnchor - 0.012);
        col = mix(col, uGreen, rPadAnchor * 0.58);

        csm_DiffuseColor = vec4(col, 1.0);
      }
    `,
  });

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(180, 180), deckMaterial);
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  const TOTAL = 169;
  const slabGeo = new THREE.BoxGeometry(1, 1, 1);
  const slabMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.42, metalness: 0.15 });
  const slabs = new THREE.InstancedMesh(slabGeo, slabMat, TOTAL);
  slabs.castShadow = true;
  slabs.receiveShadow = true;
  slabs.frustumCulled = false;
  scene.add(slabs);

  const cBlue = new THREE.Color(PALETTE.blue);
  const cAmber = new THREE.Color(PALETTE.amber);
  const cRed = new THREE.Color(PALETTE.red);
  const cGreen = new THREE.Color(PALETTE.green);
  const cNeutral = new THREE.Color(PALETTE.neutral);
  const cNeutralDark = new THREE.Color(PALETTE.neutralDark);
  const tmpColor = new THREE.Color();

  const dummy = new THREE.Object3D();
  function setSlab(i, x, y, z, sx, sy, sz, ry, col) {
    if (sx <= 0.0005 || sy <= 0.0005 || sz <= 0.0005) {
      dummy.position.set(0, -10, 0);
      dummy.scale.set(0.0001, 0.0001, 0.0001);
      dummy.rotation.set(0, 0, 0);
    } else {
      dummy.position.set(x, y, z);
      dummy.scale.set(sx, sy, sz);
      dummy.rotation.set(0, ry, 0);
    }
    dummy.updateMatrix();
    slabs.setMatrixAt(i, dummy.matrix);
    slabs.setColorAt(i, col);
  }

  function update(state) {
    const L = state.layer;
    deckUniforms.uFlood.value = L.floodIn;
    deckUniforms.uDigest.value = L.digestOut;
    deckUniforms.uRetrieve.value = L.retrieveOut;
    deckUniforms.uAnchor.value = L.anchorShift;
    deckUniforms.uHandoff.value = L.handoffFlow;

    // A. Raw flood stack (0..95) at intake (-1.55..-2.25) -> archives cleanly behind artifact_vault (3.0, -0.82)
    const floodPresence = L.floodIn;
    const divert = L.floodDivert;
    for (let i = 0; i < 96; i++) {
      const colIdx = i % 4;
      const rowIdx = Math.floor(i / 4);
      const appear = clamp01(floodPresence * 1.4 - (rowIdx / 24) * 0.45);
      const fx = -1.55 - colIdx * 0.22;
      const fy = 0.03 + rowIdx * 0.056;
      const fz = -0.34 + (colIdx % 2) * 0.23 + (colIdx >= 2 ? 0.24 : 0) - 0.1;
      const vx = 2.52 + (i % 6) * 0.15;
      const vy = 0.025 + Math.floor(i / 6) * 0.036;
      const vz = -0.84 - (i % 2) * 0.11;

      const x = mix(fx, vx, divert);
      const y = mix(fy, vy, divert) + Math.sin(divert * Math.PI) * (0.35 + (i % 5) * 0.06);
      const z = mix(fz, vz, divert);
      const scale = appear * (1 - L.handoffFlow * 0.55);
      tmpColor.copy(cRed).lerp(cNeutralDark, divert);
      setSlab(i, x, y, z, 0.19 * scale, 0.035 * scale, 0.32 * scale, 0, tmpColor);
    }

    // B. Truncation comparison stack (96..115) beside intake
    const truncVis = L.floodIn * (1 - L.floodDivert);
    for (let k = 0; k < 20; k++) {
      const i = 96 + k;
      const keep = k < 5 || k >= 15;
      const y = 0.03 + k * 0.056;
      const x = -1.35;
      const z = 0.72;
      const s = keep ? truncVis : truncVis * (1 - clamp01((L.floodIn - 0.35) / 0.45));
      const dropOffset = keep ? 0 : (1 - s) * 0.38;
      setSlab(i, x, y, z + dropOffset, 0.17 * s, 0.034 * s, 0.25 * s, 0, keep ? cNeutral : cRed);
    }

    // C. Quiet needle slab (116, amber, line 14,238)
    {
      const inX = -1.62, inY = 0.03 + 14 * 0.056, inZ = 0.05;
      const outX = 1.38, outY = 0.295, outZ = -0.10;
      const introX = 1.22, introY = 0.21, introZ = -0.05;
      const toFlood = L.floodIn * (1 - L.digestOut);
      let nx = mix(introX, inX, toFlood);
      let ny = mix(introY, inY, toFlood);
      let nz = mix(introZ, inZ, toFlood);
      nx = mix(nx, outX, L.digestOut);
      ny = mix(ny, outY, L.digestOut);
      nz = mix(nz, outZ, L.digestOut);
      setSlab(116, nx, ny, nz, 0.34, 0.048, 0.26, -0.08, cAmber);
    }

    // D. Compact 531-token digest stack (117..122, blue) beside gate exit chute
    for (let k = 0; k < 6; k++) {
      const i = 117 + k;
      const present = mix(0.65, 1.0, L.digestOut) * (1 - L.floodIn * (1 - L.digestOut) * 0.7);
      const x = mix(1.22, 1.38, L.digestOut);
      const y = 0.04 + k * 0.04;
      const z = mix(-0.05, -0.10, L.digestOut);
      setSlab(i, x, y, z, 0.35 * present, 0.028 * present, 0.27 * present, -0.08, cBlue);
    }

    // E. Targeted retrieval slice (123..134) sliding out from artifact_vault front along amber rail
    for (let k = 0; k < 12; k++) {
      const i = 123 + k;
      const slide = clamp01(L.retrieveOut * 1.25 - (k / 12) * 0.22);
      const isHandle = k === 11;
      const x = 3.0 + ((k % 2) - 0.5) * 0.035;
      const y = 0.045 + k * 0.034;
      const z = mix(0.32, 1.18, slide);
      const vis = L.retrieveOut > 0.01 ? slide : 0;
      setSlab(i, x, y, z, (isHandle ? 0.34 : 0.28) * vis, 0.025 * vis, 0.23 * vis, 0, isHandle ? cAmber : cBlue);
    }

    // F. Source-code line slabs (135..154) positioned cleanly inside/in front of anchor_clamp's cantilevered jaws
    const shift = L.anchorShift;
    for (let k = 0; k < 20; k++) {
      const i = 135 + k;
      const isTargetSpan = k >= 6 && k <= 9;
      const baseLineY = 1.06 - k * 0.046;
      const shiftedLineY = baseLineY - shift * 0.23;
      const active = clamp01(shift * 2.2 + 0.35);
      const inBounds = shiftedLineY > 0.03 ? 1 : 0;
      const col = isTargetSpan ? cGreen : cNeutral;
      const width = isTargetSpan ? 0.32 : 0.24;
      setSlab(i, 5.95, Math.max(0.03, shiftedLineY), 0.72, width * active * inBounds, 0.03 * inBounds, 0.075 * inBounds, -0.15, col);
    }

    // G. Stale unanchored line pointer (155, red) stuck at old line height
    {
      const s = clamp01(shift * 2.0);
      setSlab(155, 5.68, 1.06 - 7.5 * 0.046, 0.75, 0.13 * s, 0.15 * s, 0.05 * s, -0.15, cRed);
    }

    // H. Relocating content-anchor caliper block (156, amber) tracking the moved green span
    {
      const s = clamp01(shift * 2.0 + 0.25);
      const trackedY = (1.06 - 7.5 * 0.046) - shift * 0.23;
      setSlab(156, 6.22, trackedY, 0.75, 0.13 * s, 0.18 * s, 0.055 * s, -0.15, cAmber);
    }

    // I. Multi-harness handoff packets (157..168) gliding along the full rail
    for (let k = 0; k < 12; k++) {
      const i = 157 + k;
      const vis = L.handoffFlow;
      const phase = ((k / 12) + L.handoffFlow * 0.45) % 1.0;
      const x = mix(-2.1, 7.6, phase);
      const z = phase < 0.5
        ? mix(0.0, -0.25, phase * 2)
        : mix(-0.25, 0.2, (phase - 0.5) * 2);
      const isAmber = k % 3 === 0;
      setSlab(i, x, 0.065, z, 0.2 * vis, 0.048 * vis, 0.14 * vis, 0, isAmber ? cAmber : cBlue);
    }

    slabs.instanceMatrix.needsUpdate = true;
    if (slabs.instanceColor) slabs.instanceColor.needsUpdate = true;
  }

  return { update, ground, slabs };
}
