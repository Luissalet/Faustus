#!/usr/bin/env node
/**
 * WP13 check: the Timeline screen — mount, drag a clip with a snapping
 * server round-trip (`timeline.snap`), and undo through
 * `POST .../undo` (`src/creator/ops/undo.py::undo_to`, wired by
 * `routes/creator_timeline_routes.py`).
 *
 * Bundles the REAL source (`Timeline.tsx`/`TimelineTrack.tsx`/
 * `adapters/creator_timeline.ts`) with esbuild, mounts it into a
 * `happy-dom` document (no real browser needed — same pattern
 * `studio/checks/embed.check.mjs` uses), and drives it with a fake
 * `fetch` standing in for `routes/creator_*_routes.py`.
 *
 * Run: `node studio/checks/creator-timeline.check.mjs`
 */
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync, rmSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── the files this lot owns exist ──
for (const p of [
  'studio/src/screens/creator/Timeline.tsx',
  'studio/src/screens/creator/TimelineTrack.tsx',
  'studio/src/screens/creator/timeline.css',
  'studio/src/adapters/creator_timeline.ts',
  'src/creator/timeline/clocks.py',
  'src/creator/timeline/tracks.py',
  'src/creator/timeline/retiming.py',
  'src/creator/timeline/snapping.py',
  'src/creator/timeline/validate.py',
  'src/creator/timeline/project_view.py',
  'routes/creator_timeline_routes.py',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── source-level checks ──
{
  const track = read('studio/src/screens/creator/TimelineTrack.tsx');
  assert.ok(track.includes('onPointerDown') && track.includes('onPointerMove') && track.includes('onPointerUp'), 'TimelineTrack must handle Pointer Events for dragging');
  assert.ok(track.includes('previewSnap'), 'TimelineTrack must show a client-side snap preview while dragging');

  const timeline = read('studio/src/screens/creator/Timeline.tsx');
  assert.ok(/case 'j': case 'J':/.test(timeline) && /case 'l': case 'L':/.test(timeline), 'Timeline must handle J/K/L shuttle keys');
  assert.ok(timeline.includes("case 'ArrowLeft'") && timeline.includes("case 'ArrowRight'"), 'Timeline must nudge by frame on arrow keys');
  assert.ok(timeline.includes("case 'i': case 'I':") && timeline.includes("case 'o': case 'O':"), 'Timeline must set in/out on I/O');
  assert.ok(timeline.includes('undoTo') && timeline.includes("testId=\"timeline-undo\""), 'Timeline must offer an Undo control wired to undoTo');
  assert.ok(timeline.includes('role="application"') && timeline.includes("aria-label={t('Timeline editor')}"), 'Timeline must be an accessible, keyboard-focusable region');

  const adapter = read('studio/src/adapters/creator_timeline.ts');
  for (const fn of ['snapClip', 'retimeClip', 'addMarker', 'rippleInsert', 'undoTo', 'getTimelineView', 'getTimelineValidation', 'previewSnap']) {
    assert.ok(adapter.includes(`export function ${fn}`), `creator_timeline.ts must export ${fn}`);
  }

  const routes = read('routes/creator_timeline_routes.py');
  assert.ok(routes.includes('undo_to'), 'creator_timeline_routes.py must wire undo_to');
  assert.ok(routes.includes('_require_flag()'), 'creator_timeline_routes.py must gate on creator_enabled');

  const appPy = read('app.py');
  assert.ok(appPy.includes('setup_creator_timeline_routes'), 'app.py must include the WP13 timeline router');

  const screen = read('studio/src/screens/creator/CreatorScreen.tsx');
  assert.ok(screen.includes("import { Timeline } from './Timeline'"), 'CreatorScreen.tsx must link to the Timeline tab');
  assert.ok(screen.includes("doc.kind === 'timeline'"), 'CreatorScreen.tsx must show the Timeline tab only for timeline documents');
}

// ── i18n ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  for (const key of ['Timeline', 'Timeline editor', 'Undone.', 'Redone.', 'Valid', 'JSON', '{n} problem(s)']) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

// ── esbuild + happy-dom: mount, drag-with-snap, undo ──
//
// The bundle is written to a real temp file and loaded with Node's own ESM
// `import()`, after copying the happy-dom globals onto Node's real
// `globalThis` (the same pattern `studio/checks/embed.check.mjs` uses) so
// react-dom's own `document`/`window` lookups resolve to the identical
// happy-dom objects this test dispatches events on.
//
// Each simulated pointer event is awaited past a macrotask before the
// next one fires (see the `tick()` helper below) — three
// `dispatchEvent` calls back to back, with no yield in between, all
// observe the SAME pre-drag React fiber: `pointerdown`'s `setDragState`
// has not committed yet when `pointermove`'s handler closure runs, so it
// still sees `dragState === null` (confirmed empirically while building
// this check). A real drag arrives as separate browser events across
// animation frames, so this is a faithful simulation, not a workaround
// for a component bug.
async function main() {
  const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

  const harnessSrc = `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { Timeline } from './studio/src/screens/creator/Timeline';

    const initialDoc = {
      id: 'doc_t1', project_id: 'p1', kind: 'timeline', schema_version: 1,
      revision: 1, state: 'proposed', asset_refs: [], created_at: '', updated_at: '',
      content: {
        clock: { ticks_per_second_numerator: '30', ticks_per_second_denominator: '1' },
        duration_ticks: '1000',
        tracks: [{
          id: 't0', kind: 'video', locked: false,
          clips: [
            { id: 'a', asset_ref: 'occ_a', timeline_start_ticks: '0', timeline_duration_ticks: '100',
              source_range: { start_ticks: '0', duration_ticks: '100' },
              source_clock: { ticks_per_second_numerator: '30', ticks_per_second_denominator: '1' } },
            { id: 'b', asset_ref: 'occ_b', timeline_start_ticks: '150', timeline_duration_ticks: '50',
              source_range: { start_ticks: '0', duration_ticks: '50' },
              source_clock: { ticks_per_second_numerator: '30', ticks_per_second_denominator: '1' } },
          ],
        }],
        markers: [],
      },
    };

    export function mount(containerId) {
      let doc = initialDoc;
      const setDoc = (d) => { doc = d; globalThis.__lastDoc = d; render(); };
      const root = createRoot(document.getElementById(containerId));
      function render() {
        root.render(React.createElement(Timeline, {
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

  const tmp = mkdtempSync(join(tmpdir(), 'creator-timeline-check-'));
  const { writeFileSync } = await import('node:fs');
  const bundlePath = join(tmp, 'timeline-harness.mjs');
  writeFileSync(bundlePath, code, 'utf8');

  const window = new Window({ url: 'https://studio.faustus.invalid/' });
  const document = window.document;
  document.body.innerHTML = '<div id="root"></div>';

  for (const key of [
    'window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'Element', 'Event', 'CustomEvent',
    'MouseEvent', 'PointerEvent', 'KeyboardEvent', 'localStorage', 'sessionStorage',
    'MutationObserver', 'getComputedStyle', 'DocumentFragment', 'requestAnimationFrame',
    'cancelAnimationFrame', 'Response', 'Headers',
  ]) {
    if (key in window) {
      try { globalThis[key] = window[key]; } catch { /* a few are getter-only in Node 22 */ }
    }
  }
  if (!globalThis.PointerEvent) globalThis.PointerEvent = globalThis.MouseEvent;

  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    calls.push({ url: u, method: (init.method || 'GET') });
    if (u.includes('/timeline/validate')) {
      return new Response(JSON.stringify({ revision: 1, ok: true, findings: [] }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    if (u.includes('/commands') && init.method === 'POST') {
      const body = JSON.parse(init.body);
      const op = body.op;
      if (op.type === 'timeline.snap') {
        // Server-side snap: 'a' ends at tick 100 — the clip being dragged
        // ('b') snaps onto that cut regardless of the exact near_ticks sent.
        const nextDoc = JSON.parse(JSON.stringify(globalThis.__lastDoc));
        nextDoc.revision = 2;
        nextDoc.content.tracks[0].clips[1].timeline_start_ticks = '100';
        return new Response(JSON.stringify({ document: nextDoc, applied: true, deduped: false }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ detail: 'unhandled op in fixture: ' + op.type }), { status: 400 });
    }
    if (u.includes('/undo') && init.method === 'POST') {
      const body = JSON.parse(init.body);
      if (body.target_revision !== 1) {
        return new Response('bad target_revision', { status: 400 });
      }
      const restored = JSON.parse(JSON.stringify(globalThis.__lastDoc));
      restored.revision = 3; // a NEW revision, never rewriting history
      restored.content.tracks[0].clips[1].timeline_start_ticks = '150'; // back to the original position
      return new Response(JSON.stringify({ document: restored, applied: true, deduped: false }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ detail: 'unhandled: ' + u }), { status: 404 });
  };

  const mod = await import(pathToFileURL(bundlePath).toString());
  mod.mount('root');
  await new Promise((r) => setTimeout(r, 30));

  // ── mounted, shows both clips ──
  const clipB = document.querySelector('[data-testid="timeline-clip-b"]');
  assert.ok(clipB, 'clip b must be rendered');
  assert.equal(clipB.getAttribute('x'), String(150 * 0.5), 'clip b starts at its initial tick * pixelsPerTick');

  // ── drag clip b leftward toward clip a's end (tick 100) — server snaps it there ──
  const dispatch = (el, type, props) => {
    const ev = new PointerEvent(type, { bubbles: true, cancelable: true, pointerId: 1, ...props });
    el.dispatchEvent(ev);
  };
  // Each dispatch is awaited past a macrotask so React 18 actually commits
  // the state update in between — real pointer events arrive on separate
  // animation frames; three synchronous `dispatchEvent` calls in a row
  // would all observe the SAME pre-drag fiber (confirmed empirically:
  // without this, `onPointerMove`'s closure still sees `dragState === null`
  // from before `pointerdown`'s `setDragState` committed).
  const tick = () => new Promise((r) => setTimeout(r, 10));
  dispatch(clipB, 'pointerdown', { clientX: 75 });
  await tick();
  dispatch(clipB, 'pointermove', { clientX: 50 }); // drag left by 25px = 50 ticks -> candidate ~100
  await tick();
  dispatch(clipB, 'pointerup', { clientX: 50 });

  await new Promise((r) => setTimeout(r, 30));

  const snapCall = calls.find((c) => c.url.includes('/commands'));
  assert.ok(snapCall, 'dragging must POST a timeline.snap command');
  assert.equal(globalThis.__lastDoc.revision, 2, 'the document must advance to revision 2 after the snap commits');
  assert.equal(
    globalThis.__lastDoc.content.tracks[0].clips[1].timeline_start_ticks, '100',
    "clip b must land exactly on clip a's cut (tick 100), not a rounded approximation",
  );

  const clipBAfter = document.querySelector('[data-testid="timeline-clip-b"]');
  assert.equal(clipBAfter.getAttribute('x'), String(100 * 0.5), 'the re-rendered clip must reflect the server-confirmed position');

  // ── undo: back to revision 3 (a NEW revision, content restored) ──
  const undoBtn = document.querySelector('[data-testid="timeline-undo"]');
  assert.ok(undoBtn, 'Undo button must be rendered');
  assert.ok(!undoBtn.disabled, 'Undo must be enabled once a revision change has happened this session');
  undoBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

  await new Promise((r) => setTimeout(r, 30));

  const undoCall = calls.find((c) => c.url.includes('/undo'));
  assert.ok(undoCall, 'clicking Undo must POST to .../undo');
  assert.equal(globalThis.__lastDoc.revision, 3, 'undo must land on a NEW revision (3), never revision 1 again');
  assert.equal(
    globalThis.__lastDoc.content.tracks[0].clips[1].timeline_start_ticks, '150',
    'undo must restore the pre-drag content',
  );

  rmSync(tmp, { recursive: true, force: true });
  console.log('ok creator-timeline (' + calls.length + ' fetch calls)');
}

await main();
