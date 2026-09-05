import { Archive, ChevronRight, FolderKanban, Pin, Plus, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import { listProjects, type Project } from '../adapters/projects';
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
          {visible.map((project) => (
            <Link
              key={project.id}
              to={`/projects/${project.id}`}
              className="fs-row"
              data-testid="project-row"
            >
              <span className="fs-row__main">
                <span className="fs-row__name">
                  {project.pinned && <Pin size={12} aria-hidden="true" className="fs-pj__pin" />}
                  {project.name}
                </span>
                <span className="fs-row__meta">
                  {[project.workspace, relativeTime(project.updated_at)]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              <ChevronRight size={16} aria-hidden="true" className="fs-row__go" />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
