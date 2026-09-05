import { Activity, Archive, ArchiveRestore, ArrowLeft, Brain, Check, Download, Eye, FileText, FolderOpen, FolderPlus, MessageSquare, Pin, PinOff, Plus, Send, Settings2, Target, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, Menu, Skeleton, Toast } from '../components';
import { listModels, type ChatSession, type ModelRoute } from '../adapters/chat';
import { pickNative } from '../adapters/composer';
import { relativeTime } from '../adapters/home';
import {
  addContextRoot,
  AGENT_FLAGS,
  chatsIn,
  deleteProject,
  exportProjectUrl,
  flagOn,
  getContextPreview,
  getProject,
  removeChatFromProject,
  removeContextRoot,
  startChatInProject,
  updateProject,
  type Project,
} from '../adapters/projects';
import { EXPORT_FORMATS } from '../adapters/sessions';
import { ProjectAudit } from './project/Audit';
import { ProjectMemoryFiles } from './project/Memory';
import { ProjectObjectives } from './project/Objectives';
import { ProjectSettings } from './project/Settings';
import './projects.css';
import './home.css';
import { t, tn } from '../i18n';

const TABS = [
  { id: 'brief', label: 'Brief', icon: FileText },
  { id: 'chats', label: 'Chats', icon: MessageSquare },
  { id: 'objetivos', label: 'Objectives', icon: Target },
  { id: 'memoria', label: 'Memory', icon: Brain },
  { id: 'actividad', label: 'Agent activity', icon: Activity },
  { id: 'contexto', label: 'Context', icon: Eye },
  { id: 'ajustes', label: 'Settings', icon: Settings2 },
] as const;

type TabId = (typeof TABS)[number]['id'];

const FORMAT_LABEL: Record<string, string> = { md: 'Markdown', txt: 'Plain text', json: 'JSON', html: 'HTML', pdf: 'PDF', docx: 'Word (.docx)' };

/**
 * A project as a page (UI-040), and now the whole of it: start a chat
 * here, its chats, objectives, memory files, what the agent changed, the
 * context block the model receives, and the settings. `/projects/new`
 * is the same page with only the form.
 */
export function ProjectScreen() {
  const { projectId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const creating = projectId === 'new';
  const [project, setProject] = useState<Project | null>(null);
  const [failed, setFailed] = useState(false);
  const [chats, setChats] = useState<ChatSession[] | null>(null);
  const [context, setContext] = useState<string | null>(null);
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [routeId, setRouteId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<'delete' | { chat: string } | null>(null);
  const [rootInput, setRootInput] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const rawTab = params.get('tab');
  const tab: TabId = creating ? 'ajustes' : TABS.some((x) => x.id === rawTab) ? (rawTab as TabId) : 'brief';

  const reload = useCallback(async () => {
    if (creating) return;
    try {
      const p = await getProject(projectId);
      setProject(p);
      setFailed(false);
      void chatsIn(p).then(setChats).catch(() => setChats([]));
      void getContextPreview(projectId).then(setContext).catch(() => setContext(''));
    } catch {
      setFailed(true);
    }
  }, [projectId, creating]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    void listModels()
      .then((list) => {
        setRoutes(list);
        let last = '';
        try {
          last = (JSON.parse(localStorage.getItem('faustus_studio_route') ?? '{}') as { id?: string }).id ?? '';
        } catch {
          /* private mode */
        }
        setRouteId((id) => id || (list.some((r) => r.id === last) ? last : (list[0]?.id ?? '')));
      })
      .catch(() => setRoutes([]));
  }, []);

  const setTab = (id: TabId) => {
    const next = new URLSearchParams(params);
    next.set('tab', id);
    setParams(next);
  };

  const flags = useMemo(() => (project ? AGENT_FLAGS.map((f) => ({ ...f, on: flagOn(project, f.key) })) : []), [project]);

  const start = async () => {
    if (!project) return;
    setBusy('start');
    try {
      const sid = await startChatInProject(project, routes.find((r) => r.id === routeId) ?? null, prompt);
      const q = new URLSearchParams({ s: sid });
      if (prompt.trim()) {
        q.set('draft', prompt.trim());
        q.set('send', '1');
      }
      navigate(`/studio?${q}`);
    } catch (e) {
      say((e as Error).message);
      setBusy(null);
    }
  };

  const patch = async (key: string, input: Parameters<typeof updateProject>[1], done: string) => {
    if (!project) return;
    setBusy(key);
    try {
      setProject(await updateProject(project.id, input));
      say(done);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const addRootPath = async (path: string) => {
    if (!project || !path.trim()) return;
    try {
      await addContextRoot(project.id, path.trim());
      setRootInput(null);
      await reload();
      say(t('Added'));
    } catch (e) {
      say((e as Error).message);
    }
  };

  const addRoot = async () => {
    if (!project) return;
    try {
      const pick = await pickNative('folder', project.workspace ?? '');
      if (pick.status === 'ok' && pick.path) await addRootPath(pick.path);
      // No system dialog (remote browser): type the path instead.
      else if (pick.status === 'unavailable') setRootInput('');
    } catch (e) {
      say((e as Error).message);
    }
  };

  if (creating) {
    return (
      <div className="fs-screen fs-pj" data-testid="project-new">
        <div>
          <Link to="/projects" className="fs-tab" style={{ paddingInline: 0 }}>
            <ArrowLeft size={14} aria-hidden="true" /> {t('Projects')}
          </Link>
        </div>
        <header className="fs-screen__head">
          <div>
            <h1 className="fs-screen__title">{t('New project')}</h1>
            <p className="fs-prose fs-pj__lede">{t('A project groups a folder, instructions, memory and conversations. The agent follows its manners inside that folder.')}</p>
          </div>
        </header>
        <div className="fs-panel">
          <ProjectSettings project={null} say={say} onCancel={() => navigate('/projects')} onSaved={(p) => navigate(`/projects/${p.id}`)} />
        </div>
        {notice && (
          <Toast>
            <Check size={12} aria-hidden="true" /> {notice}
          </Toast>
        )}
      </div>
    );
  }

  if (failed) {
    return (
      <EmptyState icon={FolderOpen} title={t('Could not find that project')} body={t('The identifier in the URL matches no project, or the API is not responding.')} primaryAction={{ label: t('See all projects'), onClick: () => navigate('/projects') }} />
    );
  }

  if (!project) {
    return (
      <div className="fs-screen">
        <Skeleton label={t('Loading the project')} width="40%" height="32px" />
        <Skeleton label={t('Loading the detail')} count={5} height="20px" />
      </div>
    );
  }

  const roots = [
    ...(project.workspace ? [{ id: '', path: project.workspace, kind: 'folder' as const, name: project.workspace.split(/[\\/]/).filter(Boolean).pop() ?? project.workspace, primary: true }] : []),
    ...(project.context_items ?? []).map((i) => ({ ...i, primary: false })),
  ];

  return (
    <div className="fs-screen fs-pj" data-testid="project" data-archived={project.archived || undefined}>
      <div>
        <Link to="/projects" className="fs-tab" style={{ paddingInline: 0 }}>
          <ArrowLeft size={14} aria-hidden="true" /> {t('Projects')}
        </Link>
      </div>

      <header className="fs-screen__head">
        <div className="fs-pj__title">
          <h1 className="fs-screen__title">{project.name}</h1>
          <p className="fs-screen__sub">{project.workspace ?? project.folder}</p>
        </div>
        <div className="fs-pj__head-actions">
          <Button variant="ghost" size="sm" icon={project.pinned ? PinOff : Pin} label={project.pinned ? t('Unpin') : t('Pin')} loading={busy === 'pin'} onClick={() => void patch('pin', { pinned: !project.pinned }, project.pinned ? t('Unpinned') : t('Pinned'))} testId="project-pin" />
          <Menu
            trigger={<Button variant="ghost" size="sm" icon={Download} label={t('Export chats')} title={t('Every chat in this project as one .zip')} />}
            items={EXPORT_FORMATS.map((f) => ({
              label: FORMAT_LABEL[f] ?? f,
              onSelect: () => {
                const a = document.createElement('a');
                a.href = exportProjectUrl(project.id, f);
                a.download = '';
                document.body.appendChild(a);
                a.click();
                a.remove();
              },
            }))}
            align="end"
          />
          <Button variant="ghost" size="sm" icon={project.archived ? ArchiveRestore : Archive} label={project.archived ? t('Restore') : t('Archive')} loading={busy === 'archive'} onClick={() => void patch('archive', { archived: !project.archived }, project.archived ? t('Project restored') : t('Project archived'))} testId="project-archive" />
        </div>
      </header>

      {project.archived && (
        <p className="fs-notice" data-tone="warning">
          {t('This project is archived. Its existing chats still keep their context.')}
        </p>
      )}

      <div className="fs-tabs" role="tablist" aria-label={t('Project sections')}>
        {TABS.map((entry) => (
          <button key={entry.id} type="button" role="tab" aria-selected={tab === entry.id} className="fs-tab" data-testid={`project-tab-${entry.id}`} onClick={() => setTab(entry.id)}>
            {t(entry.label)}
          </button>
        ))}
      </div>

      {tab === 'brief' && (
        <div className="fs-pj__brief">
          <form
            className="fs-pj__start"
            onSubmit={(e) => {
              e.preventDefault();
              void start();
            }}
          >
            <label className="fs-pj__start-label" htmlFor="fs-pj-prompt">
              {t('Start a chat in {name}', { name: project.name })}
            </label>
            <textarea id="fs-pj-prompt" className="fs-field fs-pj__textarea" rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t('What do you want to do here? (optional)')} disabled={project.archived} data-testid="project-prompt" />
            <div className="fs-pj__row">
              <select className="fs-field" value={routeId} onChange={(e) => setRouteId(e.target.value)} aria-label={t('Model for the new chat')} disabled={project.archived}>
                {routes.length === 0 && <option value="">{t('No model available')}</option>}
                {routes.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.model} · {r.endpointName}
                  </option>
                ))}
              </select>
              <Button type="submit" variant="primary" size="sm" icon={Send} label={t('Start chat')} loading={busy === 'start'} disabled={project.archived || !routes.length} testId="project-start" />
            </div>
          </form>

          <div className="fs-pj__cards">
            <section className="fs-panel fs-pj__card">
              <div className="fs-pj__card-head">
                <h3>{t('Instructions')}</h3>
                <Button variant="ghost" size="sm" label={t('Edit')} onClick={() => setTab('ajustes')} />
              </div>
              {project.instructions ? <p className="fs-prose fs-pj__instructions">{project.instructions}</p> : <p className="fs-pj__muted">{t('Add guidance that should apply to every chat in this project.')}</p>}
            </section>

            <section className="fs-panel fs-pj__card">
              <div className="fs-pj__card-head">
                <h3>{t('The agent here')}</h3>
                <Button variant="ghost" size="sm" label={t('Change')} onClick={() => setTab('ajustes')} />
              </div>
              <ul className="fs-pj__flags">
                {flags.map((f) => (
                  <li key={f.key} data-on={f.on || undefined} title={t(f.help)}>
                    <span className="fs-pj__flag-dot" aria-hidden="true" />
                    {t(f.label)}
                    <small>{f.on ? t('on') : t('off')}</small>
                  </li>
                ))}
                {project.test_command && (
                  <li data-on="">
                    <span className="fs-pj__flag-dot" aria-hidden="true" />
                    {t('Test command')}
                    <small>
                      <code>{project.test_command}</code>
                    </small>
                  </li>
                )}
                {project.review_model && (
                  <li data-on="">
                    <span className="fs-pj__flag-dot" aria-hidden="true" />
                    {t('Reviewer model')}
                    <small>{project.review_model}</small>
                  </li>
                )}
              </ul>
            </section>

            <section className="fs-panel fs-pj__card">
              <div className="fs-pj__card-head">
                <h3>{t('Work roots')}</h3>
                <Button variant="ghost" size="sm" icon={FolderPlus} label={t('Add')} onClick={() => void addRoot()} testId="project-add-root" />
              </div>
              <p className="fs-pj__muted">{t('The agent can read and change every file or folder listed here.')}</p>
              {rootInput !== null && (
                <form
                  className="fs-pj__row"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void addRootPath(rootInput);
                  }}
                >
                  <input className="fs-field fs-pj__grow" value={rootInput} onChange={(e) => setRootInput(e.target.value)} placeholder={t('Path of the folder or file to add')} spellCheck={false} data-testid="project-root-path" />
                  <Button type="submit" variant="secondary" size="sm" label={t('Add')} disabled={!rootInput.trim()} />
                  <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={() => setRootInput(null)} />
                </form>
              )}
              {roots.length === 0 ? (
                <p className="fs-pj__muted">{t('Add a primary folder or another file or folder to start working.')}</p>
              ) : (
                <ul className="fs-pj__roots">
                  {roots.map((r) => (
                    <li key={r.id || 'primary'}>
                      <FolderOpen size={13} aria-hidden="true" />
                      <span>
                        <strong>{r.name}</strong>
                        <small>
                          {r.path}
                          {r.primary ? ` · ${t('primary')}` : ''}
                        </small>
                      </span>
                      {r.primary ? (
                        <Button variant="ghost" size="sm" label={t('Change')} onClick={() => setTab('ajustes')} />
                      ) : (
                        <Button variant="ghost" size="sm" icon={X} label={t('Remove')} onClick={() => void removeContextRoot(project.id, r.id).then(reload, (e: Error) => say(e.message))} />
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
          <p className="fs-file__meta">
            {t('Created {when}', { when: relativeTime(project.created_at) })} · {t('updated {when}', { when: relativeTime(project.updated_at) })}
          </p>
        </div>
      )}

      {tab === 'chats' && (
        <div className="fs-panel">
          <div className="fs-pj__card-head">
            <h3>{chats ? tn(chats.length, '{n} conversation in {folder}', '{n} conversations in {folder}', { folder: project.folder ?? '' }) : t('Conversations')}</h3>
            {!project.archived && <Button variant="secondary" size="sm" icon={Plus} label={t('New chat')} onClick={() => setTab('brief')} />}
          </div>
          {chats === null ? (
            <Skeleton label={t('Loading the conversations')} count={3} height="44px" />
          ) : chats.length === 0 ? (
            <p className="fs-pj__muted">{t('No chats yet. Start one from the brief and it will stay grouped here.')}</p>
          ) : (
            <div className="fs-list fs-list--rail">
              {chats.map((c) => (
                <div key={c.id} className="fs-pj__chat">
                  <Link to={`/studio?s=${encodeURIComponent(c.id)}`} className="fs-row" data-testid="project-chat">
                    <span className="fs-row__main">
                      <span className="fs-row__name">{c.name || t('Untitled')}</span>
                      <span className="fs-row__meta">{[c.model, tn(c.messageCount, '{n} message', '{n} messages'), relativeTime(c.lastMessageAt ?? c.createdAt)].filter(Boolean).join(' · ')}</span>
                    </span>
                  </Link>
                  <Button variant="ghost" size="sm" icon={Trash2} label={t('Delete')} onClick={() => setConfirm({ chat: c.id })} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === 'objetivos' && (
        <div className="fs-panel">
          <ProjectObjectives projectId={project.id} say={say} />
        </div>
      )}

      {tab === 'memoria' && (
        <div className="fs-panel">
          <ProjectMemoryFiles project={project} say={say} />
        </div>
      )}

      {tab === 'actividad' && (
        <div className="fs-panel">
          <ProjectAudit projectId={project.id} say={say} />
        </div>
      )}

      {tab === 'contexto' && (
        <div>
          <p className="fs-panel__label">{t('What the model receives, literally')}</p>
          <p className="fs-prose" style={{ marginBlockEnd: 'var(--fs-space-3)' }}>
            {t('This block is what Faustus prepends to every conversation of this project. It was available in the API and no screen showed it: knowing what it knows before asking for anything is half of trusting it.')}
          </p>
          {context === null ? (
            <Skeleton label={t('Loading the context')} count={4} height="20px" />
          ) : context ? (
            <pre className="fs-context" data-testid="project-context">
              {context}
            </pre>
          ) : (
            <EmptyState icon={Eye} title={t('No context block')} body={t('This project prepends nothing yet. As soon as it has a folder, instructions or memory, it will appear here exactly as the model reads it.')} />
          )}
        </div>
      )}

      {tab === 'ajustes' && (
        <div className="fs-panel">
          <ProjectSettings
            project={project}
            say={say}
            onCancel={() => setTab('brief')}
            onSaved={(p) => {
              setProject(p);
              void reload();
              setTab('brief');
            }}
          />
          <div className="fs-pj__danger">
            <h3>{t('Danger zone')}</h3>
            <p className="fs-pj__muted">{t('Deleting the project keeps its chats and its folder; only the grouping, the instructions and the objectives go.')}</p>
            <Button variant="danger" size="sm" icon={Trash2} label={t('Delete project')} onClick={() => setConfirm('delete')} testId="project-delete" />
          </div>
        </div>
      )}

      {confirm === 'delete' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('Delete “{name}”?', { name: project.name })}
          testId="project-confirm-delete"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={t('Delete')}
                loading={busy === 'delete'}
                onClick={() => {
                  setBusy('delete');
                  void deleteProject(project.id)
                    .then(() => navigate('/projects'))
                    .catch((e: Error) => {
                      say(e.message);
                      setBusy(null);
                    });
                }}
                testId="project-confirm-delete-ok"
              />
            </>
          }
        >
          <p className="fs-prose">{t('The chats stay, ungrouped. The folder on disk is not touched.')}</p>
        </Dialog>
      )}

      {confirm && typeof confirm === 'object' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('Delete this chat?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={t('Delete')}
                onClick={() => {
                  const id = confirm.chat;
                  setConfirm(null);
                  void removeChatFromProject(project.id, id)
                    .then(() => chatsIn(project))
                    .then(setChats)
                    .then(() => say(t('Chat deleted')), (e: Error) => say(e.message));
                }}
              />
            </>
          }
        >
          <p className="fs-prose">{t('This cannot be undone.')}</p>
        </Dialog>
      )}

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
