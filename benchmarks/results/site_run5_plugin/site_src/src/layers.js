// layers.js: the followed evidence, shown at true scale with InstancedMesh and chapter-scoped troika-three-text labels.
// Every value is a pure function of story time T (via site.at); no per-frame easing.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { Text } from 'troika-three-text';
import { PROPS } from './props.js';
import { ramp } from './shots.js';

const PALETTE = {
  neutral: '#9BA2AB',
  neutralDim: '#5E6670',
  ink: '#ECEAE3',
  blue: '#6F96FF',
  amber: '#F0B429',
  red: '#FF7768',
  green: '#63D3A4',
};

const FONT_URL = new URL('fonts/jetbrains-mono.woff', document.baseURI).href;

function makeLabel(text, { size = 0.1, color = PALETTE.ink, anchorX = 'center', anchorY = 'bottom' } = {}) {
  const t = new Text();
  t.text = text;
  t.font = FONT_URL;
  t.fontSize = size;
  t.color = color;
  t.anchorX = anchorX;
  t.anchorY = anchorY;
  t.letterSpacing = 0.04;
  t.material.toneMapped = false;
  return t;
}

export function initLayers(site) {
  const at = site.at;

  // -------------------------------------------------------------------------
  // 1. LOG STREAM: 6,053 slabs (1 slab = 50 tokens of the 302,628-token log)
  //    Collapses through capture_press's open roller bed to 11 kept digest slabs (531 tok) + 1 amber needle (L14,238).
  // -------------------------------------------------------------------------
  const BEFORE = 302628;
  const AFTER = 531;
  const PER = 50;
  const N = Math.round(BEFORE / PER); // 6,053
  const KEEP = Math.round(AFTER / PER); // 11
  const COLS = 24;
  const DEEP = 10;
  const ROWS = Math.ceil(N / COLS);
  const LEVELS = Math.ceil(ROWS / DEEP); // 26

  const X_START = -3.28;
  const DX = 0.092;
  const DZ = 0.062;
  const DY = 0.032;
  const Z0 = -((DEEP - 1) * DZ) / 2;
  const BASE_Y = 0.024;
  const PRESS_X = 0.0;
  const OUT_X = 0.96;

  const slabGeo = new THREE.BoxGeometry(0.074, 0.022, 0.046);
  const slabMat = new THREE.MeshStandardMaterial({ color: '#ffffff', roughness: 0.52, metalness: 0.12 });
  const floodMesh = new THREE.InstancedMesh(slabGeo, slabMat, N);
  floodMesh.castShadow = true;
  floodMesh.receiveShadow = true;

  const cNeutral = new THREE.Color(PALETTE.neutral);
  const cDim = new THREE.Color(PALETTE.neutralDim);
  const cRed = new THREE.Color(PALETTE.red);
  const cBlue = new THREE.Color(PALETTE.blue);
  const cAmber = new THREE.Color(PALETTE.amber);
  const cGreen = new THREE.Color(PALETTE.green);

  const NEEDLE_I = Math.round(N * (14238 / 20001));
  for (let i = 0; i < N; i++) {
    const col = i % COLS;
    const row = Math.floor(i / COLS);
    const level = Math.floor(row / DEEP);
    if (i === NEEDLE_I) {
      floodMesh.setColorAt(i, cAmber);
    } else if (level >= LEVELS - 4) {
      floodMesh.setColorAt(i, cRed);
    } else if ((col + level) % 3 === 0) {
      floodMesh.setColorAt(i, cNeutral);
    } else {
      floodMesh.setColorAt(i, cDim);
    }
  }
  floodMesh.instanceColor.needsUpdate = true;

  // Dedicated 11 kept digest slabs + 1 bright amber quiet needle slab emerging from capture_press's blue aperture
  const digestGroup = new THREE.Group();
  const keptGeo = new RoundedBoxGeometry(0.11, 0.032, 0.5, 2, 0.004);
  const keptMat = new THREE.MeshStandardMaterial({ color: PALETTE.blue, roughness: 0.4, metalness: 0.15 });
  const keptMesh = new THREE.InstancedMesh(keptGeo, keptMat, KEEP);
  keptMesh.castShadow = true;
  keptMesh.receiveShadow = true;
  digestGroup.add(keptMesh);

  const needleSlab = new THREE.Mesh(
    new RoundedBoxGeometry(0.13, 0.042, 0.56, 2, 0.005),
    new THREE.MeshBasicMaterial({ color: PALETTE.amber, toneMapped: false }),
  );
  needleSlab.castShadow = true;
  digestGroup.add(needleSlab);

  const intakePlinth = new THREE.Mesh(
    new THREE.BoxGeometry(2.38, 0.024, 0.78),
    new THREE.MeshStandardMaterial({ color: '#2F3640', roughness: 0.72, metalness: 0.3 }),
  );
  intakePlinth.position.set(-2.22, 0.012, 0);
  intakePlinth.receiveShadow = true;

  const outPlinth = new THREE.Mesh(
    new THREE.BoxGeometry(0.84, 0.34, 0.68),
    new THREE.MeshStandardMaterial({ color: '#636C77', roughness: 0.48, metalness: 0.45 }),
  );
  outPlinth.position.set(1.28, 0.17, 0);
  outPlinth.castShadow = true;
  outPlinth.receiveShadow = true;
  digestGroup.add(outPlinth);

  const logRoot = new THREE.Group();
  logRoot.add(intakePlinth, floodMesh);

  const m = new THREE.Matrix4();
  const p = new THREE.Vector3();
  const q = new THREE.Quaternion();
  const s = new THREE.Vector3();

  // Chapter-scoped 3D labels (only shown in the chapters where their subject is framed, preventing off-frame or under-panel leaks)
  const pressLabel = makeLabel('CAPTURE PRESS', { size: 0.115, color: PALETTE.ink });
  pressLabel.position.set(0, 1.84, 0);
  site.label('press-label', pressLabel, 'capture_press');

  const needleLabel = makeLabel('L14,238 · NEEDLE', { size: 0.088, color: PALETTE.amber });
  needleLabel.position.set(1.42, 0.78, 0.08);

  const vaultLabel = makeLabel('LOCAL STORE · run:8956c843e556', { size: 0.084, color: PALETTE.blue });
  vaultLabel.position.set(0, 1.72, 0.24);
  site.label('vault-label', vaultLabel, 'artifact_vault');

  const caliperLabel = makeLabel('ANCHOR RELOCATOR · 0 WRONG', { size: 0.092, color: PALETTE.green });
  caliperLabel.position.set(-0.08, 1.32, 0);
  site.label('caliper-label', caliperLabel, 'anchor_caliper');

  const birthTag = makeLabel('BIRTH GATE · PreToolUse', { size: 0.115, color: PALETTE.amber, anchorX: 'center', anchorY: 'middle' });
  birthTag.position.set(0, 0.11, -0.72);
  birthTag.rotation.x = -Math.PI * 0.45;

  const outputTag = makeLabel('OUTPUT GATE · PostToolUse', { size: 0.115, color: PALETTE.blue, anchorX: 'center', anchorY: 'middle' });
  outputTag.position.set(0, 0.11, 0.74);
  outputTag.rotation.x = -Math.PI * 0.45;

  function updateLogStream({ T, chapter }) {
    const tDigestStart = at('digest', 0.05);
    const tDigestEnd = at('digest', 0.85);
    const floodAlert = ramp(T, at('flood', 0.15), at('flood', 0.45));
    const digestProgress = ramp(T, tDigestStart, tDigestEnd);

    for (let i = 0; i < N; i++) {
      const col = i % COLS;
      const row = Math.floor(i / COLS);
      const level = Math.floor(row / DEEP);
      const laneZ = Z0 + (row % DEEP) * DZ;

      const landStart = at('intro', 0.02 + 0.25 * (level / LEVELS));
      const landed = T < at('intro', 0.5) ? ramp(T, landStart, landStart + 0.18) : 1;
      const baseX = X_START + col * DX;
      const baseY = BASE_Y + 0.015 + level * (DY + 0.008 * floodAlert);

      const feedDelay = ((COLS - 1 - col) / COLS) * 0.42 + (level / LEVELS) * 0.28;
      const feed = ramp(digestProgress, feedDelay, feedDelay + 0.3);

      const x = baseX + feed * (PRESS_X + 0.32 - baseX);
      // Guide incoming slabs cleanly onto the press roller bed height (y = 0.36..0.68) as they enter the press jaw
      const y = baseY + (1 - landed) * 0.55 - feed * Math.max(0, baseY - 0.42) * 0.85;

      const shrink = x > -0.18 ? THREE.MathUtils.clamp(1 - (x + 0.18) / 0.45, 0, 1) : 1;
      const scale = landed * shrink;

      if (scale <= 0.002) {
        m.makeScale(0, 0, 0);
      } else {
        m.compose(p.set(x, y, laneZ), q, s.setScalar(scale));
      }
      floodMesh.setMatrixAt(i, m);
    }
    floodMesh.instanceMatrix.needsUpdate = true;

    const outShow = ramp(T, at('digest', 0.06), at('digest', 0.56));
    for (let k = 0; k < KEEP; k++) {
      const delay = (k / KEEP) * 0.45;
      const emerge = ramp(outShow, delay, delay + 0.55);
      const targetX = OUT_X + (k % 6) * 0.13;
      const targetY = 0.365 + Math.floor(k / 6) * 0.044;
      const x = THREE.MathUtils.lerp(0.45, targetX, emerge);
      const sc = emerge > 0.01 ? emerge : 0;
      m.compose(p.set(x, targetY, 0), q, s.setScalar(sc));
      keptMesh.setMatrixAt(k, m);
    }
    keptMesh.instanceMatrix.needsUpdate = true;

    const needleEmerge = ramp(T, at('digest', 0.12), at('digest', 0.52));
    needleSlab.visible = needleEmerge > 0.01;
    needleSlab.position.set(
      THREE.MathUtils.lerp(0.35, OUT_X + 0.32, needleEmerge),
      0.468,
      0,
    );
    needleSlab.scale.setScalar(Math.max(0.001, needleEmerge));

    // Scope labels strictly to the chapters where their subject is framed
    pressLabel.visible = chapter === 'flood' || chapter === 'digest';
    pressLabel.rotation.y = chapter === 'flood' ? -0.6 : 0.45;
    needleLabel.visible = chapter === 'digest' && needleEmerge > 0.35;
    needleLabel.rotation.y = 0.45;
    vaultLabel.visible = chapter === 'retrieve';
    vaultLabel.rotation.y = -0.1;
    caliperLabel.visible = chapter === 'anchors';
    caliperLabel.rotation.y = 0.42;
    birthTag.visible = chapter === 'harnesses';
    outputTag.visible = chapter === 'harnesses';
  }

  site.layer('log_stream', logRoot, {
    box: [[-3.38, 0, -0.45], [0.2, 1.15, 0.45]],
    update: updateLogStream,
  });

  site.layer('digest_output', digestGroup, {
    box: [[0.82, 0, -0.42], [1.74, 0.74, 0.42]],
  });
  site.label('needle-label', needleLabel, 'digest_output');

  // -------------------------------------------------------------------------
  // 2. VAULT EXCERPTS: archived slabs resting inside artifact_vault's open middle drawer
  //    (in Three.js Y-up local coords: drawer floor y=0.54, extended forward to z=+0.76).
  // -------------------------------------------------------------------------
  const vaultGroup = new THREE.Group();
  vaultGroup.position.fromArray(PROPS.artifact_vault.at);
  vaultGroup.rotation.y = PROPS.artifact_vault.turn;

  const ARCHIVE_N = 80;
  const archiveGeo = new THREE.BoxGeometry(0.06, 0.032, 0.09);
  const archiveMat = new THREE.MeshStandardMaterial({ color: '#9BA2AB', roughness: 0.55, metalness: 0.2 });
  const archiveMesh = new THREE.InstancedMesh(archiveGeo, archiveMat, ARCHIVE_N);
  archiveMesh.castShadow = true;
  archiveMesh.receiveShadow = true;
  // Place neatly in the 5 bays between the drawer's tray dividers (x in -0.52..+0.52, z in +0.16..+0.68)
  for (let i = 0; i < ARCHIVE_N; i++) {
    const col = i % 10;
    const row = Math.floor(i / 10) % 4;
    const lvl = Math.floor(i / 40);
    m.compose(
      p.set(-0.46 + col * 0.102, 0.57 + lvl * 0.038, 0.2 + row * 0.145),
      q,
      s.set(1, 1, 1),
    );
    archiveMesh.setMatrixAt(i, m);
  }
  archiveMesh.instanceMatrix.needsUpdate = true;
  vaultGroup.add(archiveMesh);

  // The retrieved 21-line excerpt slab (exact blue #6F96FF + amber handle tag) rising under the blue linear carriage
  const excerptGroup = new THREE.Group();
  const excerptSlab = new THREE.Mesh(
    new RoundedBoxGeometry(0.38, 0.046, 0.22, 2, 0.005),
    new THREE.MeshBasicMaterial({ color: PALETTE.blue, toneMapped: false }),
  );
  excerptSlab.castShadow = true;
  const excerptTag = new THREE.Mesh(
    new THREE.BoxGeometry(0.11, 0.052, 0.048),
    new THREE.MeshBasicMaterial({ color: PALETTE.amber, toneMapped: false }),
  );
  excerptTag.position.set(-0.12, 0.01, 0.12);
  excerptGroup.add(excerptSlab, excerptTag);
  excerptGroup.position.set(0.0, 0.65, 0.44);
  vaultGroup.add(excerptGroup);

  const linkRoot = new THREE.Group();
  linkRoot.add(vaultGroup);

  // Exact blue address vector connecting press output -> vault open drawer
  const beamStart = new THREE.Vector3(1.62, 0.47, 0.0);
  const beamEnd = new THREE.Vector3(2.55, 0.78, -0.78);
  const beamDir = new THREE.Vector3().subVectors(beamEnd, beamStart);
  const beamLen = beamDir.length();
  const addressBeam = new THREE.Mesh(
    new THREE.BoxGeometry(0.02, 0.018, beamLen),
    new THREE.MeshBasicMaterial({ color: PALETTE.blue, toneMapped: false }),
  );
  addressBeam.position.copy(beamStart).addScaledVector(beamDir, 0.5);
  addressBeam.lookAt(beamEnd);
  linkRoot.add(addressBeam);

  function updateVaultExcerpts({ T }) {
    const pull = ramp(T, at('retrieve', 0.15), at('retrieve', 0.72));
    excerptGroup.position.set(0.0, 0.65 + pull * 0.18, 0.44 + pull * 0.2);

    const beamGrow = ramp(T, at('digest', 0.55), at('retrieve', 0.45));
    addressBeam.visible = beamGrow > 0.01;
    addressBeam.scale.set(1, 1, Math.max(0.001, beamGrow));
    addressBeam.position.copy(beamStart).addScaledVector(beamDir, 0.5 * beamGrow);
  }

  site.layer('vault_excerpts', linkRoot, {
    box: [[2.05, 0.4, -1.7], [3.45, 1.35, -0.4]],
    update: updateVaultExcerpts,
  });

  // -------------------------------------------------------------------------
  // 3. ANCHOR SPANS: live source-line bars mounted cleanly on anchor_caliper's bed_plinth (top y = 0.40).
  // -------------------------------------------------------------------------
  const anchorGroup = new THREE.Group();
  anchorGroup.position.fromArray(PROPS.anchor_caliper.at);
  anchorGroup.rotation.y = PROPS.anchor_caliper.turn;

  const LINE_COUNT = 16;
  const LINE_STEP = 0.068;
  const LINE_X0 = -0.56;
  const LINE_Y = 0.425;
  const lineGeo = new RoundedBoxGeometry(0.048, 0.034, 0.42, 2, 0.004);
  const lineMat = new THREE.MeshStandardMaterial({ color: '#ffffff', roughness: 0.45, metalness: 0.15 });
  const lineMesh = new THREE.InstancedMesh(lineGeo, lineMat, LINE_COUNT);
  lineMesh.castShadow = true;
  lineMesh.receiveShadow = true;

  for (let i = 0; i < LINE_COUNT; i++) {
    if (i < 3) lineMesh.setColorAt(i, cAmber);
    else if (i >= 5 && i <= 7) lineMesh.setColorAt(i, cGreen);
    else if (i >= 14) lineMesh.setColorAt(i, cRed);
    else lineMesh.setColorAt(i, cNeutral);
  }
  lineMesh.instanceColor.needsUpdate = true;
  anchorGroup.add(lineMesh);

  // Bare unanchored pointer (stays stuck stale -> RED front rail marker)
  const barePointer = new THREE.Mesh(
    new THREE.BoxGeometry(0.22, 0.036, 0.09),
    new THREE.MeshBasicMaterial({ color: PALETTE.red, toneMapped: false }),
  );
  barePointer.position.set(LINE_X0 + 3.0 * LINE_STEP, LINE_Y + 0.036, 0.25);
  anchorGroup.add(barePointer);

  // Anchored repo: lock frame (relocates smoothly in lockstep with shifted target lines -> GREEN + AMBER hash prism)
  const anchorLock = new THREE.Group();
  const lockFrame = new THREE.Mesh(
    new THREE.BoxGeometry(0.22, 0.034, 0.52),
    new THREE.MeshBasicMaterial({ color: PALETTE.green, toneMapped: false }),
  );
  const hashHead = new THREE.Mesh(
    new THREE.BoxGeometry(0.08, 0.09, 0.09),
    new THREE.MeshBasicMaterial({ color: PALETTE.amber, toneMapped: false }),
  );
  hashHead.position.set(0, 0.065, -0.18);
  anchorLock.add(lockFrame, hashHead);
  anchorLock.position.set(LINE_X0 + 3.0 * LINE_STEP, LINE_Y + 0.095, 0);
  anchorGroup.add(anchorLock);

  function updateAnchorSpans({ T }) {
    const insert = ramp(T, at('anchors', 0.18), at('anchors', 0.65));
    const shift = insert * 3 * LINE_STEP;

    for (let i = 0; i < LINE_COUNT; i++) {
      if (i < 3) {
        const drop = ramp(insert, i * 0.15, i * 0.15 + 0.55);
        const y = LINE_Y + (1 - drop) * 0.34;
        m.compose(p.set(LINE_X0 + i * LINE_STEP, y, 0), q, s.setScalar(drop > 0.01 ? drop : 0));
      } else {
        const origIdx = i - 3;
        const x = LINE_X0 + origIdx * LINE_STEP + shift;
        const vis = x <= 0.50 ? 1 : 0;
        m.compose(p.set(x, LINE_Y, 0), q, s.setScalar(vis));
      }
      lineMesh.setMatrixAt(i, m);
    }
    lineMesh.instanceMatrix.needsUpdate = true;

    barePointer.position.set(LINE_X0 + 3.0 * LINE_STEP, LINE_Y + 0.036, 0.25);
    anchorLock.position.set(LINE_X0 + 3.0 * LINE_STEP + shift, LINE_Y + 0.095, 0);
  }

  site.layer('anchor_spans', anchorGroup, {
    box: [[4.55, 0.3, -0.4], [6.15, 1.15, 0.65]],
    update: updateAnchorSpans,
  });

  // -------------------------------------------------------------------------
  // 4. HOST LANES: 7 parallel calibrated lanes on the front instrument plate
  // -------------------------------------------------------------------------
  const HOSTS = [
    { name: 'CLAUDE', birth: 'amber', out: 'blue' },
    { name: 'CODEX', birth: 'amber', out: 'blue' },
    { name: 'ANTIGRAVITY', birth: 'blue', out: 'red' },
    { name: 'HERMES', birth: 'blue', out: 'blue' },
    { name: 'OH MY PI', birth: 'blue', out: 'blue' },
    { name: 'OPENCODE', birth: 'blue', out: 'blue' },
    { name: 'DEEPSEEK', birth: 'amber', out: 'blue' },
  ];

  const lanesRoot = new THREE.Group();
  lanesRoot.position.set(2.65, 0, 1.88);

  const plate = new THREE.Mesh(
    new THREE.BoxGeometry(4.4, 0.036, 1.62),
    new THREE.MeshStandardMaterial({ color: '#2B323C', roughness: 0.7, metalness: 0.35 }),
  );
  plate.position.set(0, 0.018, 0);
  plate.receiveShadow = true;
  plate.castShadow = true;
  lanesRoot.add(plate);

  const LANE_DX = 0.58;
  const LANE_X0 = -((HOSTS.length - 1) * LANE_DX) / 2;

  const railGeo = new THREE.BoxGeometry(0.42, 0.022, 1.32);
  const railMat = new THREE.MeshStandardMaterial({ color: '#464F5D', roughness: 0.52, metalness: 0.45 });
  const railMesh = new THREE.InstancedMesh(railGeo, railMat, HOSTS.length);
  railMesh.receiveShadow = true;
  for (let i = 0; i < HOSTS.length; i++) {
    m.compose(p.set(LANE_X0 + i * LANE_DX, 0.045, 0), q, s.set(1, 1, 1));
    railMesh.setMatrixAt(i, m);
  }
  railMesh.instanceMatrix.needsUpdate = true;
  lanesRoot.add(railMesh);

  const gateGeo = new THREE.BoxGeometry(0.36, 1.0, 0.14);
  const gateMat = new THREE.MeshBasicMaterial({ color: '#ffffff', toneMapped: false });
  const gateMesh = new THREE.InstancedMesh(gateGeo, gateMat, HOSTS.length * 2);

  const colorFor = (code) => (code === 'blue' ? cBlue : code === 'amber' ? cAmber : cRed);
  const heightFor = (code) => (code === 'blue' ? 0.24 : code === 'amber' ? 0.14 : 0.04);

  HOSTS.forEach((h, i) => {
    gateMesh.setColorAt(i * 2, colorFor(h.birth));
    gateMesh.setColorAt(i * 2 + 1, colorFor(h.out));
  });
  gateMesh.instanceColor.needsUpdate = true;
  lanesRoot.add(gateMesh);

  const pktGeo = new RoundedBoxGeometry(0.18, 0.045, 0.14, 2, 0.005);
  const pktMat = new THREE.MeshStandardMaterial({ color: '#DCE0E7', roughness: 0.35, metalness: 0.25 });
  const pktMesh = new THREE.InstancedMesh(pktGeo, pktMat, HOSTS.length);
  pktMesh.castShadow = true;
  lanesRoot.add(pktMesh);

  function updateHostLanes({ T }) {
    const activate = ramp(T, at('harnesses', 0.05), at('harnesses', 0.45));
    const glide = ramp(T, at('harnesses', 0.2), at('harnesses', 0.88));

    HOSTS.forEach((h, i) => {
      const lx = LANE_X0 + i * LANE_DX;
      const bh = heightFor(h.birth) * (0.35 + 0.65 * activate);
      const oh = heightFor(h.out) * (0.35 + 0.65 * activate);
      m.compose(p.set(lx, 0.056 + bh / 2, -0.34), q, s.set(1, bh, 1));
      gateMesh.setMatrixAt(i * 2, m);
      m.compose(p.set(lx, 0.056 + oh / 2, 0.34), q, s.set(1, oh, 1));
      gateMesh.setMatrixAt(i * 2 + 1, m);

      const pz = THREE.MathUtils.lerp(-0.54, 0.54, ramp(glide, i * 0.05, i * 0.05 + 0.65));
      m.compose(p.set(lx, 0.082, pz), q, s.set(1, 1, 1));
      pktMesh.setMatrixAt(i, m);
    });
    gateMesh.instanceMatrix.needsUpdate = true;
    pktMesh.instanceMatrix.needsUpdate = true;
  }

  site.layer('host_lanes', lanesRoot, {
    box: [[0.4, 0, 1.05], [4.9, 0.42, 2.7]],
    update: updateHostLanes,
  });

  site.label('birth-gate-label', birthTag, 'host_lanes');
  site.label('output-gate-label', outputTag, 'host_lanes');
}
