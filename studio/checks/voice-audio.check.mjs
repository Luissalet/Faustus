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
