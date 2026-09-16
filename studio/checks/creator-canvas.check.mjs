#!/usr/bin/env node
/**
 * WP20 check: the Canvas screen — mount, move a layer (via the layer
 * list's reorder control, a real `canvas.reorder` command with
 * `expected_revision`) and undo through `POST .../undo` (the SAME generic
 * revision-undo route `creator-timeline.check.mjs` exercises — see
 * `Canvas.tsx`'s import comment for why it is reused rather than
 * duplicated).
 *
 * Bundles the REAL source (`Canvas.tsx`/`CanvasLayers.tsx`/
 * `adapters/creator_canvas.ts`) with esbuild, mounts it into a
 * `happy-dom` document (same pattern as `creator-timeline.check.mjs`),
 * and drives it with a fake `fetch` standing in for
 * `routes/creator_canvas_routes.py` / the generic document-commands route.
 *
 * `CanvasLayers`' interactive `<canvas>` drawing depends on real
 * `Image.onload` events happy-dom does not fire for fake URLs, so this
 * check exercises the layer-list reorder control (a real command round
 * trip that needs no image to have loaded) rather than simulating a
 * pointer drag on the canvas surface — the SAME `apply_command` path a
 * drag ultimately uses, per `Canvas.tsx::onCommitMove`/`reorder`.
 *
 * Run: `node studio/checks/creator-canvas.check.mjs`
 */
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── the files this lot owns exist ──
for (const p of [
  'studio/src/screens/creator/Canvas.tsx',
  'studio/src/screens/creator/CanvasLayers.tsx',
  'studio/src/screens/creator/canvas.css',
  'studio/src/adapters/creator_canvas.ts',
  'src/creator/canvas.py',
  'src/creator/ops/canvas_layer_style_ops.py',
  'routes/creator_canvas_routes.py',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── source-level checks ──
{
  const canvasTsx = read('studio/src/screens/creator/Canvas.tsx');
  assert.ok(canvasTsx.includes('undoTo') && canvasTsx.includes('testId="canvas-undo"'), 'Canvas must offer an Undo control wired to undoTo');
  assert.ok(canvasTsx.includes('role="application"') && canvasTsx.includes("aria-label={t('Canvas editor')}"), 'Canvas must be an accessible region');
  assert.ok(canvasTsx.includes('reorderLayers') || canvasTsx.includes('moveLayer'), 'Canvas must commit layer moves/reorders through a typed op');
  assert.ok(canvasTsx.includes('requestInpaint'), 'Canvas must bridge to WP11 via requestInpaint');
  assert.ok(canvasTsx.includes('exportCanvas'), 'Canvas must offer export');

  const layersTsx = read('studio/src/screens/creator/CanvasLayers.tsx');
  assert.ok(layersTsx.includes('onPointerDown') && layersTsx.includes('onPointerMove') && layersTsx.includes('onPointerUp'), 'CanvasLayers must handle Pointer Events for move/scale');
  assert.ok(layersTsx.includes('onCommitPolygonMask'), 'CanvasLayers must support committing a polygon mask');

  const adapter = read('studio/src/adapters/creator_canvas.ts');
  for (const fn of ['addLayer', 'moveLayer', 'setMask', 'cropRegion', 'rotate', 'reorderLayers',
                    'setLayerTransform', 'setBlendMode', 'setPolygonMask', 'renderUrl', 'exportCanvas',
                    'importLegacyCanvas', 'requestInpaint']) {
    assert.ok(adapter.includes(`export function ${fn}`), `creator_canvas.ts must export ${fn}`);
  }

  const canvasOps = read('src/creator/ops/canvas_layer_style_ops.py');
  assert.ok(canvasOps.includes('"canvas.set_layer_transform"') && canvasOps.includes('"canvas.set_blend_mode"')
    && canvasOps.includes('"canvas.set_polygon_mask"'), 'canvas_layer_style_ops.py must register its three op types');

  const canvasPy = read('src/creator/canvas.py');
  assert.ok(canvasPy.includes('def render(') && canvasPy.includes('def region_to_source(')
    && canvasPy.includes('def import_legacy(') && canvasPy.includes('def export(')
    && canvasPy.includes('def inpaint_request('), 'canvas.py must implement its five documented entry points');

  const routes = read('routes/creator_canvas_routes.py');
  assert.ok(routes.includes('/canvas/{doc_id}/render'), 'creator_canvas_routes.py must wire the render route');
  assert.ok(routes.includes('/canvas/{doc_id}/export'), 'creator_canvas_routes.py must wire the export route');
  assert.ok(routes.includes('/canvas/import-legacy'), 'creator_canvas_routes.py must wire the import-legacy route');
  assert.ok(routes.includes('/canvas/{doc_id}/inpaint-request'), 'creator_canvas_routes.py must wire the inpaint-request route');
  assert.ok(routes.includes('_require_flag()'), 'creator_canvas_routes.py must gate on creator_enabled');

  const appPy = read('app.py');
  assert.ok(appPy.includes('setup_creator_canvas_routes'), 'app.py must include the WP20 canvas router');

  const screen = read('studio/src/screens/creator/CreatorScreen.tsx');
  assert.ok(screen.includes("import { Canvas } from './Canvas'"), 'CreatorScreen.tsx must link to the Canvas tab');
  assert.ok(screen.includes("doc.kind === 'canvas'"), 'CreatorScreen.tsx must show the Canvas view only for canvas documents');
  assert.ok(screen.includes('creator-tab-canvas'), 'CreatorScreen.tsx must add a top-level Canvas tab');
}

// ── i18n ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  for (const key of ['Canvas', 'Canvas editor', 'Layers', 'Blend mode', 'Normal', 'Multiply', 'Screen',
                     'Polygon mask', 'Export', 'Undone.', 'Redone.']) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

// ── esbuild + happy-dom: mount, reorder a layer (typed command), undo ──
async function main() {
  const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

  const harnessSrc = `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { Canvas } from './studio/src/screens/creator/Canvas';

    const initialDoc = {
      id: 'doc_c1', project_id: 'p1', kind: 'canvas', schema_version: 1,
      revision: 1, state: 'proposed', asset_refs: [], created_at: '', updated_at: '',
      content: {
        width: 64, height: 64, operation_semantics_version: 1, base_asset_ref: 'occ_base',
        layers: [
          { id: 'bottom', kind: 'raster', visible: true, opacity: 1, offset: { x: 0, y: 0 }, asset_ref: 'occ_a' },
          { id: 'top', kind: 'raster', visible: true, opacity: 1, offset: { x: 4, y: 4 }, asset_ref: 'occ_b' },
        ],
      },
    };

    export function mount(containerId) {
      let doc = initialDoc;
      const setDoc = (d) => { doc = d; globalThis.__lastDoc = d; render(); };
      const root = createRoot(document.getElementById(containerId));
      function render() {
        root.render(React.createElement(Canvas, {
          doc, onDocUpdated: setDoc, onRevisionConflict: () => { globalThis.__conflict = true; },
        }));
      }
      globalThis.__lastDoc = doc;
      render();
    }
  `;

  const bundle = await esbuildBuild({
    stdin: { contents: harnessSrc, resolveDir: root, loader: 'tsx', sourcefile: 'harness.tsx' },
    bundle: true,
    format: 'esm',
    platform: 'browser',
    write: false,
    logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' },
    loader: { '.tsx': 'tsx', '.ts': 'ts', '.css': 'empty' },
  });
  const code = bundle.outputFiles[0].text;

  const tmp = mkdtempSync(join(tmpdir(), 'creator-canvas-check-'));
  const bundlePath = join(tmp, 'canvas-harness.mjs');
  writeFileSync(bundlePath, code, 'utf8');

  const window = new Window({ url: 'https://studio.faustus.invalid/' });
  const document = window.document;
  document.body.innerHTML = '<div id="root"></div>';

  for (const key of [
    'window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'Element', 'Event', 'CustomEvent',
    'MouseEvent', 'PointerEvent', 'KeyboardEvent', 'localStorage', 'sessionStorage',
    'MutationObserver', 'getComputedStyle', 'DocumentFragment', 'requestAnimationFrame',
    'cancelAnimationFrame', 'Response', 'Headers', 'HTMLCanvasElement', 'Image',
  ]) {
    if (key in window) {
      try { globalThis[key] = window[key]; } catch { /* a few are getter-only in Node 22 */ }
    }
  }
  if (!globalThis.PointerEvent) globalThis.PointerEvent = globalThis.MouseEvent;
  // happy-dom's canvas 2D context is not implemented — CanvasLayers draws
  // defensively (`ctx = canvas.getContext('2d'); if (!ctx) return;`), so a
  // null context here is the same "no-op paint" path a real browser takes
  // before any image has loaded; this check verifies the COMMAND path, not
  // pixels (that is `test_creator_wp20_canvas.py`'s job, with real PIL).

  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    calls.push({ url: u, method: (init.method || 'GET') });
    if (u.includes('/render')) {
      return new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), { status: 200, headers: { 'Content-Type': 'image/png' } });
    }
    if (u.includes('/commands') && init.method === 'POST') {
      const body = JSON.parse(init.body);
      const op = body.op;
      if (op.type === 'canvas.reorder') {
        const nextDoc = JSON.parse(JSON.stringify(globalThis.__lastDoc));
        nextDoc.revision = 2;
        const byId = Object.fromEntries(nextDoc.content.layers.map((l) => [l.id, l]));
        nextDoc.content.layers = op.order.map((id) => byId[id]);
        return new Response(JSON.stringify({ document: nextDoc, applied: true, deduped: false }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ detail: 'unhandled op in fixture: ' + op.type }), { status: 400 });
    }
    if (u.includes('/undo') && init.method === 'POST') {
      const body = JSON.parse(init.body);
      if (body.target_revision !== 1) return new Response('bad target_revision', { status: 400 });
      const restored = JSON.parse(JSON.stringify(globalThis.__lastDoc));
      restored.revision = 3; // a NEW revision, never rewriting history
      restored.content.layers = JSON.parse(JSON.stringify(globalThis.__initialLayers));
      return new Response(JSON.stringify({ document: restored, applied: true, deduped: false }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ detail: 'unhandled: ' + u }), { status: 404 });
  };

  const mod = await import(pathToFileURL(bundlePath).toString());
  globalThis.__initialLayers = [
    { id: 'bottom', kind: 'raster', visible: true, opacity: 1, offset: { x: 0, y: 0 }, asset_ref: 'occ_a' },
    { id: 'top', kind: 'raster', visible: true, opacity: 1, offset: { x: 4, y: 4 }, asset_ref: 'occ_b' },
  ];
  mod.mount('root');
  await new Promise((r) => setTimeout(r, 30));

  // ── mounted, shows both layers in the list ──
  const bottomRow = document.querySelector('[data-testid="canvas-layer-bottom"]');
  assert.ok(bottomRow, 'layer "bottom" must be rendered in the layer list');

  // ── move "bottom" up (a real canvas.reorder command) ──
  const upBtn = document.querySelector('[data-testid="canvas-layer-up-bottom"]');
  assert.ok(upBtn, '"move layer up" control must be rendered');
  upBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

  await new Promise((r) => setTimeout(r, 30));

  const reorderCall = calls.find((c) => c.url.includes('/commands'));
  assert.ok(reorderCall, 'moving a layer must POST a canvas.reorder command');
  assert.equal(globalThis.__lastDoc.revision, 2, 'the document must advance to revision 2 after the reorder commits');
  assert.deepEqual(
    globalThis.__lastDoc.content.layers.map((l) => l.id), ['top', 'bottom'],
    '"bottom" must now be above "top" in paint order',
  );

  // ── undo: back to a NEW revision 3, content restored ──
  const undoBtn = document.querySelector('[data-testid="canvas-undo"]');
  assert.ok(undoBtn, 'Undo button must be rendered');
  assert.ok(!undoBtn.disabled, 'Undo must be enabled once a revision change has happened this session');
  undoBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

  await new Promise((r) => setTimeout(r, 30));

  const undoCall = calls.find((c) => c.url.includes('/undo'));
  assert.ok(undoCall, 'clicking Undo must POST to .../undo');
  assert.equal(globalThis.__lastDoc.revision, 3, 'undo must land on a NEW revision (3), never revision 1 again');
  assert.deepEqual(
    globalThis.__lastDoc.content.layers.map((l) => l.id), ['bottom', 'top'],
    'undo must restore the pre-reorder layer order',
  );

  rmSync(tmp, { recursive: true, force: true });
  console.log('ok creator-canvas (' + calls.length + ' fetch calls)');
}

await main();
