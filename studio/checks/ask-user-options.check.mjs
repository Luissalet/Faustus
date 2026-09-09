// ask_user options reach the card as {label, description}, not "[object Object]".
//
// The tool sends options as objects; the live event path used to String()
// them, so every button read "[object Object]" (10-09-2026) and the model
// looked like it never asked with options. History and live must agree.
// Run by tests/test_studio_ask_user_options_js.py, or by hand:
//   node studio/checks/ask-user-options.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-ask-user-')), 'chat.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/chat.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { askOptionsFrom, toolEventsFrom } = await import(pathToFileURL(out).href);

// objects, strings and junk, in one list
const opts = askOptionsFrom([
  { label: 'SQLite (recommended)', description: 'No server to run; one file in the project.' },
  { label: 'Postgres', description: '' },
  'Write my own',
  { value: 'legacy-value-shape' },
  { description: 'no label at all' },
  42,
  null,
]);
assert.deepEqual(opts, [
  { label: 'SQLite (recommended)', description: 'No server to run; one file in the project.' },
  { label: 'Postgres', description: '' },
  { label: 'Write my own', description: '' },
  { label: 'legacy-value-shape', description: '' },
]);
assert.ok(!opts.some((o) => o.label === '[object Object]'), 'no option is the string of an object');

// the persisted tool event (history reload) reads the same shape
const [ev] = toolEventsFrom({ tool_events: [{ tool: 'ask_user', round: 1, ask_user: {
  question: 'Which storage?', multi: true,
  options: [{ label: 'A', description: 'a' }, { label: 'B', description: 'b' }],
} }] });
assert.equal(ev.ask.question, 'Which storage?');
assert.equal(ev.ask.multi, true);
assert.deepEqual(ev.ask.options, [{ label: 'A', description: 'a' }, { label: 'B', description: 'b' }]);
assert.equal(ev.ask.kind, 'question');

console.log('ok ask-user-options');
