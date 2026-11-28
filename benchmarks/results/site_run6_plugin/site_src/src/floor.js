// floor.js: calibrated Addressable Evidence bench floor using three-custom-shader-material.
// Follows all three shader rules: THREE.Color uniforms (divided by GROUND_LIGHT), fwidth()-smoothed lines,
// and palette state switching (never held blends).
import * as THREE from 'three';
import CustomShaderMaterial from 'three-custom-shader-material/vanilla';

export function createBenchFloor(scene, { groundLight, groundEnvironment, groundHex = '#15181D' }) {
  const scaled = (hex) => new THREE.Color(hex).multiplyScalar(1 / groundLight);

  const uniforms = {
    uGround: { value: scaled(groundHex) },
    uGridMinor: { value: scaled('#1D2229') },
    uGridMajor: { value: scaled('#2A303A') },
    uTrackBase: { value: scaled('#222730') },
    uBlue: { value: scaled('#6F96FF') },
    uAmber: { value: scaled('#F0B429') },
    uRed: { value: scaled('#FF7768') },
    uGreen: { value: scaled('#63D3A4') },
    uFloodActive: { value: 0.0 },
    uDigestActive: { value: 0.0 },
    uAnchorActive: { value: 0.0 },
    uHostActive: { value: 0.0 },
  };

  const material = new CustomShaderMaterial({
    baseMaterial: THREE.MeshStandardMaterial,
    vertexShader: /* glsl */ `
      varying vec3 vWorldPos;
      void main() {
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWorldPos = wp.xyz;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uGround;
      uniform vec3 uGridMinor;
      uniform vec3 uGridMajor;
      uniform vec3 uTrackBase;
      uniform vec3 uBlue;
      uniform vec3 uAmber;
      uniform vec3 uRed;
      uniform vec3 uGreen;
      uniform float uFloodActive;
      uniform float uDigestActive;
      uniform float uAnchorActive;
      uniform float uHostActive;
      varying vec3 vWorldPos;

      // Signed-distance anti-aliased grid line mask (1 on line, 0 off), smoothed over 1 px with fwidth.
      float gridMask(vec2 coord, float stepSize, float halfWidth) {
        vec2 p = coord / stepSize;
        vec2 g = abs(fract(p - 0.5) - 0.5) * stepSize;
        float d = min(g.x, g.y) - halfWidth;
        float w = max(fwidth(d), 0.0015);
        return 1.0 - smoothstep(-w, w, d);
      }

      // Signed distance to a 2D line segment a -> b.
      float sdSegment(vec2 p, vec2 a, vec2 b) {
        vec2 pa = p - a, ba = b - a;
        float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
        return length(pa - ba * h);
      }

      float lineMask(float dist, float halfWidth) {
        float d = dist - halfWidth;
        float w = max(fwidth(d), 0.002);
        return 1.0 - smoothstep(-w, w, d);
      }

      void main() {
        vec2 xz = vWorldPos.xz;
        float minor = gridMask(xz, 0.5, 0.005);
        float major = gridMask(xz, 2.0, 0.011);

        // Fade grid subtly with distance from bench center so horizon stays clean.
        float distFromBench = length(xz - vec2(-1.0, -1.2));
        float gridFade = 1.0 - smoothstep(7.0, 18.0, distFromBench);

        vec3 col = uGround;
        col = mix(col, uGridMinor, minor * 0.55 * gridFade);
        col = mix(col, uGridMajor, major * 0.85 * gridFade);

        // Calibrated bench tracks connecting the stations:
        // 1) Intake flood lane: (+3.8, 0.0) -> (0.0, 0.0)
        float dIntake = sdSegment(xz, vec2(3.8, 0.0), vec2(0.0, 0.0));
        float mIntake = lineMask(dIntake, 0.045);
        vec3 cIntake = mix(uTrackBase, uRed, uFloodActive);
        col = mix(col, cIntake, mIntake * 0.85);

        // 2) Digest exit rail: (0.0, 0.0) -> (-3.4, -0.5)
        float dExit = sdSegment(xz, vec2(0.0, 0.0), vec2(-3.4, -0.5));
        float mExit = lineMask(dExit, 0.04);
        vec3 cExit = mix(uTrackBase, uAmber, uDigestActive);
        col = mix(col, cExit, mExit * 0.9);

        // 3) Anchor verification bus: (-3.4, -0.5) -> (-1.7, -3.4)
        float dAnchor = sdSegment(xz, vec2(-3.4, -0.5), vec2(-1.7, -3.4));
        float mAnchor = lineMask(dAnchor, 0.035);
        vec3 cAnchor = mix(uTrackBase, uGreen, uAnchorActive);
        col = mix(col, cAnchor, mAnchor * 0.85);

        // 4) Host return bus: (-1.7, -3.4) -> (0.0, 0.0)
        float dHost = sdSegment(xz, vec2(-1.7, -3.4), vec2(0.0, 0.0));
        float mHost = lineMask(dHost, 0.035);
        vec3 cHost = mix(uTrackBase, uBlue, uHostActive);
        col = mix(col, cHost, mHost * 0.85);

        csm_DiffuseColor = vec4(col, 1.0);
      }
    `,
    uniforms,
    envMap: scene.environment,
    envMapIntensity: groundEnvironment,
    roughness: 0.95,
    metalness: 0.0,
  });

  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), material);
  mesh.rotation.x = -Math.PI / 2;
  mesh.receiveShadow = true;

  function update(T, at, ramp) {
    // Switch tracks crisply over short transitions so every resting shot holds pure palette states.
    const floodOn = ramp(T, at('intro', 0.85), at('flood', 0.25)) * (1 - ramp(T, at('digest', 0.05), at('digest', 0.35)));
    const digestOn = ramp(T, at('digest', 0.05), at('digest', 0.35));
    const anchorOn = ramp(T, at('anchors', 0.05), at('anchors', 0.35));
    const hostOn = ramp(T, at('hosts', 0.05), at('hosts', 0.35));
    uniforms.uFloodActive.value = floodOn;
    uniforms.uDigestActive.value = digestOn;
    uniforms.uAnchorActive.value = anchorOn;
    uniforms.uHostActive.value = hostOn;
  }

  return { mesh, material, update };
}
