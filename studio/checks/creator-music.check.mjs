#!/usr/bin/env node
/**
 * WP24 check: the Music screen — mount, pick a `song` document, generate a
 * take (`POST /api/creator/music/generate` → poll
 * `GET /api/creator/music/{job_id}` → reload the document), and see the
 * resulting take rendered with its own seed and an `<audio>` player.
 *
 * Bundles the REAL source (`Music.tsx`/`adapters/creator_music.ts`/
 * `adapters/creator.ts`) with esbuild, mounts it into a `happy-dom`
 * document (no real browser needed — same pattern
 * `studio/checks/creator-timeline.check.mjs` uses), and drives it with a
 * fake `fetch` standing in for `routes/creator_music_routes.py` +
 * `routes/creator_routes.py`.
 *
 * Run: `node studio/checks/creator-music.check.mjs`
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
  'studio/src/screens/creator/Music.tsx',
  'studio/src/screens/creator/music.css',
  'studio/src/adapters/creator_music.ts',
  'src/creator/adapters/music.py',
  'src/creator/music.py',
  'src/creator/ops/song_ops.py',
  'routes/creator_music_routes.py',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── source-level checks ──
{
  const music = read('studio/src/screens/creator/Music.tsx');
  assert.ok(music.includes('generateMusic'), 'Music.tsx must call generateMusic');
  assert.ok(music.includes('pollMusicJob'), 'Music.tsx must poll the generation job to completion');
  assert.ok(music.includes('preflight_required') || music.includes('runPreflightAndApprove'),
    'Music.tsx must handle the preflight-required gate');
  assert.ok(music.includes('<audio'), 'Music.tsx must render an <audio> player per take');
  assert.ok(music.includes('markTakeFavorite') && music.includes('data-testid={`music-favorite-'),
    'Music.tsx must offer a favorite toggle per take');
  assert.ok(music.includes('testId="music-generate"'), 'Music.tsx must expose a Generate control');

  const adapter = read('studio/src/adapters/creator_music.ts');
  for (const fn of ['editLyrics', 'addSection', 'removeSection', 'reorderSections', 'setStyle', 'setSeed',
                    'selectTake', 'markTakeFavorite', 'generateMusic', 'getMusicJob', 'generateMusicVariants']) {
    assert.ok(adapter.includes(`export function ${fn}`), `creator_music.ts must export ${fn}`);
  }
  assert.ok(adapter.includes('export async function pollMusicJob'), 'creator_music.ts must export pollMusicJob');

  const ops = read('src/creator/ops/song_ops.py');
  for (const opType of ['song.edit_lyrics', 'song.add_section', 'song.reorder_sections',
                        'song.set_style', 'song.set_seed', 'song.record_take']) {
    assert.ok(ops.includes(`"${opType}"`), `song_ops.py must register ${opType}`);
  }

  const routes = read('routes/creator_music_routes.py');
  assert.ok(routes.includes('/generate') && routes.includes('/variants'),
    'creator_music_routes.py must expose generate and variants routes');
  assert.ok(routes.includes('_require_approved_preflight'),
    'creator_music_routes.py must require an approved preflight before generating');
  assert.ok(routes.includes('_require_flag()'), 'creator_music_routes.py must gate on creator_enabled');

  const appPy = read('app.py');
  assert.ok(appPy.includes('setup_creator_music_routes'), 'app.py must include the WP24 music router');

  const screen = read('studio/src/screens/creator/CreatorScreen.tsx');
  assert.ok(screen.includes("import { Music } from './Music'"), 'CreatorScreen.tsx must link to the Music tab');
  assert.ok(screen.includes("data-testid=\"creator-tab-music\""), 'CreatorScreen.tsx must expose a Music tab');
}

// ── i18n ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  for (const key of ['Music', 'Takes', 'Seed', 'Generate', 'Song']) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

// ── esbuild + happy-dom: mount, generate, see the take ──
async function main() {
  const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

  const harnessSrc = `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { Music } from './studio/src/screens/creator/Music';

    export function mount(containerId) {
      const root = createRoot(document.getElementById(containerId));
      root.render(React.createElement(Music, { projectId: 'p1' }));
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

  const tmp = mkdtempSync(join(tmpdir(), 'creator-music-check-'));
  const bundlePath = join(tmp, 'music-harness.mjs');
  writeFileSync(bundlePath, code, 'utf8');

  const window = new Window({ url: 'https://studio.faustus.invalid/' });
  const document = window.document;
  document.body.innerHTML = '<div id="root"></div>';

  for (const key of [
    'window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'Element', 'Event', 'CustomEvent',
    'MouseEvent', 'localStorage', 'sessionStorage', 'MutationObserver', 'getComputedStyle',
    'DocumentFragment', 'requestAnimationFrame', 'cancelAnimationFrame', 'Response', 'Headers',
    'HTMLAudioElement', 'DOMException',
  ]) {
    if (key in window) {
      try { globalThis[key] = window[key]; } catch { /* a few are getter-only in Node 22 */ }
    }
  }

  let songDoc = {
    id: 'doc_song1', project_id: 'p1', kind: 'song', schema_version: 1,
    revision: 1, state: 'proposed', asset_refs: [], created_at: '', updated_at: '',
    content: {
      language: 'en',
      sections: [{ id: 's1', kind: 'verse', lyrics: 'hello world' }],
      takes: [], selected_take: null, style_tags: ['pop'], bpm_target: null, key_target: null, seed: null,
    },
  };

  const calls = [];
  let jobPolls = 0;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    const method = init.method || 'GET';
    calls.push({ url: u, method });

    if (u.includes('/api/creator/documents') && method === 'GET' && u.includes('project_id=')) {
      return new Response(JSON.stringify({ documents: [songDoc] }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (u.endsWith(`/api/creator/documents/${songDoc.id}`) && method === 'GET') {
      return new Response(JSON.stringify(songDoc), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (u === '/api/creator/music/generate' && method === 'POST') {
      return new Response(JSON.stringify({
        job_id: 'music_j1', state: 'queued', doc_id: songDoc.id, occurrence_id: null, take_id: null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (u === '/api/creator/music/music_j1') {
      jobPolls += 1;
      const done = jobPolls >= 2;
      if (done) {
        songDoc = JSON.parse(JSON.stringify(songDoc));
        songDoc.revision += 1;
        songDoc.content.takes.push({
          id: 'take_1', occurrence_id: 'occ_take_1', engine: 'ace_step', engine_version: 'fake-1.0',
          seed: 42, bpm: 120, key: 'C major', duration_s: 60, created_at: 0, lyrics_timing: null,
          warnings: [], favorite: false,
        });
      }
      return new Response(JSON.stringify({
        job_id: 'music_j1', state: done ? 'completed' : 'running', doc_id: songDoc.id,
        occurrence_id: done ? 'occ_take_1' : null, take_id: done ? 'take_1' : null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(JSON.stringify({ detail: 'unhandled: ' + method + ' ' + u }), { status: 404 });
  };

  const mod = await import(pathToFileURL(bundlePath).toString());
  mod.mount('root');
  await new Promise((r) => setTimeout(r, 30));

  const openBtn = document.querySelector(`[data-testid="music-open-${songDoc.id}"]`);
  assert.ok(openBtn, 'the song must be listed and openable');
  openBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));

  const generateBtn = document.querySelector('[data-testid="music-generate"]');
  assert.ok(generateBtn, 'the Generate button must be rendered once a song is open');
  generateBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

  // Poll interval defaults to 1000ms in pollMusicJob; the check drives it
  // with a short enough wait budget that two polls (running, then
  // completed) both land, without slowing the check down needlessly.
  await new Promise((r) => setTimeout(r, 2500));

  const genCall = calls.find((c) => c.url === '/api/creator/music/generate');
  assert.ok(genCall, 'clicking Generate must POST /api/creator/music/generate');
  const pollCalls = calls.filter((c) => c.url === '/api/creator/music/music_j1');
  assert.ok(pollCalls.length >= 2, 'the screen must poll the job at least until it completes');

  const audio = document.querySelector('[data-testid="music-audio-take_1"]');
  assert.ok(audio, 'a completed take must render an <audio> player');
  assert.equal(audio.getAttribute('src'), '/api/artifacts/occ_take_1/download',
    "the <audio> element must point at the take's own occurrence download URL");

  const favBtn = document.querySelector('[data-testid="music-favorite-take_1"]');
  assert.ok(favBtn, 'the take must offer a favorite toggle');

  rmSync(tmp, { recursive: true, force: true });
  console.log('ok creator-music (' + calls.length + ' fetch calls)');
}

await main();
