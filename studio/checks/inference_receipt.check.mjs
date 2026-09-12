// INF-02 (Lote B — Studio) — capability assessments and per-session launch
// receipts: `buildServePlan` (studio/src/lib/cookbook/serve.ts) reports
// canonical manifest option names, never the form's own field names; the
// adapter (studio/src/adapters/cookbook.ts) hits the exact INF-02 routes
// and their exact wire shapes (routes/inference_routes.py,
// src/contracts/inference.py), and its three pure helpers
// (`summarizeReceipt`, `receiptTone`, `baseUrlFromCmd`) behave as documented.
// Drives the real TypeScript through esbuild, same pattern as
// `studio/checks/serve_veracity.check.mjs` (pure functions) and
// `studio/checks/l69a-security-adapters.check.mjs` (network adapters).
//
// Run by tests/test_inf02_receipt_js.py, or by hand:
//   node studio/checks/inference_receipt.check.mjs
import { build } from 'esbuild';

async function loadModule(entryPoint) {
  const result = await build({ entryPoints: [entryPoint], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const originalFetch = globalThis.fetch;
let failed = 0;
const check = (cond, msg) => {
  if (!cond) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const baseCtx = { platform: 'linux', remoteHost: '', env: 'none', envPath: '', hwBackend: 'cuda', hostPlatform: 'linux' };
const MOE = { repo: 'x', kind: 'moe', total_params: 1, active_params: 1, num_experts: 8, mtp: null, architectures: ['X'], source: 'hf_config', observed_at: null, note: null };

try {
  // ── buildServePlan (studio/src/lib/cookbook/serve.ts) ──────────────────
  {
    const serve = await loadModule('studio/src/lib/cookbook/serve.ts');

    // llama.cpp: canonical names, never the form's internal field names.
    const llamaFields = {
      ctx: '16384', ngl: '99', flash_attn: true, cache_type: 'q8_0', n_cpu_moe: '4',
      llama_tensor_split: '1,1', llama_main_gpu: '0', llama_parallel: '2',
      llama_batch_size: '2048', llama_ubatch_size: '512', llama_fit: 'on',
      llama_speculative_mtp: true,
    };
    const linux = { ...baseCtx };
    const llamaReceipt = serve.buildServeCmd(llamaFields, 'model.gguf', 'llamacpp', linux);
    const plan = serve.buildServePlan(llamaFields, 'model.gguf', llamaReceipt, null);
    check(plan.implementation === 'llama-server', 'buildServePlan: implementation comes from the receipt');
    check(plan.model === 'model.gguf', 'buildServePlan: model is the resolved model argument');
    check(plan.options.ctx === 16384 && plan.options.ngl === 99, 'buildServePlan: ctx/ngl are canonical manifest names, numeric');
    check(plan.options.cache_type_k === 'q8_0' && plan.options.cache_type_v === 'q8_0', 'buildServePlan: one form field fans out to cache_type_k AND cache_type_v');
    check(plan.options.tensor_split === '1,1' && plan.options.parallel === 2 && plan.options.batch_size === 2048 && plan.options.ubatch_size === 512, 'buildServePlan: llama-server-only options use their manifest names');
    check(plan.options.n_cpu_moe === 4, 'buildServePlan: n_cpu_moe included when set > 0');
    check(plan.options.fit === 'on', 'buildServePlan: fit carries the select value verbatim');
    check(plan.options.mtp === true, 'buildServePlan: llama_speculative_mtp -> canonical "mtp"');
    check(!('llama_parallel' in plan.options) && !('n_gpu_layers' in plan.options), "buildServePlan: never the form's own field spellings");

    // Remote Windows -> llama_cpp.server: SAME canonical names sent regardless
    // of which binary actually runs — assess_options (Python) is the one that
    // decides tensor_split/parallel/etc. are unsupported for the wrapper, not
    // this function silently withholding them.
    const remoteWin = { ...baseCtx, platform: 'windows', remoteHost: 'winbox' };
    const pyReceipt = serve.buildServeCmd(llamaFields, 'model.gguf', 'llamacpp', remoteWin);
    check(pyReceipt.implementation === 'llama_cpp.server', 'sanity: remote Windows selects the python wrapper');
    const pyPlan = serve.buildServePlan(llamaFields, 'model.gguf', pyReceipt, null);
    check(pyPlan.implementation === 'llama_cpp.server', 'buildServePlan: python wrapper implementation carried through');
    check(pyPlan.options.tensor_split === '1,1' && pyPlan.options.parallel === 2, 'buildServePlan: still sends the full option set for the wrapper (Python assesses omission, not this function)');

    // vLLM: ctx/dtype/gpu_mem always present (they always land in the
    // command with a default); expert_parallel/moe_env only when toggled on.
    const vllmPlanOff = serve.buildServePlan({ ctx: '8192', dtype: 'auto', gpu_mem: '0.9' }, 'Qwen3.5-27B', { ...llamaReceipt, implementation: 'vllm' }, null);
    check(vllmPlanOff.options.ctx === 8192 && vllmPlanOff.options.dtype === 'auto' && vllmPlanOff.options.gpu_mem === 0.9, 'buildServePlan/vllm: ctx/dtype/gpu_mem always present');
    check(!('expert_parallel' in vllmPlanOff.options), 'buildServePlan/vllm: expert_parallel absent when the switch is off');
    const vllmPlanOn = serve.buildServePlan({ ctx: '8192', expert_parallel: true, moe_env: true }, 'Qwen3.5-27B-A10B', { ...llamaReceipt, implementation: 'vllm' }, MOE);
    check(vllmPlanOn.options.expert_parallel === true && vllmPlanOn.options.moe_env === true, 'buildServePlan/vllm: expert_parallel/moe_env present when toggled on');
    check(vllmPlanOn.arch && vllmPlanOn.arch.kind === 'moe', 'buildServePlan: arch is carried through verbatim (never re-derived from the name)');

    // sglang: ctx/dtype/gpu_mem are omitted at their defaults (buildServeCmd
    // itself never emits a flag for them either).
    const sglangPlan = serve.buildServePlan({ ctx: '', dtype: 'auto', gpu_mem: '0.90' }, 'x', { ...llamaReceipt, implementation: 'sglang' }, null);
    check(Object.keys(sglangPlan.options).length === 0, 'buildServePlan/sglang: nothing sent when every field is still the default');
    const sglangPlan2 = serve.buildServePlan({ ctx: '4096', dtype: 'bfloat16', gpu_mem: '0.7' }, 'x', { ...llamaReceipt, implementation: 'sglang' }, null);
    check(sglangPlan2.options.ctx === 4096 && sglangPlan2.options.dtype === 'bfloat16' && sglangPlan2.options.gpu_mem === 0.7, 'buildServePlan/sglang: non-default values are sent');

    // ollama: launch itself takes no options (num_ctx/keep_alive are
    // per-request per the manifest, not part of `ollama serve`).
    const ollamaPlan = serve.buildServePlan({ ctx: '8192' }, 'llama3.2:latest', { ...llamaReceipt, implementation: 'ollama' }, null);
    check(Object.keys(ollamaPlan.options).length === 0, 'buildServePlan/ollama: no launch-time options');

    // A backend outside the manifest's coverage (image generators) has no
    // structured plan at all — not an error, the same legitimate case as a
    // manual command.
    const noPlan = serve.buildServePlan({}, 'x', { ...llamaReceipt, implementation: 'diffusers' }, null);
    check(noPlan === null, 'buildServePlan: null for an implementation the manifest does not cover (diffusers/mlx_image)');
  }

  // ── pure adapter helpers (studio/src/adapters/cookbook.ts) ─────────────
  {
    const { summarizeReceipt, receiptTone, baseUrlFromCmd, parseLaunchReceipt } = await loadModule('studio/src/adapters/cookbook.ts');

    const assessment = (option, state, requested = 1, value = null) => ({ option, requested, support: 'supported', scope: 'server_start', requirements: [], effective: { value, state }, benefit: { state: 'not_evaluated', benchmark_id: null }, evidence: { kind: 'engine_probe', observed_at: null }, reasons: [] });

    const mixed = { verify_state: 'verified', assessments: [assessment('ctx', 'confirmed', 8192, 8192), assessment('flash_attn', 'confirmed', true, true), assessment('cache_type_k', 'mismatch', 'q8_0', 'f16'), assessment('parallel', 'unconfirmed')] };
    check(summarizeReceipt(mixed) === '2 confirmed · 1 mismatch · 1 unconfirmed', `summarizeReceipt: exact counts in order — got "${summarizeReceipt(mixed)}"`);
    check(summarizeReceipt({ assessments: [] }) === 'nothing assessed', 'summarizeReceipt: empty assessments reads as "nothing assessed", not "0 confirmed"');

    check(receiptTone(mixed) === 'danger', 'receiptTone: a mismatch is danger even though most options confirmed');
    check(receiptTone({ verify_state: 'pending', assessments: [] }) === 'warning', 'receiptTone: pending (never verified) is warning');
    check(receiptTone({ verify_state: 'failed', assessments: [] }) === 'danger', 'receiptTone: verify_state failed is danger outright');
    check(receiptTone({ verify_state: 'verified', assessments: [assessment('ctx', 'unconfirmed')] }) === 'warning', 'receiptTone: verified but nothing confirmed (engine exposes nothing) is warning, not ok');
    check(receiptTone({ verify_state: 'verified', assessments: [assessment('ctx', 'confirmed', 8192, 8192)] }) === 'ok', 'receiptTone: verified with a real confirmation and no mismatch is ok');

    check(baseUrlFromCmd('llama-server --model x.gguf --port 8080 -c 8192') === 'http://127.0.0.1:8080', 'baseUrlFromCmd: reads --port');
    check(baseUrlFromCmd('OLLAMA_HOST=0.0.0.0:11500 ollama serve') === 'http://127.0.0.1:11500', 'baseUrlFromCmd: OLLAMA_HOST 0.0.0.0 never leaves this machine');
    check(baseUrlFromCmd('ollama serve') === 'http://127.0.0.1:11434', 'baseUrlFromCmd: bare "ollama serve" defaults to the documented port');
    check(baseUrlFromCmd('python3 -m pip install vllm') === null, 'baseUrlFromCmd: no port in the command -> null, never a guessed default');
    check(baseUrlFromCmd('') === null, 'baseUrlFromCmd: empty command -> null');

    const parsed = parseLaunchReceipt({ session_id: 's1', engine: { implementation: 'llama-server', generation: 2 }, requested_cmd: 'a', final_cmd: 'b', assessments: [{ option: 'ctx', requested: 1, support: 'supported', scope: 'server_start', effective: {}, benefit: {}, evidence: {} }], created_at: '2026-01-01T00:00:00Z' });
    check(parsed.engine.generation === 2 && parsed.engine.version === null, 'parseLaunchReceipt: numeric/absent engine fields round-trip, never coerced');
    check(parsed.assessments[0].effective.state === 'unconfirmed', 'parseLaunchReceipt: a missing effective.state defaults to "unconfirmed" (the contract default), never crashes');
    check(parsed.verify_state === 'pending', 'parseLaunchReceipt: a missing verify_state defaults to "pending"');
    check(Array.isArray(parsed.checks) && parsed.checks.length === 0, 'parseLaunchReceipt: missing checks/differences default to []');
  }

  // ── network calls (studio/src/adapters/cookbook.ts) ─────────────────────
  {
    const { assessServe, serveModel, getServeReceipt, verifyServeReceipt, ServeIncompatibleError } = await loadModule('studio/src/adapters/cookbook.ts');

    // assessServe -> POST /api/model/serve/assess
    let seenPath, seenInit;
    globalThis.fetch = async (path, init) => {
      seenPath = path; seenInit = init;
      return new Response(JSON.stringify({
        assessments: [{ option: 'expert_parallel', requested: true, support: 'unsupported', scope: 'server_start', requirements: ['arch.kind==moe'], effective: {}, benefit: {}, evidence: {}, reasons: ['requirement not met: arch.kind==moe'] }],
        blockers: [{ option: 'expert_parallel', requested: true, support: 'unsupported', scope: 'server_start', requirements: ['arch.kind==moe'], effective: {}, benefit: {}, evidence: {}, reasons: ['requirement not met: arch.kind==moe'] }],
      }), { status: 200 });
    };
    const a = await assessServe({ implementation: 'vllm', options: { expert_parallel: true }, arch: { kind: 'dense' } });
    check(seenPath === '/api/model/serve/assess' && JSON.parse(seenInit.body).implementation === 'vllm', 'assessServe: POSTs the plan to /api/model/serve/assess');
    check(a.blockers.length === 1 && a.blockers[0].option === 'expert_parallel', 'assessServe: blockers[] surfaces the unsupported option');
    check(a.assessments[0].reasons[0].includes('arch.kind==moe'), 'assessServe: reasons[] is preserved, not summarized away');

    // serveModel: 200 -> receipt attached
    globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, session_id: 'serve-1', receipt: { session_id: 'serve-1', engine: { implementation: 'vllm' }, created_at: '2026-01-01T00:00:00Z', verify_state: 'pending' } }), { status: 200 });
    const launched = await serveModel({ repo_id: 'x/y', cmd: 'vllm serve x' });
    check(launched.sessionId === 'serve-1', 'serveModel: session_id round-trips');
    check(launched.receipt && launched.receipt.verify_state === 'pending', 'serveModel: the receipt the launch just filed comes back attached, verify_state pending');

    // serveModel: 409 serve.incompatible -> ServeIncompatibleError, not a bare ApiError
    globalThis.fetch = async () => new Response(JSON.stringify({
      error: 'one or more requested options are unsupported for this implementation', error_class: 'serve.incompatible',
      assessments: [{ option: 'expert_parallel', requested: true, support: 'unsupported', scope: 'server_start', effective: {}, benefit: {}, evidence: {}, reasons: ['requirement not met: arch.kind==moe'] }],
    }), { status: 409 });
    let threw = null;
    try { await serveModel({ repo_id: 'x/y', cmd: 'vllm serve x', plan: { implementation: 'vllm', model: 'x', options: { expert_parallel: true }, arch: null }, force_manual: false }); } catch (e) { threw = e; }
    check(threw instanceof ServeIncompatibleError, 'serveModel: a 409 serve.incompatible throws ServeIncompatibleError specifically');
    check(threw && threw.assessments.length === 1 && threw.assessments[0].option === 'expert_parallel', 'serveModel: ServeIncompatibleError carries the assessments — no second round-trip needed to show why');

    // getServeReceipt: 404 -> null (not a thrown error the caller must catch
    // for the ordinary "no receipt yet" case)
    globalThis.fetch = async () => new Response(JSON.stringify({ error: 'no launch receipt', error_class: 'serve.receipt_not_found' }), { status: 404 });
    const missing = await getServeReceipt('serve-none');
    check(missing === null, 'getServeReceipt: 404 serve.receipt_not_found resolves to null, not a throw');

    globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify({ receipt: { session_id: 'serve-1', engine: { implementation: 'ollama' }, created_at: '2026-01-01T00:00:00Z', verify_state: 'verified' } }), { status: 200 }); };
    const got = await getServeReceipt('serve-1');
    check(seenPath === '/api/model/serve/serve-1/receipt' && got.verify_state === 'verified', 'getServeReceipt: GETs the exact receipt path');

    // verifyServeReceipt: POSTs base_url/authorized_probe, authorized_probe
    // defaults false so an accidental call never sends a real request.
    globalThis.fetch = async (path, init) => { seenPath = path; seenInit = init; return new Response(JSON.stringify({ receipt: { session_id: 'serve-1', engine: { implementation: 'ollama' }, created_at: '2026-01-01T00:00:00Z', verify_state: 'verified' } }), { status: 200 }); };
    await verifyServeReceipt('serve-1', { baseUrl: 'http://127.0.0.1:11434' });
    check(seenPath === '/api/model/serve/serve-1/verify', 'verifyServeReceipt: POSTs the exact verify path');
    const body1 = JSON.parse(seenInit.body);
    check(body1.base_url === 'http://127.0.0.1:11434' && body1.authorized_probe === false, 'verifyServeReceipt: authorized_probe defaults false when the caller does not opt in');
    await verifyServeReceipt('serve-1', { authorizedProbe: true });
    check(JSON.parse(seenInit.body).authorized_probe === true, 'verifyServeReceipt: authorized_probe true only when the caller passes it explicitly');
  }

  console.log(failed === 0 ? 'ALL OK' : `${failed} FAILED`);
  process.exitCode = failed === 0 ? 0 : 1;
} finally {
  globalThis.fetch = originalFetch;
}
