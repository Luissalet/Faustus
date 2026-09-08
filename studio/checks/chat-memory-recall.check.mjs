import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const out = join(mkdtempSync(join(tmpdir(), 'faustus-memory-recall-')), 'chat.mjs');
await build({entryPoints:['studio/src/adapters/chat.ts'], bundle:true, platform:'node', format:'esm', outfile:out});
const {sendTurn} = await import(pathToFileURL(out));
let body;
globalThis.fetch = async (url, options) => {
  assert.equal(url, '/api/chat_stream');
  body = options.body;
  return new Response('data: [DONE]\n\n');
};
for (const options of [{}, {noMemory:true}, {incognito:true}, {compare:true}, {noMemory:true,compare:true}]) {
  for await (const _event of sendTurn({sessionId:'qa',message:'test',mode:'agent',...options})) {}
  assert.deepEqual(body.getAll('no_memory'), Object.keys(options).length ? ['true'] : []);
  assert.equal(body.get('incognito'), options.incognito ? 'true' : null);
}
console.log('Memory recall transport: all checks passed');
