// Run the adapter, reducer and actual Studio history/rejoin effects. No server,
// storage or chat request is used; deferred reads expose replacement windows.
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const scratch = mkdtempSync(join(tmpdir(), 'studio-terminal-'));
const bundled = join(scratch, 'product.mjs');
await build({ stdin: { contents: `export * from './studio/src/adapters/chat.ts';
  export * from './studio/src/screens/studio/model.ts';`, resolveDir: root },
  bundle: true, platform: 'node', format: 'esm', outfile: bundled, logLevel: 'silent' });
const product = await import(pathToFileURL(bundled).href);
const { loadTurnStatus, blankTurn, restoreTerminalStatus, apply, decode } = product;
const run = 'a'.repeat(32), otherRun = 'b'.repeat(32);
const nativeError = { text: 'Input needs 1052441 tokens; available 1044456. á漢🙂',
  error_class: 'context_length_exceeded', trace_id: run, step_id: 'prepare',
  status: 400, retryable: false };
const status = (sessionId = 'A', extra = {}) => ({ sessionId, runId: run,
  state: 'error', active: false, error: nativeError, ...extra });
const wire = (extra = {}) => ({ session_id: 'A', run_id: run, status: 'error',
  active: false, error: nativeError, ...extra });

const originalFetch = globalThis.fetch;
let fetches = [];
globalThis.fetch = async (path, init) => {
  fetches.push({ path, init }); return Response.json(wire({ session_id: 'A /漢' }));
};
const signal = new AbortController().signal;
assert.deepEqual(await loadTurnStatus('A /漢', signal, run), status('A /漢'));
assert.equal(fetches[0].path, `/api/hoard-copilot/turns/A%20%2F%E6%BC%A2/status?run_id=${run}`);
assert.equal(fetches[0].init.signal, signal);
assert.equal(fetches[0].init.credentials, 'same-origin');
assert.equal(fetches[0].init.method, undefined, 'GET only');
for (const raw of [wire({ session_id: 'B' }), wire({ run_id: otherRun }), wire({ run_id: '' })]) {
  globalThis.fetch = async () => Response.json(raw);
  assert.equal(await loadTurnStatus('A', undefined, run), null, 'identity mismatch is not a terminal error');
}
globalThis.fetch = async () => Response.json({ detail: 'missing' }, { status: 404 });
assert.equal(await loadTurnStatus('A'), null);
globalThis.fetch = async () => Response.json({ detail: 'owner' }, { status: 401 });
await assert.rejects(loadTurnStatus('A'), /owner/);
globalThis.fetch = originalFetch;
console.log('ok: native GET identity, owner failure, 404 and exact payload');

const user = { ...blankTurn('user', 'original input'), dbId: 'user-db', historyIndex: 0, streaming: false };
const input = [user], before = JSON.stringify(input);
const restored = restoreTerminalStatus(input, 'A', status());
const view = restored[1];
const live = apply(apply(blankTurn('assistant'), decode(nativeError, 'error')), { type: 'done' });
for (const key of ['error', 'errorClass', 'traceId', 'stepId', 'streaming', 'text']) assert.deepEqual(view[key], live[key]);
assert.equal(view.error, nativeError.text);
assert.equal(view.dbId, undefined); assert.equal(view.historyIndex, undefined);
assert.equal(view.live, undefined); assert.equal(view.metrics, undefined);
assert.equal(view.text, ''); assert.equal(JSON.stringify(input), before);
assert.deepEqual(restoreTerminalStatus(restored, 'A', status()), restored, 'repeat GET is idempotent');
for (const candidate of [null, status('B'), status('A', { error: undefined }),
  status('A', { error: {} }), status('A', { error: { text: '' } }),
  status('A', { error: { text: 400 } }), status('A', { active: true }),
  ...['running', 'queued', 'waiting', 'done', 'stopped'].map(state => status('A', { state }))]) {
  assert.deepEqual(restoreTerminalStatus(input, 'A', candidate), input, 'no invented error');
}
assert.deepEqual(restoreTerminalStatus(restored, 'A', status('A', { state: 'queued', runId: otherRun })), input);
const assistant = { ...blankTurn('assistant', 'saved answer'), dbId: 'assistant-db', historyIndex: 1, metrics: { runId: run } };
const merged = restoreTerminalStatus([user, assistant], 'A', status());
assert.equal(merged.length, 2); assert.equal(merged[1].text, 'saved answer');
assert.equal(merged[1].dbId, 'assistant-db'); assert.equal(merged[1].historyIndex, 1);
assert.equal(merged[1].error, nativeError.text);
for (const key of ['text', 'error', 'message', 'detail']) {
  assert.equal(restoreTerminalStatus(input, 'A', status('A', { error: { [key]: 'native legacy wording' } }))[1].error,
    'native legacy wording', `preserve SSE ${key} compatibility`);
}
console.log('ok: existing SSE decoder/reducer, exact native error, no saved assistant or invented error');

const { Window } = await import('happy-dom');
const window = new Window();
globalThis.window = window; globalThis.document = window.document;
Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const React = await import('react');
const { createRoot } = await import('react-dom/client');
const source = process.env.QA881_IDS_BASELINE
  ? execFileSync('git', ['show', 'd728e26c735d4c471181bce97f1f724ac459d519:studio/src/screens/Studio.tsx'], { cwd: root, encoding: 'utf8' })
  : process.env.QA881_BASELINE
  ? execFileSync('git', ['show', '88e863e1579d4b4c4b2c7d11c022bed065da2612:studio/src/screens/Studio.tsx'], { cwd: root, encoding: 'utf8' })
  : readFileSync(join(root, 'studio/src/screens/Studio.tsx'), 'utf8');
function section(from, to) {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `source boundary ${from}`);
  return source.slice(start, end);
}
const mapping = section('  const turnsFromHistory =', '  /** F1 (CONTRATO_CABLES2)');
const historyEffect = section('  /* History: whenever', '  /* The gallery sends');
const recovery = section('  const reconcileOutbox =', '  const ensureSession =');
const identitySync = section('  const syncIds = useCallback(', '  const sendToGroup =');
const harnessFile = join(scratch, 'harness.mjs');
await build({ stdin: { contents: `
import React, {useState,useEffect,useRef,useCallback} from '${pathToFileURL(join(root, 'node_modules/react/index.js')).href}';
export function Harness({sessionId,fixture}) {
 const {loadHistory,loadTurnStatus,blankTurn,cleanUserText,restoreFromMetadata,restoreTerminalStatus,
   metricsFrom,apply,appendSteer,readAttachmentsFor,loadAgentProgress,panelDispatch,
   refreshStaleWires,pendingOutboxFor,executionForSend,resolveExecutionAutonomy,
   sendTurn,resumeTurn,refreshSessions,refreshActivity,streamFailureMessage}=fixture;
 const attachmentsFromMetadata=()=>[],agentMessageFromMetadata=()=>null;
 const [turns,setTurns]=useState([]),[title,setTitle]=useState(''),[busy,setBusy]=useState(false);
 const [attachments,setAttachments]=useState([]),[loadError,setLoadError]=useState(null),[notice,setNotice]=useState(null);
 const [routeId,setRouteId]=useState('');
 const controllerRef=useRef(null),runEpoch=useRef(0),runIdRef=useRef(null),pinnedRef=useRef(false),freshRef=useRef(null);
 const rejoinRef=useRef(()=>{}),pendingSessionRoute=useRef(null),visibleSession=useRef(sessionId);visibleSession.current=sessionId;
 const uiEventRef=useRef(()=>{}),groupId=null,routes=[],knobs={autonomyPreset:'full'},executionPicks={};
 const t=x=>x,pickedRouteId=()=>'',routeForSession=()=>null;
 const patchLast=useCallback(fn=>setTurns(list=>list?.map((turn,i)=>i===list.length-1?fn(turn):turn)),[]);
 ${mapping}
 ${identitySync}
 ${recovery}
 ${historyEffect}
 fixture.screen={turns,title,busy,attachments,loadError,notice,rejoin,reconcileOutbox,syncIds,replaceTurns:setTurns,runId:runIdRef.current,
   startLocal:()=>{runEpoch.current++;controllerRef.current=new AbortController();setBusy(true);setTurns([blankTurn('user','new input'),blankTurn('assistant','new answer')]);},
   finishLocal:()=>{controllerRef.current=null;setBusy(false);}};
 return null;
}`, loader: 'tsx', resolveDir: root }, bundle: true, platform: 'node', format: 'esm',
  packages: 'external', outfile: harnessFile, logLevel: 'silent' });
const { Harness } = await import(pathToFileURL(harnessFile).href);
function fixture() {
  const f = { history: [], statuses: [], resumes: [], sends: [], panels: [], pending: null, resumeEvents: [], sendEvents: [] };
  Object.assign(f, product, {
    loadHistory: (sid, signal) => new Promise((resolve, reject) => f.history.push({ sid, signal, resolve, reject })),
    loadTurnStatus: (sid, signal) => new Promise((resolve, reject) => f.statuses.push({ sid, signal, resolve, reject })),
    readAttachmentsFor: () => [], loadAgentProgress: async () => [], refreshStaleWires: () => {},
    panelDispatch: event => f.panels.push(event), pendingOutboxFor: () => f.pending,
    executionForSend: async () => ({ defaults: {} }), resolveExecutionAutonomy: () => ({ override: undefined }),
    sendTurn: async function* (options) { f.sends.push(options); for (const event of f.sendEvents) yield event; },
    resumeTurn: async function* (sid, options) { f.resumes.push({ sid, options }); for (const event of f.resumeEvents) { options.onRunId(run); yield event; } },
    refreshSessions: () => {}, refreshActivity: () => {}, streamFailureMessage: String,
  });
  return f;
}
const history = (sid = 'A') => ({ name: `chat ${sid}`, model: '', history: [{ role: 'user', content: `input ${sid}`, index: 0, metadata: { _db_id: `db-${sid}` } }] });
async function mount(f, sid = 'A') {
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host);
  const render = sid => React.act(async () => root.render(React.createElement(Harness, { sessionId: sid, fixture: f })));
  await render(sid);
  return { render, close: () => React.act(() => root.unmount()) };
}
async function settleHistory(f, index, sid = 'A') { await React.act(async () => f.history[index].resolve(history(sid))); }
async function settleStatus(f, index, value = status()) { await React.act(async () => f.statuses[index].resolve(value)); }
async function loaded(f, index = 0, sid = 'A', value = status(sid)) {
  await settleHistory(f, index, sid);
  // Baseline has no status read: assert its observable screen, not a missing symbol.
  if (f.statuses[index]) await settleStatus(f, index, value);
}
{
  const f = fixture(), app = await mount(f);
  await loaded(f);
  assert.equal(f.screen.turns.at(-1)?.error, nativeError.text, 'refresh must restore the native error after a user-only history');
  assert.equal(f.screen.turns.length, 2); assert.equal(f.screen.turns[0].dbId, 'db-A');
  assert.equal(f.screen.runId, null, 'terminal view cannot become a Stop target');
  assert.equal(f.screen.busy, false); assert.equal(f.sends.length, 0);
  assert.equal(f.resumes.length, 1, 'ordinary rejoin still checks the active stream');
  await app.close();
  const second = fixture(), refresh = await mount(second); await loaded(second);
  assert.equal(second.screen.turns.at(-1).error, nativeError.text, 'fresh page has no local state');
  assert.equal(second.sends.length, 0); await refresh.close();
}
console.log('ok: real history effect restores an error on two fresh mounts, no send or Stop target');
{
  const f = fixture(), app = await mount(f);
  await settleHistory(f, 0); await app.render('B');
  await settleStatus(f, 0);
  assert.equal(f.screen.turns, null, 'late A status must not render in B');
  await loaded(f, 1, 'B', status('B', { runId: otherRun, error: { text: 'B only' } }));
  assert.equal(f.screen.turns.at(-1).error, 'B only'); assert.equal(f.screen.title, 'chat B');
  assert.equal(f.resumes.some(call => call.sid === 'A'), false);
  await app.close();
}
{
  const f = fixture(), app = await mount(f); await settleHistory(f, 0);
  await React.act(async () => { f.screen.startLocal(); });
  await React.act(async () => { f.screen.finishLocal(); });
  await settleStatus(f, 0);
  assert.equal(f.screen.turns.at(-1).text, 'new answer', 'epoch protects a new turn even after its controller cleared');
  assert.equal(f.resumes.length, 0); await app.close();
}
console.log('ok: late hydrate cannot cross sessions or replace a newer completed turn');
{
  const f = fixture(); f.resumeEvents = [decode(nativeError, 'error'), { type: 'done' }];
  const app = await mount(f); await loaded(f, 0, 'A', status('A', { state: 'running', active: true, error: undefined }));
  assert.equal(f.history.length, 2, 'joined stream must reconcile its saved history');
  assert.equal(f.screen.turns.at(-1).error, nativeError.text);
  await loaded(f, 1);
  assert.equal(f.screen.turns.at(-1).error, nativeError.text, 'post-rejoin history also restores the terminal error');
  assert.equal(f.screen.turns.filter(turn => turn.terminalRun).length, 1);
  assert.equal(f.sends.length, 0); await app.close();
}
{
  const f = fixture(); f.resumeEvents = [{ type: 'done' }];
  const app = await mount(f); await loaded(f, 0, 'A', null);
  await settleHistory(f, 1);
  await React.act(async () => f.screen.startLocal()); await React.act(async () => f.screen.finishLocal());
  await settleStatus(f, 1);
  assert.equal(f.screen.turns.at(-1).text, 'new answer', 'late rejoin refresh is also bound to its epoch');
  await app.close();
}
console.log('ok: active rejoin and its terminal refresh retain the error and isolate newer turns');
{
  const f = fixture(), app = await mount(f); await loaded(f, 0, 'A', null);
  assert.equal(f.screen.turns.length, 1, 'unknown run leaves ordinary history');
  f.pending = { id: 'stable-client-id', text: 'uncertain input', attachments: [] };
  f.sendEvents = [{ type: 'done' }];
  await React.act(async () => f.screen.reconcileOutbox('A'));
  assert.equal(f.sends.length, 1); assert.equal(f.sends[0].clientMessageId, 'stable-client-id');
  await loaded(f, 1);
  assert.equal(f.screen.turns.at(-1).error, nativeError.text, 'uncertain-send reconciliation uses the same canonical status');
  assert.equal(f.pending.id, 'stable-client-id'); await app.close();
}
{
  const f = fixture(), app = await mount(f); await loaded(f, 0, 'A', null);
  f.pending = { id: 'stable-client-id', text: 'uncertain input', attachments: [] };
  await React.act(async () => f.screen.reconcileOutbox('A')); await settleHistory(f, 1);
  await React.act(async () => f.screen.startLocal()); await React.act(async () => f.screen.finishLocal());
  await settleStatus(f, 1);
  assert.equal(f.screen.turns.at(-1).text, 'new answer', 'late outbox refresh cannot overwrite a newer completed turn');
  await app.close();
}
console.log('ok: unknown runs invent nothing; outbox preserves its existing id and refresh race guard');
{
  const f = fixture(), app = await mount(f); await loaded(f);
  const [userA, errorA] = f.screen.turns;
  const userB = blankTurn('user', 'new rejected input B');
  const assistantB = { ...blankTurn('assistant'), error: 'HTTP rejection before admission', streaming: false };
  await React.act(async () => f.screen.replaceTurns([userA, errorA, userB, assistantB]));
  let sync;
  await React.act(async () => { sync = f.screen.syncIds('A'); });
  await loaded(f, 1); await React.act(async () => sync);
  const rejected = f.screen.turns.find(turn => turn.id === userB.id);
  assert.equal(rejected.dbId, undefined, 'unpersisted B must not borrow the saved user A id');
  assert.equal(rejected.historyIndex, undefined, 'unpersisted B must not borrow the saved user A index');
  assert.equal(f.screen.turns.find(turn => turn.id === errorA.id).error, nativeError.text);
  assert.equal(f.screen.turns.at(-1).error, assistantB.error);

  // A later, genuinely saved B still receives its own ids across the local
  // runtime view; ordinary edit/delete identity synchronization is preserved.
  await React.act(async () => { sync = f.screen.syncIds('A'); });
  await React.act(async () => f.history[2].resolve({ ...history(), history: [...history().history,
    { role: 'user', content: userB.text, index: 1, metadata: { _db_id: 'saved-user-B' } },
    { role: 'assistant', content: 'saved answer B', index: 2, metadata: { _db_id: 'saved-assistant-B' } },
  ] }));
  await settleStatus(f, 2, status('A', { runId: otherRun, state: 'done', error: undefined }));
  await React.act(async () => sync);
  const syncedB = f.screen.turns.find(turn => turn.id === userB.id);
  assert.equal(syncedB.dbId, 'saved-user-B'); assert.equal(syncedB.historyIndex, 1);
  assert.equal(f.screen.turns.at(-1).dbId, 'saved-assistant-B');
  assert.equal(f.screen.turns.at(-1).historyIndex, 2);
  const runtime = f.screen.turns.find(turn => turn.id === errorA.id);
  assert.equal(runtime.dbId, undefined); assert.equal(runtime.historyIndex, undefined);
  assert.equal(runtime.error, nativeError.text); assert.equal(f.sends.length, 0);
  await app.close();
}
{
  const f = fixture(), app = await mount(f); await loaded(f);
  const runtimeId = f.screen.turns.at(-1).id;
  let sync;
  await React.act(async () => { sync = f.screen.syncIds('A'); });
  await React.act(async () => f.history[1].resolve({ ...history(), history: [...history().history,
    { role: 'assistant', content: 'real saved assistant A', index: 1, metadata: { _db_id: 'saved-assistant-A' } },
  ] }));
  await settleStatus(f, 1, status('A', { state: 'done', error: undefined }));
  await React.act(async () => sync);
  const runtime = f.screen.turns.find(turn => turn.id === runtimeId);
  assert.equal(runtime.dbId, undefined, 'a local runtime view must never receive a saved assistant identity');
  assert.equal(runtime.historyIndex, undefined); assert.equal(runtime.error, nativeError.text);
  await app.close();
}
console.log('ok: real syncIds excludes runtime views on both sides and preserves saved B identities');
console.log('ALL OK');
