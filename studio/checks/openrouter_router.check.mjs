// OBJ-8 / Lote B1 — OpenRouter per-endpoint preferences + the MOD-05 model
// router, exposed in the Studio (contract: scratchpad/CONTRATO_OBJ8_B.md).
//
//  1. `studio/src/adapters/openrouter.ts` and `studio/src/adapters/
//     modelRouter.ts` against their real wire shapes (`docs/api/
//     openrouter.md`, `docs/api/model_router.md`) — same esbuild-bundle-
//     and-stub-fetch shape `studio/checks/l69a-security-adapters.check.mjs`
//     uses.
//  2. Source inspection (no bundling — the same posture `studio/checks/
//     l65-source-wiring.check.mjs` uses) that the new screens actually wire
//     to the endpoints above: `OpenRouterPrefs.tsx` reads/writes the
//     per-endpoint prefs route and gates on `isOpenRouterEndpoint`,
//     `ModelRouter.tsx` reads the router config route and states the MOD-05
//     chat-turn integration is pending, and `Settings.tsx` registers both
//     as sections and offers the per-endpoint "OpenRouter preferences"
//     jump from Models.
//  3. Every `t('…')`/`t("…")` key literal used by those three files has a
//     row in `docs/ui/i18n/es.tsv` — the table `scripts/i18n_es.py` builds
//     `studio/src/i18n/es.ts` from.
//
// Run by tests/test_l99_studio_openrouter_router_js.py, or by hand:
//   node studio/checks/openrouter_router.check.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { build } from 'esbuild';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p) => readFileSync(join(root, p), 'utf8');

async function loadModule(entryPoint) {
  const result = await build({ entryPoints: [entryPoint], bundle: true, format: 'esm', platform: 'node', write: false });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

let failed = 0;
const check = (cond, msg) => {
  if (!cond) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg);
};

const originalFetch = globalThis.fetch;
try {
  // ── 1a. adapters/openrouter.ts against docs/api/openrouter.md ──
  {
    const mod = await loadModule('studio/src/adapters/openrouter.ts');
    const {
      isOpenRouterEndpoint, getOpenRouterEndpointPrefs, putOpenRouterEndpointPrefs,
      deleteOpenRouterEndpointPrefs, getAllOpenRouterPrefs, diffOpenRouterPrefs,
      openRouterPrefsDirty, providerListFromText, providerListToText,
      DEFAULT_OPENROUTER_PREFS, OpenRouterApiError,
    } = mod;

    check(isOpenRouterEndpoint('https://openrouter.ai/api/v1') === true, 'isOpenRouterEndpoint() accepts openrouter.ai');
    check(isOpenRouterEndpoint('https://eu.openrouter.ai/api/v1') === true, 'isOpenRouterEndpoint() accepts a subdomain of openrouter.ai');
    check(isOpenRouterEndpoint('https://api.openai.com/v1') === false, 'isOpenRouterEndpoint() rejects an unrelated host');
    check(isOpenRouterEndpoint('not a url') === false, "isOpenRouterEndpoint() rejects garbage instead of throwing");

    let seenPath;
    globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify(DEFAULT_OPENROUTER_PREFS), { status: 200 }); };
    const prefs = await getOpenRouterEndpointPrefs('ep1');
    check(seenPath === '/api/openrouter/endpoints/ep1/prefs', 'getOpenRouterEndpointPrefs() reads GET /api/openrouter/endpoints/{id}/prefs');
    check(prefs.data_collection === 'auto' && prefs.sort === '', 'getOpenRouterEndpointPrefs() decodes the schema defaults');

    globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify({ endpoints: { ep1: DEFAULT_OPENROUTER_PREFS } }), { status: 200 }); };
    const all = await getAllOpenRouterPrefs();
    check(seenPath === '/api/openrouter/prefs', 'getAllOpenRouterPrefs() reads GET /api/openrouter/prefs');
    check(all.endpoints.ep1.zdr === false, 'getAllOpenRouterPrefs() surfaces the endpoints map');

    let seenInit;
    globalThis.fetch = async (path, init) => { seenPath = path; seenInit = init; return new Response(JSON.stringify({ ...DEFAULT_OPENROUTER_PREFS, sort: 'price', zdr: true }), { status: 200 }); };
    const saved = await putOpenRouterEndpointPrefs('ep1', { sort: 'price', zdr: true });
    check(seenPath === '/api/openrouter/endpoints/ep1/prefs' && seenInit.method === 'PUT', 'putOpenRouterEndpointPrefs() PUTs endpoints/{id}/prefs');
    check(JSON.parse(seenInit.body).sort === 'price' && JSON.parse(seenInit.body).zdr === true, 'putOpenRouterEndpointPrefs() sends only the patch given');
    check(saved.sort === 'price' && saved.zdr === true, 'putOpenRouterEndpointPrefs() returns the merged document');

    globalThis.fetch = async (path, init) => { seenPath = path; seenInit = init; return new Response(JSON.stringify({ deleted: true, endpoint_id: 'ep1' }), { status: 200 }); };
    const del = await deleteOpenRouterEndpointPrefs('ep1');
    check(seenPath === '/api/openrouter/endpoints/ep1/prefs' && seenInit.method === 'DELETE', 'deleteOpenRouterEndpointPrefs() DELETEs endpoints/{id}/prefs');
    check(del.deleted === true, 'deleteOpenRouterEndpointPrefs() returns the decoded body');

    // 400 with an error_class — the contract's own convention for a
    // rejected patch (routes/git_routes.py::_error's shape, reused here).
    globalThis.fetch = async () => new Response(JSON.stringify({ error_class: 'openrouter.invalid_prefs', detail: 'sort must be one of…' }), { status: 400 });
    let threw = null;
    try { await putOpenRouterEndpointPrefs('ep1', { sort: 'bogus' }); } catch (e) { threw = e; }
    check(threw instanceof OpenRouterApiError && threw.errorClass === 'openrouter.invalid_prefs', 'a rejected PUT throws OpenRouterApiError carrying error_class');

    const base = DEFAULT_OPENROUTER_PREFS;
    const draft = { ...base, sort: 'price', order: ['anthropic'] };
    check(openRouterPrefsDirty(base, base) === false, 'openRouterPrefsDirty() is false against itself');
    check(openRouterPrefsDirty(base, draft) === true, 'openRouterPrefsDirty() is true once sort/order changed');
    const patch = diffOpenRouterPrefs(base, draft);
    check(patch.sort === 'price' && Array.isArray(patch.order) && patch.order[0] === 'anthropic' && !('zdr' in patch), 'diffOpenRouterPrefs() sends only the changed fields');

    check(providerListFromText('anthropic, openai,\nmistral').length === 3, 'providerListFromText() splits on commas and newlines');
    check(providerListToText(['a', 'b']) === 'a, b', 'providerListToText() joins with ", "');
  }

  // ── 1b. adapters/modelRouter.ts against docs/api/model_router.md ──
  {
    const mod = await loadModule('studio/src/adapters/modelRouter.ts');
    const {
      getModelRouterConfig, updateModelRouterConfig, previewModelRouterDecision,
      getModelRouterLog, getModelRouterStats, diffModelRouterConfig,
      modelRouterConfigDirty, modelListFromText, modelListToText, summarizeDecision,
      DEFAULT_MODEL_ROUTER_CONFIG, ModelRouterApiError,
    } = mod;

    let seenPath, seenInit;
    globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify(DEFAULT_MODEL_ROUTER_CONFIG), { status: 200 }); };
    const cfg = await getModelRouterConfig();
    check(seenPath === '/api/model-router/config', 'getModelRouterConfig() reads GET /api/model-router/config');
    check(cfg.enabled === false && cfg.allow_paid_escalation === false, 'getModelRouterConfig() decodes the inert defaults');

    globalThis.fetch = async (path, init) => { seenPath = path; seenInit = init; return new Response(JSON.stringify({ ...DEFAULT_MODEL_ROUTER_CONFIG, enabled: true }), { status: 200 }); };
    const updated = await updateModelRouterConfig({ enabled: true });
    check(seenPath === '/api/model-router/config' && seenInit.method === 'PUT', 'updateModelRouterConfig() PUTs /api/model-router/config');
    check(JSON.parse(seenInit.body).enabled === true, 'updateModelRouterConfig() sends the patch as JSON');
    check(updated.enabled === true, 'updateModelRouterConfig() returns the saved config');

    globalThis.fetch = async (path, init) => {
      seenPath = path; seenInit = init;
      return new Response(JSON.stringify({
        decision: { model: 'qwen2.5:7b', reason: 'ok', alternatives: [], escalated: false, escalation: null },
        explain: 'Modelo elegido: qwen2.5:7b',
      }), { status: 200 });
    };
    const preview = await previewModelRouterDecision({ capabilities: ['tool_call'], max_latency_s: 8 }, ['qwen2.5:7b']);
    check(seenPath === '/api/model-router/preview' && seenInit.method === 'POST', 'previewModelRouterDecision() POSTs /api/model-router/preview');
    const sentBody = JSON.parse(seenInit.body);
    check(Array.isArray(sentBody.requirements.capabilities) && sentBody.installed[0] === 'qwen2.5:7b', 'previewModelRouterDecision() sends requirements + installed');
    check(preview.decision.model === 'qwen2.5:7b' && preview.decision.escalated === false, 'previewModelRouterDecision() surfaces the decision');
    check(summarizeDecision(preview.decision) === 'qwen2.5:7b', 'summarizeDecision() names the chosen model');
    check(summarizeDecision({ model: null, escalated: true, reason: '', alternatives: [], escalation: {} }) === 'escalated', 'summarizeDecision() reports "escalated" — never invents a model name');

    globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify({ entries: [{ ts: '2026-09-11T00:00:00Z', requested: 'auto', chosen: 'qwen2.5:7b', reason: 'ok', escalated: false }] }), { status: 200 }); };
    const log = await getModelRouterLog(9999); // clamped
    check(seenPath === '/api/model-router/log?limit=2000', 'getModelRouterLog() clamps an over-large limit to 2000');
    check(log.entries.length === 1 && log.entries[0].chosen === 'qwen2.5:7b', 'getModelRouterLog() surfaces the entries[] array');

    globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify({ stats: { 'qwen2.5:7b': { ok: 12, fail: 0, ewma_latency_s: 3.2, last_error_class: null, updated_at: '2026-09-11T10:00:00Z' } } }), { status: 200 }); };
    const stats = await getModelRouterStats();
    check(seenPath === '/api/model-router/stats', 'getModelRouterStats() reads GET /api/model-router/stats');
    check(stats.stats['qwen2.5:7b'].ok === 12, 'getModelRouterStats() surfaces per-model history');

    globalThis.fetch = async () => new Response(JSON.stringify({ error_class: 'model_router.invalid_config', detail: 'candidates must be a list' }), { status: 400 });
    let threw = null;
    try { await updateModelRouterConfig({ candidates: 'not-a-list' }); } catch (e) { threw = e; }
    check(threw instanceof ModelRouterApiError && threw.errorClass === 'model_router.invalid_config', 'a rejected PUT throws ModelRouterApiError carrying error_class');

    const base = DEFAULT_MODEL_ROUTER_CONFIG;
    const draft = { ...base, enabled: true, min_capabilities: ['tool_call'] };
    check(modelRouterConfigDirty(base, base) === false, 'modelRouterConfigDirty() is false against itself');
    check(modelRouterConfigDirty(base, draft) === true, 'modelRouterConfigDirty() is true once enabled/min_capabilities changed');
    const patch2 = diffModelRouterConfig(base, draft);
    check(patch2.enabled === true && Array.isArray(patch2.min_capabilities) && !('prefer_local' in patch2), 'diffModelRouterConfig() sends only the changed fields');

    check(modelListFromText('a, a, b\nc').length === 3, 'modelListFromText() dedupes while splitting on commas and newlines');
    check(modelListToText(['a', 'b']) === 'a, b', 'modelListToText() joins with ", "');
  }

  // ── 2. Source wiring: the screens actually call the routes above, and
  //      Settings.tsx actually registers both sections + the per-endpoint
  //      jump from Models. ──
  const orScreen = read('studio/src/screens/settings/OpenRouterPrefs.tsx');
  const mrScreen = read('studio/src/screens/settings/ModelRouter.tsx');
  const settingsScreen = read('studio/src/screens/Settings.tsx');

  check(orScreen.includes("from '../../adapters/openrouter'"), 'OpenRouterPrefs.tsx imports adapters/openrouter.ts, never fetches directly');
  check(orScreen.includes('isOpenRouterEndpoint'), 'OpenRouterPrefs.tsx filters endpoints with isOpenRouterEndpoint()');
  check(orScreen.includes('getOpenRouterEndpointPrefs') && orScreen.includes('putOpenRouterEndpointPrefs') && orScreen.includes('deleteOpenRouterEndpointPrefs'), 'OpenRouterPrefs.tsx calls GET/PUT/DELETE on the per-endpoint prefs');
  check(/GET \/api\/openrouter\/endpoints\/\{id\}\/prefs failed\./.test(orScreen), 'OpenRouterPrefs.tsx names the real route in its failure copy');
  check(!/\bfetch\(/.test(orScreen), 'OpenRouterPrefs.tsx never calls fetch() directly (adapter-only, guard rule)');

  check(mrScreen.includes("from '../../adapters/modelRouter'"), 'ModelRouter.tsx imports adapters/modelRouter.ts, never fetches directly');
  check(mrScreen.includes('getModelRouterConfig') && mrScreen.includes('updateModelRouterConfig'), 'ModelRouter.tsx reads and patches the router config');
  check(mrScreen.includes('previewModelRouterDecision'), 'ModelRouter.tsx wires the "Probar decisión" preview call');
  check(mrScreen.includes('getModelRouterLog') && mrScreen.includes('getModelRouterStats'), 'ModelRouter.tsx wires both the Registro and Estadísticas tabs');
  check(mrScreen.includes('MOD-05'), 'ModelRouter.tsx states the chat-turn integration is pending (MOD-05), per docs/api/model_router.md');
  check(!/\bfetch\(/.test(mrScreen), 'ModelRouter.tsx never calls fetch() directly (adapter-only, guard rule)');

  check(/'openrouter'.*label:\s*'OpenRouter'/.test(settingsScreen) || /key:\s*'openrouter'/.test(settingsScreen), "Settings.tsx registers the 'openrouter' section");
  check(/key:\s*'model_router'/.test(settingsScreen), "Settings.tsx registers the 'model_router' section");
  check(settingsScreen.includes('<OpenRouterPrefsSection'), 'Settings.tsx renders OpenRouterPrefsSection for its section');
  check(settingsScreen.includes('<ModelRouterSection'), 'Settings.tsx renders ModelRouterSection for its section');
  check(settingsScreen.includes('isOpenRouterEndpoint(ep.baseUrl)') && settingsScreen.includes('onOpenOpenRouterPrefs'), "Settings.tsx's Models list offers the per-endpoint OpenRouter preferences jump");

  // ── 3. Every t() key literal these three files use has a Spanish row. ──
  const tsvKeys = new Set();
  for (const line of read('docs/ui/i18n/es.tsv').split('\n')) {
    const tab = line.indexOf('\t');
    if (tab > 0) tsvKeys.add(line.slice(0, tab));
  }
  const keyRe = [
    /(?<![A-Za-z_.])t\(\s*'((?:[^'\\]|\\.)*)'/g,
    /(?<![A-Za-z_.])t\(\s*"((?:[^"\\]|\\.)*)"/g,
  ];
  const tnRe = /(?<![A-Za-z_.])tn\([^,]+,\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'/g;
  function keysUsedIn(src) {
    const keys = new Set();
    for (const re of keyRe) for (const m of src.matchAll(re)) keys.add(m[1].replace(/\\'/g, "'"));
    for (const m of src.matchAll(tnRe)) { keys.add(m[1].replace(/\\'/g, "'")); keys.add(m[2].replace(/\\'/g, "'")); }
    return keys;
  }
  for (const [name, src] of [['OpenRouterPrefs.tsx', orScreen], ['ModelRouter.tsx', mrScreen]]) {
    const used = keysUsedIn(src);
    const missing = [...used].filter((k) => !tsvKeys.has(k));
    check(used.size > 0, `${name} uses at least one t() key (the scan itself is not silently matching nothing)`);
    check(missing.length === 0, `${name}: every t() key has a row in docs/ui/i18n/es.tsv${missing.length ? ' — missing: ' + missing.join(' | ') : ''}`);
  }
  // The two new section labels and the per-endpoint jump button's label,
  // read straight out of Settings.tsx's own literals.
  for (const key of ['OpenRouter', 'Model router', 'OpenRouter preferences']) {
    check(tsvKeys.has(key), `docs/ui/i18n/es.tsv has a row for "${key}" (Settings.tsx)`);
  }

  console.log(failed === 0 ? 'ALL OK' : `${failed} FAILED`);
  process.exitCode = failed === 0 ? 0 : 1;
} finally {
  globalThis.fetch = originalFetch;
}
