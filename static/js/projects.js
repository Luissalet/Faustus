//
// Projects: one object binding a sidebar chat folder, a workspace folder on
// disk, standing instructions, and a Markdown memory kept inside that folder.
//
// The backend is the authority — routes/chat_routes.py resolves the workspace
// from the session and routes/chat_helpers.py prepends the instructions, so a
// stale tab can never send the agent to the wrong folder. Everything here is
// presentation: keep the workspace pill honest, and give the user somewhere to
// edit a project.
//
// Deliberately no new CSS: reuses the existing .modal / .confirm-btn /
// .styled-prompt-input classes so it inherits every theme.

import Storage from './storage.js';
import uiModule from './ui.js';
import workspaceModule from './workspace.js';
import { makeWindowDraggable } from './windowDrag.js';

const API = `${window.location.origin}/api/projects`;

// Which workspace WE applied. Without this, leaving a project chat for a
// project-less one would silently leave the agent pointed at the old project's
// folder — while a workspace the user set by hand must survive the switch.
const AUTO_KEY = 'odysseus-project-workspace';

let _projects = [];
let _loaded = false;
let _active = null;
let _modal = null;
let _editingId = null;

// ---------------------------------------------------------------- API client

async function _req(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

export async function loadProjects(force = false) {
  if (_loaded && !force) return _projects;
  try {
    _projects = await _req('');
    _loaded = true;
  } catch (e) {
    // Non-admin or auth-gated installs just don't get projects. Fail quiet:
    // this runs on every session switch.
    _projects = [];
    _loaded = true;
  }
  return _projects;
}

export const createProject = (body) => _req('', { method: 'POST', body: JSON.stringify(body) });
export const updateProject = (id, body) => _req(`/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
export const deleteProject = (id) => _req(`/${id}`, { method: 'DELETE' });
export const previewProject = (id) => _req(`/${id}/preview`);
export const listMemory = (id) => _req(`/${id}/memory`);
export const readMemory = (id, name) => _req(`/${id}/memory/${encodeURIComponent(name)}`);
export const writeMemory = (id, name, content) =>
  _req(`/${id}/memory/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ content }) });

// ------------------------------------------------------------ active project

function _byFolder(folder) {
  const key = (folder || '').trim().toLowerCase();
  if (!key) return null;
  return _projects.find(p => (p.folder || '').trim().toLowerCase() === key && p.enabled !== false) || null;
}

export function getActiveProject() {
  return _active;
}

function _syncWorkspace(project) {
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

function _syncPillTitle(project) {
  const pill = document.getElementById('workspace-indicator-btn');
  if (!pill) return;
  if (project && project.workspace) {
    pill.title =
      `Project: ${project.name}\nFolder: ${project.workspace}\n` +
      'Set by the project, so it follows this chat. Edit it in the project settings.';
  }
}

/** Called from sessions.js when the user opens a chat. */
export async function onSessionSwitch(sessionId, folder) {
  await loadProjects();
  const project = _byFolder(folder);
  _active = project;
  _syncWorkspace(project);
  _syncPillTitle(project);
  try {
    document.dispatchEvent(new CustomEvent('project-changed', { detail: { project } }));
  } catch (_) {}
  return project;
}

// -------------------------------------------------------------------- panel

function _esc(s) { return uiModule && uiModule.esc ? uiModule.esc(s || '') : String(s || ''); }

function _listHtml() {
  if (!_projects.length) {
    return '<div class="workspace-empty">No projects yet. Create one to bind a chat folder to a folder on disk.</div>';
  }
  return _projects.map(p => `
    <div class="workspace-row" data-project="${_esc(p.id)}" style="justify-content:space-between;gap:10px">
      <span style="display:flex;flex-direction:column;min-width:0">
        <strong>${_esc(p.name)}</strong>
        <span class="muted" style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${_esc(p.folder)} → ${_esc(p.workspace || 'no folder bound')}
        </span>
      </span>
      <button type="button" class="confirm-btn confirm-btn-secondary" data-edit="${_esc(p.id)}">Edit</button>
    </div>`).join('');
}

function _formHtml(p) {
  const v = p || { name: '', folder: '', workspace: '', instructions: '' };
  return `
    <label class="muted" style="display:block;margin:8px 0 2px">Name</label>
    <input type="text" class="styled-prompt-input" id="project-name" value="${_esc(v.name)}"
           placeholder="Covernet" spellcheck="false" />

    <label class="muted" style="display:block;margin:10px 0 2px">Chat folder in the sidebar</label>
    <input type="text" class="styled-prompt-input" id="project-folder" value="${_esc(v.folder)}"
           placeholder="Same as the name if you leave it blank" spellcheck="false" />

    <label class="muted" style="display:block;margin:10px 0 2px">Project folder on disk</label>
    <div style="display:flex;gap:6px">
      <input type="text" class="styled-prompt-input" id="project-workspace" style="flex:1"
             value="${_esc(v.workspace)}" placeholder="D:\\Proyectos\\covernet" spellcheck="false" />
      <button type="button" class="confirm-btn confirm-btn-secondary" id="project-pick">Browse…</button>
    </div>
    <p class="muted" style="margin:6px 0 0;font-size:11px">
      File tools are confined here in every chat of this project, and the project's memory lives in
      <code>.odysseus/</code> inside it. Shell commands start here but are not sandboxed.
    </p>

    <label class="muted" style="display:block;margin:12px 0 2px">Instructions</label>
    <textarea class="styled-prompt-input" id="project-instructions" rows="7"
              style="width:100%;resize:vertical;font-family:inherit"
              placeholder="Standing rules for every chat in this project.">${_esc(v.instructions)}</textarea>
    <p class="muted" id="project-cost" style="margin:6px 0 0;font-size:11px"></p>`;
}

function _getModal() {
  if (_modal) return _modal;
  _modal = document.createElement('div');
  _modal.id = 'projects-modal';
  _modal.className = 'modal';
  _modal.style.display = 'none';
  _modal.innerHTML = `
    <div class="modal-content">
      <div class="modal-header">
        <h4 id="projects-title">Projects</h4>
        <button class="close-btn" id="projects-close" aria-label="Close">✖</button>
      </div>
      <div class="modal-body" id="projects-body"></div>
      <div class="modal-footer workspace-footer" id="projects-footer"></div>
    </div>`;
  document.body.appendChild(_modal);
  _modal.querySelector('#projects-close').addEventListener('click', closeProjectsPanel);
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(_modal, { content, header });
  return _modal;
}

function _renderList() {
  const m = _getModal();
  _editingId = null;
  m.querySelector('#projects-title').textContent = 'Projects';
  m.querySelector('#projects-body').innerHTML = _listHtml();
  m.querySelector('#projects-footer').innerHTML =
    '<button type="button" class="confirm-btn confirm-btn-primary" id="projects-new">New project</button>';
  m.querySelector('#projects-new').addEventListener('click', () => _renderForm(null));
  m.querySelectorAll('[data-edit]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _renderForm(_projects.find(p => p.id === btn.dataset.edit) || null);
    });
  });
}

function _updateCost() {
  const m = _getModal();
  const el = m.querySelector('#project-cost');
  if (!el) return;
  const chars = (m.querySelector('#project-instructions')?.value || '').length;
  // Rough but useful: with a 32k local context, knowing the standing block is
  // 4k characters is the difference between "it works" and "it forgot the
  // system prompt three turns in".
  el.textContent = chars ? `${chars} characters — roughly ${Math.ceil(chars / 4)} tokens on every turn of this project.` : '';
}

function _renderForm(project) {
  const m = _getModal();
  _editingId = project ? project.id : null;
  m.querySelector('#projects-title').textContent = project ? `Edit ${project.name}` : 'New project';
  m.querySelector('#projects-body').innerHTML = _formHtml(project);
  m.querySelector('#projects-footer').innerHTML = `
    ${project ? '<button type="button" class="confirm-btn confirm-btn-secondary" id="project-delete">Delete</button>' : ''}
    <button type="button" class="confirm-btn confirm-btn-secondary" id="project-back">Back</button>
    <button type="button" class="confirm-btn confirm-btn-primary" id="project-save">Save</button>`;

  m.querySelector('#project-back').addEventListener('click', _renderList);
  m.querySelector('#project-instructions').addEventListener('input', _updateCost);
  _updateCost();

  m.querySelector('#project-pick').addEventListener('click', () => {
    // Borrow the existing directory browser and write its pick into the field
    // instead of binding it globally.
    workspaceModule.openWorkspaceBrowser((path) => {
      const input = m.querySelector('#project-workspace');
      if (input) input.value = path;
    });
  });

  m.querySelector('#project-save').addEventListener('click', async () => {
    const body = {
      name: m.querySelector('#project-name').value.trim(),
      folder: m.querySelector('#project-folder').value.trim(),
      workspace: m.querySelector('#project-workspace').value.trim(),
      instructions: m.querySelector('#project-instructions').value,
    };
    if (!body.name) { uiModule.showError?.('The project needs a name'); return; }
    try {
      if (_editingId) await updateProject(_editingId, body);
      else await createProject(body);
      await loadProjects(true);
      uiModule.showToast?.(_editingId ? 'Project saved' : `Project "${body.name}" created`);
      _renderList();
      _refreshActive();
    } catch (e) {
      uiModule.showError?.(String(e.message || e));
    }
  });

  const del = m.querySelector('#project-delete');
  if (del) {
    del.addEventListener('click', async () => {
      // Two-step rather than a confirm() dialog: window.confirm blocks the
      // whole page and this codebase avoids it elsewhere too.
      if (del.dataset.armed !== '1') {
        del.dataset.armed = '1';
        del.textContent = 'Really delete?';
        setTimeout(() => { del.dataset.armed = '0'; del.textContent = 'Delete'; }, 4000);
        return;
      }
      try {
        await deleteProject(_editingId);
        await loadProjects(true);
        uiModule.showToast?.('Project deleted. Its folder and files are untouched.');
        _renderList();
        _refreshActive();
      } catch (e) {
        uiModule.showError?.(String(e.message || e));
      }
    });
  }
}

async function _refreshActive() {
  // Re-resolve the current chat so an edit takes effect without a switch.
  const id = window.__odysseusLastSelectedSessionId;
  if (!id) return;
  try {
    const { project } = await _req(`/resolve/session/${encodeURIComponent(id)}`);
    _active = project ? _projects.find(p => p.id === project.id) || project : null;
    _syncWorkspace(_active);
    _syncPillTitle(_active);
  } catch (_) {}
}

export async function openProjectsPanel() {
  await loadProjects(true);
  const m = _getModal();
  m.style.display = 'flex';
  _renderList();
}

export function closeProjectsPanel() {
  if (_modal) _modal.style.display = 'none';
}

export function initProjects() {
  loadProjects();
}

export default {
  initProjects,
  onSessionSwitch,
  openProjectsPanel,
  closeProjectsPanel,
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
