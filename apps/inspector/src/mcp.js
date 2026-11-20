// MCP App host: the inspector inside Antigravity (or any MCP Apps host), built on the MCP Apps SDK.
//
// - The model-visible tool `open_inspector` delivers a small summary (session, revision, view
//   directives) as its tool result; every later call to it updates this view.
// - The review metadata ("scene") and the review GLB ("glb": geometry, normals, UVs, materials, textures,
//   built by the package's glTF writer) come from the app-only tool `inspector_payload` in chunks (the
//   stored CallToolResult of a model tool call is capped at 1 MiB).
// - Review notes are saved eagerly to the asset session through `inspector_save_note` (frames are
//   removed without teardown), QA through `inspector_qa_state`, and what the reviewer selected or
//   wrote is attached to the next user message with updateModelContext.
// - No browser storage, no network: three.js and the SDK are bundled into this document.
import * as THREE from "three";
import { App, applyDocumentTheme, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import { createInspector } from "./viewer.js";
import { base64ToArrayBuffer } from "./gltf.js";

const MAX_IN_FLIGHT = 3; // the host allows 4 concurrent calls per app; keep one free for note saves
let caps = {};
let current = { sessionId: null, revision: null }; // what the view shows
let lastSummary = null;
let loading = null;

const app = new App({ name: "antigravity-3d inspector", version: "1.0.0" }, {}, { autoResize: true });

async function callTool(name, args) {
  const res = await app.callServerTool({ name, arguments: args });
  if (res.isError) {
    const text = (res.content || []).filter((c) => c.type === "text").map((c) => c.text).join(" ");
    const err = new Error(text || name + " failed");
    err.structured = res.structuredContent;
    throw err;
  }
  return res.structuredContent || {};
}

/** Fetch one chunked part ("scene" metadata JSON or the base64 "glb") of a revision. */
async function fetchPart(sessionId, revision, part) {
  const first = await callTool("inspector_payload", { session_id: sessionId, revision, part, chunk: 0 });
  const chunks = new Array(first.chunks);
  chunks[0] = first.data;
  let next = 1;
  async function worker() {
    while (next < first.chunks) {
      const i = next++;
      chunks[i] = (await callTool("inspector_payload", { session_id: sessionId, revision, part, chunk: i })).data;
    }
  }
  await Promise.all(Array.from({ length: Math.min(MAX_IN_FLIGHT, Math.max(0, first.chunks - 1)) }, worker));
  return chunks.join("");
}

function modelContextText(info) {
  const s = lastSummary || {};
  const lines = [`3D inspector: ${info.asset_name || s.asset || "asset"} (session ${s.session_id}, revision ${info.revision}).`];
  if (info.node) lines.push(`Reviewer selected node "${info.node.id}" (${info.node.prim}, ${info.node.tris} tris, material ${info.node.mat || "default"}).`);
  else lines.push("Reviewer selection: whole asset.");
  if (info.edited) {
    const where = info.edited.node_id == null ? "the whole asset" : `node "${info.edited.node_id}"`;
    lines.push(info.edited.text.trim() ? `Reviewer just wrote on ${where}: "${info.edited.text.trim()}"` : `Reviewer cleared the note on ${where}.`);
  } else if (info.note) {
    lines.push(`Note on this selection: "${info.note}"`);
  }
  lines.push(`Review notes: ${info.notes_total} current, ${info.notes_stale} stale. The review_notes tool lists them all.`);
  return lines.join("\n");
}

let contextTimer = null;
function pushContext(info) {
  if (!caps.updateModelContext) return;
  clearTimeout(contextTimer);
  // Only in response to a reviewer action (selection, a note); debounced so typing sends one update.
  contextTimer = setTimeout(() => {
    app.updateModelContext({ content: [{ type: "text", text: modelContextText(info) }] })
      .catch((e) => console.warn("[inspector] updateModelContext failed: " + e.message));
  }, 250);
}

const host = {
  kind: "mcp",
  notesWhere: "Saved to the asset session.",
  notes: {
    // Session and revision are the ones the edit was typed under (bound by the viewer), never "whatever is
    // loaded now": a reload inside the debounce window must not move a note to other geometry or another asset.
    async save(nodeId, text, revision, sessionId) {
      if (!sessionId) throw new Error("no asset session");
      const r = await callTool("inspector_save_note", { session_id: sessionId, node_id: nodeId == null ? "" : nodeId, text, revision: revision || "" });
      return r.notes;
    },
  },
  exportNotes: null, // set after connect when the host offers ui/download-file
  loadGlb(model) {
    return fetchPart(model.session_id, model.revision, "glb").then(base64ToArrayBuffer);
  },
  onSelection(info) { pushContext(info); },
  onNotesChanged(info) { pushContext(info); },
  onAsk: null, // set after connect when the host offers ui/message
  onRefresh() { refresh(); },
};

const ui = createInspector(host);
ui.startThree(THREE);
window.__inspectorApp = app;
window.__inspectorUi = ui;

function applyContext(ctx) {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles && ctx.styles.variables) applyHostStyleVariables(ctx.styles.variables);
  const maxH = ctx.containerDimensions && ctx.containerDimensions.maxHeight;
  // Inline frames grow with the reported content height (up to 800 px), so the layout gets a fixed
  // height there; side pane and global view fill their container.
  if (maxH) ui.setLayout("inline", Math.max(320, Math.min(maxH, 600)));
  else ui.setLayout("fill");
  ui.theme();
}

async function loadRevision(summary) {
  const sid = summary.session_id, rev = summary.revision;
  ui.message(`Loading ${summary.asset || "asset"}…`);
  const text = await fetchPart(sid, rev, "scene");
  const payload = JSON.parse(text);
  current = { sessionId: sid, revision: payload.revision };
  ui.load(payload);
  const [notes, qa] = await Promise.all([
    callTool("inspector_list_notes", { session_id: sid }),
    callTool("inspector_qa_state", { session_id: sid }),
  ]);
  ui.setNotes(notes.notes);
  ui.setQa(qa.qa);
}

async function show(summary) {
  lastSummary = summary;
  if (!summary || !summary.session_id) {
    current = { sessionId: null, revision: null };
    ui.message((summary && summary.message) || "No asset session is open. Ask the agent to open one.");
    return;
  }
  if (summary.session_id !== current.sessionId || summary.revision !== current.revision) {
    const job = loadRevision(summary);
    loading = job;
    try { await job; } finally { if (loading === job) loading = null; }
  } else if (summary.notes) {
    callTool("inspector_list_notes", { session_id: summary.session_id }).then((n) => ui.setNotes(n.notes)).catch(() => {});
  }
  ui.setView(summary.view);
}

async function refresh() {
  if (!current.sessionId && !(lastSummary && lastSummary.session_id)) return;
  const sid = current.sessionId || lastSummary.session_id;
  try {
    const st = await callTool("inspector_qa_state", { session_id: sid });
    ui.setQa(st.qa);
    if (st.revision !== current.revision) await show(Object.assign({}, lastSummary, { session_id: sid, revision: st.revision, view: null }));
    const notes = await callTool("inspector_list_notes", { session_id: sid });
    ui.setNotes(notes.notes);
  } catch (e) {
    ui.message("Refresh failed: " + e.message);
  }
}

function report(e) {
  console.error("[inspector] " + (e && e.stack ? e.stack : e));
  ui.message("The inspector could not load the asset: " + (e && e.message ? e.message : e));
}

// Handlers first, then connect, so no notification is missed.
app.ontoolinput = () => { if (!ui.state.loaded) ui.message("Opening the inspector…"); };
app.ontoolresult = (result) => {
  if (result.isError) {
    const text = (result.content || []).filter((c) => c.type === "text").map((c) => c.text).join(" ");
    ui.message(text || "The inspector tool failed.");
    return;
  }
  if (!result.structuredContent) {
    // A refused call (bad node id, no session, ...) comes back as one ERROR line without a summary.
    const text = (result.content || []).filter((c) => c.type === "text").map((c) => c.text).join(" ");
    if (!ui.state.loaded) ui.message(text || "No inspector data.");
    return;
  }
  show(result.structuredContent).catch(report);
};
app.ontoolcancelled = () => { if (!ui.state.loaded) ui.message("Cancelled."); };
app.onhostcontextchanged = (ctx) => applyContext(app.getHostContext() || ctx);
app.onteardown = async () => { await ui.flushNote(); return {}; };

app.connect().then(() => {
  caps = app.getHostCapabilities() || {};
  applyContext(app.getHostContext());
  if (caps.downloadFile) {
    host.exportNotes = (doc, text) => app.downloadFile({ contents: [{ type: "resource", resource: {
      uri: "file:///" + (doc.asset_name.replace(/[^\w.-]+/g, "_") || "asset") + "_review_notes.json", mimeType: "application/json", text } }] });
  }
  if (caps.message) {
    host.onAsk = (info) => app.sendMessage({ role: "user", content: [{ type: "text", text:
      `Please address the review notes on ${info.asset_name} (revision ${info.revision}): read them with review_notes.` }] });
  }
  ui.refreshHostActions();
}).catch(report);
