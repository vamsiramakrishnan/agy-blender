// Minimal MCP Apps *host* for tests (tests/test_inspector_app.py): the SDK's own AppBridge drives the
// inspector app in an iframe, exactly like a host does: ui/initialize with host context and
// capabilities, tool input / result notifications, tools/call forwarding, ui/update-model-context,
// ui/message, size changes. Built into host.bundle.js by ../build.mjs (committed, so the Python
// browser test needs no node_modules).
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";

const log = { calls: [], context: [], messages: [], sizes: [], initialized: false };
window.__hostLog = log;

/**
 * opts = { url, mode: "pane" | "inline", width, height, theme, variables, capabilities, maxHeight }
 * Tool calls are answered by window.serverCall(name, args) (exposed by the test).
 */
window.startHost = async function startHost(opts) {
  const frame = document.createElement("iframe");
  frame.id = "app";
  frame.setAttribute("sandbox", "allow-scripts"); // opaque origin: no shared storage, no same-origin access
  frame.style.width = opts.width + "px";
  frame.style.height = (opts.mode === "inline" ? 150 : opts.height) + "px"; // inline frames start at 150 px
  frame.style.border = "0";
  frame.style.display = "block";
  document.getElementById("slot").appendChild(frame);
  const hostContext = {
    theme: opts.theme, styles: { variables: opts.variables }, platform: "web", displayMode: "inline", locale: "en-US",
    timeZone: "UTC", ...(opts.mode === "inline" ? { containerDimensions: { maxHeight: opts.maxHeight || 800 } } : {}),
  };
  const bridge = new AppBridge(null, { name: "agy3d-test-host", version: "1.0.0" }, opts.capabilities, { hostContext });
  bridge.oncalltool = async (params) => {
    log.calls.push(params);
    return await window.serverCall(params.name, params.arguments || {});
  };
  bridge.onupdatemodelcontext = async (params) => { log.context.push(params); return {}; };
  bridge.onmessage = async (params) => { log.messages.push(params); return {}; };
  bridge.onsizechange = (p) => {
    log.sizes.push(p);
    if (opts.mode === "inline" && p.height) frame.style.height = Math.min(800, Math.ceil(p.height)) + "px";
  };
  const ready = new Promise((resolve) => { bridge.oninitialized = () => { log.initialized = true; resolve(); }; });
  // Listen before the app loads: the app sends ui/initialize as soon as its script runs. The frame's
  // WindowProxy stays the same across the navigation, so the transport can be bound to it now.
  await bridge.connect(new PostMessageTransport(frame.contentWindow, frame.contentWindow));
  frame.src = opts.url;
  await ready;
  window.__bridge = bridge;
  return true;
};

window.sendTool = async function sendTool(args, result) {
  await window.__bridge.sendToolInput({ arguments: args });
  await window.__bridge.sendToolResult(result);
  return true;
};

window.setTheme = async function setTheme(theme, variables) {
  await window.__bridge.sendHostContextChange({ theme, styles: { variables } });
  return true;
};
