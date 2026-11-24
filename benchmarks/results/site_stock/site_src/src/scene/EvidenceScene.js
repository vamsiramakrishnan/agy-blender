import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import {
  BloomEffect,
  EffectComposer,
  EffectPass,
  RenderPass,
  ToneMappingEffect,
  ToneMappingMode,
  VignetteEffect,
} from 'postprocessing';
import {
  createBackdropMaterial,
  createByteFloodSystem,
  createConduitMaterial,
  createHoloscanMaterial,
} from './shaders.js';
import { createStudioEnvironment } from './environment.js';
import { STAGE_POS, sampleCameraState } from './cameraPath.js';
import { HARNESSES } from '../data/straitjacketData.js';

function smoothstep(e0, e1, x) {
  const t = Math.max(0, Math.min(1, (x - e0) / (e1 - e0)));
  return t * t * (3 - 2 * t);
}

function actProximity(actFloat, targetAct, radius = 0.92) {
  const d = Math.abs(actFloat - targetAct);
  return 1.0 - smoothstep(radius * 0.45, radius, d);
}

export class EvidenceScene {
  constructor(canvas, callbacks = {}) {
    this.canvas = canvas;
    this.callbacks = callbacks;
    this.disposed = false;

    this.scrollTarget = 0;
    this.scrollSmooth = 0;
    this.pointer = new THREE.Vector2(0, 0);
    this.pointerSmooth = new THREE.Vector2(0, 0);
    this.digestMode = 'digest';
    this.anchorScenario = 'insert';
    this.anchorScenarioUserOverride = false;
    this.selectedHarnessIdx = 0;

    this.conduitMaterials = [];
    this.holoMaterials = [];
    this.workerPods = [];
    this.relayArcs = [];
    this.signalCapsules = [];
    this.lineSlabs = [];
    this.vaultPlatters = [];
    this.gateIrises = [];
    this.symbolOrbs = [];
    this.stageGroups = [];
    this.clock = new THREE.Clock();
    this.elapsed = 0;

    this.caliperY = 0.75;
    this.slabOffset = 0;

    this._initRenderer();
    this._initScene();
    this._initLights();
    this._initPostProcessing();
    this._initStageGroups();
    this._initProceduralStages();
    this._bindEvents();
    this._loadModels();
    this._animate();
  }

  _initRenderer() {
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas,
      antialias: true,
      powerPreference: 'high-performance',
      alpha: false,
    });
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.renderer.setPixelRatio(this.dpr);
    this.renderer.setSize(window.innerWidth, window.innerHeight, false);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.NoToneMapping;
    this.renderer.shadowMap.enabled = false;
  }

  _initScene() {
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#07090d');
    this.scene.fog = new THREE.FogExp2('#080b12', 0.036);

    this.aspect = window.innerWidth / Math.max(window.innerHeight, 1);
    this.camera = new THREE.PerspectiveCamera(40, this.aspect, 0.1, 160);
    this.camera.position.set(0.25, 0.18, 6.2);
    this.lookAtTarget = new THREE.Vector3(-0.78, -0.04, 0);

    this.envTexture = createStudioEnvironment(this.renderer);
    this.scene.environment = this.envTexture;
    this.scene.environmentIntensity = 1.15;

    this.backdropMat = createBackdropMaterial();
    this.backdropDome = new THREE.Mesh(
      new THREE.SphereGeometry(110, 32, 24),
      this.backdropMat
    );
    this.scene.add(this.backdropDome);

    this.byteFlood = createByteFloodSystem(4200);
    this.byteFlood.material.uniforms.uGateCenter.value.copy(STAGE_POS.gates);
    this.byteFlood.material.uniforms.uVaultTarget.value.copy(STAGE_POS.vault);
    this.scene.add(this.byteFlood);

    this._buildCorridorGuideRails();
  }

  _buildCorridorGuideRails() {
    const railGroup = new THREE.Group();
    const mat = new THREE.LineBasicMaterial({
      color: '#1b2638',
      transparent: true,
      opacity: 0.34,
    });
    for (const x of [-5.2, 5.2]) {
      const pts = [new THREE.Vector3(x, -3.8, 8), new THREE.Vector3(x, -3.8, -172)];
      const geo = new THREE.BufferGeometry().setFromPoints(pts);
      railGroup.add(new THREE.Line(geo, mat));
    }
    const tickPts = [];
    for (let z = 6; z >= -170; z -= 6) {
      tickPts.push(new THREE.Vector3(-5.2, -3.8, z), new THREE.Vector3(-4.65, -3.8, z));
      tickPts.push(new THREE.Vector3(4.65, -3.8, z), new THREE.Vector3(5.2, -3.8, z));
    }
    railGroup.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(tickPts), mat));
    this.scene.add(railGroup);
  }

  _initLights() {
    this.ambientLight = new THREE.AmbientLight('#8fb2dc', 0.44);
    this.scene.add(this.ambientLight);

    this.keyLight = new THREE.DirectionalLight('#fff2dc', 2.25);
    this.keyLight.position.set(6, 9, 8);
    this.scene.add(this.keyLight);

    this.rimLight = new THREE.DirectionalLight('#58a6ff', 1.65);
    this.rimLight.position.set(-8, 5, -6);
    this.scene.add(this.rimLight);

    this.stageLight = new THREE.PointLight('#f0b429', 4.8, 26, 1.4);
    this.scene.add(this.stageLight);

    this.fillLight = new THREE.PointLight('#3b82f6', 3.2, 28, 1.5);
    this.scene.add(this.fillLight);
  }

  _initPostProcessing() {
    this.usePostProcessing = true;
    try {
      this.composer = new EffectComposer(this.renderer, {
        frameBufferType: THREE.HalfFloatType,
        multisampling: 2,
      });
      this.composer.addPass(new RenderPass(this.scene, this.camera));

      this.bloomEffect = new BloomEffect({
        mipmapBlur: true,
        luminanceThreshold: 0.74,
        luminanceSmoothing: 0.25,
        intensity: 0.56,
      });
      this.vignetteEffect = new VignetteEffect({
        eskil: false,
        offset: 0.28,
        darkness: 0.50,
      });
      this.toneMappingEffect = new ToneMappingEffect({
        mode: ToneMappingMode.ACES_FILMIC,
      });

      this.composer.addPass(
        new EffectPass(
          this.camera,
          this.bloomEffect,
          this.vignetteEffect,
          this.toneMappingEffect
        )
      );
    } catch (err) {
      console.warn('[EvidenceScene] Postprocessing fallback to direct render:', err);
      this.usePostProcessing = false;
      this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
      this.renderer.toneMappingExposure = 1.08;
    }
  }

  _initStageGroups() {
    // Dedicated stage container for each of the 7 acts for clean visibility gating
    for (let i = 0; i < 7; i++) {
      const grp = new THREE.Group();
      this.scene.add(grp);
      this.stageGroups.push(grp);
    }
  }

  _initProceduralStages() {
    // --- Stage 2 (Act 03): Capture & Retrieval Conduits (Gates <-> Vault) ---
    const gPos = STAGE_POS.gates;
    const vPos = STAGE_POS.vault;
    const captureCurve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(gPos.x - 1.95, gPos.y - 0.1, gPos.z),
      new THREE.Vector3(gPos.x - 2.42, gPos.y - 0.72, gPos.z + 0.2),
      new THREE.Vector3(vPos.x, vPos.y + 1.25, vPos.z),
    ]);
    this.captureConduitMat = createConduitMaterial('#f85149', 0.9, 2.0);
    this.conduitMaterials.push(this.captureConduitMat);
    this.stageGroups[2].add(
      new THREE.Mesh(new THREE.TubeGeometry(captureCurve, 40, 0.052, 10, false), this.captureConduitMat)
    );

    const retrieveCurve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(vPos.x + 0.55, vPos.y + 1.05, vPos.z + 0.15),
      new THREE.Vector3(gPos.x + 0.25, vPos.y + 0.15, gPos.z + 0.7),
      new THREE.Vector3(gPos.x + 2.55, gPos.y - 0.95, gPos.z + 0.35),
      new THREE.Vector3(gPos.x + 3.15, gPos.y - 0.05, gPos.z),
    ]);
    this.retrieveConduitMat = createConduitMaterial('#f0b429', 0.74, 2.5);
    this.conduitMaterials.push(this.retrieveConduitMat);
    this.stageGroups[2].add(
      new THREE.Mesh(new THREE.TubeGeometry(retrieveCurve, 56, 0.042, 10, false), this.retrieveConduitMat)
    );

    // --- Stage 4 (Act 05): Frozen Prompt Prefix Beam & Symbol Navigation ---
    this.prefixGroup = new THREE.Group();
    this.prefixGroup.position.copy(STAGE_POS.prefix);
    this.stageGroups[4].add(this.prefixGroup);

    const prefixHolomat = createHoloscanMaterial('#58a6ff', 0.54);
    this.holoMaterials.push(prefixHolomat);

    const steelMat = new THREE.MeshStandardMaterial({
      color: '#a8b6c7',
      metalness: 0.92,
      roughness: 0.24,
    });
    const darkMat = new THREE.MeshStandardMaterial({
      color: '#11161f',
      metalness: 0.9,
      roughness: 0.35,
    });
    const amberGlowMat = new THREE.MeshStandardMaterial({
      color: '#f0b429',
      emissive: '#f0b429',
      emissiveIntensity: 2.0,
      roughness: 0.2,
    });

    const prefixSlab = new THREE.Mesh(new THREE.BoxGeometry(3.5, 0.88, 0.42), prefixHolomat);
    prefixSlab.position.set(-1.35, 0, 0);
    this.prefixGroup.add(prefixSlab);

    for (let i = 0; i < 6; i++) {
      const bar = new THREE.Mesh(
        new THREE.BoxGeometry(0.42, 0.56, 0.14),
        new THREE.MeshStandardMaterial({
          color: '#1d4ed8',
          emissive: '#3b82f6',
          emissiveIntensity: 1.0,
          metalness: 0.5,
          roughness: 0.22,
        })
      );
      bar.position.set(-2.72 + i * 0.54, 0, 0);
      this.prefixGroup.add(bar);
    }

    for (const y of [-0.54, 0.54]) {
      const rail = new THREE.Mesh(new THREE.BoxGeometry(3.75, 0.08, 0.54), steelMat);
      rail.position.set(-1.35, y, 0);
      this.prefixGroup.add(rail);
    }

    const dividerRing = new THREE.Mesh(new THREE.TorusGeometry(0.72, 0.045, 14, 36), amberGlowMat);
    dividerRing.rotation.y = Math.PI / 2;
    dividerRing.position.set(0.68, 0, 0);
    this.prefixGroup.add(dividerRing);

    const suffixCurve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0.7, 0.0, 0.0),
      new THREE.Vector3(1.65, 0.25, 0.35),
      new THREE.Vector3(2.85, -0.1, -0.15),
    ]);
    const suffixConduitMat = createConduitMaterial('#f0b429', 0.85, 2.0);
    this.conduitMaterials.push(suffixConduitMat);
    this.prefixGroup.add(
      new THREE.Mesh(new THREE.TubeGeometry(suffixCurve, 36, 0.036, 10, false), suffixConduitMat)
    );

    const orbPositions = [
      [1.75, 0.72, 0.35],
      [2.45, -0.58, 0.42],
      [2.85, 0.18, -0.45],
      [1.45, -0.68, -0.48],
    ];
    orbPositions.forEach((p, idx) => {
      const grp = new THREE.Group();
      grp.position.set(p[0], p[1], p[2]);
      const shell = new THREE.Mesh(new THREE.OctahedronGeometry(0.22, 0), darkMat);
      const core = new THREE.Mesh(
        new THREE.OctahedronGeometry(0.11, 0),
        idx % 2 === 0 ? amberGlowMat : prefixHolomat
      );
      grp.add(shell, core);
      grp.userData.basePos = new THREE.Vector3(...p);
      grp.userData.phase = idx * 1.6;
      this.prefixGroup.add(grp);
      this.symbolOrbs.push(grp);

      const cCurve = new THREE.CatmullRomCurve3([
        new THREE.Vector3(0.72, 0, 0),
        new THREE.Vector3(p[0], p[1], p[2]),
      ]);
      const cMat = createConduitMaterial(idx % 2 === 0 ? '#f0b429' : '#58a6ff', 0.55 + idx * 0.12, 1.5);
      this.conduitMaterials.push(cMat);
      this.prefixGroup.add(new THREE.Mesh(new THREE.TubeGeometry(cCurve, 20, 0.016, 6, false), cMat));
    });

    // --- Stage 6 (Act 07): Monumental Finale Halo ---
    this.finaleGroup = new THREE.Group();
    this.finaleGroup.position.copy(STAGE_POS.finale);
    this.stageGroups[6].add(this.finaleGroup);

    const haloMat = createHoloscanMaterial('#f0b429', 0.32);
    this.holoMaterials.push(haloMat);
    this.finaleHalo = new THREE.Mesh(new THREE.TorusGeometry(2.65, 0.024, 16, 84), haloMat);
    this.finaleGroup.add(this.finaleHalo);

    this.finaleHaloOuter = new THREE.Mesh(
      new THREE.TorusGeometry(3.25, 0.014, 12, 96),
      new THREE.MeshBasicMaterial({ color: '#3b82f6', transparent: true, opacity: 0.28 })
    );
    this.finaleHaloOuter.rotation.x = Math.PI * 0.14;
    this.finaleGroup.add(this.finaleHaloOuter);
  }

  _loadModels() {
    const loader = new GLTFLoader();
    const manifest = [
      { key: 'cinch', url: './models/straitjacket_cinch.glb' },
      { key: 'vault', url: './models/artifact_vault.glb' },
      { key: 'prism', url: './models/evidence_prism.glb' },
      { key: 'anchor', url: './models/anchor_clamp.glb' },
      { key: 'harnesses', url: './models/harness_nodes.glb' },
    ];

    let loadedCount = 0;
    const assets = {};

    const onOneDone = () => {
      loadedCount += 1;
      if (this.callbacks.onProgress) {
        this.callbacks.onProgress(loadedCount / manifest.length);
      }
      if (loadedCount === manifest.length) {
        this._mountLoadedModels(assets);
        if (this.callbacks.onReady) this.callbacks.onReady();
      }
    };

    manifest.forEach(({ key, url }) => {
      loader.load(
        url,
        (gltf) => {
          assets[key] = gltf.scene;
          onOneDone();
        },
        undefined,
        (err) => {
          console.error(`[EvidenceScene] Failed to load ${url}:`, err);
          onOneDone();
        }
      );
    });
  }

  _mountLoadedModels(assets) {
    // Stage 0 (Act 01): Hero Cinch | Stage 1 (Act 02): Flood Cinch | Stage 6 (Act 07): Finale Cinch
    if (assets.cinch) {
      this.heroCinch = assets.cinch;
      this.heroCinch.position.copy(STAGE_POS.hero);
      this.heroCinch.scale.setScalar(0.92);
      this.stageGroups[0].add(this.heroCinch);

      this.heroParts = {
        left: this.heroCinch.getObjectByName('BracketLeft'),
        right: this.heroCinch.getObjectByName('BracketRight'),
        strapOver: this.heroCinch.getObjectByName('StrapOver'),
        strapUnder: this.heroCinch.getObjectByName('StrapUnder'),
        reticle: this.heroCinch.getObjectByName('CoreReticle'),
      };

      this.floodCinch = assets.cinch.clone(true);
      this.floodCinch.position.copy(STAGE_POS.flood);
      this.floodCinch.scale.setScalar(0.96);
      this.stageGroups[1].add(this.floodCinch);
      this.floodParts = {
        left: this.floodCinch.getObjectByName('BracketLeft'),
        right: this.floodCinch.getObjectByName('BracketRight'),
        strapOver: this.floodCinch.getObjectByName('StrapOver'),
        strapUnder: this.floodCinch.getObjectByName('StrapUnder'),
        reticle: this.floodCinch.getObjectByName('CoreReticle'),
      };

      this.finaleCinch = assets.cinch.clone(true);
      this.finaleCinch.scale.setScalar(0.90);
      this.finaleGroup.add(this.finaleCinch);
    }

    // Stage 2 (Act 03): Artifact Vault + Evidence Prism (Four Gates + Digest Slab)
    if (assets.vault) {
      this.vaultModel = assets.vault;
      this.vaultModel.position.copy(STAGE_POS.vault);
      this.vaultModel.scale.setScalar(0.84);
      this.stageGroups[2].add(this.vaultModel);

      for (let i = 0; i < 6; i++) {
        const platter = this.vaultModel.getObjectByName(`VaultPlatter_${i}`);
        if (platter) this.vaultPlatters.push(platter);
      }
      this.vaultSpine = this.vaultModel.getObjectByName('VaultCoreSpine');
    }

    if (assets.prism) {
      this.prismModel = assets.prism;
      this.prismModel.position.copy(STAGE_POS.gates);
      this.prismModel.scale.setScalar(0.92);
      this.stageGroups[2].add(this.prismModel);

      for (const gname of ['Gate_Birth', 'Gate_Entry', 'Gate_Residence', 'Gate_Emission']) {
        const iris = this.prismModel.getObjectByName(`${gname}_Iris`);
        if (iris) this.gateIrises.push(iris);
      }
      this.digestSlab = this.prismModel.getObjectByName('DigestSlab');
      this.needleCrystal = this.prismModel.getObjectByName('NeedleCrystal');
    }

    // Stage 3 (Act 04): Content Anchor Clamp
    if (assets.anchor) {
      this.anchorModel = assets.anchor;
      this.anchorModel.position.copy(STAGE_POS.anchor);
      this.anchorModel.scale.setScalar(0.86);
      this.stageGroups[3].add(this.anchorModel);

      this.anchorCaliper = this.anchorModel.getObjectByName('AnchorCaliper');
      this.unanchoredPtr = this.anchorModel.getObjectByName('UnanchoredPointer');
      this.hashLensRing = this.anchorModel.getObjectByName('HashLensRing');

      if (this.hashLensRing && this.hashLensRing.material) {
        this.hashLensRing.material = this.hashLensRing.material.clone();
      }
      const ptrNeedle = this.anchorModel.getObjectByName('StaticPtrNeedle');
      if (ptrNeedle && ptrNeedle.material) {
        ptrNeedle.material = ptrNeedle.material.clone();
        this.staticPtrNeedle = ptrNeedle;
      }

      for (let i = 0; i < 14; i++) {
        const key = `LineSlab_${String(i).padStart(2, '0')}`;
        const slab = this.anchorModel.getObjectByName(key);
        if (slab) {
          slab.userData.baseY = slab.position.y;
          slab.userData.idx = i;
          if (slab.material) slab.material = slab.material.clone();
          this.lineSlabs.push(slab);
        }
      }
    }

    // Stage 5 (Act 06): Cross-Harness ACP & Task Ledger Constellation
    if (assets.harnesses) {
      this.constellationRoot = new THREE.Group();
      this.constellationRoot.position.copy(STAGE_POS.constellation);
      this.stageGroups[5].add(this.constellationRoot);

      this.ledgerDisc = assets.harnesses.getObjectByName('LedgerDisc');
      this.stewardSpire = assets.harnesses.getObjectByName('StewardSpire');
      const podTemplate = assets.harnesses.getObjectByName('WorkerPodTemplate');
      const capsuleTemplate = assets.harnesses.getObjectByName('SignalCapsuleTemplate');

      if (this.ledgerDisc) this.constellationRoot.add(this.ledgerDisc);
      if (this.stewardSpire) this.constellationRoot.add(this.stewardSpire);

      const orbitR = 3.25;
      if (podTemplate) {
        HARNESSES.forEach((h, idx) => {
          const pod = podTemplate.clone(true);
          const angle = (idx / HARNESSES.length) * Math.PI * 2.0 - Math.PI * 0.5;
          const x = Math.cos(angle) * orbitR;
          const z = Math.sin(angle) * orbitR;
          const y = Math.sin(idx * 1.7) * 0.25 + 0.35;
          pod.position.set(x, y, z);
          pod.rotation.y = -angle + Math.PI / 2;
          pod.userData = { harnessIdx: idx, basePos: new THREE.Vector3(x, y, z), angle };

          const accent = pod.getObjectByName('PodAccentRing');
          if (accent && accent.material) {
            accent.material = accent.material.clone();
            accent.material.color.set(h.color);
            accent.material.emissive.set(h.color);
            accent.material.emissiveIntensity = 1.6;
            pod.userData.accentMesh = accent;
          }

          pod.traverse((child) => {
            if (child.isMesh) child.userData.harnessIdx = idx;
          });

          this.constellationRoot.add(pod);
          this.workerPods.push(pod);

          const nextIdx = (idx + 1) % HARNESSES.length;
          const nextAngle = (nextIdx / HARNESSES.length) * Math.PI * 2.0 - Math.PI * 0.5;
          const nx = Math.cos(nextAngle) * orbitR;
          const nz = Math.sin(nextAngle) * orbitR;
          const ny = Math.sin(nextIdx * 1.7) * 0.25 + 0.35;

          const mid = new THREE.Vector3((x + nx) * 0.5, Math.max(y, ny) + 0.85, (z + nz) * 0.5);
          const arcCurve = new THREE.CatmullRomCurve3([
            new THREE.Vector3(x, y + 0.15, z),
            mid,
            new THREE.Vector3(nx, ny + 0.15, nz),
          ]);
          const arcMat = createConduitMaterial(idx % 2 === 0 ? '#f0b429' : '#58a6ff', 0.42 + idx * 0.04, 1.0);
          this.conduitMaterials.push(arcMat);
          this.constellationRoot.add(
            new THREE.Mesh(new THREE.TubeGeometry(arcCurve, 32, 0.022, 8, false), arcMat)
          );
          this.relayArcs.push({ curve: arcCurve, mat: arcMat, from: idx, to: nextIdx });

          const spokeCurve = new THREE.CatmullRomCurve3([
            new THREE.Vector3(x, y - 0.15, z),
            new THREE.Vector3(x * 0.55, 0.05, z * 0.55),
            new THREE.Vector3(Math.cos(angle) * 1.5, 0.08, Math.sin(angle) * 1.5),
          ]);
          const spokeMat = createConduitMaterial(h.color, 0.35, 1.0);
          spokeMat.uniforms.uOpacity.value = 0.52;
          this.conduitMaterials.push(spokeMat);
          this.constellationRoot.add(
            new THREE.Mesh(new THREE.TubeGeometry(spokeCurve, 20, 0.015, 6, false), spokeMat)
          );
        });
      }

      if (capsuleTemplate && this.relayArcs.length > 0) {
        for (let i = 0; i < 4; i++) {
          const cap = capsuleTemplate.clone(true);
          cap.scale.setScalar(1.15);
          cap.userData = { arcIdx: (i * 2) % this.relayArcs.length, offset: i * 0.25 };
          this.constellationRoot.add(cap);
          this.signalCapsules.push(cap);
        }
      }
    }
  }

  _bindEvents() {
    this.raycaster = new THREE.Raycaster();

    this._onResize = () => {
      if (this.disposed) return;
      const w = window.innerWidth;
      const h = Math.max(window.innerHeight, 1);
      this.aspect = w / h;
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.camera.aspect = this.aspect;
      this.camera.updateProjectionMatrix();
      this.renderer.setPixelRatio(this.dpr);
      this.renderer.setSize(w, h, false);
      if (this.usePostProcessing && this.composer) {
        this.composer.setSize(w, h);
      }
      if (this.byteFlood) {
        this.byteFlood.material.uniforms.uPixelRatio.value = this.dpr;
      }
    };

    this._onPointerMove = (e) => {
      const w = Math.max(window.innerWidth, 1);
      const h = Math.max(window.innerHeight, 1);
      this.pointer.x = (e.clientX / w) * 2 - 1;
      this.pointer.y = -(e.clientY / h) * 2 + 1;
    };

    this._onClick = (e) => {
      if (this.workerPods.length === 0) return;
      const actFloat = this.scrollSmooth * 6.0;
      if (actFloat < 4.3 || actFloat > 5.7) return;
      const rect = this.canvas.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1
      );
      this.raycaster.setFromCamera(ndc, this.camera);
      const hits = this.raycaster.intersectObjects(this.workerPods, true);
      if (hits.length > 0 && hits[0].object.userData.harnessIdx !== undefined) {
        const idx = hits[0].object.userData.harnessIdx;
        this.setSelectedHarness(idx);
        if (this.callbacks.onHarnessSelect) this.callbacks.onHarnessSelect(idx);
      }
    };

    window.addEventListener('resize', this._onResize);
    window.addEventListener('pointermove', this._onPointerMove, { passive: true });
    this.canvas.addEventListener('click', this._onClick);
  }

  setScrollProgress(p) {
    this.scrollTarget = Math.max(0, Math.min(1, p));
  }

  setDigestMode(mode) {
    this.digestMode = mode;
  }

  setAnchorScenario(scenario, userInitiated = false) {
    this.anchorScenario = scenario;
    if (userInitiated) this.anchorScenarioUserOverride = true;
  }

  setSelectedHarness(idx) {
    this.selectedHarnessIdx = idx;
  }

  _updateStages(t, dt) {
    const time = this.elapsed;
    const actFloat = t * 6.0; // Continuous act index 0.0 .. 6.0

    // Clean per-stage visibility gating: only active stage (and immediate transition neighbor)
    // is visible — eliminates all foreground/background stage photobombing!
    for (let i = 0; i < this.stageGroups.length; i++) {
      const dist = Math.abs(actFloat - i);
      this.stageGroups[i].visible = dist < 0.66;
    }

    // Shared shader uniforms
    this.backdropMat.uniforms.uTime.value = time;
    this.backdropMat.uniforms.uScroll.value = t;
    for (const m of this.conduitMaterials) m.uniforms.uTime.value = time;
    for (const m of this.holoMaterials) m.uniforms.uTime.value = time;

    // Byte-flood GPU particle transitions driven by exact act proximity
    const floodWeight = actProximity(actFloat, 1.0, 0.85);
    const funnelWeight = actProximity(actFloat, 2.0, 0.85);
    const floodU = this.byteFlood.material.uniforms;
    floodU.uTime.value = time;
    floodU.uFloodIntensity.value = floodWeight;
    floodU.uFunnelIntensity.value = funnelWeight;
    floodU.uDigestMode.value =
      this.digestMode === 'raw' ? 0.0 : this.digestMode === 'retrieve' ? 2.0 : 1.0;
    // Anchor flood corridor around STAGE_POS.flood (z = -26) when in Act 2
    floodU.uOrigin.value.set(0, 0, this.camera.position.z - 8.0);
    // Keep floodPos centered at STAGE_POS.flood.z (-26) via particle group z offset
    this.byteFlood.position.z = floodWeight * (STAGE_POS.flood.z + 8.2);

    // --- ACT 1: Hero Straitjacket Cinch ---
    if (this.heroCinch && this.heroParts && this.stageGroups[0].visible) {
      const pull = smoothstep(0.0, 0.65, actFloat);
      const mobileScale = this.aspect < 0.72 ? 0.68 : 0.92;
      this.heroCinch.scale.setScalar(mobileScale);
      this.heroCinch.rotation.y = Math.sin(time * 0.45) * 0.16 + this.pointerSmooth.x * 0.22 - pull * 0.45;
      this.heroCinch.rotation.x = Math.cos(time * 0.35) * 0.06 - this.pointerSmooth.y * 0.12;
      this.heroCinch.position.y = STAGE_POS.hero.y + Math.sin(time * 0.9) * 0.06;

      const spread = (1.0 - pull) * 0.12 + Math.sin(time * 1.1) * 0.03;
      if (this.heroParts.left) this.heroParts.left.position.x = -spread;
      if (this.heroParts.right) this.heroParts.right.position.x = spread;
      if (this.heroParts.reticle) this.heroParts.reticle.rotation.z = time * 0.32;
    }

    // --- ACT 2: Flood Containment Cinch ---
    if (this.floodCinch && this.floodParts && this.stageGroups[1].visible) {
      const clampPhase = smoothstep(0.55, 1.35, actFloat);
      const openAmt = (1.0 - clampPhase) * 1.25 + 0.12;
      if (this.floodParts.left) this.floodParts.left.position.x = -openAmt;
      if (this.floodParts.right) this.floodParts.right.position.x = openAmt;
      const strapScale = 0.72 + clampPhase * 0.28;
      if (this.floodParts.strapOver) this.floodParts.strapOver.scale.set(strapScale, strapScale, 1);
      if (this.floodParts.strapUnder) this.floodParts.strapUnder.scale.set(strapScale, strapScale, 1);
      this.floodCinch.rotation.y = -0.32 + clampPhase * 0.26 + Math.sin(time * 0.55) * 0.06;
    }

    // --- ACT 3: Four Gates & Artifact Vault ---
    if (this.stageGroups[2].visible) {
      this.gateIrises.forEach((iris, idx) => {
        const dir = idx % 2 === 0 ? 1 : -1;
        iris.rotation.x = time * (0.28 + idx * 0.09) * dir + t * 5.0 * dir;
      });
      if (this.digestSlab) {
        this.digestSlab.position.y = Math.sin(time * 1.4) * 0.06;
      }
      if (this.needleCrystal) {
        this.needleCrystal.rotation.y = time * 1.1;
        this.needleCrystal.rotation.z = Math.sin(time * 0.8) * 0.25;
      }
      this.vaultPlatters.forEach((platter, idx) => {
        const speed = (idx % 2 === 0 ? 1 : -1) * (0.24 + idx * 0.07);
        const boost = this.digestMode === 'retrieve' ? 2.2 : 1.0;
        platter.rotation.y = time * speed * boost;
      });
      if (this.retrieveConduitMat) {
        this.retrieveConduitMat.uniforms.uOpacity.value =
          this.digestMode === 'retrieve' ? 1.0 : this.digestMode === 'digest' ? 0.58 : 0.14;
        this.retrieveConduitMat.uniforms.uSpeed.value = this.digestMode === 'retrieve' ? 1.45 : 0.65;
      }
      if (this.captureConduitMat) {
        this.captureConduitMat.uniforms.uOpacity.value = this.digestMode === 'raw' ? 0.15 : 0.92;
      }
    }

    // --- ACT 4: Content Anchor Caliper & Articulated File Slabs ---
    if (this.anchorModel && this.anchorCaliper && this.stageGroups[3].visible) {
      if (!this.anchorScenarioUserOverride && actFloat > 2.55 && actFloat < 3.55) {
        const localA = Math.max(0, Math.min(1, actFloat - 2.65));
        const autoScen = localA < 0.28 ? 'verified' : localA < 0.72 ? 'insert' : 'rewrite';
        if (autoScen !== this.anchorScenario) {
          this.anchorScenario = autoScen;
          if (this.callbacks.onAutoAnchorScenario) this.callbacks.onAutoAnchorScenario(autoScen);
        }
      }

      const targetShift =
        this.anchorScenario === 'verified' ? 0 : this.anchorScenario === 'insert' ? 4 : 2;
      const isRewritten = this.anchorScenario === 'rewrite';

      const lerpRate = Math.min(1, dt * 6.5);
      this.slabOffset = THREE.MathUtils.lerp(this.slabOffset, targetShift * 0.30, lerpRate);
      const desiredCaliperY = isRewritten ? 0.75 - this.slabOffset + 0.18 : 0.75 - this.slabOffset;
      this.caliperY = THREE.MathUtils.lerp(this.caliperY, desiredCaliperY, lerpRate);
      this.anchorCaliper.position.y = this.caliperY;
      this.anchorCaliper.position.z = THREE.MathUtils.lerp(
        this.anchorCaliper.position.z,
        isRewritten ? 0.38 : 0.0,
        lerpRate
      );

      if (this.hashLensRing && this.hashLensRing.material) {
        const lensCol = isRewritten ? new THREE.Color('#f0b429') : new THREE.Color('#3fb950');
        this.hashLensRing.material.color.lerp(lensCol, lerpRate);
        this.hashLensRing.material.emissive.lerp(lensCol, lerpRate);
        this.hashLensRing.material.emissiveIntensity = isRewritten ? 2.4 + Math.sin(time * 8) * 0.6 : 2.3;
      }

      if (this.staticPtrNeedle && this.staticPtrNeedle.material) {
        const drifted = targetShift > 0 || isRewritten;
        const pCol = drifted ? new THREE.Color('#f85149') : new THREE.Color('#3fb950');
        this.staticPtrNeedle.material.color.lerp(pCol, lerpRate);
        this.staticPtrNeedle.material.emissive.lerp(pCol, lerpRate);
        this.staticPtrNeedle.material.emissiveIntensity = drifted ? 2.5 + Math.sin(time * 7) * 0.7 : 1.1;
      }

      this.lineSlabs.forEach((slab) => {
        const idx = slab.userData.idx;
        const isTarget = idx >= 3 && idx <= 5;
        slab.position.y = slab.userData.baseY - this.slabOffset * (idx >= 2 ? 1.0 : 0.25);

        if (isTarget && slab.material) {
          const targetCol = isRewritten ? new THREE.Color('#5a1e24') : new THREE.Color('#0f2744');
          const targetEmissive = isRewritten ? new THREE.Color('#f85149') : new THREE.Color('#3fb950');
          slab.material.color.lerp(targetCol, lerpRate);
          slab.material.emissive.lerp(targetEmissive, lerpRate);
          slab.material.emissiveIntensity = isRewritten ? 0.55 : 0.42;
        }
      });

      this.anchorModel.rotation.y = -0.22 + Math.sin(time * 0.45) * 0.07 + this.pointerSmooth.x * 0.12;
    }

    // --- ACT 5: Frozen Prompt Prefix & Dynamic Symbol Orbs ---
    if (this.prefixGroup && this.stageGroups[4].visible) {
      this.prefixGroup.rotation.y = 0.16 + Math.sin(time * 0.35) * 0.06 + this.pointerSmooth.x * 0.12;
      this.symbolOrbs.forEach((orb) => {
        const ph = orb.userData.phase;
        orb.rotation.x = time * 0.7 + ph;
        orb.rotation.y = time * 0.9 + ph;
        orb.position.y = orb.userData.basePos.y + Math.sin(time * 1.4 + ph) * 0.14;
      });
    }

    // --- ACT 6: Cross-Harness ACP Constellation ---
    if (this.stageGroups[5].visible) {
      if (this.ledgerDisc) this.ledgerDisc.rotation.y = time * 0.16;
      if (this.stewardSpire) this.stewardSpire.rotation.y = -time * 0.28;

      this.workerPods.forEach((pod, idx) => {
        const selected = idx === this.selectedHarnessIdx;
        const base = pod.userData.basePos;
        const targetY = base.y + (selected ? 0.52 : 0.0) + Math.sin(time * 1.3 + idx) * 0.08;
        pod.position.y = THREE.MathUtils.lerp(pod.position.y, targetY, Math.min(1, dt * 6));
        const s = THREE.MathUtils.lerp(pod.scale.x, selected ? 1.24 : 1.0, Math.min(1, dt * 6));
        pod.scale.setScalar(s);
        if (pod.userData.accentMesh) {
          pod.userData.accentMesh.material.emissiveIntensity = selected ? 3.2 : 1.4;
        }
      });

      const tangent = new THREE.Vector3();
      const posTmp = new THREE.Vector3();
      this.signalCapsules.forEach((cap) => {
        const arc = this.relayArcs[
          (cap.userData.arcIdx + Math.floor(time * 0.24 + cap.userData.offset)) % this.relayArcs.length
        ];
        const u = (time * 0.24 + cap.userData.offset) % 1.0;
        arc.curve.getPointAt(u, posTmp);
        arc.curve.getTangentAt(u, tangent);
        cap.position.copy(posTmp);
        cap.lookAt(posTmp.clone().add(tangent));
        cap.rotateY(Math.PI / 2);
      });
    }

    // --- ACT 7: Monumental Finale ---
    if (this.stageGroups[6].visible) {
      if (this.finaleCinch) {
        this.finaleCinch.rotation.y = time * 0.26 + this.pointerSmooth.x * 0.2;
        this.finaleCinch.rotation.x = Math.sin(time * 0.4) * 0.06;
      }
      if (this.finaleHalo) {
        this.finaleHalo.rotation.z = time * 0.18;
        this.finaleHalo.rotation.x = Math.sin(time * 0.28) * 0.18;
      }
      if (this.finaleHaloOuter) {
        this.finaleHaloOuter.rotation.z = -time * 0.12;
      }
    }
  }

  _animate() {
    if (this.disposed) return;
    this.rafId = requestAnimationFrame(() => this._animate());

    const dt = Math.min(this.clock.getDelta(), 0.05);
    this.elapsed += dt;

    const damp = 1.0 - Math.exp(-dt * 9.0);
    this.scrollSmooth = THREE.MathUtils.lerp(this.scrollSmooth, this.scrollTarget, damp);
    this.pointerSmooth.lerp(this.pointer, 1.0 - Math.exp(-dt * 5.0));

    const camState = sampleCameraState(this.scrollSmooth, this.aspect);

    const parallaxAmt = this.aspect < 0.9 ? 0.08 : 0.30;
    this.camera.position.set(
      camState.position.x + this.pointerSmooth.x * parallaxAmt,
      camState.position.y + this.pointerSmooth.y * (parallaxAmt * 0.5),
      camState.position.z
    );
    this.lookAtTarget.copy(camState.target);
    this.camera.lookAt(this.lookAtTarget);

    if (Math.abs(this.camera.fov - camState.fov) > 0.05) {
      this.camera.fov = camState.fov;
      this.camera.updateProjectionMatrix();
    }

    this.backdropDome.position.copy(this.camera.position);
    this.backdropMat.uniforms.uTopColor.value.copy(camState.topColor);
    this.backdropMat.uniforms.uHorizonColor.value.copy(camState.horizonColor);
    this.backdropMat.uniforms.uAccentColor.value.copy(camState.accentColor);
    this.backdropMat.uniforms.uAccentStrength.value = camState.accentStrength;
    this.scene.fog.color.copy(camState.topColor);

    this.stageLight.color.copy(camState.accentColor);
    this.stageLight.position.set(
      camState.target.x + 2.6,
      camState.target.y + 2.2,
      camState.target.z + 3.4
    );
    this.fillLight.position.set(
      camState.target.x - 3.4,
      camState.target.y - 1.4,
      camState.target.z + 2.8
    );
    this.keyLight.position.set(
      camState.target.x + 6,
      camState.target.y + 9,
      camState.target.z + 8
    );
    this.keyLight.target.position.copy(camState.target);
    this.keyLight.target.updateMatrixWorld();

    this._updateStages(this.scrollSmooth, dt);

    if (this.usePostProcessing && this.composer) {
      this.composer.render(dt);
    } else {
      this.renderer.render(this.scene, this.camera);
    }
  }

  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this.rafId);
    window.removeEventListener('resize', this._onResize);
    window.removeEventListener('pointermove', this._onPointerMove);
    this.canvas.removeEventListener('click', this._onClick);
    if (this.composer) this.composer.dispose();
    if (this.envTexture) this.envTexture.dispose();
    this.renderer.dispose();
  }
}
