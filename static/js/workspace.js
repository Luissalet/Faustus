// static/js/workspace.js
//
// Workspace picker: browse server directories in a draggable modal, choose a
// folder, and show it as a removable pill in the chat input bar. While set, the
// chat request sends `workspace` so the agent's file/shell tools are confined
// to that folder (see routes/chat_routes.py + src/tool_execution.py).

import Storage, { KEYS } from './storage.js';
import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';

const API_BASE = window.location.origin;
// Same folder glyph as the overflow menu item + pill (not an emoji).
const _FOLDER_SVG = '<svg class="workspace-row-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const _FILE_SVG = '<svg class="workspace-row-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
let _modal = null;
let _curPath = '';
// Set only while the browser is open on someone else's behalf (the project
// editor). Null means "Use this folder" binds the workspace, as it always has.
let _onPick = null;
let _pickerOptions = {};

export function getWorkspace() {
  return Storage.get(KEYS.WORKSPACE, '') || '';
}

function _basename(p) {
  if (!p) return '';
  // Handle both POSIX (/) and Windows (\) separators.
  const parts = p.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

// Workspace only applies to agent mode (it scopes the file/shell tools), so the
// pill + overflow entry are hidden in chat mode, like the bash toggle.
function _isChatMode() {
  const b = document.getElementById('mode-chat-btn');
  return !!(b && b.classList.contains('active'));
}

export function syncWorkspaceIndicator(path) {
  const chat = _isChatMode();
  const pill = document.getElementById('workspace-indicator-btn');
  const name = document.getElementById('workspace-indicator-name');
  const overflow = document.getElementById('overflow-workspace-btn');
  if (pill) {
    pill.style.display = (path && !chat) ? '' : 'none';
    pill.classList.toggle('active', !!path);
    if (path) pill.title = `Workspace: ${path}\nFile tools are confined here; shell commands start here but are not sandboxed and can reach outside it.\nClick to clear.`;
  }
  if (name) name.textContent = path ? _basename(path) : '';
  if (overflow) {
    overflow.style.display = chat ? 'none' : '';
    overflow.classList.toggle('active', !!path);
  }
  // Recompute the "+" overflow dot (app.js owns updatePlusDot via this event).
  try { document.dispatchEvent(new CustomEvent('overflow-state-change')); } catch (_) {}
}

// Called by the agent/chat mode toggle so the pill + overflow entry follow mode.
export function applyMode(_mode) {
  syncWorkspaceIndicator(getWorkspace());
}

export function setWorkspace(path) {
  if (path) Storage.set(KEYS.WORKSPACE, path);
  else Storage.remove(KEYS.WORKSPACE);
  syncWorkspaceIndicator(path || '');
}

/**
 * Validate a manually entered path server-side, then persist the canonical
 * form. Returns {ok, path|null}. Without this, a typo / file path / deleted
 * folder / filesystem root would be stored and shown as active while the
 * backend silently refuses to bind it on every send.
 */
export async function vetAndSetWorkspace(path) {
  try {
    const res = await fetch(`${API_BASE}/api/workspace/vet?path=${encodeURIComponent(path)}`, { credentials: 'same-origin' });
    if (!res.ok) return { ok: false, path: null };
    const data = await res.json();
    if (data.ok && data.path) {
      setWorkspace(data.path);
      return { ok: true, path: data.path };
    }
    return { ok: false, path: null };
  } catch (e) {
    return { ok: false, path: null };
  }
}

export function clearWorkspace() {
  setWorkspace('');
  if (uiModule && uiModule.showToast) uiModule.showToast('Workspace cleared');
}

async function _load(path) {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  if (_pickerOptions.includeFiles) params.set('include_files', 'true');
  const url = `${API_BASE}/api/workspace/browse${params.size ? `?${params.toString()}` : ''}`;
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`browse failed: ${res.status}`);
  return res.json();
}

function _render(data) {
  _curPath = data.path;
  const body = _modal.querySelector('#workspace-body');
  const pathEl = _modal.querySelector('#workspace-cur-path');
  if (pathEl) {
    // Reflect the resolved (realpath) location back into the editable field.
    pathEl.value = data.path;
    pathEl.title = data.path;
  }
  let rows = '';
  if (data.parent) {
    rows += `<div class="workspace-row workspace-up" data-path="${encodeURIComponent(data.parent)}">↑ ..</div>`;
  }
  for (const d of data.dirs) {
    // Backend supplies the full child path (os.path.join → cross-platform).
    rows += `<div class="workspace-row" data-path="${encodeURIComponent(d.path)}">${_FOLDER_SVG}<span>${uiModule.esc(d.name)}</span></div>`;
  }
  for (const file of (data.files || [])) {
    rows += `<button type="button" class="workspace-row workspace-file" data-file-path="${encodeURIComponent(file.path)}">${_FILE_SVG}<span>${uiModule.esc(file.name)}</span><small>${Number(file.size || 0).toLocaleString()} B</small></button>`;
  }
  if (data.truncated) {
    rows += '<div class="workspace-empty">Too many folders to list. Type or paste a path above to jump in.</div>';
  }
  if (!data.dirs.length && !data.parent) rows = '<div class="workspace-empty">No subfolders</div>';
  body.innerHTML = rows || '<div class="workspace-empty">No subfolders</div>';
  body.querySelectorAll('.workspace-row').forEach((row) => {
    if (row.dataset.filePath) {
      row.addEventListener('click', () => _choose(decodeURIComponent(row.dataset.filePath)));
    } else {
      row.addEventListener('click', () => _navigate(decodeURIComponent(row.dataset.path)));
    }
  });
  // Filesystem roots (and sensitive dirs) can be browsed through but never
  // bound as the workspace; the backend rejects them too.
  const useBtn = _modal.querySelector('#workspace-use');
  if (useBtn) {
    useBtn.disabled = data.selectable === false;
    useBtn.title = data.selectable === false ? 'This folder cannot be used as a workspace' : '';
  }
}

function _choose(path) {
  if (_onPick) {
    const cb = _onPick;
    _onPick = null;
    closeWorkspaceBrowser();
    cb(path);
    return;
  }
  setWorkspace(path);
  if (uiModule && uiModule.showToast) uiModule.showToast(`Workspace set: ${_basename(path)}`);
  closeWorkspaceBrowser();
}

async function _navigate(path) {
  try {
    _render(await _load(path));
  } catch (e) {
    if (uiModule && uiModule.showError) uiModule.showError('Could not open folder');
  }
}

function _getModal() {
  if (_modal) return _modal;
  _modal = document.createElement('div');
  _modal.id = 'workspace-modal';
  _modal.className = 'modal';
  _modal.style.display = 'none';
  _modal.innerHTML = `
    <div class="modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Select workspace</h4>
        <button class="close-btn" id="workspace-close" aria-label="Close">✖</button>
      </div>
      <input type="text" class="styled-prompt-input workspace-cur" id="workspace-cur-path"
             spellcheck="false" autocomplete="off" autocapitalize="off" autocorrect="off"
             placeholder="Type or paste a folder path, then press Enter" />
      <p class="muted workspace-note">File tools are <strong>confined</strong> to this folder. Shell commands start here but are <strong>not sandboxed</strong> and can reach outside it. A workspace scopes the tools; it is not a security boundary.</p>
      <div class="modal-body workspace-body" id="workspace-body"></div>
      <div class="modal-footer workspace-footer">
        <button type="button" class="confirm-btn confirm-btn-secondary" id="workspace-cancel">Cancel</button>
        <button type="button" class="confirm-btn confirm-btn-primary" id="workspace-use">Use this folder</button>
      </div>
    </div>`;
  document.body.appendChild(_modal);
  _modal.querySelector('#workspace-close').addEventListener('click', closeWorkspaceBrowser);
  _modal.querySelector('#workspace-cancel').addEventListener('click', closeWorkspaceBrowser);
  // Editable path bar: Enter navigates to a typed/pasted folder.
  _modal.querySelector('#workspace-cur-path').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const v = e.target.value.trim();
      if (!v) return;
      if (_pickerOptions.includeFiles) {
        fetch(`${API_BASE}/api/workspace/vet-context?path=${encodeURIComponent(v)}`, { credentials: 'same-origin' })
          .then(res => res.ok ? res.json() : null)
          .then(data => {
            if (!data?.ok) throw new Error('invalid path');
            if (data.kind === 'file') _choose(data.path);
            else _navigate(data.path);
          })
          .catch(() => uiModule?.showError?.('That file or folder cannot be added'));
      } else {
        _navigate(v);
      }
    }
  });
  _modal.querySelector('#workspace-use').addEventListener('click', () => {
    // Borrowed by the project editor: with a picker callback the chosen folder
    // is handed back instead of being bound globally.
    _choose(_curPath);
  });
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(_modal, { content, header });
  return _modal;
}

/**
 * @param {?function(string):void} onPick When given, "Use this folder" hands
 *   the chosen path to this callback instead of binding it as the workspace.
 *   The project editor uses it to fill its folder field.
 */
export async function openWorkspaceBrowser(onPick = null, options = {}) {
  _onPick = typeof onPick === 'function' ? onPick : null;
  _pickerOptions = options || {};
  const modal = _getModal();
  const title = modal.querySelector('.modal-header h4');
  if (title) title.innerHTML = `${_FOLDER_SVG}${uiModule.esc(_pickerOptions.title || 'Select workspace')}`;
  const note = modal.querySelector('.workspace-note');
  if (note) note.innerHTML = _pickerOptions.includeFiles
    ? 'Choose a file, or open a folder and add it with the button below. Project work roots can be <strong>read and modified</strong> by the agent.'
    : 'File tools are <strong>confined</strong> to this folder. Shell commands start here but are <strong>not sandboxed</strong> and can reach outside it.';
  const use = modal.querySelector('#workspace-use');
  if (use) use.textContent = _pickerOptions.useLabel || 'Use this folder';
  modal.style.display = 'flex';
  try {
    _render(await _load(getWorkspace() || ''));
  } catch (e) {
    if (uiModule && uiModule.showError) uiModule.showError('Could not browse folders');
  }
}

export function closeWorkspaceBrowser() {
  if (_modal) _modal.style.display = 'none';
  _onPick = null;
  _pickerOptions = {};
}

export function initWorkspace() {
  // Restore persisted workspace into the pill on load.
  syncWorkspaceIndicator(getWorkspace());
  const overflow = document.getElementById('overflow-workspace-btn');
  if (overflow) overflow.addEventListener('click', openWorkspaceBrowser);
  const pill = document.getElementById('workspace-indicator-btn');
  if (pill) pill.addEventListener('click', clearWorkspace);
}

export default { initWorkspace, openWorkspaceBrowser, getWorkspace, setWorkspace, vetAndSetWorkspace, clearWorkspace, syncWorkspaceIndicator, applyMode };
