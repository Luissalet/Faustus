//
// Projects: one object binding a sidebar chat folder, a folder on disk,
// standing instructions, and a Markdown memory kept inside that folder.
//
// The backend is the authority — routes/chat_routes.py resolves the workspace
// from the session and routes/chat_helpers.py prepends the instructions, so a
// stale tab can never send the agent to the wrong folder. This module is the
// gallery, the editor, and keeping the workspace pill honest.
//
// The markup shell lives in index.html (#projects-modal); the gallery and the
// detail view are rendered here.

import Storage from './storage.js';
import uiModule from './ui.js';
import workspaceModule from './workspace.js';

const API = `${window.location.origin}/api/projects`;

// Which workspace WE applied. Without this, leaving a project chat for a
// project-less one would silently leave the agent pointed at the old project's
// folder — while a workspace the user set by hand must survive the switch.
const AUTO_KEY = 'odysseus-project-workspace';

let _projects = [];
let _loaded = false;
let _active = null;
let _wired = false;
let _draft = null;        // project shown in the detail view

const ICON_FOLDER = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const ICON_DISK = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M2 20h20"/></svg>';
const ICON_CHAT = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
const ICON_DOC = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

const $ = (id) => document.getElementById(id);
const esc = (s) => (uiModule && uiModule.esc ? uiModule.esc(s == null ? '' : String(s)) : String(s == null ? '' : s));

function basename(p) {
  if (!p) return '';
  const parts = String(p).replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

// ---------------------------------------------------------------- API client

async function req(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

export async function loadProjects(force = false) {
  if (_loaded && !force) return _projects;
  try {
    _projects = await req('');
    _loaded = true;
  } catch (e) {
    // Non-admin or auth-gated installs simply have no projects. Fail quiet:
    // this runs on every session switch.
    _projects = [];
    _loaded = true;
  }
  return _projects;
}

export const createProject = (body) => req('', { method: 'POST', body: JSON.stringify(body) });
export const updateProject = (id, body) => req(`/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
export const deleteProject = (id) => req(`/${id}`, { method: 'DELETE' });
export const previewProject = (id) => req(`/${id}/preview`);
export const listMemory = (id) => req(`/${id}/memory`);
export const readMemory = (id, name) => req(`/${id}/memory/${encodeURIComponent(name)}`);
export const writeMemory = (id, name, content) =>
  req(`/${id}/memory/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ content }) });

// ------------------------------------------------------------ active project

function byFolder(folder) {
  const key = (folder || '').trim().toLowerCase();
  if (!key) return null;
  return _projects.find(p => (p.folder || '').trim().toLowerCase() === key && p.enabled !== false) || null;
}

export function getActiveProject() {
  return _active;
}

function syncWorkspace(project) {
  const applied = Storage.get(AUTO_KEY, '') || '';
  const current = workspaceModule.getWorkspace();
  if (project && project.workspace) {
    if (current !== project.workspace) workspaceModule.setWorkspace(project.workspace);
    Storage.set(AUTO_KEY, project.workspace);
    return;
  }
  // Leaving a project: drop only the folder we put there ourselves.
  if (applied && current === applied) workspaceModule.setWorkspace('');
  Storage.remove(AUTO_KEY);
}

function syncPillTitle(project) {
  const pill = $('workspace-indicator-btn');
  if (!pill || !project || !project.workspace) return;
  pill.title =
    `Project: ${project.name}\nFolder: ${project.workspace}\n` +
    'Set by the project, so it follows this chat. Change it in Projects.';
}

/** Called from sessions.js when the user opens a chat. */
export async function onSessionSwitch(sessionId, folder) {
  await loadProjects();
  const project = byFolder(folder);
  _active = project;
  syncWorkspace(project);
  syncPillTitle(project);
  try {
    document.dispatchEvent(new CustomEvent('project-changed', { detail: { project } }));
  } catch (_) {}
  return project;
}

// ------------------------------------------------------------------ gallery

function chatsIn(project) {
  if (!project) return [];
  try {
    const all = (window.sessionModule && window.sessionModule.getSessions()) || [];
    const key = (project.folder || '').trim().toLowerCase();
    return all.filter(s => (s.folder || '').trim().toLowerCase() === key);
  } catch (_) {
    return [];
  }
}

function sortProjects(list, mode) {
  const copy = list.slice();
  if (mode === 'name') return copy.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  if (mode === 'created') return copy.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  return copy.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
}

function cardHtml(p) {
  const sub = (p.instructions || '').trim();
  const chats = chatsIn(p).length;
  return `
    <div class="project-card" data-project="${esc(p.id)}" role="button" tabindex="0">
      <div>
        <div class="project-card-name">${esc(p.name)}</div>
        <div class="project-card-sub">${sub ? esc(sub) : 'No instructions yet'}</div>
      </div>
      <div class="project-card-meta">
        <span class="project-chip" title="Chat folder">${ICON_FOLDER}<span>${esc(p.folder)}</span></span>
        <span class="project-chip" title="${esc(p.workspace || 'No folder bound')}">${ICON_DISK}<span>${esc(basename(p.workspace) || 'no folder')}</span></span>
        <span class="project-chip" title="${chats} chat(s) in this project">${ICON_CHAT}<span>${chats}</span></span>
      </div>
    </div>`;
}

function renderGallery() {
  const grid = $('projects-grid');
  if (!grid) return;
  $('projects-gallery').classList.remove('hidden');
  $('projects-detail').classList.add('hidden');

  const q = ($('projects-search')?.value || '').trim().toLowerCase();
  const mode = $('projects-sort')?.value || 'updated';
  let list = _projects;
  if (q) {
    list = list.filter(p =>
      (p.name || '').toLowerCase().includes(q) ||
      (p.folder || '').toLowerCase().includes(q) ||
      (p.workspace || '').toLowerCase().includes(q));
  }
  list = sortProjects(list, mode);

  if (!list.length) {
    grid.innerHTML = `<div class="projects-empty">${
      _projects.length
        ? 'No project matches that search.'
        : 'No projects yet.<br>A project ties a chat folder to a folder on disk, a set of standing instructions, and a memory that survives between chats.'
    }</div>`;
    return;
  }
  grid.innerHTML = list.map(cardHtml).join('');
  grid.querySelectorAll('.project-card').forEach(card => {
    const open = () => openDetail(_projects.find(p => p.id === card.dataset.project) || null);
    card.addEventListener('click', open);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
  });
}

// ------------------------------------------------------------------- detail

function detailHtml(p, isNew) {
  const chats = chatsIn(p);
  return `
    <div class="project-detail-head">
      <button type="button" class="project-back-btn" id="project-back">&#8592; Projects</button>
      <h3 class="project-detail-title">${isNew ? 'New project' : esc(p.name)}</h3>
    </div>

    <div class="project-field">
      <label for="project-name">Name</label>
      <input type="text" class="styled-prompt-input" id="project-name" value="${esc(p.name)}" placeholder="Covernet" spellcheck="false" />
    </div>

    <div class="project-field">
      <label for="project-folder">Chat folder in the sidebar</label>
      <input type="text" class="styled-prompt-input" id="project-folder" value="${esc(p.folder)}" placeholder="Same as the name if left blank" spellcheck="false" />
      <p class="project-hint">Every chat in this sidebar folder belongs to the project.</p>
    </div>

    <div class="project-field">
      <label for="project-workspace">Project folder on disk</label>
      <div class="project-row">
        <input type="text" class="styled-prompt-input" id="project-workspace" value="${esc(p.workspace)}" placeholder="D:\\Proyectos\\covernet" spellcheck="false" />
        <button type="button" class="project-back-btn" id="project-pick">Browse&hellip;</button>
      </div>
      <p class="project-hint">File tools are confined here in every chat of this project, and the project's memory lives in <code>.odysseus/</code> inside it. Shell commands start here but are not sandboxed.</p>
    </div>

    <div class="project-field">
      <label for="project-instructions">Instructions</label>
      <textarea class="styled-prompt-input" id="project-instructions" rows="8" placeholder="Standing rules for every chat in this project.">${esc(p.instructions)}</textarea>
      <p class="project-hint" id="project-cost"></p>
    </div>

    ${isNew ? '' : `
    <div class="project-section-title">${ICON_DOC} Memory</div>
    <div id="project-memory-list"><p class="project-hint">Loading&hellip;</p></div>

    <div class="project-section-title">${ICON_CHAT} Chats <span class="project-count">${chats.length}</span></div>
    <div id="project-chat-list">${
      chats.length
        ? chats.map(s => `<div class="project-chat-row" data-session="${esc(s.id)}"><span class="grow">${esc(s.name || 'Untitled')}</span></div>`).join('')
        : '<p class="project-hint">No chats yet. "New chat here" starts one inside this project.</p>'
    }</div>`}

    <div class="project-detail-actions">
      <button type="button" class="projects-new-btn" id="project-save">${isNew ? 'Create project' : 'Save'}</button>
      ${isNew ? '' : '<button type="button" class="project-back-btn" id="project-new-chat">New chat here</button>'}
      ${isNew ? '' : '<button type="button" class="project-back-btn" id="project-context">Show what the model sees</button>'}
      ${isNew ? '' : '<button type="button" class="project-back-btn project-danger" id="project-delete">Delete</button>'}
    </div>`;
}

function updateCost() {
  const el = $('project-cost');
  if (!el) return;
  const chars = ($('project-instructions')?.value || '').length;
  // Rough but useful: with a 32k local context, knowing the standing block
  // costs 4k characters is the difference between "it works" and "it forgot
  // the system prompt three turns in".
  el.textContent = chars
    ? `${chars} characters — roughly ${Math.ceil(chars / 4)} tokens on every turn of this project.`
    : 'Sent with every message in this project.';
}

async function renderMemoryList(project) {
  const host = $('project-memory-list');
  if (!host) return;
  try {
    const { dir, files } = await listMemory(project.id);
    if (!files.length) {
      host.innerHTML = `<p class="project-hint">Nothing yet. The agent writes here as it learns; the folder is <code>${esc(dir)}</code>.</p>`;
      return;
    }
    host.innerHTML = files.map(f => `
      <div class="project-memory-row" data-file="${esc(f.name)}">
        <span class="grow">${esc(f.name)}</span>
        <span class="project-file-size">${f.size} B</span>
        <button type="button" class="project-back-btn" data-open="${esc(f.name)}">Open</button>
      </div>`).join('') +
      `<p class="project-hint">In ${esc(dir)}. MEMORY.md is the index injected into every chat of this project.</p>`;
    host.querySelectorAll('[data-open]').forEach(btn => {
      btn.addEventListener('click', () => openMemoryFile(project, btn.dataset.open));
    });
  } catch (e) {
    host.innerHTML = `<p class="project-hint">Could not read the memory folder: ${esc(e.message || e)}</p>`;
  }
}

async function openMemoryFile(project, name) {
  let content = '';
  try {
    content = (await readMemory(project.id, name)).content;
  } catch (e) {
    uiModule.showError?.(String(e.message || e));
    return;
  }
  const host = $('projects-detail');
  host.innerHTML = `
    <div class="project-detail-head">
      <button type="button" class="project-back-btn" id="memory-back">&#8592; ${esc(project.name)}</button>
      <h3 class="project-detail-title">${esc(name)}</h3>
    </div>
    <textarea class="styled-prompt-input project-memory-editor" id="memory-editor" spellcheck="false">${esc(content)}</textarea>
    <div class="project-detail-actions">
      <button type="button" class="projects-new-btn" id="memory-save">Save</button>
    </div>`;
  $('memory-back').addEventListener('click', () => openDetail(project));
  $('memory-save').addEventListener('click', async () => {
    try {
      await writeMemory(project.id, name, $('memory-editor').value);
      uiModule.showToast?.(`${name} saved`);
    } catch (e) {
      uiModule.showError?.(String(e.message || e));
    }
  });
}

function collectForm() {
  return {
    name: $('project-name').value.trim(),
    folder: $('project-folder').value.trim(),
    workspace: $('project-workspace').value.trim(),
    instructions: $('project-instructions').value,
  };
}

async function newChatInProject(project) {
  try {
    const sessions = (window.sessionModule && window.sessionModule.getSessions()) || [];
    const current = sessions.find(s => s.id === window.__odysseusLastSelectedSessionId) || sessions[0];
    const fd = new FormData();
    fd.append('name', project.name);
    if (current) {
      fd.append('endpoint_url', current.endpoint_url || '');
      fd.append('model', current.model || '');
      fd.append('skip_validation', 'true');
    }
    const res = await fetch(`${window.location.origin}/api/session`, {
      method: 'POST', body: fd, credentials: 'same-origin',
    });
    if (!res.ok) throw new Error(`could not create the chat (${res.status})`);
    const data = await res.json();
    const sid = data.session_id || data.id;

    // Drop it straight into the project's folder — that IS the binding.
    const move = new FormData();
    move.append('folder', project.folder);
    await fetch(`${window.location.origin}/api/session/${sid}`, {
      method: 'PATCH', body: move, credentials: 'same-origin',
    });

    closeProjectsPanel();
    await window.sessionModule.loadSessions();
    await window.sessionModule.selectSession(sid);
  } catch (e) {
    uiModule.showError?.(String(e.message || e));
  }
}

export function openDetail(project) {
  const isNew = !project || !project.id;
  _draft = project || { name: '', folder: '', workspace: '', instructions: '' };
  const host = $('projects-detail');
  if (!host) return;
  $('projects-gallery').classList.add('hidden');
  host.classList.remove('hidden');
  host.innerHTML = detailHtml(_draft, isNew);

  $('project-back').addEventListener('click', renderGallery);
  $('project-instructions').addEventListener('input', updateCost);
  updateCost();

  $('project-pick').addEventListener('click', () => {
    // Borrow the existing directory browser and write its pick into the field
    // instead of binding it globally.
    workspaceModule.openWorkspaceBrowser((path) => {
      const input = $('project-workspace');
      if (input) input.value = path;
    });
  });

  $('project-save').addEventListener('click', async () => {
    const body = collectForm();
    if (!body.name) { uiModule.showError?.('The project needs a name'); return; }
    try {
      const saved = isNew ? await createProject(body) : await updateProject(_draft.id, body);
      await loadProjects(true);
      uiModule.showToast?.(isNew ? `Project "${saved.name}" created` : 'Project saved');
      refreshActive();
      openDetail(_projects.find(p => p.id === saved.id) || saved);
    } catch (e) {
      uiModule.showError?.(String(e.message || e));
    }
  });

  if (isNew) return;

  renderMemoryList(_draft);

  $('project-chat-list')?.querySelectorAll('.project-chat-row').forEach(row => {
    row.addEventListener('click', async () => {
      closeProjectsPanel();
      await window.sessionModule.selectSession(row.dataset.session);
    });
  });

  $('project-new-chat').addEventListener('click', () => newChatInProject(_draft));

  $('project-context').addEventListener('click', async () => {
    try {
      const { block, chars } = await previewProject(_draft.id);
      const host2 = $('projects-detail');
      host2.innerHTML = `
        <div class="project-detail-head">
          <button type="button" class="project-back-btn" id="ctx-back">&#8592; ${esc(_draft.name)}</button>
          <h3 class="project-detail-title">What the model is told — ${chars} characters</h3>
        </div>
        <textarea class="styled-prompt-input project-memory-editor" readonly spellcheck="false">${esc(block)}</textarea>`;
      $('ctx-back').addEventListener('click', () => openDetail(_draft));
    } catch (e) {
      uiModule.showError?.(String(e.message || e));
    }
  });

  const del = $('project-delete');
  del.addEventListener('click', async () => {
    // Two-step instead of confirm(): a native dialog blocks the whole page and
    // this codebase avoids it elsewhere.
    if (del.dataset.armed !== '1') {
      del.dataset.armed = '1';
      del.textContent = 'Really delete?';
      setTimeout(() => { del.dataset.armed = '0'; del.textContent = 'Delete'; }, 4000);
      return;
    }
    try {
      await deleteProject(_draft.id);
      await loadProjects(true);
      uiModule.showToast?.('Project deleted. Its folder and files are untouched.');
      refreshActive();
      renderGallery();
    } catch (e) {
      uiModule.showError?.(String(e.message || e));
    }
  });
}

async function refreshActive() {
  // Re-resolve the current chat so an edit takes effect without a switch.
  const id = window.__odysseusLastSelectedSessionId;
  if (!id) return;
  try {
    const { project } = await req(`/resolve/session/${encodeURIComponent(id)}`);
    _active = project ? (_projects.find(p => p.id === project.id) || project) : null;
    syncWorkspace(_active);
    syncPillTitle(_active);
  } catch (_) {}
}

// -------------------------------------------------------------------- panel

export async function openProjectsPanel() {
  const modal = $('projects-modal');
  if (!modal) return;
  wire();
  modal.classList.remove('hidden');
  await loadProjects(true);
  renderGallery();
  $('projects-search')?.focus();
}

export function closeProjectsPanel() {
  $('projects-modal')?.classList.add('hidden');
}

function wire() {
  if (_wired) return;
  _wired = true;
  $('close-projects-modal')?.addEventListener('click', closeProjectsPanel);
  $('projects-new-btn')?.addEventListener('click', () => openDetail(null));
  $('projects-search')?.addEventListener('input', renderGallery);
  $('projects-sort')?.addEventListener('change', renderGallery);
  $('projects-modal')?.addEventListener('click', (e) => {
    if (e.target && e.target.id === 'projects-modal') closeProjectsPanel();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const modal = $('projects-modal');
    if (modal && !modal.classList.contains('hidden')) closeProjectsPanel();
  });
}

export function initProjects() {
  wire();
  $('sidebar-projects-btn')?.addEventListener('click', openProjectsPanel);
  $('rail-projects')?.addEventListener('click', openProjectsPanel);
  loadProjects();
}

export default {
  initProjects,
  onSessionSwitch,
  openProjectsPanel,
  closeProjectsPanel,
  openDetail,
  getActiveProject,
  loadProjects,
  createProject,
  updateProject,
  deleteProject,
  previewProject,
  listMemory,
  readMemory,
  writeMemory,
};
