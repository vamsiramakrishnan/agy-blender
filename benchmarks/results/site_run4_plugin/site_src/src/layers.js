// layers.js: custom GLSL machined floor shader + four scroll-driven 3D mechanism layers + troika 3D labels.
// Every visual element is a pure function of story time T (zero per-frame easing), strictly using
// Straitjacket's Addressable Evidence semantic palette (#6F96FF blue, #F0B429 amber, #FF7768 red, #63D3A4 green).
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import CustomShaderMaterial from 'three-custom-shader-material/vanilla';
import { Text } from 'troika-three-text';
import { ramp } from './shots.js';

const PALETTE = {
  bg: '#0D0F12',
  surface: '#15181D',
  surface2: '#1C2026',
  grid: '#272C33',
  neutral: '#8D939A',
  ink: '#ECEAE3',
  blue: '#6F96FF',
  amber: '#F0B429',
  red: '#FF7768',
  green: '#63D3A4',
};

const FONT_URL = new URL('fonts/jetbrains-mono.woff', document.baseURI).href;

// Exact semantic colour material for thin accent marks that must match the panel's hex under NeutralToneMapping.
const exactMat = (hex) => new THREE.MeshBasicMaterial({ color: hex, toneMapped: false });
const litMat = (hex, roughness = 0.55, metalness = 0.15) =>
  new THREE.MeshStandardMaterial({ color: hex, roughness, metalness });

// Machined precision floor & recessed instrument rail using CustomShaderMaterial (keeps lights, shadows & N8AO).
// Obeys all 3 GLSL rules: THREE.Color uniforms, switched (never static-blended) states, fwidth() anti-aliased edges.
export function createMachinedFloorMaterial() {
  const uniforms = {
    uBase: { value: new THREE.Color('#181C22') },
    uBench: { value: new THREE.Color('#1E232B') },
    uGrid: { value: new THREE.Color('#2E3540') },
    uBlue: { value: new THREE.Color(PALETTE.blue) },
    uAmber: { value: new THREE.Color(PALETTE.amber) },
    uProgress: { value: 0 },
  };
  const mat = new CustomShaderMaterial({
    baseMaterial: THREE.MeshStandardMaterial,
    vertexShader: /* glsl */ `
      varying vec3 vWorldPos;
      void main() {
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWorldPos = wp.xyz;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uBase;
      uniform vec3 uBench;
      uniform vec3 uGrid;
      uniform vec3 uBlue;
      uniform vec3 uAmber;
      uniform float uProgress;
      varying vec3 vWorldPos;

      float gridLine(vec2 coord, float spacing, float width) {
        vec2 g = abs(fract(coord / spacing - 0.5) - 0.5) / (fwidth(coord / spacing) + 1e-5);
        float line = min(g.x, g.y);
        return 1.0 - smoothstep(width - 0.5, width + 0.5, line);
      }

      void main() {
        vec2 p = vWorldPos.xz;
        // 64-unit architectural drafting grid (0.5 m minor, 2.0 m major)
        float minor = gridLine(p, 0.5, 0.85) * 0.35;
        float major = gridLine(p, 2.0, 1.1) * 0.72;

        // Recessed central instrument bench strip connecting all three stations (x in [-2.35, 6.85], z in [-1.05, 1.05])
        float bx = max(p.x - 6.85, -2.35 - p.x);
        float bz = abs(p.y) - 1.05;
        float benchDist = max(bx, bz);
        float wb = fwidth(benchDist) + 1e-4;
        float inBench = 1.0 - smoothstep(-wb, wb, benchDist);
        float benchBorder = 1.0 - smoothstep(0.0, 0.025 + wb, abs(benchDist));

        // Calibrated data bus strip along front edge of the bench (z = +0.92), advancing with scroll progress
        float railZ = abs(p.y - 0.92) - 0.018;
        float railX = max(p.x - 6.72, -2.22 - p.x);
        float railDist = max(railX, railZ);
        float wr = fwidth(railDist) + 1e-4;
        float inRail = 1.0 - smoothstep(-wr, wr, railDist);
        float headX = mix(-2.22, 6.72, clamp(uProgress, 0.0, 1.0));
        float behindHead = 1.0 - smoothstep(-0.04, 0.04, p.x - headX);
        float atHead = 1.0 - smoothstep(0.04, 0.09, abs(p.x - headX));

        // Distance fade so grid dissolves cleanly into ground well before fog
        float fade = 1.0 - smoothstep(5.5, 16.0, length(p - vec2(2.6, 0.0)));

        vec3 col = mix(uBase, uBench, inBench);
        col = mix(col, uGrid, max(minor, major) * fade);
        col = mix(col, uGrid, benchBorder * 0.9);
        // Active scroll bus switches sharply between Blue (traversed) and Amber (active cursor)
        vec3 railCol = mix(uGrid, uBlue, behindHead);
        railCol = mix(railCol, uAmber, atHead);
        col = mix(col, railCol, inRail);

        csm_DiffuseColor = vec4(col, 1.0);
      }
    `,
    uniforms,
    roughness: 0.92,
    metalness: 0.06,
  });
  return { material: mat, uniforms };
}

function makeLabel(text, { size = 0.085, color = PALETTE.ink, position = [0, 0, 0], rotation = [0, 0, 0] } = {}) {
  const t = new Text();
  t.text = text;
  t.font = FONT_URL;
  t.fontSize = size;
  t.anchorX = 'center';
  t.anchorY = 'bottom';
  t.color = color;
  t.letterSpacing = 0.04;
  t.position.set(...position);
  t.rotation.set(...rotation);
  return t;
}

export function installLayers(stage, at, floorUniforms) {
  // ---------------------------------------------------------------------------
  // LAYER 1: token_stream — Exact 6,053 -> 11 digest slabs + 1 proud Amber needle
  // (1 slab = 50 tokens: 302,628 / 50 = 6,053 raw flood -> 531 / 50 = 11 digest!)
  // One InstancedMesh draw call (+ shadow map).
  // ---------------------------------------------------------------------------
  const BEFORE = 302628;
  const AFTER = 531;
  const PER = 50;
  const N = Math.round(BEFORE / PER); // 6,053
  const KEEP = Math.round(AFTER / PER); // 11
  const NEEDLE_IDX = Math.round((14238 / 20001) * N); // ~4,308

  const COLS = 22;
  const DEEP = 14;
  const ROWS = Math.ceil(N / COLS);
  const LEVELS = Math.ceil(ROWS / DEEP);

  const slabGeo = new THREE.BoxGeometry(0.056, 0.018, 0.042);
  const slabMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.52, metalness: 0.12 });
  const floodMesh = new THREE.InstancedMesh(slabGeo, slabMat, N);
  floodMesh.castShadow = true;
  floodMesh.receiveShadow = true;

  const cRed = new THREE.Color(PALETTE.red);
  const cBlue = new THREE.Color(PALETTE.blue);
  const cAmber = new THREE.Color(PALETTE.amber);
  const cNeutral = new THREE.Color(PALETTE.neutral);
  for (let i = 0; i < N; i++) {
    const isDigest = i < KEEP;
    const isNeedle = i === NEEDLE_IDX;
    floodMesh.setColorAt(i, isNeedle ? cAmber : isDigest ? cBlue : cRed);
  }
  floodMesh.instanceColor.needsUpdate = true;

  // Proud Amber quiet-needle beacon slab & archive shuttle packet travelling to Station 2
  const streamGroup = new THREE.Group();
  streamGroup.add(floodMesh);

  const needleBeacon = new THREE.Mesh(
    new RoundedBoxGeometry(0.20, 0.075, 0.16, 1, 0.006),
    exactMat(PALETTE.amber),
  );
  needleBeacon.castShadow = true;
  streamGroup.add(needleBeacon);

  // Machined steel exit catch-tray outside the press's blue exit slot (x in [0.95, 1.56])
  const exitTray = new THREE.Mesh(
    new RoundedBoxGeometry(0.62, 0.24, 0.54, 1, 0.006),
    litMat(PALETTE.surface2, 0.62, 0.35),
  );
  exitTray.position.set(1.26, 0.12, 0.0);
  exitTray.castShadow = exitTray.receiveShadow = true;
  streamGroup.add(exitTray);

  const archiveShuttle = new THREE.Mesh(
    new RoundedBoxGeometry(0.32, 0.12, 0.24, 1, 0.008),
    litMat(PALETTE.neutral, 0.55, 0.25),
  );
  archiveShuttle.castShadow = true;
  streamGroup.add(archiveShuttle);

  const m = new THREE.Matrix4();
  const pos = new THREE.Vector3();
  const quat = new THREE.Quaternion();
  const scl = new THREE.Vector3();

  // Intake bed spans x in [-2.12, -0.92], z in [-0.39, +0.39], y up from 0.28 (real press roller top y = 0.272)
  const IN_X0 = -2.10;
  const IN_DX = 0.055;
  const IN_Z0 = -0.36;
  const IN_DZ = 0.055;
  const IN_DY = 0.022;
  const BED_Y = 0.28;
  const THROAT_X = -0.08;
  const EXIT_X0 = 1.04;

  function updateTokenStream({ T }) {
    // Keep machined floor progress bus synced to story time
    floorUniforms.uProgress.value = T / 6;

    // Compression runs through chapter "flood" (local 0.05 -> 0.90), holding compressed afterward
    const tPressStart = at('flood', 0.05);
    const tPressEnd = at('flood', 0.90);

    for (let i = 0; i < N; i++) {
      const col = i % COLS;
      const row = Math.floor(i / COLS);
      const lane = row % DEEP;
      const level = Math.floor(row / DEEP);

      // Start already piled cleanly at intro so opening frame immediately shows the raw 302k-token flood!
      const startX = IN_X0 + col * IN_DX;
      const startY = BED_Y + 0.01 + level * IN_DY;
      const startZ = IN_Z0 + lane * IN_DZ;

      // Stagger by column (nearest the press enters first) and level so slabs never pass through each other
      const delay = ((COLS - 1 - col) / COLS) * 0.48 + (level / LEVELS) * 0.22;
      const moveStart = tPressStart + delay * (tPressEnd - tPressStart);
      const move = ramp(T, moveStart, moveStart + 0.28);

      if (i < KEEP) {
        // The 11 surviving digest slabs emerge neatly onto the exit tray in a crisp bounded stack
        const outX = EXIT_X0 + (i % 4) * 0.13;
        const outY = BED_Y + 0.02 + Math.floor(i / 4) * 0.052;
        const outZ = -0.13 + (i % 3) * 0.13;
        const fx = THREE.MathUtils.lerp(startX, outX, move);
        const fy = THREE.MathUtils.lerp(startY, outY, move);
        const fz = THREE.MathUtils.lerp(startZ, outZ, move);
        const sVal = THREE.MathUtils.lerp(1.0, 1.65, move);
        m.compose(pos.set(fx, fy, fz), quat, scl.set(sVal, sVal * 1.25, sVal));
      } else if (i === NEEDLE_IDX) {
        // Hide the tiny raw instance once the proud amber needle beacon takes over on exit
        const x = THREE.MathUtils.lerp(startX, THROAT_X, move);
        const sc = 1 - move;
        m.compose(pos.set(x, startY, startZ), quat, scl.setScalar(sc));
      } else {
        // Raw flood slabs slide smoothly into the open press throat and compress to zero inside it
        const x = THREE.MathUtils.lerp(startX, THROAT_X, move);
        const y = THREE.MathUtils.lerp(startY, BED_Y + 0.04, move * move);
        const sc = THREE.MathUtils.clamp((THROAT_X - x) / 0.18, 0, 1);
        m.compose(pos.set(x, y, startZ), quat, scl.setScalar(sc));
      }
      floodMesh.setMatrixAt(i, m);
    }
    floodMesh.instanceMatrix.needsUpdate = true;

    // Quiet needle beacon (line 14,238) rides out of the press throat atop the bounded 531-token digest
    const needleOut = ramp(T, at('flood', 0.42), at('flood', 0.80));
    needleBeacon.position.set(
      THREE.MathUtils.lerp(-0.10, EXIT_X0 + 0.22, needleOut),
      THREE.MathUtils.lerp(BED_Y + 0.18, BED_Y + 0.28, needleOut),
      0.0,
    );
    needleBeacon.scale.setScalar(THREE.MathUtils.lerp(0.001, 1.0, ramp(T, at('flood', 0.35), at('flood', 0.52))));

    // Full captured archive copy shuttles along rear rail from Press (x=0.85) to Vault (x=2.65)
    const shuttleT = ramp(T, at('flood', 0.55), at('vault', 0.35));
    archiveShuttle.position.set(THREE.MathUtils.lerp(0.85, 2.65, shuttleT), 0.16, -0.58);
    archiveShuttle.scale.setScalar(ramp(T, at('flood', 0.45), at('flood', 0.62)));
  }

  stage.layer('token_stream', streamGroup, {
    update: updateTokenStream,
    box: [[-2.12, 0.05, -0.60], [1.48, 1.25, 0.60]],
  });

  // ---------------------------------------------------------------------------
  // LAYER 2: vault_slice — Local disk archive in open drawer + 21-line retrieval slice + stable prompt prefix
  // Station 2 (evidence_vault) sits at x = 2.75, z = -0.05; open drawer floor at z ~ +0.43, y ~ 0.62.
  // ---------------------------------------------------------------------------
  const vaultGroup = new THREE.Group();
  const VX = 2.75;
  const VZ = -0.05;

  // Archived cartridges resting directly inside the 6 real divider bays of the pulled-out drawer (Neutral #8D939A)
  const bayGeo = new RoundedBoxGeometry(0.10, 0.13, 0.22, 1, 0.005);
  const bayMesh = new THREE.InstancedMesh(bayGeo, litMat(PALETTE.neutral, 0.58, 0.22), 18);
  bayMesh.castShadow = bayMesh.receiveShadow = true;
  for (let i = 0; i < 18; i++) {
    const col = i % 6;
    const row = Math.floor(i / 6);
    m.compose(
      pos.set(VX - 0.375 + col * 0.15, 0.67 + row * 0.032, VZ + 0.54 + (row - 1) * 0.045),
      quat,
      scl.set(1, 1, 1),
    );
    bayMesh.setMatrixAt(i, m);
  }
  bayMesh.instanceMatrix.needsUpdate = true;
  vaultGroup.add(bayMesh);

  // Stable Prompt Prefix column beside the vault (Blue #6F96FF — locked & untouched across turns!)
  const prefixGroup = new THREE.Group();
  prefixGroup.position.set(VX + 0.98, 0, -0.22);
  for (let i = 0; i < 6; i++) {
    const block = new THREE.Mesh(
      new RoundedBoxGeometry(0.32, 0.075, 0.28, 1, 0.006),
      litMat(PALETTE.blue, 0.42, 0.12),
    );
    block.position.y = 0.05 + i * 0.095;
    block.castShadow = block.receiveShadow = true;
    prefixGroup.add(block);
  }
  // Exact-colour blue front lock stripe on the prefix stack
  const prefixStripe = new THREE.Mesh(new THREE.BoxGeometry(0.33, 0.56, 0.015), exactMat(PALETTE.blue));
  prefixStripe.position.set(0, 0.30, 0.145);
  prefixGroup.add(prefixStripe);
  vaultGroup.add(prefixGroup);

  // Retrieved 21-line evidence slice (Blue body + Amber handle tag) that lifts out of the open drawer
  // up to the real overhead blue pickup carriage and appends cleanly onto the TOP of the prompt prefix!
  const slicePacket = new THREE.Group();
  const sliceBody = new THREE.Mesh(
    new RoundedBoxGeometry(0.28, 0.068, 0.24, 1, 0.006),
    litMat(PALETTE.blue, 0.38, 0.15),
  );
  sliceBody.castShadow = true;
  const sliceTag = new THREE.Mesh(new THREE.BoxGeometry(0.29, 0.022, 0.25), exactMat(PALETTE.amber));
  sliceTag.position.y = 0.044;
  slicePacket.add(sliceBody, sliceTag);
  vaultGroup.add(slicePacket);

  // Vertical retrieval drop wire connecting overhead carriage to the lifted slice
  const tether = new THREE.Mesh(new THREE.CylinderGeometry(0.007, 0.007, 1, 8), litMat(PALETTE.amber, 0.4, 0.5));
  vaultGroup.add(tether);

  function updateVaultSlice({ T }) {
    const lift = ramp(T, at('vault', 0.18), at('vault', 0.50));
    const travel = ramp(T, at('vault', 0.50), at('vault', 0.82));
    const dock = ramp(T, at('vault', 0.80), at('vault', 0.96));

    const sx = THREE.MathUtils.lerp(VX + 0.075, VX + 0.98, travel);
    const sz = THREE.MathUtils.lerp(VZ + 0.54, -0.22, travel);
    const sy = THREE.MathUtils.lerp(0.74, 1.18, lift) - dock * 0.54;
    slicePacket.position.set(sx, sy, sz);

    const railY = 1.44;
    const len = Math.max(0.02, railY - sy);
    tether.position.set(sx, sy + len / 2, sz);
    tether.scale.set(1, len, 1);
    tether.visible = lift > 0.02 && dock < 0.95;
  }

  stage.layer('vault_slice', vaultGroup, {
    update: updateVaultSlice,
    box: [[2.10, 0.05, -0.50], [3.96, 1.50, 0.86]],
  });

  // ---------------------------------------------------------------------------
  // LAYER 3: anchor_track — Source Anchor Relocation vs. Unanchored Silent Drift
  // Mounted cleanly in front of Station 3's left-flank vertical leadscrew (x = 4.41, z = 0.46).
  // ---------------------------------------------------------------------------
  const anchorGroup = new THREE.Group();
  const AX = 4.18;
  const AZ = 0.78;
  anchorGroup.position.set(AX, 0, AZ);

  // Machined upright steel backing board for the code-line specimen
  const backplate = new THREE.Mesh(
    new RoundedBoxGeometry(0.68, 1.18, 0.04, 1, 0.005),
    litMat(PALETTE.surface2, 0.65, 0.4),
  );
  backplate.position.set(0, 0.64, -0.04);
  backplate.castShadow = backplate.receiveShadow = true;
  anchorGroup.add(backplate);

  // 12 original code lines (indices 3..5 are the anchored 3-line target span)
  const LINE_COUNT = 12;
  const LINE_STEP = 0.072;
  const TOP_Y = 1.08;
  const lineBars = [];
  const lineGeo = new RoundedBoxGeometry(0.46, 0.046, 0.042, 1, 0.004);
  for (let i = 0; i < LINE_COUNT; i++) {
    const isTarget = i >= 3 && i <= 5;
    const widthScale = isTarget ? 1.0 : 0.65 + ((i * 37) % 10) * 0.032;
    const mat = isTarget ? litMat(PALETTE.blue, 0.4, 0.1) : litMat(PALETTE.neutral, 0.6, 0.2);
    const bar = new THREE.Mesh(lineGeo, mat);
    bar.scale.x = widthScale;
    bar.position.set(-(1 - widthScale) * 0.23, TOP_Y - i * LINE_STEP, 0.01);
    bar.castShadow = true;
    bar.userData = { index: i, isTarget };
    anchorGroup.add(bar);
    lineBars.push(bar);
  }

  // 3 newly inserted lines that slide in from the left at the top of the file ("insert-above")
  const insertedBars = [];
  for (let j = 0; j < 3; j++) {
    const bar = new THREE.Mesh(lineGeo, litMat('#A5AEBA', 0.45, 0.15));
    bar.scale.x = 0.82;
    bar.position.set(-0.04, TOP_Y - j * LINE_STEP, 0.015);
    bar.castShadow = true;
    anchorGroup.add(bar);
    insertedBars.push(bar);
  }

  // Left indicator: UNANCHORED bare line pointer (stays frozen at lines 3..5, switches sharply to Red #FF7768!)
  const bareMat = exactMat(PALETTE.blue);
  const barePointer = new THREE.Mesh(new THREE.BoxGeometry(0.045, LINE_STEP * 3.05, 0.055), bareMat);
  barePointer.position.set(-0.29, TOP_Y - 4 * LINE_STEP, 0.02);
  anchorGroup.add(barePointer);

  // Right indicator: ANCHORED caliper jaw (physically glides down 3 lines to track content, switches to Green #63D3A4!)
  const anchorMat = exactMat(PALETTE.blue);
  const caliperJaw = new THREE.Group();
  const jawSpine = new THREE.Mesh(new THREE.BoxGeometry(0.05, LINE_STEP * 3.15, 0.06), anchorMat);
  const jawTop = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.022, 0.06), anchorMat);
  jawTop.position.set(-0.04, LINE_STEP * 1.55, 0);
  const jawBot = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.022, 0.06), anchorMat);
  jawBot.position.set(-0.04, -LINE_STEP * 1.55, 0);
  caliperJaw.add(jawSpine, jawTop, jawBot);
  caliperJaw.position.set(0.30, TOP_Y - 4 * LINE_STEP, 0.02);
  anchorGroup.add(caliperJaw);

  // Refusal badge block at the bottom right showing rewrite-the-span -> Amber refusal (0 wrong answers)
  const refuseMat = exactMat(PALETTE.amber);
  const refuseGate = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.036, 0.05), refuseMat);
  refuseGate.position.set(0.24, 0.14, 0.02);
  anchorGroup.add(refuseGate);

  const cGreen = new THREE.Color(PALETTE.green);

  function updateAnchorTrack({ T }) {
    // Lines insert above during chapter "anchors" (local 0.20 -> 0.62)
    const shift = ramp(T, at('anchors', 0.20), at('anchors', 0.62));
    const relocated = ramp(T, at('anchors', 0.45), at('anchors', 0.62));
    const insertedIn = ramp(T, at('anchors', 0.14), at('anchors', 0.52));

    for (let i = 0; i < LINE_COUNT; i++) {
      const bar = lineBars[i];
      const y = TOP_Y - (i + shift * 3.0) * LINE_STEP;
      bar.position.y = y;
      // Hide bottom 3 bars cleanly once pushed off the specimen plate
      bar.visible = y > 0.16;
      if (bar.userData.isTarget) {
        // Switch cleanly from Blue (initial) to Green (verified/relocated) — never held at an off-palette blend
        bar.material.color.copy(relocated > 0.5 ? cGreen : cBlue);
      }
    }

    for (let j = 0; j < 3; j++) {
      const bar = insertedBars[j];
      bar.position.set(THREE.MathUtils.lerp(-0.46, -0.04, insertedIn), TOP_Y - j * LINE_STEP, 0.018);
      bar.scale.set(0.82 * Math.max(0.001, insertedIn), 1, 1);
    }

    // Bare pointer stays stuck at original coordinates and switches to Red (silently wrong!)
    bareMat.color.copy(shift > 0.35 ? cRed : cBlue);
    // Anchored caliper smoothly follows the shifted target span and switches to Green (relocated!)
    caliperJaw.position.y = TOP_Y - (4 + shift * 3.0) * LINE_STEP;
    anchorMat.color.copy(relocated > 0.5 ? cGreen : cBlue);

    // Refusal indicator appears cleanly in the last third of the chapter
    refuseGate.scale.x = Math.max(0.001, ramp(T, at('anchors', 0.66), at('anchors', 0.82)));
  }

  stage.layer('anchor_track', anchorGroup, {
    update: updateAnchorTrack,
    box: [[3.80, 0.06, 0.64], [4.56, 1.28, 0.90]],
  });

  // ---------------------------------------------------------------------------
  // LAYER 4: host_lanes — Seven parallel coding harnesses aligned to the real 7 channels of anchor_manifold
  // Real channels sit at local x = -0.72 + i * 0.24, bed top y = 0.54, Birth Gate z = +0.26, Output Gate z = -0.26.
  // ---------------------------------------------------------------------------
  const lanesGroup = new THREE.Group();
  const MX = 5.55;
  const LANE_SPECS = [
    { name: 'claude', birthRewrite: false, outputGate: true },
    { name: 'codex', birthRewrite: false, outputGate: true },
    { name: 'antigravity', birthRewrite: true, outputGate: false }, // exact ADR-005 boundary!
    { name: 'hermes', birthRewrite: true, outputGate: true },
    { name: 'omp', birthRewrite: true, outputGate: true },
    { name: 'opencode', birthRewrite: true, outputGate: true },
    { name: 'dsh', birthRewrite: true, outputGate: true },
  ];

  const lanePackets = [];
  const laneGeo = new RoundedBoxGeometry(0.13, 0.075, 0.20, 1, 0.006);
  for (let i = 0; i < LANE_SPECS.length; i++) {
    const spec = LANE_SPECS[i];
    const x = MX - 0.72 + i * 0.24;
    const mat = litMat(PALETTE.red, 0.45, 0.15);
    const pkt = new THREE.Mesh(laneGeo, mat);
    pkt.castShadow = true;
    lanesGroup.add(pkt);
    lanePackets.push({ mesh: pkt, x, spec, index: i });

    // Gate enforcement indicator pill mounted atop each lane's real gate actuator bridge (y = 0.98)
    // Front Birth Gate marker (z = +0.26)
    const birthPill = new THREE.Mesh(
      new THREE.BoxGeometry(0.15, 0.028, 0.055),
      exactMat(spec.birthRewrite ? PALETTE.amber : PALETTE.grid),
    );
    birthPill.position.set(x, 0.98, 0.26);
    lanesGroup.add(birthPill);

    // Rear Output Gate marker (z = -0.26): Red on Antigravity (unsupported boundary!), Blue on others
    const outPill = new THREE.Mesh(
      new THREE.BoxGeometry(0.15, 0.028, 0.055),
      exactMat(spec.outputGate ? PALETTE.blue : PALETTE.red),
    );
    outPill.position.set(x, 0.98, -0.26);
    lanesGroup.add(outPill);
  }

  // ACP Handoff bridge token (Amber #F0B429) gliding laterally across the bounded exit lanes
  const handoffToken = new THREE.Mesh(
    new RoundedBoxGeometry(0.13, 0.055, 0.13, 1, 0.005),
    exactMat(PALETTE.amber),
  );
  handoffToken.castShadow = true;
  lanesGroup.add(handoffToken);

  function updateHostLanes({ T }) {
    const flow = ramp(T, at('harnesses', 0.08), at('harnesses', 0.82));
    const handoff = ramp(T, at('harnesses', 0.48), at('harnesses', 0.90));

    for (const lp of lanePackets) {
      // Stagger each lane slightly for crisp visual rhythm
      const p = THREE.MathUtils.clamp((flow * 1.18) - lp.index * 0.025, 0, 1);
      const z = THREE.MathUtils.lerp(0.50, -0.48, p);
      // Bounded threshold: birthRewrite bounds at z = +0.26 (p ~ 0.26), outputGate bounds at z = -0.26 (p ~ 0.74)
      const gateP = lp.spec.birthRewrite ? 0.26 : 0.74;
      const bounded = p > gateP;
      const scaleZ = bounded ? 0.45 : 1.08;
      const scaleY = bounded ? 0.65 : 1.12;
      lp.mesh.position.set(lp.x, 0.56 + (scaleY * 0.038), z);
      lp.mesh.scale.set(1, scaleY, scaleZ);
      lp.mesh.material.color.copy(bounded ? cBlue : cRed);
    }

    // Compact Amber ACP checkpoint handle crosses between Lane 0 and Lane 5 above the bounded exit lip
    const x0 = MX - 0.72;
    const x5 = MX - 0.72 + 5 * 0.24;
    handoffToken.position.set(THREE.MathUtils.lerp(x0, x5, handoff), 0.74, -0.48);
    handoffToken.scale.setScalar(ramp(T, at('harnesses', 0.42), at('harnesses', 0.56)));
  }

  stage.layer('host_lanes', lanesGroup, {
    update: updateHostLanes,
    box: [[4.65, 0.48, -0.60], [6.45, 1.06, 0.60]],
  });

  // ---------------------------------------------------------------------------
  // 3D TROIKA LABELS (registered with stage.label so check_site validates legibility & bounds)
  // Shown only during their single-station focus chapter (never cluttering wide group panoramas).
  // ---------------------------------------------------------------------------
  stage.label(
    'press-label',
    makeLabel('CTX RUN · 302K -> 531', {
      size: 0.095,
      color: PALETTE.amber,
      position: [0.0, 1.88, 0.0],
      rotation: [0, -0.52, 0],
    }),
    'containment_press',
  );

  stage.label(
    'vault-label',
    makeLabel('LOCAL STORE · 64 MiB', {
      size: 0.088,
      color: PALETTE.blue,
      position: [0.0, 1.54, 0.0],
      rotation: [0, 0.52, 0],
    }),
    'evidence_vault',
  );

  stage.label(
    'anchor-label',
    makeLabel('ANCHOR · 0 WRONG', {
      size: 0.052,
      color: PALETTE.green,
      position: [0.0, 1.24, 0.02],
      rotation: [0, -0.28, 0],
    }),
    'anchor_track',
  );

  stage.label(
    'manifold-label',
    makeLabel('7 HARNESSES · 2 GATES', {
      size: 0.092,
      color: PALETTE.ink,
      position: [-0.05, 1.12, -0.42],
      rotation: [-1.12, 0.12, 0],
    }),
    'anchor_manifold',
  );
}
