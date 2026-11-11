import {BrowserCommandError} from './controller.js';
export const RELAY_URL = 'ws://127.0.0.1:7016';
export const RETRY_DELAYS_MS = [1000, 2000, 5000, 10000];
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

/** No pending effects are saved or resent across a socket/worker lifetime. */
export class BrowserRelay {
  constructor({controller, extensionId, WebSocket, timers = globalThis, onState = () => {}}) {
    this.controller = controller; this.extensionId = extensionId; this.WebSocket = WebSocket; this.timers = timers; this.onState = onState;
    this.connection = null; this.cleanup = Promise.resolve(); this.retry = null; this.attempt = 0;
  }
  report(state, message = '') { this.onState({state, message}); }
  /** Reconnects quickly after a relay restart; the worker alarm stays as the slow fallback. */
  scheduleRetry() {
    if (this.retry !== null || typeof this.timers.setTimeout !== 'function') return;
    const delay = RETRY_DELAYS_MS[Math.min(this.attempt, RETRY_DELAYS_MS.length - 1)]; this.attempt += 1;
    this.retry = this.timers.setTimeout(() => { this.retry = null; this.connect(); }, delay);
  }
  connect() {
    if (this.retry !== null) { this.timers.clearTimeout?.(this.retry); this.retry = null; }
    if (this.connection) return;
    let socket; try { socket = new this.WebSocket(RELAY_URL); } catch (error) { this.report('disconnected', String(error)); this.scheduleRetry(); return; }
    const connection = {socket, ready: false, readyStarted: false, seen: new Set(), incoming: Promise.resolve(), heartbeat: null}; this.connection = connection; this.report('connecting');
    socket.onopen = () => {
      if (this.connection !== connection) return;
      try { socket.send(JSON.stringify({type: 'hello', protocol: 1, extensionId: this.extensionId})); } catch (error) { this.lose(connection, String(error)); return; }
      connection.heartbeat = this.timers.setInterval(() => {
        if (this.connection !== connection) return;
        try { socket.send(JSON.stringify({type: 'ping'})); } catch (error) { this.lose(connection, String(error)); }
      }, 20000);
    };
    socket.onmessage = event => {
      connection.incoming = connection.incoming.then(() => this.message(connection, event.data)).catch(error => this.lose(connection, String(error)));
    };
    socket.onerror = () => this.lose(connection, 'WebSocket unavailable; reconnect never replays actions.');
    socket.onclose = event => this.lose(connection, event.reason || 'Relay disconnected; pending actions are not replayed.');
  }
  lose(connection, message) {
    if (this.connection !== connection) return;
    this.connection = null; connection.ready = false;
    if (this.controller.capture) this.controller.capture.onFrame = null;
    if (connection.heartbeat !== null) this.timers.clearInterval(connection.heartbeat);
    const previous = this.cleanup; this.cleanup = Promise.allSettled([previous, this.controller.disconnect()]).then(() => {});
    try { connection.socket.close(); } catch { /* The transport may already be closed. */ }
    this.report('disconnected', message); this.scheduleRetry();
  }
  async message(connection, raw) {
    if (this.connection !== connection) return;
    const message = JSON.parse(raw);
    if (message.type === 'pong') return;
    if (message.type === 'ping') { connection.socket.send(JSON.stringify({type: 'pong'})); return; }
    if (message.type === 'ready') {
      if (connection.readyStarted || !uuid(message.connection_id)) throw new BrowserCommandError('invalid_protocol', 'Invalid or repeated ready handshake.');
      connection.readyStarted = true; await this.cleanup; if (this.connection !== connection) return;
      this.controller.openSession(message.connection_id); connection.ready = true; this.attempt = 0;
      if (this.controller.capture) this.controller.capture.onFrame = (tabId, frame) => {
        if (this.connection !== connection || !connection.ready) return;
        try { connection.socket.send(JSON.stringify({type: 'recording_frame', tab_id: tabId, frame})); }
        catch (error) { this.lose(connection, String(error)); }
      };
      this.report('ready'); return;
    }
    if (message.type === 'error') throw new BrowserCommandError(message.error?.code ?? 'relay_error', message.error?.message ?? message.message ?? 'Relay refused the connection.');
    if (message.type !== 'command' || !uuid(message.id)) throw new BrowserCommandError('invalid_protocol', 'Expected command with UUID id.');
    let reply;
    if (!connection.ready) reply = {ok: false, error: {code: 'connection_not_ready', message: 'Relay handshake is incomplete.'}, phase: 'not_dispatched'};
    else if (connection.seen.has(message.id)) reply = {ok: false, error: {code: 'duplicate_command', message: 'This call id was already received; it will not be replayed.'}, phase: 'outcome_unknown'};
    else { connection.seen.add(message.id); reply = await this.controller.run(message.arguments); }
    if (this.connection === connection) connection.socket.send(JSON.stringify({type: 'result', id: message.id, ...reply}));
  }
}
