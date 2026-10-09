// Voice turn timeline: the gap arithmetic (studio/src/voice/timeline.ts) and the
// panel wiring (VoicePanel.tsx) with synthetic audio -- no microphone, no sound.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Window } from 'happy-dom';
import { writeFileSync, unlinkSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const dom = new Window({ url: 'http://localhost' });
for (const key of ['window', 'document', 'localStorage', 'navigator', 'HTMLElement']) Object.defineProperty(globalThis, key, { value: dom[key] ?? dom, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
process.env.NODE_ENV = 'development';

// ---- 1. pure arithmetic ----
const lib = await build({ entryPoints: ['studio/src/voice/timeline.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const { computeGaps, timelineLine, timelineDetail, seconds, postTimeline } = await import(`data:text/javascript;base64,${Buffer.from(lib.outputFiles[0].text).toString('base64')}`);
const T = 1_790_000_000_000;
const full = { speech_end: T, transcript_ready: T + 400, request_sent: T + 450, first_token: T + 1650, tts_chunk: T + 2100, first_audio: T + 2250, stt_server_ms: 310, tts_server_ms: 120 };
const g = computeGaps(full);
assert.equal(g.speech_to_text, 400); assert.equal(g.text_to_first_token, 1250); assert.equal(g.first_token_to_audio, 600);
assert.equal(g.text_to_request, 50); assert.equal(g.request_to_first_token, 1200);
assert.equal(g.first_token_to_tts_chunk, 450); assert.equal(g.tts_chunk_to_playback, 150); assert.equal(g.total, 2250);
assert.equal(timelineLine(full), `speech→text 0.4 s · text→1st token ${seconds(1250)} · 1st token→audio 0.6 s`);
assert.match(timelineDetail(full), /Server speech-to-text: 0\.3 s/);
// missing or impossible marks give no number, never a wrong one
const partial = computeGaps({ speech_end: T, transcript_ready: T + 400 });
assert.equal(partial.speech_to_text, 400); assert.equal(partial.first_token_to_audio, null);
assert.equal(computeGaps({ speech_end: T + 5, transcript_ready: T }).speech_to_text, null);
assert.equal(timelineLine({ speech_end: T }), '');
assert.equal(seconds(null), '-');
// no session or turn id: nothing is posted
let posted = [];
globalThis.fetch = async (url, init) => { posted.push([url, JSON.parse(init.body)]); return new Response('{}'); };
postTimeline('', 't1', full); postTimeline('s1', '', full);
assert.equal(posted.length, 0);
postTimeline('s1', 't1', full);
assert.deepEqual(posted[0][1].session_id, 's1'); assert.equal(posted[0][0], '/api/voice/timeline');
// a failing network never throws into the voice turn
globalThis.fetch = () => Promise.reject(new Error('offline'));
postTimeline('s1', 't1', full);

// ---- 2. the panel records marks where they happen ----
let finishCapture, captureOptions, posts = [];
const config = { configured: true, dependency_installed: true, execution: 'browser' };
globalThis.voiceFixture = {
  capabilities: async () => config, openMic: async () => { throw Error('no physical mic'); },
  capture: async (_, options) => {
    captureOptions = options;
    const done = new Promise(r => { finishCapture = r; });
    return { done, analyser: null, stop() {}, cancel() {}, rawText: 'Hello there', sttServerMs: 280 };
  },
  playSpeech: async (_text, _cfg, _signal, onAudio, onSynth) => {
    await new Promise(r => setTimeout(r, 15)); onSynth?.({ serverMs: 90 });
    await new Promise(r => setTimeout(r, 15)); onAudio?.(null, true);
  },
};
const panel = await build({ entryPoints: ['studio/src/voice/VoicePanel.tsx'], bundle: true, format: 'esm', platform: 'node', packages: 'external', write: false, loader: { '.css': 'empty' }, plugins: [{ name: 'voice-fixture', setup(b) {
  b.onResolve({ filter: /^\.\/audio$/ }, () => ({ path: 'voice-fixture', namespace: 'fixture' }));
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({ contents: 'export const {capabilities,capture,openMic,playSpeech}=globalThis.voiceFixture; export const watchForSpeech=()=>{}; export const audioLevel=()=>0;' }));
} }] });
const output = resolve(`.voice-timeline-check-${process.pid}.mjs`); writeFileSync(output, panel.outputFiles[0].text);
try {
  const React = await import('react'); const { act } = React;
  const { createRoot } = await import('react-dom/client'); const { MemoryRouter } = await import('react-router');
  const { default: VoicePanel } = await import(pathToFileURL(output));
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const flush = async fn => act(async () => { await fn?.(); await wait(0); });
  const turn = (text) => ({ id: 'a1', role: 'assistant', text, thinking: '', thoughts: [], steps: [], rounds: 1, sources: [], images: [], attachments: [] });
  const mount = async (extra = {}) => {
    const host = document.createElement('div'); document.body.append(host); const root = createRoot(host);
    const sent = [];
    const render = props => root.render(React.createElement(MemoryRouter, null, React.createElement(VoicePanel, { sessionId: 's1', sessionName: 'Test', onSend: t => sent.push(t), onStop() {}, onClose() {}, busy: false, ...props })));
    await flush(() => render({}));
    return { host, sent, render, button: label => [...host.querySelectorAll('button')].find(b => b.textContent === label) };
  };

  localStorage.setItem('faustus_voice_prefs', JSON.stringify({ continuous: false, autoSend: true, readAloud: true }));
  globalThis.fetch = async (url, init) => { if (String(url).includes('/api/voice/timeline')) posts.push(JSON.parse(init.body)); return new Response('{}'); };
  const a = await mount();
  await flush(() => a.button('Start speaking').click());
  await flush(() => { captureOptions.onTranscribing(); });      // the VAD closed the turn
  await wait(20);
  await flush(() => finishCapture('Hello there'));              // STT done -> auto-send
  assert.deepEqual(a.sent, ['Hello there']);
  await flush(() => a.render({ busy: true }));
  await wait(20);
  await flush(() => a.render({ busy: true, turn: turn('Hi. ') })); // first token arrives
  await wait(20);
  await flush(() => a.render({ busy: false, turn: turn('Hi. How are you?') })); // sentence out -> TTS chunk -> audio plays
  await wait(120);
  await flush();
  assert.equal(posts.length, 1, 'one POST per turn');
  const p = posts[0];
  assert.equal(p.session_id, 's1'); assert.equal(p.turn_id, 'a1');
  for (const k of ['speech_end', 'transcript_ready', 'request_sent', 'first_token', 'tts_chunk', 'first_audio']) assert.ok(p[k] > 1e12, `${k} is a wall-clock instant`);
  assert.ok(p.speech_end <= p.transcript_ready && p.transcript_ready <= p.request_sent && p.request_sent <= p.first_token && p.first_token <= p.tts_chunk && p.tts_chunk <= p.first_audio, 'marks are in order');
  assert.ok(p.transcript_ready - p.speech_end >= 15, 'speech->text covers the STT wait');
  assert.equal(p.stt_server_ms, 280); assert.equal(p.tts_server_ms, 90);
  const line = a.host.querySelector('[data-testid="voice-timeline"]');
  assert.ok(line, 'the compact line is on the panel');
  assert.match(line.textContent, /speech→text \d\.\d s · text→1st token \d\.\d s · 1st token→audio \d\.\d s/);
  assert.match(line.getAttribute('title'), /Server text-to-speech: 0\.1 s/);

  // A transcript the person reviewed and sent by hand is their wait, not the system's: no timeline.
  posts = [];
  localStorage.setItem('faustus_voice_prefs', JSON.stringify({ continuous: false, autoSend: false, readAloud: true }));
  const b = await mount();
  await flush(() => b.button('Start speaking').click());
  await flush(() => { captureOptions.onTranscribing(); });
  await flush(() => finishCapture('Hello there'));
  await flush(() => b.button('Send message').click());
  await flush(() => b.render({ busy: false, turn: { ...turn('Hi there.'), id: 'a2' } }));
  await wait(120); await flush();
  assert.equal(posts.length, 0, 'a hand-sent turn is not timed');
  assert.equal(b.host.querySelector('[data-testid="voice-timeline"]'), null);
  console.log('ALL OK: voice timeline gaps, order, server timings, one POST per turn, hand-sent turns untimed');
} finally {
  try { unlinkSync(output); } catch { /* already gone */ }
}
process.exit(0);
