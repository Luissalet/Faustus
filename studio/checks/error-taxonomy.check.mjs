// UX-08: `error_class` (OBS-03, `src/contracts/errors.py`) → a title and an
// action in the person's own language, instead of a provider's raw JSON.
//
// This mirrors the Python module's `_DEFAULTS` table by name, category by
// category, so the two stay honest about which category means what;
// `friendlyError` additionally covers the concrete failure mode this lote
// was asked to close: a task/automation failure reason that IS the literal
// `_stream_error_chunk`/§34.5 error payload, stored verbatim.
//
// Run by tests/test_studio_error_taxonomy_js.py, or by hand:
//   node studio/checks/error-taxonomy.check.mjs
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const output = join(mkdtempSync(join(tmpdir(), 'faustus-errclass-')), 'fixture.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'components', 'errorTaxonomy.ts')],
  bundle: true, format: 'esm', platform: 'node', outfile: output, logLevel: 'silent',
});
const { describeError, friendlyError, ERROR_CATEGORIES } = await import(pathToFileURL(output).href);

// ── The ten OBS-03 categories, unchanged ──
assert.deepEqual(
  [...ERROR_CATEGORIES].sort(),
  ['capability', 'cancelled', 'conflict', 'permission', 'resource', 'schema', 'timeout', 'transport', 'unknown', 'verification'].sort(),
);

// ── Every category resolves to non-empty, non-raw text ──
for (const category of ERROR_CATEGORIES) {
  const d = describeError(`${category}.some_subcode`);
  assert.equal(d.category, category, category);
  assert.equal(d.subcode, 'some_subcode', category);
  assert.ok(d.title && d.title !== `${category}.some_subcode`, `${category}: title must not just echo the code`);
  assert.ok(d.action, `${category}: must name an action`);
}

// ── The four actions the lote brief names by example ──
assert.equal(describeError('transport.llm_service_error').action, 'Retry');
assert.equal(describeError('timeout.deadline_exceeded').action, 'Retry');
assert.equal(describeError('permission.approval_required').action, 'Request permission');
assert.equal(describeError('capability.backend_unavailable').action, 'Change model');
assert.equal(describeError('cancelled.user_stopped').action, 'Resume');

// ── Retryability matches `_DEFAULTS` (transport/timeout only) ──
assert.equal(describeError('transport.network_unreachable').retryable, true);
assert.equal(describeError('timeout.deadline_exceeded').retryable, true);
assert.equal(describeError('permission.denied').retryable, false);
assert.equal(describeError('unknown.panic').retryable, false);

// ── Absent / unrecognised code: something is always shown, never blank ──
assert.equal(describeError(undefined).category, null);
assert.ok(describeError(undefined).title);
assert.equal(describeError('a_tool_defined_code_with_no_dot').category, null);
assert.equal(describeError('not_a_real_category.subcode').category, null);

// ── friendlyError: the actual bug this lote closes ──
{
  // A provider failure stored as the literal SSE error payload.
  const raw = JSON.stringify({ error: 'model backend is down', error_class: 'transport.llm_service_error', status: 502, attempts: 3, retryable: true });
  const f = friendlyError(raw);
  assert.equal(f.category, 'transport');
  assert.equal(f.message, 'model backend is down', 'the human message, not the whole JSON blob');
  assert.notEqual(f.message, raw);
}
{
  // §34.5's status-error shape uses `text` rather than `error`.
  const raw = JSON.stringify({ status: 429, text: 'rate limited', raw: '...', error_class: 'resource.rate_limited' });
  assert.equal(friendlyError(raw).message, 'rate limited');
}
{
  // Plain text (the overwhelmingly common case today) passes through.
  const f = friendlyError('The connection to the model timed out.');
  assert.equal(f.category, null);
  assert.equal(f.message, 'The connection to the model timed out.');
}
{
  // JSON with no error_class: still shown, never crashes, category stays null.
  const f = friendlyError('{"detail": "not our shape"}');
  assert.equal(f.category, null);
}
{
  // Malformed JSON starting with `{`: falls back to the raw text, no throw.
  const f = friendlyError('{not valid json');
  assert.equal(f.message, '{not valid json');
}
assert.equal(friendlyError(null).message, '');
assert.equal(friendlyError(undefined).message, '');

console.log('Error taxonomy: ALL OK');
