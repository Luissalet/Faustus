// INF-01 — Cookbook/serve veracity: architecture from metadata, never from
// a model's name (H01/H03), no unmeasured speedup claim (H02), and the
// native-vs-wrapper translation receipt (H04). Drives the real
// `studio/src/lib/cookbook/serve.ts` through esbuild, same pattern as
// `studio/checks/l89-git-merge.check.mjs`.
//
// Run by tests/test_inf01_serve_js.py, or by hand:
//   node studio/checks/serve_veracity.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-inf01-serve-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({
    entryPoints: [join(root, 'studio', 'src', rel)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: out,
    logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const serve = await load(join('lib', 'cookbook', 'serve.ts'), 'serve.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

const DENSE = { repo: 'x', kind: 'dense', total_params: 27_000_000_000, active_params: null, num_experts: null, mtp: null, architectures: ['X'], source: 'hf_config', observed_at: '2026-01-01T00:00:00Z', note: null };
const MOE = { repo: 'x', kind: 'moe', total_params: 397_000_000_000, active_params: 17_000_000_000, num_experts: 512, mtp: null, architectures: ['X'], source: 'hf_config', observed_at: '2026-01-01T00:00:00Z', note: null };
const MOE_MTP = { ...MOE, mtp: true };
const UNKNOWN = { repo: 'x', kind: 'unknown', total_params: null, active_params: null, num_experts: null, mtp: null, architectures: null, source: 'none', observed_at: null, note: 'metadata unavailable' };

const baseCtx = { platform: 'linux', remoteHost: '', env: 'none', envPath: '', hwBackend: 'cuda', hostPlatform: 'linux' };

// ── T01: a densely-verified Qwen3.5-27B never gets MoE flags from the name ──
{
  const opts = serve.detectModelOptimizations('Qwen3.5-27B', DENSE);
  assert(!opts.flags.includes('--enable-expert-parallel'), 'T01: dense-verified Qwen3.5-27B does not get --enable-expert-parallel');
  assert(opts.envVars.length === 0, 'T01: dense-verified Qwen3.5-27B gets no MoE env vars');
  assert(opts.hints.some((h) => /qwen3/i.test(h)), 'T01: the name-based family guess still surfaces as a hint');
}

// ── T02: unknown/absent architecture — no MoE, no MTP, "not verified" tip ──
{
  const withUnknown = serve.detectModelOptimizations('Qwen3.5-27B-A10B', UNKNOWN);
  const withoutArch = serve.detectModelOptimizations('Qwen3.5-27B-A10B');
  for (const [label, opts] of [['unknown arch', withUnknown], ['no arch passed', withoutArch]]) {
    assert(!opts.flags.includes('--enable-expert-parallel'), `T02 (${label}): no --enable-expert-parallel`);
    assert(opts.envVars.length === 0, `T02 (${label}): no MoE env vars`);
    assert(!opts.flags.some((f) => f.includes('speculative-config')), `T02 (${label}): no MTP speculative-config flag`);
    assert(
      opts.tips.includes('Architecture not verified from metadata — MoE/MTP options are not suggested from the name'),
      `T02 (${label}): carries the "not verified" tip`,
    );
  }
}

// ── verified MoE really does produce the branch ──
{
  const opts = serve.detectModelOptimizations('Qwen3.5-27B-A10B', MOE);
  assert(opts.flags.includes('--enable-expert-parallel'), 'verified MoE: --enable-expert-parallel is produced');
  assert(opts.envVars.some((v) => v.startsWith('VLLM_USE_')), 'verified MoE: MoE env vars are produced');
}

// ── MTP only with mtp: true, never from a substring (A17B included) ──
{
  const a17b = serve.detectModelOptimizations('Qwen3.5-397B-A17B', MOE); // mtp: null on MOE
  assert(!a17b.spec, 'MTP: A17B name alone (mtp unset) does not produce a spec');
  assert(!a17b.flags.some((f) => f.includes('speculative-config')), 'MTP: A17B name alone does not add --speculative-config');

  const verified = serve.detectModelOptimizations('Qwen3-Next-80B-A3B', MOE_MTP);
  assert(Boolean(verified.spec), 'MTP: mtp === true produces a spec');
  assert(verified.flags.some((f) => f.includes('speculative-config')), 'MTP: mtp === true adds --speculative-config');
}

// ── H02: no unmeasured speedup claim anywhere in detectModelOptimizations' tips ──
{
  const forbidden = /x faster|faster generation|speedup/i;
  const samples = [
    serve.detectModelOptimizations('Qwen3-Next-80B-A3B', MOE_MTP),
    serve.detectModelOptimizations('DeepSeek-V3-671B', MOE_MTP),
    serve.detectModelOptimizations('Kimi-K2', MOE_MTP),
  ];
  for (const opts of samples) {
    for (const tip of opts.tips) assert(!forbidden.test(tip), `H02: tip has no unmeasured speedup claim: "${tip}"`);
  }
}

// ── T03: buildServeCmd's translation receipt ──────────────────────────────
const llamaFields = { llama_tensor_split: '1,1', llama_parallel: '2', llama_speculative_mtp: true, llama_spec_tokens: '3', port: '8080', ctx: '8192' };

{
  const remoteWin = { ...baseCtx, platform: 'windows', remoteHost: 'winbox' };
  const receipt = serve.buildServeCmd(llamaFields, 'model.gguf', 'llamacpp', remoteWin);
  assert(receipt.implementation === 'llama_cpp.server', 'T03: remote Windows selects llama_cpp.server');
  const omittedOptions = receipt.omitted.map((o) => o.option);
  for (const opt of ['--tensor-split', '--parallel', '--spec-type (MTP)']) {
    assert(omittedOptions.includes(opt), `T03: remote Windows omits ${opt} with a reason`);
  }
  assert(receipt.omitted.every((o) => Boolean(o.reason)), 'T03: every omitted option carries a reason');
  assert(receipt.cmd.includes('llama_cpp.server'), 'T03: the command really invokes the python wrapper');
}

{
  const localWin = { ...baseCtx, platform: 'windows', remoteHost: '' };
  const receipt = serve.buildServeCmd(llamaFields, 'model.gguf', 'llamacpp', localWin);
  assert(receipt.implementation === 'llama-server', 'T03: local Windows selects the native binary');
  assert(receipt.omitted.length === 0, 'T03: local Windows omits nothing');
}

{
  const linux = { ...baseCtx, platform: 'linux', remoteHost: '' };
  const receipt = serve.buildServeCmd(llamaFields, 'model.gguf', 'llamacpp', linux);
  assert(receipt.implementation === 'llama-server', 'T03: Linux selects the native binary');
  assert(receipt.omitted.length === 0, 'T03: Linux omits nothing');
}

// ── buildServeCmdString stays a plain string for callers that want only that ──
{
  const linux = { ...baseCtx, platform: 'linux', remoteHost: '' };
  const s = serve.buildServeCmdString({ port: '8080', ctx: '8192' }, 'model.gguf', 'llamacpp', linux);
  assert(typeof s === 'string' && s.includes('llama-server'), 'buildServeCmdString returns the plain command string');
}

// ── moe_env in a vLLM command only fires with verified MoE ──────────────────
{
  const linux = { ...baseCtx, arch: DENSE };
  const receiptDense = serve.buildServeCmd({ moe_env: true, port: '8000', ctx: '8192' }, 'Qwen3.5-27B', 'vllm', linux);
  assert(!receiptDense.cmd.includes('VLLM_USE_DEEP_GEMM'), 'moe_env: dense-verified model gets no forced MoE env vars from the fallback default either');

  const moeCtx = { ...baseCtx, arch: MOE };
  const receiptMoe = serve.buildServeCmd({ moe_env: true, port: '8000', ctx: '8192' }, 'Qwen3.5-27B-A10B', 'vllm', moeCtx);
  assert(receiptMoe.cmd.includes('VLLM_USE_DEEP_GEMM'), 'moe_env: verified-MoE model gets its family MoE env vars');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nINF-01 serve veracity: all checks passed');
