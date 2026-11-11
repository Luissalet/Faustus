/** Durable names and explicit human turns. No cookies, credentials or commands are stored. */
const error = (code, message) => Object.assign(new Error(message), {code});
const webURL = url => /^https?:\/\//i.test(url ?? '');
const saveSession = (state, name, value) => Object.defineProperty(state.sessions, name, {value, enumerable: true, configurable: true, writable: true});
export class JourneyStore {
  async database() {
    if (!this.db) this.db = new Promise((resolve, reject) => {
      const request = indexedDB.open('faustus-browser-sessions', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('state');
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    return this.db;
  }
  async read() {
    const db = await this.database();
    return new Promise((resolve, reject) => {
      const request = db.transaction('state').objectStore('state').get('journeys');
      request.onsuccess = () => resolve(request.result ?? {sessions: {}, turns: {}}); request.onerror = () => reject(request.error);
    });
  }
  async write(value) {
    const db = await this.database();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('state', 'readwrite'); tx.objectStore('state').put(value, 'journeys');
      tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
    });
  }
}

export class BrowserJourneys {
  constructor(controller, store = new JourneyStore()) { this.controller = controller; this.store = store; this.queue = Promise.resolve(); }
  transact(fn) {
    const task = this.queue.then(async () => { const state = await this.store.read(); const result = await fn(state); await this.store.write(state); return result; });
    this.queue = task.catch(() => {}); return task;
  }
  name(value) {
    if (typeof value !== 'string' || !value.trim()) throw error('invalid_arguments', 'A nonempty session name is required.');
    return value.trim();
  }
  async execute(args) {
    const c = this.controller;
    return this.transact(async state => {
      const {action, tab_id: tabId} = args;
      if (action === 'sessions') return {sessions: Object.entries(state.sessions).map(([name, value]) => ({name, ...value}))};
      if (action === 'session_forget') { const name = this.name(args.name); const forgotten = Object.hasOwn(state.sessions, name); delete state.sessions[name]; return {name, forgotten}; }
      if (action === 'session_open') {
        const name = this.name(args.name), saved = Object.hasOwn(state.sessions, name) ? state.sessions[name] : null;
        if (!saved) throw error('session_not_found', 'No browser session named ' + name);
        let tab; try { tab = await c.chrome.tabs.get(saved.tab_id); } catch { /* The browser may have restarted. */ }
        let restored = false;
        if (!tab || tab.url !== saved.url) { tab = await c.chrome.tabs.create({url: saved.url, active: false}); c.created.add(tab.id); restored = true; }
        saveSession(state, name, {...saved, tab_id: tab.id});
        return {name, tab: {id: tab.id, url: tab.pendingUrl || tab.url || saved.url}, restored, note: 'Uses this browser profile; no cookie or profile copy. Take a fresh snapshot once loading finishes.'};
      }
      const tab = await c.chrome.tabs.get(tabId);
      if (action === 'handoff_status') return {tab_id: tabId, turn: state.turns[tabId] ?? null};
      if (action === 'session_save') {
        const name = this.name(args.name); if (!webURL(tab.url)) throw error('unsupported_tab', 'Only http(s) sessions can be saved.');
        saveSession(state, name, {tab_id: tabId, url: tab.url, title: tab.title ?? '', saved_at: new Date().toISOString()});
        return {name, ...state.sessions[name]};
      }
      if (action === 'handoff' || action === 'adopt_tab') {
        if (!webURL(tab.url)) throw error('unsupported_tab', 'Choose an http(s) tab.');
        const turn = {id: crypto.randomUUID(), kind: action === 'handoff' ? 'handoff' : 'adoption', status: 'waiting',
          name: args.name ? this.name(args.name) : '', reason: args.reason ?? '', url: tab.url, title: tab.title ?? '', requested_at: new Date().toISOString()};
        state.turns[tabId] = turn; c.invalidate(tabId);
        void c.chrome.action?.openPopup?.().catch(() => {});
        return {tab_id: tabId, turn, note: 'Open Faustus Browser in the toolbar. Only its real Continue / Cede tab button completes this turn.'};
      }
      throw error('invalid_arguments', 'Unknown session action.');
    });
  }
  async waiting(tabId) { await this.queue; return (await this.store.read()).turns[tabId]?.status === 'waiting'; }
  async tabTurns() { await this.queue; return (await this.store.read()).turns; }
  updated(tabId, change) {
    if (!change.url && !change.title) return Promise.resolve();
    return this.transact(async state => {
      for (const saved of Object.values(state.sessions)) if (saved.tab_id === tabId) {
        if (webURL(change.url)) saved.url = change.url;
        if (change.title !== undefined) saved.title = change.title;
      }
    });
  }
  removed(tabId) {
    return this.transact(async state => {
      for (const saved of Object.values(state.sessions)) if (saved.tab_id === tabId) saved.tab_id = null;
      delete state.turns[tabId];
    });
  }
  async panel() {
    await this.queue; const state = await this.store.read(), tabs = await this.controller.chrome.tabs.query({});
    return {sessions: Object.entries(state.sessions).map(([name, value]) => ({name, ...value})),
      turns: Object.entries(state.turns).filter(([, turn]) => turn.status === 'waiting').map(([tab_id, turn]) => ({tab_id: Number(tab_id), ...turn})),
      tabs: tabs.filter(tab => webURL(tab.url)).map(tab => ({id: tab.id, title: tab.title, url: tab.url, active: tab.active}))};
  }
  // Invoked only from the extension's own popup, never through browser_session or page messages.
  async person({tab_id: tabId, turn_id: turnId, name}) {
    return this.transact(async state => {
      const turn = state.turns[tabId];
      if (turnId && (!turn || turn.id !== turnId || turn.status !== 'waiting')) throw error('turn_changed', 'This card is no longer current. Refresh it.');
      const tab = await this.controller.chrome.tabs.get(tabId);
      if (!webURL(tab.url)) throw error('unsupported_tab', 'Choose an http(s) tab.');
      const chosen = name?.trim() || turn?.name;
      if (chosen) saveSession(state, this.name(chosen), {tab_id: tabId, url: tab.url, title: tab.title ?? '', saved_at: new Date().toISOString(), adopted: true});
      state.turns[tabId] = {...turn, kind: turn?.kind ?? 'adoption', status: 'continued', continued_at: new Date().toISOString(), url: tab.url};
      this.controller.invalidate(tabId);
      return {tab_id: tabId, status: 'continued'};
    });
  }
}

export const JOURNEY_ACTIONS = ['sessions', 'session_save', 'session_open', 'session_forget', 'adopt_tab', 'handoff', 'handoff_status'];
