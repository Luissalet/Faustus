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
    tabs: {onUpdated, onRemoved, async get(id) { calls.push({api: 'tabs.get', tabId: id}); const page = pages.get(id); if (!page) throw Error('No tab'); return {...page}; }, async query(query) { calls.push({api: 'tabs.query', query}); return [...pages.values()].map(page => ({...page})); },
      async create({url, active}) { calls.push({api: 'tabs.create', url, active}); const id = 100 + pages.size; pages.set(id, {id, url, title: 'New ' + id, active: !!active, windowId: 7, serial: 1, value: '', selection: [0, 0], focused: null, clicks: 0, mouseTargets: []}); return {...pages.get(id)}; },
      async remove(id) { calls.push({api: 'tabs.remove', tabId: id}); pages.delete(id); }},
    runtime: {reload() { calls.push({api: 'runtime.reload'}); }, getManifest() { return {version: '1.1.0'}; }},
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
        if (method === 'Page.getLayoutMetrics') return {cssVisualViewport: {clientWidth: 800, clientHeight: 600}, cssContentSize: {width: 800, height: 2000}};
        if (method === 'DOMSnapshot.captureSnapshot') return {documents: [{nodes: {backendNodeId: [rootId(page), backend(page, 1), backend(page, 2)]}, layout: {nodeIndex: [0, 1, 2], bounds: [[0, 0, 800, 2000], [10, 100, 200, 30], [10, 1500, 100, 40]]}}]};
        if (method === 'DOM.getNodeForLocation') return hitMode === 'missing' ? {} : {backendNodeId: hitBackend(page), frameId: hitMode === 'foreign' ? 'foreign-frame' : 'frame-' + tabId};
        if (method === 'Input.dispatchMouseEvent') { const target = hitMode === 'missing' ? null : hitBackend(page); page.mouseTargets.push({type: params.type, backendNodeId: target, hitMode}); if (params.type === 'mouseReleased' && !['missing', 'overlay', 'foreign'].includes(hitMode)) page.clicks++; return {}; }
        if (method === 'Input.insertText') {
          if (page.focused === backend(page, 1)) { const [from, to] = page.selection; page.value = page.value.slice(0, from) + params.text + page.value.slice(to); page.selection = [from + params.text.length, from + params.text.length]; }
          return {};
        }
        if (method === 'Input.dispatchKeyEvent') { if (params.type === 'keyDown' && params.code === 'KeyA' && params.modifiers === 2) page.selection = [0, page.value.length]; return {}; }
        if (method === 'Page.navigate') { page.url = params.url; page.serial++; onEvent.emit({tabId}, 'Page.frameNavigated', {frame: {id: 'frame-' + tabId, url: page.url}}); return {frameId: 'frame-' + tabId, loaderId: 'loader-' + page.serial}; }
        if (method === 'Page.captureScreenshot') return {data: Buffer.from('SIMULATED-PNG-TARGET-' + tabId + (params.clip ? '-CLIP' : '')).toString('base64')};
        if (method === 'Page.reload') { page.serial++; onEvent.emit({tabId}, 'Page.frameNavigated', {frame: {id: 'frame-' + tabId, url: page.url}}); return {}; }
        if (method === 'Page.getNavigationHistory') { const h = page.history ?? {index: 0, entries: [page.url]}; return {currentIndex: h.index, entries: h.entries.map((url, i) => ({id: i + 1, url}))}; }
        if (method === 'Page.navigateToHistoryEntry') { const h = page.history; h.index = params.entryId - 1; page.url = h.entries[h.index]; page.serial++; onEvent.emit({tabId}, 'Page.frameNavigated', {frame: {id: 'frame-' + tabId, url: page.url}}); return {}; }
        if (method === 'DOM.resolveNode' && params.objectGroup?.startsWith('faustus-select:')) return {object: {objectId: 'obj-' + params.backendNodeId}};
        if (method === 'Runtime.callFunctionOn' && params.functionDeclaration.includes("'select'")) { page.selected = params.arguments[0].value; return {result: {value: {value: page.selected, text: page.selected}}}; }
        if (method === 'Runtime.releaseObjectGroup' && params.objectGroup?.startsWith('faustus-select:')) return {};
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
  const timers = {callbacks: new Set(), periods: [], setInterval(fn, period) { this.callbacks.add(fn); this.periods.push(period); return fn; }, clearInterval(fn) { this.callbacks.delete(fn); },
    timeouts: [], setTimeout(fn, ms) { this.timeouts.push({fn, ms, cleared: false}); return this.timeouts.length; }, clearTimeout(id) { if (this.timeouts[id - 1]) this.timeouts[id - 1].cleared = true; }};
  return {Socket, instances, timers};
}

test('view.visible marks which nodes are on screen and reports where the viewport is', async () => {
  const f = start(fixture());
  const reply = await f.controller.run({action: 'snapshot', tab_id: 1, view: {visible: true}}); assert.equal(reply.ok, true, JSON.stringify(reply));
  const by = role => reply.result.nodes.find(node => node.role === role);
  assert.equal(by('textbox').in_view, true); assert.equal(by('button').in_view, false); assert.equal(by('RootWebArea').in_view, true);
  assert.equal(reply.result.nodes.find(node => node.name === 'No DOM backing').in_view, false);
  assert.deepEqual(reply.result.viewport, {x: 0, y: 0, width: 800, height: 600, page_width: 800, page_height: 2000});
  const plain = await f.snapshot(1); assert.equal('in_view' in plain.nodes[0], false); assert.equal(plain.viewport, undefined);
  assert.equal(f.calls.filter(call => call.method === 'DOMSnapshot.captureSnapshot').length, 1);
  await f.controller.disconnect();
});

test('a lost relay is retried with short backoff, reset after ready, and a manual connect cancels the pending retry', async () => {
  const f = fixture(), ws = sockets(), relay = new BrowserRelay({controller: f.controller, extensionId: ID, WebSocket: ws.Socket, timers: ws.timers});
  relay.connect(); const first = ws.instances[0]; first.open(); first.message({type: 'ready', connection_id: SESSION}); await relay.connection.incoming;
  first.close(); await relay.cleanup; assert.deepEqual(ws.timers.timeouts.map(t => t.ms), [1000]);
  ws.timers.timeouts[0].fn(); assert.equal(ws.instances.length, 2); ws.instances[1].close(); await relay.cleanup;
  assert.deepEqual(ws.timers.timeouts.map(t => t.ms), [1000, 2000]);
  ws.timers.timeouts[1].fn(); const third = ws.instances[2]; third.open(); third.message({type: 'ready', connection_id: SESSION2}); await relay.connection.incoming;
  third.close(); await relay.cleanup; assert.equal(ws.timers.timeouts.at(-1).ms, 1000);
  relay.connect(); assert.equal(ws.timers.timeouts.at(-1).cleared, true); assert.equal(ws.instances.length, 4); ws.instances[3].close(); await relay.cleanup;
});

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


/** Explicit simulated CDP for file transport. Fixed production readers execute on real Node Blobs. */
function fileFixture(data = Buffer.from('Élodie你🙂'), mode = 'download') {
  const f = fixture(), original = f.chrome.debugger.sendCommand.bind(f.chrome.debugger), intercept = new Map(), objects = new Map();
  const blobURL = URL.createObjectURL(new Blob([data], {type: 'application/octet-stream'}));
  const state = {mode, selected: [], count: 0, chooser: 'selectSingle', chooserFrame: 'frame-2', chooserBackend: true, disabled: false, emitChooser: true, href: blobURL, baseURI: null, releases: 0, mutateAfterMeta: false, detachAfterMeta: false, foreignDocumentAfterMeta: false, baseAfterMeta: null};
  const document = {get URL() { return f.pages.get(2).url; }, get baseURI() { return state.baseURI ?? this.URL; }};
  const anchor = {localName: 'a', isConnected: true, ownerDocument: document,
    get href() { return new URL(state.href, this.baseURI).href; },
    get baseURI() { return this.ownerDocument.baseURI; },
    getAttribute(name) { return name === 'href' ? state.href : name === 'download' ? 'cambios-你.bin' : null; }};
  f.chrome.debugger.sendCommand = async ({tabId}, method, params = {}) => {
    const page = f.pages.get(tabId), input = f.backend(page, 10);
    if (['Page.setInterceptFileChooserDialog', 'DOM.setFileInputFiles', 'DOM.resolveNode', 'Runtime.callFunctionOn', 'Runtime.releaseObjectGroup'].includes(method)
        || method === 'DOM.describeNode' && params.backendNodeId === input) {
      f.calls.push({api:'cdp', tabId, method, params:structuredClone(params)});
      if (!f.attached.has(tabId)) throw Error('Not attached');
      if (f.gates.has(method)) await f.gates.get(method).promise;
      if (f.errors.has(method)) throw Error(f.errors.get(method));
      if (method === 'Page.setInterceptFileChooserDialog') { intercept.set(tabId, params.enabled);return {}; }
      if (method === 'DOM.describeNode') return {node:{backendNodeId:input,localName:'input',attributes:['type','file',...(state.disabled?['disabled','']:[])]}};
      if (method === 'DOM.setFileInputFiles') { state.selected = [...params.files];state.count++;return {}; }
      if (method === 'DOM.resolveNode') { objects.set('anchor', anchor);return {object:{objectId:'anchor'}}; }
      if (method === 'Runtime.releaseObjectGroup') { state.releases++;objects.clear();return {}; }
      try {
        const fn = Function('document', 'return (' + params.functionDeclaration + ')')(document);
        const result = await fn.apply(objects.get(params.objectId), (params.arguments ?? []).map(arg => arg.value));
        if (params.returnByValue) {
          if (result && Number.isSafeInteger(result.bytes)) {
            if (state.mutateAfterMeta) state.href = 'blob:https://writer.test/replaced';
            if (state.detachAfterMeta) anchor.isConnected = false;
            if (state.foreignDocumentAfterMeta) anchor.ownerDocument = {URL: document.URL, baseURI: document.baseURI};
            if (state.baseAfterMeta !== null) state.baseURI = state.baseAfterMeta;
          }
          return {result:{value:result}};
        }
        objects.set('blob', result);return {result:{objectId:'blob'}};
      } catch (error) { return {exceptionDetails:{text:String(error.message ?? error)}}; }
    }
    const result = await original({tabId}, method, params);
    if (method === 'DOM.describeNode' && params.backendNodeId === f.backend(page, 2)) {
      Object.assign(result.node, {localName:state.mode === 'download' ? 'a' : 'button',attributes:state.mode === 'download' ? ['href',state.href,'download','cambios-你.bin'] : []});
    }
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased' && intercept.get(tabId) && state.emitChooser) {
      // Event arrives BEFORE the click CDP promise resolves: subscribing later loses it.
      f.onEvent.emit({tabId}, 'Page.fileChooserOpened', {...(state.chooserBackend?{backendNodeId:input}:{}),frameId:state.chooserFrame,mode:state.chooser});
    }
    return result;
  };
  const close = async () => { URL.revokeObjectURL(blobURL);await f.controller.disconnect(); };
  return {...f, file:state, anchor, document, intercept, objects, close};
}

test('upload uses the immediate chooser event, target tab and normal readback without Runtime', async () => {
  const f = start(fileFixture(Buffer.alloc(0), 'upload'));
  try {
    const snap = await f.snapshot(2), files = ['C:/qa/paquete-你.json'];
    const result = await f.controller.run({action:'upload',tab_id:2,ref:ref(snap,'button'),files});
    assert.equal(result.ok,true,JSON.stringify(result));assert.equal(result.phase,'readback_complete');
    assert.deepEqual(f.file.selected,files);assert.equal(f.file.count,1);assert.equal(result.result.upload.selected,1);
    assert.equal(f.intercept.get(2),false);assert.equal(f.pages.get(1).active,true);assert.equal(f.pages.get(2).active,false);
    assert(!f.calls.some(call=>call.method?.startsWith('Runtime.')));
    assert(f.calls.filter(call=>call.method==='DOM.setFileInputFiles').every(call=>call.tabId===2));
  } finally { await f.close(); }
});

test('upload refuses foreign chooser frames and single-file mismatch, and restores interception', async () => {
  const f = start(fileFixture(Buffer.alloc(0),'upload'));
  try {
    f.file.chooserFrame = 'foreign';let snap=await f.snapshot(2);
    let result=await f.controller.run({action:'upload',tab_id:2,ref:ref(snap,'button'),files:['C:/one']});
    assert.equal(result.ok,false);assert.equal(result.error.code,'unsupported_file_chooser');assert.equal(f.file.count,0);assert.equal(f.intercept.get(2),false);
    f.file.chooserFrame='frame-2';snap=await f.snapshot(2);
    result=await f.controller.run({action:'upload',tab_id:2,ref:ref(snap,'button'),files:['C:/one','C:/two']});
    assert.equal(result.error.code,'invalid_file_selection');assert.equal(f.file.count,0);assert.equal(f.intercept.get(2),false);
    f.file.chooser='selectMultiple';snap=await f.snapshot(2);
    result=await f.controller.run({action:'upload',tab_id:2,ref:ref(snap,'button'),files:['C:/one','C:/two']});
    assert.equal(result.ok,true);assert.deepEqual(f.file.selected,['C:/one','C:/two']);
  } finally { await f.close(); }
});

test('lost chooser action is not replayed and an old ref cannot select files in the next session', async () => {
  const f = start(fileFixture(Buffer.alloc(0),'upload'));
  try {
    f.file.emitChooser=false;const snap=await f.snapshot(2), previous=ref(snap,'button');
    const pending=f.controller.run({action:'upload',tab_id:2,ref:previous,files:['C:/one']});
    await tick();await f.controller.disconnect();const result=await pending;
    assert.equal(result.ok,false);assert.equal(f.file.count,0);assert.equal(f.pages.get(2).clicks,1);
    assert(f.calls.some(call=>call.method==='Page.setInterceptFileChooserDialog'&&call.params.enabled===false));
    f.controller.openSession(SESSION2);
    const stale=await f.controller.run({action:'upload',tab_id:2,ref:previous,files:['C:/one']});
    assert.equal(stale.error.code,'stale_ref');assert.equal(f.pages.get(2).clicks,1);assert.equal(f.file.count,0);
  } finally { await f.close(); }
});

for (const data of [Buffer.alloc(0),Buffer.from('á你🙂é\n'),Buffer.concat([Buffer.from(Array.from({length:256},(_,i)=>i)),Buffer.alloc(140000,255),Buffer.from('X')])]) {
  test('download fixed readers preserve complete Blob bytes, including padding/Unicode/zero: '+data.length, async()=>{
    const f=start(fileFixture(data));
    try {
      const snap=await f.snapshot(2),result=await f.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/qa/new.bin'});
      assert.equal(result.ok,true,JSON.stringify(result));assert.equal(result.phase,'readback_complete');
      const transfer=result.result.download;
      assert.deepEqual(Buffer.concat(transfer.parts.map(part=>Buffer.from(part,'base64'))),data);
      assert.equal(transfer.bytes,data.length);assert.equal(transfer.name,'cambios-你.bin');assert.equal(f.file.releases,1);assert.equal(f.objects.size,0);
      assert.equal(f.pages.get(2).clicks,0);assert.equal(f.pages.get(1).active,true);
      assert(f.calls.filter(call=>call.method?.startsWith('Runtime.')).every(call=>call.tabId===2));
    } finally { await f.close(); }
  });
}

test('revoked Blob and changed link return no transfer, release handles and never replay',async()=>{
  const f=start(fileFixture(Buffer.from('original')));
  try {
    let snap=await f.snapshot(2);URL.revokeObjectURL(f.file.href);
    let result=await f.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/new'});
    assert.equal(result.error.code,'download_read_failed');assert.equal(result.phase,'outcome_unknown');assert.equal(result.result,undefined);assert.equal(f.objects.size,0);
  } finally { await f.close(); }
  const changed=start(fileFixture(Buffer.from('original')));
  try {
    changed.file.mutateAfterMeta=true;const snap=await changed.snapshot(2);
    const result=await changed.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/new'});
    assert.equal(result.error.code,'download_link_changed');assert.equal(result.result,undefined);assert.equal(changed.objects.size,0);assert.equal(changed.file.releases,1);
  } finally { await changed.close(); }
});

test('document replacement during a Blob read invalidates the observed link before publishing',async()=>{
  const f=start(fileFixture(Buffer.alloc(70000,1))),gate=deferred();
  try {
    const snap=await f.snapshot(2);f.gates.set('Runtime.callFunctionOn',gate);
    const pending=f.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/new'});
    await tick();f.pages.get(2).serial++;f.onUpdated.emit(2,{status:'loading'});gate.resolve();
    const result=await pending;assert.equal(result.ok,false);assert.equal(result.error.code,'stale_ref');assert.equal(result.result,undefined);assert.equal(f.objects.size,0);
  } finally {gate.resolve();await f.close();}
});


test('missing input backend, disabled input and document replacement select no file',async()=>{
  const f=start(fileFixture(Buffer.alloc(0),'upload'));
  try {
    for(const condition of ['backend','disabled']){
      f.file.chooserBackend=condition!=='backend';f.file.disabled=condition==='disabled';
      const snap=await f.snapshot(2),result=await f.controller.run({action:'upload',tab_id:2,ref:ref(snap,'button'),files:['C:/one']});
      assert.equal(result.error.code,'unsupported_file_chooser');assert.equal(f.file.count,0);assert.equal(f.intercept.get(2),false);
    }
    f.file.disabled=false;f.file.chooserBackend=true;f.file.emitChooser=false;
    const snap=await f.snapshot(2),pending=f.controller.run({action:'upload',tab_id:2,ref:ref(snap,'button'),files:['C:/one']});
    await tick();f.onUpdated.emit(2,{status:'loading'});
    const result=await pending;assert.equal(result.error.code,'stale_ref');assert.equal(f.file.count,0);assert.equal(f.intercept.get(2),false);
  } finally {await f.close();}
});

test('file-input dispatch error restores the chooser without retrying',async()=>{
  const f=start(fileFixture(Buffer.alloc(0),'upload'));
  try {
    const snap=await f.snapshot(2);f.errors.set('DOM.setFileInputFiles','lost file ACK');
    const result=await f.controller.run({action:'upload',tab_id:2,ref:ref(snap,'button'),files:['C:/one']});
    assert.equal(result.ok,false);assert.equal(result.phase,'outcome_unknown');assert.equal(f.intercept.get(2),false);
    assert.equal(f.calls.filter(c=>c.method==='DOM.setFileInputFiles').length,1);
  } finally {await f.close();}
});


for (const [name, data, uri] of [
  ['Unicode', Buffer.from('payload privado Élodie你🙂\n'), 'data:text/plain;charset=utf-8,' + encodeURIComponent('payload privado Élodie你🙂\n')],
  ['binary', Buffer.from(Array.from({length:256},(_,i)=>i)), 'data:application/octet-stream;base64,' + Buffer.from(Array.from({length:256},(_,i)=>i)).toString('base64')],
]) {
  test('data link fixed readers retain bytes but omit the URL from public source: '+name, async()=>{
    const f=start(fileFixture(data));f.file.href=uri;
    try {
      const snap=await f.snapshot(2),result=await f.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/new'});
      assert.equal(result.ok,true,JSON.stringify(result));
      assert.deepEqual(Buffer.concat(result.result.download.parts.map(part=>Buffer.from(part,'base64'))),data);
      assert.deepEqual(result.result.source,{scheme:'data:',url_omitted:true,document:{...f.controller.state(2).refs.get(ref(snap,'button')).document}});
      assert.equal(Object.hasOwn(result.result.source,'url'),false);assert(!JSON.stringify(result.result.source).includes(uri.split(',')[1]));
      assert.equal(f.file.releases,1);assert.equal(f.objects.size,0);
    } finally {await f.close();}
  });
}

for (const condition of ['detached-before','detached-after','foreign-document-after']) {
  test('retained anchor with unchanged main document fails when '+condition,async()=>{
    const f=start(fileFixture(Buffer.alloc(70000,2)));
    try {
      const snap=await f.snapshot(2),serial=f.pages.get(2).serial,generation=f.controller.state(2).generation;
      if(condition==='detached-before')f.anchor.isConnected=false;
      else if(condition==='detached-after')f.file.detachAfterMeta=true;
      else f.file.foreignDocumentAfterMeta=true;
      const result=await f.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/new'});
      assert.equal(result.ok,false);assert.equal(result.error.code,'download_read_failed');assert.equal(result.result,undefined);
      assert.equal(f.pages.get(2).serial,serial);assert.equal(f.controller.state(2).generation,generation);
      assert.equal(f.pages.get(2).url,snap.document.url);assert.equal(f.objects.size,0);assert.equal(f.file.releases,1);
      assert.equal(f.calls.filter(c=>c.method==='DOM.resolveNode').length,1);
      assert.equal(f.effects().length,0);
    } finally {await f.close();}
  });
}

async function withOwnedDownloadServer(data, run) {
  const {createServer}=await import('node:http'),requests=[];
  const server=createServer((request,response)=>{requests.push(request.url);if(request.url!=='/assets/download.bin'){response.writeHead(404);response.end();return;}response.writeHead(200,{'Content-Type':'application/octet-stream'});response.end(data);});
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  try{return await run('http://127.0.0.1:'+server.address().port,requests);}
  finally{server.closeIdleConnections();await new Promise((resolve,reject)=>server.close(error=>error?reject(error):resolve()));}
}

test('relative href uses the actual base URI instead of the main frame URL',async()=>{
  const data=Buffer.from('base correcto Élodie你🙂');
  await withOwnedDownloadServer(data,async(origin,requests)=>{
    const f=start(fileFixture(data));f.pages.get(2).url=origin+'/page/index.html';f.file.baseURI=origin+'/assets/';f.file.href='download.bin';
    try {
      const snap=await f.snapshot(2),result=await f.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/new'});
      assert.equal(result.ok,true,JSON.stringify(result));assert.deepEqual(requests,['/assets/download.bin']);
      assert.deepEqual(Buffer.concat(result.result.download.parts.map(part=>Buffer.from(part,'base64'))),data);
      assert.equal(result.result.source.url,origin+'/assets/download.bin');assert.equal(result.result.source.scheme,'http:');
      assert.equal(f.file.releases,1);assert.equal(f.objects.size,0);
    } finally {await f.close();}
  });
});

test('base URI replacement during the read fails without publishing bytes',async()=>{
  const data=Buffer.alloc(70000,7);
  await withOwnedDownloadServer(data,async(origin,requests)=>{
    const f=start(fileFixture(data));f.pages.get(2).url=origin+'/page/index.html';f.file.baseURI=origin+'/assets/';f.file.href='download.bin';f.file.baseAfterMeta=origin+'/different/';
    try {
      const snap=await f.snapshot(2),serial=f.pages.get(2).serial,generation=f.controller.state(2).generation;
      const result=await f.controller.run({action:'download',tab_id:2,ref:ref(snap,'button'),output_path:'C:/new'});
      assert.equal(result.ok,false);assert.equal(result.error.code,'download_link_changed');assert.equal(result.result,undefined);
      assert.deepEqual(requests,['/assets/download.bin']);assert.equal(f.pages.get(2).serial,serial);assert.equal(f.controller.state(2).generation,generation);
      assert.equal(f.file.href,'download.bin');assert.equal(f.objects.size,0);assert.equal(f.file.releases,1);
    } finally {await f.close();}
  });
});


// ---------------------------------------------------------------- looking and moving like a person (1.1.0)
test('reload waits for the new document and reads it back', async () => {
  const f = start(fixture()); const before = await f.snapshot(1);
  const reply = await f.controller.run({action: 'reload', tab_id: 1});
  assert.equal(reply.ok, true, JSON.stringify(reply)); assert.equal(reply.phase, 'readback_complete');
  assert.ok(f.calls.some(call => call.method === 'Page.reload'));
  assert.notEqual(reply.result.snapshot.generation, before.generation);
  assert.equal(reply.result.snapshot.document.url, 'https://writer.test/1');
});

test('navigate reads back the page it went to, not the one it left', async () => {
  const f = start(fixture()); await f.snapshot(1);
  const reply = await f.controller.run({action: 'navigate', tab_id: 1, url: 'https://writer.test/library'});
  assert.equal(reply.ok, true, JSON.stringify(reply)); assert.equal(reply.result.snapshot.document.url, 'https://writer.test/library');
});

test('back and forward follow the tab history and say when there is none', async () => {
  const f = start(fixture()); f.pages.get(1).history = {index: 1, entries: ['https://writer.test/home', 'https://writer.test/1']};
  const back = await f.controller.run({action: 'back', tab_id: 1});
  assert.equal(back.ok, true, JSON.stringify(back)); assert.equal(back.result.snapshot.document.url, 'https://writer.test/home');
  const none = await f.controller.run({action: 'back', tab_id: 1});
  assert.equal(none.ok, false); assert.equal(none.error.code, 'no_history_entry'); assert.equal(none.phase, 'not_dispatched');
  const forward = await f.controller.run({action: 'forward', tab_id: 1});
  assert.equal(forward.ok, true); assert.equal(forward.result.snapshot.document.url, 'https://writer.test/1');
});

test('scroll turns the wheel at the centre, over an element, or brings a ref into view', async () => {
  const f = start(fixture()); let snap = await f.snapshot(1);
  const down = await f.controller.run({action: 'scroll', tab_id: 1, direction: 'down', amount: 2});
  assert.equal(down.ok, true, JSON.stringify(down));
  const wheel = f.calls.filter(call => call.method === 'Input.dispatchMouseEvent' && call.params.type === 'mouseWheel');
  assert.deepEqual(wheel.at(-1).params, {type: 'mouseWheel', x: 400, y: 300, deltaX: 0, deltaY: 240});
  snap = down.result.snapshot;
  const over = await f.controller.run({action: 'scroll', tab_id: 1, direction: 'up', ref: ref(snap, 'button')});
  assert.equal(over.ok, true, JSON.stringify(over));
  assert.equal(f.calls.filter(call => call.params?.type === 'mouseWheel').at(-1).params.deltaY, -360);
  snap = over.result.snapshot;
  const into = await f.controller.run({action: 'scroll', tab_id: 1, ref: ref(snap, 'button')});
  assert.equal(into.ok, true); assert.ok(f.calls.at(-1).method !== 'Input.dispatchMouseEvent' || f.calls.some(c => c.method === 'DOM.scrollIntoViewIfNeeded'));
  const bad = await f.controller.run({action: 'scroll', tab_id: 1, direction: 'sideways'});
  assert.equal(bad.ok, false); assert.equal(bad.error.code, 'invalid_arguments');
});

test('hover moves the mouse over the verified target without clicking', async () => {
  const f = start(fixture()); const snap = await f.snapshot(1);
  const reply = await f.controller.run({action: 'hover', tab_id: 1, ref: ref(snap, 'button')});
  assert.equal(reply.ok, true, JSON.stringify(reply));
  assert.deepEqual(f.pages.get(1).mouseTargets.map(m => m.type), ['mouseMoved']); assert.equal(f.pages.get(1).clicks, 0);
});

test('select picks an option through the fixed reader and reports it', async () => {
  const f = start(fixture()); const snap = await f.snapshot(1);
  const reply = await f.controller.run({action: 'select', tab_id: 1, ref: ref(snap, 'textbox'), value: 'Grande'});
  assert.equal(reply.ok, true, JSON.stringify(reply)); assert.deepEqual(reply.result.selected, {value: 'Grande', text: 'Grande'});
  const call = f.calls.find(c => c.method === 'Runtime.callFunctionOn'); assert.match(call.params.functionDeclaration, /localName !== 'select'/);
});

test('screenshot can zoom on a region or a ref', async () => {
  const f = start(fixture()); const snap = await f.snapshot(1);
  const region = await f.controller.run({action: 'screenshot', tab_id: 1, region: [10, 20, 210, 120]});
  assert.equal(region.ok, true, JSON.stringify(region)); assert.deepEqual(region.result.clip, {x: 10, y: 20, width: 200, height: 100, scale: 1});
  assert.equal(Buffer.from(region.result.image.data, 'base64').toString(), 'SIMULATED-PNG-TARGET-1-CLIP');
  const byRef = await f.controller.run({action: 'screenshot', tab_id: 1, ref: ref(snap, 'button')});
  assert.equal(byRef.ok, true, JSON.stringify(byRef)); assert.deepEqual(byRef.result.clip, {x: -50, y: 10, width: 150, height: 40, scale: 1});
  const whole = await f.controller.run({action: 'screenshot', tab_id: 1}); assert.equal(whole.result.clip, undefined);
});

test('open_tab works in the background and close_tab only closes those tabs', async () => {
  const f = start(fixture());
  const opened = await f.controller.run({action: 'open_tab', url: 'https://writer.test/product'});
  assert.equal(opened.ok, true); assert.equal(opened.result.tab.active, false); assert.equal(opened.result.tab.opened_by_faustus, true);
  assert.deepEqual(f.calls.find(c => c.api === 'tabs.create'), {api: 'tabs.create', url: 'https://writer.test/product', active: false});
  const tabs = await f.controller.run({action: 'tabs'}); assert.equal(tabs.result.tabs.find(t => t.id === opened.result.tab.id).opened_by_faustus, true);
  const mine = await f.controller.run({action: 'close_tab', tab_id: 1});
  assert.equal(mine.ok, false); assert.equal(mine.error.code, 'not_opened_by_faustus'); assert.ok(f.pages.has(1));
  const closed = await f.controller.run({action: 'close_tab', tab_id: opened.result.tab.id});
  assert.equal(closed.ok, true); assert.equal(f.pages.has(opened.result.tab.id), false);
  const bad = await f.controller.run({action: 'open_tab', url: 'file:///etc/passwd'}); assert.equal(bad.ok, false);
});

test('reload_extension asks the browser to reload the extension', async () => {
  const f = start(fixture());
  const reply = await f.controller.run({action: 'reload_extension'});
  assert.equal(reply.ok, true); assert.equal(reply.result.version, '1.1.0');
  await new Promise(resolve => setTimeout(resolve, 250)); assert.ok(f.calls.some(c => c.api === 'runtime.reload'));
});

test('wait gives the page time and reads it again without input', async () => {
  const f = start(fixture()); await f.snapshot(1);
  const started = Date.now();
  const reply = await f.controller.run({action: 'wait', tab_id: 1, seconds: 0.3});
  assert.equal(reply.ok, true, JSON.stringify(reply)); assert.ok(Date.now() - started >= 280);
  assert.ok(reply.result.snapshot.nodes.length > 0); assert.equal(f.effects().length, 0);
});
