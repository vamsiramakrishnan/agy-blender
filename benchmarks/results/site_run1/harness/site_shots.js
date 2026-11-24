// site_shots.js SITE_DIR OUT_DIR [--device phone] [--n 10] [--page index.html]: open a static site as a visitor would and
// scroll through it. Serves SITE_DIR on a local port, blocks every request to another host (the page is tested as it
// will be hosted: offline except for what it carries), saves a screenshot at N even scroll positions and a contact
// sheet, and prints console errors, failed and blocked requests, and whether a WebGL canvas is drawing.
const http = require('http'); const fs = require('fs'); const path = require('path'); const { execFileSync } = require('child_process');
const { chromium } = require('playwright');
const SITE = path.resolve(process.argv[2]); const out = path.resolve(process.argv[3]);
const arg = (n, d) => { const i = process.argv.indexOf('--' + n); return i < 0 ? d : process.argv[i + 1]; };
const device = arg('device', 'desktop'); const N = parseInt(arg('n', '10'), 10); const entry = arg('page', 'index.html');
const allowHosts = (arg('allow', '') || '').split(',').filter(Boolean);
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.glb': 'model/gltf-binary', '.gltf': 'model/gltf+json', '.bin': 'application/octet-stream', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.ktx2': 'image/ktx2', '.wasm': 'application/wasm', '.hdr': 'application/octet-stream', '.mp4': 'video/mp4', '.ico': 'image/x-icon', '.txt': 'text/plain', '.map': 'application/json' };
const server = http.createServer((req, res) => {
  let rel = decodeURIComponent(req.url.split('?')[0]); if (rel.endsWith('/')) rel += 'index.html';
  const file = path.join(SITE, rel);
  if (!file.startsWith(SITE) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end('not found'); return; }
  res.writeHead(200, { 'content-type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream', 'cache-control': 'no-store' });
  fs.createReadStream(file).pipe(res);
});
(async () => {
  fs.mkdirSync(out, { recursive: true });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const view = device === 'phone' ? { width: 390, height: 844 } : { width: 1440, height: 900 };
  const ctx = await browser.newContext({ viewport: view, deviceScaleFactor: 1, isMobile: device === 'phone', hasTouch: device === 'phone' });
  const page = await ctx.newPage();
  const problems = []; const external = new Map();
  await page.route('**/*', (route) => {
    const u = new URL(route.request().url());
    if (u.origin + '/' === base || u.protocol === 'data:' || u.protocol === 'blob:') return route.continue();
    external.set(u.host, (external.get(u.host) || 0) + 1);
    if (allowHosts.includes(u.host)) return route.continue();
    return route.abort();
  });
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text().slice(0, 300)); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message.slice(0, 300)));
  page.on('response', (r) => { if (r.status() >= 400) problems.push('http ' + r.status() + ': ' + r.url().replace(base, '')); });
  await page.goto(base + entry, { waitUntil: 'load', timeout: 60000 }).catch((e) => problems.push('goto: ' + e.message.slice(0, 200)));
  await page.waitForTimeout(6000);
  const info = await page.evaluate(() => {
    const canv = [...document.querySelectorAll('canvas')].map((c) => { let gl = null; try { gl = c.getContext('webgl2') || c.getContext('webgl'); } catch (e) {} return { w: c.width, h: c.height, webgl: !!gl }; });
    return { title: document.title, scrollHeight: document.documentElement.scrollHeight, inner: innerHeight, canvases: canv, textChars: document.body.innerText.length,
      globals: ['THREE', '__THREE__', 'gsap', 'ScrollTrigger', 'Lenis', 'lenis'].filter((k) => k in window), three: window.__THREE__ || null,
      sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth };
  });
  console.log('page', JSON.stringify(info));
  const H = Math.max(info.scrollHeight - info.inner, 0); const files = [];
  for (let i = 0; i < N; i++) {
    const y = Math.round(H * i / Math.max(N - 1, 1));
    await page.evaluate((yy) => window.scrollTo({ top: yy, behavior: 'instant' }), y);
    await page.mouse.wheel(0, 1);
    await page.waitForTimeout(2500);
    const f = path.join(out, `${device}_${String(i).padStart(2, '0')}.jpg`);
    await page.screenshot({ path: f, type: 'jpeg', quality: 80 }); files.push(f);
  }
  await browser.close(); server.close();
  if (files.length > 1) {
    const cols = device === 'phone' ? Math.min(files.length, 10) : 4; const rows = Math.ceil(files.length / cols);
    const list = path.join(out, `_${device}.txt`); fs.writeFileSync(list, files.map((f) => `file '${f}'\nduration 1`).join('\n') + '\n');
    execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', list, '-vf', `scale=${device === 'phone' ? 200 : 640}:-1,tile=${cols}x${rows}:padding=4:color=0x555555`, '-frames:v', '1', '-q:v', '3', path.join(out, `sheet_${device}.jpg`)]);
    fs.unlinkSync(list);
  }
  console.log('other hosts requested (blocked unless allowed):', JSON.stringify([...external]));
  for (const p of [...new Set(problems)].slice(0, 40)) console.log('PROBLEM', p);
  process.exit(problems.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
