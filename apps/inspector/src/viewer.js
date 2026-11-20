// The inspector front end shared by both hosts: the standalone inspector.html (embedded payload,
// notes exported as a file) and the Antigravity MCP App (data and notes through the MCP server).
// Everything host-specific comes in through the `host` object given to createInspector().
//
// The asset itself is the "review GLB" (antigravity3d/artifacts/review_glb.py): built by the same glTF
// writer and conversion as the package GLBs, loaded with three's GLTFLoader. Geometry, normals, UVs and
// materials are the GLB's; the debug channels below are overrides on the loaded meshes, never a second
// geometry or material path.
import { gltfLoaderFor, parseGlb } from "./gltf.js";

export const MODES = ["lit", "clay", "albedo", "orm", "occ", "rough", "metal", "normal", "uv", "prim"];
export const PRESETS = ["front", "right", "top", "persp"];
const TEX_KINDS = { albedo: "albedo", orm: "orm", occ: "orm", rough: "orm", metal: "orm", normal: "normal" };
const REASONS = {
  albedo: "No albedo texture with UVs at this LOD", orm: "No ORM texture with UVs at this LOD",
  occ: "No ORM texture with UVs at this LOD", rough: "No ORM texture with UVs at this LOD",
  metal: "No ORM texture with UVs at this LOD", normal: "No normal map with UVs at this LOD",
  uv: "No UVs at this LOD", prim: "This LOD level is one merged mesh without per-node parts",
};
export const NOTE_ASSET = "__asset__";

const $ = (id) => document.getElementById(id);
const $all = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));
export const fmt = (n) => Number(n).toLocaleString("en-US");
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  for (const k in attrs || {}) {
    if (k === "text") e.textContent = attrs[k];
    else if (k === "style") e.setAttribute("style", attrs[k]);
    else if (k === "dataset") Object.assign(e.dataset, attrs[k]);
    else e.setAttribute(k, attrs[k]);
  }
  for (const c of children || []) if (c != null) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return e;
}

export function noteKey(nodeId) { return nodeId == null ? NOTE_ASSET : nodeId; }

/** A three.js colour usable from CSS: host variables can be any CSS colour syntax, three only parses some. */
function cssColor(THREE, name, fallback) {
  const probe = document.createElement("span");
  probe.style.color = cssVar(name) || fallback;
  document.body.appendChild(probe);
  const rgb = getComputedStyle(probe).color.match(/[\d.]+/g) || [];
  probe.remove();
  if (rgb.length < 3) return new THREE.Color(fallback);
  return new THREE.Color().setRGB(+rgb[0] / 255, +rgb[1] / 255, +rgb[2] / 255, THREE.SRGBColorSpace);
}

/**
 * host = {
 *   kind: "standalone" | "mcp",
 *   notes: { save(nodeId|null, text, revision, sessionId) -> Promise<entries[]> },  // bound at edit time
 *   exportNotes(doc) | null,          // file export of the notes (standalone: download)
 *   loadGlb(payload) -> Promise<ArrayBuffer>,   // the review GLB of this payload's revision
 *   onSelection(info, fromUser), onNotesChanged(info), onAsk() | null, onRefresh() | null,
 * }
 */
export function createInspector(host) {
  const state = {
    mode: "lit", lod: 0, wire: false, coll: false, selected: null, preset: "persp", userMoved: false,
    glOk: false, lastExport: null, revision: null, loaded: false,
  };
  window.__inspectorReady = false;
  window.__inspectorError = null;
  window.__inspectorState = state;
  let P = null; // the loaded payload (data model)
  let qa = null;
  let notes = []; // [{id, node_id, text, revision, updated_at, stale}]
  let gl = null; // three.js part, once started
  let pendingSave = null;

  /* ---------- layout and side panel ---------- */
  $("side-toggle").addEventListener("click", function () {
    const hidden = $("app").classList.toggle("side-hidden");
    this.setAttribute("aria-expanded", hidden ? "false" : "true");
    this.textContent = hidden ? "Show panel" : "Hide panel";
    if (gl) gl.resize();
  });
  function showTab(name) {
    $all(".tab").forEach((t) => t.setAttribute("aria-selected", t.dataset.tab === name ? "true" : "false"));
    $all(".panels section").forEach((s) => s.classList.toggle("active", s.dataset.panel === name));
  }
  $all(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  if (host.onRefresh) {
    $("btn-refresh").hidden = false;
    $("btn-refresh").addEventListener("click", () => host.onRefresh());
  }

  /* ---------- panels rendered from the data model ---------- */
  function lodLevels() {
    if (!P) return [{ label: "LOD0", tris: 0, parts: [] }];
    return P.levels;
  }
  function gauge(key, label, value, max) {
    const over = value > max, pct = Math.min(100, max > 0 ? value / max * 100 : 100);
    return el("div", { class: "gauge", dataset: { key, state: over ? "over" : "ok" } }, [
      el("div", { class: "gauge-head" }, [el("span", { text: label }), el("span", { class: "gauge-num", text: fmt(value) + " / " + fmt(max) })]),
      el("div", { class: "bar" }, [el("i", { style: "width:" + pct.toFixed(1) + "%" })]),
      el("div", { class: "gauge-state", text: over ? "OVER BUDGET by " + fmt(value - max) : "Within budget (" + fmt(max - value) + " spare)" }),
    ]);
  }
  function renderGauges() {
    const g = $("gauges");
    g.replaceChildren();
    if (!P) return;
    const b = P.budget, s = P.summary;
    g.append(gauge("tris", "Triangles", lodLevels()[state.lod].tris, b.max_tris),
      gauge("draws", "Draw calls", s.draw_calls, b.max_draw_calls),
      gauge("hulls", "Collision hulls", s.hulls, b.max_collision_hulls));
  }
  function renderQa() {
    const box = $("qa");
    box.replaceChildren();
    const q = qa || (P && P.qa);
    if (!q) { box.append(el("p", { class: "muted", text: "No QA results for this asset yet." })); return; }
    box.append(el("p", { class: "qa-line" }, [el("strong", { text: "Tier 1:" }), " ",
      el("span", { class: "pill " + (q.tier1_ok ? "pass" : "fail"), text: q.tier1_ok ? "PASS" : "FAIL" }), " " + (q.tier1_line || "")]));
    const thr = q.threshold != null ? q.threshold : 3.4;
    if (q.tier15_score == null) {
      box.append(el("p", { class: "qa-line" }, [el("strong", { text: "Tier 1.5:" }), " not scored (export threshold " + thr.toFixed(1) + ")"]));
    } else {
      const ok = q.tier15_score >= thr, scale = Math.max(4, q.tier15_score);
      box.append(el("div", { class: "gauge", dataset: { key: "tier15", state: ok ? "ok" : "over" } }, [
        el("div", { class: "gauge-head" }, [el("span", {}, [el("strong", { text: "Tier 1.5 score" })]),
          el("span", { class: "gauge-num", text: q.tier15_score.toFixed(2) + " (export threshold " + thr.toFixed(1) + ")" })]),
        el("div", { class: "bar" }, [el("i", { style: "width:" + Math.min(100, q.tier15_score / scale * 100).toFixed(1) + "%" }),
          el("span", { class: "tick", style: "left:" + (thr / scale * 100).toFixed(1) + "%" })]),
        el("div", { class: "gauge-state", text: ok ? "Meets export threshold" : "BELOW export threshold by " + (thr - q.tier15_score).toFixed(2) }),
      ]));
    }
    if (q.export_allowed != null) {
      box.append(el("p", { class: "qa-line" }, [el("strong", { text: "Export gate:" }), " ",
        el("span", { class: "pill " + (q.export_allowed ? "pass" : "fail"), text: q.export_allowed ? "OPEN" : "BLOCKED" })]));
    }
    const defects = Object.entries(q.defects || {}).sort((a, b) => b[1] - a[1]);
    if (defects.length) {
      box.append(el("h2", { style: "margin-top:10px", text: "Defect probability" }));
      for (const [name, p] of defects) {
        const pc = Math.max(0, Math.min(1, Number(p))) * 100;
        box.append(el("div", { class: "defect" }, [el("span", { text: name }),
          el("div", { class: "bar" }, [el("i", { style: "width:" + pc.toFixed(0) + "%" })]), el("span", { class: "pct", text: pc.toFixed(0) + "%" })]));
      }
    }
    if ((q.audit_deltas || []).length) {
      box.append(el("h2", { style: "margin-top:10px", text: "Audit deltas" }));
      box.append(el("ul", { class: "deltas" }, q.audit_deltas.map((d) => el("li", { text: d }))));
    }
  }
  function renderNodes() {
    const body = $("nodes").tBodies[0];
    body.replaceChildren();
    const legend = el("ul");
    for (const m of (P ? P.nodes : [])) {
      const btn = el("button", { type: "button", class: "rowbtn", text: m.id });
      const count = notes.filter((n) => n.node_id === m.id && !n.stale).length;
      const tr = el("tr", { "aria-selected": "false", title: m.prim, dataset: { node: m.id } }, [
        el("td", {}, [el("span", { class: "sw", style: "background:" + m.color }), btn,
          count ? el("span", { class: "badge", title: "review notes", text: count + " note" + (count > 1 ? "s" : "") }) : null]),
        el("td", { class: "num", text: fmt(m.tris) }), el("td", { text: m.mat || "default" }),
      ]);
      tr.addEventListener("click", () => selectNode(m.id === state.selected ? null : m.id, true));
      btn.addEventListener("click", (ev) => { ev.stopPropagation(); selectNode(m.id === state.selected ? null : m.id, true); });
      body.append(tr);
      legend.append(el("li", {}, [el("span", { class: "sw", style: "background:" + m.color }), m.id]));
    }
    $("legend").replaceChildren(legend);
    markRows();
  }
  function markRows() {
    $all("#nodes tbody tr").forEach((tr) => tr.setAttribute("aria-selected", tr.dataset.node === state.selected ? "true" : "false"));
  }
  function renderLodButtons() {
    const g = $("lod-group");
    $all("[data-lod]", g).forEach((b) => b.remove());
    lodLevels().forEach((l, i) => {
      const b = el("button", { type: "button", class: "btn", "aria-pressed": i === state.lod ? "true" : "false", dataset: { lod: String(i) },
        text: l.label + " (" + fmt(l.tris) + " tris)" });
      b.addEventListener("click", () => setLod(i));
      g.append(b);
    });
  }
  function renderHeader() {
    $("asset-name").textContent = P ? P.asset_name : "";
    $("engine-chip").textContent = "Target engine: " + (P ? P.budget.target_engine : "-");
    const chip = $("rev-chip");
    chip.hidden = !(P && P.revision && host.kind === "mcp");
    if (P && P.revision) { chip.textContent = "rev " + P.revision.slice(0, 8); chip.title = "Revision " + P.revision; }
    if (P) document.title = P.asset_name + " - 3D inspector";
  }

  /* ---------- modes ---------- */
  function modeAvailable(mode) {
    if (mode === "lit" || mode === "clay") return true;
    if (!P) return false;
    const parts = lodLevels()[state.lod].parts;
    if (mode === "uv") return parts.some((p) => p.uv);
    if (mode === "prim") return parts.some((p) => p.node != null);
    return parts.some((p) => p.uv && p.maps.includes(TEX_KINDS[mode]));
  }
  function refreshModeButtons() {
    $all("#modebar [data-mode]").forEach((b) => {
      const ok = modeAvailable(b.dataset.mode);
      b.setAttribute("aria-disabled", ok ? "false" : "true");
      b.title = ok ? "" : (REASONS[b.dataset.mode] || "Unavailable");
      b.setAttribute("aria-pressed", b.dataset.mode === state.mode ? "true" : "false");
    });
    document.body.dataset.channel = state.mode;
    $("legend").hidden = state.mode !== "prim";
  }
  function setMode(mode) {
    if (!modeAvailable(mode)) return false;
    state.mode = mode; refreshModeButtons(); if (gl) gl.rebuild();
    return true;
  }
  function setLod(i) {
    if (!P || i < 0 || i >= lodLevels().length) return false;
    state.lod = i;
    $all("[data-lod]").forEach((x) => x.setAttribute("aria-pressed", x.dataset.lod === String(i) ? "true" : "false"));
    if (!modeAvailable(state.mode)) state.mode = "lit";
    refreshModeButtons(); renderGauges(); if (gl) gl.rebuild();
    return true;
  }
  $all("#modebar [data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $all("[data-preset]").forEach((b) => b.addEventListener("click", () => setPreset(b.dataset.preset)));
  function setPreset(name) {
    state.preset = name; state.userMoved = false;
    $all("[data-preset]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.preset === name ? "true" : "false"));
    if (gl) gl.preset(name);
  }
  function setWire(on) { state.wire = !!on; $("btn-wire").setAttribute("aria-pressed", String(state.wire)); if (gl) gl.rebuildWire(); }
  function setColl(on) {
    if (!P || !P.colliders.length) return false;
    state.coll = !!on; $("btn-coll").setAttribute("aria-pressed", String(state.coll)); if (gl) gl.rebuildColliders();
    return true;
  }
  $("btn-wire").addEventListener("click", () => setWire(!state.wire));
  $("btn-coll").addEventListener("click", () => setColl(!state.coll));
  function refreshCollButton() {
    const b = $("btn-coll"), none = !P || !P.colliders.length;
    b.setAttribute("aria-disabled", none ? "true" : "false");
    b.title = none ? "No collision hulls supplied for this asset" : "";
    if (none && state.coll) { state.coll = false; b.setAttribute("aria-pressed", "false"); }
  }

  /* ---------- selection ---------- */
  function selectionInfo() {
    const m = P && state.selected != null ? P.nodes.find((x) => x.id === state.selected) : null;
    const mine = currentNote();
    return {
      asset_name: P ? P.asset_name : "", revision: P ? P.revision : null, node: m ? { id: m.id, prim: m.prim, tris: m.tris, mat: m.mat || "" } : null,
      note: mine ? mine.text : "", notes_total: notes.filter((n) => !n.stale).length, notes_stale: notes.filter((n) => n.stale).length,
    };
  }
  function selectNode(id, fromUser) {
    if (id != null && !(P && P.nodes.some((m) => m.id === id))) return false;
    flushNote();
    state.selected = id;
    markRows();
    refreshNoteBox();
    if (gl) gl.applySelection();
    if (fromUser && host.onSelection) host.onSelection(selectionInfo());
    return true;
  }

  /* ---------- notes ---------- */
  function currentNote() {
    if (!P) return null;
    return notes.find((n) => noteKey(n.node_id) === noteKey(state.selected) && n.revision === P.revision && !n.stale) || null;
  }
  function refreshNoteBox() {
    $("note-label").textContent = "Notes for: " + (state.selected != null ? "node " + state.selected : "whole asset");
    const n = currentNote();
    $("note-text").value = n ? n.text : "";
    renderNoteList();
  }
  function updateNotesStatus(msg) {
    const cur = notes.filter((n) => !n.stale && n.text.trim()).length, stale = notes.filter((n) => n.stale).length;
    $("notes-status").textContent = (msg ? msg + " " : "") + cur + " note(s)" + (stale ? ", " + stale + " stale" : "") + ". " + (host.notesWhere || "");
  }
  function renderNoteList() {
    const ul = $("note-list");
    ul.replaceChildren();
    const sorted = notes.filter((n) => n.text.trim()).slice().sort((a, b) => (a.stale - b.stale) || String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    for (const n of sorted) {
      const who = el("button", { type: "button", class: "who", text: n.node_id == null ? "whole asset" : n.node_id });
      who.addEventListener("click", () => selectNode(n.node_id, true));
      const tag = n.stale ? " (stale: written against revision " + String(n.revision || "?").slice(0, 8) + ")" : "";
      ul.append(el("li", { dataset: { stale: String(!!n.stale), node: noteKey(n.node_id) } }, [who, tag ? el("span", { class: "muted", text: tag }) : null, ": " + n.text]));
    }
  }
  function setNotes(list) {
    notes = (list || []).map((n) => Object.assign({}, n, { stale: !!n.stale || (P && n.revision != null && n.revision !== P.revision) }));
    if (document.activeElement !== $("note-text") || !pendingSave) refreshNoteBox(); else renderNoteList();
    renderNodes();
    updateNotesStatus();
  }
  function localUpsert(nodeId, text) {
    const key = noteKey(nodeId), rev = P ? P.revision : null;
    const i = notes.findIndex((n) => noteKey(n.node_id) === key && n.revision === rev && !n.stale);
    const now = new Date().toISOString();
    if (i >= 0) notes[i] = Object.assign({}, notes[i], { text, updated_at: now });
    else notes.push({ id: "local-" + key, node_id: nodeId, text, revision: rev, updated_at: now, stale: false });
  }
  function sessionOf(model) { return model ? (model.session_id || null) : null; }
  /** Sends the pending edit with the revision and session it was typed under (captured at input time). */
  function flushNote() {
    if (!pendingSave) return Promise.resolve();
    const p = pendingSave; pendingSave = null; clearTimeout(p.timer);
    return save(p.nodeId, p.text, p.revision, p.sessionId);
  }
  function save(nodeId, text, revision, sessionId) {
    updateNotesStatus("Saving…");
    return Promise.resolve(host.notes.save(nodeId, text, revision, sessionId)).then((list) => {
      // the reply lists the notes of the session the edit belongs to: only show it if that is still on screen
      if (list && sessionId === sessionOf(P)) setNotes(list); else renderNoteList();
      updateNotesStatus("Saved.");
      if (host.onNotesChanged) host.onNotesChanged(Object.assign(selectionInfo(), { edited: { node_id: nodeId, text } }));
    }).catch((e) => {
      updateNotesStatus("NOT saved (" + (e && e.message ? e.message : e) + "); keep this view open and retry.");
    });
  }
  $("note-text").addEventListener("input", function () {
    if (!P) return;
    const nodeId = state.selected, text = this.value;
    localUpsert(nodeId, text);
    if (pendingSave && pendingSave.nodeId !== nodeId) flushNote();
    if (pendingSave) clearTimeout(pendingSave.timer);
    pendingSave = { nodeId, text, revision: P.revision || null, sessionId: sessionOf(P),
                    timer: setTimeout(flushNote, host.saveDelayMs != null ? host.saveDelayMs : 400) };
    updateNotesStatus("Editing…");
  });
  $("note-text").addEventListener("blur", () => { flushNote(); });
  function exportDoc() {
    const list = notes.filter((n) => n.text && n.text.trim()).map((n) => ({ node_id: n.node_id, text: n.text, revision: n.revision || null, stale: !!n.stale }));
    return { schema: "antigravity3d.review_notes/1", asset_name: P ? P.asset_name : "", revision: P ? P.revision : null, exported_at: new Date().toISOString(), notes: list };
  }
  $("btn-export").addEventListener("click", () => {
    if (!host.exportNotes) return;
    flushNote();
    const doc = exportDoc();
    state.lastExport = JSON.stringify(doc, null, 2);
    host.exportNotes(doc, state.lastExport);
  });
  $("btn-ask").addEventListener("click", () => { if (host.onAsk) flushNote().then(() => host.onAsk(selectionInfo())); });
  /** Show only the buttons whose host capability exists (re-run once the host's capabilities are known). */
  function refreshHostActions() {
    $("btn-export").hidden = !host.exportNotes;
    $("btn-ask").hidden = !host.onAsk;
  }
  refreshHostActions();

  /* ---------- viewport messages ---------- */
  function message(msg) {
    $("vp-msg-text").textContent = msg || "";
    $("vp-msg").classList.toggle("show", !!msg);
  }
  function fail(msg) {
    window.__inspectorError = msg;
    message(msg);
    $("gl").style.visibility = "hidden";
    $("vp-note").style.display = "none";
    console.warn("[inspector] " + msg);
  }

  /* ---------- loading a data model ---------- */
  function load(payload) {
    // An edit still in its debounce window belongs to what was on screen when it was typed: send it now,
    // against its own revision and session (it then lists as stale against the new revision, which is right).
    flushNote();
    const sameAsset = P && payload && P.asset_name === payload.asset_name && sessionOf(P) === sessionOf(payload);
    P = payload;
    state.loaded = true;
    state.revision = P.revision || null;
    if (!sameAsset) { state.selected = null; state.userMoved = false; }
    if (state.selected != null && !P.nodes.some((m) => m.id === state.selected)) state.selected = null;
    if (state.lod >= lodLevels().length) state.lod = 0;
    if (!modeAvailable(state.mode)) state.mode = "lit";
    message("");
    renderHeader(); renderLodButtons(); renderNodes(); renderGauges(); renderQa(); refreshCollButton(); refreshModeButtons();
    setNotes(notes.map((n) => Object.assign({}, n, { stale: false })));
    if (gl) gl.setModel(P);
  }

  /** Agent-driven view changes (from a tool result). Unknown or unavailable values are ignored. */
  function setView(v) {
    if (!v) return;
    if (v.lod != null) setLod(Number(v.lod));
    if (v.channel) setMode(v.channel);
    if (v.preset) setPreset(v.preset);
    if (v.wireframe != null) setWire(v.wireframe);
    if (v.colliders != null) setColl(v.colliders);
    if ("node" in v) selectNode(v.node, false); // null = whole asset
  }

  function setLayout(layout, height) {
    const root = document.documentElement;
    root.dataset.layout = layout;
    if (layout === "inline") root.style.setProperty("--app-h", height + "px");
    else root.style.removeProperty("--app-h");
    if (gl) gl.resize();
  }

  /* ---------- three.js ---------- */
  function startThree(THREE) {
    try {
      gl = createGl(THREE, gltfLoaderFor(THREE));
    } catch (e) {
      gl = null;
      fail("WebGL is unavailable in this browser, so the 3D viewport cannot render. The other panels still work.");
      return false;
    }
    state.glOk = true;
    if (P) gl.setModel(P);
    return true;
  }

  function createGl(THREE, GLTFLoader) {
    const canvas = $("gl");
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
    scene.add(camera);
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(0.5, 1, 1); camera.add(key); camera.add(key.target); key.target.position.set(0, 0, -1);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f96, 0.8));
    try {
      const envScene = new THREE.Scene();
      const eg = new THREE.SphereGeometry(10, 24, 16), cols = [], pa = eg.attributes.position;
      for (let i = 0; i < pa.count; i++) { const t = (pa.getY(i) / 10 + 1) / 2, v = 0.25 + 0.75 * t; cols.push(v, v, v); }
      eg.setAttribute("color", new THREE.Float32BufferAttribute(cols, 3));
      envScene.add(new THREE.Mesh(eg, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide })));
      const pm = new THREE.PMREMGenerator(renderer);
      scene.environment = pm.fromScene(envScene, 0.04).texture;
    } catch (e) { console.warn("[inspector] environment map unavailable: " + e.message); }

    // Until a model arrives (the MCP App starts the viewport first) use a 1 m placeholder box, so helpers and
    // the camera never see an empty (infinite) box.
    let bounds = new THREE.Box3(new THREE.Vector3(-0.5, 0, -0.5), new THREE.Vector3(0.5, 1, 0.5));
    let center = bounds.getCenter(new THREE.Vector3()), radius = 1;
    let dirty = true;
    // The loaded GLB: per level, [{node, mesh}] with the GLB's own material kept as mesh.userData.gltfMaterial;
    // hulls: [mesh]. Debug channels swap mesh.material for an override and swap the GLB material back for "lit".
    let levelMeshes = [], hullMeshes = [], gltfScene = null, overrides = [], loadToken = 0;

    const checker = (function () {
      const c = document.createElement("canvas"); c.width = c.height = 512;
      const x = c.getContext("2d");
      for (let i = 0; i < 16; i++) for (let j = 0; j < 16; j++) { x.fillStyle = (i + j) % 2 ? "#e8e8e8" : "#3a5fcd"; x.fillRect(i * 32, j * 32, 32, 32); }
      x.fillStyle = "#c0392b"; x.font = "bold 28px sans-serif"; x.fillText("0,0", 6, 506);
      x.fillStyle = "#1e8449"; x.fillText("1,1", 462, 34);
      const t = new THREE.CanvasTexture(c); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.colorSpace = THREE.SRGBColorSpace;
      t.flipY = false; // glTF UVs have a top-left origin: "0,0" stays at the bottom-left of UV space as in the package
      return t;
    })();
    // Raw texel view of one texture: c.rgb (or one channel) exactly as stored. sRGB textures (albedo) are decoded to
    // linear when sampled, so they are re-encoded for display; data textures (ORM, normal) are shown as they are.
    const RAW_VS = "varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }";
    const RAW_FS = "uniform sampler2D map; uniform int chan; uniform bool srgb; varying vec2 vUv; void main(){ vec4 c = texture2D(map, vUv); vec3 o = chan == 0 ? c.rgb : vec3(chan == 1 ? c.r : (chan == 2 ? c.g : c.b)); gl_FragColor = srgb ? linearToOutputTexel(vec4(o, 1.0)) : vec4(o, 1.0); }";
    const CHAN = { albedo: 0, orm: 0, normal: 0, occ: 1, rough: 2, metal: 3 };
    const SLOT = { albedo: "map", orm: "roughnessMap", occ: "aoMap", rough: "roughnessMap", metal: "metalnessMap", normal: "normalMap" };
    function keep(m) { overrides.push(m); return m; }
    function overrideFor(mode, mesh, nodeInfo) {
      const orig = mesh.userData.gltfMaterial;
      if (mode === "lit") return orig;
      if (mode === "clay") return keep(new THREE.MeshStandardMaterial({ color: 0xc9c4bc, roughness: 0.85, metalness: 0 }));
      if (mode === "prim") return keep(new THREE.MeshBasicMaterial({ color: new THREE.Color(nodeInfo ? nodeInfo.color : "#888888") }));
      const hasUv = !!mesh.geometry.attributes.uv;
      if (mode === "uv") return keep(hasUv ? new THREE.MeshBasicMaterial({ map: checker }) : new THREE.MeshBasicMaterial({ color: 0x808080 }));
      const tex = hasUv && orig ? orig[SLOT[mode]] : null;
      if (!tex) return keep(new THREE.MeshBasicMaterial({ color: 0x808080 }));
      return keep(new THREE.ShaderMaterial({
        uniforms: { map: { value: tex }, chan: { value: CHAN[mode] }, srgb: { value: tex.colorSpace === THREE.SRGBColorSpace } },
        vertexShader: RAW_VS, fragmentShader: RAW_FS }));
    }

    const wireGroup = new THREE.Group(), selGroup = new THREE.Group(), collGroup = new THREE.Group(), helpers = new THREE.Group();
    scene.add(wireGroup, selGroup, collGroup, helpers);
    function disposeChildren(g) {
      while (g.children.length) {
        const c = g.children.pop();
        if (c.geometry && c.userData.own) c.geometry.dispose();
        if (c.material && c.userData.ownMat) c.material.dispose();
      }
    }
    function currentParts() { return levelMeshes[state.lod] || []; }
    function rebuild() {
      for (const m of overrides) m.dispose();
      overrides = [];
      const mode = modeAvailable(state.mode) ? state.mode : "lit";
      levelMeshes.forEach((parts, i) => parts.forEach(({ mesh, node }) => {
        mesh.visible = i === state.lod;
        mesh.material = overrideFor(mode, mesh, node != null ? P.nodes.find((n) => n.id === node) : null);
      }));
      rebuildWire(); rebuildSel(); dirty = true;
    }
    function rebuildWire() {
      disposeChildren(wireGroup);
      if (state.wire) {
        const wm = new THREE.LineBasicMaterial({ color: cssColor(THREE, "--wire", "#1b1f24"), transparent: true, opacity: 0.45 });
        currentParts().forEach(({ mesh }) => {
          const ls = new THREE.LineSegments(new THREE.WireframeGeometry(mesh.geometry), wm);
          ls.applyMatrix4(mesh.matrixWorld); ls.userData.own = true; wireGroup.add(ls);
        });
      }
      dirty = true;
    }
    function rebuildSel() {
      disposeChildren(selGroup);
      dirty = true;
      if (!P || state.selected === null) return;
      const ac = cssColor(THREE, "--accent", "#2f5fd0");
      for (const { mesh, node } of currentParts()) {
        if (node !== state.selected) continue;
        const ov = new THREE.Mesh(mesh.geometry, new THREE.MeshBasicMaterial({ color: ac, transparent: true, opacity: 0.4, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 }));
        ov.applyMatrix4(mesh.matrixWorld); ov.userData.ownMat = true; selGroup.add(ov);
        const box = new THREE.Box3().setFromObject(mesh);
        const bh = new THREE.Box3Helper(box, ac); bh.material.depthTest = false; bh.userData.ownMat = true; selGroup.add(bh);
      }
    }
    function rebuildColliders() {
      disposeChildren(collGroup);
      dirty = true;
      if (!state.coll) return;
      const col = cssColor(THREE, "--collider", "#c46a12");
      hullMeshes.forEach((h) => {
        const m = new THREE.Mesh(h.geometry, new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: 0.22, depthWrite: false, side: THREE.DoubleSide }));
        m.applyMatrix4(h.matrixWorld); m.userData.ownMat = true; collGroup.add(m);
        const l = new THREE.LineSegments(new THREE.EdgesGeometry(h.geometry, 20), new THREE.LineBasicMaterial({ color: col }));
        l.applyMatrix4(h.matrixWorld); l.userData.own = true; l.userData.ownMat = true; collGroup.add(l);
      });
    }
    function buildHelpers() {
      disposeChildren(helpers);
      const half = Math.max(Math.abs(bounds.min.x), Math.abs(bounds.max.x), Math.abs(bounds.min.z), Math.abs(bounds.max.z));
      const size = Math.min(200, Math.max(4, 2 * Math.ceil(half + 1)));
      const grid = new THREE.GridHelper(size, size, cssColor(THREE, "--grid-center", "#7b8590"), cssColor(THREE, "--grid", "#a9b1ba"));
      grid.userData.own = true; grid.userData.ownMat = true; helpers.add(grid);
      /* 1 m reference bar at the +X/+Z corner of the bounds, with end ticks */
      const bx = bounds.max.x + 0.3, bz = bounds.max.z + 0.3, p = [bx, 0, bz, bx, 1, bz, bx - 0.08, 0, bz, bx + 0.08, 0, bz, bx - 0.08, 1, bz, bx + 0.08, 1, bz];
      const rg = new THREE.BufferGeometry(); rg.setAttribute("position", new THREE.Float32BufferAttribute(p, 3));
      const rl = new THREE.LineSegments(rg, new THREE.LineBasicMaterial({ color: cssColor(THREE, "--accent", "#2f5fd0"), depthTest: false }));
      rl.userData.own = true; rl.userData.ownMat = true; rl.renderOrder = 5; helpers.add(rl);
      dirty = true;
    }

    /* camera: compact orbit / pan / zoom controller (no external OrbitControls) */
    const ctl = { target: center.clone(), theta: 0.6, phi: 1.1, radius: 5 };
    const vp = $("viewport");
    function fitDistance() {
      const vf = camera.fov * Math.PI / 180, hf = 2 * Math.atan(Math.tan(vf / 2) * camera.aspect);
      return radius / Math.sin(Math.min(vf, hf) / 2) * 1.1;
    }
    function applyCamera() {
      const sp = Math.sin(ctl.phi);
      camera.position.set(ctl.target.x + ctl.radius * sp * Math.sin(ctl.theta), ctl.target.y + ctl.radius * Math.cos(ctl.phi), ctl.target.z + ctl.radius * sp * Math.cos(ctl.theta));
      camera.up.set(0, 1, 0); camera.lookAt(ctl.target);
      camera.near = Math.max(ctl.radius / 500, 0.001); camera.far = ctl.radius * 100 + radius * 10;
      camera.updateProjectionMatrix(); dirty = true;
    }
    /* Presets use the pipeline contact-sheet directions. In Y-up three.js space: front = camera on +Z,
       right = camera on +X, top = camera on +Y (front of the asset at screen bottom). */
    const PRESET_ANGLES = { front: [0, Math.PI / 2], right: [Math.PI / 2, Math.PI / 2], top: [0, 0.0001], persp: [0.6, 1.1] };
    function preset(name) {
      const p = PRESET_ANGLES[name] || PRESET_ANGLES.persp; ctl.theta = p[0]; ctl.phi = p[1]; ctl.target.copy(center); ctl.radius = fitDistance();
      applyCamera();
    }
    function resize() {
      const r = vp.getBoundingClientRect(), w = Math.max(1, Math.floor(r.width)), h = Math.max(1, Math.floor(r.height));
      renderer.setSize(w, h, false); camera.aspect = w / h;
      if (!state.userMoved) preset(state.preset); else applyCamera();
    }
    window.__resize = resize;
    if (window.ResizeObserver) new ResizeObserver(resize).observe(vp); else window.addEventListener("resize", resize);

    const ptrs = {};
    let down = null;
    function panBy(dx, dy) {
      const k = 2 * ctl.radius * Math.tan(camera.fov * Math.PI / 360) / canvas.clientHeight;
      const r = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0), u = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      ctl.target.addScaledVector(r, -dx * k).addScaledVector(u, dy * k);
    }
    function zoomBy(f) { ctl.radius = Math.min(Math.max(ctl.radius * f, radius * 0.05), radius * 60); }
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    canvas.addEventListener("pointerdown", (e) => {
      canvas.setPointerCapture(e.pointerId); ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      down = Object.keys(ptrs).length === 1 ? { x: e.clientX, y: e.clientY, moved: 0, button: e.button, pan: e.button !== 0 || e.shiftKey } : null;
      canvas.focus({ preventScroll: true });
    });
    canvas.addEventListener("pointermove", (e) => {
      const p = ptrs[e.pointerId]; if (!p) return;
      const ids = Object.keys(ptrs), dx = e.clientX - p.x, dy = e.clientY - p.y;
      if (ids.length === 1) {
        if (down) down.moved += Math.abs(dx) + Math.abs(dy);
        if (down && down.pan) panBy(dx, dy);
        else { ctl.theta -= 2 * Math.PI * dx / canvas.clientHeight; ctl.phi = Math.min(Math.max(ctl.phi - 2 * Math.PI * dy / canvas.clientHeight, 0.0001), Math.PI - 0.0001); }
      } else if (ids.length === 2) {
        const o = ptrs[ids[0] === String(e.pointerId) ? ids[1] : ids[0]];
        const d0 = Math.hypot(p.x - o.x, p.y - o.y), d1 = Math.hypot(e.clientX - o.x, e.clientY - o.y);
        if (d1 > 0 && d0 > 0) zoomBy(d0 / d1);
        panBy(dx / 2, dy / 2);
      }
      p.x = e.clientX; p.y = e.clientY; state.userMoved = true; applyCamera();
    });
    function endPtr(e) {
      if (!ptrs[e.pointerId]) return;
      delete ptrs[e.pointerId];
      if (down && e.type === "pointerup" && down.moved < 4 && down.button === 0 && !down.pan) pick(e);
      down = null;
    }
    canvas.addEventListener("pointerup", endPtr); canvas.addEventListener("pointercancel", endPtr);
    canvas.addEventListener("wheel", (e) => { e.preventDefault(); zoomBy(Math.exp(e.deltaY * 0.0012)); state.userMoved = true; applyCamera(); }, { passive: false });
    canvas.addEventListener("keydown", (e) => {
      let h = true;
      if (e.key === "ArrowLeft") ctl.theta -= 0.1; else if (e.key === "ArrowRight") ctl.theta += 0.1;
      else if (e.key === "ArrowUp") ctl.phi = Math.max(0.0001, ctl.phi - 0.1); else if (e.key === "ArrowDown") ctl.phi = Math.min(Math.PI - 0.0001, ctl.phi + 0.1);
      else if (e.key === "+" || e.key === "=") zoomBy(0.9); else if (e.key === "-") zoomBy(1.1); else h = false;
      if (h) { e.preventDefault(); state.userMoved = true; applyCamera(); }
    });
    const ray = new THREE.Raycaster();
    function pick(e) {
      if (!P) return;
      const r = canvas.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2((e.clientX - r.left) / r.width * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
      const parts = currentParts().filter((p) => p.node != null);
      const hit = ray.intersectObjects(parts.map((p) => p.mesh), false)[0];
      const part = hit ? parts.find((p) => p.mesh === hit.object) : null;
      selectNode(part ? part.node : null, true);
    }
    window.__pick = (x, y) => pick({ clientX: x, clientY: y });
    /** Screen position (client px) of a node's bounding-box centre at the current LOD: lets tests click it like a user. */
    window.__nodeScreenPoint = (id) => {
      const part = currentParts().find((p) => p.node === id); if (!part) return null;
      const v = new THREE.Box3().setFromObject(part.mesh).getCenter(new THREE.Vector3()).project(camera), r = canvas.getBoundingClientRect();
      return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height };
    };
    /** For tests: the loaded meshes' GLB data (normals, material) per node at the current LOD. */
    window.__inspectorMeshes = () => currentParts().map(({ node, mesh }) => {
      const n = mesh.geometry.attributes.normal, g = mesh.userData.gltfMaterial;
      return { node, vertices: mesh.geometry.attributes.position.count, normals: n ? Array.from(n.array) : null,
        positions: Array.from(mesh.geometry.attributes.position.array),
        material: g ? { type: g.type, name: g.name, color: g.color ? g.color.getHexString() : null, map: !!g.map, normalMap: !!g.normalMap,
          aoMap: !!g.aoMap, roughnessMap: !!g.roughnessMap, metalness: g.metalness, roughness: g.roughness,
          mapSize: g.map && g.map.image ? [g.map.image.width, g.map.image.height] : null } : null };
    });

    function disposeModel() {
      if (gltfScene) {
        scene.remove(gltfScene);
        gltfScene.traverse((o) => {
          if (o.geometry) o.geometry.dispose();
          const m = o.userData && o.userData.gltfMaterial;
          if (m) { for (const k in m) if (m[k] && m[k].isTexture) m[k].dispose(); m.dispose(); }
        });
      }
      for (const m of overrides) m.dispose();
      overrides = []; levelMeshes = []; hullMeshes = []; gltfScene = null;
    }
    function nodeObject(gltf, index) {
      // GLTFLoader records which glTF node each object came from; names are not reliable (it sanitises them).
      let found = null;
      gltf.scene.traverse((o) => { const a = gltf.parser.associations.get(o); if (!found && a && a.nodes === index) found = o; });
      return found;
    }
    function meshOf(obj) {
      let m = null;
      if (obj) obj.traverse((o) => { if (!m && o.isMesh) m = o; });
      return m;
    }
    function setModel(model) {
      const token = ++loadToken;
      disposeChildren(wireGroup); disposeChildren(selGroup); disposeChildren(collGroup);
      disposeModel();
      dirty = true;
      return Promise.resolve(host.loadGlb(model)).then((buffer) => parseGlb(THREE, GLTFLoader, buffer)).then((gltf) => {
        if (token !== loadToken || model !== P) return; // superseded by a newer model
        gltfScene = gltf.scene;
        gltfScene.updateMatrixWorld(true);
        const prepare = (index) => {
          const mesh = meshOf(nodeObject(gltf, index));
          if (!mesh) throw new Error("review GLB has no mesh for node " + index);
          mesh.userData.gltfMaterial = mesh.material;
          return mesh;
        };
        levelMeshes = model.levels.map((level) => level.parts.map((p) => ({ node: p.node, mesh: prepare(p.glb) })));
        hullMeshes = model.colliders.map((c) => { const m = prepare(c.glb); m.visible = false; return m; });
        scene.add(gltfScene);
        bounds = new THREE.Box3();
        (levelMeshes[0] || []).forEach(({ mesh }) => bounds.expandByObject(mesh));
        if (bounds.isEmpty()) bounds.set(new THREE.Vector3(-0.5, 0, -0.5), new THREE.Vector3(0.5, 1, 0.5));
        center = bounds.getCenter(new THREE.Vector3());
        radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.05);
        theme(); rebuild(); rebuildColliders();
        if (!state.userMoved) preset(state.preset); else applyCamera();
        resize();
        state.glbLoaded = true;
      }).catch((e) => {
        if (token === loadToken) fail("The asset could not be loaded: " + (e && e.message ? e.message : e));
      });
    }
    function theme() { renderer.setClearColor(cssColor(THREE, "--viewport-bg", "#e4e7eb"), 1); buildHelpers(); rebuildWire(); rebuildSel(); rebuildColliders(); dirty = true; }
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", theme);

    theme(); resize();
    function frame() {
      if (dirty) {
        dirty = false; renderer.render(scene, camera);
        if (state.glbLoaded && !window.__inspectorReady) window.__inspectorReady = true;
      }
      requestAnimationFrame(frame);
    }
    frame();
    return {
      setModel, rebuild, rebuildWire, rebuildColliders, preset, resize, theme,
      applySelection() { rebuildSel(); },
    };
  }

  selectNode(null, false);
  refreshModeButtons();
  updateNotesStatus();

  return {
    state, load, setView, setNotes, setQa(q) { qa = q; renderQa(); }, selectNode, setMode, setLod, setPreset, setWire, setColl,
    startThree, fail, message, flushNote, setLayout, selectionInfo, exportDoc, refreshHostActions,
    theme() { if (gl) gl.theme(); },
    get payload() { return P; },
    get notes() { return notes; },
  };
}
