import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const out = join(mkdtempSync(join(tmpdir(), 'faustus-model-label-')), 'label.mjs');
await build({entryPoints:['studio/src/lib/model-label.ts'], bundle:true, platform:'node', format:'esm', outfile:out});
const {modelLabel, isInstalled} = await import(pathToFileURL(out));
const codex = {model:'client-default', endpointId:'a', endpointName:'Codex · subscription'};
const claude = {...codex, endpointId:'b', endpointName:'Claude Code · subscription'};
assert.notEqual(modelLabel(codex, [codex, claude]), modelLabel(claude, [codex, claude]));
assert.equal(modelLabel(codex, []), 'Codex · subscription · client-default');
const local = {...codex, model:'qwen3.5:9b', endpointName:'Local'};
assert.equal(modelLabel(local, [local]), 'qwen3.5:9b');
assert.equal(modelLabel(local, [local, {...local, endpointId:'b'}]), 'Local · qwen3.5:9b');
assert.equal(modelLabel({...codex, endpointName:''}, []), 'client-default');

// Lote 70a, punto A.11 — isInstalled(current, routes): true only when the
// current endpointId+model pair is still present in the live routes list.
assert.equal(isInstalled(local, [local]), true, 'present by endpointId+model is installed');
assert.equal(isInstalled(local, []), false, 'absent from an empty routes list is not installed');
assert.equal(isInstalled(local, [{...local, endpointId:'other'}]), false, 'same model on a different endpoint does not count as installed');
assert.equal(isInstalled(local, [{...local, model:'qwen3.5:70b'}]), false, 'a different model on the same endpoint does not count as installed');
assert.equal(isInstalled(local, [claude, local]), true, 'found anywhere in the list, not just first');
console.log('Model labels: all checks passed');
