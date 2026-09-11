// Lote 65 — Studio: turno de chat. Proves two pure helpers in Transcript.tsx
// by driving them directly through esbuild, same pattern as the other
// checks in this lote.
//
//   - EXEC-02: `maskSecrets` redacts value-shaped secrets in a command
//     preview (never the key name, never the real argv — display only).
//   - RES-04: `reportPartsProgress` turns `_final_report_in_parts`'s own
//     "part N of M" progress message into a written/pending count.
//
// Run by tests/test_l65_studio_events_js.py, or by hand:
//   node studio/checks/l65-transcript-helpers.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-l65-transcript-')), 'transcript.mjs');
await build({
  entryPoints: [join(root, 'studio/src/screens/studio/Transcript.tsx')], bundle: true,
  platform: 'node', format: 'esm', jsx: 'automatic', outfile: out, logLevel: 'silent',
});
// Transcript.tsx pulls in shell/display.ts, which touches `document`/
// `window.localStorage` once at module load (its `data-fullwidth`
// attribute and its persisted display prefs) — real DOM effects that a
// browser always has and this node check does not. Minimal stand-ins, just
// enough for module load to succeed; nothing under test here calls either.
globalThis.window ??= { localStorage: { getItem: () => null, setItem: () => {} } };
globalThis.document ??= { documentElement: { toggleAttribute: () => {} } };
const { maskSecrets, reportPartsProgress } = await import(pathToFileURL(out).href);

// ── EXEC-02: maskSecrets ──
assert.equal(maskSecrets('curl -H "Authorization: Bearer sk_live_abcdef123456"'), 'curl -H "Authorization: Bearer ****"');
assert.equal(maskSecrets('mysql -u root --password=hunter2 db'), 'mysql -u root --password=**** db');
assert.equal(maskSecrets('curl "https://api.example.com?token=abcdef123456"'), 'curl "https://api.example.com?token=****"');
assert.equal(maskSecrets('aws configure set aws_access_key_id AKIAABCDEFGHIJKLMNOP'), 'aws configure set aws_access_key_id ****');
// Nothing secret-shaped: unchanged, byte for byte.
assert.equal(maskSecrets('ls -la /home/user/project'), 'ls -la /home/user/project');
assert.equal(maskSecrets('git commit -m "fix token refresh bug"'), 'git commit -m "fix token refresh bug"');

// ── RES-04: reportPartsProgress ──
assert.deepEqual(reportPartsProgress('Writing sections 5-8 of 12 (part 2 of 3)'), { written: 1, total: 3 });
assert.deepEqual(reportPartsProgress('Writing sections 1-4 of 12 (part 1 of 3)'), { written: 0, total: 3 });
assert.deepEqual(reportPartsProgress('Writing sections 9-12 of 12 (part 3 of 3)'), { written: 2, total: 3 });
assert.equal(reportPartsProgress('searching'), null);
assert.equal(reportPartsProgress(''), null);

console.log('ok l65-transcript-helpers');
