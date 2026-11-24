import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { spawn } from 'node:child_process';

const SITE_DIR = path.resolve('site');
const OUT_DIR = '/tmp/sj-verify';
fs.mkdirSync(OUT_DIR, { recursive: true });

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.glb': 'model/gltf-binary',
  '.json': 'application/json',
};

// Static server that serves ./site/ at BOTH / and /straitjacket/
function startServer() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const urlPath = (req.url || '/').split('?')[0];
      let rel = urlPath;
      if (rel.startsWith('/straitjacket/')) {
        rel = rel.slice('/straitjacket'.length);
      } else if (rel === '/straitjacket') {
        res.writeHead(302, { Location: '/straitjacket/' });
        res.end();
        return;
      }
      if (rel === '/' || rel === '') rel = '/index.html';
      const filePath = path.join(SITE_DIR, decodeURIComponent(rel));
      if (!filePath.startsWith(SITE_DIR) || !fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
        res.writeHead(404);
        res.end('Not found: ' + urlPath);
        return;
      }
      const ext = path.extname(filePath);
      res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
      fs.createReadStream(filePath).pipe(res);
    });
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({ server, port });
    });
  });
}

class CDPClient {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.id = 1;
    this.pending = new Map();
    this.listeners = new Map();
  }
  async connect() {
    await new Promise((resolve, reject) => {
      this.ws.addEventListener('open', resolve, { once: true });
      this.ws.addEventListener('error', reject, { once: true });
    });
    this.ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result);
      } else if (msg.method) {
        const handlers = this.listeners.get(msg.method) || [];
        for (const fn of handlers) fn(msg.params);
      }
    });
  }
  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(fn);
  }
  send(method, params = {}) {
    const id = this.id++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() {
    this.ws.close();
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function run() {
  const { server, port } = await startServer();
  const baseUrl = `http://127.0.0.1:${port}/straitjacket/`;
  const chromium = process.env.CHROMIUM_BIN || 'chromium';
  const debugPort = 9222 + Math.floor(Math.random() * 500);

  const chrome = spawn(
    chromium,
    [
      '--headless=new',
      '--no-sandbox',
      '--disable-gpu-sandbox',
      '--enable-webgl',
      '--ignore-gpu-blocklist',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader',
      `--remote-debugging-port=${debugPort}`,
      '--window-size=1440,900',
      'about:blank',
    ],
    { stdio: ['ignore', 'pipe', 'pipe'] }
  );

  let wsUrl = null;
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      const targets = await res.json();
      const page = targets.find((t) => t.type === 'page');
      if (page && page.webSocketDebuggerUrl) {
        wsUrl = page.webSocketDebuggerUrl;
        break;
      }
    } catch {
      await sleep(150);
    }
  }
  if (!wsUrl) throw new Error('Could not connect to Chromium CDP');

  const cdp = new CDPClient(wsUrl);
  await cdp.connect();

  const consoleErrors = [];
  const pageErrors = [];
  const externalRequests = [];
  const failedRequests = [];
  const allRequests = [];

  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Network.enable');

  cdp.on('Runtime.consoleAPICalled', (p) => {
    if (p.type === 'error' || p.type === 'warning') {
      const text = (p.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
      if (p.type === 'error') consoleErrors.push(text);
    }
  });
  cdp.on('Runtime.exceptionThrown', (p) => {
    pageErrors.push(p.exceptionDetails?.exception?.description || p.exceptionDetails?.text || 'Unknown exception');
  });
  cdp.on('Network.requestWillBeSent', (p) => {
    const u = p.request.url;
    if (u.startsWith('data:') || u.startsWith('blob:')) return;
    allRequests.push(u);
    if (!u.startsWith(`http://127.0.0.1:${port}/`)) {
      externalRequests.push(u);
    }
  });
  cdp.on('Network.responseReceived', (p) => {
    if (p.response.status >= 400) {
      failedRequests.push(`${p.response.status} ${p.response.url}`);
    }
  });
  cdp.on('Network.loadingFailed', (p) => {
    failedRequests.push(`FAILED ${p.errorText}`);
  });

  async function setViewport(width, height, mobile = false) {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width,
      height,
      deviceScaleFactor: mobile ? 2 : 1,
      mobile,
    });
    await sleep(250);
  }

  async function evalJS(expression) {
    const res = await cdp.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    return res.result?.value;
  }

  async function snap(name) {
    const { data } = await cdp.send('Page.captureScreenshot', { format: 'png' });
    const file = path.join(OUT_DIR, `${name}.png`);
    fs.writeFileSync(file, Buffer.from(data, 'base64'));
    console.log(`Captured ${file}`);
  }

  // 1) Load Desktop 1440x900 under /straitjacket/
  await setViewport(1440, 900, false);
  await cdp.send('Page.navigate', { url: baseUrl });

  // Wait up to 8s for all 5 GLBs + shader compile + loading curtain hide
  for (let i = 0; i < 40; i++) {
    const hidden = await evalJS(`document.querySelector('.loading-curtain')?.classList.contains('is-hidden')`);
    if (hidden) break;
    await sleep(200);
  }
  await sleep(600);

  const diag = await evalJS(`(() => {
    const canvas = document.querySelector('canvas');
    const curtain = document.querySelector('.loading-curtain');
    const docsLinks = Array.from(document.querySelectorAll('a[href*="./docs/"]')).map(a => a.getAttribute('href'));
    return {
      title: document.title,
      canvasW: canvas ? canvas.width : 0,
      canvasH: canvas ? canvas.height : 0,
      curtainHidden: curtain ? curtain.classList.contains('is-hidden') : null,
      scrollHeight: document.documentElement.scrollHeight,
      overflowX: document.documentElement.scrollWidth > window.innerWidth,
      docsLinksCount: docsLinks.length,
    };
  })()`);
  console.log('Desktop Diagnostics:', JSON.stringify(diag));

  async function scrollToAct(actIndex) {
    await evalJS(`(() => {
      const sections = Array.from(document.querySelectorAll('[data-act]'));
      const el = sections[${actIndex}];
      if (!el) return;
      const vh = window.innerHeight;
      const max = document.documentElement.scrollHeight - vh;
      let y = 0;
      if (${actIndex} === 0) y = 0;
      else if (${actIndex} === sections.length - 1) y = Math.min(max, el.offsetTop);
      else y = Math.max(0, Math.min(max, Math.round(el.offsetTop + el.offsetHeight * 0.5 - vh * 0.5)));
      window.scrollTo({ top: y, behavior: 'instant' });
      window.dispatchEvent(new Event('scroll'));
    })()`);
    await sleep(1000);
  }

  // Capture all 7 acts on Desktop
  const desktopActs = [
    ['desk_01_hero', 0],
    ['desk_02_flood', 1],
    ['desk_03_gates', 2],
    ['desk_04_anchors', 3],
    ['desk_05_prefix', 4],
    ['desk_06_harnesses', 5],
    ['desk_07_receipts', 6],
  ];

  for (const [name, idx] of desktopActs) {
    await scrollToAct(idx);
    await snap(name);
  }

  // Test interactive controls: click "Rewritten" in Act 4 and verify 3D refusal state
  await scrollToAct(3);
  await evalJS(`(() => {
    const btns = Array.from(document.querySelectorAll('button'));
    const rw = btns.find(b => b.textContent.includes('Rewritten'));
    if (rw) rw.click();
  })()`);
  await sleep(900);
  await snap('desk_04_anchors_refused');

  // Also test clicking "Bounded Retrieval" in Act 3
  await scrollToAct(2);
  await evalJS(`(() => {
    const btns = Array.from(document.querySelectorAll('button'));
    const ret = btns.find(b => b.textContent.includes('Bounded Retrieval'));
    if (ret) ret.click();
  })()`);
  await sleep(800);
  await snap('desk_03_gates_retrieve');

  // 2) Phone-width 390x844 verification
  await setViewport(390, 844, true);
  await evalJS(`window.dispatchEvent(new Event('resize'))`);
  await sleep(400);
  await scrollToAct(0);

  const mobileDiag = await evalJS(`(() => ({
    innerW: window.innerWidth,
    scrollW: document.documentElement.scrollWidth,
    overflowX: document.documentElement.scrollWidth > window.innerWidth,
  }))()`);
  console.log('Mobile Diagnostics:', JSON.stringify(mobileDiag));

  for (const [name, idx] of [
    ['mob_01_hero', 0],
    ['mob_02_flood', 1],
    ['mob_03_gates', 2],
    ['mob_06_harnesses', 5],
    ['mob_07_receipts', 6],
  ]) {
    await scrollToAct(idx);
    await snap(name);
  }

  // Also verify loading from root path '/' (proving relative base works from ANY path!)
  await setViewport(1440, 900, false);
  await cdp.send('Page.navigate', { url: `http://127.0.0.1:${port}/` });
  await sleep(1600);
  const rootTitle = await evalJS(`document.title`);
  console.log('Root-path check OK:', rootTitle);

  // Summary Report
  console.log('--- VERIFICATION REPORT ---');
  console.log(`Total requests: ${allRequests.length}`);
  console.log(`External requests: ${externalRequests.length}`, externalRequests);
  console.log(`Failed requests: ${failedRequests.length}`, failedRequests);
  console.log(`Console errors: ${consoleErrors.length}`, consoleErrors);
  console.log(`Page exceptions: ${pageErrors.length}`, pageErrors);

  cdp.close();
  chrome.kill('SIGTERM');
  server.close();

  if (externalRequests.length || failedRequests.length || consoleErrors.length || pageErrors.length) {
    process.exitCode = 1;
  }
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
