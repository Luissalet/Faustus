/**
 * Mail helpers with no DOM state and no fetch: addresses, sender hues,
 * HTML sanitising for the reader, inline images, folds. Ported from the
 * previous interface's `emailLibrary/utils.js` and `signatureFold.js`
 * (only the logic; the markup is React's).
 */

export interface Address {
  name: string;
  email: string;
}

/** "Ana <ana@x.com>, bob@y.com" → [{name, email}]. Tolerates bare names. */
export function splitAddresses(raw: string): Address[] {
  const out: Address[] = [];
  let buf = '';
  let quoted = false;
  for (const ch of String(raw || '')) {
    if (ch === '"') quoted = !quoted;
    if ((ch === ',' || ch === ';') && !quoted) {
      if (buf.trim()) out.push(parseAddress(buf));
      buf = '';
    } else buf += ch;
  }
  if (buf.trim()) out.push(parseAddress(buf));
  return out;
}

export function parseAddress(raw: string): Address {
  const s = String(raw || '').trim();
  const m = s.match(/^\s*"?([^"<]*)"?\s*<([^>]+)>\s*$/);
  if (m) return { name: m[1].trim(), email: m[2].trim().toLowerCase() };
  const e = s.match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/);
  if (e) return { name: s === e[0] ? '' : s.replace(e[0], '').replace(/[<>"]/g, '').trim(), email: e[0].toLowerCase() };
  return { name: s.replace(/["<>]/g, ''), email: '' };
}

export function formatAddress(a: Address): string {
  return a.name && a.email ? `${a.name} <${a.email}>` : a.email || a.name;
}

export function joinAddresses(list: Address[]): string {
  return list.map(formatAddress).filter(Boolean).join(', ');
}

/** Display name for a sender: the name, else the local part of the address. */
export function displayName(name: string, email: string): string {
  const n = String(name || '').trim();
  if (n && n !== email) return n;
  const e = String(email || '').trim();
  return e.includes('@') ? e.split('@')[0] : e;
}

export function initials(s: string): string {
  const parts = String(s || '')
    .replace(/[<>"]/g, '')
    .trim()
    .split(/[\s._-]+/)
    .filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** Stable hue bucket (0–6) for a sender, so an avatar keeps its colour. */
export function hueIndex(s: string, buckets = 7): number {
  let h = 0;
  for (const ch of String(s || '').toLowerCase()) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % buckets;
}

export function isValidEmail(s: string): boolean {
  return /^[\w.+%-]+@[\w-]+(?:\.[\w-]+)+$/.test(String(s || '').trim());
}

/* ── Tags ── */

export const DONE_RESPONSE_TAGS = new Set(['urgent', 'reply-soon', 'action-needed']);

export function normalizeTag(tag: string): string {
  return String(tag || '')
    .trim()
    .toLowerCase()
    .replace(/_/g, '-');
}

/** Tags worth showing on a row: response tags disappear once the mail is done. */
export function visibleTags(tags: string[], answered: boolean): string[] {
  const list = (tags || []).map(normalizeTag).filter(Boolean);
  return answered ? list.filter((x) => !DONE_RESPONSE_TAGS.has(x)) : list;
}

/* ── HTML for the reader ── */

const URL_ATTRS = ['href', 'src', 'xlink:href', 'srcset', 'action', 'formaction', 'background', 'poster', 'data'];
const STRIP_CSS_PROPS = ['color', 'background', 'background-color', 'font-family', 'font', '-webkit-text-fill-color', 'position', 'z-index'];
const HIGHLIGHT_INLINE_TAGS = new Set(['SPAN', 'FONT', 'EM', 'B', 'I', 'STRONG', 'SMALL', 'U']);
const HAS_BG_COLOR = /background(?:-color)?\s*:\s*(?!\s*(?:transparent|none|inherit|initial)\b)[^;]+/i;
const CONTROL_CHARS = new RegExp('[\\u0000-\\u0020\\u007f-\\u009f]+', 'g');

function compactScheme(value: string): string {
  return String(value || '').replace(CONTROL_CHARS, '').toLowerCase();
}

function dangerousUrl(value: string): boolean {
  const c = compactScheme(value);
  return c.startsWith('javascript:') || c.startsWith('vbscript:') || c.startsWith('data:');
}

export interface SanitizeOptions {
  /** Keep the mail's own colours and fonts (the "original" view). Default strips them so the mail reads in the theme. */
  keepStyles?: boolean;
  /** Turns `cid:` references into URLs the server can serve. */
  inlineImageUrl?: (cid: string) => string;
  /** Remote (http) images are held back unless allowed; held-back ones get `data-held` and lose `src`. */
  allowRemoteImages?: boolean;
}

export interface SanitizedHtml {
  html: string;
  /** How many remote images were held back. */
  heldImages: number;
}

function sanitizeOnce(html: string, o: SanitizeOptions, counter: { held: number }): string {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  doc.querySelectorAll('script, iframe, object, embed, form, style, link, svg, math, base, meta, noscript, frame, frameset, applet, portal, template').forEach((el) => el.remove());
  const marked: Element[] = [];
  doc.querySelectorAll('*').forEach((el) => {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      if (name.startsWith('on') || name === 'srcdoc') {
        el.removeAttribute(attr.name);
        continue;
      }
      if (URL_ATTRS.includes(name)) {
        const bad = name === 'srcset' ? attr.value.split(',').some(dangerousUrl) : dangerousUrl(attr.value);
        if (bad) el.removeAttribute(attr.name);
      }
    }
    if (!o.keepStyles) {
      el.removeAttribute('color');
      const bgcolor = el.getAttribute('bgcolor');
      el.removeAttribute('bgcolor');
      el.removeAttribute('face');
      const style = el.getAttribute('style');
      const hadHighlight = HIGHLIGHT_INLINE_TAGS.has(el.tagName) && ((style && HAS_BG_COLOR.test(style)) || (bgcolor && bgcolor !== 'transparent'));
      if (hadHighlight) marked.push(el);
      if (style) {
        const kept = style
          .split(';')
          .map((s) => s.trim())
          .filter((decl) => {
            if (!decl) return false;
            const lower = compactScheme(decl);
            if (lower.includes('javascript:') || lower.includes('vbscript:') || lower.includes('data:') || lower.includes('expression(')) return false;
            const prop = decl.split(':', 1)[0].trim().toLowerCase();
            return !STRIP_CSS_PROPS.includes(prop);
          });
        if (kept.length) el.setAttribute('style', kept.join('; '));
        else el.removeAttribute('style');
      }
    } else {
      const style = el.getAttribute('style');
      if (style && /javascript:|vbscript:|expression\(|data:/i.test(compactScheme(style))) el.removeAttribute('style');
    }
    if (el.tagName === 'A') {
      el.setAttribute('target', '_blank');
      el.setAttribute('rel', 'noopener noreferrer');
    }
    if (el.tagName === 'IMG') {
      const src = el.getAttribute('src') || '';
      if (/^cid:/i.test(src)) {
        const cid = src.replace(/^cid:/i, '').replace(/^<|>$/g, '').trim();
        if (o.inlineImageUrl && cid) el.setAttribute('src', o.inlineImageUrl(cid));
        else el.removeAttribute('src');
        el.setAttribute('data-inline', '');
      } else if (/^https?:/i.test(src) || /^\/\//.test(src)) {
        if (!o.allowRemoteImages) {
          el.setAttribute('data-held', src);
          el.removeAttribute('src');
          el.removeAttribute('srcset');
          counter.held += 1;
        }
      }
      el.setAttribute('loading', 'lazy');
      el.removeAttribute('width');
      el.removeAttribute('height');
    }
  });
  marked.forEach((el) => {
    if (el.tagName === 'MARK' || !el.firstChild) return;
    const mark = doc.createElement('mark');
    while (el.firstChild) mark.appendChild(el.firstChild);
    el.appendChild(mark);
  });
  return doc.body.innerHTML;
}

export function sanitizeMailHtml(html: string, o: SanitizeOptions = {}): SanitizedHtml {
  let out = String(html ?? '');
  const counter = { held: 0 };
  for (let i = 0; i < 4; i++) {
    counter.held = 0;
    const next = sanitizeOnce(out, o, counter);
    if (next === out) break;
    out = next;
  }
  return { html: out, heldImages: counter.held };
}

/** Plain text → safe HTML with clickable links and addresses. */
export function textToHtml(text: string): string {
  const esc = String(text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const urlRe = /\b((?:https?:\/\/|www\.)[^\s<>"']+[^\s<>"'.,;:!?)\]])/g;
  const mailRe = /\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b/g;
  return esc
    .replace(urlRe, (m) => {
      const href = m.startsWith('www.') ? `https://${m}` : m;
      return `<a href="${href.replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer">${m}</a>`;
    })
    .replace(mailRe, (m) => `<a href="mailto:${m}">${m}</a>`)
    .replace(/\n/g, '<br>');
}

/** Text of an HTML fragment, whitespace collapsed. */
export function htmlToText(html: string): string {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  return (doc.body.textContent || '').replace(/\s+/g, ' ').trim();
}

/* ── Thread turns and folds ── */

export interface TurnMeta {
  author: string;
  email: string;
  date: string;
}

/** "Ana Ruiz <ana@x> · 3 Sep 2026, 10:15" → parts. */
export function parseTurnMeta(meta: string): TurnMeta {
  if (!meta) return { author: '', email: '', date: '' };
  const m = String(meta);
  const eMatch = m.match(/<([^<>\s]+@[^<>\s]+)>/) || m.match(/\b([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})\b/);
  const email = eMatch ? eMatch[1].toLowerCase().trim() : '';
  const parts = m.split(/\s+[·•]\s+/);
  let author = '';
  let date = '';
  if (parts.length >= 2) {
    author = parts[0].replace(/<[^>]+>/g, '').trim();
    date = parts.slice(1).join(' · ').trim();
  } else author = m.replace(/<[^>]+>/g, '').trim();
  return { author, email, date };
}

const WROTE = '(?:wrote|écrit|escribió|scrisse|schrieb|skrev|schreef|napsal|írta|написал|書きました|写道)';
const ORIG_RE = /(?:^|\n)[\s>]*[-_=]{3,}\s*(?:Original\s+Message|Forwarded\s+message|Mensaje\s+original|Mensaje\s+reenviado|Ursprüngliche\s+Nachricht|Message\s+d'origine)\s*[-_=]{3,}/i;

/**
 * Splits a plain-text body into the new part and the quoted history. The
 * server's thread parser does this for HTML; this covers text-only mail.
 */
export function splitQuotedText(text: string): { top: string; quoted: string } {
  const s = String(text || '');
  const lines = s.split('\n');
  const wroteRe = new RegExp(`^\\s*(?:On|El|Le|Am|Il|Op|Den|Dne)\\b.*${WROTE}\\s*:?\\s*$`, 'i');
  let cut = -1;
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*>/.test(l)) {
      cut = i;
      if (i > 0 && wroteRe.test(lines[i - 1])) cut = i - 1;
      break;
    }
    if (wroteRe.test(l)) {
      cut = i;
      break;
    }
  }
  const om = s.match(ORIG_RE);
  if (om && om.index !== undefined) {
    const at = s.slice(0, om.index).split('\n').length - 1;
    if (cut === -1 || at < cut) cut = at + (om[0].startsWith('\n') ? 1 : 0);
  }
  if (cut <= 0) return { top: s, quoted: '' };
  return { top: lines.slice(0, cut).join('\n').replace(/\s+$/, ''), quoted: lines.slice(cut).join('\n') };
}

/** Strips `>` prefixes so a quoted block reads as prose. */
export function unquote(text: string): string {
  return String(text || '')
    .split('\n')
    .map((l) => l.replace(/^\s*(?:>\s?)+/, ''))
    .join('\n')
    .trim();
}

function norm(s: string): string {
  return String(s || '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

function foldInto(doc: Document, kind: string, label: string): HTMLDetailsElement {
  const details = doc.createElement('details');
  details.className = 'fs-mail__fold';
  details.setAttribute('data-kind', kind);
  const summary = doc.createElement('summary');
  summary.textContent = label;
  details.appendChild(summary);
  return details;
}

/**
 * Folds the trailing signature (when the server knows the sender's usual
 * signature) into a `<details>` so long disclaimers stop at one line.
 * Returns the HTML unchanged when the hint is not found at the end.
 */
export function foldSignature(html: string, hint: string, label: string): string {
  const h = norm(hint);
  if (h.length < 12 || !html) return html;
  const doc = new DOMParser().parseFromString(`<div id="r">${html}</div>`, 'text/html');
  const root = doc.getElementById('r');
  if (!root) return html;
  const nodes = Array.from(root.childNodes);
  let acc = '';
  const take: ChildNode[] = [];
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    const txt = norm(n.textContent || '');
    if (!txt) {
      take.unshift(n);
      continue;
    }
    const next = norm(`${txt} ${acc}`);
    if (h.includes(next) || next.includes(h.slice(0, 40))) {
      acc = next;
      take.unshift(n);
      if (next.includes(h.slice(0, 40)) && acc.length >= Math.min(h.length, 60)) break;
    } else break;
  }
  if (!take.length || acc.length < 12 || root.childNodes.length === take.length) return html;
  const details = foldInto(doc, 'signature', label);
  const inner = doc.createElement('div');
  take.forEach((n) => inner.appendChild(n));
  details.appendChild(inner);
  root.appendChild(details);
  return root.innerHTML;
}

/** Wraps blockquotes that hold a reply history into a fold, keeping the new part readable. */
export function foldQuotes(html: string, label: string): string {
  if (!html || !/<blockquote|gmail_quote|divRplyFwdMsg|yahoo_quoted|appendonsend/i.test(html)) return html;
  const doc = new DOMParser().parseFromString(`<div id="r">${html}</div>`, 'text/html');
  const root = doc.getElementById('r');
  if (!root) return html;
  const candidates = root.querySelectorAll('blockquote, .gmail_quote, #divRplyFwdMsg, .yahoo_quoted, div[id^="appendonsend"]');
  let folded = 0;
  candidates.forEach((q) => {
    if (q.closest('details.fs-mail__fold')) return;
    const text = norm(q.textContent || '');
    if (text.length < 40) return;
    const details = foldInto(doc, 'quote', label);
    q.replaceWith(details);
    details.appendChild(q);
    folded += 1;
  });
  return folded ? root.innerHTML : html;
}

/** True when the text talks about an attachment (so we can warn before sending without one). */
export function mentionsAttachment(text: string): boolean {
  return /\b(attached|attachment|attaching|adjunto|adjunta|adjuntamos|adjuntar|anexo|ci-joint|pièce jointe|anbei|im anhang|allegato)\b/i.test(String(text || ''));
}

export function bytesLabel(n: number): string {
  if (!n) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
