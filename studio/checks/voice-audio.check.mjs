import assert from 'node:assert/strict';
import { build } from 'esbuild';
const result = await build({ entryPoints: ['studio/src/voice/audio.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
globalThis.window = globalThis;
globalThis.document = { documentElement: {} };
const { capture, playSpeech } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
const config = { configured: true, dependency_installed: true, provider: 'local', execution: 'local' };
let requests = 0;
globalThis.fetch = async () => { requests++; return new Response(JSON.stringify({ text: 'hola' })); };

// Denied permission never starts an upload or browser/cloud fallback.
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: async () => { throw new DOMException('Denied', 'NotAllowedError'); } } } });
globalThis.MediaRecorder = class { static isTypeSupported() { return true; } };
await assert.rejects(capture(config, { signal: new AbortController().signal }), { name: 'NotAllowedError' });
assert.equal(requests, 0);

// A microphone grant arriving after cancellation must immediately release tracks.
let grant, stops = 0;
navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { grant = resolve; });
const abort = new AbortController(); const pending = capture(config, { signal: abort.signal });
abort.abort(); grant({ getTracks: () => [{ stop() { stops++; } }] });
await assert.rejects(pending, { name: 'AbortError' }); assert.equal(stops, 1);

// Releasing a recording explicitly cannot submit audio after cancel (even without aborting signal).
let closes = 0;
const track = { stop() { stops++; }, onended: null };
navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [track] });
globalThis.AudioContext = class {
  state = 'running';
  async resume() {}
  async close() { this.state = 'closed'; closes++; }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createAnalyser() { return { fftSize: 8, getByteTimeDomainData(data) { data.fill(150); } }; }
};
globalThis.MediaRecorder = class {
  state = 'inactive'; mimeType = 'audio/webm';
  static isTypeSupported() { return true; }
  start() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; queueMicrotask(() => { this.ondataavailable({ data: new Blob(['test']) }); this.onstop(); }); }
};
const recording = await capture(config, { signal: new AbortController().signal });
await new Promise(resolve => setTimeout(resolve, 60));
recording.cancel(); await assert.rejects(recording.done, { name: 'AbortError' });
await new Promise(resolve => setTimeout(resolve, 0)); assert.equal(requests, 0); assert.equal(closes, 1);

// Normal completion is exactly one upload to the selected provider.
const normal = await capture(config, { signal: new AbortController().signal });
await new Promise(resolve => setTimeout(resolve, 60)); normal.stop();
assert.equal(await normal.done, 'hola'); assert.equal(requests, 1);

// A late synthesis response cannot play after barge-in/close.
let deliver, audioObjects = 0;
globalThis.fetch = () => new Promise(resolve => { deliver = resolve; });
globalThis.Audio = class { constructor() { audioObjects++; } };
const playbackAbort = new AbortController(); const playback = playSpeech('Hola.', config, playbackAbort.signal);
playbackAbort.abort(); deliver(new Response(new Blob(['audio'])));
await assert.rejects(playback, { name: 'AbortError' }); assert.equal(audioObjects, 0);

// Browser recognition starts once; stop never silently starts a second listen.
let starts = 0, rec;
globalThis.SpeechRecognition = class {
  constructor() { rec = this; }
  start() { starts++; }
  abort() { this.onend?.(); }
  stop() { this.onend?.(); }
};
const browser = await capture({ ...config, provider: 'browser', execution: 'browser' }, { signal: new AbortController().signal });
rec.onresult({ results: [[{ transcript: 'Hola Faustus' }]] }); browser.stop();
assert.equal(await browser.done, 'Hola Faustus'); assert.equal(starts, 1);
console.log('ALL OK: audio permission, late grant, cancel, one upload, late TTS and browser recognition');
// Natural dictation runs for browser capture too and preserves its original.
let polishRequests = 0;
globalThis.fetch = async (url, options) => {
  assert.equal(url, '/api/stt/polish'); polishRequests++;
  assert.deepEqual(JSON.parse(options.body), { text: 'um book Friday no Saturday', mode: 'clean' });
  return new Response(JSON.stringify({ status: 'edited', text: 'Book Saturday.' }));
};
const natural = await capture({ ...config, execution: 'browser' }, { signal: new AbortController().signal, natural: true });
rec.onresult({ results: [[{ transcript: 'um book Friday no Saturday' }]] }); natural.stop();
assert.equal(await natural.done, 'Book Saturday.'); assert.equal(natural.rawText, 'um book Friday no Saturday');
assert.equal(polishRequests, 1);
// Stop commands must never reach the editor.
const stopCommand = await capture({ ...config, execution: 'browser' }, { signal: new AbortController().signal, natural: true });
rec.onresult({ results: [[{ transcript: 'stop' }]] }); stopCommand.stop();
assert.equal(await stopCommand.done, 'stop'); assert.equal(polishRequests, 1);
// Network failure preserves speech instead of dropping the user's message.
globalThis.fetch = async () => { throw new Error('offline'); };
const fallback = await capture({ ...config, execution: 'browser' }, { signal: new AbortController().signal, natural: true });
rec.onresult({ results: [[{ transcript: 'keep my words' }]] }); fallback.stop();
assert.equal(await fallback.done, 'keep my words');
// Cancelling after ASR prevents a late editor response from being submitted.
let finishPolish;
globalThis.fetch = () => new Promise(resolve => { finishPolish = resolve; });
const cancelledPolish = await capture({ ...config, execution: 'browser' }, { signal: new AbortController().signal, natural: true });
rec.onresult({ results: [[{ transcript: 'late words' }]] }); cancelledPolish.stop();
await new Promise(resolve => setTimeout(resolve, 0)); cancelledPolish.cancel();
finishPolish(new Response(JSON.stringify({ status: 'edited', text: 'Late words.' })));
await assert.rejects(cancelledPolish.done, { name: 'AbortError' });
console.log('ALL OK: natural dictation, original, stop command, fallback and cancelled polish');
// Server ASR uses the same editor after releasing the microphone.
const paths = [];
globalThis.fetch = async (url) => {
  paths.push(url);
  return new Response(JSON.stringify(url === '/api/stt/transcribe'
    ? { text: 'eh martes no miércoles', language: 'es' }
    : { status: 'edited', text: 'Miércoles.' }));
};
const localNatural = await capture(config, { signal: new AbortController().signal, natural: true });
await new Promise(resolve => setTimeout(resolve, 60)); localNatural.stop();
assert.equal(await localNatural.done, 'Miércoles.');
assert.equal(localNatural.rawText, 'eh martes no miércoles'); assert.equal(localNatural.language, 'es');
assert.deepEqual(paths, ['/api/stt/transcribe', '/api/stt/polish']);
console.log('ALL OK: local ASR and natural dictation pipeline');
// An all-filler utterance can legitimately clean to no message at all.
globalThis.fetch = async () => new Response(JSON.stringify({ status: 'edited', text: '' }));
const fillerOnly = await capture({ ...config, execution: 'browser' }, { signal: new AbortController().signal, natural: true });
rec.onresult({ results: [[{ transcript: 'umm eeh' }]] }); fillerOnly.stop();
assert.equal(await fillerOnly.done, ''); assert.equal(fillerOnly.rawText, 'umm eeh');
console.log('ALL OK: all-filler cleanup yields no message');
