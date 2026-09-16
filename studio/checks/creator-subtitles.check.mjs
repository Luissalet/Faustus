#!/usr/bin/env node
/**
 * WP16 check: the Subtitles screen — mount, edit a cue's text through a
 * fake `subtitles.edit_cue` command round-trip, and verify the export
 * buttons + QA warning surface render from a fake `/qa` response.
 *
 * Bundles the REAL source (`Subtitles.tsx`/`adapters/creator_subtitles.ts`)
 * with esbuild, mounts it into a `happy-dom` document (no real browser
 * needed — same pattern `studio/checks/creator-timeline.check.mjs` uses),
 * and drives it with a fake `fetch` standing in for
 * `routes/creator_subtitle_routes.py` + the generic commands route.
 *
 * Run: `node studio/checks/creator-subtitles.check.mjs`
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
  'studio/src/screens/creator/Subtitles.tsx',
  'studio/src/adapters/creator_subtitles.ts',
  'src/creator/subtitles.py',
  'src/creator/ops/subtitle_ops.py',
  'routes/creator_subtitle_routes.py',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── source-level checks ──
{
  const screen = read('studio/src/screens/creator/Subtitles.tsx');
  assert.ok(screen.includes("data-testid=\"subtitles-screen\""), 'Subtitles must be identifiable for embedding tests');
  assert.ok(screen.includes('onKeyDown={onListKeyDown}') && screen.includes("ArrowDown"), 'Subtitles cue list must support keyboard navigation');
  assert.ok(screen.includes('subtitles-undo') && screen.includes('subtitles-redo'), 'Subtitles must offer Undo/Redo controls');
  assert.ok(screen.includes('subtitles-export-srt') && screen.includes('subtitles-export-vtt') && screen.includes('subtitles-export-ass'),
    'Subtitles must offer SRT/VTT/ASS export controls');
  assert.ok(screen.includes('editCueLines') && screen.includes('onBlur'), 'cue text must be inline-editable');
  assert.ok(screen.includes('cue.unmapped') && screen.includes('cue.edited'), 'Subtitles must surface unmapped/edited flags per cue');
  assert.ok(screen.includes('qaByCueId') || screen.includes('cue-warnings'), 'Subtitles must surface QA warnings per cue');

  const adapter = read('studio/src/adapters/creator_subtitles.ts');
  for (const fn of ['createFromTranscript', 'getQa', 'exportUrl', 'fetchExportText', 'editCue', 'splitCue',
    'mergeCues', 'shiftCues', 'setStyle', 'applyRetiming', 'regenerateFromTranscript']) {
    assert.ok(adapter.includes(`export function ${fn}`) || adapter.includes(`export async function ${fn}`),
      `creator_subtitles.ts must export ${fn}`);
  }

  const routes = read('routes/creator_subtitle_routes.py');
  assert.ok(routes.includes('from-transcript'), 'creator_subtitle_routes.py must wire from-transcript');
  assert.ok(routes.includes('/qa') || routes.includes('"/{doc_id}/qa"'), 'creator_subtitle_routes.py must wire qa');
  assert.ok(routes.includes('/export'), 'creator_subtitle_routes.py must wire export');
  assert.ok(routes.includes('_require_flag()'), 'creator_subtitle_routes.py must gate on creator_enabled');

  const opsFile = read('src/creator/ops/subtitle_ops.py');
  for (const opType of ['subtitles.edit_cue', 'subtitles.split_cue', 'subtitles.merge_cues',
    'subtitles.shift_cues', 'subtitles.set_style', 'subtitles.apply_retiming', 'subtitles.regenerate_from_transcript']) {
    assert.ok(opsFile.includes(`"${opType}"`), `subtitle_ops.py must register ${opType}`);
  }

  const appPy = read('app.py');
  assert.ok(appPy.includes('setup_creator_subtitle_routes'), 'app.py must include the WP16 subtitle router');

  const creatorScreen = read('studio/src/screens/creator/CreatorScreen.tsx');
  assert.ok(creatorScreen.includes("import { Subtitles } from './Subtitles'"), 'CreatorScreen.tsx must link to the Subtitles tab');
  assert.ok(creatorScreen.includes("data-testid=\"creator-tab-subtitles\""), 'CreatorScreen.tsx must add a Subtitles tab');
  assert.ok(creatorScreen.includes("tab === 'subtitles' && <Subtitles"), 'CreatorScreen.tsx must render the Subtitles tab body');
}

// ── i18n ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  for (const key of ['Subtitles', 'Subtitle tracks', 'Regenerate from transcript', 'Export SRT', 'Export VTT', 'Export ASS', 'unmapped']) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

// ── esbuild + happy-dom: mount, edit a cue, see QA warnings ──
async function main() {
  const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

  const CLOCK = { ticks_per_second_numerator: '16000', ticks_per_second_denominator: '1' };
  const initialDoc = {
    id: 'doc_sub1', project_id: 'p1', kind: 'subtitles', schema_version: 1,
    revision: 1, state: 'proposed', asset_refs: [], created_at: '', updated_at: '',
    content: {
      language: 'en', clock: CLOCK,
      profile: { max_chars_per_line: 42, max_lines: 2, cps_max: 17.0, min_duration_seconds: 1.0, max_duration_seconds: 7.0, min_gap_seconds: 0.08, pause_break_seconds: 0.6 },
      style: { font_family: 'Arial', font_size: 42, primary_color: '#FFFFFF', outline_color: '#000000', back_color: '#000000', bold: false, italic: false, shadow: 1, alignment: 2, position: 'bottom_center', margin_l: 20, margin_r: 20, margin_v: 24 },
      styles: {},
      cues: [
        { id: 'sub_0', start_ticks: '0', duration_ticks: '16000', lines: ['hello world'], speaker_id: null, source_cue_ids: ['cue_0'], editorial_status: 'proposed', edited: false, unmapped: false, estimated_timing: false, style_id: null },
        { id: 'sub_1', start_ticks: '32000', duration_ticks: '4000', lines: ['x'.repeat(50)], speaker_id: null, source_cue_ids: ['cue_1'], editorial_status: 'proposed', edited: false, unmapped: false, estimated_timing: false, style_id: null },
      ],
    },
  };

  const harnessSrc = `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { Subtitles } from './studio/src/screens/creator/Subtitles';

    export function mount(containerId) {
      const root = createRoot(document.getElementById(containerId));
      root.render(React.createElement(Subtitles, { projectId: 'p1' }));
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

  const tmp = mkdtempSync(join(tmpdir(), 'creator-subtitles-check-'));
  const bundlePath = join(tmp, 'subtitles-harness.mjs');
  writeFileSync(bundlePath, code, 'utf8');

  const window = new Window({ url: 'https://studio.faustus.invalid/' });
  const document = window.document;
  document.body.innerHTML = '<div id="root"></div>';

  for (const key of [
    'window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'Element', 'Event', 'CustomEvent',
    'MouseEvent', 'PointerEvent', 'KeyboardEvent', 'FocusEvent', 'localStorage', 'sessionStorage',
    'MutationObserver', 'getComputedStyle', 'DocumentFragment', 'requestAnimationFrame',
    'cancelAnimationFrame', 'Response', 'Headers',
  ]) {
    if (key in window) {
      try { globalThis[key] = window[key]; } catch { /* a few are getter-only in Node 22 */ }
    }
  }

  let currentDoc = initialDoc;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    calls.push({ url: u, method: (init.method || 'GET') });

    if (u.includes('/api/creator/documents') && (!init.method || init.method === 'GET') && u.includes('project_id=p1')) {
      return new Response(JSON.stringify({ documents: [currentDoc] }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (u.includes('/api/creator/subtitles/') && u.includes('/qa')) {
      const report = {
        total_cues: currentDoc.content.cues.length,
        cues_with_issues: 1,
        issues: [{ cue_id: 'sub_1', start_ticks: '32000', issues: [{ rule: 'max_chars_per_line', actual: 50, target: 42 }] }],
      };
      return new Response(JSON.stringify({ doc_id: currentDoc.id, revision: currentDoc.revision, report }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (u.includes('/commands') && init.method === 'POST') {
      const body = JSON.parse(init.body);
      const op = body.op;
      if (op.type === 'subtitles.edit_cue') {
        const nextDoc = JSON.parse(JSON.stringify(currentDoc));
        nextDoc.revision += 1;
        const cue = nextDoc.content.cues.find((c) => c.id === op.object_id);
        cue.lines = op.lines;
        cue.edited = true;
        currentDoc = nextDoc;
        return new Response(JSON.stringify({ document: nextDoc, applied: true, deduped: false }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify({ detail: 'unhandled op in fixture: ' + op.type }), { status: 400 });
    }
    return new Response(JSON.stringify({ detail: 'unhandled: ' + u }), { status: 404 });
  };

  const mod = await import(pathToFileURL(bundlePath).toString());
  mod.mount('root');
  await new Promise((r) => setTimeout(r, 40));

  const track = document.querySelector('[data-testid="subtitles-track-doc_sub1"]');
  assert.ok(track, 'the fetched subtitles document must be listed as a track');
  track.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 40));

  const cue0 = document.querySelector('[data-testid="subtitles-cue-sub_0"]');
  assert.ok(cue0, 'cue sub_0 must be rendered');
  const cue1Warnings = document.querySelector('[data-testid="subtitles-cue-sub_1"] .fs-subtitles__cue-warnings');
  assert.ok(cue1Warnings, 'cue sub_1 must show its QA warning (max_chars_per_line)');
  assert.ok(cue1Warnings.textContent.includes('max_chars_per_line'), 'the warning must name the violated rule');

  const textarea = document.querySelector('[data-testid="subtitles-cue-text-sub_0"]');
  assert.ok(textarea, 'cue sub_0 must have an editable textarea');
  // React implements `onBlur` on the native 'focusout' event (which
  // bubbles — plain 'blur' does not), so that is what must be dispatched
  // here for the handler to actually fire.
  textarea.value = 'edited by check';
  textarea.dispatchEvent(new (globalThis.FocusEvent)('focusout', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 40));

  const editCall = calls.find((c) => c.url.includes('/commands'));
  assert.ok(editCall, 'editing a cue must POST a subtitles.edit_cue command');
  assert.equal(currentDoc.content.cues[0].lines[0], 'edited by check', 'the edit must reach the document content');
  assert.equal(currentDoc.content.cues[0].edited, true, 'an edited cue must be marked edited: true');

  rmSync(tmp, { recursive: true, force: true });
  console.log('ok creator-subtitles (' + calls.length + ' fetch calls)');
}

await main();
