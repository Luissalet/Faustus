// static/js/mentionChips.js
// Turns the "@path" tokens in a sent message into clickable chips that open
// the file in the viewer panel.
//
// It closes the loop on the "@" picker: after sending, you can see at a glance
// which files the turn actually pointed at, and check one without leaving the
// chat. Purely cosmetic — the server resolves the mentions from the message
// text either way (src/file_mentions.py).

// Mirrors MENTION_RE in src/file_mentions.py. Kept in sync by
// tests/test_mention_chips_js.py.
const MENTION_RE = /(?<![\w@/\\.-])@(?:"([^"\n]{1,300})"|([A-Za-z0-9_.][\w./\\-]{0,299}))/g;
const SKIP_ANCESTORS = 'code, pre, a, .harness-node, .file-viewer-panel, .msg-actions';

/** The path a mention token refers to, trailing sentence punctuation removed. */
export function mentionPath(quoted, bare) {
  let raw = String(quoted || bare || '').trim();
  while (raw && '.,;:!?'.includes(raw.slice(-1))) raw = raw.slice(0, -1);
  return raw;
}

/**
 * Split a text node's content into plain strings and mention parts.
 * Exported for tests — the DOM walk below is a thin wrapper on it.
 */
export function splitMentions(text) {
  const out = [];
  let last = 0;
  const re = new RegExp(MENTION_RE.source, 'g');
  let m;
  while ((m = re.exec(text)) !== null) {
    const path = mentionPath(m[1], m[2]);
    if (!path) continue;
    // The token as written, minus any punctuation the path dropped.
    const token = m[0].slice(0, m[0].length - ((m[1] || m[2] || '').length - path.length));
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    out.push({ mention: path, token });
    last = m.index + token.length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}

// The bound workspace, memoized. `decorate()` asks for it once per message and
// the sweep runs on every streaming tick, so an uncached localStorage read +
// JSON.parse here is a synchronous cost paid hundreds of times a second — and
// most of all with no workspace bound, the default, where nothing is decorated
// and the answer is therefore never remembered.
let _wsCache = null;    // null = not read yet ('' is a valid "none bound")

function _readWorkspace() {
  try {
    if (window.workspaceModule && window.workspaceModule.getWorkspace) {
      const w = window.workspaceModule.getWorkspace();
      if (w) return typeof w === 'string' ? w : (w.path || '');
    }
    const raw = localStorage.getItem('odysseus-workspace');
    if (!raw) return '';
    try { const v = JSON.parse(raw); return typeof v === 'string' ? v : (v && v.path) || ''; }
    catch (_) { return raw; }
  } catch (_) { return ''; }
}

function _workspace() {
  if (_wsCache === null) _wsCache = _readWorkspace();
  return _wsCache;
}

/** Forget the memoized workspace (workspace.js fires odysseus:workspace-change). */
export function invalidateWorkspace() { _wsCache = null; }

/** Decorate the mentions inside one rendered message body. Idempotent. */
export function decorate(bodyEl) {
  if (!bodyEl || bodyEl.dataset.mentionChips === '1') return 0;
  if (!_workspace()) return 0;           // nothing to open them against
  bodyEl.dataset.mentionChips = '1';
  const walker = document.createTreeWalker(bodyEl, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || node.nodeValue.indexOf('@') < 0) return NodeFilter.FILTER_REJECT;
      const parent = node.parentElement;
      if (!parent || parent.closest(SKIP_ANCESTORS)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  let n;
  while ((n = walker.nextNode())) targets.push(n);
  let made = 0;
  for (const node of targets) {
    const parts = splitMentions(node.nodeValue);
    if (parts.length < 2) continue;
    const frag = document.createDocumentFragment();
    for (const part of parts) {
      if (part.text != null) { frag.appendChild(document.createTextNode(part.text)); continue; }
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'mention-chip';
      chip.textContent = part.token;
      chip.title = 'Open ' + part.mention;
      chip.dataset.mentionPath = part.mention;
      frag.appendChild(chip);
      made++;
    }
    node.parentNode.replaceChild(frag, node);
  }
  return made;
}

// Kept so the observer can actually be stopped — the anonymous one it replaced
// outlived every teardown.
let _observer = null;

export function stopMentionChips() {
  if (_observer) { try { _observer.disconnect(); } catch (_) {} _observer = null; }
}

export function initMentionChips(root) {
  const box = root || document.getElementById('chat-history');
  if (!box || box._mentionChipsWired) return;
  box._mentionChipsWired = true;

  let sweepPending = false;
  const sweep = () => {
    sweepPending = false;
    // With no workspace bound (the default) no chip can be made, so skip the
    // whole-transcript querySelectorAll instead of re-deciding per message.
    if (!_workspace()) return;
    box.querySelectorAll('.msg-user .body').forEach(b => { try { decorate(b); } catch (_) {} });
  };
  // The streaming renderer rewrites innerHTML several times a second and each
  // rewrite is a mutation burst; coalesce them into one sweep per frame.
  const scheduleSweep = () => {
    if (sweepPending) return;
    sweepPending = true;
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(sweep);
    else setTimeout(sweep, 16);
  };
  sweep();
  // Messages arrive from streaming, history loads and session switches; one
  // observer covers all three without hooking each render path.
  stopMentionChips();
  try {
    _observer = new MutationObserver(scheduleSweep);
    _observer.observe(box, { childList: true, subtree: true });
  } catch (_) { _observer = null; }
  // Binding a workspace makes previously-skipped messages decoratable.
  document.addEventListener('odysseus:workspace-change', () => {
    invalidateWorkspace();
    scheduleSweep();
  });

  box.addEventListener('click', (ev) => {
    const chip = ev.target && ev.target.closest ? ev.target.closest('.mention-chip') : null;
    if (!chip) return;
    ev.preventDefault();
    const path = chip.dataset.mentionPath;
    const ws = _workspace();
    if (!path || !ws) return;
    import('./fileViewer.js')
      .then(mod => mod.open(path, { workspace: ws }))
      .catch(() => {});
  });
}

export default { initMentionChips, stopMentionChips, decorate, splitMentions, mentionPath, invalidateWorkspace };
