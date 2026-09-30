// The advisor's note in the turn activity: the live `advisor_advice` event
// decodes onto `turn.advice`, a reloaded turn restores it from
// `metadata.advisor`, and the collapsible card plus its Spanish strings are
// wired. Drives the real decode()/apply()/restoreFromMetadata() through esbuild.
//
// Run by tests/test_advisor_studio_js.py, or by hand:
//   node studio/checks/advisor-advice.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);

async function bundle(entry, outName) {
  const out = join(mkdtempSync(join(tmpdir(), 'fs-advisor-')), outName);
  await build({ entryPoints: [join(root, entry)], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const { decode } = await bundle('studio/src/adapters/chat.ts', 'chat.mjs');
const { apply, blankTurn, restoreFromMetadata } = await bundle('studio/src/screens/studio/model.ts', 'model.mjs');

// Live: decode, then the reducer keeps every use in order.
{
  const ev = decode({
    type: 'advisor_advice', round: 2, trigger: 'plan_or_write', ok: true, no_advice: false,
    model: 'big-model', tokens_in: 900, tokens_out: 120, latency_ms: 4200.5,
    text: 'Do X, not Y, because Z.',
  }, null);
  assert.equal(ev.type, 'advice');
  assert.equal(ev.advice.trigger, 'plan_or_write');
  assert.equal(ev.advice.text, 'Do X, not Y, because Z.');
  assert.equal(ev.advice.tokensIn, 900);
  assert.equal(ev.advice.latencyMs, 4200.5);
  assert.equal(ev.advice.noAdvice, false);
  let turn = apply(blankTurn('t1'), ev);
  const second = decode({ type: 'advisor_advice', trigger: 'final', ok: true, no_advice: true }, null);
  turn = apply(turn, second);
  assert.equal(turn.advice.length, 2);
  assert.equal(turn.advice[1].noAdvice, true);
  assert.equal(turn.advice[1].text, undefined);
  // A failed call shows why, not advice.
  const failed = decode({ type: 'advisor_advice', trigger: 'loop', ok: false, error: 'endpoint down' }, null);
  assert.equal(failed.advice.ok, false);
  assert.equal(failed.advice.error, 'endpoint down');
  // No trigger, no item.
  assert.equal(decode({ type: 'advisor_advice', ok: true }, null), null);
}

// Reload: metadata.advisor comes back as turn.advice; absent stays absent.
{
  const base = blankTurn('t2');
  const restored = restoreFromMetadata(base, {
    advisor: [
      { trigger: 'final', ok: true, no_advice: false, text: 'Run the tests.', model: 'm', tokens_in: 10, tokens_out: 5, latency_ms: 10 },
      { trigger: 'loop', ok: false, error: 'x' },
      { ok: true },
    ],
  });
  assert.equal(restored.advice.length, 2);
  assert.equal(restored.advice[0].text, 'Run the tests.');
  const untouched = restoreFromMetadata(base, {});
  assert.equal(untouched.advice, undefined);
}

// The card and its strings are wired (source inspection: JSX in a large component).
{
  const transcript = readFileSync(join(root, 'studio/src/screens/studio/Transcript.tsx'), 'utf8');
  assert.ok(transcript.includes('function AdviceCard'), 'Transcript.tsx must define AdviceCard');
  assert.ok(transcript.includes('<AdviceCard advice={turn.advice} />'), 'AdviceCard must render from turn.advice');
  assert.ok(transcript.includes('data-testid="turn-advice"'));
  const es = readFileSync(join(root, 'studio/src/i18n/es.ts'), 'utf8');
  for (const key of ['Advice ({trigger})', 'Advice ({trigger}): nothing to add', 'Advice ({trigger}): not available',
    'first plan or write', 'loop breaker', 'before the final answer',
    'Advisory only: a second model wrote this, and the agent may ignore it.']) {
    assert.ok(es.includes(`"${key}":`), `es.ts must translate: ${key}`);
  }
}

console.log('ok advisor-advice');
