import test from 'node:test';
import assert from 'node:assert/strict';
import {BrowserJourneys} from '../../browser-extension/journeys.js';
import {BrowserCapture} from '../../browser-extension/capture.js';

function fixture() {
  let data = {sessions: {}, turns: {}};
  const store = {async read() { return structuredClone(data); }, async write(value) { data = structuredClone(value); }};
  const calls = [], tabs = new Map([[7, {id: 7, url: 'https://fixture.test/', title: 'Fixture', active: true}]]);
  const controller = {created: new Set(), chrome: {
    action: {async openPopup() { calls.push('popup'); }},
    tabs: {async get(id) { if (!tabs.has(id)) throw Error('No tab'); return tabs.get(id); }, async query() { return [...tabs.values()]; },
      async create({url, active}) { const tab = {id: tabs.size + 100, url, active}; tabs.set(tab.id, tab); calls.push(tab); return tab; }},
    debugger: {async sendCommand(source, method, params) { calls.push({source, method, params}); return {}; }}},
    invalidate(id) { calls.push({invalidated: id}); }, async send(context, method, params) { return this.chrome.debugger.sendCommand({tabId: context.tabId}, method, params); }};
  return {controller, store, calls, tabs, journeys: new BrowserJourneys(controller, store)};
}

test('durable named sessions reuse, restore in background, forget only the name and allow arbitrary literal names', async () => {
  const f = fixture();
  for (const name of ['purchases', '__proto__', 'constructor']) await f.journeys.execute({action: 'session_save', name, tab_id: 7});
  const freshWorker = new BrowserJourneys(f.controller, f.store);
  assert.equal((await freshWorker.execute({action: 'sessions'})).sessions.length, 3);
  assert.equal((await freshWorker.execute({action: 'session_open', name: '__proto__'})).restored, false);
  f.tabs.delete(7);
  const restored = await freshWorker.execute({action: 'session_open', name: 'purchases'});
  assert.equal(restored.restored, true); assert.equal(f.tabs.get(restored.tab.id).active, false);
  await freshWorker.execute({action: 'session_forget', name: 'purchases'});
  assert.equal(f.tabs.size, 1); assert.equal((await freshWorker.execute({action: 'sessions'})).sessions.length, 2);
  await freshWorker.updated(restored.tab.id, {url: 'https://fixture.test/final', title: 'Final'});
  await assert.rejects(freshWorker.execute({action: 'session_open', name: 'missing'}), {code: 'session_not_found'});
});

test('saved sessions follow navigation and closed tabs restore their final URL', async () => {
  const f = fixture();
  await f.journeys.execute({action: 'session_save', tab_id: 7, name: 'moving'});
  await f.journeys.updated(7, {url: 'https://fixture.test/next', title: 'Next'});
  await f.journeys.removed(7); f.tabs.delete(7);
  const result = await f.journeys.execute({action: 'session_open', name: 'moving'});
  assert.equal(result.tab.url, 'https://fixture.test/next'); assert.equal(result.restored, true);
});

test('handoff and adoption survive a fresh worker, stale cards cannot continue and each transition invalidates refs', async () => {
  const f = fixture();
  const first = await f.journeys.execute({action: 'handoff', tab_id: 7, reason: 'Complete the step'});
  const second = await f.journeys.execute({action: 'adopt_tab', tab_id: 7, name: 'handed over'});
  const fresh = new BrowserJourneys(f.controller, f.store);
  assert.equal(await fresh.waiting(7), true);
  await assert.rejects(fresh.person({tab_id: 7, turn_id: first.turn.id}), {code: 'turn_changed'});
  assert.equal((await fresh.execute({action: 'sessions'})).sessions.length, 0);
  await fresh.person({tab_id: 7, turn_id: second.turn.id});
  assert.equal(await fresh.waiting(7), false);
  assert.equal((await fresh.execute({action: 'sessions'})).sessions[0].adopted, true);
  assert.equal(f.calls.filter(call => call.invalidated === 7).length, 3);
});

test('HAR retains redirects, binary body encoding, failures and observed query fields', async () => {
  const f = fixture(), capture = new BrowserCapture(f.controller), context = {tabId: 7};
  f.controller.chrome.debugger.sendCommand = async (source, method) => method === 'Network.getResponseBody' ? {body: 'AAEC', base64Encoded: true} : {};
  await capture.execute(context, {action: 'har_start', include_bodies: true});
  const request = (url, timestamp) => capture.onEvent(7, 'Network.requestWillBeSent', {requestId: 'r', timestamp, wallTime: 1000 + timestamp, request: {url, method: 'GET', headers: {Accept: '*/*'}}});
  request('https://fixture.test/start?q=a&q=b', 1);
  capture.onEvent(7, 'Network.requestWillBeSent', {requestId: 'r', timestamp: 2, wallTime: 1002,
    redirectResponse: {status: 302, headers: {Location: '/end'}, protocol: 'h2', mimeType: 'text/html'}, request: {url: 'https://fixture.test/end', method: 'GET', headers: {}}});
  capture.onEvent(7, 'Network.responseReceived', {requestId: 'r', timestamp: 3, response: {status: 200, protocol: 'h2', mimeType: 'application/octet-stream'}});
  capture.onEvent(7, 'Network.loadingFinished', {requestId: 'r', timestamp: 4, encodedDataLength: 3});
  request('https://fixture.test/failed', 5); capture.onEvent(7, 'Network.loadingFailed', {requestId: 'r', timestamp: 6, errorText: 'net::ERR_FAILED'});
  const result = await capture.execute(context, {action: 'har_stop'});
  const har = JSON.parse(Buffer.concat(result.download.parts.map(part => Buffer.from(part, 'base64'))));
  assert.equal(har.log.version, '1.2'); assert.equal(har.log.entries.length, 3);
  assert.deepEqual(har.log.entries[0].request.queryString, [{name: 'q', value: 'a'}, {name: 'q', value: 'b'}]);
  assert.equal(har.log.entries[0].response.status, 302); assert.equal(har.log.entries[0].response.redirectURL, '/end');
  assert.equal(har.log.entries[1].response.content.encoding, 'base64'); assert.equal(har.log.entries[1].response.content.text, 'AAEC');
  assert.equal(har.log.entries[2]._error, 'net::ERR_FAILED'); assert.equal(capture.network.size, 0);
});

test('recording ACKs frames, freezes a bounded recent minute including a static tail and removes its cursor on stop', async () => {
  const f = fixture(), capture = new BrowserCapture(f.controller), context = {tabId: 7};
  await capture.execute(context, {action: 'recording_start', cursor: true});
  const now = Date.now() / 1000;
  for (const timestamp of [now - 100, now - 70, now - 30, now - 29.99, now - 1]) capture.onEvent(7, 'Page.screencastFrame', {sessionId: 1, data: 'jpeg', metadata: {timestamp}});
  assert.equal(capture.video.get(7).frames.length, 4);
  const frozen = capture.freeze(7); assert(frozen.frames.at(-1).timestamp - frozen.frames[0].timestamp < 60);
  capture.onEvent(7, 'Page.screencastFrame', {sessionId: 2, data: 'later', metadata: {timestamp: now + 1}});
  assert.deepEqual((await capture.execute(context, {action: 'last_minute'})).video.frames, frozen.frames);
  const full = await capture.execute(context, {action: 'recording_stop'}); assert(full.video.frames.length >= 5);
  assert.equal(capture.video.size, 0);
  assert.equal(f.calls.filter(call => call.method === 'Page.screencastFrameAck').length, 6);
  assert(f.calls.some(call => call.method === 'Runtime.evaluate' && call.params.expression.includes('removeEventListener')));
});
