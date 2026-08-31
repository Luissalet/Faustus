// static/js/chatExport.js
// One client for both chat-export endpoints:
//
//   GET /api/session/{sid}/export?fmt=      → one conversation
//   GET /api/sessions/export?fmt=&project=  → a project/folder/id-list as .zip
//
// Downloads go through fetch + Blob rather than window.open. The old
// window.open path could not see the response at all: a 400 (unknown format,
// batch too large) or a 503 (PDF/DOCX dependency missing) landed as a blank
// tab or a page of raw JSON, and the user was left guessing. Here the error
// body is read and handed to whatever notifier the caller already uses.

export const EXPORT_FORMATS = [
  { id: 'md',   label: 'Markdown' },
  { id: 'txt',  label: 'Plain text' },
  { id: 'json', label: 'JSON' },
  { id: 'html', label: 'HTML' },
  { id: 'pdf',  label: 'PDF' },
  { id: 'docx', label: 'Word (.docx)' },
];

export const EXPORT_FORMAT_IDS = EXPORT_FORMATS.map(f => f.id);

// Blob URLs pin the whole payload in memory until revoked — a 200 MB project
// zip would stay there for the life of the tab. Revoking synchronously after
// click() cancels the download in some browsers, so revoke on a short delay
// (same trick as documentLibrary.js's bulk export).
const REVOKE_DELAY_MS = 2000;

const API_BASE = () => window.location.origin;

/** Pull the download name out of a Content-Disposition header.
 *  The server sends both halves of RFC 6266 — a quoted ASCII fallback and
 *  `filename*=UTF-8''<pct-encoded>` — so prefer the starred one, which is the
 *  only one that still spells "Informe 2026 — año.pdf" correctly. */
export function filenameFromDisposition(header, fallback = 'export') {
  const raw = String(header || '');
  const star = raw.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (star) {
    try { return decodeURIComponent(star[1].trim()); } catch (_) { /* malformed → fall through */ }
  }
  const quoted = raw.match(/filename\s*=\s*"([^"]*)"/i);
  if (quoted && quoted[1].trim()) return quoted[1].trim();
  const bare = raw.match(/filename\s*=\s*([^;]+)/i);
  if (bare && bare[1].trim()) return bare[1].trim().replace(/^"|"$/g, '');
  return fallback;
}

/** Turn a failed response into the message the server actually wrote. */
async function errorMessageFor(res) {
  let detail = '';
  try {
    const body = await res.json();
    if (body && typeof body.detail === 'string') detail = body.detail;
  } catch (_) { /* not JSON — fall back to the status */ }
  if (detail) return detail;
  if (res.status === 503) return 'This export format is not available on the server.';
  return `Export failed (HTTP ${res.status})`;
}

/**
 * Fetch `url`, save the body as a file, and report failures through `onError`.
 * Returns true when the download started.
 */
export async function downloadExport(url, { fallbackName = 'export', onError, onDone } = {}) {
  const fail = (msg) => { if (typeof onError === 'function') onError(msg); return false; };

  let res;
  try {
    res = await fetch(url, { credentials: 'same-origin' });
  } catch (_) {
    return fail('Export failed: could not reach the server.');
  }
  if (!res.ok) return fail(await errorMessageFor(res));

  const name = filenameFromDisposition(res.headers.get('Content-Disposition'), fallbackName);
  let blob;
  try {
    blob = await res.blob();
  } catch (_) {
    return fail('Export failed while downloading the file.');
  }

  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = name;
    a.rel = 'noopener';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(objectUrl), REVOKE_DELAY_MS);
  }

  if (typeof onDone === 'function') onDone(name);
  return true;
}

/** Export a single conversation. `opts.filename` overrides the server's name. */
export function exportSession(sessionId, fmt, opts = {}) {
  const format = EXPORT_FORMAT_IDS.includes(fmt) ? fmt : 'md';
  const params = new URLSearchParams({ fmt: format });
  if (opts.filename) params.set('filename', opts.filename);
  return downloadExport(
    `${API_BASE()}/api/session/${encodeURIComponent(sessionId)}/export?${params}`,
    { fallbackName: opts.filename || `conversation.${format}`, ...opts },
  );
}

/** Export a whole project / folder / list of chats as one .zip. */
export function exportSessionsZip({ project = '', folder = '', ids = [] } = {}, fmt, opts = {}) {
  const format = EXPORT_FORMAT_IDS.includes(fmt) ? fmt : 'md';
  const params = new URLSearchParams({ fmt: format });
  if (project) params.set('project', project);
  if (folder) params.set('folder', folder);
  if (ids && ids.length) params.set('ids', Array.from(ids).join(','));
  if (opts.filename) params.set('filename', opts.filename);
  return downloadExport(
    `${API_BASE()}/api/sessions/export?${params}`,
    { fallbackName: `${folder || project || 'chats'}.zip`, ...opts },
  );
}

/**
 * Floating "pick a format" menu anchored to `anchorEl`, listing all six
 * formats (PDF and DOCX included). Shared by the sidebar chat menu, the
 * folder header, the bulk-select bar and the project hub so there is one
 * format list, not four that drift apart.
 *
 * The element removes itself on outside click rather than living on in
 * `document.body` — the session list re-renders often and orphan menus would
 * pile up behind it.
 */
export function openExportFormatMenu(anchorEl, onPick) {
  document.querySelectorAll('.export-format-menu').forEach(m => m.remove());

  const menu = document.createElement('div');
  menu.className = 'dropdown session-folder-submenu export-format-menu';
  menu.style.display = 'block';
  menu.addEventListener('click', (e) => e.stopPropagation());

  EXPORT_FORMATS.forEach(f => {
    const opt = document.createElement('div');
    opt.className = 'dropdown-item-compact';
    opt.dataset.exportFmt = f.id;
    opt.textContent = f.label;
    opt.addEventListener('click', (e) => {
      e.stopPropagation();
      close();
      onPick(f);
    });
    menu.appendChild(opt);
  });

  document.body.appendChild(menu);

  // Measure off-screen, then clamp into the viewport.
  menu.style.top = '-9999px';
  menu.style.left = '0px';
  const rect = anchorEl.getBoundingClientRect();
  const box = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - box.width - 8)) + 'px';
  const belowFits = rect.bottom + 4 + box.height <= window.innerHeight;
  menu.style.top = (belowFits ? rect.bottom + 4 : Math.max(8, rect.top - box.height - 4)) + 'px';

  function close() {
    menu.remove();
    document.removeEventListener('click', onOutside, true);
  }
  function onOutside(ev) {
    if (!menu.contains(ev.target)) close();
  }
  setTimeout(() => document.addEventListener('click', onOutside, true), 0);
  return menu;
}

export default {
  EXPORT_FORMATS,
  EXPORT_FORMAT_IDS,
  filenameFromDisposition,
  downloadExport,
  exportSession,
  exportSessionsZip,
  openExportFormatMenu,
};
