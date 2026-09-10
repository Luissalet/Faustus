// Lote 55 item 4 — SET-02: studio/src/adapters/fit.ts's endpointProfiles /
// costLabel / fetchModelCapabilities against routes/local_models_routes.py's
// real wire shape (snake_case in, camelCase out) — same esbuild-bundle-
// and-stub-fetch shape studio/checks/activity-feed.check.mjs uses.
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const result = await build({ entryPoints: ['studio/src/adapters/fit.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const { endpointProfiles, costLabel, fetchModelCapabilities } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);

const originalFetch = globalThis.fetch;
try {
  // ── endpointProfiles: snake_case -> camelCase, and cached for the module's lifetime ──
  let calls = 0;
  globalThis.fetch = async (path) => {
    calls += 1;
    assert.equal(path, '/api/models/endpoint-profile');
    return new Response(JSON.stringify({
      endpoints: {
        'local-ollama': { is_local: true, cost: 'free_local', has_api_key: false },
        'cloud-keyed': { is_local: false, cost: 'paid', has_api_key: true },
      },
    }), { status: 200 });
  };
  const first = await endpointProfiles();
  assert.deepEqual(first, {
    'local-ollama': { isLocal: true, cost: 'free_local', hasApiKey: false },
    'cloud-keyed': { isLocal: false, cost: 'paid', hasApiKey: true },
  });
  await endpointProfiles();
  assert.equal(calls, 1, 'endpointProfiles() is cached — the picker opening twice does not ask twice');
  const refreshed = await endpointProfiles(true);
  assert.equal(calls, 2, 'refresh=true bypasses the cache');
  assert.deepEqual(refreshed, first);

  // A failed read keeps the picker usable — empty object, not a thrown error.
  globalThis.fetch = async () => { throw new Error('offline'); };
  assert.deepEqual(await endpointProfiles(true), {});

  // ── costLabel: one sentence per cost bucket, never blank ──
  assert.match(costLabel('free_local'), /no per-token cost|Local/);
  assert.match(costLabel('paid'), /provider/);
  assert.match(costLabel('unconfigured'), /likely fail/);

  // ── fetchModelCapabilities: only entries the server actually tested, and only tested keys survive ──
  let seenPath;
  globalThis.fetch = async (path) => {
    seenPath = path;
    return new Response(JSON.stringify({
      tested: {
        tool_calling: { ok: true, tested_at: '2026-09-10T00:00:00Z' },
        vision: { ok: false, tested_at: '2026-09-10T00:00:00Z' },
        json_mode: {},                       // present in the manifest but never actually run
        context_length_effective: { ok: true },  // a real tested key this picker row does not surface
      },
    }), { status: 200 });
  };
  const manifest = await fetchModelCapabilities('qwen3:8b', 'local-ollama');
  assert.equal(seenPath, '/api/models/qwen3%3A8b/capabilities?endpoint_id=local-ollama');
  assert.deepEqual(manifest.tested, {
    tool_calling: { ok: true, testedAt: '2026-09-10T00:00:00Z' },
    vision: { ok: false, testedAt: '2026-09-10T00:00:00Z' },
  });
  assert.ok(!('json_mode' in manifest.tested), 'an untested key (no `ok`) is not shown as tested');
  assert.ok(!('context_length_effective' in manifest.tested), 'only the three keys this row cares about survive');
} finally {
  globalThis.fetch = originalFetch;
}
console.log('ALL OK: endpoint profile caching/mapping, cost labels, and capability manifest filtering');
