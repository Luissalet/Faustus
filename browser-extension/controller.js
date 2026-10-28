/** Targeted CDP commands. Never activates a tab or runs page JavaScript. */
export class BrowserCommandError extends Error {
  constructor(code, message) { super(message); this.name = 'BrowserCommandError'; this.code = code; }
}
const fail = (code, message) => { throw new BrowserCommandError(code, message); };
const equalDocument = (a, b) => a.frame_id === b.frame_id && a.loader_id === b.loader_id && a.url === b.url && a.backend_id === b.backend_id;
const tabInfo = tab => ({id: tab.id, url: tab.url ?? tab.pendingUrl ?? '', title: tab.title ?? '', active: !!tab.active, window_id: tab.windowId});
const valueOf = value => value?.value ?? (value?.relatedNodes ? value.relatedNodes.map(node => ({text: node.text ?? '', idref: node.idref ?? ''})) : null);
const textOf = value => { const result = valueOf(value); return result == null ? '' : typeof result === 'string' ? result : JSON.stringify(result); };

// Convex CDP content quads clipped to the target's CSS viewport, never screen coordinates.
function visiblePoint(quads, width, height) {
  for (const quad of quads ?? []) {
    if (!Array.isArray(quad) || quad.length !== 8 || !quad.every(Number.isFinite)) continue;
    let polygon = Array.from({length: 4}, (_, i) => ({x: quad[i * 2], y: quad[i * 2 + 1]}));
    for (const [axis, bound, greater] of [['x', 0, true], ['x', width, false], ['y', 0, true], ['y', height, false]]) {
      const next = [], inside = point => greater ? point[axis] >= bound : point[axis] <= bound;
      for (let i = 0; i < polygon.length; i++) {
        const a = polygon[i], b = polygon[(i + 1) % polygon.length], ai = inside(a), bi = inside(b);
        if (ai) next.push(a);
        if (ai !== bi) { const t = (bound - a[axis]) / (b[axis] - a[axis]); next.push({x: a.x + t * (b.x - a.x), y: a.y + t * (b.y - a.y)}); }
      }
      polygon = next;
    }
    const area = Math.abs(polygon.reduce((sum, point, i) => { const next = polygon[(i + 1) % polygon.length]; return sum + point.x * next.y - point.y * next.x; }, 0)) / 2;
    if (polygon.length >= 3 && area > 0) return {x: polygon.reduce((sum, p) => sum + p.x, 0) / polygon.length, y: polygon.reduce((sum, p) => sum + p.y, 0) / polygon.length};
  }
  fail('node_not_visible', 'The referenced DOM node has no visible content quad in this tab.');
}

function subtreeBackendIds(root) {
  const ids = new Set(), pending = [root];
  while (pending.length) {
    const node = pending.pop(); if (!node || typeof node !== 'object') continue;
    if (Number.isInteger(node.backendNodeId)) ids.add(node.backendNodeId);
    for (const name of ['children', 'shadowRoots', 'pseudoElements', 'distributedNodes']) if (Array.isArray(node[name])) pending.push(...node[name]);
    for (const name of ['contentDocument', 'templateContent', 'importedDocument']) if (node[name]) pending.push(node[name]);
  }
  return ids;
}

function keyboard(key) {
  if (typeof key !== 'string' || !key) fail('invalid_arguments', 'press requires a key, for example Enter or Ctrl+A.');
  const parts = key.split('+'), raw = parts.pop(), modifiers = parts.reduce((mask, part) => {
    const bit = {alt: 1, ctrl: 2, control: 2, meta: 4, cmd: 4, command: 4, shift: 8}[part.toLowerCase()];
    if (!bit) fail('unsupported_key', 'Unknown key modifier: ' + part); return mask | bit;
  }, 0);
  const named = {Enter: [13, 'Enter', '\r'], Return: [13, 'Enter', '\r'], Escape: [27, 'Escape'], Esc: [27, 'Escape'], Tab: [9, 'Tab'], Backspace: [8, 'Backspace'], Delete: [46, 'Delete'], Insert: [45, 'Insert'], ArrowLeft: [37, 'ArrowLeft'], ArrowUp: [38, 'ArrowUp'], ArrowRight: [39, 'ArrowRight'], ArrowDown: [40, 'ArrowDown'], Home: [36, 'Home'], End: [35, 'End'], PageUp: [33, 'PageUp'], PageDown: [34, 'PageDown'], Space: [32, 'Space', ' '], ' ': [32, 'Space', ' ']};
  let spec = named[raw], actual = raw;
  if (/^F(?:[1-9]|1[0-2])$/.test(raw)) spec = [111 + Number(raw.slice(1)), raw];
  if (/^[a-z]$/i.test(raw)) { actual = modifiers & 8 ? raw.toUpperCase() : raw; spec = [raw.toUpperCase().charCodeAt(0), 'Key' + raw.toUpperCase(), actual]; }
  if (/^[0-9]$/.test(raw)) spec = [raw.charCodeAt(0), 'Digit' + raw, raw];
  if (!spec) fail('unsupported_key', 'Unsupported key: ' + raw + '. Use type for arbitrary Unicode text.');
  if (raw === 'Return') actual = 'Enter'; if (raw === 'Esc') actual = 'Escape'; if (spec[1] === 'Space') actual = ' ';
  return {key: actual, code: spec[1], windowsVirtualKeyCode: spec[0], nativeVirtualKeyCode: spec[0], modifiers, ...(!(modifiers & 7) && spec[2] ? {text: spec[2], unmodifiedText: spec[2]} : {})};
}

export class BrowserController {
  constructor(chrome) { this.chrome = chrome; this.tabs = new Map(); this.owned = new Set(); this.session = null; }

  state(tabId) {
    if (!this.tabs.has(tabId)) this.tabs.set(tabId, {generation: 0, lifetime: 0, refs: new Map(), document: null, queue: Promise.resolve(), attaching: null, detaching: null, detached: new AbortController()});
    return this.tabs.get(tabId);
  }
  invalidate(tabId) { const state = this.state(tabId); state.generation++; state.refs.clear(); state.document = null; }
  openSession(id) {
    if (this.session) fail('session_already_open', 'Disconnect the previous relay session before opening another.');
    const cancellation = new AbortController(); this.session = {id, signal: cancellation.signal, cancel: () => cancellation.abort()};
    for (const tabId of this.tabs.keys()) this.invalidate(tabId);
  }
  async disconnect() {
    const previous = this.session; this.session = null; previous?.cancel();
    for (const tabId of this.tabs.keys()) this.invalidate(tabId);
    const attaching = [...this.tabs.values()].map(state => state.attaching).filter(Boolean);
    await Promise.allSettled([...this.owned].map(tabId => this.detachOwned(tabId)));
    await Promise.allSettled(attaching);
    await Promise.allSettled([...this.owned].map(tabId => this.detachOwned(tabId)));
  }
  onDetach(source) {
    if (!Number.isInteger(source.tabId)) return;
    const state = this.state(source.tabId); this.owned.delete(source.tabId); state.lifetime++; state.detached.abort(); state.detached = new AbortController(); this.invalidate(source.tabId);
  }
  onEvent(source, method, params = {}) {
    if (source.sessionId || !Number.isInteger(source.tabId)) return;
    if (method === 'DOM.documentUpdated' || method === 'Page.frameNavigated' && !params.frame?.parentId || method === 'Page.navigatedWithinDocument' && params.frameId === this.state(source.tabId).document?.frame_id) this.invalidate(source.tabId);
  }
  onTabUpdated(tabId, change) { if (change.url !== undefined || change.status === 'loading') this.invalidate(tabId); }
  onTabRemoved(tabId) { this.onDetach({tabId}); }

  context(session, tabId, lifetime) {
    const state = this.state(tabId), detached = state.detached.signal;
    const sameSession = () => { if (this.session !== session) fail('connection_lost', 'Relay connection changed; this command will not be replayed.'); };
    const current = () => { sameSession(); if (state.lifetime !== lifetime) fail('debugger_detached', 'The debugger detached; this command will not be replayed.'); };
    return {session, tabId, state, phase: 'not_dispatched', current, wait: async (promise, expectedDetach = false) => {
      const validate = expectedDetach ? sameSession : current; validate(); let lost, gone;
      const cancelled = new Promise((_, reject) => {
        lost = () => reject(new BrowserCommandError('connection_lost', 'Relay disconnected while awaiting the browser; no automatic replay.'));
        gone = () => reject(new BrowserCommandError('debugger_detached', 'Debugger detached while awaiting the browser; no automatic replay.'));
        session.signal.addEventListener('abort', lost, {once: true}); if (!expectedDetach) detached.addEventListener('abort', gone, {once: true});
      });
      try { const result = await Promise.race([promise, cancelled]); validate(); return result; }
      finally { session.signal.removeEventListener('abort', lost); if (!expectedDetach) detached.removeEventListener('abort', gone); }
    }};
  }
  async attach(context) {
    context.current(); const {tabId, state} = context;
    if (this.owned.has(tabId)) return;
    if (state.detaching) await context.wait(state.detaching);
    if (!state.attaching) {
      const lifetime = state.lifetime;
      state.attaching = (async () => {
        try { await this.chrome.debugger.attach({tabId}, '1.3'); }
        catch (error) { throw new BrowserCommandError('debugger_unavailable', 'Cannot attach to tab ' + tabId + '; another debugger or browser restriction may own it. ' + String(error)); }
        if (state.lifetime !== lifetime) fail('debugger_detached', 'Debugger detached during attachment.');
        this.owned.add(tabId);
        if (this.session !== context.session) { await this.detachOwned(tabId); fail('connection_lost', 'Connection ended during debugger attachment; the owned attachment was released.'); }
      })();
      state.attaching.finally(() => { state.attaching = null; }).catch(() => {});
    }
    await context.wait(state.attaching);
    await this.send(context, 'Page.enable'); await this.send(context, 'DOM.enable'); await this.send(context, 'Accessibility.enable');
  }
  async detachOwned(tabId) {
    const state = this.state(tabId);
    if (state.detaching) return state.detaching;
    if (!this.owned.has(tabId)) return false;
    state.detaching = (async () => {
      try { await this.chrome.debugger.detach({tabId}); this.owned.delete(tabId); return true; }
      finally { this.invalidate(tabId); }
    })();
    try { return await state.detaching; } finally { state.detaching = null; }
  }
  async send(context, method, params = {}) { context.current(); return context.wait(this.chrome.debugger.sendCommand({tabId: context.tabId}, method, params)); }
  async metadata(context) { context.current(); const tab = await context.wait(this.chrome.tabs.get(context.tabId)); if (tab.id !== context.tabId) fail('target_changed', 'Browser returned a different tab.'); return tabInfo(tab); }
  async document(context) {
    const tab = await this.metadata(context), tree = await this.send(context, 'Page.getFrameTree'), dom = await this.send(context, 'DOM.getDocument', {depth: 0});
    const frame = tree?.frameTree?.frame, backend = dom?.root?.backendNodeId;
    if (!frame?.id || typeof frame.url !== 'string' || !Number.isInteger(backend)) fail('document_unavailable', 'The main-frame document is not readable yet.');
    const url = frame.url + (!frame.url.includes('#') ? frame.urlFragment ?? '' : '');
    const after = await this.metadata(context);
    if (tab.url !== after.url || after.url !== url) fail('document_changed', 'Tab URL and main-frame document changed during observation; take a new snapshot.');
    return {tab: after, stamp: {frame_id: frame.id, loader_id: frame.loaderId ?? '', url, backend_id: backend}};
  }
  async snapshot(context) {
    this.invalidate(context.tabId); const generation = context.state.generation, before = await this.document(context);
    const tree = await this.send(context, 'Accessibility.getFullAXTree', {frameId: before.stamp.frame_id}), after = await this.document(context);
    if (generation !== context.state.generation || !equalDocument(before.stamp, after.stamp)) { this.invalidate(context.tabId); fail('document_changed', 'Document changed during AX snapshot; old references were invalidated.'); }
    const returned = tree?.nodes; if (!Array.isArray(returned)) fail('snapshot_unavailable', 'Accessibility did not return a node tree.');
    const byId = new Map(returned.map(node => [node.nodeId, node])), excluded = new Set();
    const exclude = node => { if (!node || excluded.has(node.nodeId)) return; excluded.add(node.nodeId); for (const id of node.childIds ?? []) exclude(byId.get(id)); };
    for (const node of returned) if (node.frameId && node.frameId !== before.stamp.frame_id) exclude(node);
    const nodes = returned.filter(node => !excluded.has(node.nodeId));
    const refs = new Map(), axRefs = new Map();
    for (const [index, node] of nodes.entries()) if (Number.isInteger(node.backendDOMNodeId) && (!node.frameId || node.frameId === before.stamp.frame_id)) {
      const ref = 'r:' + context.session.id + ':' + context.tabId + ':' + generation + ':' + index;
      refs.set(ref, {backendId: node.backendDOMNodeId, generation, document: after.stamp}); axRefs.set(node.nodeId, ref);
    }
    const result = {tab: after.tab, generation, document: {frame_id: after.stamp.frame_id, loader_id: after.stamp.loader_id, url: after.stamp.url}, scope: 'main_frame', nodes: nodes.map(node => ({ref: axRefs.get(node.nodeId) ?? null, role: textOf(node.role), name: textOf(node.name), value: textOf(node.value), properties: Object.fromEntries((node.properties ?? []).map(property => [property.name, valueOf(property.value)])), ignored: !!node.ignored, child_refs: (node.childIds ?? []).map(id => axRefs.get(id)).filter(Boolean)}))};
    context.current(); context.state.refs = refs; context.state.document = after.stamp; return result;
  }
  async reference(context, ref) {
    if (typeof ref !== 'string' || !ref) fail('invalid_arguments', 'This action requires a ref from the latest snapshot of the explicit tab.');
    const saved = context.state.refs.get(ref), generation = context.state.generation;
    if (!saved || saved.generation !== generation) fail('stale_ref', 'Reference is not from the latest snapshot of this tab and connection.');
    const current = await this.document(context);
    if (generation !== context.state.generation || !equalDocument(current.stamp, saved.document)) { this.invalidate(context.tabId); fail('stale_ref', 'The referenced document changed; take a fresh snapshot.'); }
    await this.send(context, 'DOM.describeNode', {backendNodeId: saved.backendId, depth: 0});
    if (generation !== context.state.generation) fail('stale_ref', 'Document changed while resolving the node.');
    return {saved, generation};
  }
  async verifyReference(context, resolved) {
    const current = await this.document(context);
    if (resolved.generation !== context.state.generation || !equalDocument(current.stamp, resolved.saved.document)) { this.invalidate(context.tabId); fail('stale_ref', 'Document changed before input dispatch; take a fresh snapshot.'); }
  }

  async execute(context, args) {
    const {action} = args;
    if (action === 'release') {
      const tab = await this.metadata(context); this.invalidate(context.tabId); context.phase = 'outcome_unknown';
      // An expected onDetach invalidates lifetime while this release is being acknowledged.
      const released = await context.wait(this.detachOwned(context.tabId), true); context.phase = 'action_confirmed'; return {tab, released};
    }
    await this.attach(context);
    if (action === 'snapshot') return this.snapshot(context);
    if (action === 'screenshot') {
      const before = await this.document(context), image = await this.send(context, 'Page.captureScreenshot', {format: 'png', fromSurface: true}), after = await this.document(context);
      if (!equalDocument(before.stamp, after.stamp)) fail('document_changed', 'Document changed during the targeted screenshot.');
      if (typeof image?.data !== 'string' || !image.data) fail('screenshot_unavailable', 'Page.captureScreenshot returned no PNG.'); return {tab: after.tab, image: {data: image.data, mimeType: 'image/png'}};
    }
    let resolved;
    if (action === 'click' || action === 'type' || action === 'press' && args.ref !== undefined) resolved = await this.reference(context, args.ref);
    if (action === 'click') {
      await this.send(context, 'DOM.scrollIntoViewIfNeeded', {backendNodeId: resolved.saved.backendId});
      const geometry = await this.send(context, 'DOM.getContentQuads', {backendNodeId: resolved.saved.backendId}), layout = await this.send(context, 'Page.getLayoutMetrics'), viewport = layout.cssVisualViewport ?? layout.visualViewport;
      if (!viewport || !(viewport.clientWidth > 0 && viewport.clientHeight > 0)) fail('viewport_unavailable', 'The target tab has no CSS viewport.');
      const visible = visiblePoint(geometry.quads, viewport.clientWidth, viewport.clientHeight);
      const point = {x: Math.max(0, Math.min(Math.ceil(viewport.clientWidth) - 1, Math.round(visible.x))), y: Math.max(0, Math.min(Math.ceil(viewport.clientHeight) - 1, Math.round(visible.y)))};
      const target = await this.send(context, 'DOM.describeNode', {backendNodeId: resolved.saved.backendId, depth: -1, pierce: true});
      if (target?.node?.backendNodeId !== resolved.saved.backendId) fail('stale_ref', 'The referenced DOM target is no longer available; take a fresh snapshot.');
      const descendants = subtreeBackendIds(target.node); await this.verifyReference(context, resolved);
      const hit = await this.send(context, 'DOM.getNodeForLocation', {...point, includeUserAgentShadowDOM: true});
      if (resolved.generation !== context.state.generation) fail('stale_ref', 'Document changed during click hit-test; take a fresh snapshot.');
      if (hit?.frameId !== resolved.saved.document.frame_id || !descendants.has(hit?.backendNodeId)) fail('target_obscured', 'The click point is covered by another target or frame, or has no DOM hit. Observe the page and take a fresh snapshot; no mouse input was dispatched.');
      context.phase = 'outcome_unknown'; await this.send(context, 'Input.dispatchMouseEvent', {type: 'mousePressed', ...point, button: 'left', buttons: 1, clickCount: 1});
      await this.send(context, 'Input.dispatchMouseEvent', {type: 'mouseReleased', ...point, button: 'left', buttons: 0, clickCount: 1});
    } else if (action === 'type') {
      if (typeof args.text !== 'string') fail('invalid_arguments', 'type requires text; it inserts Unicode at the focused selection without replacing the field.');
      await this.send(context, 'DOM.focus', {backendNodeId: resolved.saved.backendId}); await this.verifyReference(context, resolved);
      context.phase = 'outcome_unknown'; await this.send(context, 'Input.insertText', {text: args.text});
    } else if (action === 'press') {
      const key = keyboard(args.key);
      if (resolved) { await this.send(context, 'DOM.focus', {backendNodeId: resolved.saved.backendId}); await this.verifyReference(context, resolved); }
      else await this.document(context);
      context.phase = 'outcome_unknown'; await this.send(context, 'Input.dispatchKeyEvent', {type: 'keyDown', ...key});
      const {text: _text, unmodifiedText: _unmodified, ...up} = key; await this.send(context, 'Input.dispatchKeyEvent', {type: 'keyUp', ...up});
    } else if (action === 'navigate') {
      let url; try { url = new URL(args.url); } catch { fail('invalid_arguments', 'navigate requires an absolute http or https URL.'); }
      if (!['http:', 'https:'].includes(url.protocol)) fail('invalid_arguments', 'navigate only accepts http or https URLs.');
      await this.document(context); this.invalidate(context.tabId); context.phase = 'outcome_unknown'; const ack = await this.send(context, 'Page.navigate', {url: url.href}); context.phase = 'action_confirmed';
      if (ack?.errorText) fail('navigation_failed', 'Page.navigate acknowledged failure: ' + ack.errorText);
    } else fail('invalid_arguments', 'Unknown browser action.');
    context.phase = 'action_confirmed';
    let snapshot; try { snapshot = await this.snapshot(context); } catch (error) { throw new BrowserCommandError('readback_failed', 'Browser acknowledged ' + action + ', but AX readback failed: ' + String(error)); }
    context.phase = 'readback_complete'; return {tab: snapshot.tab, action, snapshot};
  }

  async run(args) {
    const session = this.session;
    if (!session) return {ok: false, error: {code: 'connection_lost', message: 'No ready relay connection.'}, phase: 'not_dispatched'};
    if (!args || typeof args !== 'object' || !['tabs', 'snapshot', 'click', 'type', 'press', 'navigate', 'screenshot', 'release'].includes(args.action)) return {ok: false, error: {code: 'invalid_arguments', message: 'Unknown browser action.'}, phase: 'not_dispatched'};
    if (args.action === 'tabs') {
      try { const tabs = await this.chrome.tabs.query({}); if (this.session !== session) fail('connection_lost', 'Connection changed during tabs query.'); return {ok: true, result: {tabs: tabs.map(tabInfo)}, phase: 'readback_complete'}; }
      catch (error) { return {ok: false, error: {code: error.code ?? 'browser_command_failed', message: String(error.message ?? error)}, phase: 'not_dispatched'}; }
    }
    if (!Number.isInteger(args.tab_id) || args.tab_id < 0) return {ok: false, error: {code: 'invalid_arguments', message: 'An explicit integer tab_id is required.'}, phase: 'not_dispatched'};
    const state = this.state(args.tab_id), lifetime = state.lifetime;
    const task = state.queue.then(async () => {
      const context = this.context(session, args.tab_id, lifetime);
      try { context.current(); const result = await this.execute(context, args); return {ok: true, result, phase: context.phase === 'not_dispatched' ? 'readback_complete' : context.phase}; }
      catch (error) { return {ok: false, error: {code: error.code ?? 'browser_command_failed', message: String(error.message ?? error)}, phase: context.phase}; }
    });
    state.queue = task.catch(() => {}); return task;
  }
}
