// evidence.js: scroll-driven procedural evidence layer, custom shaders, and local-font 3D labels.
// Every transform, colour, and uniform is a pure deterministic function of story time T.
import * as THREE from 'three';
import CustomShaderMaterial from 'three-custom-shader-material/vanilla';
import { Text } from 'troika-three-text';
import { ramp } from './shots.js';

const COL = {
  bg: new THREE.Color('#0D0F12'),
  neutral: new THREE.Color('#353B45'),
  slate: new THREE.Color('#4B5361'),
  blue: new THREE.Color('#6F96FF'),
  blueDark: new THREE.Color('#263A68'),
  amber: new THREE.Color('#F0B429'),
  red: new THREE.Color('#FF7768'),
  redDark: new THREE.Color('#7A2C25'),
  green: new THREE.Color('#63D3A4'),
};

// Architectural ruled bench plane with custom shader (zero glow, crisp fwidth grid + evidence track).
export function createBenchGround() {
  const uniforms = {
    uProgress: { value: 0 },
  };
  const mat = new CustomShaderMaterial({
    baseMaterial: THREE.MeshStandardMaterial,
    roughness: 0.96,
    metalness: 0.04,
    envMapIntensity: 0.2,
    uniforms,
    vertexShader: /* glsl */ `
      varying vec3 vWPos;
      void main() {
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWPos = wp.xyz;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uProgress;
      varying vec3 vWPos;

      float gridLine(vec2 coord, float spacing, float widthPx) {
        vec2 p = coord / spacing;
        vec2 g = abs(fract(p - 0.5) - 0.5) / max(fwidth(p), vec2(0.0001));
        float line = min(g.x, g.y);
        return 1.0 - smoothstep(widthPx * 0.5, widthPx * 1.15, line);
      }

      void main() {
        vec3 base = vec3(0.055, 0.064, 0.078);
        vec3 majorCol = vec3(0.145, 0.168, 0.200);
        vec3 minorCol = vec3(0.088, 0.100, 0.120);

        // Fade architectural grid away from the working bench so the horizon stays calm.
        float distBench = length(vec2((vWPos.x - 3.0) * 0.11, (vWPos.z - 0.3) * 0.16));
        float benchMask = 1.0 - smoothstep(0.65, 1.45, distBench);

        float major = gridLine(vWPos.xz, 1.0, 1.15) * benchMask;
        float minor = gridLine(vWPos.xz, 0.25, 0.8) * benchMask * 0.55;

        vec3 col = mix(base, minorCol, minor);
        col = mix(col, majorCol, major);

        // Calibrated evidence datum rail from x=-2.8 to x=6.8 along z=0
        float trackHalfW = 0.032;
        float onX = step(-2.8, vWPos.x) * step(vWPos.x, 6.8);
        float rail = (1.0 - smoothstep(trackHalfW * 0.6, trackHalfW, abs(vWPos.z))) * onX;
        float progressX = mix(-2.8, 6.8, clamp(uProgress, 0.0, 1.0));
        float reached = step(vWPos.x, progressX);
        vec3 railCol = mix(vec3(0.20, 0.23, 0.28), vec3(0.435, 0.588, 1.0), reached);
        col = mix(col, railCol, rail * 0.9);

        // Station datum rings (square architectural footprints around x=0, x=3.2, x=6.0)
        vec2 s0 = abs(vWPos.xz - vec2(0.0, 0.0)) - vec2(1.05, 0.78);
        vec2 s1 = abs(vWPos.xz - vec2(3.2, -0.5)) - vec2(1.0, 0.85);
        vec2 s2 = abs(vWPos.xz - vec2(6.0, 0.4)) - vec2(0.95, 0.82);
        float dBox = min(
          abs(max(s0.x, s0.y)),
          min(abs(max(s1.x, s1.y)), abs(max(s2.x, s2.y)))
        );
        float frameLine = (1.0 - smoothstep(0.008, 0.022, dBox)) * benchMask;
        col = mix(col, vec3(0.28, 0.32, 0.38), frameLine * 0.8);

        csm_DiffuseColor = vec4(col, 1.0);
      }
    `,
  });

  const geo = new THREE.PlaneGeometry(200, 200);
  geo.rotateX(-Math.PI / 2);
  const mesh = new THREE.Mesh(geo, mat);
  mesh.receiveShadow = true;
  mesh.name = 'evidence-bench-ground';

  return {
    mesh,
    setProgress(p) {
      uniforms.uProgress.value = p;
    },
  };
}

// One InstancedMesh for all semantic evidence slabs + local-font Troika 3D labels.
export function createEvidenceLayer(scene, onChange = () => {}) {
  const MAX_SLABS = 240;
  const slabGeo = new THREE.BoxGeometry(1, 1, 1);

  // Preserve InstancedMesh instanceColor explicitly in CustomShaderMaterial while adding crisp edge-bevel shading.
  const slabVertex = /* glsl */ `
    varying vec3 vLocalPos;
    varying vec3 vInstCol;
    void main() {
      vLocalPos = position;
      #ifdef USE_INSTANCING_COLOR
        vInstCol = instanceColor;
      #else
        vInstCol = vec3(1.0);
      #endif
    }
  `;
  const slabFragment = /* glsl */ `
    varying vec3 vLocalPos;
    varying vec3 vInstCol;
    void main() {
      vec3 edge = abs(vLocalPos);
      float rim = max(max(edge.x, edge.y), edge.z);
      float edgeDarken = 1.0 - smoothstep(0.42, 0.5, rim) * 0.22;
      float stripe = 0.95 + 0.05 * step(0.5, fract(vLocalPos.x * 6.0));
      csm_DiffuseColor = vec4(vInstCol * edgeDarken * stripe, 1.0);
    }
  `;
  const slabMat = new CustomShaderMaterial({
    baseMaterial: THREE.MeshStandardMaterial,
    roughness: 0.42,
    metalness: 0.18,
    vertexShader: slabVertex,
    fragmentShader: slabFragment,
  });

  const slabs = new THREE.InstancedMesh(slabGeo, slabMat, MAX_SLABS);
  slabs.castShadow = true;
  slabs.receiveShadow = true;
  slabs.frustumCulled = false;
  slabs.name = 'evidence-slabs';
  // Initialize instanceColor buffer before first compile so USE_INSTANCING_COLOR is active
  for (let i = 0; i < MAX_SLABS; i++) slabs.setColorAt(i, COL.neutral);
  scene.add(slabs);

  const dummy = new THREE.Object3D();
  const cTmp = new THREE.Color();

  // Helper to place one slab instance
  let cursor = 0;
  function putSlab(x, y, z, sx, sy, sz, color, ry = 0, rz = 0) {
    if (cursor >= MAX_SLABS) return;
    dummy.position.set(x, y, z);
    dummy.rotation.set(0, ry, rz);
    dummy.scale.set(sx, sy, sz);
    dummy.updateMatrix();
    slabs.setMatrixAt(cursor, dummy.matrix);
    slabs.setColorAt(cursor, color);
    cursor++;
  }

  // Troika 3D labels using bundled JetBrains Mono WOFF (zero network fetches)
  const fontUrl = new URL('fonts/jetbrains-mono-700.woff', document.baseURI).href;
  const labels = [];
  function addLabel(text, pos, { size = 0.11, color = '#ECEAE3', rotX = -Math.PI / 2, rotY = 0, range = [0, 7] } = {}) {
    const t = new Text();
    t.text = text;
    t.font = fontUrl;
    t.fontSize = size;
    t.letterSpacing = 0.06;
    t.anchorX = 'center';
    t.anchorY = 'middle';
    t.color = color;
    t.position.set(pos[0], pos[1], pos[2]);
    t.rotation.set(rotX, rotY, 0);
    t.material.depthWrite = false;
    t.material.transparent = true;
    t.sync(() => onChange());
    scene.add(t);
    labels.push({ mesh: t, range });
    return t;
  }

  // Bench floor & station instrument markings (all verified against straitjacket receipts)
  addLabel('302,628 TOK RAW FLOOD', [-1.85, 0.015, 1.05], { size: 0.11, color: '#FF7768', range: [0.0, 2.5] });
  addLabel('L14,238 QUIET NEEDLE', [-1.65, 1.74, 0.25], { size: 0.095, color: '#F0B429', rotX: 0, rotY: 0.25, range: [0.85, 2.35] });
  addLabel('531 TOK · 570× · run:8956c843e556', [1.45, 0.015, 0.85], { size: 0.10, color: '#6F96FF', range: [1.85, 3.65] });
  addLabel('LOCAL VAULT · 64 MiB/STREAM', [3.2, 0.015, 0.88], { size: 0.10, color: '#8D939A', range: [2.75, 4.45] });
  addLabel('ctx get --lines 1280:1300', [3.35, 0.48, 1.25], { size: 0.088, color: '#6F96FF', rotX: 0, rotY: -0.22, range: [2.95, 4.35] });
  addLabel('1,452 RELOCATED · 466 REFUSED · 0 WRONG', [6.0, 0.015, 1.48], { size: 0.095, color: '#63D3A4', range: [3.85, 5.5] });
  addLabel('BIRTH GATE (PRE)    /    OUTPUT GATE (POST)', [4.55, 0.015, 2.55], { size: 0.095, color: '#F0B429', range: [4.85, 6.45] });
  addLabel('STRAITJACKET · EVIDENCE BENCH', [3.0, 0.015, 2.9], { size: 0.13, color: '#8D939A', range: [5.85, 7.1] });

  // Host lane semantic config from straitjacket/docs/HOST-CAPABILITIES.md
  // [birthGateColor, outputGateColor]
  const HOST_LANES = [
    [COL.slate, COL.green],   // Claude Code: default auto (no rewrite), replaces output
    [COL.slate, COL.blue],    // Codex CLI: rewrites withheld, contract-tested post
    [COL.blue, COL.red],      // Antigravity: pre-tool rewrite / deny, NO post-tool output replace
    [COL.blue, COL.blue],     // Hermes: pre + post
    [COL.blue, COL.blue],     // Oh My Pi: pre + post
    [COL.blue, COL.blue],     // OpenCode: pre + post
    [COL.amber, COL.blue],    // DeepSeek Harness: deny+retry pre, replace post
  ];

  function update(state) {
    const T = state.T ?? 0;
    cursor = 0;

    // ------------------------------------------------------------------
    // 1. INTAKE FLOOD (-X of capture_gate at x=0): 6 turns x 16 slabs
    // ------------------------------------------------------------------
    // During intro (0..1) and flood (1..2) the red column stands tall & resident;
    // during digest (2..3) the bulk diverts cleanly downward into local storage.
    const floodRise = ramp(T, 0.05, 1.35);
    const digestDivert = ramp(T, 2.0, 2.85);
    const turns = 6;
    const rows = 16;
    for (let turn = 0; turn < turns; turn++) {
      const colX = -2.55 + turn * 0.28;
      // Turns 1..6 stack up as T enters chapter 2 (flood)
      const turnVis = ramp(T, 0.1 + turn * 0.12, 0.55 + turn * 0.16);
      const activeScale = Math.max(0.18, turnVis);
      for (let r = 0; r < rows; r++) {
        const isNeedle = turn === 3 && r === 11; // ~71% depth -> line 14,238
        const baseY = 0.08 + r * 0.095 * (0.45 + 0.55 * activeScale);
        if (isNeedle) {
          // The quiet needle survives through the gate to the output slot during digest!
          const nx = THREE.MathUtils.lerp(colX, 1.15, digestDivert);
          const ny = THREE.MathUtils.lerp(baseY, 0.86, digestDivert);
          const nz = THREE.MathUtils.lerp(0.0, 0.0, digestDivert);
          putSlab(nx, ny, nz, 0.24, 0.068, 0.44, COL.amber);
        } else {
          // Red bulk diverts downward into the containment manifold during digest
          const yDrop = digestDivert * (baseY + 0.25);
          const curY = Math.max(0.025, baseY - yDrop);
          const hScale = 1.0 - digestDivert * 0.86;
          if (hScale > 0.05) {
            const c = r % 3 === 0 ? COL.red : COL.redDark;
            putSlab(colX, curY, (turn % 2 === 0 ? -0.06 : 0.06), 0.21, 0.056 * hScale, 0.36, c);
          }
        }
      }
    }

    // Truncation cut planes during chapter 2 (flood): shows head+tail slicing out the middle (dropping L14,238)
    const truncVis = ramp(T, 1.15, 1.55) * (1.0 - ramp(T, 1.95, 2.25));
    if (truncVis > 0.01) {
      putSlab(-1.85, 0.42, 0.0, 1.75 * truncVis, 0.022, 0.62, COL.red);
      putSlab(-1.85, 1.28, 0.0, 1.75 * truncVis, 0.022, 0.62, COL.red);
    }

    // ------------------------------------------------------------------
    // 2. BOUNDED DIGEST BAR (+X of capture_gate -> artifact_vault)
    // ------------------------------------------------------------------
    const digestOut = 0.35 + 0.65 * ramp(T, 1.85, 2.65);
    // Compact 531-token blue digest assembly emerging cleanly on the right of capture_gate
    for (let k = 0; k < 5; k++) {
      const bx = 0.95 + k * 0.14 * digestOut;
      const by = 0.64 + (k % 2) * 0.085;
      putSlab(bx, by, 0.0, 0.11, 0.055, 0.34, k === 2 ? COL.amber : COL.blue);
    }
    // Calibrated conduit link from capture_gate to artifact_vault (x=1.65..2.5)
    for (let k = 0; k < 4; k++) {
      const frac = k / 3;
      const cx = THREE.MathUtils.lerp(1.65, 2.52, frac);
      const cz = THREE.MathUtils.lerp(0.0, -0.35, frac);
      const active = ramp(T, 2.1 + frac * 0.35, 2.55 + frac * 0.35);
      cTmp.copy(COL.neutral).lerp(COL.blue, active);
      putSlab(cx, 0.08, cz, 0.18, 0.045, 0.14, cTmp, -0.2);
    }

    // ------------------------------------------------------------------
    // 3. ARTIFACT VAULT EXTRACTION SLICE (at [3.2, 0, -0.5], turn -0.22)
    // ------------------------------------------------------------------
    const retrievePull = ramp(T, 3.05, 3.65) * (1.0 - 0.55 * ramp(T, 4.15, 4.65));
    const vx = 3.2;
    const vz = -0.5;
    const vYaw = -0.22;
    const cosV = Math.cos(vYaw);
    const sinV = Math.sin(vYaw);
    // 6 neutral archival cartridges parked inside/beside vault + 1 blue bounded slice pulling forward along local +Z
    for (let slot = 0; slot < 6; slot++) {
      const lx = -0.5 + slot * 0.2;
      const isSelected = slot === 3;
      const lz = isSelected ? 0.55 + retrievePull * 0.95 : 0.42;
      const wx = vx + lx * cosV + lz * sinV;
      const wz = vz - lx * sinV + lz * cosV;
      const wy = isSelected ? 0.58 : 0.46;
      putSlab(wx, wy, wz, 0.12, isSelected ? 0.14 : 0.42, isSelected ? 0.52 : 0.22, isSelected ? COL.blue : COL.slate, vYaw);
      if (isSelected) {
        // Amber address handle pin riding on top of the retrieved bounded slice
        putSlab(wx, wy + 0.105, wz, 0.13, 0.04, 0.16, COL.amber, vYaw);
      }
    }

    // ------------------------------------------------------------------
    // 4. ANCHOR CLAMP SOURCE STACK (at [6.0, 0, 0.4], turn -0.35)
    // ------------------------------------------------------------------
    const ax = 6.0;
    const az = 0.4;
    const aYaw = -0.35;
    const cosA = Math.cos(aYaw);
    const sinA = Math.sin(aYaw);
    const editShift = ramp(T, 4.1, 4.55);   // lines shift downward by 3 rows when imports are inserted above
    const rewriteRefuse = ramp(T, 4.68, 4.92); // rewritten span triggers explicit amber refusal

    // 10 stacked code-line plates positioned just in front/beside anchor_clamp
    for (let row = 0; row < 10; row++) {
      const isTarget = row >= 4 && row <= 6;
      const baseRowY = 1.24 - row * 0.105;
      const shiftedY = baseRowY - editShift * 0.26;
      const clampedY = Math.max(0.14, shiftedY);
      const lx = -0.05;
      const lz = 0.62;
      const wx = ax + lx * cosA + lz * sinA;
      const wz = az - lx * sinA + lz * cosA;
      let col = COL.slate;
      if (isTarget) {
        if (rewriteRefuse > 0.5) col = COL.amber;
        else col = editShift > 0.35 ? COL.green : COL.blue;
      }
      putSlab(wx, clampedY, wz, isTarget ? 0.68 : 0.56, 0.068, 0.16, col, aYaw);
    }
    // 2 newly inserted lines sliding in from above during editShift
    if (editShift > 0.02) {
      for (let ins = 0; ins < 2; ins++) {
        const iy = THREE.MathUtils.lerp(1.75, 1.34 - ins * 0.105, editShift);
        const wx = ax - 0.05 * cosA + 0.62 * sinA;
        const wz = az + 0.05 * sinA + 0.62 * cosA;
        putSlab(wx, iy, wz, 0.56 * editShift, 0.064, 0.16, COL.blueDark, aYaw);
      }
    }
    // Unanchored bare line pointer (RED) stays stuck at original height while the code shifted away!
    const staleLx = 0.42;
    const staleLz = 0.62;
    const swx = ax + staleLx * cosA + staleLz * sinA;
    const swz = az - staleLx * sinA + staleLz * cosA;
    putSlab(swx, 0.72, swz, 0.16, 0.26, 0.19, editShift > 0.2 ? COL.red : COL.neutral, aYaw);

    // Anchored lock jaw indicator (GREEN when verified/relocated, AMBER when refused)
    const lockLx = -0.46;
    const lockY = 0.72 - editShift * 0.26;
    const lwx = ax + lockLx * cosA + staleLz * sinA;
    const lwz = az - lockLx * sinA + staleLz * cosA;
    const lockCol = rewriteRefuse > 0.5 ? COL.amber : COL.green;
    putSlab(lwx, lockY, lwz, 0.14, 0.28, 0.22, lockCol, aYaw);

    // ------------------------------------------------------------------
    // 5. SEVEN HOST LANES (in front of right bench: x=3.0..6.1, z=1.55..2.35)
    // ------------------------------------------------------------------
    const lanesRise = ramp(T, 4.95, 5.45);
    const lanesKeep = 0.22 + 0.78 * lanesRise;
    for (let h = 0; h < HOST_LANES.length; h++) {
      const [birthCol, outputCol] = HOST_LANES[h];
      const lx = 3.05 + h * 0.5;
      const laneH = 0.045 * lanesKeep;
      // Neutral lane rail
      putSlab(lx, laneH * 0.5, 1.72, 0.34, laneH, 1.25, COL.neutral);
      // Birth gate marker (PreToolUse)
      const gH = 0.24 * lanesKeep;
      putSlab(lx, laneH + gH * 0.5, 1.38, 0.26, gH, 0.22, birthCol);
      // Output gate marker (PostToolUse)
      putSlab(lx, laneH + gH * 0.5, 2.02, 0.26, gH, 0.22, outputCol);
    }

    // Hide any unused instances cleanly below the floor
    dummy.scale.set(0, 0, 0);
    dummy.position.set(0, -10, 0);
    dummy.updateMatrix();
    for (let i = cursor; i < MAX_SLABS; i++) {
      slabs.setMatrixAt(i, dummy.matrix);
    }
    slabs.instanceMatrix.needsUpdate = true;
    if (slabs.instanceColor) slabs.instanceColor.needsUpdate = true;

    // Update 3D Troika label opacities deterministically from T
    for (const item of labels) {
      const [a, b] = item.range;
      const fadeIn = ramp(T, a - 0.15, a + 0.25);
      const fadeOut = 1.0 - ramp(T, b - 0.25, b + 0.2);
      const op = fadeIn * fadeOut;
      item.mesh.visible = op > 0.02;
      item.mesh.fillOpacity = op;
    }
  }

  return {
    update,
    dispose() {
      slabGeo.dispose();
      slabMat.dispose();
      for (const item of labels) item.mesh.dispose();
    },
  };
}
