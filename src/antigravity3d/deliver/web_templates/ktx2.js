
// A KTX2Loader owns a pool of transcoder workers. It lives only while loads that use it are in flight (a count per
// renderer, shared by assets loading at the same time through a well-known key): every React Three Fiber Canvas remount
// makes a new renderer, and a loader kept for ever per renderer would pile up worker pools (three.js warns about several
// active KTX2Loaders). The transcoder files stay in the browser's cache, so the next load starts a pool again cheaply.
// Pass `ktx2Loader` to use the project's own: it is never disposed here.
const KTX2_KEY = Symbol.for("agy3d.ktx2Loaders");
function leaseKtx2(options) {
  if (options.ktx2Loader) return { loader: options.ktx2Loader, release() {} };
  const renderer = options.renderer;
  if (!renderer) throw new Error(`${DATA.asset} is compressed (KTX2): pass { renderer } so the right GPU format is chosen`);
  const registry = (globalThis[KTX2_KEY] ??= new WeakMap());
  let entry = registry.get(renderer);
  if (!entry) {
    const path = options.transcoderPath ?? join(SITE, DATA.basisUrl);
    entry = { loader: new KTX2Loader().setTranscoderPath(path.endsWith("/") ? path : path + "/").detectSupport(renderer), uses: 0 };
    registry.set(renderer, entry);
  }
  entry.uses++;
  let released = false;
  return {
    loader: entry.loader,
    release() {
      if (released) return;
      released = true;
      if (--entry.uses === 0) { entry.loader.dispose(); if (registry.get(renderer) === entry) registry.delete(renderer); }
    },
  };
}
