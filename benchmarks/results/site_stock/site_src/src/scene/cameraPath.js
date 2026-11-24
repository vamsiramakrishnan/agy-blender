import * as THREE from 'three';

/**
 * Cleanly separated spatial stages along a generous Z-corridor (24–28 units apart)
 * combined with per-stage visibility gating in EvidenceScene so adjacent acts
 * NEVER photobomb or clip into the active camera frame.
 *
 * Desktop asymmetric framing rule:
 *   - Act 0 (Hero):          Centered slightly right so left hero copy breathes
 *   - Act 1 (Left panel):    3D Flood & Cinch framed in right 56% of viewport
 *   - Act 2 (Right panel):   3D Four Gates & Vault framed in left 58% of viewport
 *   - Act 3 (Left panel):    3D Anchor Clamp framed in right 56% of viewport
 *   - Act 4 (Right panel):   3D Frozen Prefix & Symbols framed in left 58% of viewport
 *   - Act 5 (Left panel):    3D 7-Harness Constellation framed in right 58% of viewport
 *   - Act 6 (Finale):        Centered symmetrical monument behind receipts gateway
 */

export const STAGE_POS = {
  hero: new THREE.Vector3(0.0, 0.0, 0.0),
  flood: new THREE.Vector3(0.0, 0.0, -26.0),
  gates: new THREE.Vector3(0.0, 0.45, -52.0),
  vault: new THREE.Vector3(-2.65, -2.05, -51.6),
  anchor: new THREE.Vector3(0.0, 0.05, -78.0),
  prefix: new THREE.Vector3(0.0, 0.05, -104.0),
  constellation: new THREE.Vector3(0.0, -0.35, -132.0),
  finale: new THREE.Vector3(0.0, 0.1, -162.0),
};

// Exactly 7 act keyframes spaced at actIndex / 6 (0/6, 1/6, 2/6, 3/6, 4/6, 5/6, 6/6)
// Because App.jsx computes DOM-anchored progress (0..6 -> 0..1), each section
// locks squarely onto its matching keyframe on both desktop and phone screens.
const KEYFRAMES = [
  {
    // Act 01: Hero Straitjacket Cinch
    t: 0 / 6,
    pos: new THREE.Vector3(0.25, 0.18, 6.2),
    target: new THREE.Vector3(-0.78, -0.04, 0.0),
    mobilePos: new THREE.Vector3(0.0, 0.12, 7.6),
    mobileTarget: new THREE.Vector3(0.0, -0.18, 0.0),
    fov: 40,
    topColor: new THREE.Color('#06080d'),
    horizonColor: new THREE.Color('#101724'),
    accentColor: new THREE.Color('#f0b429'),
    accentStrength: 0.22,
  },
  {
    // Act 02: The 302,628-token Flood & Cinch Containment (panel on LEFT -> subject on RIGHT)
    t: 1 / 6,
    pos: new THREE.Vector3(-0.2, 0.55, -18.0),
    target: new THREE.Vector3(-1.55, 0.0, -26.0),
    mobilePos: new THREE.Vector3(0.3, 0.55, -17.2),
    mobileTarget: new THREE.Vector3(0.0, -1.15, -26.0),
    fov: 44,
    topColor: new THREE.Color('#0d0608'),
    horizonColor: new THREE.Color('#230f14'),
    accentColor: new THREE.Color('#f85149'),
    accentStrength: 0.30,
  },
  {
    // Act 03: Four Gates & Artifact Vault (panel on RIGHT -> subject on LEFT)
    t: 2 / 6,
    pos: new THREE.Vector3(1.15, 0.72, -43.2),
    target: new THREE.Vector3(1.75, -0.32, -52.0),
    mobilePos: new THREE.Vector3(0.55, 0.75, -41.5),
    mobileTarget: new THREE.Vector3(-0.3, -1.65, -52.0),
    fov: 43,
    topColor: new THREE.Color('#060911'),
    horizonColor: new THREE.Color('#0d1c30'),
    accentColor: new THREE.Color('#3b82f6'),
    accentStrength: 0.26,
  },
  {
    // Act 04: Content Anchor Caliper (panel on LEFT -> subject on RIGHT)
    t: 3 / 6,
    pos: new THREE.Vector3(-1.35, 0.22, -71.0),
    target: new THREE.Vector3(-1.35, 0.05, -78.0),
    mobilePos: new THREE.Vector3(-0.7, 0.35, -70.0),
    mobileTarget: new THREE.Vector3(0.0, -1.15, -78.0),
    fov: 41,
    topColor: new THREE.Color('#060b0a'),
    horizonColor: new THREE.Color('#0d1e1b'),
    accentColor: new THREE.Color('#3fb950'),
    accentStrength: 0.24,
  },
  {
    // Act 05: Frozen Prompt Prefix Beam & Symbol Navigation (panel on RIGHT -> subject on LEFT)
    t: 4 / 6,
    pos: new THREE.Vector3(1.35, 0.78, -96.4),
    target: new THREE.Vector3(1.55, 0.02, -104.0),
    mobilePos: new THREE.Vector3(0.4, 0.75, -94.8),
    mobileTarget: new THREE.Vector3(-0.2, -1.35, -104.0),
    fov: 42,
    topColor: new THREE.Color('#060912'),
    horizonColor: new THREE.Color('#0f1a2e'),
    accentColor: new THREE.Color('#58a6ff'),
    accentStrength: 0.26,
  },
  {
    // Act 06: Cross-Harness ACP & Task Ledger Constellation (panel on LEFT -> subject on RIGHT)
    t: 5 / 6,
    pos: new THREE.Vector3(-1.25, 4.35, -122.4),
    target: new THREE.Vector3(-1.75, -0.35, -132.0),
    mobilePos: new THREE.Vector3(0.0, 4.9, -121.0),
    mobileTarget: new THREE.Vector3(0.0, -1.85, -132.0),
    fov: 44,
    topColor: new THREE.Color('#080910'),
    horizonColor: new THREE.Color('#171522'),
    accentColor: new THREE.Color('#f0b429'),
    accentStrength: 0.26,
  },
  {
    // Act 07: Monumental Finale & Docs Gateway (centered behind receipts)
    t: 6 / 6,
    pos: new THREE.Vector3(0.0, 0.45, -154.2),
    target: new THREE.Vector3(0.0, 0.1, -162.0),
    mobilePos: new THREE.Vector3(0.0, 0.5, -152.5),
    mobileTarget: new THREE.Vector3(0.0, -0.6, -162.0),
    fov: 41,
    topColor: new THREE.Color('#06080d'),
    horizonColor: new THREE.Color('#111824'),
    accentColor: new THREE.Color('#f0b429'),
    accentStrength: 0.22,
  },
];

function smoothstep(edge0, edge1, x) {
  const t = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

const _pos = new THREE.Vector3();
const _target = new THREE.Vector3();
const _posA = new THREE.Vector3();
const _posB = new THREE.Vector3();
const _tgtA = new THREE.Vector3();
const _tgtB = new THREE.Vector3();
const _top = new THREE.Color();
const _horizon = new THREE.Color();
const _accent = new THREE.Color();

export function sampleCameraState(t, aspect = 1.6) {
  const clamped = Math.max(0, Math.min(1, t));
  const scaled = clamped * (KEYFRAMES.length - 1);
  const idx = Math.min(KEYFRAMES.length - 2, Math.floor(scaled));
  const local = smoothstep(0, 1, scaled - idx);

  const a = KEYFRAMES[idx];
  const b = KEYFRAMES[idx + 1];

  // Smooth blend between desktop framing and dedicated phone-portrait framing
  const mobileBlend = smoothstep(0.98, 0.52, aspect);

  _posA.lerpVectors(a.pos, a.mobilePos, mobileBlend);
  _posB.lerpVectors(b.pos, b.mobilePos, mobileBlend);
  _tgtA.lerpVectors(a.target, a.mobileTarget, mobileBlend);
  _tgtB.lerpVectors(b.target, b.mobileTarget, mobileBlend);

  _pos.lerpVectors(_posA, _posB, local);
  _target.lerpVectors(_tgtA, _tgtB, local);

  const fov = THREE.MathUtils.lerp(a.fov, b.fov, local) + mobileBlend * 7.5;

  _top.copy(a.topColor).lerp(b.topColor, local);
  _horizon.copy(a.horizonColor).lerp(b.horizonColor, local);
  _accent.copy(a.accentColor).lerp(b.accentColor, local);
  const accentStrength = THREE.MathUtils.lerp(a.accentStrength, b.accentStrength, local);

  return {
    position: _pos,
    target: _target,
    fov,
    topColor: _top,
    horizonColor: _horizon,
    accentColor: _accent,
    accentStrength,
  };
}
