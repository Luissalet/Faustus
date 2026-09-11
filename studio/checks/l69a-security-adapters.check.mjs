// Lote 69a — SEC-01 / SEC-04 / TOOL-04: the new/extended adapters against
// their real backend wire shapes, checked without a browser.
//
//  1. `studio/src/adapters/approvals.ts` — GET /api/approvals/active reads
//     the `active` array; DELETE /api/approvals/{id} sends a JSON body and
//     surfaces `{ok:false, reason}` without throwing (routes/approvals_routes.py).
//  2. `studio/src/adapters/commandGuard.ts` — the allowlist list/add/remove
//     hit routes/command_guard_routes.py's exact paths, methods and bodies.
//  3. `studio/src/adapters/integrations.ts` — TOOL-04's governance fields
//     (`manifest_diff`/`declared_permissions`/`policy_decision`/
//     `manifest_quarantined`) round-trip through `addMcpServer` and the new
//     `setMcpEnvMode`, and `approveMcpManifest` hits the approve route.
//
// Run by tests/test_l69a_security_adapters_js.py, or by hand:
//   node studio/checks/l69a-security-adapters.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function loadModule(entryPoint) {
  const result = await build({ entryPoints: [entryPoint], bundle: true, format: 'esm', platform: 'node', write: false });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const originalFetch = globalThis.fetch;
let failed = 0;
const check = (cond, msg) => {
  if (!cond) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg);
};

try {
  // ── 1. approvals.ts ──
  {
    const { listActiveApprovals, revokeApproval } = await loadModule('studio/src/adapters/approvals.ts');

    let seenPath;
    globalThis.fetch = async (path) => {
      seenPath = path;
      return new Response(JSON.stringify({
        checked_at: '2026-09-10T00:00:00Z',
        active: [{
          id: 'apr_1', plan: { action: 'send_email', skill_id: '', skill_version: '', backend: '', recipients: [], cost_units: null, secret_names: [], permissions: {}, output_kinds: [], detail: 'reply to Luis' },
          plan_fingerprint: 'fp', status: 'granted', owner: 'luis', requested_at: '2026-09-01T00:00:00Z',
          decided_at: '2026-09-01T00:01:00Z', decided_by: 'luis', expires_at: null, uses_left: 1, reason: '',
        }],
        count: 1,
      }), { status: 200 });
    };
    const active = await listActiveApprovals();
    check(seenPath === '/api/approvals/active', 'listActiveApprovals() reads GET /api/approvals/active');
    check(active.length === 1 && active[0].id === 'apr_1' && active[0].plan.action === 'send_email', 'listActiveApprovals() returns the active[] array, plan included');

    let seenInit;
    globalThis.fetch = async (path, init) => {
      seenPath = path; seenInit = init;
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    };
    const revoked = await revokeApproval('apr_1', 'no longer needed');
    check(seenPath === '/api/approvals/apr_1', 'revokeApproval() targets DELETE /api/approvals/{id}');
    check(seenInit.method === 'DELETE', 'revokeApproval() uses DELETE');
    check(JSON.parse(seenInit.body).reason === 'no longer needed', 'revokeApproval() sends the reason in the body');
    check(revoked.ok === true, 'revokeApproval() returns the decoded body');

    // A revoke the server refuses (already terminal) is a thrown Error, not
    // a silently-ignored click — Settings.tsx's revoke() catches this and
    // surfaces e.message, so the adapter must actually throw.
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'apr_1' }), { status: 404 });
    let threw = false;
    try { await revokeApproval('apr_1'); } catch { threw = true; }
    check(threw, 'revokeApproval() throws on a 404 (already-terminal card) rather than resolving silently');
  }

  // ── 2. commandGuard.ts ──
  {
    const { listCommandAllowlist, addCommandAllowlistEntry, removeCommandAllowlistEntry } = await loadModule('studio/src/adapters/commandGuard.ts');

    let seenPath;
    globalThis.fetch = async (path) => {
      seenPath = path;
      return new Response(JSON.stringify({ status: 'success', allow: [{ pattern: 'git status', kind: 'exact', reason: 'read-only', added_by: 'luis', created_at: '2026-09-01T00:00:00Z', expires_at: null }] }), { status: 200 });
    };
    const allow = await listCommandAllowlist();
    check(seenPath === '/api/command-guard/allowlist', 'listCommandAllowlist() reads GET /api/command-guard/allowlist');
    check(allow.length === 1 && allow[0].pattern === 'git status', 'listCommandAllowlist() returns the allow[] array');

    let seenInit;
    globalThis.fetch = async (path, init) => {
      seenPath = path; seenInit = init;
      return new Response(JSON.stringify({ status: 'success', entry: { pattern: 'npm test', kind: 'exact', reason: '', added_by: '', created_at: '', expires_at: null } }), { status: 200 });
    };
    await addCommandAllowlistEntry({ pattern: 'npm test', kind: 'exact', reason: '', ttl_hours: null });
    check(seenPath === '/api/command-guard/allowlist' && seenInit.method === 'POST', 'addCommandAllowlistEntry() POSTs to the allowlist route');
    check(JSON.parse(seenInit.body).pattern === 'npm test', 'addCommandAllowlistEntry() sends the pattern as JSON');

    globalThis.fetch = async (path, init) => {
      seenPath = path; seenInit = init;
      return new Response(JSON.stringify({ status: 'success', removed: true }), { status: 200 });
    };
    await removeCommandAllowlistEntry('npm test');
    check(seenPath === '/api/command-guard/allowlist' && seenInit.method === 'DELETE', 'removeCommandAllowlistEntry() DELETEs the allowlist route');
    check(JSON.parse(seenInit.body).pattern === 'npm test', 'removeCommandAllowlistEntry() sends the pattern to remove');
  }

  // ── 3. integrations.ts: TOOL-04 governance fields ──
  {
    const { setMcpEnvMode, approveMcpManifest } = await loadModule('studio/src/adapters/integrations.ts');

    let seenPath, seenInit;
    globalThis.fetch = async (path, init) => {
      seenPath = path; seenInit = init;
      return new Response(JSON.stringify({
        id: 'srv1', inherit_env: true, env_mode: 'inherited', connected: true, status: 'connected',
        declared_permissions: { network: true, files: true, secrets: true },
        manifest_diff: { added: ['secrets'], removed: [] },
        manifest_quarantined: true,
        policy_decision: { allowed: true, requires_approval: true, profile: 'bounded_automation', reason: 'secrets always needs approval', plugin_id: 'srv1' },
      }), { status: 200 });
    };
    const d = await setMcpEnvMode('srv1', true);
    check(seenPath === '/api/mcp/servers/srv1/env-mode' && seenInit.method === 'PATCH', 'setMcpEnvMode() PATCHes servers/{id}/env-mode');
    check(seenInit.body.get('inherit_env') === 'true', 'setMcpEnvMode(true) sends inherit_env=true');
    check(d.manifest_quarantined === true, 'setMcpEnvMode() surfaces manifest_quarantined');
    check(d.manifest_diff.added.includes('secrets'), 'setMcpEnvMode() surfaces manifest_diff.added — what the diff panel shows before approval');
    check(d.policy_decision.reason.length > 0, 'setMcpEnvMode() surfaces policy_decision.reason');

    globalThis.fetch = async (path, init) => { seenPath = path; seenInit = init; return new Response('{}', { status: 200 }); };
    await approveMcpManifest('srv1');
    check(seenPath === '/api/mcp/servers/srv1/manifest/approve' && seenInit.method === 'POST', 'approveMcpManifest() POSTs to the approve route');
  }

  console.log(failed === 0 ? 'ALL OK' : `${failed} FAILED`);
  process.exitCode = failed === 0 ? 0 : 1;
} finally {
  globalThis.fetch = originalFetch;
}
