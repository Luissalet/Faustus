#!/usr/bin/env node
/**
 * WP14 check: the Render panel — mount, load a plan, start a render (going
 * through a 403-approval-required round trip first, same shape as the
 * server's real `preflight_digest` gate), then poll until it reports done.
 *
 * Bundles the REAL source (`RenderPanel.tsx`/`adapters/creator_render.ts`)
 * with esbuild, mounts it into a `happy-dom` document (no real browser
 * needed — same pattern `studio/checks/creator-timeline.check.mjs` uses),
 * and drives it with a fake `fetch` standing in for
 * `routes/creator_render_routes.py`.
 *
 * Run: `node studio/checks/creator-render.check.mjs`
 */
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── the files this lot owns exist ──
for (const p of [
  'studio/src/screens/creator/RenderPanel.tsx',
  'studio/src/adapters/creator_render.ts',
  'src/creator/render/__init__.py',
  'src/creator/render/graph.py',
  'src/creator/render/profiles.py',
  'src/creator/render/cache.py',
  'src/creator/render/service.py',
  'routes/creator_render_routes.py',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── source-level checks ──
{
  const timeline = read('studio/src/screens/creator/Timeline.tsx');
  assert.ok(timeline.includes("import { RenderPanel } from './RenderPanel'"), 'Timeline.tsx must link to RenderPanel');
  assert.ok(timeline.includes('testId="timeline-render-open"'), 'Timeline.tsx must offer a Render button');

  const panel = read('studio/src/screens/creator/RenderPanel.tsx');
  assert.ok(panel.includes('render-profile-select'), 'RenderPanel must offer a profile picker');
  assert.ok(panel.includes('render-start'), 'RenderPanel must offer a Render/start control');
  assert.ok(panel.includes('render-cancel'), 'RenderPanel must offer a Cancel control while running');
  assert.ok(panel.includes('render-progress'), 'RenderPanel must show progress');

  const adapter = read('studio/src/adapters/creator_render.ts');
  for (const fn of ['getRenderPlan', 'startRender', 'getRenderJob', 'cancelRender']) {
    assert.ok(adapter.includes(`export function ${fn}`), `creator_render.ts must export ${fn}`);
  }

  const routes = read('routes/creator_render_routes.py');
  assert.ok(routes.includes('_require_flag()'), 'creator_render_routes.py must gate on creator_enabled');
  assert.ok(routes.includes('preflight_digest'), 'creator_render_routes.py must require a preflight_digest');

  const appPy = read('app.py');
  assert.ok(appPy.includes('setup_creator_render_routes'), 'app.py must include the WP14 render router');

  const graph = read('src/creator/render/graph.py');
  assert.ok(graph.includes('FILTER_ALLOWLIST'), 'graph.py must enforce an allowed-filter set');
}

// ── i18n ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  for (const key of ['Profile', 'Resolution', 'Estimated size', 'Render finished.', 'Render failed.']) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

// ── esbuild + happy-dom: mount, 403-then-approve, start, poll to done ──
async function main() {
  const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

  const harnessSrc = `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { RenderPanel } from './studio/src/screens/creator/RenderPanel';

    export function mount(containerId) {
      const root = createRoot(document.getElementById(containerId));
      root.render(React.createElement(RenderPanel, {
        projectId: 'p1', docId: 'doc_t1', onClose: () => { globalThis.__closed = true; },
      }));
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

  const tmp = mkdtempSync(join(tmpdir(), 'creator-render-check-'));
  const { writeFileSync } = await import('node:fs');
  const bundlePath = join(tmp, 'render-harness.mjs');
  writeFileSync(bundlePath, code, 'utf8');

  const window = new Window({ url: 'https://studio.faustus.invalid/' });
  const document = window.document;
  document.body.innerHTML = '<div id="root"></div>';

  for (const key of [
    'window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'Element', 'Event', 'CustomEvent',
    'MouseEvent', 'localStorage', 'sessionStorage', 'MutationObserver', 'getComputedStyle',
    'DocumentFragment', 'requestAnimationFrame', 'cancelAnimationFrame', 'Response', 'Headers',
  ]) {
    if (key in window) {
      try { globalThis[key] = window[key]; } catch { /* a few are getter-only in Node 22 */ }
    }
  }

  let submitAttempts = 0;
  let jobPolls = 0;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    calls.push({ url: u, method: (init.method || 'GET') });
    if (u.endsWith('/api/creator/render/plan') && init.method === 'POST') {
      return new Response(JSON.stringify({
        doc_id: 'doc_t1', doc_revision: 1,
        profile: { id: 'youtube_1080p', label: 'YouTube 1080p (16:9)', width: 1920, height: 1080,
                   fps: { numerator: 30, denominator: 1 }, video_codec: 'libx264', video_bitrate: '8M',
                   pix_fmt: 'yuv420p', audio_codec: 'aac', audio_bitrate: '192k', audio_rate: 48000,
                   container_ext: 'mp4' },
        graph: { filter_complex: '[0:v]null[vout]', video_map: '[vout]', audio_map: '', inputs: [],
                 duration_ticks: '30', subtitle_occurrence_id: null },
        cache_key: 'k1', cached_occurrence_id: null, estimated_duration_seconds: 1.0,
        estimated_size_bytes: 1000000, engine_build: 'ffmpeg 6.0', engine_available: true, engine_reason: '',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (u.endsWith('/api/creator/render') && init.method === 'POST') {
      submitAttempts += 1;
      if (submitAttempts === 1) {
        return new Response(JSON.stringify({
          detail: { reason: 'approval_required', detail: 'approve first', digest: 'digest123' },
        }), { status: 403, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify({ render_token: 'render_abc', state: 'starting' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (u.includes('/api/creator/render/render_abc') && (init.method || 'GET') === 'GET') {
      jobPolls += 1;
      const done = jobPolls >= 2;
      return new Response(JSON.stringify({
        render_token: 'render_abc', state: done ? 'done' : 'starting',
        engine_state: done ? 'completed' : 'running', progress: done ? 1.0 : 0.4,
        ffmpeg_job_id: 'ffmpeg_x', error: null,
        result: done ? { cache_hit: false, status: 'completed', job_id: 'ffmpeg_x',
                         occurrence_id: 'occ_out1', cache_key: 'k1', artifacts: [] } : null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(JSON.stringify({ detail: 'unhandled: ' + u }), { status: 404 });
  };

  const mod = await import(pathToFileURL(bundlePath).toString());
  mod.mount('root');
  await new Promise((r) => setTimeout(r, 30));

  const startBtn = document.querySelector('[data-testid="render-start"]');
  assert.ok(startBtn, 'Render start button must be rendered once a plan loaded');
  assert.ok(!startBtn.disabled, 'start button must be enabled when engine_available is true');

  // First click: 403 approval_required — the panel must surface it, not crash.
  startBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(document.body.textContent.includes('Approval'), 'panel must explain the approval-required 403');

  // Second click: succeeds (simulating the operator having approved out of band).
  document.querySelector('[data-testid="render-start"]').dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(document.querySelector('[data-testid="render-progress"]'), 'progress must render once a render_token exists');

  // Poll loop: the panel polls every second; wait past two ticks.
  await new Promise((r) => setTimeout(r, 2500));
  assert.ok(document.querySelector('[data-testid="render-result"]'), 'result must render once the job reports done');
  assert.ok(document.body.textContent.includes('occ_out1'), 'result must name the produced occurrence id');

  console.log('creator-render.check.mjs: OK (' + calls.length + ' fetch calls)');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
