// The header gauge and its panel list every model in memory: which one, who
// loaded it, what it holds and on which cards. Drives the real adapter through
// esbuild: a server that sends `resident` is shown as sent (biggest first,
// per-card bytes); an older one that only sends `ollama` and
// `external_runners` still lists every model, with the weights flagged as not
// measured; and the new strings have Spanish rows.
//
// Run by tests/test_model_residency_js.py, or by hand:
//   node studio/checks/vitals-residents.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-vitals-')), 'usage.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/usage.ts')], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const u = await import(pathToFileURL(out).href);
const GB = 1024 ** 3;

const gpus = [{ index: 0, name: 'NVIDIA GeForce RTX 5060 Ti' }, { index: 1, name: 'NVIDIA GeForce RTX 5060 Ti' }, { index: 2, name: 'x' }];
const sent = {
  gpu: gpus,
  resident: {
    models: [
      { model: 'qwen3.8-27b-q8-llamacpp', engine: 'llama.cpp', loaded_by: 'Start-LlamaServer.ps1 (powershell.exe)', bytes: 32 * GB, measured: true, gpus: [{ index: 0, bytes: 16 * GB }, { index: 1, bytes: 16 * GB }] },
      { model: 'qwen2.5-3b-helper', engine: 'llama.cpp', loaded_by: 'Faustus: helper', bytes: 3 * GB, measured: true, gpus: [{ index: 2, bytes: 3 * GB }] },
    ],
    others: [],
  },
};
const rows = u.residentModels(sent);
assert.equal(rows.length, 2);
assert.equal(rows[0].model, 'qwen3.8-27b-q8-llamacpp');
assert.equal(u.cardsText(rows[0].gpus, sent), 'GPU 0 16.0 GB + GPU 1 16.0 GB');
assert.equal(u.shortModelName('D:\\models\\qwen3.8-27b-q8.gguf'), 'qwen3.8-27b-q8');
assert.equal(u.shortModelName('qwen3.5:9b'), 'qwen3.5');

// An older server: both sources still listed, biggest first, weights unmeasured.
const legacy = {
  ollama: { reachable: true, models: [{ name: 'qwen3.5:9b', gpu_pct: 100, cpu_pct: 0, size: 9 * GB, size_vram: 8 * GB, per_gpu: [{ index: 3, bytes: 8 * GB }] }] },
  external_runners: [{ model: 'qwen3.8-27b', footprint_bytes: 28 * GB, context_length: 131072 }],
};
const old = u.residentModels(legacy);
assert.deepEqual(old.map((m) => m.model), ['qwen3.8-27b', 'qwen3.5:9b']);
assert.equal(old[0].measured, false);
assert.equal(old[1].loaded_by, 'Ollama');

const es = readFileSync(join(root, 'studio/src/i18n/es.ts'), 'utf8');
for (const key of ['Models in memory', 'Loaded by', 'Size in memory', 'Other processes on the GPUs', 'No model in memory.', 'loaded by {who}', '{n} GB of weights (in memory not measured)']) {
  assert.ok(es.includes(JSON.stringify(key) + ':'), `Spanish row for ${key}`);
}
console.log('ok vitals-residents');
