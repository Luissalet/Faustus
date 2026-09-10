// Local models capability manifest (Lote 17): announced vs tested chips, and
// the two adapter calls behind the "Calibrate" button — bundled the same way
// studio/checks/chat-outbox.check.mjs bundles chat.ts, with fetch mocked.
//
// Run by tests/test_model_calibration.py, or by hand:
//   node studio/checks/model-caps.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const out = join(mkdtempSync(join(tmpdir(), 'faustus-model-caps-')), 'localModels.mjs');
await build({ entryPoints: ['studio/src/adapters/localModels.ts'], bundle: true, platform: 'node', format: 'esm', outfile: out });
const lm = await import(pathToFileURL(out).href);

// capChipState: the tri-state a chip renders, straight off a test result —
// undefined and `ok: null` (skipped/unknown) both read as "announced" (gray),
// never as a silent pass.
assert.equal(lm.capChipState(undefined), 'announced');
assert.equal(lm.capChipState({ ok: null }), 'announced');
assert.equal(lm.capChipState({ ok: true }), 'tested');
assert.equal(lm.capChipState({ ok: false }), 'failed');

// loadModelCapabilities: GET /api/models/{name}/capabilities?endpoint_id=…,
// with the model name URL-encoded (a real tag has a ':').
{
  const seen = [];
  globalThis.fetch = async (url) => {
    seen.push(url);
    return new Response(JSON.stringify({ model: 'qwen3.5:9b', endpoint_id: 'ep1', announced: {}, tested: {}, degraded: [] }), { status: 200 });
  };
  const manifest = await lm.loadModelCapabilities('ep1', 'qwen3.5:9b');
  assert.equal(seen.length, 1);
  assert.equal(seen[0], '/api/models/qwen3.5%3A9b/capabilities?endpoint_id=ep1');
  assert.deepEqual(manifest.degraded, []);
}

// calibrateModel: POST .../calibrate, same encoding, and the manifest it
// gets back is what the row should cache — including a failed probe.
{
  let method = '';
  globalThis.fetch = async (url, init) => {
    method = init?.method;
    return new Response(JSON.stringify({
      model: 'qwen3.5:9b', endpoint_id: 'ep1', announced: {},
      tested: { tool_calling: { ok: false, tested_at: '2026-09-10T00:00:00Z', evidence: { error: 'timeout' } } },
      degraded: ['tools nativas anunciadas pero fallan en calibración: tratadas como texto (fence)'],
    }), { status: 200 });
  };
  const manifest = await lm.calibrateModel('ep1', 'qwen3.5:9b');
  assert.equal(method, 'POST');
  assert.equal(manifest.tested.tool_calling.ok, false);
  assert.equal(lm.capChipState(manifest.tested.tool_calling), 'failed');
  assert.equal(manifest.degraded.length, 1);
}

// A 409 ("not loaded — load it first") surfaces as a thrown Error with that
// message, the same shape every other adapter call in this file uses.
{
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'qwen3.5:9b is not loaded — load it first, calibration never loads a model on its own' }), { status: 409 });
  await assert.rejects(
    () => lm.calibrateModel('ep1', 'qwen3.5:9b'),
    (e) => /load it first/.test(e.message),
  );
}

console.log('ok model-caps');
