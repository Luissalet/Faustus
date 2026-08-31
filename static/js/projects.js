// Projects: a first-class workspace that keeps chats, a folder on disk,
// standing instructions and durable Markdown memory together.
//
// The backend remains authoritative. routes/chat_routes.py resolves the
// workspace from the selected session and routes/chat_helpers.py injects the
// project context, so stale browser state cannot change the agent boundary.

import Storage from './storage.js';
import uiModule from './ui.js';
import workspaceModule from './workspace.js';

const API = `${window.location.origin}/api/projects`;
const AUTO_KEY = 'odysseus-project-workspace';

let _projects = [];
let _loaded = false;
let _active = null;
let _wired = false;
let _draft = null;
let _galleryTab = 'active';
let _returnFocus = null;

const ICON = {
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  disk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="15" rx="2"/><path d="M7 22h10M12 19v3"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h6"/></svg>',
  pin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 17-5 5M5 3l16 16M16 3l5 5-4 4 1 5-1 1-5-5-4 4-5-5 4-4-2-5 1-1 5 1z"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/></svg>',
  archive: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V8M1 3h22v5H1zM10 12h4"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5a3 3 0 1 0-6 0 4 4 0 0 0-2.5 6 4 4 0 0 0 .5 6.5A4 4 0 0 0 12 18Z"/><path d="M12 5a3 3 0 1 1 6 0 4 4 0 0 1 2.5 6 4 4 0 0 1-.5 6.5A4 4 0 0 1 12 18Z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>',
};

const $ = (id) => document.getElementById(id);
const esc = (value) => (uiModule?.esc ? uiModule.esc(value == null ? '' : String(value)) : String(value == null ? '' : value));

function basename(path) {
  if (!path) return '';
  const parts = String(path).replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function initials(name) {
  return String(name || 'P').trim().split(/\s+/).slice(0, 2).map(part => part[0] || '').join('').toUpperCase() || 'P';
}

function epochMs(value) {
  if (!value) return 0;
  if (typeof value === 'number') return value < 100000000000 ? value * 1000 : value;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function relativeTime(value) {
  const stamp = epochMs(value);
  if (!stamp) return 'No activity yet';
  const seconds = Math.max(0, Math.round((Date.now() - stamp) / 1000));
  if (seconds < 45) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(stamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function formatBytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10240 ? 1 : 0)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function currentModelChoice() {
  const sessions = window.sessionModule?.getSessions?.() || [];
  const currentId = window.sessionModule?.getCurrentSessionId?.() || window.__odysseusLastSelectedSessionId;
  const current = sessions.find(session => session.id === currentId);
  if (current?.endpoint_url && current?.model) {
    return {
      url: current.endpoint_url,
      model: current.model,
      endpointId: current.endpoint_id || '',
    };
  }
  const pending = window.sessionModule?.getPendingChat?.();
  if (pending?.url && pending?.modelId) {
    return {
      url: pending.url,
      model: pending.modelId,
      endpointId: pending.endpointId || '',
    };
  }
  return null;
}

function modelChoicesFromItems(items) {
  const choices = [];
  const seen = new Set();
  (items || []).forEach(item => {
    if (item?.offline) return;
    const models = [...(item.models || []), ...(item.models_extra || [])];
    const labels = [...(item.models_display || []), ...(item.models_extra_display || [])];
    models.forEach((model, index) => {
      const key = `${item.endpoint_id || item.url || ''}::${model}`;
      if (!model || !item.url || seen.has(key)) return;
      seen.add(key);
      choices.push({
        url: item.url,
        model,
        endpointId: item.endpoint_id || '',
        label: labels[index] || model.split('/').pop() || model,
        endpointLabel: item.name || item.endpoint_name || item.provider || '',
      });
    });
  });
  return choices;
}

async function loadProjectModelChoices() {
  let items = window.modelsModule?.getCachedItems?.() || [];
  if (!items.length) {
    try {
      const response = await fetch(`${window.location.origin}/api/models`, { credentials: 'same-origin' });
      if (response.ok) items = (await response.json()).items || [];
    } catch (_) {}
  }
  return modelChoicesFromItems(items);
}

async function populateProjectModelSelect() {
  const select = $('project-model-select');
  if (!select) return;
  const preferred = currentModelChoice();
  const choices = await loadProjectModelChoices();
  if (!select.isConnected) return;
  select.replaceChildren();
  if (!choices.length && preferred) choices.push({
    ...preferred,
    label: preferred.model.split('/').pop() || preferred.model,
    endpointLabel: '',
  });
  if (!choices.length) {
    select.appendChild(new Option('No models available', ''));
    select.disabled = true;
    return;
  }
  choices.forEach(choice => {
    const suffix = choice.endpointLabel ? ` · ${choice.endpointLabel}` : '';
    select.appendChild(new Option(`${choice.label}${suffix}`, JSON.stringify(choice)));
  });
  const preferredIndex = preferred ? choices.findIndex(choice =>
    choice.model === preferred.model && String(choice.url).replace(/\/+$/, '') === String(preferred.url).replace(/\/+$/, '')
  ) : -1;
  select.selectedIndex = preferredIndex >= 0 ? preferredIndex : 0;
  select.disabled = false;
}

function selectedProjectModelChoice() {
  const raw = $('project-model-select')?.value || '';
  if (!raw) return currentModelChoice();
  try { return JSON.parse(raw); } catch (_) { return currentModelChoice(); }
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
    const rows = await req('');
    _projects = (Array.isArray(rows) ? rows : []).map(project => ({
      pinned: false,
      archived: false,
      context_items: [],
      ...project,
    }));
  } catch (_) {
    // Auth-gated/non-admin installs retain the rest of the chat UI.
    _projects = [];
  }
  _loaded = true;
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
export const addProjectContext = (id, path) =>
  req(`/${id}/context`, { method: 'POST', body: JSON.stringify({ path }) });
export const removeProjectContext = (id, itemId) =>
  req(`/${id}/context/${encodeURIComponent(itemId)}`, { method: 'DELETE' });

// ------------------------------------------------------------ active project

function byFolder(folder) {
  const key = String(folder || '').trim().toLocaleLowerCase();
  if (!key) return null;
  return _projects.find(project =>
    String(project.folder || '').trim().toLocaleLowerCase() === key && project.enabled !== false
  ) || null;
}

export function getActiveProject() {
  return _active;
}

function syncWorkspace(project) {
  const applied = Storage.get(AUTO_KEY, '') || '';
  const current = workspaceModule.getWorkspace();
  if (project?.workspace) {
    if (current !== project.workspace) workspaceModule.setWorkspace(project.workspace);
    Storage.set(AUTO_KEY, project.workspace);
    return;
  }
  // Clear only a workspace that Projects applied. A folder chosen manually is
  // user state and must survive navigation to an ordinary chat.
  if (applied && current === applied) workspaceModule.setWorkspace('');
  Storage.remove(AUTO_KEY);
}

function syncProjectIndicators(project) {
  const workspacePill = $('workspace-indicator-btn');
  if (workspacePill && project?.workspace) {
    workspacePill.title = `Project: ${project.name}\nFolder: ${project.workspace}\nOpen Projects to change it.`;
  }

  const pill = $('project-active-pill');
  const label = $('project-active-pill-label');
  if (!pill || !label) return;
  pill.hidden = !project;
  label.textContent = project?.name || '';
  pill.setAttribute('aria-label', project ? `Open project ${project.name}` : 'Open project');
  pill.title = project ? `Open ${project.name}` : 'Open project';
}

/** Called by sessions.js whenever a chat becomes active. */
export async function onSessionSwitch(sessionId, folder) {
  await loadProjects();
  const project = byFolder(folder);
  _active = project;
  syncWorkspace(project);
  syncProjectIndicators(project);
  try {
    document.dispatchEvent(new CustomEvent('project-changed', { detail: { project } }));
  } catch (_) {}
  return project;
}

async function refreshActive() {
  const id = window.__odysseusLastSelectedSessionId;
  if (!id) {
    syncProjectIndicators(_active);
    return;
  }
  try {
    const { project } = await req(`/resolve/session/${encodeURIComponent(id)}`);
    _active = project ? (_projects.find(item => item.id === project.id) || project) : null;
    syncWorkspace(_active);
    syncProjectIndicators(_active);
  } catch (_) {}
}

// ------------------------------------------------------------------ gallery

function chatsIn(project) {
  if (!project) return [];
  try {
    const all = window.sessionModule?.getSessions?.() || [];
    const key = String(project.folder || '').trim().toLocaleLowerCase();
    return all
      .filter(session => String(session.folder || '').trim().toLocaleLowerCase() === key && !session.archived)
      .sort((a, b) => epochMs(b.updated_at || b.created_at) - epochMs(a.updated_at || a.created_at));
  } catch (_) {
    return [];
  }
}

function activityAt(project) {
  return Math.max(epochMs(project.updated_at || project.created_at), ...chatsIn(project).map(chat => epochMs(chat.updated_at || chat.created_at)), 0);
}

function sortProjects(list, mode) {
  return list.slice().sort((a, b) => {
    if (!a.archived && Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1;
    if (mode === 'name') return String(a.name || '').localeCompare(String(b.name || ''));
    if (mode === 'created') return epochMs(b.created_at) - epochMs(a.created_at);
    return activityAt(b) - activityAt(a);
  });
}

function cardHtml(project) {
  const chats = chatsIn(project).length;
  const description = String(project.instructions || '').trim();
  return `
    <div class="project-card-wrap">
      <button type="button" class="project-card${project.pinned ? ' is-pinned' : ''}" data-project="${esc(project.id)}">
        <span class="project-card-top">
          <span class="project-card-avatar">${esc(initials(project.name))}</span>
          ${project.pinned ? `<span class="project-card-pin" title="Pinned">${ICON.pin}</span>` : ''}
        </span>
        <span class="project-card-copy">
          <strong class="project-card-name">${esc(project.name)}</strong>
          <span class="project-card-sub">${description ? esc(description) : 'Add instructions to guide every chat in this project.'}</span>
        </span>
        <span class="project-card-footer">
          <span>${relativeTime(activityAt(project))}</span>
          <span class="project-card-meta">
            <span title="Conversation group">${ICON.folder}${esc(project.folder)}</span>
            ${project.workspace ? `<span title="${esc(project.workspace)}">${ICON.disk}${esc(basename(project.workspace))}</span>` : ''}
            <span title="${chats} recent chat${chats === 1 ? '' : 's'}">${ICON.chat}${chats}</span>
          </span>
        </span>
      </button>
      <button type="button" class="project-card-delete" data-project-delete="${esc(project.id)}" aria-label="Delete ${esc(project.name)}" title="Delete project">${ICON.trash}</button>
    </div>`;
}

function setGalleryTab(tab) {
  _galleryTab = tab === 'archived' ? 'archived' : 'active';
  renderGallery();
}

function renderGallery() {
  const grid = $('projects-grid');
  if (!grid) return;
  $('projects-gallery')?.classList.remove('hidden');
  $('projects-detail')?.classList.add('hidden');

  const active = _projects.filter(project => !project.archived);
  const archived = _projects.filter(project => project.archived);
  const activeTab = $('projects-active-tab');
  const archivedTab = $('projects-archived-tab');
  activeTab?.classList.toggle('active', _galleryTab === 'active');
  archivedTab?.classList.toggle('active', _galleryTab === 'archived');
  activeTab?.setAttribute('aria-selected', String(_galleryTab === 'active'));
  archivedTab?.setAttribute('aria-selected', String(_galleryTab === 'archived'));
  if ($('projects-active-count')) $('projects-active-count').textContent = active.length ? String(active.length) : '';
  if ($('projects-archived-count')) $('projects-archived-count').textContent = archived.length ? String(archived.length) : '';
  if ($('projects-new-btn')) $('projects-new-btn').hidden = _galleryTab === 'archived';

  const query = String($('projects-search')?.value || '').trim().toLocaleLowerCase();
  const mode = $('projects-sort')?.value || 'updated';
  let list = _galleryTab === 'archived' ? archived : active;
  if (query) {
    list = list.filter(project => [
      project.name, project.folder, project.workspace, project.instructions,
      ...(project.context_items || []).map(item => item.path),
    ]
      .some(value => String(value || '').toLocaleLowerCase().includes(query)));
  }
  list = sortProjects(list, mode);

  if (!list.length) {
    const isSearch = Boolean(query);
    const archivedEmpty = _galleryTab === 'archived';
    grid.innerHTML = `
      <div class="projects-empty">
        <span class="projects-empty-icon">${archivedEmpty ? ICON.archive : ICON.folder}</span>
        <h3>${isSearch ? 'No matching projects' : archivedEmpty ? 'No archived projects' : 'Create your first project'}</h3>
        <p>${isSearch ? 'Try a different name, folder or instruction.' : archivedEmpty ? 'Projects you archive will stay available here.' : 'Give related chats a shared folder, instructions and memory.'}</p>
        ${!isSearch && !archivedEmpty ? '<button type="button" class="projects-primary-btn" id="projects-empty-new">New project</button>' : ''}
      </div>`;
    $('projects-empty-new')?.addEventListener('click', () => openSettings(null));
    return;
  }

  grid.innerHTML = list.map(cardHtml).join('');
  grid.querySelectorAll('[data-project]').forEach(card => {
    card.addEventListener('click', () => {
      const project = _projects.find(item => item.id === card.dataset.project);
      if (project) openDetail(project);
    });
  });
  grid.querySelectorAll('[data-project-delete]').forEach(button => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      const project = _projects.find(item => item.id === button.dataset.projectDelete);
      if (project) removeProject(project);
    });
  });
}

// --------------------------------------------------------------- project hub

function chatRowsHtml(project) {
  const chats = chatsIn(project);
  if (!chats.length) {
    return '<div class="project-recents-empty">No chats yet. Start one above and it will stay grouped here.</div>';
  }
  return chats.slice(0, 12).map(chat => `
    <div class="project-chat-row-wrap">
      <button type="button" class="project-chat-row" data-session="${esc(chat.id)}">
        <span class="project-chat-icon">${ICON.chat}</span>
        <span class="grow"><strong>${esc(chat.name || 'Untitled')}</strong><small>${esc(chat.summary || chat.last_message || 'Open conversation')}</small></span>
        <time>${relativeTime(chat.updated_at || chat.created_at)}</time>
      </button>
      <button type="button" class="project-chat-delete" data-chat-delete="${esc(chat.id)}" aria-label="Delete ${esc(chat.name || 'chat')}" title="Delete chat">${ICON.trash}</button>
    </div>`).join('');
}

function hubHtml(project) {
  const instructions = String(project.instructions || '').trim();
  const attached = Array.isArray(project.context_items) ? project.context_items : [];
  const rootRows = [
    project.workspace ? `<div class="project-location-row is-primary">${ICON.disk}<span><strong>${esc(basename(project.workspace))}</strong><small>${esc(project.workspace)} · primary folder</small></span></div>` : '',
    ...attached.map(item => `<div class="project-location-row" data-context-root="${esc(item.id)}">${item.kind === 'folder' ? ICON.folder : ICON.doc}<span><strong>${esc(item.name || basename(item.path))}</strong><small>${esc(item.path)}</small></span><button type="button" class="project-root-remove" data-remove-context="${esc(item.id)}" aria-label="Remove ${esc(item.name || 'root')}" title="Remove from project">×</button></div>`),
  ].filter(Boolean).join('');
  const archivedBanner = project.archived ? `
    <div class="project-archived-banner">${ICON.archive}<span>This project is archived. Its existing chats still keep their context.</span><button type="button" id="project-restore">Restore</button></div>` : '';
  return `
    <div class="project-hub">
      <header class="project-hub-head">
        <button type="button" class="project-icon-btn project-back" id="project-back" aria-label="Back to projects">${ICON.arrow}</button>
        <span class="project-hub-avatar">${esc(initials(project.name))}</span>
        <span class="project-hub-title">
          <h2>${esc(project.name)}</h2>
          <span>${project.workspace ? esc(project.workspace) : `Conversation group · ${esc(project.folder)}`}</span>
        </span>
        <span class="project-hub-actions">
          <button type="button" class="project-icon-btn${project.pinned ? ' active' : ''}" id="project-pin" aria-label="${project.pinned ? 'Unpin project' : 'Pin project'}" title="${project.pinned ? 'Unpin' : 'Pin'}">${ICON.pin}</button>
          <button type="button" class="project-icon-btn" id="project-settings" aria-label="Project settings" title="Project settings">${ICON.edit}</button>
          <button type="button" class="project-icon-btn" id="project-archive" aria-label="${project.archived ? 'Restore project' : 'Archive project'}" title="${project.archived ? 'Restore' : 'Archive'}">${ICON.archive}</button>
        </span>
      </header>
      ${archivedBanner}
      <div class="project-hub-grid">
        <section class="project-hub-main">
          <section class="project-start-card${project.archived ? ' disabled' : ''}">
            <label for="project-chat-input">Start a chat in ${esc(project.name)}</label>
            <textarea id="project-chat-input" rows="4" placeholder="What do you want to work on?" ${project.archived ? 'disabled' : ''}></textarea>
            <div class="project-start-footer">
              <label class="project-start-model" for="project-model-select"><span>Model</span><select id="project-model-select" aria-label="Model for the new project chat"><option value="">Loading models…</option></select></label>
              <button type="button" class="projects-primary-btn project-start-btn" id="project-start-chat" ${project.archived ? 'disabled' : ''}>Start chat ${ICON.send}</button>
            </div>
          </section>

          <section class="project-recents">
            <div class="project-section-head"><div><h3>Recent chats</h3><p>Conversations in ${esc(project.folder)}</p></div>${!project.archived ? '<button type="button" class="project-text-btn" id="project-empty-chat">New chat</button>' : ''}</div>
            <div class="project-chat-list">${chatRowsHtml(project)}</div>
          </section>
        </section>

        <aside class="project-context-column">
          <section class="project-context-card">
            <div class="project-context-head"><div><span class="project-context-icon">${ICON.edit}</span><h3>Instructions</h3></div><button type="button" class="project-mini-btn" id="project-edit-instructions" aria-label="Edit instructions">${instructions ? 'Edit' : 'Add'}</button></div>
            <p class="project-instructions-preview${instructions ? '' : ' empty'}">${instructions ? esc(instructions) : 'Add guidance that should apply to every chat in this project.'}</p>
          </section>

          <section class="project-context-card">
            <div class="project-context-head"><div><span class="project-context-icon">${ICON.memory}</span><h3>Memory</h3></div><button type="button" class="project-mini-btn" id="project-add-memory" aria-label="Add memory note">Add</button></div>
            <div id="project-memory-list" class="project-memory-list"><p class="project-muted">Loading memory…</p></div>
          </section>

          <section class="project-context-card project-context-location">
            <div class="project-context-head"><div><span class="project-context-icon">${ICON.folder}</span><h3>Work roots</h3></div><button type="button" class="project-mini-btn" id="project-add-context" aria-label="Add a work file or folder">Add</button></div>
            <p class="project-context-help">The agent can read and modify every file or folder listed here.</p>
            <div class="project-roots-list">${rootRows || '<p class="project-muted">Add a primary folder or another file/folder to start working.</p>'}</div>
            <button type="button" class="project-text-btn project-primary-folder-link" id="project-edit-context">${project.workspace ? 'Change primary folder' : 'Set primary folder'}</button>
          </section>

          <button type="button" class="project-context-preview-btn" id="project-context-preview">Show exactly what the model sees</button>
        </aside>
      </div>
    </div>`;
}

export function openDetail(project) {
  if (!project?.id) {
    openSettings(null);
    return;
  }
  _draft = project;
  const host = $('projects-detail');
  if (!host) return;
  $('projects-gallery')?.classList.add('hidden');
  host.classList.remove('hidden');
  host.innerHTML = hubHtml(project);

  $('project-back')?.addEventListener('click', renderGallery);
  $('project-settings')?.addEventListener('click', () => openSettings(project));
  $('project-edit-instructions')?.addEventListener('click', () => openSettings(project, 'project-instructions'));
  $('project-edit-context')?.addEventListener('click', () => openSettings(project, 'project-workspace'));
  $('project-add-context')?.addEventListener('click', () => addContextRoot(project));
  $('project-pin')?.addEventListener('click', () => setProjectFlags(project, { pinned: !project.pinned }, project.pinned ? 'Project unpinned' : 'Project pinned'));
  $('project-archive')?.addEventListener('click', () => setArchived(project, !project.archived));
  $('project-restore')?.addEventListener('click', () => setArchived(project, false));
  $('project-context-preview')?.addEventListener('click', () => openContextPreview(project));
  $('project-add-memory')?.addEventListener('click', () => {
    if (!project.workspace) {
      uiModule.showToast?.('Connect a project folder before adding memory');
      openSettings(project, 'project-workspace');
      return;
    }
    openMemoryFile(project, '');
  });
  $('project-start-chat')?.addEventListener('click', () => newChatInProject(project, $('project-chat-input')?.value || ''));
  $('project-empty-chat')?.addEventListener('click', () => newChatInProject(project, ''));
  $('project-chat-input')?.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      newChatInProject(project, event.currentTarget.value || '');
    }
  });
  host.querySelectorAll('[data-session]').forEach(row => {
    row.addEventListener('click', async () => {
      closeProjectsPanel();
      await window.sessionModule?.selectSession?.(row.dataset.session);
    });
  });
  host.querySelectorAll('[data-remove-context]').forEach(button => {
    button.addEventListener('click', () => removeContextRoot(project, button.dataset.removeContext));
  });
  host.querySelectorAll('[data-chat-delete]').forEach(button => {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      await removeProjectChat(project, button.dataset.chatDelete);
    });
  });
  renderMemoryList(project);
  if (!project.archived) populateProjectModelSelect();
}

async function renderMemoryList(project) {
  const host = $('project-memory-list');
  if (!host) return;
  if (!project.workspace) {
    host.innerHTML = '<p class="project-muted">No folder connected, so this project has no file-backed memory yet.</p>';
    return;
  }
  try {
    const { files } = await listMemory(project.id);
    if (!files.length) {
      host.innerHTML = '<p class="project-muted">No memory notes yet. Add one when a decision should survive into the next chat.</p>';
      return;
    }
    host.innerHTML = files.slice(0, 8).map(file => `
      <button type="button" class="project-memory-row" data-memory-file="${esc(file.name)}">
        ${ICON.doc}<span>${esc(file.name)}</span><small>${formatBytes(file.size)}</small>
      </button>`).join('');
    host.querySelectorAll('[data-memory-file]').forEach(button => {
      button.addEventListener('click', () => openMemoryFile(project, button.dataset.memoryFile));
    });
  } catch (error) {
    host.innerHTML = `<p class="project-muted">Could not read project memory: ${esc(error.message || error)}</p>`;
  }
}

// ----------------------------------------------------------- settings/editor

function settingsHtml(project, isNew) {
  const value = project || { name: '', folder: '', workspace: '', instructions: '' };
  return `
    <div class="project-settings-view">
      <header class="project-subview-head">
        <button type="button" class="project-icon-btn" id="project-settings-back" aria-label="Go back">${ICON.arrow}</button>
        <div><h2>${isNew ? 'New project' : 'Project settings'}</h2><p>${isNew ? 'Create a focused home for related work.' : esc(value.name)}</p></div>
      </header>
      <div class="project-settings-grid">
        <form class="project-settings-form" id="project-settings-form">
          <div class="project-field">
            <label for="project-name">Name <span>Required</span></label>
            <input type="text" id="project-name" value="${esc(value.name)}" placeholder="My project" maxlength="80" autocomplete="off" spellcheck="false" />
          </div>
          <div class="project-field">
            <label for="project-folder">Conversation group</label>
            <input type="text" id="project-folder" value="${esc(isNew ? '' : value.folder)}" placeholder="Created from the project name" readonly />
            <p>${isNew ? 'A matching group is created automatically in the chat sidebar.' : 'This stable group keeps existing chats attached when you rename the project.'}</p>
          </div>
          <div class="project-field">
            <label for="project-workspace">Primary working folder <span>Optional</span></label>
            <div class="project-input-row">
              <input type="text" id="project-workspace" value="${esc(value.workspace)}" placeholder="D:\\Projects\\my-project" spellcheck="false" />
              <button type="button" class="projects-secondary-btn" id="project-pick">Browse…</button>
            </div>
            <p>Relative paths and terminal commands start here. You can attach more working files and folders from the project page.</p>
          </div>
          <div class="project-field">
            <label for="project-instructions">Instructions <span>Optional</span></label>
            <textarea id="project-instructions" rows="10" maxlength="10000" placeholder="How should Odysseus work in this project?">${esc(value.instructions)}</textarea>
            <p id="project-cost">Sent with every message in this project.</p>
          </div>
          <div class="project-form-actions">
            <button type="button" class="projects-secondary-btn" id="project-settings-cancel">Cancel</button>
            <button type="submit" class="projects-primary-btn" id="project-save">${isNew ? 'Create project' : 'Save changes'}</button>
          </div>
        </form>
        <aside class="project-settings-aside">
          <div class="project-settings-note">
            <span class="project-context-icon">${ICON.folder}</span>
            <h3>One place for the whole job</h3>
            <p>Chats share instructions, memory, previous conversations and every attached working root. File contents stay on disk and are loaded only when needed.</p>
          </div>
          ${isNew ? '' : `
            <div class="project-danger-zone">
              <h3>Project actions</h3>
              <button type="button" class="projects-secondary-btn" id="project-settings-archive">${value.archived ? 'Restore project' : 'Archive project'}</button>
              <button type="button" class="projects-danger-btn" id="project-delete">Delete project</button>
              <p>Deleting the project never removes its folder or memory files from disk.</p>
            </div>`}
        </aside>
      </div>
    </div>`;
}

function updateInstructionCost() {
  const output = $('project-cost');
  if (!output) return;
  const chars = String($('project-instructions')?.value || '').length;
  output.textContent = chars
    ? `${chars.toLocaleString()} characters · roughly ${Math.ceil(chars / 4).toLocaleString()} tokens on every turn.`
    : 'Sent with every message in this project.';
}

function openSettings(project, focusId = '') {
  const isNew = !project?.id;
  _draft = project || { name: '', folder: '', workspace: '', instructions: '' };
  const host = $('projects-detail');
  if (!host) return;
  $('projects-gallery')?.classList.add('hidden');
  host.classList.remove('hidden');
  host.innerHTML = settingsHtml(_draft, isNew);

  const goBack = () => isNew ? renderGallery() : openDetail(_projects.find(item => item.id === _draft.id) || _draft);
  $('project-settings-back')?.addEventListener('click', goBack);
  $('project-settings-cancel')?.addEventListener('click', goBack);
  $('project-name')?.addEventListener('input', event => {
    if (isNew && $('project-folder')) $('project-folder').value = event.currentTarget.value.trim();
  });
  $('project-instructions')?.addEventListener('input', updateInstructionCost);
  updateInstructionCost();

  $('project-pick')?.addEventListener('click', () => {
    workspaceModule.openWorkspaceBrowser(path => {
      if ($('project-workspace')) $('project-workspace').value = path;
    });
  });

  $('project-settings-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const name = String($('project-name')?.value || '').trim();
    if (!name) {
      uiModule.showError?.('The project needs a name');
      $('project-name')?.focus();
      return;
    }
    const body = {
      name,
      // The folder is deliberately stable for an existing project. It is the
      // no-migration binding used by sessions, not a second display name.
      folder: isNew ? name : _draft.folder,
      workspace: String($('project-workspace')?.value || '').trim(),
      instructions: String($('project-instructions')?.value || ''),
    };
    const save = $('project-save');
    if (save) { save.disabled = true; save.textContent = isNew ? 'Creating…' : 'Saving…'; }
    try {
      const saved = isNew ? await createProject(body) : await updateProject(_draft.id, body);
      await loadProjects(true);
      const current = _projects.find(item => item.id === saved.id) || saved;
      uiModule.showToast?.(isNew ? `Project “${saved.name}” created` : 'Project saved');
      await refreshActive();
      openDetail(current);
    } catch (error) {
      uiModule.showError?.(String(error.message || error));
      if (save) { save.disabled = false; save.textContent = isNew ? 'Create project' : 'Save changes'; }
    }
  });

  $('project-settings-archive')?.addEventListener('click', () => setArchived(_draft, !_draft.archived));
  $('project-delete')?.addEventListener('click', () => removeProject(_draft));
  if (focusId) setTimeout(() => $(focusId)?.focus(), 0);
  else setTimeout(() => $('project-name')?.focus(), 0);
}

async function setProjectFlags(project, updates, toast) {
  try {
    const saved = await updateProject(project.id, updates);
    await loadProjects(true);
    const current = _projects.find(item => item.id === saved.id) || saved;
    if (toast) uiModule.showToast?.(toast);
    await refreshActive();
    openDetail(current);
  } catch (error) {
    uiModule.showError?.(String(error.message || error));
  }
}

async function setArchived(project, archived) {
  await setProjectFlags(project, { archived }, archived ? 'Project archived' : 'Project restored');
}

async function removeProject(project) {
  const ok = await uiModule.styledConfirm?.(
    `Delete “${project.name}”? Its folder and memory files will stay on disk.`,
    { title: 'Delete project', confirmText: 'Delete', cancelText: 'Cancel', danger: true },
  );
  if (!ok) return;
  try {
    await deleteProject(project.id);
    await loadProjects(true);
    if (_active?.id === project.id) _active = null;
    await refreshActive();
    uiModule.showToast?.('Project deleted. Its files were left untouched.');
    renderGallery();
  } catch (error) {
    uiModule.showError?.(String(error.message || error));
  }
}

async function addContextRoot(project) {
  workspaceModule.openWorkspaceBrowser(async path => {
    try {
      await addProjectContext(project.id, path);
      await loadProjects(true);
      const current = _projects.find(item => item.id === project.id) || project;
      await refreshActive();
      uiModule.showToast?.(`${basename(path)} added as a project work root`);
      openDetail(current);
    } catch (error) {
      uiModule.showError?.(String(error.message || error));
    }
  }, {
    includeFiles: true,
    title: 'Add project work root',
    useLabel: 'Add this folder',
  });
}

async function removeContextRoot(project, itemId) {
  try {
    await removeProjectContext(project.id, itemId);
    await loadProjects(true);
    const current = _projects.find(item => item.id === project.id) || project;
    await refreshActive();
    uiModule.showToast?.('Work root removed from project; files were left untouched');
    openDetail(current);
  } catch (error) {
    uiModule.showError?.(String(error.message || error));
  }
}

async function removeProjectChat(project, chatId) {
  if (!chatId) return;
  const chat = chatsIn(project).find(item => item.id === chatId);
  const label = chat?.name ? `“${chat.name}”` : 'this chat';
  const ok = await window.sessionModule?.deleteSessionById?.(chatId, {
    confirmMessage: `Delete ${label}? This cannot be undone.`,
  });
  if (!ok) return;
  const current = _projects.find(item => item.id === project.id) || project;
  openDetail(current);
}

// ----------------------------------------------------------- memory/context

async function openMemoryFile(project, name) {
  const isNew = !name;
  let content = '';
  if (!isNew) {
    try {
      content = (await readMemory(project.id, name)).content || '';
    } catch (error) {
      uiModule.showError?.(String(error.message || error));
      return;
    }
  }
  const host = $('projects-detail');
  if (!host) return;
  host.innerHTML = `
    <div class="project-editor-view">
      <header class="project-subview-head">
        <button type="button" class="project-icon-btn" id="memory-back" aria-label="Back to project">${ICON.arrow}</button>
        <div><h2>${isNew ? 'New memory note' : esc(name)}</h2><p>${esc(project.name)} · durable project memory</p></div>
      </header>
      <div class="project-editor-card">
        ${isNew ? '<div class="project-field"><label for="memory-filename">File name</label><input type="text" id="memory-filename" value="" placeholder="decisions.md" spellcheck="false" autocomplete="off" /><p>Markdown only. Use a short descriptive name such as <code>decisions.md</code>.</p></div>' : ''}
        <label class="project-editor-label" for="memory-editor">Markdown</label>
        <textarea id="memory-editor" class="project-memory-editor" spellcheck="false">${esc(content)}</textarea>
        <div class="project-form-actions">
          <button type="button" class="projects-secondary-btn" id="memory-cancel">Cancel</button>
          <button type="button" class="projects-primary-btn" id="memory-save">Save memory</button>
        </div>
      </div>
    </div>`;
  const back = () => openDetail(_projects.find(item => item.id === project.id) || project);
  $('memory-back')?.addEventListener('click', back);
  $('memory-cancel')?.addEventListener('click', back);
  $('memory-save')?.addEventListener('click', async () => {
    const filename = isNew ? String($('memory-filename')?.value || '').trim() : name;
    if (!/^[A-Za-z0-9._-]{1,120}\.md$/.test(filename)) {
      uiModule.showError?.('Use a Markdown filename such as decisions.md');
      $('memory-filename')?.focus();
      return;
    }
    try {
      if (isNew) {
        const existing = await listMemory(project.id);
        if ((existing.files || []).some(file => String(file.name).toLocaleLowerCase() === filename.toLocaleLowerCase())) {
          throw new Error(`${filename} already exists. Open that note instead.`);
        }
      }
      await writeMemory(project.id, filename, $('memory-editor')?.value || '');
      await loadProjects(true);
      uiModule.showToast?.(`${filename} saved`);
      back();
    } catch (error) {
      uiModule.showError?.(String(error.message || error));
    }
  });
  setTimeout(() => (isNew ? $('memory-filename') : $('memory-editor'))?.focus(), 0);
}

async function openContextPreview(project) {
  try {
    const { block, chars } = await previewProject(project.id);
    const host = $('projects-detail');
    if (!host) return;
    host.innerHTML = `
      <div class="project-editor-view">
        <header class="project-subview-head">
          <button type="button" class="project-icon-btn" id="context-back" aria-label="Back to project">${ICON.arrow}</button>
          <div><h2>Model context</h2><p>${Number(chars || 0).toLocaleString()} characters sent before each message</p></div>
        </header>
        <div class="project-editor-card">
          <p class="project-context-explainer">This is the exact project block Odysseus prepends to the chat. It combines work roots, instructions, chat lookup guidance and the short memory index.</p>
          <textarea class="project-memory-editor project-context-preview" readonly spellcheck="false">${esc(block || '')}</textarea>
        </div>
      </div>`;
    $('context-back')?.addEventListener('click', () => openDetail(_projects.find(item => item.id === project.id) || project));
  } catch (error) {
    uiModule.showError?.(String(error.message || error));
  }
}

// --------------------------------------------------------------- new chat

async function newChatInProject(project, initialPrompt = '') {
  const button = $('project-start-chat') || $('project-empty-chat');
  if (button) button.disabled = true;
  try {
    const choice = selectedProjectModelChoice();
    const endpointUrl = choice?.url || window.sessionModule?.getCurrentEndpointUrl?.() || '';
    const modelId = choice?.model || window.sessionModule?.getCurrentModel?.() || '';
    if (!endpointUrl) {
      throw new Error('Choose a model in the chat before starting a project conversation.');
    }
    const cleanPrompt = String(initialPrompt || '').trim();
    const title = cleanPrompt ? cleanPrompt.split(/\r?\n/)[0].slice(0, 80) : `New chat · ${project.name}`;
    const form = new FormData();
    form.append('name', title);
    form.append('endpoint_url', endpointUrl);
    form.append('model', modelId);
    if (choice?.endpointId) form.append('endpoint_id', choice.endpointId);
    form.append('skip_validation', 'true');
    const created = await fetch(`${window.location.origin}/api/session`, {
      method: 'POST', body: form, credentials: 'same-origin',
    });
    if (!created.ok) throw new Error(`Could not create the chat (HTTP ${created.status})`);
    const payload = await created.json();
    const sessionId = payload.session_id || payload.id;
    if (!sessionId) throw new Error('The new chat did not return a session id');

    const move = new FormData();
    move.append('folder', project.folder);
    const moved = await fetch(`${window.location.origin}/api/session/${encodeURIComponent(sessionId)}`, {
      method: 'PATCH', body: move, credentials: 'same-origin',
    });
    if (!moved.ok) throw new Error(`Could not attach the chat to the project (HTTP ${moved.status})`);

    // Empty PATCH intentionally touches updated_at, keeping Last updated useful
    // when the activity was a new chat rather than a settings edit.
    await updateProject(project.id, {}).catch(() => null);
    closeProjectsPanel();
    await window.sessionModule?.loadSessions?.();
    await window.sessionModule?.selectSession?.(sessionId);

    const composer = $('message');
    if (cleanPrompt && composer) {
      composer.value = cleanPrompt;
      composer.dispatchEvent(new Event('input', { bubbles: true }));
      const chatForm = $('chat-form');
      if (chatForm?.requestSubmit) chatForm.requestSubmit();
      else chatForm?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    } else {
      composer?.focus();
    }
  } catch (error) {
    uiModule.showError?.(String(error.message || error));
    if (button) button.disabled = false;
  }
}

// -------------------------------------------------------------------- panel

export async function openProjectsPanel(projectId = '') {
  const modal = $('projects-modal');
  if (!modal) return;
  wire();
  _returnFocus = document.activeElement;
  modal.classList.remove('hidden');
  document.body.classList.add('projects-open');
  await loadProjects(true);
  const requested = projectId ? _projects.find(project => project.id === projectId) : null;
  if (requested) openDetail(requested);
  else renderGallery();
  if (!requested) $('projects-search')?.focus();
}

export function closeProjectsPanel() {
  $('projects-modal')?.classList.add('hidden');
  document.body.classList.remove('projects-open');
  try { _returnFocus?.focus?.(); } catch (_) {}
}

function wire() {
  if (_wired) return;
  _wired = true;
  $('close-projects-modal')?.addEventListener('click', closeProjectsPanel);
  $('projects-new-btn')?.addEventListener('click', () => openSettings(null));
  $('projects-search')?.addEventListener('input', renderGallery);
  $('projects-sort')?.addEventListener('change', renderGallery);
  $('projects-active-tab')?.addEventListener('click', () => setGalleryTab('active'));
  $('projects-archived-tab')?.addEventListener('click', () => setGalleryTab('archived'));
  $('sidebar-projects-btn')?.addEventListener('click', () => openProjectsPanel());
  $('rail-projects')?.addEventListener('click', () => openProjectsPanel());
  $('project-active-pill')?.addEventListener('click', () => openProjectsPanel(_active?.id || ''));
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const confirm = $('styled-confirm-overlay');
    if (confirm && !confirm.classList.contains('hidden') && confirm.style.display !== 'none') return;
    const modal = $('projects-modal');
    if (modal && !modal.classList.contains('hidden')) closeProjectsPanel();
  });
}

export function initProjects() {
  wire();
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
  addProjectContext,
  removeProjectContext,
};
