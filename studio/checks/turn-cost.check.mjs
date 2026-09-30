// The per-turn cost view and the send-after hand-off, on the client side:
// a value the server did not report stays unknown (never 0), the reply carries
// the run id that keys its cost view (live `metrics` event and reloaded
// metadata), a "send after" message is acknowledged only after it was handed
// over, and every new string has a Spanish row. Drives the real adapters and
// formatters through esbuild.
//
// Run by tests/test_cx3_turn_cost_js.py, or by hand:
//   node studio/checks/turn-cost.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);

async function bundle(entry, outName) {
  const out = join(mkdtempSync(join(tmpdir(), 'fs-turncost-')), outName);
  await build({
    entryPoints: [join(root, entry)], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const report = await bundle('studio/src/adapters/runReport.ts', 'runReport.mjs');
const { decode, metricsFrom } = await bundle('studio/src/adapters/chat.ts', 'chat.mjs');

// Nulls stay null.
{
  const cost = report.turnCostFrom({
    found: true, run_id: 'r1', session_id: 's1', state: 'done', total_ms: 9000, accounted_ms: 6000, unaccounted_ms: 3000,
    phases: [
      { key: 'main_rounds', calls: 2, duration_ms: 4000, input_tokens: 1200, output_tokens: null, output_tokens_unknown_calls: 2,
        cost_usd: null, cost_state: 'unknown', estimated_calls: 1, models: ['m'] },
      { key: 'tools', calls: 3, duration_ms: null, duration_unknown_calls: 3, cost_state: 'known',
        items: [{ tool: 'send_email', calls: 1, duration_ms: 50, failed: 1, unknown_effect: 1 }] },
    ],
    notes: ['a note', ''],
  });
  assert.equal(cost.found, true);
  assert.equal(cost.unaccountedMs, 3000);
  const [main, tools] = cost.phases;
  assert.equal(main.outputTokens, null, 'unreported output tokens must stay null');
  assert.equal(main.outputTokensUnknownCalls, 2);
  assert.equal(main.costUsd, null);
  assert.equal(main.costState, 'unknown');
  assert.equal(main.estimatedCalls, 1);
  assert.equal(tools.durationMs, null, 'unreported duration must stay null, not 0');
  assert.equal(tools.items[0].label, 'send_email');
  assert.equal(tools.items[0].unknownEffect, 1);
  assert.deepEqual(cost.notes, ['a note']);
  const empty = report.turnCostFrom(null);
  assert.equal(empty.found, false);
  assert.equal(empty.totalMs, null);
  assert.deepEqual(empty.phases, []);
}

// The reply carries its run id.
{
  assert.equal(metricsFrom({ run_id: 'run-a', output_tokens: 3 }).runId, 'run-a');
  assert.equal(metricsFrom({ trace_id: 'run-b' }).runId, 'run-b');
  assert.equal(metricsFrom({}).runId, undefined);
  const ev = decode({ type: 'metrics', trace_id: 'run-live', data: { output_tokens: 5 } }, null);
  assert.equal(ev.type, 'metrics');
  assert.equal(ev.metrics.runId, 'run-live');
}

// Send after: handed off first, acknowledged only when asked to.
{
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push(`${init.method || 'GET'} ${url}`);
    if (String(url).endsWith('/claim')) {
      return { ok: true, status: 200, json: async () => ({ messages: [{ receipt_id: 'a', text: 'hello' }, { receipt_id: '', text: 'x' }, { receipt_id: 'b', text: '' }], blocked: '' }) };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const claimed = await report.claimQueuedSends('sess 1');
  assert.deepEqual(claimed.messages, [{ receiptId: 'a', text: 'hello' }]);
  assert.deepEqual(calls, ['POST /api/chat/steer/sess%201/claim'], 'claiming must not acknowledge anything');
  assert.equal(await report.acknowledgeQueuedSend('a'), true);
  assert.equal(calls[1], 'POST /api/chat/steer/receipt/a/ack');
  globalThis.fetch = async () => { throw new Error('offline'); };
  assert.deepEqual(await report.claimQueuedSends('s'), { messages: [], blocked: 'network' });
  assert.equal(await report.acknowledgeQueuedSend('a'), false);
}

// Wiring and Spanish strings.
{
  const transcript = readFileSync(join(root, 'studio/src/screens/studio/Transcript.tsx'), 'utf8');
  assert.match(transcript, /<TurnCostBreakdown sessionId=\{sessionId\} runId=\{turn\.metrics\.runId\}/);
  const studio = readFileSync(join(root, 'studio/src/screens/Studio.tsx'), 'utf8');
  assert.match(studio, /claimQueuedSends\(sessionId\)/);
  assert.ok(studio.indexOf('await acknowledgeQueuedSend') > studio.indexOf('void run(sessionId, joined)'), 'acknowledge after the turn is started');
  const es = readFileSync(join(root, 'studio/src/i18n/es.ts'), 'utf8');
  const tsv = readFileSync(join(root, 'docs/ui/i18n/es.tsv'), 'utf8');
  const strings = [
    'Where did the cost go?', 'Model rounds', 'Recovery', 'Retries', 'Compaction', 'Other model calls', 'Turn time',
    'No cost record was kept for this turn.', 'not attributed to a phase: {n}', 'effect unknown', 'unnamed', '{n} calls',
    'Attempt', 'This failed, but it may already have taken effect. Check the destination before trying again.',
  ];
  for (const s of strings) {
    assert.ok(es.includes(JSON.stringify(s) + ':'), `es.ts lacks: ${s}`);
    assert.ok(tsv.split('\n').some((l) => l.startsWith(s + '\t')), `es.tsv lacks: ${s}`);
  }
}

console.log('ok turn-cost');
