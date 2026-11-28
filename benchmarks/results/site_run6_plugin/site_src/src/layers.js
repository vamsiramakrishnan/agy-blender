// layers.js: The followed evidence rendered as InstancedMesh layers + troika 3D labels.
// Everything is a pure function of story time T (scrubbable, reversible, deterministic).
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { Text } from 'troika-three-text';
import { ramp } from './shots.js';
import { lit } from './stage.js';
import { PROPS } from './props.js';

// Palette colours scaled for the stage lighting so lit blocks keep their exact design hue with full 3D shading.
const COL_RED = lit('#FF7768');
const COL_BLUE = lit('#6F96FF');
const COL_AMBER = lit('#F0B429');
const COL_GREEN = lit('#63D3A4');
const COL_SLATE = lit('#525B68');
const COL_NEUTRAL = lit('#39404B');

// ---------------------------------------------------------------------------
// 1. FLOOD LAYER: 302,628 tokens (6,053 slabs @ 50 tok/slab) -> 531 tokens (11 slabs)
// ---------------------------------------------------------------------------
function buildFloodLayer(at) {
  const BEFORE = 302628, AFTER = 531, PER = 50;
  const N = Math.round(BEFORE / PER);   // 6,053 slabs
  const KEEP = Math.round(AFTER / PER); // 11 kept slabs (10 blue digest + 1 amber needle)

  const COLS = 28, DEEP = 16;
  const ROWS = Math.ceil(N / COLS);     // 217
  const LEVELS = Math.ceil(ROWS / DEEP); // 14

  const X0 = 1.08, Z0 = -0.52, DX = 0.092, DZ = 0.068, DY = 0.052;
  const PRESS_GATE = 0.35;
  const EXIT_BASE = -1.15;
  const FALL = 1.1;

  const geo = new THREE.BoxGeometry(0.072, 0.032, 0.048);
  // White base material so per-instance colour carries the exact lit semantic hue.
  const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.55, metalness: 0.15 });
  const mesh = new THREE.InstancedMesh(geo, mat, N);
  mesh.castShadow = true;
  mesh.receiveShadow = true;

  // Assign static semantic colours once: 11 kept slabs (10 blue + 1 amber needle at index 5), bulk slabs flood red/slate.
  const cTmp = new THREE.Color();
  for (let i = 0; i < N; i++) {
    const row = Math.floor(i / COLS);
    const col = i % COLS;
    const isKept = row < KEEP && col === 0;
    if (isKept) {
      cTmp.copy(row === 5 ? COL_AMBER : COL_BLUE);
    } else {
      // Subtle banding across log lines so the 6,053-slab stack reads as structured lines rather than a solid lump.
      cTmp.copy((row + col) % 7 === 0 ? COL_RED : COL_SLATE);
      if (row % 3 === 0) cTmp.lerp(COL_RED, 0.55);
    }
    mesh.setColorAt(i, cTmp);
  }
  mesh.instanceColor.needsUpdate = true;

  const m = new THREE.Matrix4();
  const p = new THREE.Vector3();
  const q = new THREE.Quaternion();
  const s = new THREE.Vector3();

  function update({ T }) {
    const compressPhase = ramp(T, at('digest', 0.02), at('digest', 0.62));
    for (let i = 0; i < N; i++) {
      const row = Math.floor(i / COLS);
      const col = i % COLS;
      const level = Math.floor(row / DEEP);
      const laneZ = Z0 + (row % DEEP) * DZ;
      const baseY = 0.02 + level * DY;

      // In intro/flood the top levels finish settling onto the massive intake stack (bottom 65% already present at T=0!).
      const settleStart = at('intro', -0.25 + 0.72 * (level / LEVELS) + 0.08 * (col / COLS));
      const settle = ramp(T, settleStart, settleStart + 0.35);

      // In digest, rows march cleanly along -X into the open Digest Press channel without crossing each other.
      const goStart = at('digest', 0.02 + 0.42 * (row / ROWS));
      const march = ramp(T, goStart, goStart + 0.28);

      const isKept = row < KEEP && col === 0;
      if (isKept) {
        // Kept slabs glide smoothly through the open press channel onto the calibrated output rail at x in [-1.95, -1.05].
        const isNeedle = row === 5;
        const targetX = EXIT_BASE - row * 0.082;
        const targetY = isNeedle ? 0.36 : 0.30;
        const targetZ = isNeedle ? 0.0 : (row % 2 === 0 ? -0.11 : 0.11);
        const startX = X0 + col * DX;
        const px = THREE.MathUtils.lerp(startX, targetX, march);
        const py = THREE.MathUtils.lerp(baseY + FALL * (1 - settle), targetY, march);
        const pz = THREE.MathUtils.lerp(laneZ, targetZ, march);
        const sx = isNeedle ? 1.45 : 1.05;
        const sy = isNeedle ? 2.2 : 1.15;
        const sz = isNeedle ? 2.1 : 1.25;
        m.compose(p.set(px, py, pz), q, s.set(sx, sy, sz));
      } else {
        // Discarded bulk marches into the press intake and compresses cleanly inside the press housing.
        const travel = X0 + COLS * DX - PRESS_GATE;
        const px = X0 + col * DX - march * travel;
        const py = baseY + FALL * (1 - settle);
        const scale = settle <= 0.001 ? 0 : THREE.MathUtils.clamp((px - PRESS_GATE - 0.04) / DX, 0, 1);
        // Hide any remnants completely once well past digest so finale shows the clean bounded state.
        const postFade = 1 - ramp(T, at('digest', 0.75), at('digest', 0.92));
        const finalScale = scale * (compressPhase > 0.95 ? postFade : 1);
        m.compose(p.set(Math.max(px, PRESS_GATE), py, laneZ), q, s.setScalar(finalScale));
      }
      mesh.setMatrixAt(i, m);
    }
    mesh.instanceMatrix.needsUpdate = true;
  }

  return { mesh, update };
}

// ---------------------------------------------------------------------------
// 2. NEEDLE + EVIDENCE SHUTTLE LAYER (Press exit -> Anchor Rack open drawer)
// ---------------------------------------------------------------------------
function buildNeedleLayer(at) {
  const group = new THREE.Group();

  // Calibrated dark-steel exit & transfer rail linking Digest Press (-0.9, 0, 0) to Anchor Rack (-2.65, 0, -0.35).
  const railMat = new THREE.MeshStandardMaterial({ color: 0x242931, metalness: 0.72, roughness: 0.42 });
  const blueStripMat = new THREE.MeshStandardMaterial({ color: COL_BLUE, roughness: 0.45, metalness: 0.2 });
  const amberMat = new THREE.MeshStandardMaterial({ color: COL_AMBER, roughness: 0.35, metalness: 0.25 });

  // Two parallel rails angled smoothly from press exit (-0.95, 0.24, 0) toward anchor rack drawer (-2.55, 0.24, -0.32).
  const a = new THREE.Vector3(-0.92, 0.24, 0.0);
  const b = new THREE.Vector3(-2.55, 0.24, -0.32);
  const dir = new THREE.Vector3().subVectors(b, a);
  const len = dir.length();
  const mid = new THREE.Vector3().addVectors(a, b).multiplyScalar(0.5);
  const yaw = -Math.atan2(dir.z, dir.x);

  const trackGroup = new THREE.Group();
  trackGroup.position.copy(mid);
  trackGroup.rotation.y = yaw;

  for (const offsetZ of [-0.14, 0.14]) {
    const rail = new THREE.Mesh(new THREE.BoxGeometry(len, 0.032, 0.036), railMat);
    rail.position.set(0, 0, offsetZ);
    rail.castShadow = true;
    rail.receiveShadow = true;
    trackGroup.add(rail);
  }
  // Support stanchions grounding the rail to the floor
  for (const frac of [-0.36, 0.0, 0.36]) {
    const leg = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.24, 0.32), railMat);
    leg.position.set(frac * len, -0.12, 0);
    leg.castShadow = true;
    leg.receiveShadow = true;
    trackGroup.add(leg);
  }
  group.add(trackGroup);

  // Moving digest shuttle cartridge carrying the amber quiet-needle marker (L14238) + blue receipt bar.
  const shuttle = new THREE.Group();
  const baseTray = new THREE.Mesh(new RoundedBoxGeometry(0.32, 0.05, 0.30, 2, 0.008), railMat);
  baseTray.castShadow = true;
  baseTray.receiveShadow = true;
  shuttle.add(baseTray);

  const digestBar = new THREE.Mesh(new RoundedBoxGeometry(0.26, 0.04, 0.06, 2, 0.006), blueStripMat);
  digestBar.position.set(0, 0.045, -0.07);
  digestBar.castShadow = true;
  shuttle.add(digestBar);

  const needleBlock = new THREE.Mesh(new RoundedBoxGeometry(0.22, 0.13, 0.09, 2, 0.01), amberMat);
  needleBlock.position.set(0, 0.088, 0.055);
  needleBlock.castShadow = true;
  shuttle.add(needleBlock);

  group.add(shuttle);

  const curPos = new THREE.Vector3();
  function update({ T }) {
    // Emerges from the Digest Press exit during "digest", pauses at the exit for reading, then glides to Anchor Rack.
    const emerge = ramp(T, at('digest', 0.05), at('digest', 0.42));
    const transfer = ramp(T, at('anchors', 0.0), at('anchors', 0.38));
    const tRail = 0.26 * emerge + 0.72 * transfer;
    curPos.lerpVectors(a, b, tRail);
    curPos.y = 0.275;
    shuttle.position.copy(curPos);
    shuttle.rotation.y = yaw;
    const vis = ramp(T, at('digest', 0.0), at('digest', 0.15));
    shuttle.scale.setScalar(0.25 + 0.75 * vis);
  }

  return {
    group,
    update,
    // Fixed world framing box covering the press exit specimen zone for the "digest" shot
    box: [[-2.05, 0.0, -0.45], [-0.85, 0.58, 0.45]],
  };
}

// ---------------------------------------------------------------------------
// 3. ANCHORS LAYER: Shifting code-span bars + moving amber caliper on Anchor Rack
// ---------------------------------------------------------------------------
function buildAnchorsLayer(at) {
  const anchorProp = PROPS.anchor_rack;
  const group = new THREE.Group();
  group.position.fromArray(anchorProp.at);
  group.rotation.y = anchorProp.turn;

  const COUNT = 20;
  const barGeo = new RoundedBoxGeometry(0.76, 0.030, 0.065, 2, 0.005);
  const barMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.45, metalness: 0.22 });
  const bars = new THREE.InstancedMesh(barGeo, barMat, COUNT);
  bars.castShadow = true;
  bars.receiveShadow = true;
  group.add(bars);

  // Moving amber caliper bracket sliding along the vertical bay rail to track the relocated green span.
  const caliperMat = new THREE.MeshStandardMaterial({ color: COL_AMBER, roughness: 0.35, metalness: 0.45 });
  const caliper = new THREE.Group();
  const cLeft = new THREE.Mesh(new RoundedBoxGeometry(0.05, 0.13, 0.11, 2, 0.006), caliperMat);
  cLeft.position.set(-0.43, 0, 0.01);
  const cRight = new THREE.Mesh(new RoundedBoxGeometry(0.05, 0.13, 0.11, 2, 0.006), caliperMat);
  cRight.position.set(0.43, 0, 0.01);
  const cBridge = new THREE.Mesh(new THREE.BoxGeometry(0.90, 0.018, 0.022), caliperMat);
  cBridge.position.set(0, 0, 0.065);
  cLeft.castShadow = cRight.castShadow = cBridge.castShadow = true;
  caliper.add(cLeft, cRight, cBridge);
  group.add(caliper);

  // Static colours for bars: top 3 are inserted edit lines (blue), slot 11..12 is target anchored span (green),
  // slot 17 is rewritten-and-refused span (red), rest neutral slate.
  const c = new THREE.Color();
  const TARGET_INDEX = 10;
  const REFUSED_INDEX = 14;
  const BASE_Y = 0.56;
  const STEP_Y = 0.044;
  const FRONT_Z = 0.36;

  const m = new THREE.Matrix4();
  const p = new THREE.Vector3();
  const q = new THREE.Quaternion();
  const s = new THREE.Vector3();

  function update({ T }) {
    // During "anchors", 3 lines insert above -> shifting the anchored span cleanly downward while caliper relocates.
    const editShift = ramp(T, at('anchors', 0.08), at('anchors', 0.46));
    const refuseFlip = ramp(T, at('anchors', 0.18), at('anchors', 0.48));

    for (let i = 0; i < COUNT; i++) {
      const isInserted = i >= COUNT - 3;
      let y, z = FRONT_Z, sx = 1.0;

      if (isInserted) {
        // 3 new lines slide in from +Z at the top of the bay
        const slot = i - (COUNT - 3); // 0, 1, 2
        y = BASE_Y + (COUNT - 3 + slot) * STEP_Y;
        z = FRONT_Z + (1 - editShift) * 0.22;
        sx = editShift;
        c.copy(COL_BLUE);
      } else {
        // Existing lines shift downward by 3 slots as the edit lands above them
        const shiftSlots = i <= TARGET_INDEX + 2 ? 3 * editShift : 0;
        y = BASE_Y + Math.max(0, i - shiftSlots) * STEP_Y;
        if (i === TARGET_INDEX || i === TARGET_INDEX + 1) {
          c.copy(COL_GREEN);
          z = FRONT_Z + 0.025;
        } else if (i === REFUSED_INDEX || i === REFUSED_INDEX + 1) {
          c.copy(refuseFlip > 0.45 ? COL_RED : COL_SLATE);
          z = FRONT_Z + 0.025 * refuseFlip;
        } else {
          c.copy(i % 2 === 0 ? COL_SLATE : COL_NEUTRAL);
        }
      }
      bars.setColorAt(i, c);
      m.compose(p.set(0, y, z), q, s.set(Math.max(0.001, sx), 1, 1));
      bars.setMatrixAt(i, m);
    }
    bars.instanceMatrix.needsUpdate = true;
    if (bars.instanceColor) bars.instanceColor.needsUpdate = true;

    // Caliper follows the relocated green target span (lines TARGET_INDEX..TARGET_INDEX+1) with zero drift!
    const targetY = BASE_Y + (TARGET_INDEX + 0.5 - 3 * editShift) * STEP_Y;
    caliper.position.set(0, targetY, FRONT_Z + 0.015);
  }

  return {
    group,
    update,
    box: [[-4.15, 0.45, -1.1], [-2.65, 1.55, 0.25]],
  };
}

// ---------------------------------------------------------------------------
// 4. HOST LANES LAYER: 7 parallel channels x 6 evidence packets on Host Manifold
// ---------------------------------------------------------------------------
function buildLanesLayer(at) {
  const hostProp = PROPS.host_manifold;
  const group = new THREE.Group();
  group.position.fromArray(hostProp.at);
  group.rotation.y = hostProp.turn;

  // 7 hosts matching docs/HOST-CAPABILITIES.md:
  // 0: Claude (output gate), 1: Codex (output gate), 2: Antigravity (birth rewrite, NO output gate),
  // 3: Hermes (birth+output), 4: OMP (birth+output), 5: OpenCode (birth+output), 6: DSH (deny + output)
  const LANES = 7;
  const PER_LANE = 6;
  const TOTAL = LANES * PER_LANE;
  const LANE_DX = 0.31;
  const LANE_X0 = -((LANES - 1) * LANE_DX) / 2;
  const Z_START = 0.56;
  const Z_BIRTH = 0.20;
  const Z_OUTPUT = -0.22;
  const Z_END = -0.58;
  const BED_Y = 0.215;

  const pktGeo = new RoundedBoxGeometry(0.14, 0.11, 0.11, 2, 0.008);
  const pktMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.45, metalness: 0.2 });
  const packets = new THREE.InstancedMesh(pktGeo, pktMat, TOTAL);
  packets.castShadow = true;
  packets.receiveShadow = true;
  group.add(packets);

  const m = new THREE.Matrix4();
  const p = new THREE.Vector3();
  const q = new THREE.Quaternion();
  const s = new THREE.Vector3();
  const col = new THREE.Color();

  function update({ T }) {
    // Scroll-driven steady travel along all 7 lanes (pure function of T).
    const flow = (T - at('hosts', 0)) * 0.45 + 0.65;

    for (let lane = 0; lane < LANES; lane++) {
      const lx = LANE_X0 + lane * LANE_DX;
      const isAgy = lane === 2;
      const isBirthRewrite = lane === 2 || lane === 3 || lane === 4 || lane === 5;
      const hasOutputGate = !isAgy;

      for (let k = 0; k < PER_LANE; k++) {
        const idx = lane * PER_LANE + k;
        // Wrap smoothly in [0, 1) purely from flow + lane phase
        const rawU = (flow + k / PER_LANE + lane * 0.07) % 1;
        const u = rawU < 0 ? rawU + 1 : rawU;
        const z = THREE.MathUtils.lerp(Z_START, Z_END, u);

        let sx = 1.0, sy = 1.0, sz = 1.0;
        if (z > Z_BIRTH) {
          // Incoming raw tool payload (tall red flood block)
          col.copy(COL_RED);
          sx = 1.15; sy = 1.35; sz = 1.05;
        } else if (z > Z_OUTPUT) {
          // Between Birth Gate and Output Gate
          if (isAgy) {
            // Antigravity rewrites at birth (amber bounded block)
            col.copy(COL_AMBER);
            sx = 0.78; sy = 0.58; sz = 0.75;
          } else if (isBirthRewrite) {
            // Birth-capable host pre-bounds in blue
            col.copy(COL_BLUE);
            sx = 0.78; sy = 0.62; sz = 0.75;
          } else {
            // Claude / Codex / DSH run command as written; raw payload still waiting for output gate
            col.copy(COL_RED);
            sx = 1.08; sy = 1.2; sz = 0.95;
          }
        } else {
          // Past Output Gate -> model prompt side
          if (hasOutputGate) {
            col.copy(COL_BLUE);
            sx = 0.72; sy = 0.48; sz = 0.70;
          } else {
            // Antigravity: already rewritten at birth in amber (output gate is open/red boundary)
            col.copy(COL_AMBER);
            sx = 0.72; sy = 0.50; sz = 0.70;
          }
        }

        // Fade smoothly at lane entry/exit ends so packets never pop
        const endFade = Math.min(THREE.MathUtils.smoothstep(u, 0.0, 0.08), 1 - THREE.MathUtils.smoothstep(u, 0.92, 1.0));
        const y = BED_Y + (0.055 * sy * endFade);
        m.compose(p.set(lx, y, z), q, s.set(sx * endFade, sy * endFade, sz * endFade));
        packets.setMatrixAt(idx, m);
        packets.setColorAt(idx, col);
      }
    }
    packets.instanceMatrix.needsUpdate = true;
    if (packets.instanceColor) packets.instanceColor.needsUpdate = true;
  }

  return {
    group,
    update,
    box: [[-2.95, 0.1, -4.05], [-0.45, 0.9, -2.75]],
  };
}

// ---------------------------------------------------------------------------
// 5. TROIKA 3D LABELS (upright, self-hosted JetBrains Mono .woff, registered with stage)
// ---------------------------------------------------------------------------
function makeLabel(text, color, fontSize, pos, rotY = 0) {
  const t = new Text();
  t.text = text;
  t.font = new URL('fonts/jetbrains-mono.woff', document.baseURI).href;
  t.fontSize = fontSize;
  t.letterSpacing = 0.04;
  t.anchorX = 'center';
  t.anchorY = 'bottom';
  t.color = color;
  t.position.set(...pos);
  t.rotation.y = rotY;
  return t;
}

export function registerStoryLayers(site) {
  const at = site.at;

  // Upright 3D labels sized for the shots where each prop is the subject (~3% of camera distance),
  // angled cleanly toward each chapter's camera and shown only while that chapter is settled.
  const pressLabel = makeLabel('DIGEST PRESS · 570×', '#ECEAE3', 0.12, [0, 1.56, 0], -0.62);
  const needleLabel = makeLabel('L14238 · run:8956c843e556', '#F0B429', 0.088, [-1.52, 0.72, -0.08], -0.62);
  const anchorLabel = makeLabel('ANCHOR RACK · 0 WRONG', '#63D3A4', 0.11, [0, 1.76, 0], 0.14);
  const hostsLabel = makeLabel('7 HOSTS · BIRTH / OUTPUT GATES', '#6F96FF', 0.14, [0, 1.32, -0.52], 0);

  const flood = buildFloodLayer(at);
  site.layer('flood', flood.mesh, { update: flood.update });

  const needle = buildNeedleLayer(at);
  site.layer('needle', needle.group, {
    box: needle.box,
    update(state) {
      needle.update(state);
      const onDigest = state.chapter === 'digest' && !state.moving;
      pressLabel.visible = onDigest;
      needleLabel.visible = onDigest;
    },
  });

  const anchors = buildAnchorsLayer(at);
  site.layer('anchors', anchors.group, {
    box: anchors.box,
    update(state) {
      anchors.update(state);
      anchorLabel.visible = state.chapter === 'anchors' && !state.moving;
    },
  });

  const lanes = buildLanesLayer(at);
  site.layer('lanes', lanes.group, {
    box: lanes.box,
    update(state) {
      lanes.update(state);
      hostsLabel.visible = state.chapter === 'hosts' && !state.moving;
    },
  });

  site.label('press-label', pressLabel, 'digest_press');
  site.label('needle-label', needleLabel, 'needle');
  site.label('anchor-label', anchorLabel, 'anchor_rack');
  site.label('hosts-label', hostsLabel, 'host_manifold');
}
