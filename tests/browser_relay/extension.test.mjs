import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {BrowserController} from '../../browser-extension/controller.js';
import {BrowserRelay, RELAY_URL} from '../../browser-extension/relay.js';

const ID = 'cneonpjikbdhbaddfpmmdjemfdphlppa';
const SESSION = '00000000-0000-4000-8000-000000000001';
const SESSION2 = '00000000-0000-4000-8000-000000000002';
const CALL = '10000000-0000-4000-8000-000000000001';
const CALL2 = '10000000-0000-4000-8000-000000000002';
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const event = () => { const listeners = []; return {addListener(fn) { listeners.push(fn); }, emit(...args) { for (const fn of listeners) fn(...args); }}; };

/** Simulated Chrome target, not a browser/DOM implementation or Brave acceptance. */
function fixture() {
  const pages = new Map([1, 2].map(id => [id, {id, url: 'https://writer.test/' + id, title: 'Target ' + id, active: id === 1, windowId: 7, serial: 1, value: 'Saved', selection: [5, 5], focused: null, clicks: 0, mouseTargets: []}]));
  const attached = new Set(), external = new Set(), calls = [], gates = new Map(), errors = new Map();
  const onDetach = event(), onEvent = event(), onUpdated = event(), onRemoved = event();
  let axFailure = false, foreignFrame = false, hitMode = 'target';
  const rootId = page => page.id * 10000 + page.serial * 100;
  const backend = (page, n) => rootId(page) + n;
  const hitBackend = page => backend(page, {target: 2, span: 3, shadow: 5, pseudo: 7, distributed: 9, overlay: 20, foreign: 2}[hitMode]);
  const chrome = {
    tabs: {onUpdated, onRemoved, async get(id) { calls.push({api: 'tabs.get', tabId: id}); const page = pages.get(id); if (!page) throw Error('No tab'); return {...page}; }, async query(query) { calls.push({api: 'tabs.query', query}); return [...pages.values()].map(page => ({...page})); }},
    debugger: {
      onDetach, onEvent,
      async attach({tabId}, version) { calls.push({api: 'attach', tabId, version}); if (external.has(tabId) || attached.has(tabId)) throw Error('Another debugger is already attached'); if (gates.has('attach')) await gates.get('attach').promise; attached.add(tabId); },
      async detach({tabId}) { calls.push({api: 'detach', tabId}); if (!attached.has(tabId)) throw Error('Not attached by this extension'); attached.delete(tabId); onDetach.emit({tabId}, 'canceled_by_user'); },
      async sendCommand({tabId}, method, params = {}) {
        calls.push({api: 'cdp', tabId, method, params: structuredClone(params)});
        if (!attached.has(tabId)) throw Error('Debugger is not attached');
        if (gates.has(method)) await gates.get(method).promise;
        if (errors.has(method)) throw Error(errors.get(method));
        const page = pages.get(tabId); if (!page) throw Error('No tab');
        if (['Page.enable', 'DOM.enable', 'Accessibility.enable'].includes(method)) return {};
        if (method === 'Page.getFrameTree') { const url = new URL(page.url); return {frameTree: {frame: {id: 'frame-' + tabId, loaderId: 'loader-' + page.serial, url: page.url.slice(0, page.url.length - url.hash.length), ...(url.hash ? {urlFragment: url.hash} : {})}}}; }
        if (method === 'DOM.getDocument') return {root: {backendNodeId: rootId(page)}};
        if (method === 'Accessibility.getFullAXTree') {
          if (axFailure) throw Error('Simulated AX readback failure');
          const nodes = [{nodeId: 'root', frameId: 'frame-' + tabId, backendDOMNodeId: rootId(page), role: {value: 'RootWebArea'}, name: {value: page.title}, childIds: ['text', 'button', 'virtual', 'foreign']}, {nodeId: 'text', parentId: 'root', backendDOMNodeId: backend(page, 1), role: {value: 'textbox'}, name: {value: 'Draft'}, value: {value: page.value}, properties: [{name: 'focusable', value: {value: true}}, {name: 'editable', value: {value: 'plaintext'}}]}, {nodeId: 'button', parentId: 'root', backendDOMNodeId: backend(page, 2), role: {value: 'button'}, name: {value: 'Open library'}, value: {value: page.clicks}}, {nodeId: 'virtual', parentId: 'root', role: {value: 'StaticText'}, name: {value: 'No DOM backing'}}];
          if (foreignFrame) nodes.push({nodeId: 'foreign', parentId: 'root', frameId: 'other-frame', backendDOMNodeId: 99901, role: {value: 'RootWebArea'}, name: {value: 'Other frame'}, childIds: ['foreign-child']}, {nodeId: 'foreign-child', parentId: 'foreign', backendDOMNodeId: 99902, role: {value: 'button'}, name: {value: 'Other frame action'}});
          return {nodes};
        }
        if (['DOM.describeNode', 'DOM.focus', 'DOM.scrollIntoViewIfNeeded', 'DOM.getContentQuads'].includes(method)) {
          if (![rootId(page), backend(page, 1), backend(page, 2)].includes(params.backendNodeId)) throw Error('Node no longer belongs to this document');
          if (method === 'DOM.describeNode') return {node: params.depth === -1 && params.backendNodeId === backend(page, 2) ? {backendNodeId: backend(page, 2), children: [{backendNodeId: backend(page, 3), nodeName: 'SPAN'}], shadowRoots: [{backendNodeId: backend(page, 4), children: [{backendNodeId: backend(page, 5), nodeName: 'SPAN'}]}], pseudoElements: [{backendNodeId: backend(page, 6), children: [{backendNodeId: backend(page, 7)}]}], distributedNodes: [{backendNodeId: backend(page, 9)}], assignedSlot: {backendNodeId: backend(page, 20)}} : {backendNodeId: params.backendNodeId}};
          if (method === 'DOM.focus') page.focused = params.backendNodeId;
          if (method === 'DOM.getContentQuads') return {quads: [[-50, 10, 100, 10, 100, 50, -50, 50]]};
          return {};
        }
        if (method === 'Page.getLayoutMetrics') return {cssVisualViewport: {clientWidth: 800, clientHeight: 600}};
        if (method === 'DOM.getNodeForLocation') return hitMode === 'missing' ? {} : {backendNodeId: hitBackend(page), frameId: hitMode === 'foreign' ? 'foreign-frame' : 'frame-' + tabId};
        if (method === 'Input.dispatchMouseEvent') { const target = hitMode === 'missing' ? null : hitBackend(page); page.mouseTargets.push({type: params.type, backendNodeId: target, hitMode}); if (params.type === 'mouseReleased' && !['missing', 'overlay', 'foreign'].includes(hitMode)) page.clicks++; return {}; }
        if (method === 'Input.insertText') {
          if (page.focused === backend(page, 1)) { const [from, to] = page.selection; page.value = page.value.slice(0, from) + params.text + page.value.slice(to); page.selection = [from + params.text.length, from + params.text.length]; }
          return {};
        }
        if (method === 'Input.dispatchKeyEvent') { if (params.type === 'keyDown' && params.code === 'KeyA' && params.modifiers === 2) page.selection = [0, page.value.length]; return {}; }
        if (method === 'Page.navigate') { page.url = params.url; page.serial++; onEvent.emit({tabId}, 'Page.frameNavigated', {frame: {id: 'frame-' + tabId, url: page.url}}); return {frameId: 'frame-' + tabId, loaderId: 'loader-' + page.serial}; }
        if (method === 'Page.captureScreenshot') return {data: Buffer.from('SIMULATED-PNG-TARGET-' + tabId).toString('base64')};
        throw Error('Unexpected CDP method ' + method);
      },
    },
  };
  const controller = new BrowserController(chrome);
  onDetach.addListener(source => controller.onDetach(source)); onEvent.addListener((source, method, params) => controller.onEvent(source, method, params)); onUpdated.addListener((tabId, change) => controller.onTabUpdated(tabId, change)); onRemoved.addListener(tabId => controller.onTabRemoved(tabId));
  const snapshot = async tabId => { const reply = await controller.run({action: 'snapshot', tab_id: tabId}); assert.equal(reply.ok, true, JSON.stringify(reply)); return reply.result; };
  const effects = () => calls.filter(call => call.method?.startsWith('Input.') || call.method === 'Page.navigate');
  return {controller, chrome, pages, attached, external, calls, gates, errors, onDetach, onEvent, onUpdated, snapshot, effects, axFailure(value) { axFailure = value; }, foreignFrame(value) { foreignFrame = value; }, hitMode(value) { hitMode = value; }, backend};
}
const ref = (snapshot, role) => snapshot.nodes.find(node => node.role === role).ref;
function start(f) { f.controller.openSession(SESSION); return f; }

test('manifest has the agreed stable identity, MV3 module, endpoint and only required permissions', async () => {
  const manifest = JSON.parse(await readFile(new URL('../../browser-extension/manifest.json', import.meta.url), 'utf8'));
  const digest = createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest('hex').slice(0, 32);
  const extensionId = [...digest].map(n => String.fromCharCode(97 + parseInt(n, 16))).join('');
  assert.equal(extensionId, ID); assert.equal(manifest.manifest_version, 3); assert.equal(manifest.background.type, 'module'); assert.equal(manifest.minimum_chrome_version, '116'); assert.deepEqual(manifest.permissions, ['debugger', 'tabs', 'alarms']); assert.equal(manifest.host_permissions, undefined); assert.equal(manifest.content_scripts, undefined); assert.equal(RELAY_URL, 'ws://127.0.0.1:7016');
});

test('two target snapshots preserve semantics, exclude child frames, and refs cannot cross tabs', async () => {
  const f = start(fixture()); f.foreignFrame(true);
  const one = await f.snapshot(1), two = await f.snapshot(2);
  assert.equal(one.scope, 'main_frame'); assert.equal(one.nodes.find(node => node.role === 'textbox').properties.focusable, true); assert.equal(one.nodes.find(node => node.role === 'textbox').value, 'Saved'); assert.equal(one.nodes.find(node => node.name === 'No DOM backing').ref, null); assert(!one.nodes.some(node => node.name === 'Other frame action')); assert.equal(one.document.frame_id, 'frame-1');
  assert.notEqual(ref(one, 'button'), ref(two, 'button'));
  const bad = await f.controller.run({action: 'click', tab_id: 2, ref: ref(one, 'button')}); assert.equal(bad.ok, false); assert.equal(bad.error.code, 'stale_ref'); assert.equal(bad.phase, 'not_dispatched'); assert.equal(f.effects().length, 0);
  await f.controller.disconnect(); assert.equal(f.attached.size, 0);
});

test('a new snapshot invalidates previous refs; input never activates a tab or window', async () => {
  const f = start(fixture()), old = await f.snapshot(2), current = await f.snapshot(2);
  const rejected = await f.controller.run({action: 'click', tab_id: 2, ref: ref(old, 'button')}); assert.equal(rejected.error.code, 'stale_ref'); assert.equal(f.effects().length, 0);
  const clicked = await f.controller.run({action: 'click', tab_id: 2, ref: ref(current, 'button')}); assert.equal(clicked.ok, true); assert.equal(clicked.phase, 'readback_complete'); assert.equal(f.pages.get(2).clicks, 1); assert.equal(clicked.result.snapshot.nodes.find(node => node.role === 'button').value, '1');
  const inputs = f.effects(); assert.equal(inputs.length, 2); assert(inputs.every(call => call.tabId === 2)); assert(inputs.every(call => call.params.x >= 0 && call.params.x <= 100)); assert.equal(f.pages.get(1).active, true); assert.equal(f.pages.get(2).active, false); assert(!f.calls.some(call => /bringToFront|tabs.update|windows.update|Runtime\./.test(call.api + (call.method ?? ''))));
  await f.controller.disconnect();
});

test('silent same-URL document replacement and navigation events both invalidate refs before effects', async () => {
  const f = start(fixture()), first = await f.snapshot(1); f.pages.get(1).serial++;
  const silent = await f.controller.run({action: 'type', tab_id: 1, ref: ref(first, 'textbox'), text: 'Wrong'}); assert.equal(silent.error.code, 'stale_ref'); assert.equal(f.effects().length, 0);
  const second = await f.snapshot(1); f.onUpdated.emit(1, {status: 'loading'});
  const navigated = await f.controller.run({action: 'click', tab_id: 1, ref: ref(second, 'button')}); assert.equal(navigated.error.code, 'stale_ref'); assert.equal(f.effects().length, 0); await f.controller.disconnect();
});

test('type inserts exact Unicode, preserves existing text, and explicit Ctrl+A then type replaces', async () => {
  const f = start(fixture()), first = await f.snapshot(2), text = ' Élodie 李明 🐚\n';
  const inserted = await f.controller.run({action: 'type', tab_id: 2, ref: ref(first, 'textbox'), text}); assert.equal(inserted.ok, true); assert.equal(f.pages.get(2).value, 'Saved' + text); assert.equal(inserted.result.snapshot.nodes.find(node => node.role === 'textbox').value, 'Saved' + text);
  assert.deepEqual(f.effects().filter(call => call.method === 'Input.insertText').map(call => call.params), [{text}]);
  const selected = await f.controller.run({action: 'press', tab_id: 2, ref: ref(inserted.result.snapshot, 'textbox'), key: 'Ctrl+A'}); assert.equal(selected.ok, true);
  const replaced = await f.controller.run({action: 'type', tab_id: 2, ref: ref(selected.result.snapshot, 'textbox'), text: 'Replacement Ω'}); assert.equal(replaced.ok, true); assert.equal(f.pages.get(2).value, 'Replacement Ω'); assert.equal(f.pages.get(1).value, 'Saved'); await f.controller.disconnect();
});

test('press uses the explicit tab focus, native key pairs, and refuses unsupported keys before input', async () => {
  const f = start(fixture());
  for (const key of ['Enter', 'Escape', 'Tab', 'Backspace', 'ArrowLeft', 'Ctrl+A', 'Shift+ArrowRight']) { const reply = await f.controller.run({action: 'press', tab_id: 2, key}); assert.equal(reply.ok, true, JSON.stringify(reply)); }
  const events = f.effects().filter(call => call.method === 'Input.dispatchKeyEvent'); assert.equal(events.length, 14); assert(events.every(call => call.tabId === 2)); assert.equal(events[0].params.text, '\r'); assert.equal(events[10].params.modifiers, 2); assert.equal(events[12].params.modifiers, 8);
  const before = events.length, refused = await f.controller.run({action: 'press', tab_id: 2, key: 'Magic+Enter'}); assert.equal(refused.error.code, 'unsupported_key'); assert.equal(f.effects().filter(call => call.method === 'Input.dispatchKeyEvent').length, before); await f.controller.disconnect();
});

test('readback failure retains action ACK and does not retry the click', async () => {
  const f = start(fixture()), first = await f.snapshot(2); f.axFailure(true);
  const reply = await f.controller.run({action: 'click', tab_id: 2, ref: ref(first, 'button')}); assert.equal(reply.ok, false); assert.equal(reply.error.code, 'readback_failed'); assert.equal(reply.phase, 'action_confirmed'); assert.match(reply.error.message, /acknowledged click/); assert.equal(f.pages.get(2).clicks, 1); assert.equal(f.effects().length, 2);
  const oldRef = await f.controller.run({action: 'click', tab_id: 2, ref: ref(first, 'button')}); assert.equal(oldRef.error.code, 'stale_ref'); assert.equal(f.effects().length, 2); await f.controller.disconnect();
});

test('an unacknowledged mutation is outcome_unknown, with one dispatch and no replay', async () => {
  const f = start(fixture()), first = await f.snapshot(2); f.errors.set('Input.insertText', 'Connection vanished after input dispatch');
  const reply = await f.controller.run({action: 'type', tab_id: 2, ref: ref(first, 'textbox'), text: 'Once'}); assert.equal(reply.ok, false); assert.equal(reply.phase, 'outcome_unknown'); assert.equal(f.effects().length, 1); await f.controller.disconnect();
});

test('targeted screenshot and tabs metadata preserve active tab; release never closes or steals', async () => {
  const f = start(fixture()); f.external.add(1);
  const listed = await f.controller.run({action: 'tabs'}); assert.equal(listed.ok, true); assert.equal(listed.result.tabs.length, 2); assert.deepEqual(f.calls.find(call => call.api === 'tabs.query').query, {});
  const unavailable = await f.controller.run({action: 'snapshot', tab_id: 1}); assert.equal(unavailable.error.code, 'debugger_unavailable'); assert(!f.calls.some(call => call.api === 'detach' && call.tabId === 1));
  const image = await f.controller.run({action: 'screenshot', tab_id: 2}); assert.equal(image.ok, true); assert.equal(image.result.tab.id, 2); assert.equal(image.result.image.mimeType, 'image/png'); assert.equal(Buffer.from(image.result.image.data, 'base64').toString(), 'SIMULATED-PNG-TARGET-2'); assert.equal(f.calls.find(call => call.method === 'Page.captureScreenshot').tabId, 2);
  const released = await f.controller.run({action: 'release', tab_id: 2}); assert.equal(released.ok, true, JSON.stringify(released)); assert.equal(released.result.released, true); assert.equal(released.phase, 'action_confirmed'); assert.equal(f.attached.size, 0);
  const other = await f.controller.run({action: 'release', tab_id: 1}); assert.equal(other.ok, true); assert.equal(other.result.released, false); assert.equal(f.external.has(1), true); assert.equal(f.pages.size, 2); assert.equal(f.pages.get(1).active, true); await f.controller.disconnect();
});

test('navigate is http(s), targeted, and new-document readback replaces old refs, including hash URLs', async () => {
  const f = start(fixture()), first = await f.snapshot(2);
  const bad = await f.controller.run({action: 'navigate', tab_id: 2, url: 'javascript:alert(1)'}); assert.equal(bad.error.code, 'invalid_arguments'); assert.equal(f.effects().length, 0);
  const next = await f.controller.run({action: 'navigate', tab_id: 2, url: 'https://writer.test/next#library'}); assert.equal(next.ok, true, JSON.stringify(next)); assert.equal(next.result.snapshot.document.url, 'https://writer.test/next#library'); assert.equal(next.result.snapshot.tab.id, 2);
  const old = await f.controller.run({action: 'click', tab_id: 2, ref: ref(first, 'button')}); assert.equal(old.error.code, 'stale_ref'); assert.equal(f.effects().length, 1); await f.controller.disconnect();
});

test('detach cancels an in-flight command and queued effects; an explicit new snapshot may reattach', async () => {
  const f = start(fixture()), first = await f.snapshot(2), gate = deferred(); f.gates.set('DOM.getContentQuads', gate);
  const clicking = f.controller.run({action: 'click', tab_id: 2, ref: ref(first, 'button')}), queued = f.controller.run({action: 'type', tab_id: 2, ref: ref(first, 'textbox'), text: 'Must not run'});
  await tick(); f.attached.delete(2); f.onDetach.emit({tabId: 2}, 'canceled_by_user');
  let timer; try {
    const replies = await Promise.race([Promise.all([clicking, queued]), new Promise((_, reject) => { timer = setTimeout(() => reject(Error('Detach did not cancel pending CDP and queued input without waiting for its ACK')), 1000); })]);
    assert.equal(replies[0].error.code, 'debugger_detached'); assert.equal(replies[1].error.code, 'debugger_detached'); assert.equal(f.effects().length, 0);
  } finally { clearTimeout(timer); gate.resolve(); }
  f.gates.delete('DOM.getContentQuads'); const fresh = await f.snapshot(2); assert.notEqual(ref(fresh, 'button'), ref(first, 'button')); await f.controller.disconnect();
});

test('same-tab actions serialize; different tabs do not share an input/readback queue', async () => {
  const f = start(fixture()), first = await f.snapshot(1), second = await f.snapshot(2), gate = deferred(); f.gates.set('DOM.getContentQuads', gate);
  const click = f.controller.run({action: 'click', tab_id: 1, ref: ref(first, 'button')}), queued = f.controller.run({action: 'type', tab_id: 1, ref: ref(first, 'textbox'), text: 'Stale after click readback'});
  await tick(); const other = await f.controller.run({action: 'type', tab_id: 2, ref: ref(second, 'textbox'), text: '!'}); assert.equal(other.ok, true); assert.equal(f.pages.get(1).value, 'Saved'); gate.resolve(); assert.equal((await click).ok, true); assert.equal((await queued).error.code, 'stale_ref'); assert.equal(f.pages.get(1).value, 'Saved'); await f.controller.disconnect();
});

test('disconnect during attach releases the late owned attachment before a new session', async () => {
  const f = start(fixture()), gate = deferred(); f.gates.set('attach', gate);
  const opening = f.controller.run({action: 'snapshot', tab_id: 2}); await tick(); const closing = f.controller.disconnect(); gate.resolve(); await closing;
  assert.equal((await opening).ok, false); assert.equal(f.attached.size, 0); assert.equal(f.effects().length, 0); assert.equal(f.calls.filter(call => call.api === 'detach').length, 1);
  f.gates.delete('attach'); f.controller.openSession(SESSION2); assert.equal((await f.snapshot(2)).tab.id, 2); await f.controller.disconnect();
});

function sockets() {
  const instances = [];
  class Socket {
    constructor(url) { this.url = url; this.sent = []; this.closed = false; instances.push(this); }
    open() { this.onopen?.(); }
    send(text) { if (this.closed) throw Error('Socket closed'); this.sent.push(JSON.parse(text)); }
    message(value) { this.onmessage?.({data: JSON.stringify(value)}); }
    close() { if (!this.closed) { this.closed = true; this.onclose?.({reason: 'Closed'}); } }
  }
  const timers = {callbacks: new Set(), periods: [], setInterval(fn, period) { this.callbacks.add(fn); this.periods.push(period); return fn; }, clearInterval(fn) { this.callbacks.delete(fn); }};
  return {Socket, instances, timers};
}

test('WS handshake, heartbeat and duplicate IDs dispatch once; reads return native wire DTOs', async () => {
  const f = fixture(), ws = sockets(), states = [], relay = new BrowserRelay({controller: f.controller, extensionId: ID, WebSocket: ws.Socket, timers: ws.timers, onState: value => states.push(value)});
  relay.connect(); const socket = ws.instances[0]; assert.equal(socket.url, RELAY_URL); socket.open(); assert.deepEqual(socket.sent[0], {type: 'hello', protocol: 1, extensionId: ID}); assert.deepEqual(ws.timers.periods, [20000]);
  socket.message({type: 'ready', connection_id: SESSION}); socket.message({type: 'command', id: CALL, arguments: {action: 'snapshot', tab_id: 2}}); await relay.connection.incoming;
  const snapshotReply = socket.sent.find(message => message.type === 'result'); assert.equal(snapshotReply.ok, true); assert.equal(snapshotReply.id, CALL); assert.equal(snapshotReply.result.tab.id, 2);
  socket.message({type: 'command', id: CALL2, arguments: {action: 'click', tab_id: 2, ref: ref(snapshotReply.result, 'button')}}); socket.message({type: 'command', id: CALL2, arguments: {action: 'click', tab_id: 2, ref: ref(snapshotReply.result, 'button')}}); await relay.connection.incoming;
  assert.equal(f.pages.get(2).clicks, 1); assert.equal(socket.sent.at(-1).error.code, 'duplicate_command'); assert.equal(socket.sent.at(-1).phase, 'outcome_unknown');
  for (const callback of ws.timers.callbacks) callback(); assert.equal(socket.sent.at(-1).type, 'ping'); socket.message({type: 'pong'}); await relay.connection.incoming; assert.equal(f.effects().length, 2); assert.equal(states.at(-1).state, 'ready'); socket.close(); await relay.cleanup; assert.equal(f.attached.size, 0); assert.equal(ws.timers.callbacks.size, 0);
});

test('WS disconnect after input dispatch never replays or emits an old result on a new connection', async () => {
  const f = fixture(), ws = sockets(), relay = new BrowserRelay({controller: f.controller, extensionId: ID, WebSocket: ws.Socket, timers: ws.timers});
  relay.connect(); const first = ws.instances[0]; first.open(); first.message({type: 'ready', connection_id: SESSION}); first.message({type: 'command', id: CALL, arguments: {action: 'snapshot', tab_id: 2}}); await relay.connection.incoming;
  const captured = first.sent.at(-1).result, gate = deferred(); f.gates.set('Input.insertText', gate);
  first.message({type: 'command', id: CALL2, arguments: {action: 'type', tab_id: 2, ref: ref(captured, 'textbox'), text: 'Once'}}); await tick(); assert.equal(f.effects().length, 1);
  first.close(); await relay.cleanup; relay.connect(); const second = ws.instances[1]; second.open(); second.message({type: 'ready', connection_id: SESSION2}); await relay.connection.incoming;
  gate.resolve(); await tick(); assert.equal(f.effects().length, 1); assert(!second.sent.some(message => message.type === 'result')); assert.equal(first.sent.filter(message => message.type === 'result').length, 1);
  f.gates.delete('Input.insertText'); second.message({type: 'command', id: CALL, arguments: {action: 'click', tab_id: 2, ref: ref(captured, 'button')}}); await relay.connection.incoming; assert.equal(second.sent.at(-1).error.code, 'stale_ref'); assert.equal(f.effects().length, 1); second.close(); await relay.cleanup;
});

test('malformed handshake refuses commands and never takes ownership of an unrelated debugger', async () => {
  const f = fixture(), ws = sockets(), relay = new BrowserRelay({controller: f.controller, extensionId: ID, WebSocket: ws.Socket, timers: ws.timers});
  f.external.add(2); relay.connect(); const socket = ws.instances[0]; socket.open(); socket.message({type: 'ready', connection_id: 'not-a-uuid'}); socket.message({type: 'command', id: CALL, arguments: {action: 'click', tab_id: 2, ref: 'anything'}}); await tick(); await relay.cleanup;
  assert.equal(socket.closed, true); assert.equal(f.effects().length, 0); assert.equal(f.calls.filter(call => call.api === 'attach' || call.api === 'detach').length, 0); assert.equal(f.external.has(2), true);
});

test('a command received before ready reports not_dispatched without an effect or timeout', async () => {
  const f = fixture(), ws = sockets(), relay = new BrowserRelay({controller: f.controller, extensionId: ID, WebSocket: ws.Socket, timers: ws.timers});
  relay.connect(); const socket = ws.instances[0]; socket.open(); socket.message({type: 'command', id: CALL, arguments: {action: 'press', tab_id: 2, key: 'Enter'}}); await relay.connection.incoming;
  const reply = socket.sent.find(message => message.type === 'result'); assert.ok(reply); assert.equal(reply.phase, 'not_dispatched'); assert.equal(reply.error.code, 'connection_not_ready'); assert.equal(f.effects().length, 0); socket.close(); await relay.cleanup;
});

test('click accepts the target and normal light, shadow, pseudo and distributed descendants', async () => {
  const f = start(fixture());
  try {
    for (const mode of ['target', 'span', 'shadow', 'pseudo', 'distributed']) {
      const snapshot = await f.snapshot(2); f.hitMode(mode); const before = f.effects().length;
      const clicked = await f.controller.run({action: 'click', tab_id: 2, ref: ref(snapshot, 'button')});
      assert.equal(clicked.ok, true, mode + ': ' + JSON.stringify(clicked)); assert.equal(clicked.phase, 'readback_complete'); assert.equal(f.effects().length - before, 2);
      assert(f.effects().slice(before).every(call => Number.isInteger(call.params.x) && Number.isInteger(call.params.y))); assert.equal(f.pages.get(2).mouseTargets.at(-1).hitMode, mode);
    }
    assert.equal(f.pages.get(2).clicks, 5); assert.equal(f.pages.get(1).active, true); assert.equal(f.pages.get(2).active, false);
  } finally { await f.controller.disconnect(); }
});

test('click refuses a same-document overlay without input, retargeting or automatic replay', async () => {
  const f = start(fixture());
  try {
    const healthy = await f.snapshot(2), healthyClick = await f.controller.run({action: 'click', tab_id: 2, ref: ref(healthy, 'button')});
    assert.equal(healthyClick.ok, true); assert.equal(f.pages.get(2).clicks, 1); assert.equal(f.pages.get(2).mouseTargets.length, 2);
    const current = await f.snapshot(2), before = f.effects().length; f.hitMode('overlay');
    const covered = await f.controller.run({action: 'click', tab_id: 2, ref: ref(current, 'button')});
    assert.equal(f.effects().length, before, 'CLICK_REF_DISPATCHED_TO_COVERING_NODE'); assert.equal(covered.ok, false); assert.equal(covered.error.code, 'target_obscured'); assert.equal(covered.phase, 'not_dispatched'); assert.match(covered.error.message, /snapshot|observe/i); assert.equal(f.pages.get(2).clicks, 1);
  } finally { await f.controller.disconnect(); }
});

test('click refuses a missing hit or a hit in a different frame before mouse input', async () => {
  const f = start(fixture());
  try {
    for (const mode of ['missing', 'foreign']) {
      const current = await f.snapshot(2), before = f.effects().length; f.hitMode(mode);
      const reply = await f.controller.run({action: 'click', tab_id: 2, ref: ref(current, 'button')});
      assert.equal(f.effects().length, before, mode + ': wrong-target input dispatched'); assert.equal(reply.ok, false); assert.equal(reply.error.code, 'target_obscured'); assert.equal(reply.phase, 'not_dispatched');
    }
  } finally { await f.controller.disconnect(); }
});
