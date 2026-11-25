I've built the five Blender 5.3 GLB assets, compiled the Three.js + GLSL + React site into `./site/`, and launched the headless Chromium verification suite (testing desktop and phone viewports under `/straitjacket/`). Waiting for the verification run to complete so I can inspect the captured screenshots and runtime report.I've refined the 3D stage isolation, desktop/mobile asymmetric camera framing, DOM-anchored scroll progress mapping, harness logo contrast badges, and mobile hero readability. Rebuilding `./site/` and running the updated headless Chromium verification suite now.Running the final production build into `./site/`, executing the headless Chromium verification suite across desktop and mobile viewports, and auditing relative URLs and `./straitjacket` integrity.I built the scroll-driven 3D front page for **Straitjacket** (`ctx-harness v0.5.0`) and compiled the self-contained static deliverable into [site/](file:///home/agent/project/site) with [site/index.html](file:///home/agent/project/site/index.html) as its entry point (`2.4 MB` total, 100% relative URLs, zero external host requests, nothing modified inside [straitjacket/](file:///home/agent/project/straitjacket)).

---

### 1. What Was Built

A 7-act, scroll-choreographed 3D web experience grounded in Straitjacket's *Addressable Evidence* design language (`#0A0C10` instrument ground, machined white `#E6EDF3` brackets, semantic Amber/Blue/Red/Green coding) and verified repository receipts:

1. **Act 01 — Restraint (Hero):** Monumental 3D Straitjacket `[ ]` brackets cinched by woven 3D amber tension straps, paired with the core invariant (`complete evidence ≠ active context → active context + addresses = reversible omission`), copyable `pip install 'ctx-harness==0.5.0'` pill, and direct `./docs/` navigation.
2. **Act 02 — The Flood:** Visualizes the checked-in `20,001`-line field-needle fixture ([field-needle-record.json](file:///home/agent/project/straitjacket/evals/field-needle-record.json)) with a 4,200-particle GPU byte-flood shader, contrasting Raw Flood (`302,628` tokens), Head+Tail truncation (`1,219` tokens, losing quiet `INFO` line `14,238`), and Straitjacket's `531`-token digest while the 3D brackets cinch shut.
3. **Act 03 — Four Gates & Local Vault (Interactive):** Four spinning mechanical iris gates (**Birth → Entry → Residence → Emission**) funneling raw bytes into a 6-platter optical local store while emitting a typed digest slab with a faceted Quiet Needle crystal. An interactive 3-mode switcher (**Raw Flood** / **Digest View** / **Bounded Retrieval**) live-controls the 3D particle funnel, vault platter speed, and glowing retrieval conduit.
4. **Act 04 — Content Anchors (Interactive):** Articulated 14-line source column (`repo:src/auth.py --lines 40:52@07407f1c`) with a static unanchored needle and sliding optical caliper. An interactive edit simulator (**Original** / **+6 Lines** / **Rewritten**) physically shifts the 3D code slabs and drives the caliper to verify in place, slide and relocate (`L46:58`), or back off and refuse cleanly (`0` wrong answers across `1,920` measured cases in [anchor-drift-2026-08-20.json](file:///home/agent/project/straitjacket/evals/anchor-drift-2026-08-20.json)).
5. **Act 05 — Stable Prompt Prefix & Symbol Navigation:** Split architectural beam contrasting an immutable holographic `cache_wrap.prefix` slab with orbiting dynamic suffix / symbol lookup nodes (`ctx search`, `ctx def`, `ctx refs`, `ctx compress --cache-prefix`).
6. **Act 06 — Cross-Harness ACP & Relay Constellation (Interactive):** Elevated architectural view of the rotating append-only **Task Ledger** disc, deterministic **Steward** spire, **7 orbiting Worker Pods** (Claude Code, Codex, Antigravity, OpenCode, Hermes, Oh My Pi, DeepSeek Harness — using official icons copied into [site/agents/](file:///home/agent/project/site/agents)), and animated **Signal Capsules** arcing along relay splines. Clicking any harness pill or raycast-clicking a 3D pod highlights its ACP endpoint and host hook contract.
7. **Act 07 — Measured Receipts & Docs Gateway:** Checked-in evaluation receipts table (including the honest small-task canary cost note), copyable quickstart snippet, and relative links into `./docs/`.

**Key source files:**
- 3D engine & shaders: [EvidenceScene.js](file:///home/agent/project/src/scene/EvidenceScene.js), [shaders.js](file:///home/agent/project/src/scene/shaders.js), [cameraPath.js](file:///home/agent/project/src/scene/cameraPath.js), [environment.js](file:///home/agent/project/src/scene/environment.js)
- UI & scroll orchestration: [App.jsx](file:///home/agent/project/src/App.jsx), [ActOverlays.jsx](file:///home/agent/project/src/components/ActOverlays.jsx), [TopNav.jsx](file:///home/agent/project/src/components/TopNav.jsx), [ChapterRail.jsx](file:///home/agent/project/src/components/ChapterRail.jsx), [app.css](file:///home/agent/project/src/styles/app.css), [straitjacketData.js](file:///home/agent/project/src/data/straitjacketData.js)
- Build config: [vite.config.js](file:///home/agent/project/vite.config.js) (`base: './'`, `outDir: 'site'`)

---

### 2. Libraries Used & Purpose

| Library | Role |
|---|---|
| `three` (`0.187.0`) | WebGL renderer, PBR materials, `GLTFLoader`, procedural PMREM studio HDRI, custom GLSL `ShaderMaterial`s, raycasting |
| `postprocessing` (`6.39.6`) | `EffectComposer` pipeline with selective mipmap `BloomEffect`, `VignetteEffect`, and `ACES_FILMIC` `ToneMappingEffect` |
| `lenis` (`1.3.28`) | Smooth inertial scrolling on wheel and touch, synchronized with DOM-anchored act progress |
| `gsap` (`3.15.1`) + `ScrollTrigger` | Scroll-scrubbed editorial panel reveals |
| `react` & `react-dom` (`19.3.1`) | Interactive HUD, chapter rail, live delivery/anchor/harness controls, accessible semantics |
| `@fontsource-variable/space-grotesk`, `@fontsource-variable/inter`, `@fontsource/jetbrains-mono` | Locally bundled WOFF/WOFF2 display, prose, and monospace typography inside `./site/assets/` |
| `vite` (`8.3.8`) + `@vitejs/plugin-react` | Relative-base static production bundler into `./site/` |
| `@gltf-transform/core`, `extensions`, `functions` | Build-time deduplication and pruning of Blender GLBs while preserving `KHR_materials_clearcoat` and `KHR_materials_emissive_strength` |

---

### 3. 3D Objects Built & Toolchain

Five articulated PBR `.glb` assemblies were generated procedurally via headless **Blender 5.3.0** ([build_assets.py](file:///home/agent/project/blender/build_assets.py)) and optimized with [optimize_models.mjs](file:///home/agent/project/scripts/optimize_models.mjs):

| GLB Asset | Triangles | Nodes | Purpose |
|---|---:|---:|---|
| [straitjacket_cinch.glb](file:///home/agent/project/site/models/straitjacket_cinch.glb) (`101.4 KiB`) | 6,056 | 30 | Sculpted `[ ]` machined brackets with hex fasteners, blue telemetry inlays, central reticle, and 3D braided/over-under woven amber tension straps + clasps |
| [artifact_vault.glb](file:///home/agent/project/site/models/artifact_vault.glb) (`67.2 KiB`) | 5,120 | 55 | Octagonal content-addressed local store silo with 8 fluted ribs, hexagonal core spine, and 6 independently rotating optical platters |
| [evidence_prism.glb](file:///home/agent/project/site/models/evidence_prism.glb) (`102.9 KiB`) | 7,796 | 67 | Precision optical bench rails carrying 4 rotating mechanical iris gates (`Birth`, `Entry`, `Residence`, `Emission`), beveled Digest Slab, and faceted Quiet Needle crystal |
| [anchor_clamp.glb](file:///home/agent/project/site/models/anchor_clamp.glb) (`56.3 KiB`) | 3,388 | 49 | Twin-rail vertical file column with 14 individually articulated code line slabs, static red unanchored pointer, and sliding caliper with optical hash-verification lens |
| [harness_nodes.glb](file:///home/agent/project/site/models/harness_nodes.glb) (`132.2 KiB`) | 8,612 | 33 | Append-only Task Ledger astrolabe disc, deterministic Steward spire, modular hexagonal Worker Pod (instanced 7× in Three.js), and relay Signal Capsule |

Four custom GLSL shaders ([shaders.js](file:///home/agent/project/src/scene/shaders.js)) complement the Blender meshes: the atmospheric instrument dome with subtle horizon coordinate lattice, the 4,200-particle byte-flood/vault-siphon system, animated directional energy conduit ribbons, and holographic fresnel/scanline glass for the frozen prompt prefix beam and finale halo.

---

### 4. How It Was Checked

Verified end-to-end with [verify_site.mjs](file:///home/agent/project/scripts/verify_site.mjs) serving `./site/` over local HTTP and driving headless Chromium (`$CHROMIUM_BIN --headless=new --no-sandbox`) over Chrome DevTools Protocol:
- **Path portability:** Loaded and verified from both `http://127.0.0.1:<port>/straitjacket/` and root `http://127.0.0.1:<port>/`.
- **Network & runtime audit:** `48` total requests across desktop + mobile + root runs, **`0` external requests**, **`0` failed requests**, **`0` console errors**, **`0` page exceptions**, and `grep` confirmed zero `http(s)://` references in [site/index.html](file:///home/agent/project/site/index.html).
- **Responsive layout & visual inspection:** Captured and visually inspected 14 screenshots across **Desktop (`1440×900`)** (all 7 acts + interactive *Bounded Retrieval* and *Rewritten / Refused* states) and **Phone (`390×844`)** (Hero, Flood, Gates, Harnesses, Receipts), confirming `overflowX: false` on both viewports and iterating on stage visibility culling, asymmetric desktop framing, mobile hero contrast scrim, and uniform harness icon contrast badges.
- **Repository integrity:** Confirmed `./straitjacket/` was never modified.

---

### 5. What Could Not Be Done / Known Limits

- **Godot:** Mentioned in the owner's original phrasing, but Godot is not installed in this environment and the task specified Three.js + shaders + Blender 5.3 for browser 3D delivery.
- **Live reference inspection & hardware GPU testing:** Without internet access, `scrolltide.co` was referenced only via `./reference/`; headless verification ran under Chromium's SwiftShader software WebGL rather than physical desktop GPUs or real mobile touch devices.
- **Documentation target pages:** All documentation links point cleanly to relative `./docs/` paths as required, though `./site/docs/` itself is not bundled here because the existing documentation site populates that path at deployment time.