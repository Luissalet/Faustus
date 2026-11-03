// Hedged first token (src/model_hedge.py): the `hedge` SSE event decodes in
// adapters/chat.ts and, only when the hedge answered, leaves a note on the
// turn (screens/studio/model.ts). Drives the real decode()/apply() through
// esbuild, like l65-chat-model-events.check.mjs.
//
//   node studio/checks/model-hedge-event.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);

async function bundle(entry, outName) {
  const out = join(mkdtempSync(join(tmpdir(), 'fs-hedge-')), outName);
  await build({
    entryPoints: [join(root, entry)], bundle: true, platform: 'node', format: 'esm',
    outfile: out, logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const { decode } = await bundle('studio/src/adapters/chat.ts', 'chat.mjs');
const { apply, blankTurn } = await bundle('studio/src/screens/studio/model.ts', 'model.mjs');

const won = decode({
  type: 'hedge', winner: 'hedge', outcome: 'first_token', after_s: 23.4,
  answered_by: 'deepseek-v4.1-flash', answered_by_endpoint_label: 'Spark B',
}, null);
assert.equal(won.type, 'hedge');
assert.equal(won.afterS, 23.4);
assert.equal(won.endpoint, 'Spark B');
const turn = apply(blankTurn('assistant'), won);
assert.match(turn.note, /Spark B/);
assert.match(turn.note, /23/);

const lost = decode({ type: 'hedge', winner: 'primary', outcome: 'first_token', after_s: 21 }, null);
assert.equal(apply(blankTurn('assistant'), lost).note, undefined);

console.log('model-hedge-event: ok');
