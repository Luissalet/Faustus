// Sub-agent transcript: the adapter that maps the route payload, the heat
// levels of the token bars, and the wiring of the board (name and button open
// the read-only panel; no write call is made from it). Bundled with esbuild
// on the fly; run by hand:  node studio/checks/subagent-transcript.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-sat-'));
const out = join(dir, 'agents.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'adapters', 'agents.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const { subagentTranscriptFrom } = await import(pathToFileURL(out).href);

const t = subagentTranscriptFrom({
  session_id: 'c1', name: 'Worker 1', model: 'qwen', offset: 0, total: 2, has_more_after: true,
  messages: [
    { index: 0, role: 'user', content: 'do it', content_truncated: false, content_chars: 5, tool_events: [] },
    { index: 1, role: 'assistant', content: 'done', tokens: { input: 900, output: 80 },
      tool_events: [{ tool: 'bash', round: 1, input: 'ls', output: 'a', output_chars: 1, exit_code: 1, duration_ms: 12 }, 'junk', {}] },
  ],
  usage: { source: 'trace', input_tokens: 900, output_tokens: 80, total_tokens: 980,
    per_call: [{ seq: 1, input: 500, output: 50, heat: 1 }, { seq: 2, input: 400, output: 30, heat: 7 }, { seq: 3, heat: 'x' }] },
});
assert.equal(t.name, 'Worker 1');
assert.equal(t.hasMoreAfter, true);
assert.equal(t.messages[1].toolEvents.length, 1, 'non-events are dropped');
assert.equal(t.messages[1].toolEvents[0].exitCode, 1);
assert.deepEqual(t.messages[1].tokens, { input: 900, output: 80 });
assert.equal(t.messages[0].tokens, null);
assert.equal(t.usage.source, 'trace');
assert.deepEqual(t.usage.calls.map((c) => c.heat), [1, 1, 0], 'heat is clamped to 0..1');
assert.equal(subagentTranscriptFrom({}).usage.source, 'none');
assert.equal(subagentTranscriptFrom({ usage: { source: 'weird' } }).usage.source, 'none');

const read = (p) => readFileSync(join(root, 'studio', 'src', p), 'utf8');
const board = read('screens/studio/SubagentBoard.tsx');
assert.match(board, /<SubagentTranscript[\s\S]*?sessionId=\{opened\.sessionId\}/, 'the board opens the transcript panel');
assert.match(board, /label=\{t\('Transcript'\)\}/, 'a Transcript button on the card');
assert.match(board, /fs-sa__name--link/, 'the name opens it too');
const panel = read('screens/studio/SubagentTranscript.tsx');
assert.doesNotMatch(panel, /method:\s*'(POST|PUT|PATCH|DELETE)'|stopWorker|steerWorker/, 'the panel is read-only');
assert.match(panel, /heatLevel/, 'token bars use heat levels');
const es = read('i18n/es.ts');
for (const key of ['Transcript', 'Back to sub-agents', 'Open the transcript', 'Tokens per model call']) {
  assert.ok(es.includes(`"${key}":`), `Spanish entry for ${key}`);
}
console.log('subagent-transcript: ALL OK');
