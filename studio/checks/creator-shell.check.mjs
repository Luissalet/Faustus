// WP05 — Studio: the Creator shell (three-zone workspace over the Creator
// API: library/document/capabilities-resources-preflight), wired into the
// router/sidebar the same way `/alternatives` and `/workflows` already are.
//
// Static source inspection for the wiring (like alternatives.check.mjs),
// plus a real esbuild bundle of the adapter to prove response decoding and
// the 409 revision-conflict path actually work, not just that the source
// text looks right.
//
// Run by hand:
//   node studio/checks/creator-shell.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync, rmSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');
const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

// ── the files this lot owns exist ──
for (const p of [
  'studio/src/adapters/creator.ts',
  'studio/src/screens/creator/CreatorScreen.tsx',
  'studio/src/screens/creator/creator.css',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── adapters/creator.ts: every Creator route it must cover, a typed 409 ──
{
  const src = read('studio/src/adapters/creator.ts');
  assert.ok(src.includes('export class RevisionConflictError extends CreatorApiError'), 'must export RevisionConflictError extending CreatorApiError');
  assert.ok(src.includes("response.status === 409 && detail.reason === 'revision_conflict'"), 'must detect the 409 revision_conflict shape from routes/creator_routes.py');
  assert.ok(src.includes('currentRevision'), 'RevisionConflictError must carry the server\'s current_revision');
  for (const fn of [
    'export function getCapabilities', 'export function listDocuments', 'export function createDocument',
    'export function getDocument', 'export function applyCommand', 'export function getDocumentHistory',
    'export function getCreatorProfile', 'export function putCreatorProfile',
    'export function listLibrary', 'export function exportManifest', 'export function getLineage', 'export function buildProxy',
    'export function listKnownModels', 'export function getCapabilityProfile', 'export function getParamSchema', 'export function validateParams',
    'export function runPreflight', 'export function approvePreflight', 'export function getResources',
    'export function useCreatorAvailable',
  ]) {
    assert.ok(src.includes(fn), `creator.ts must export: ${fn}`);
  }
  for (const route of [
    '/api/creator/capabilities', '/api/creator/documents', '/api/creator/profile', '/api/creator/library',
    '/api/creator/params', '/api/creator/preflight', '/api/creator/resources',
  ]) {
    assert.ok(src.includes(route), `creator.ts must call ${route}`);
  }
  assert.ok(!/\bconsole\.log\(/.test(src), 'no stray console.log');
}

// ── screen: reads through the adapter only, never fetch() directly ──
{
  const src = read('studio/src/screens/creator/CreatorScreen.tsx');
  assert.ok(!/\bfetch\(/.test(src), 'CreatorScreen.tsx must call the adapter, not fetch() directly');
  assert.ok(src.includes('export function CreatorScreen'), 'must export CreatorScreen');
  assert.ok(src.includes("from '../../adapters/creator'"), 'must read through adapters/creator.ts');
  assert.ok(src.includes('listProjects'), 'must offer a real project picker, not a free-text project id');
  assert.ok(src.includes('RevisionConflictError'), 'must special-case the 409 revision conflict, not a generic error toast');
  assert.ok(src.includes("t('Reload") , 'a 409 must offer a reload path, not just an error message');
  assert.ok(/Creator is disabled/.test(src), 'a disabled flag must show the named-setting message, not a blank screen');
  assert.ok(/creator_enabled/.test(src), 'the disabled message must name the setting (creator_enabled)');
  assert.ok(src.includes("localStorage"), 'draft/selection must persist (UX06), not just live state');
  assert.ok(src.includes('patch_content'), 'the minimal command editor must send patch_content commands');
  assert.ok(src.includes('expected_revision') || src.includes('expectedRevision'), 'the command editor must carry expected_revision');
  assert.ok(src.includes('runPreflight') && src.includes('approvePreflight'), 'must run preflight and offer Approve');
  assert.ok(src.includes('role='), 'must use ARIA roles for the three zones (accessibility)');
  assert.ok(src.includes('aria-label'), 'must label the zones/controls for keyboard and screen-reader users');
}

// ── routing: server whitelist, client route table, sidebar and the lazy
// Route entry all agree on /creator ──
{
  const app = read('app.py');
  assert.ok(/@app\.get\("\/creator"\)/.test(app), 'app.py must serve GET /creator as a deep link');
}
{
  const routes = read('studio/src/shell/routes.ts');
  assert.ok(/\{\s*path:\s*'\/creator'/.test(routes), "routes.ts TOOLS must list '/creator'");
  assert.ok(routes.includes("'/creator'") && routes.includes('SERVER_ROUTES'), 'routes.ts SERVER_ROUTES must list /creator, kept in step with app.py');
}
{
  const shell = read('studio/src/shell/AppShell.tsx');
  assert.ok(shell.includes("import('../screens/creator/CreatorScreen')"), 'AppShell.tsx must lazy-import CreatorScreen');
  assert.ok(/<Route path="\/creator" element=\{<CreatorScreen \/>\}\s*\/>/.test(shell), 'AppShell.tsx must route /creator to CreatorScreen');
  assert.ok(shell.includes('useCreatorAvailable'), 'the rail must gate the /creator entry on the capability flag, not show it unconditionally');
}

// ── i18n: every new string this lot introduces has a Spanish row ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Creator', 'Creator is disabled', 'Project library', 'Active document', 'Command history',
    'Preflight', 'Approve', 'Reload', 'Run preflight',
    'This document changed since you loaded it. The server now has revision {n}.',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

// ── esbuild: bundle the adapter standalone and exercise response decoding
// plus the 409 revision-conflict path against a fake fetch, so this check
// proves behavior, not just that the source text mentions the right names ──
{
  const tmp = mkdtempSync(join(tmpdir(), 'creator-shell-check-'));
  try {
    const bundlePath = join(tmp, 'creator-adapter.mjs');
    await esbuildBuild({
      entryPoints: [path('studio/src/adapters/creator.ts')],
      bundle: true,
      format: 'esm',
      platform: 'browser',
      outfile: bundlePath,
      logLevel: 'silent',
    });

    const harnessPath = join(tmp, 'harness.mjs');
    const harness = `
      import * as creator from ${JSON.stringify(bundlePath)};

      const calls = [];
      globalThis.fetch = async (url, init) => {
        calls.push(String(url));
        if (String(url).includes('/documents/doc1/commands')) {
          return new Response(JSON.stringify({ reason: 'revision_conflict', current_revision: 7 }), {
            status: 409, headers: { 'Content-Type': 'application/json' },
          });
        }
        if (String(url).includes('/capabilities?')) {
          return new Response(JSON.stringify({
            creator_enabled: true, project_id: 'p1',
            document_kinds: ['canvas', 'timeline'], document_states: ['proposed', 'accepted'],
            media_backends: {},
          }), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response('not found', { status: 404 });
      };

      // ── decoding a normal 200 response ──
      const caps = await creator.getCapabilities('p1');
      if (caps.creator_enabled !== true) throw new Error('expected creator_enabled true, got ' + JSON.stringify(caps));
      if (!Array.isArray(caps.document_kinds) || caps.document_kinds[0] !== 'canvas') {
        throw new Error('document_kinds not decoded: ' + JSON.stringify(caps));
      }

      // ── the 409 revision-conflict path: typed, carries current_revision ──
      let threw = null;
      try {
        await creator.applyCommand('doc1', 'cmd-1', 3, { type: 'patch_content', patch: {} });
      } catch (err) {
        threw = err;
      }
      if (!threw) throw new Error('applyCommand on a stale revision must throw');
      if (threw.name !== 'RevisionConflictError') throw new Error('expected RevisionConflictError, got ' + threw.name);
      if (threw.currentRevision !== 7) throw new Error('expected currentRevision 7, got ' + threw.currentRevision);
      if (threw.status !== 409) throw new Error('expected status 409, got ' + threw.status);

      if (calls.length !== 2) throw new Error('expected exactly 2 fetch calls, got ' + calls.length);

      console.log('harness ok');
    `;
    const { writeFileSync } = await import('node:fs');
    writeFileSync(harnessPath, harness, 'utf8');

    const { execFileSync } = await import('node:child_process');
    const out = execFileSync(process.execPath, [harnessPath], { encoding: 'utf8' });
    assert.ok(out.includes('harness ok'), `bundled adapter harness did not pass: ${out}`);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

console.log('ok creator-shell');
