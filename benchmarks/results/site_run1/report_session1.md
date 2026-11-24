Built the scroll-driven 3D visualization website for **Straitjacket** (`ctx-harness v0.5.0`) in [`site/index.html`](file:///home/agent/project/site/index.html), grounded in `./straitjacket`'s documentation, evaluation receipts, and "Addressable Evidence" visual language without modifying anything inside `./straitjacket`.

---

### 1. What Was Built

A 6-chapter, scroll-choreographed static web application ([`site/index.html`](file:///home/agent/project/site/index.html), source in [`src_site/`](file:///home/agent/project/src_site/)) featuring a persistent Three.js WebGL stage with three connected architectural stations along a Z-axis corridor, synchronized dynamically to actual DOM chapter positions across desktop and mobile viewports:

- **Chapter 00 — Hero (`#hero`)**: Introduces the core principle (`complete evidence ≠ active context`) beside the 3D **Containment Gate**, live GPU evidence particle stream, one-click `pip install 'ctx-harness==0.5.0'` copy button, and official badges for all 7 supported coding harnesses.
- **Chapter 01 — Evidence Fates (`#fates`)**: Interactive live comparison of the `evals/field-needle-record.json` 20,001-line log fixture (**531 tokens** contained with Straitjacket vs. **302,628 tokens** raw flood vs. **1,219 tokens** head+tail truncation that loses the quiet failure at line 14,238). Switching fates drives the 4,200-particle GLSL stream, lighting, and top HUD token telemetry in real time.
- **Chapter 02 — The Four Gates & Digest Anatomy (`#gates`)**: Interactive selector for **Birth**, **Entry**, **Residence**, and **Emission** that illuminates the corresponding optical shader ring inside the 3D Containment Gate alongside a deterministic digest specimen (`run:8956c843e556#stdout`).
- **Chapter 03 — Content Anchors (`#anchors`)**: Interactive drift simulator (**Insert Above**, **Delete Above**, **Move Span**, **Rewrite Span**) backed by `evals/anchor-drift-2026-08-20.json` (1,920 lookups, 0 wrong content) that slides the optical laser scan plane and updates the floating holographic address plaque on the 3D **Anchor Caliper**.
- **Chapter 04 — Cross-Harness ACP Routing (`#routing`)**: Interactive task-route selector (**General Feature Task**, **Targeted Small Edit**, **Diff Review**, **Named Test Verification**) that lights up the matching radial docking bays, curved GLSL relay arcs, and orbiting checkpoint octahedra around the 7-port 3D **Harness Router**.
- **Chapter 05 — Prefix Stability, Measured Receipts & Quickstart (`#receipts`)**: Panoramic overview of all three 3D stations, `cache_wrap.prefix` / `cache_wrap.suffix` diagram, balanced 2+3 grid of checked-in evaluation receipts (including transparent cost/resolution trade-offs), 3-step CLI quickstart with copy buttons, and a 3D pipeline provenance card.

Custom GLSL shaders ([`src_site/shaders/index.js`](file:///home/agent/project/src_site/shaders/index.js)) provide:
1. Volumetric atmospheric horizon dome with chapter-interpolated semantic palette (`#3b82f6` blue / `#ef4444` red / `#f59e0b` amber / `#22c55e` green).
2. Infinite architectural datum floor grid with radial sonar pulse tracking the active 3D station.
3. 4,200-particle GPU evidence stream with three live flow regimes (contained diversion, turbulent red flood, and clipped truncation).
4. Four optical gate aperture iris rings.
5. Anchor verification laser scan plane and 7 animated curved ACP relay conduits.

---

### 2. Libraries Used

All runtime JS libraries come from preinstalled `./node_modules` and are bundled into relative chunks inside [`site/assets/`](file:///home/agent/project/site/assets/):

- **`three` (`0.170.0`)** — WebGL renderer (`ACESFilmicToneMapping`), `GLTFLoader` for the 3 GLB instruments, `RoomEnvironment` + `PMREMGenerator` for studio metallic reflections, custom `ShaderMaterial` pipelines, and camera spline interpolation ([`src_site/scene/Stage3D.js`](file:///home/agent/project/src_site/scene/Stage3D.js)).
- **`gsap` (`^3.13.0` + `ScrollTrigger`)** — Scroll-scrubbed progress synchronization, active chapter tracking for top HUD & right scroll rail, and section entrance choreography ([`src_site/main.js`](file:///home/agent/project/src_site/main.js)).
- **`lenis` (`1.3.27`)** — Smooth wheel inertia and smooth anchor-jump scrolling synced to `gsap.ticker` and `ScrollTrigger.update`.
- **`vite` (`^7.0.0`)** — Production bundler configured with `base: './'` ([`vite.config.js`](file:///home/agent/project/vite.config.js)) so every asset inside `./site/` resolves via relative URLs from any subpath.
- **Google Fonts (`Space Grotesk` & `JetBrains Mono`)** — Referenced via the allowed `fonts.googleapis.com` stylesheet with system sans/mono fallbacks for offline use.

---

### 3. 3D Objects Built with `antigravity-3d` (Blender + three.js + Godot)

All three 3D instruments were authored in OpenUSD via `antigravity-3d` Scene IR, processed through Blender 5.3 for UV unwrapping, procedural PBR surface baking (`kernel.smart_source`), LODs, and UCX convex collision hulls, validated in both **three.js** and **Godot 4.7.2**, and shipped as standalone embedded-texture LOD0 GLBs in [`site/models/`](file:///home/agent/project/site/models/):

| Asset | Site File | Geometry / Budget | Engine & Validator Checks | Studio Preview Status |
| --- | --- | --- | --- | --- |
| **Containment Gate** (`containment_gate-5a671b`) | [`site/models/containment_gate.glb`](file:///home/agent/project/site/models/containment_gate.glb) (17 MB) | 74 nodes, 7,738 tris LOD0 (5,076 LOD1), 4 draw calls, 8 UCX hulls, UV stretch `0.000` | USD `0 crit/0 warn`; `web_three` `0 crit/0 warn`; Blender QA `0 errors`; headless **three.js** `23/23` checks passed (silhouette IoU `0.9997`) | `PREVIEW (verified: false)` — uncalibrated fallback judge (`tier15=0.96`), `max_images: 0` |
| **Anchor Caliper** (`anchor_caliper-cf3573`) | [`site/models/anchor_caliper.glb`](file:///home/agent/project/site/models/anchor_caliper.glb) (4.1 MB) | 48 nodes, 2,556 tris LOD0 (1,828 LOD1, 958 LOD2), 4 draw calls, 8 UCX hulls, UV stretch `0.000` | USD `0 crit/0 warn`; `web_three` `0 crit/0 warn`; Blender QA `0 errors`; headless **three.js** `25/25` checks passed (silhouette IoU `0.9997`) | `PREVIEW (verified: false)` — uncalibrated fallback judge (`tier15=0.89`, `brief_depiction=2.56`), `max_images: 0` |
| **Harness Router** (`harness_router-c5f2f5`) | [`site/models/harness_router.glb`](file:///home/agent/project/site/models/harness_router.glb) (7.7 MB) | 29 nodes, 3,536 tris LOD0 (1,226 LOD1), 4 draw calls, 8 UCX hulls, UV stretch `0.000` | USD `0 crit/0 warn`; `godot_4` `0 crit/0 warn`; Blender QA `0 errors`; headless **Godot 4.7.2-stable** import `ok: true` (4/4 textures loaded, 2/2 ORM+normal surfaces, 3,536 tris) | `PREVIEW (verified: false)` — uncalibrated fallback judge (`tier15=0.73`), `max_images: 0` |

---

### 4. How It Was Checked

1. **Static subpath serving**: Served the parent directory via `python3 -m http.server` and loaded `http://127.0.0.1:<port>/site/index.html` to verify relative-path portability from a non-root subpath.
2. **Headless Chromium verification (`$CHROMIUM_BIN --headless=new --no-sandbox` with SwiftShader WebGL)** across both **Desktop (`1440×900`)** and **Mobile (`390×844`)**:
   - **0 uncaught JS errors** and **0 failed local network requests** on both viewports.
   - Confirmed all **3/3 GLBs** loaded (`hudModels: "3/3 GLB READY"`) with crisp PBR metallic shading and custom GLSL overlays.
   - Verified interactive controls update both DOM and 3D stage state (e.g., Evidence Fates toggled HUD telemetry `531 tok → 302,628 tok`, Four Gates illuminated Gate 02 iris, ACP Routing updated command preview and highlighted bays).
   - Verified mobile responsiveness (`scrollWidth === clientWidth === 390`, zero horizontal overflow) and visually inspected captured PNG screenshots of Desktop Hero, Evidence Fates, Four Gates, Anchor Caliper, ACP Routing, Receipts, and Mobile Hero, Anchor Caliper, and ACP Routing to confirm unobstructed 3D framing on both form factors.
3. **Static host URL audit**: Inspected all `src` and `href` attributes in [`site/index.html`](file:///home/agent/project/site/index.html); every local script, stylesheet, icon, and GLB uses a `./...` relative path inside `./site/`, with only `fonts.googleapis.com` as an external stylesheet.

---

### 5. What Could Not Be Done

- **Native Decisions API verified 3D export & image-model PBR texture generation**: Because this machine has no internet access and no Decisions API key, the `antigravity-3d` sessions ran in `PREVIEW (UNVERIFIED)` mode (`verified: false`, uncalibrated fallback judge) with `max_images: 0`, so material maps were generated procedurally from geometry rather than via an image-generation model.
- **Live Google Fonts fetch during offline testing**: Chromium could not fetch `fonts.googleapis.com` while offline, so local verification rendered with the CSS system font fallbacks; on a live static host with internet access, `Space Grotesk` and `JetBrains Mono` will load automatically.
