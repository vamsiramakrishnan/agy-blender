// Standalone host: the inspector.html file `agy3d prop` writes into a package, opened from disk in an
// ordinary browser. Data is the embedded JSON payload; three.js is inlined by the package writer (or
// loaded from the pinned CDN URL); notes stay in this page (and in localStorage where the browser
// allows it) and leave it as an exported JSON file, the format `read_notes` parses.
import { createInspector, noteKey, NOTE_ASSET } from "./viewer.js";

const P = JSON.parse(document.getElementById("payload").textContent);
const NOTES_KEY = "antigravity3d.notes:" + P.asset_name;
let storageOk = true;

function loadStored() {
  try {
    const raw = window.localStorage.getItem(NOTES_KEY);
    const doc = raw ? JSON.parse(raw) || {} : {};
    // stored as {nodeKey: text} (the original format) or {nodeKey: {text, revision}}
    return Object.keys(doc).map((k) => {
      const v = doc[k], text = typeof v === "string" ? v : String((v && v.text) || "");
      return { id: "local-" + k, node_id: k === NOTE_ASSET ? null : k, text, revision: (v && v.revision) || P.revision, updated_at: (v && v.updated_at) || "" };
    }).filter((n) => n.text.trim());
  } catch (e) {
    storageOk = false;
    return [];
  }
}
let entries = loadStored();

function persist() {
  const doc = {};
  for (const n of entries) if (n.text.trim()) doc[noteKey(n.node_id) + (n.revision && n.revision !== P.revision ? "@" + n.revision : "")] = { text: n.text, revision: n.revision, updated_at: n.updated_at };
  try { window.localStorage.setItem(NOTES_KEY, JSON.stringify(doc)); } catch (e) { storageOk = false; }
}

const host = {
  kind: "standalone",
  saveDelayMs: 0,
  get notesWhere() { return storageOk ? "Saved in this browser." : "Browser storage unavailable: export before closing."; },
  notes: {
    save(nodeId, text, revision) {
      const key = noteKey(nodeId), now = new Date().toISOString();
      const i = entries.findIndex((n) => noteKey(n.node_id) === key && n.revision === revision);
      if (i >= 0) entries[i] = Object.assign({}, entries[i], { text, updated_at: now });
      else entries.push({ id: "local-" + key, node_id: nodeId, text, revision, updated_at: now });
      entries = entries.filter((n) => n.text.trim() || (noteKey(n.node_id) === key && n.revision === revision));
      persist();
      return entries.slice();
    },
  },
  exportNotes(doc, text) {
    try {
      const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url; a.download = (P.asset_name.replace(/[^\w.-]+/g, "_") || "asset") + "_review_notes.json";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (e) { console.warn("download failed", e); }
  },
  resolveTexture(ref) { return typeof ref === "string" ? ref : Promise.reject(new Error("texture not embedded")); },
  onSelection: null,
  onNotesChanged: null,
  onAsk: null,
  onRefresh: null,
};

const ui = createInspector(host);
ui.load(P);
ui.setNotes(entries);

function loadThree() {
  const inline = document.getElementById("three-src");
  if (inline) {
    const url = URL.createObjectURL(new Blob([inline.textContent], { type: "text/javascript" }));
    return import(/* webpackIgnore: true */ url);
  }
  return Promise.race([
    import(/* webpackIgnore: true */ P.three_url),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timed out after 20 s")), 20000)),
  ]);
}

loadThree().then((THREE) => ui.startThree(THREE)).catch((e) => {
  ui.fail("The 3D viewport could not load three.js" + (P.three_url ? " (" + P.three_url + ")" : "") + ": " + (e && e.message ? e.message : e) +
    ". Budget, node, QA and notes panels still work. For offline use rebuild the inspector with three_js_source.");
});
