/** Explicit, tab-scoped HAR and video capture. Nothing is enabled by connecting the relay. */
const error = (code, message) => Object.assign(new Error(message), {code});
const headers = values => Object.entries(values ?? {}).map(([name, value]) => ({name, value: String(value)}));
const CURSOR_SCRIPT = `(() => {
  if (globalThis.__faustusCursor) return;
  const dot = document.createElement('div'); dot.setAttribute('aria-hidden', 'true'); dot.setAttribute('data-faustus-recording-cursor', '');
  dot.style.cssText = 'position:fixed;left:0;top:0;width:14px;height:14px;border:2px solid white;border-radius:50%;background:#e06c75;box-shadow:0 0 2px #111;pointer-events:none;z-index:2147483647;display:none';
  const move = e => { if (!dot.isConnected) document.documentElement.append(dot); dot.style.display='block'; dot.style.transform='translate('+e.clientX+'px,'+e.clientY+'px)'; };
  const events = ['mousemove', 'mousedown', 'mouseup'];
  for (const event of events) addEventListener(event, move, true);
  globalThis.__faustusCursor = {dot, move, events};
})();`;
const REMOVE_CURSOR = `(() => { const c=globalThis.__faustusCursor; if(c){for(const event of c.events)removeEventListener(event,c.move,true);c.dot.remove();delete globalThis.__faustusCursor;} })();`;

export class BrowserCapture {
  constructor(controller) { this.controller = controller; this.network = new Map(); this.video = new Map(); }
  async execute(context, args) {
    const {action} = args, tabId = context.tabId, c = this.controller;
    if (action === 'har_start') {
      if (this.network.has(tabId)) throw error('capture_active', 'HAR capture is already running on this tab.');
      this.network.set(tabId, {entries: [], requests: new Map(), pending: new Set(), bodies: args.include_bodies === true, started_at: new Date().toISOString()});
      try { await c.send(context, 'Network.enable'); } catch (e) { this.network.delete(tabId); throw e; }
      context.phase = 'action_confirmed'; return {tab_id: tabId, capturing: 'har'};
    }
    if (action === 'har_stop') {
      const capture = this.network.get(tabId); if (!capture) throw error('capture_not_started', 'Start HAR capture first.');
      await c.send(context, 'Network.disable'); await Promise.allSettled([...capture.pending]); this.network.delete(tabId);
      context.phase = 'action_confirmed';
      return {tab_id: tabId, download: this.har(capture)};
    }
    if (action === 'recording_start') {
      if (this.video.has(tabId)) throw error('capture_active', 'A recording is already running on this tab.');
      const capture = {frames: [], started_at: new Date().toISOString(), cursor: args.cursor === true, script: null, frozen: null};
      this.video.set(tabId, capture);
      try {
        if (capture.cursor) {
          const added = await c.send(context, 'Page.addScriptToEvaluateOnNewDocument', {source: CURSOR_SCRIPT}); capture.script = added.identifier;
          await c.send(context, 'Runtime.evaluate', {expression: CURSOR_SCRIPT});
        }
        await c.send(context, 'Page.startScreencast', {format: 'jpeg', quality: 75, maxWidth: 1280, maxHeight: 720, everyNthFrame: 1});
        // Hidden tabs may stop producing compositor frames. A targeted capture
        // keeps the recording moving without activating the person's window.
        capture.timer = setInterval(() => {
          if (this.video.get(tabId) !== capture || capture.pending || Date.now() / 1000 - (capture.frames.at(-1)?.timestamp ?? 0) < .4) return;
          capture.pending = c.chrome.debugger.sendCommand({tabId}, 'Page.captureScreenshot', {format: 'jpeg', quality: 75, captureBeyondViewport: false})
            .then(frame => { if (this.video.get(tabId) === capture) this.frame(tabId, {timestamp: Date.now() / 1000, data: frame.data}); })
            .catch(e => { capture.frame_error = String(e); }).finally(() => { capture.pending = null; });
        }, 500);
      } catch (e) { await this.stopVideo(tabId).catch(() => {}); throw e; }
      context.phase = 'action_confirmed'; return {tab_id: tabId, recording: true, cursor: capture.cursor, last_minute: true};
    }
    if (action === 'recording_stop' || action === 'last_minute') {
      const capture = this.video.get(tabId); if (!capture) throw error('capture_not_started', 'Start recording first; the last minute is buffered from then on.');
      let video;
      if (action === 'recording_stop') { await this.stopVideo(tabId); video = this.exportVideo(capture, false); }
      else video = this.exportVideo(capture, true);
      context.phase = 'action_confirmed'; return {tab_id: tabId, video};
    }
    throw error('invalid_arguments', 'Unknown capture action.');
  }
  exportVideo(capture, lastMinute) {
    const source = lastMinute && capture.frozen ? capture.frozen : capture.frames;
    if (!source.length) throw error('recording_empty', 'No video frame has arrived from this tab yet.');
    const end = lastMinute && capture.frozen ? source.at(-1).timestamp : Math.max(Date.now() / 1000, source.at(-1).timestamp);
    const start = lastMinute ? Math.max(source[0].timestamp, end - 59.8) : source[0].timestamp;
    const anchor = [...source].reverse().find(frame => frame.timestamp <= start) ?? source[0];
    const frames = [{...anchor, timestamp: start}, ...source.filter(frame => frame.timestamp > start && frame.timestamp < end)];
    if (end > start) frames.push({...source.at(-1), timestamp: end});
    return {mime: 'video/webm', started_at: capture.started_at, cursor: capture.cursor, last_minute: lastMinute, frames};
  }
  freeze(tabId) {
    const capture = this.video.get(tabId); if (!capture?.frames.length) return null;
    const video = this.exportVideo(capture, true); capture.frozen = video.frames;
    return video;
  }
  async stopVideo(tabId) {
    const capture = this.video.get(tabId); if (!capture) return;
    clearInterval(capture.timer); await capture.pending;
    this.video.delete(tabId);
    const send = (method, params = {}) => this.controller.chrome.debugger.sendCommand({tabId}, method, params);
    try { await send('Page.stopScreencast'); }
    finally {
      if (capture.script) await send('Page.removeScriptToEvaluateOnNewDocument', {identifier: capture.script}).catch(() => {});
      if (capture.cursor) await send('Runtime.evaluate', {expression: REMOVE_CURSOR}).catch(() => {});
    }
  }
  detached(tabId) { clearInterval(this.video.get(tabId)?.timer); this.network.delete(tabId); this.video.delete(tabId); }
  frame(tabId, frame) {
    const capture = this.video.get(tabId), last = capture?.frames.at(-1);
    if (capture && (!last || frame.timestamp - last.timestamp >= .2)) {
      capture.frames.push(frame); this.onFrame?.(tabId, frame);
    }
  }
  async cleanup(tabId) {
    await this.stopVideo(tabId).catch(() => {});
    if (this.network.delete(tabId)) await this.controller.chrome.debugger.sendCommand({tabId}, 'Network.disable').catch(() => {});
  }
  onEvent(tabId, method, p) {
    const video = this.video.get(tabId);
    if (method === 'Page.screencastFrame') {
      // Always ACK, including frames already in flight after stop.
      void this.controller.chrome.debugger.sendCommand({tabId}, 'Page.screencastFrameAck', {sessionId: p.sessionId}).catch(() => {});
      if (video && typeof p.data === 'string' && Number.isFinite(p.metadata?.timestamp)) {
        // Five frames per second are enough for a browser walkthrough.
        this.frame(tabId, {timestamp: p.metadata.timestamp, data: p.data});
      }
    }
    const capture = this.network.get(tabId); if (!capture || !method.startsWith('Network.')) return;
    if (method === 'Network.requestWillBeSent') {
      if (p.redirectResponse) this.response(capture.requests.get(p.requestId), p.redirectResponse, p.timestamp);
      const r = p.request, entry = {startedDateTime: new Date(p.wallTime * 1000).toISOString(), time: 0,
        request: {method: r.method, url: r.url, httpVersion: 'HTTP/1.1', headers: headers(r.headers), queryString: [...new URL(r.url).searchParams].map(([name, value]) => ({name, value})), cookies: [], headersSize: -1, bodySize: r.postData ? new TextEncoder().encode(r.postData).length : 0,
          ...(r.postData !== undefined ? {postData: {mimeType: r.headers?.['Content-Type'] ?? '', text: r.postData}} : {})},
        response: {status: 0, statusText: '', httpVersion: '', headers: [], cookies: [], content: {size: 0, mimeType: ''}, redirectURL: '', headersSize: -1, bodySize: -1},
        cache: {}, timings: {send: 0, wait: 0, receive: 0}, _start: p.timestamp};
      capture.entries.push(entry); capture.requests.set(p.requestId, entry);
    } else if (method === 'Network.responseReceived') this.response(capture.requests.get(p.requestId), p.response, p.timestamp);
    else if (method === 'Network.dataReceived') {
      const entry = capture.requests.get(p.requestId); if (entry) entry.response.content.size += p.dataLength ?? 0;
    }
    else if (method === 'Network.loadingFinished' || method === 'Network.loadingFailed') {
      const entry = capture.requests.get(p.requestId); if (!entry) return;
      entry.time = Math.max(0, (p.timestamp - entry._start) * 1000); entry.timings.receive = Math.max(0, entry.time - entry.timings.wait);
      if (method === 'Network.loadingFailed') entry._error = p.errorText;
      else {
        entry.response.bodySize = p.encodedDataLength;
        if (capture.bodies) {
          const pending = this.controller.chrome.debugger.sendCommand({tabId}, 'Network.getResponseBody', {requestId: p.requestId})
            .then(body => { entry.response.content.text = body.body; if (body.base64Encoded) entry.response.content.encoding = 'base64'; })
            .catch(e => { entry.response.content.comment = 'Body unavailable: ' + String(e); });
          capture.pending.add(pending); void pending.finally(() => capture.pending.delete(pending));
        }
      }
    }
  }
  response(entry, r, timestamp) {
    if (!entry) return;
    entry.time = Math.max(0, (timestamp - entry._start) * 1000); entry.timings.wait = entry.time;
    entry.response = {status: r.status, statusText: r.statusText ?? '', httpVersion: r.protocol ?? '', headers: headers(r.headers), cookies: [],
      content: {size: 0, mimeType: r.mimeType ?? ''}, redirectURL: r.headers?.location ?? r.headers?.Location ?? '', headersSize: -1, bodySize: r.encodedDataLength ?? -1};
    if (r.remoteIPAddress) entry.serverIPAddress = r.remoteIPAddress;
  }
  har(capture) {
    const log = {version: '1.2', creator: {name: 'Faustus', version: '1.3'}, pages: [], entries: capture.entries.map(({_start, ...entry}) => entry)};
    const bytes = new TextEncoder().encode(JSON.stringify({log})), parts = [];
    for (let i = 0; i < bytes.length; i += 65536) { let binary = ''; for (const byte of bytes.subarray(i, i + 65536)) binary += String.fromCharCode(byte); parts.push(btoa(binary)); }
    return {name: 'network.har', mime: 'application/json', bytes: bytes.length, parts};
  }
}
export const CAPTURE_ACTIONS = ['har_start', 'har_stop', 'recording_start', 'recording_stop', 'last_minute'];
