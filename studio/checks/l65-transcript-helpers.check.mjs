// Lote 65 — Studio: turno de chat. Proves two pure helpers in Transcript.tsx
// by driving them directly through esbuild, same pattern as the other
// checks in this lote.
//
//   - EXEC-02: `maskSecrets` redacts value-shaped secrets in a command
//     preview (never the key name, never the real argv — display only).
//   - RES-04: `reportPartsProgress` turns `_final_report_in_parts`'s own
//     "part N of M" progress message into a written/pending count.
//   - `toolRailSummary`: live vs finished copy for the grouped tools line.
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
const { maskSecrets, reportPartsProgress, toolRailSummary, toolRailCounts, toolRailParts, thoughtSummary, buildActivity } = await import(pathToFileURL(out).href);

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

// ── Grouped tool rail (Cursor/ChatGPT-style collapsed summary) ──
assert.deepEqual(toolRailSummary(1, true), { one: 'Running a command', other: 'Running commands', n: 1 });
assert.deepEqual(toolRailSummary(3, true), { one: 'Running a command', other: 'Running commands', n: 3 });
assert.deepEqual(toolRailSummary(1, false), { one: 'Ran 1 command', other: 'Ran {n} commands', n: 1 });
assert.deepEqual(toolRailSummary(4, false), { one: 'Ran 1 command', other: 'Ran {n} commands', n: 4 });

// ── Cursor-style categorized counts (searches / files / edits / commands) ──
assert.deepEqual(toolRailCounts([
  { tool: 'grep', command: '{"pattern":"steerLive"}' },
  { tool: 'grep', command: '{"pattern":"_latest_plan_update"}' },
  { tool: 'read_file', command: 'studio/src/screens/Studio.tsx' },
  { tool: 'read_file', command: '{"path":"src/agent_loop.py"}' },
  { tool: 'read_file', command: 'studio/src/screens/Studio.tsx' },
  { tool: 'bash', command: 'pytest tests/test_steer.py' },
  { tool: 'edit_file', command: 'docs/ui/i18n/es.tsv' },
]), { searches: 2, files: 2, edits: 1, commands: 1 });

assert.deepEqual(
  toolRailParts({ searches: 18, files: 29, edits: 0, commands: 0 }, false),
  [
    { one: 'Explored 1 file', other: 'Explored {n} files', n: 29 },
    { one: '1 search', other: '{n} searches', n: 18 },
  ],
);
assert.deepEqual(
  toolRailParts({ searches: 3, files: 9, edits: 4, commands: 4 }, false),
  [
    { one: 'Edited 1 file', other: 'Edited {n} files', n: 4 },
    { one: 'Explored 1 file', other: 'Explored {n} files', n: 9 },
    { one: '1 search', other: '{n} searches', n: 3 },
    { one: 'Ran 1 command', other: 'Ran {n} commands', n: 4 },
  ],
);
assert.deepEqual(
  toolRailParts({ searches: 1, files: 0, edits: 0, commands: 0 }, true),
  [{ one: 'Searching', other: '{n} searches', n: 1 }],
);

assert.deepEqual(thoughtSummary(0, true), { one: 'Thinking', other: 'Thinking', n: 0 });
assert.deepEqual(thoughtSummary(1, false), { one: 'Thought 1s', other: 'Thought {n}s', n: 1 });
assert.deepEqual(thoughtSummary(9, false), { one: 'Thought 1s', other: 'Thought {n}s', n: 9 });
assert.deepEqual(thoughtSummary(22, true), { one: 'Thought 1s', other: 'Thought {n}s', n: 22 });

assert.deepEqual(
  buildActivity(5, [{ afterStep: -1 }, { afterStep: 2 }]),
  [
    { kind: 'group', thoughts: [0], from: 0, to: 3 },
    { kind: 'group', thoughts: [1], from: 3, to: 5 },
  ],
);
assert.deepEqual(buildActivity(4, []), [{ kind: 'group', thoughts: [], from: 0, to: 4 }]);
assert.deepEqual(buildActivity(0, [{ afterStep: -1 }]), [{ kind: 'group', thoughts: [0], from: 0, to: 0 }]);
assert.deepEqual(
  buildActivity(4, [{ afterStep: -1 }, { afterStep: -1 }]),
  [{ kind: 'group', thoughts: [0, 1], from: 0, to: 4 }],
);

console.log('ok l65-transcript-helpers');
