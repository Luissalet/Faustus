import { Archive, ChevronRight, FolderKanban, FolderOpen, Pin, Plus, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { ActivityDot, Button, Dialog, EmptyState, IconButton, Skeleton, Toast } from '../components';
import { pickNative } from '../adapters/composer';
import { listProjects, recentFolders, relocateProject, relocateResultMessage, type Project, type RecentFolder } from '../adapters/projects';
import { listSessions, type ChatSession } from '../adapters/chat';
import { groupActivity, useChatActivity } from '../shell/activity';
import { relativeTime } from '../adapters/home';
import './projects.css';
import './home.css';
import { t } from '../i18n';

/**
 * Proyectos (UI-040).
 *
 * The list used to live inside `#projects-modal`: no URL, no browser back,
 * nothing to bookmark or send to yourself. It is a route now, and the filter
 * lives in the query string, so a filtered view is a link.
 */
export function ProjectsScreen() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [failed, setFailed] = useState(false);
  const query = params.get('q') ?? '';
  const showArchived = params.get('archived') === '1';

  useEffect(() => {
    const controller = new AbortController();
    listProjects(controller.signal).then(setProjects).catch(() => setFailed(true));
    return () => controller.abort();
  }, []);

  /* A project's chats belong to it by folder, and a turn keeps running after
     you leave it: without this the list is silent about work in flight, which
     is exactly the moment you came back to check on. */
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const activity = useChatActivity();
  const live = activity.running.length + activity.awaiting.length;
  useEffect(() => {
    const controller = new AbortController();
    // Only worth asking when something IS alive; and again when that changes.
    if (!live) {
      setSessions([]);
      return () => controller.abort();
    }
    listSessions(controller.signal).then(setSessions).catch(() => undefined);
    return () => controller.abort();
  }, [live]);
  const liveByFolder = useMemo(() => {
    const byFolder = new Map<string, string[]>();
    for (const s of sessions) {
      if (!s.folder) continue;
      byFolder.set(s.folder, [...(byFolder.get(s.folder) ?? []), s.id]);
    }
    const out = new Map<string, ReturnType<typeof groupActivity>>();
    for (const [folder, ids] of byFolder) out.set(folder, groupActivity(activity, ids));
    return out;
  }, [sessions, activity]);

  /* IDX-01: "Reubicar" from the list, without opening the project first —
   *  same action and route as Project.tsx's, offered here too since a
   *  moved folder is usually noticed from the list ("this one shows as
   *  broken"), not from inside a project already open. */
  const [relocateTarget, setRelocateTarget] = useState<Project | null>(null);
  const [relocatePath, setRelocatePath] = useState('');
  const [relocateBusy, setRelocateBusy] = useState(false);
  const [relocateErr, setRelocateErr] = useState<string | null>(null);
  const [relocateRecent, setRelocateRecent] = useState<RecentFolder[] | null>(null);
  const [relocatePickUnavailable, setRelocatePickUnavailable] = useState(false);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const openRelocate = (project: Project) => {
    setRelocateTarget(project);
    setRelocatePath(project.workspace ?? '');
    setRelocateErr(null);
    setRelocatePickUnavailable(false);
    setRelocateRecent(null);
    recentFolders().then(setRelocateRecent).catch(() => setRelocateRecent([]));
  };

  const doRelocate = async () => {
    if (!relocateTarget || !relocatePath.trim()) return;
    setRelocateBusy(true);
    setRelocateErr(null);
    try {
      const result = await relocateProject(relocateTarget.id, relocatePath.trim());
      setProjects((cur) => (cur ? cur.map((p) => (p.id === result.project.id ? result.project : p)) : cur));
      setNotice(relocateResultMessage(result));
      setRelocateTarget(null);
    } catch (e) {
      setRelocateErr((e as Error).message);
    } finally {
      setRelocateBusy(false);
    }
  };

  const browseRelocate = async () => {
    try {
      const pick = await pickNative('folder', relocatePath || relocateTarget?.workspace || '');
      if (pick.status === 'ok' && pick.path) setRelocatePath(pick.path);
      else if (pick.status === 'unavailable') setRelocatePickUnavailable(true);
    } catch (e) {
      setRelocateErr((e as Error).message);
    }
  };

  const archivedCount = useMemo(() => (projects ?? []).filter((p) => p.archived).length, [projects]);

  const visible = useMemo(() => {
    if (!projects) return [];
    const needle = query.trim().toLowerCase();
    return projects
      .filter((project) => (showArchived ? project.archived : !project.archived))
      .filter((project) => !needle || [project.name, project.workspace, project.folder].filter(Boolean).some((field) => String(field).toLowerCase().includes(needle)))
      // Pinned first, then the most recently touched.
      .sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) || (b.updated_at ?? 0) - (a.updated_at ?? 0));
  }, [projects, query, showArchived]);

  if (failed) {
    return (
      <EmptyState
        icon={FolderKanban}
        title={t('Could not read your projects')}
        body={t('The API is not responding.')}
        primaryAction={{ label: t('Retry'), onClick: () => window.location.reload() }}
      />
    );
  }

  return (
    <div className="fs-screen" data-testid="projects">
      <header className="fs-screen__head">
        <h1 className="fs-screen__title">{t('Projects')}</h1>
        <label className="fs-search">
          <Search size={15} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder={t('Filter by name or folder')}
            aria-label={t('Filter projects')}
            data-testid="projects-filter"
            onChange={(event) => {
              const next = new URLSearchParams(params);
              if (event.target.value) next.set('q', event.target.value);
              else next.delete('q');
              // replace, not push: typing must not fill the back button with
              // one history entry per keystroke.
              setParams(next, { replace: true });
            }}
          />
        </label>
        <div className="fs-pj__head-actions">
          {archivedCount > 0 && (
            <button
              type="button"
              className="fs-chip"
              data-on={showArchived || undefined}
              onClick={() => {
                const next = new URLSearchParams(params);
                if (showArchived) next.delete('archived');
                else next.set('archived', '1');
                setParams(next, { replace: true });
              }}
            >
              <Archive size={12} aria-hidden="true" /> {t('Archived')} {archivedCount}
            </button>
          )}
          <Button variant="primary" size="sm" icon={Plus} label={t('New project')} onClick={() => navigate('/projects/new')} testId="projects-new" />
        </div>
      </header>

      {!projects && <Skeleton label={t('Loading projects')} count={4} height="44px" />}

      {projects && visible.length === 0 && (
        <EmptyState
          icon={FolderKanban}
          title={query ? t('No project matches') : showArchived ? t('Nothing archived') : t('No projects yet')}
          body={query ? t('Try another part of the name or of the folder path.') : showArchived ? t('Archived projects would be listed here.') : t('A project groups a folder, instructions, memory and conversations. The agent follows its manners inside that folder.')}
          primaryAction={query || showArchived ? undefined : { label: t('New project'), icon: Plus, onClick: () => navigate('/projects/new') }}
        />
      )}

      {projects && visible.length > 0 && (
        <div className="fs-list fs-list--rail">
          {visible.map((project) => {
            const state = project.folder ? liveByFolder.get(project.folder) ?? null : null;
            return (
            <Link
              key={project.id}
              to={`/projects/${project.id}`}
              className="fs-row"
              data-testid="project-row"
            >
              <span className="fs-row__main">
                <span className="fs-row__name">
                  {state && <ActivityDot state={state} withLabel />}
                  {project.pinned && <Pin size={12} aria-hidden="true" className="fs-pj__pin" />}
                  {project.name}
                </span>
                <span className="fs-row__meta">
                  {[project.workspace, relativeTime(project.updated_at)]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              <span
                className="fs-pj__row-relocate"
                onClick={(e) => {
                  // Inside a <Link> that wraps the whole row: stop the click
                  // here so it never reaches the Link's own navigation.
                  e.preventDefault();
                  e.stopPropagation();
                  openRelocate(project);
                }}
              >
                <IconButton icon={FolderOpen} label={t('Relocate this project’s folder')} size="sm" testId="project-relocate-row" />
              </span>
              <ChevronRight size={16} aria-hidden="true" className="fs-row__go" />
            </Link>
            );
          })}
        </div>
      )}

      {relocateTarget && (
        <Dialog
          open
          onOpenChange={(o) => !o && setRelocateTarget(null)}
          title={t('The folder has moved…')}
          testId="project-relocate-dialog"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setRelocateTarget(null)} />
              <Button
                variant="primary"
                size="sm"
                label={t('Update folder')}
                loading={relocateBusy}
                disabled={!relocatePath.trim()}
                onClick={() => void doRelocate()}
                testId="project-relocate-confirm"
              />
            </>
          }
        >
          <p className="fs-prose">{t('The chats, memories and objectives stay linked to this project — only where its files live on disk changes. The new folder must already exist.')}</p>
          <label className="fs-field-label">
            {t('New folder path')}
            <div className="fs-inline">
              <input
                className="fs-field"
                value={relocatePath}
                onChange={(e) => setRelocatePath(e.target.value)}
                placeholder={relocateTarget.workspace ?? ''}
                data-testid="project-relocate-path"
                autoFocus
              />
              <Button variant="ghost" size="sm" icon={FolderOpen} label={t('Browse…')} onClick={() => void browseRelocate()} testId="project-relocate-browse" />
            </div>
          </label>
          {relocatePickUnavailable && <p className="fs-set__help">{t('No system dialog is available for this browser — pick a recent folder below, or type the path.')}</p>}
          {relocateRecent && relocateRecent.length > 0 && (
            <div className="fs-relocate__recent" data-testid="project-relocate-recent">
              <span className="fs-set__help">{t('Recent folders')}</span>
              <ul>
                {relocateRecent.filter((f) => f.path !== relocateTarget.workspace).map((f) => (
                  <li key={f.path}>
                    <button type="button" className="fs-chip" onClick={() => setRelocatePath(f.path)} title={f.path}>
                      {f.projectName ? t('{path} ({project})', { path: f.path, project: f.projectName }) : f.path}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {relocateErr && <p className="fs-set__help" data-tone="bad" role="alert">{relocateErr}</p>}
        </Dialog>
      )}

      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}
