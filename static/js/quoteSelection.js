// static/js/quoteSelection.js
// Select text in a message → a small "Quote" button → the passage lands in the
// composer as a blockquote. Claude and ChatGPT both do this, and it is the
// cheapest fix for the most common follow-up in a long agent answer: "this
// part, explain/redo it". Without it people retype the sentence, or say "the
// third bullet" and let a 9B model guess which one that was.

const BTN_ID = 'quote-selection-btn';
const MAX_QUOTE_CHARS = 700;

/**
 * A Markdown blockquote of `text`, trimmed and length-capped.
 *
 * Exported for tests: the quoting rules are the whole feature. Blank lines
 * keep their ">" so the quote stays one block in every Markdown renderer, and
 * an over-long passage is cut on a word boundary — a hard cut mid-token reads
 * as corruption in code.
 */
export function blockquote(text) {
  let t = String(text == null ? '' : text).replace(/\r\n/g, '\n').trim();
  if (!t) return '';
  if (t.length > MAX_QUOTE_CHARS) {
    const cut = t.slice(0, MAX_QUOTE_CHARS);
    const onWord = cut.replace(/\s+\S*$/, '').trimEnd();
    // Falling back to the hard cut matters for one unbroken run — a minified
    // line or a long token would otherwise trim the quote away to nothing.
    t = (onWord.length > MAX_QUOTE_CHARS * 0.6 ? onWord : cut.trimEnd()) + '…';
  }
  return t.split('\n').map(line => (line.trim() ? `> ${line}` : '>')).join('\n');
}

/**
 * The composer's value after quoting `text` into it. Exported for tests.
 * An existing draft is kept — the quote goes above it, since the draft is
 * usually the question being asked about the quote.
 */
export function withQuote(current, text) {
  const quote = blockquote(text);
  if (!quote) return String(current == null ? '' : current);
  const draft = String(current == null ? '' : current).trim();
  return draft ? `${quote}\n\n${draft}` : `${quote}\n\n`;
}

function _selectionInside(root) {
  const sel = window.getSelection ? window.getSelection() : null;
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const text = String(sel.toString() || '').trim();
  if (text.length < 2) return null;
  const range = sel.getRangeAt(0);
  const container = range.commonAncestorContainer;
  const el = container.nodeType === 1 ? container : container.parentElement;
  if (!el || !root.contains(el)) return null;
  // Only inside a message body — not the harness cards, tool output or chips.
  if (!el.closest || !el.closest('.msg .body')) return null;
  return { text, rect: range.getBoundingClientRect() };
}

function _ensureButton() {
  let btn = document.getElementById(BTN_ID);
  if (btn) return btn;
  btn = document.createElement('button');
  btn.id = BTN_ID;
  btn.type = 'button';
  btn.className = 'quote-selection-btn';
  btn.textContent = '❝ Quote';
  btn.title = 'Quote this passage in the composer';
  btn.hidden = true;
  document.body.appendChild(btn);
  return btn;
}

export function initQuoteSelection(opts = {}) {
  const root = opts.root || document.getElementById('chat-history');
  const composerId = opts.composerId || 'message';
  if (!root || root._quoteSelectionWired) return;
  root._quoteSelectionWired = true;

  const btn = _ensureButton();
  let pending = '';

  const hide = () => { btn.hidden = true; pending = ''; };

  const place = (rect) => {
    const top = rect.top + window.scrollY - 38;
    const left = rect.left + window.scrollX + Math.max(0, rect.width / 2 - 32);
    btn.style.top = Math.max(4, top) + 'px';
    btn.style.left = Math.max(4, Math.min(left, window.innerWidth - 90)) + 'px';
    btn.hidden = false;
  };

  const refresh = () => {
    const found = _selectionInside(root);
    if (!found) { hide(); return; }
    pending = found.text;
    place(found.rect);
  };

  // mouseup covers mouse drags; keyup covers shift+arrow selection.
  root.addEventListener('mouseup', () => setTimeout(refresh, 0));
  root.addEventListener('keyup', (e) => { if (e.shiftKey) setTimeout(refresh, 0); });
  document.addEventListener('selectionchange', () => {
    // Only ever hides — showing from here would fight the click on the button.
    if (!btn.hidden && !_selectionInside(root)) hide();
  });
  window.addEventListener('scroll', hide, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });

  btn.addEventListener('mousedown', (e) => {
    // mousedown, not click: the click would clear the selection first.
    e.preventDefault();
    e.stopPropagation();
    const text = pending;
    hide();
    if (!text) return;
    const composer = document.getElementById(composerId);
    if (!composer) return;
    composer.value = withQuote(composer.value, text);
    composer.dispatchEvent(new Event('input', { bubbles: true }));
    composer.focus();
    try {
      const end = composer.value.length;
      composer.setSelectionRange(end, end);
    } catch (_) {}
    try { window.getSelection().removeAllRanges(); } catch (_) {}
  });
}

export default { initQuoteSelection, blockquote, withQuote };
