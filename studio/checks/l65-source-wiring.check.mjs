// Lote 65 — Studio: turno de chat. A few closures here are wiring across
// files a runtime bundle can't exercise in isolation (Studio.tsx's
// `pickWorkspace` needs a real DOM/Suspense boundary; the copy fix is text,
// not behaviour) — checked by source inspection instead, the same posture
// `tests/test_l29_studio_error_trace_decode_js.py`'s own
// `test_chat_event_declares_the_four_fields_as_optional` already uses for
// this file pair.
//
// Run by tests/test_l65_studio_events_js.py, or by hand:
//   node studio/checks/l65-source-wiring.check.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p) => readFileSync(join(root, p), 'utf8');

// ── QA-44 hueco 1: the WorkspaceDialog chunk is warmed BEFORE the async
// pickNative() attempt decides whether the fallback dialog is even needed,
// and the Suspense boundary around it no longer renders an empty fallback. ──
{
  const src = read('studio/src/screens/Studio.tsx');
  const fn = src.slice(src.indexOf('const pickWorkspace = useCallback'), src.indexOf('const refreshSessions'));
  const preload = fn.indexOf("import('./studio/WorkspaceDialog')");
  const nativeCall = fn.indexOf('await pickNative(');
  assert.ok(preload !== -1, 'pickWorkspace must preload the WorkspaceDialog chunk');
  assert.ok(nativeCall !== -1, 'pickWorkspace must still call pickNative');
  assert.ok(preload < nativeCall, 'the chunk preload must start before awaiting pickNative, not after');
  assert.ok(!/fallback=\{null\}>\s*\n\s*<WorkspaceDialog/.test(src), 'WorkspaceDialog\'s Suspense must not use an empty fallback');
}

// ── ask_user revision: Studio.tsx must send the card's revision back when
// answering, and its `run()` options must carry it through to sendTurn(). ──
{
  const src = read('studio/src/screens/Studio.tsx');
  assert.ok(/questionId:\s*turn\.ask\?\.questionId,\s*optionIds,\s*revision:\s*turn\.ask\?\.revision/.test(src),
    'onAnswer must pass turn.ask?.revision through to run()');
  assert.ok(/revision:\s*options\.revision/.test(src), 'run() must forward revision to sendTurn()');
}

// ── SEC-03: the working-folder dialog must not claim the terminal is
// sandboxed — only the file tools are. ──
{
  const src = read('studio/src/screens/studio/WorkspaceDialog.tsx');
  assert.ok(!/terminal are confined/i.test(src), 'must not claim the terminal itself is confined');
  assert.ok(/NOT sandboxed/i.test(src), 'must say the terminal is NOT sandboxed');
}

// ── TASK-06: the autonomy preset selector exists in Composer (verifying
// the mapa's claim it is missing is stale — see the lote's report). ──
{
  const src = read('studio/src/screens/studio/Composer.tsx');
  assert.ok(/AutonomyPresetSelector/.test(src) && /bounded_autonomous/.test(src) && /read_only/.test(src),
    'the autonomy preset selector (supervised/bounded_autonomous/read_only) must exist in Composer');
}

// ── UX-06: an incompatible attachment is checked, and rejected, BEFORE it
// is ever queued for upload. ──
{
  const src = read('studio/src/screens/studio/Composer.tsx');
  const fn = src.slice(src.indexOf('const addFiles ='), src.indexOf('const onPaste ='));
  assert.ok(/incompatibilityReason/.test(fn), 'addFiles must check incompatibilityReason before queueing');
  assert.ok(/uploads\.add\(accepted\)/.test(fn), 'only accepted files reach uploads.add');
}

console.log('ok l65-source-wiring');
