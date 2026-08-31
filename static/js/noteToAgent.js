/**
 * noteToAgent.js — hand a note or checklist to the agent (FAUSTUS).
 *
 * Roadmap, frontend: *"Todos should be assignable to an agent from the UI,
 * possibly through a button."* The agent already has `manage_notes`, so it can
 * read a list and tick items off; what was missing was the one gesture that
 * starts that — until now you had to retype your own to-do into the composer.
 *
 * The button builds a prompt that names the note by id and lists only the open
 * items, tells the agent to do the work rather than describe it, and to check
 * each item off through `manage_notes` as it goes — so the list you are
 * looking at is what gets updated, not a copy of it.
 *
 * Deliberately a separate module with a delegated listener: notes.js is 5k
 * lines of card rendering, and this needs exactly one button in it.
 */

const API_BASE = typeof window !== 'undefined' ? window.location.origin : '';

function _openItems(note) {
  const items = Array.isArray(note?.checklist_items) ? note.checklist_items
    : (Array.isArray(note?.items) ? note.items : []);
  return items
    .map((item, index) => ({ index, text: String(item?.text ?? item ?? '').trim(),
                             done: !!(item && item.done) }))
    .filter(item => item.text && !item.done);
}

export function buildPrompt(note) {
  const title = String(note?.title || '').trim() || 'untitled note';
  const id = String(note?.id || '');
  const open = _openItems(note);
  const head = `Work on this from my notes — note "${title}" (id ${id}).`;
  const rules = [
    'Do the work for real with your tools; do not just describe what could be done.',
    open.length
      ? `Tick each item off as you finish it: manage_notes with action="toggle_item", id="${id}", index=<the index shown above>.`
      : `If the note asks for something you completed, record the outcome with manage_notes (action="update", id="${id}").`,
    'If an item is ambiguous or you cannot do it, leave it unticked and say why at the end.',
  ];
  if (open.length) {
    const lines = open.map(item => `- [index ${item.index}] ${item.text}`).join('\n');
    return `${head}\n\nOpen items:\n${lines}\n\n${rules.join('\n')}`;
  }
  const body = String(note?.content || '').trim();
  return `${head}\n\n${body || '(the note is empty)'}\n\n${rules.join('\n')}`;
}

async function _fetchNote(id) {
  const res = await fetch(`${API_BASE}/api/notes/${encodeURIComponent(id)}`,
                          { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = await res.json();
  return body?.note || body;
}

function _toast(msg) {
  try { window.uiModule?.showToast?.(msg, 3000); } catch { /* toast is optional */ }
}

export async function handToAgent(noteId) {
  let note;
  try {
    note = await _fetchNote(noteId);
  } catch (e) {
    _toast(`Could not read that note (${e.message}).`);
    return false;
  }
  const input = document.getElementById('message');
  if (!input) return false;
  input.value = buildPrompt(note);
  input.dispatchEvent(new Event('input', { bubbles: true }));

  // Get the notes window out of the way so the turn is visible.
  try { document.getElementById('notes-pane')?.classList.remove('open'); } catch { /* not open */ }

  const chat = window.chatModule;
  if (chat && typeof chat.handleChatSubmit === 'function') {
    chat.handleChatSubmit({ preventDefault() {} })?.catch?.(() => {});
    return true;
  }
  // No chat module (shouldn't happen): leave the prompt in the composer rather
  // than losing it, and let the user press enter.
  input.focus();
  return false;
}

if (typeof document !== 'undefined') {
  document.addEventListener('click', (ev) => {
    const btn = ev.target?.closest?.('[data-note-to-agent]');
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();  // the card itself opens the editor on click
    handToAgent(btn.getAttribute('data-note-to-agent'));
  });
}

export default { handToAgent, buildPrompt };
