import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Window } from 'happy-dom';
import { writeFileSync, unlinkSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
const dom = new Window({ url: 'http://localhost' });
for (const key of ['window', 'document', 'localStorage', 'navigator', 'HTMLElement']) Object.defineProperty(globalThis, key, { value: dom[key] ?? dom, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
localStorage.setItem('faustus_voice_prefs', JSON.stringify({ continuous: false, autoSend: false, readAloud: false }));
let finishCapture, captureOptions, openMicResult, calls = [], sent = [];
const config = { configured: true, dependency_installed: true, execution: 'browser' };
globalThis.voiceFixture = {
  capabilities: async () => config, openMic: async () => { if (openMicResult) return openMicResult; throw Error('no physical mic'); },
  capture: async (_, options) => {
    options.signal.throwIfAborted(); captureOptions = options;
    const done = new Promise((resolve, reject) => { finishCapture = resolve; options.signal.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')), { once: true }); });
    return { done, analyser: null, stop: () => finishCapture('change Friday to Saturday'), cancel() {} };
  },
};
const result = await build({ entryPoints: ['studio/src/voice/VoicePanel.tsx'], bundle: true, format: 'esm', platform: 'node', packages: 'external', write: false, loader: { '.css': 'empty' }, plugins: [{ name: 'voice-fixture', setup(b) {
  b.onResolve({ filter: /^\.\/audio$/ }, () => ({ path: 'voice-fixture', namespace: 'fixture' }));
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({ contents: 'export const {capabilities,capture,openMic}=globalThis.voiceFixture; export const playSpeech=async()=>{}; export const watchForSpeech=()=>{}; export const audioLevel=()=>0;' }));
} }] });
const output = resolve('.voice-revision-check.mjs'); writeFileSync(output, result.outputFiles[0].text);
try {
  const React = await import('react'); const { act } = React;
  const { createRoot } = await import('react-dom/client'); const { MemoryRouter } = await import('react-router');
  const { default: VoicePanel } = await import(pathToFileURL(output));
  const host = document.createElement('div'); document.body.append(host); const root = createRoot(host);
  const flush = async fn => act(async () => { await fn?.(); await new Promise(resolve => setTimeout(resolve, 0)); });
  const button = label => [...host.querySelectorAll('button')].find(b => b.textContent === label);
  await flush(() => root.render(React.createElement(MemoryRouter, null, React.createElement(VoicePanel, { busy: false, sessionName: 'Test', onSend: text => sent.push(text), onStop() {}, onClose() {} }))));
  await flush(() => button('Start speaking').click());
  await flush(() => finishCapture('Meet Friday.'));
  assert.equal(host.querySelector('textarea').value, 'Meet Friday.');
  globalThis.fetch = async (_, options) => { calls.push(JSON.parse(options.body)); return new Response(JSON.stringify({ status: 'edited', text: 'Meet Saturday.' })); };
  await flush(() => button('Correct by voice').click());
  assert.equal(captureOptions.natural, false); assert.equal(captureOptions.autoStop, true);
  assert.equal(host.querySelector('textarea').value, 'Meet Friday.');
  assert.ok(button('Finish speaking'));
  await flush(() => button('Finish speaking').click());
  assert.deepEqual(calls, [{ text: 'change Friday to Saturday', mode: 'revise', draft: 'Meet Friday.' }]);
  assert.equal(host.querySelector('textarea').value, 'Meet Saturday.'); assert.equal(sent.length, 0);
  assert.ok(button('Send message'));
  await flush(() => button('Correct by voice').click());
  await flush(() => button('Mute microphone').click());
  assert.equal(host.querySelector('textarea').value, 'Meet Saturday.'); assert.ok(button('Send message'));
  let deliverRevision;
  globalThis.fetch = () => new Promise(resolve => { deliverRevision = resolve; });
  await flush(() => button('Correct by voice').click()); await flush(() => button('Finish speaking').click());
  await flush(() => button('Mute microphone').click());
  await flush(() => deliverRevision(new Response(JSON.stringify({ status: 'edited', text: 'Late edit must not land.' }))));
  assert.equal(host.querySelector('textarea').value, 'Meet Saturday.'); assert.ok(button('Send message'));
  globalThis.fetch = async () => { throw Error('offline'); };
  await flush(() => button('Correct by voice').click()); await flush(() => button('Finish speaking').click());
  assert.equal(host.querySelector('textarea').value, 'Meet Saturday.'); assert.ok(host.querySelector('[role="alert"]'));
  assert.equal(sent.length, 0);
  // Undo restores the exact draft before the successful revision.
  await flush(() => button('Undo voice correction').click());
  assert.equal(host.querySelector('textarea').value, 'Meet Friday.');
  assert.equal(button('Undo voice correction'), undefined);
  // The undo snapshot includes manual edits, not only the initial ASR text.
  await flush(() => {
    const textarea = host.querySelector('textarea');
    Object.getOwnPropertyDescriptor(dom.HTMLTextAreaElement.prototype, 'value').set.call(textarea, 'Meet Friday at noon.');
    textarea.dispatchEvent(new dom.Event('input', { bubbles: true }));
  });
  globalThis.fetch = async (_, options) => {
    assert.equal(JSON.parse(options.body).draft, 'Meet Friday at noon.');
    return new Response(JSON.stringify({ status: 'edited', text: 'Meet Saturday at noon.' }));
  };
  await flush(() => button('Correct by voice').click()); await flush(() => button('Finish speaking').click());
  assert.equal(host.querySelector('textarea').value, 'Meet Saturday at noon.');
  await flush(() => button('Undo voice correction').click());
  assert.equal(host.querySelector('textarea').value, 'Meet Friday at noon.');
  // A microphone permission/device response arriving after cancel is closed.
  let finishOpen, closedLateMic = 0;
  openMicResult = new Promise(resolve => { finishOpen = resolve; });
  await flush(() => button('Correct by voice').click());
  await flush(() => button('Mute microphone').click());
  await flush(() => finishOpen({ close() { closedLateMic++; } }));
  assert.equal(closedLateMic, 1);
  assert.equal(host.querySelector('textarea').value, 'Meet Friday at noon.'); assert.ok(button('Send message'));
  // Real panel filtering must preserve useful short replies, not silently
  // restart listening or discard them as ASR hallucinations.
  openMicResult = undefined;
  const shortReplies = ['sí', 'no', 'OK', 'gracias'];
  for (const utterance of shortReplies) {
    await flush(() => root.render(React.createElement(MemoryRouter, null,
      React.createElement(VoicePanel, { key: utterance, busy: false, sessionName: 'Short reply',
        onSend: text => sent.push(text), onStop() {}, onClose() {} }))));
    await flush(() => button('Start speaking').click());
    await flush(() => finishCapture(utterance));
    assert.equal(host.querySelector('[data-testid="voice-panel"]').dataset.phase, 'review', utterance);
    assert.equal(host.querySelector('textarea').value, utterance);
    assert.equal(button('Send message').disabled, false);
    await flush(() => button('Send message').click());
    assert.equal(sent.at(-1), utterance);
  }
  assert.deepEqual(sent, shortReplies);
  await flush(() => root.unmount());
  console.log('ALL OK: voice correction, visible draft, finish, review-only, cancel, failure and short replies sent');
} finally { unlinkSync(output); await dom.happyDOM.close(); }

