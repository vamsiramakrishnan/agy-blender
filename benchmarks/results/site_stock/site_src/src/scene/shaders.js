import * as THREE from 'three';

/**
 * 1. Architectural Instrument Backdrop Dome Shader
 * Deep obsidian-slate sky sphere with subtle semantic color shifts per scroll chapter
 * and a whisper-fine precision coordinate lattice.
 */
export function createBackdropMaterial() {
  return new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      uTime: { value: 0 },
      uScroll: { value: 0 },
      uTopColor: { value: new THREE.Color('#07090d') },
      uHorizonColor: { value: new THREE.Color('#0f1622') },
      uAccentColor: { value: new THREE.Color('#f0b429') },
      uAccentStrength: { value: 0.16 },
    },
    vertexShader: /* glsl */ `
      varying vec3 vWorldDir;
      void main() {
        vec4 worldPos = modelMatrix * vec4(position, 1.0);
        vWorldDir = normalize(position);
        gl_Position = projectionMatrix * viewMatrix * worldPos;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform float uScroll;
      uniform vec3 uTopColor;
      uniform vec3 uHorizonColor;
      uniform vec3 uAccentColor;
      uniform float uAccentStrength;

      varying vec3 vWorldDir;

      float hash(vec2 p) {
        return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
      }

      void main() {
        vec3 dir = normalize(vWorldDir);
        float elev = clamp(dir.y * 0.5 + 0.5, 0.0, 1.0);
        float horizonBand = exp(-pow(dir.y * 3.1, 2.0));

        vec3 col = mix(uTopColor, uHorizonColor, horizonBand * 0.85 + (1.0 - elev) * 0.25);

        // Subtle off-center atmospheric studio key glow
        vec3 keyDir = normalize(vec3(0.45 * sin(uScroll * 4.2 + 0.4), 0.18, -0.82));
        float glow = pow(max(dot(dir, keyDir), 0.0), 4.5);
        col += uAccentColor * glow * uAccentStrength;

        // Secondary opposing cool rim haze
        vec3 rimDir = normalize(vec3(-0.65, 0.25, -0.55));
        float rim = pow(max(dot(dir, rimDir), 0.0), 6.0);
        col += vec3(0.14, 0.34, 0.68) * rim * 0.14;

        // Precision subtle meridian/elevation instrument grid near horizon
        float az = atan(dir.x, dir.z);
        float lat = asin(clamp(dir.y, -1.0, 1.0));
        float g1 = abs(fract(az * 7.6394) - 0.5);
        float g2 = abs(fract(lat * 11.459 + uScroll * 1.5) - 0.5);
        float line = smoothstep(0.492, 0.499, max(g1, g2));
        col += mix(vec3(0.18, 0.24, 0.34), uAccentColor, 0.25) * line * horizonBand * 0.11;

        // Micro dithering to eliminate banding on dark gradients
        float dither = (hash(gl_FragCoord.xy + fract(uTime)) - 0.5) * (1.2 / 255.0);
        col += dither;

        gl_FragColor = vec4(col, 1.0);
      }
    `,
  });
}

/**
 * 2. GPU Byte-Flood Particle Field
 * 4,200 instanced/point-sprite byte tokens that transition smoothly across acts:
 * - Act 1: Ambient telemetry dust around the Straitjacket Cinch
 * - Act 2: Violent 302,628-token crimson flood surging past the camera
 * - Act 3: Siphoned cleanly through the Four Gates down into the Artifact Vault
 * - Act 4-7: Calm orbital precision constellation
 */
export function createByteFloodSystem(count = 4200) {
  const geo = new THREE.BufferGeometry();
  const positions = new Float32Array(count * 3);
  const seeds = new Float32Array(count * 4); // x: phase, y: lane, z: speed, w: isNeedle/type

  for (let i = 0; i < count; i++) {
    // Base spherical / cylindrical distribution
    const u = Math.random();
    const v = Math.random();
    const theta = u * Math.PI * 2.0;
    const phi = Math.acos(2.0 * v - 1.0);
    const radius = 2.6 + Math.pow(Math.random(), 0.7) * 14.0;

    positions[i * 3 + 0] = radius * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = radius * Math.cos(phi) * 0.65;
    positions[i * 3 + 2] = radius * Math.sin(phi) * Math.sin(theta);

    seeds[i * 4 + 0] = Math.random(); // phase 0..1
    seeds[i * 4 + 1] = (Math.random() - 0.5) * 2.0; // vertical spread -1..1
    seeds[i * 4 + 2] = 0.55 + Math.random() * 1.15; // speed multiplier
    // index 0 is the Quiet Needle (line 14,238), next ~8% are structural/blue digest tokens
    seeds[i * 4 + 3] = i === 0 ? 2.0 : i < count * 0.08 ? 1.0 : 0.0;
  }

  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geo.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 4));

  const mat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uPixelRatio: { value: Math.min(window.devicePixelRatio || 1, 2) },
      uFloodIntensity: { value: 0.0 },    // Act 2 surging flood
      uFunnelIntensity: { value: 0.0 },   // Act 3 vault siphon
      uDigestMode: { value: 1.0 },        // 0=raw, 1=digest, 2=retrieve
      uOrigin: { value: new THREE.Vector3(0, 0, 0) },
      uVaultTarget: { value: new THREE.Vector3(-2.6, -2.2, -16.5) },
      uGateCenter: { value: new THREE.Vector3(0.4, 0.2, -16.5) },
      uCalmOpacity: { value: 0.45 },
    },
    vertexShader: /* glsl */ `
      attribute vec4 aSeed;
      uniform float uTime;
      uniform float uPixelRatio;
      uniform float uFloodIntensity;
      uniform float uFunnelIntensity;
      uniform float uDigestMode;
      uniform vec3 uOrigin;
      uniform vec3 uVaultTarget;
      uniform vec3 uGateCenter;
      uniform float uCalmOpacity;

      varying float vType;
      varying float vAlpha;
      varying float vHeat;

      void main() {
        float phase = aSeed.x;
        float lane = aSeed.y;
        float spd = aSeed.z;
        float kind = aSeed.w; // 0 = noise, 1 = digest-kept, 2 = quiet needle

        // 1) Calm ambient telemetry orbit
        float ang = uTime * 0.08 * spd + phase * 6.28318;
        vec3 basePos = position;
        float ca = cos(ang * 0.35);
        float sa = sin(ang * 0.35);
        vec3 calmPos = vec3(
          basePos.x * ca - basePos.z * sa,
          basePos.y + sin(uTime * 0.5 * spd + phase * 12.0) * 0.35,
          basePos.x * sa + basePos.z * ca
        ) + uOrigin;

        // 2) Act 2: Violent 302,628-token flood torrent rushing along X/Z corridor
        float flowT = fract(phase + uTime * 0.22 * spd);
        float spread = mix(4.8, 1.4, smoothstep(0.65, 1.0, flowT));
        float spiralA = phase * 25.13 + uTime * 1.4 * spd;
        vec3 floodPos = vec3(
          mix(-11.0, 9.5, flowT),
          lane * spread * 0.72 + sin(spiralA) * 0.55,
          -8.2 + cos(spiralA) * spread * 0.65 + (phase - 0.5) * 3.5
        );
        if (kind > 1.5) {
          // Quiet needle stays poised near the heart of the flood corridor
          floodPos = vec3(0.0 + sin(uTime * 1.2) * 0.25, 0.08 + cos(uTime * 1.6) * 0.12, -8.0);
        }

        // 3) Act 3: Four Gates & Artifact Vault Siphon
        float gateT = fract(phase + uTime * 0.28 * spd);
        vec3 funnelPos;
        if (kind > 0.5 || uDigestMode < 0.5) {
          // Kept structural tokens (or all tokens if user toggles Raw Flood!) pass through gates
          float rTube = (uDigestMode < 0.5) ? (0.45 + abs(lane) * 1.35) : (0.14 + abs(lane) * 0.26);
          float swirl = gateT * 10.0 + phase * 6.28318;
          funnelPos = uGateCenter + vec3(
            mix(-4.6, 4.1, gateT),
            sin(swirl) * rTube,
            cos(swirl) * rTube
          );
          if (kind > 1.5) {
            funnelPos = uGateCenter + vec3(3.35, 0.02 + sin(uTime * 2.0) * 0.06, 0.14);
          }
        } else {
          // 99.8% raw noise gets captured at Gate 1 and vortexed down into the Artifact Vault!
          float tDown = gateT;
          vec3 p0 = uGateCenter + vec3(-4.4, lane * 1.3, sin(phase * 12.0) * 1.2);
          vec3 p1 = uGateCenter + vec3(-2.15, lane * 0.45, cos(phase * 12.0) * 0.45);
          vec3 p2 = uVaultTarget + vec3(cos(tDown * 14.0 + phase * 6.28) * 0.85, mix(1.4, -1.2, tDown), sin(tDown * 14.0 + phase * 6.28) * 0.85);
          if (tDown < 0.42) {
            funnelPos = mix(p0, p1, smoothstep(0.0, 0.42, tDown));
          } else {
            funnelPos = mix(p1, p2, smoothstep(0.42, 1.0, tDown));
          }
        }

        vec3 finalPos = mix(calmPos, floodPos, uFloodIntensity);
        finalPos = mix(finalPos, funnelPos, uFunnelIntensity);

        vec4 mvPosition = modelViewMatrix * vec4(finalPos, 1.0);
        gl_Position = projectionMatrix * mvPosition;

        float baseSize = kind > 1.5 ? 46.0 : (kind > 0.5 ? 16.0 : 12.5);
        float modeBoost = mix(1.0, 1.38, uFloodIntensity);
        gl_PointSize = clamp((baseSize * modeBoost * uPixelRatio) / max(-mvPosition.z, 1.2), 1.8, 52.0);

        vType = kind;
        vHeat = clamp(uFloodIntensity * 1.1 + uFunnelIntensity * (1.0 - step(0.5, kind)) * 0.8, 0.0, 1.0);
        vAlpha = mix(uCalmOpacity, 0.88, max(uFloodIntensity, uFunnelIntensity));
        if (kind > 1.5) vAlpha = 1.0;
      }
    `,
    fragmentShader: /* glsl */ `
      varying float vType;
      varying float vAlpha;
      varying float vHeat;

      void main() {
        vec2 uv = gl_PointCoord - 0.5;
        float dist = length(uv);
        if (dist > 0.5) discard;

        // Crisp square-ish precision byte glyph for normal tokens, radiant diamond star for Quiet Needle
        float core;
        vec3 col;

        if (vType > 1.5) {
          float star = exp(-dist * 5.0) + 0.65 * exp(-abs(uv.x) * 24.0) * exp(-abs(uv.y) * 5.0)
                     + 0.65 * exp(-abs(uv.y) * 24.0) * exp(-abs(uv.x) * 5.0);
          core = clamp(star, 0.0, 1.0);
          col = vec3(1.0, 0.78, 0.24); // Luminous Amber Quiet Needle
        } else if (vType > 0.5) {
          // Bounded structural token (Semantic Blue)
          float box = smoothstep(0.44, 0.14, max(abs(uv.x) * 0.75, abs(uv.y) * 1.35));
          core = box * 0.95;
          col = vec3(0.32, 0.66, 1.0);
        } else {
          // Raw log byte sliver: transitions from muted steel dust -> warning crimson/orange flood
          float sliver = smoothstep(0.46, 0.12, max(abs(uv.x) * 0.55, abs(uv.y) * 1.8));
          core = sliver * 0.78;
          vec3 calmCol = vec3(0.46, 0.56, 0.70);
          vec3 floodCol = mix(vec3(0.96, 0.26, 0.22), vec3(0.98, 0.52, 0.16), 0.32);
          col = mix(calmCol, floodCol, vHeat);
        }

        gl_FragColor = vec4(col, core * vAlpha);
      }
    `,
  });

  const points = new THREE.Points(geo, mat);
  points.frustumCulled = false;
  return points;
}

/**
 * 3. Animated Evidence Conduit Ribbon Shader
 * Drives flowing directional address/retrieval light packets along curved TubeGeometry splines.
 */
export function createConduitMaterial(colorHex = '#F0B429', speed = 0.65, pulseCount = 2.0) {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uColor: { value: new THREE.Color(colorHex) },
      uSpeed: { value: speed },
      uPulses: { value: pulseCount },
      uOpacity: { value: 0.9 },
    },
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform vec3 uColor;
      uniform float uSpeed;
      uniform float uPulses;
      uniform float uOpacity;
      varying vec2 vUv;

      void main() {
        float t = fract(vUv.x * uPulses - uTime * uSpeed);
        float head = smoothstep(0.70, 0.98, t) * (1.0 - smoothstep(0.98, 1.0, t));
        float tail = smoothstep(0.20, 0.95, t) * 0.42;
        float baseRail = 0.14;

        // Soft fade at spline endpoints
        float ends = smoothstep(0.0, 0.08, vUv.x) * smoothstep(1.0, 0.92, vUv.x);
        float radial = 1.0 - abs(vUv.y - 0.5) * 1.85;
        float intensity = (baseRail + tail + head * 1.45) * ends * clamp(radial, 0.25, 1.0);

        vec3 col = uColor * (1.0 + head * 1.25);
        gl_FragColor = vec4(col, intensity * uOpacity);
      }
    `,
  });
}

/**
 * 4. Holographic Precision Glass / Scanline Shader
 * Used for the Frozen Prompt Prefix Beam, Anchor Verification Plane, and Digest Halo.
 */
export function createHoloscanMaterial(colorHex = '#58A6FF', opacity = 0.36) {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uColor: { value: new THREE.Color(colorHex) },
      uOpacity: { value: opacity },
      uScanSpeed: { value: 1.2 },
    },
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      varying vec3 vNormal;
      varying vec3 vViewDir;
      void main() {
        vUv = uv;
        vec4 mvPos = modelViewMatrix * vec4(position, 1.0);
        vNormal = normalize(normalMatrix * normal);
        vViewDir = normalize(-mvPos.xyz);
        gl_Position = projectionMatrix * mvPos;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform vec3 uColor;
      uniform float uOpacity;
      uniform float uScanSpeed;
      varying vec2 vUv;
      varying vec3 vNormal;
      varying vec3 vViewDir;

      void main() {
        float fresnel = pow(1.0 - abs(dot(normalize(vNormal), normalize(vViewDir))), 2.2);
        float scan = 0.5 + 0.5 * sin(vUv.y * 90.0 - uTime * 3.5 * uScanSpeed);
        float band = smoothstep(0.0, 0.12, abs(fract(vUv.x - uTime * 0.22 * uScanSpeed) - 0.5));
        float pulse = 1.0 - band;
        float edge = smoothstep(0.0, 0.08, vUv.x) * smoothstep(1.0, 0.92, vUv.x)
                   * smoothstep(0.0, 0.08, vUv.y) * smoothstep(1.0, 0.92, vUv.y);

        float alpha = (0.18 + fresnel * 0.65 + pulse * 0.35 + scan * 0.08) * uOpacity * (0.4 + 0.6 * edge);
        vec3 col = uColor * (1.0 + fresnel * 0.7 + pulse * 0.6);
        gl_FragColor = vec4(col, clamp(alpha, 0.0, 0.92));
      }
    `,
  });
}
