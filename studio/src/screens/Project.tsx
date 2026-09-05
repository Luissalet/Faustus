import { ArrowLeft, Brain, Eye, FileText, Target } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import {
  formatBytes,
  getContextPreview,
  getMemory,
  getObjectives,
  getProject,
  type Objective,
  type Project,
  type ProjectMemory,
} from '../adapters/projects';
import { relativeTime } from '../adapters/home';
import './projects.css';
import './home.css';
import { t } from '../i18n';

const TABS = [
  { id: 'brief', label: 'Brief', icon: FileText },
  { id: 'contexto', label: 'Context', icon: Eye },
  { id: 'memoria', label: 'Memory', icon: Brain },
  { id: 'objetivos', label: 'Objectives', icon: Target },
] as const;

type TabId = (typeof TABS)[number]['id'];

interface Loaded {
  project: Project;
  memory: ProjectMemory | null;
  objectives: Objective[];
  context: string;
}

/**
 * A project as a page (UI-040).
 *
 * The tab lives in the query string, so "look at what this project actually
 * sends to the model" is a link you can paste to yourself.
 */
export function ProjectScreen() {
  const { projectId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState(false);

  // An unknown value (a stale link, a typo) falls back to the brief instead
  // of a page with four unselected tabs and nothing under them.
  const rawTab = params.get('tab');
  const tab: TabId = TABS.some((t) => t.id === rawTab) ? (rawTab as TabId) : 'brief';

  useEffect(() => {
    const controller = new AbortController();
    const signal = controller.signal;
    Promise.all([
      getProject(projectId, signal),
      getMemory(projectId, signal).catch(() => null),
      getObjectives(projectId, signal).catch(() => []),
      getContextPreview(projectId, signal).catch(() => ''),
    ])
      .then(([project, memory, objectives, context]) =>
        setData({ project, memory, objectives, context }),
      )
      .catch(() => setFailed(true));
    return () => controller.abort();
  }, [projectId]);

  if (failed) {
    return (
      <EmptyState
        title={t('Could not find that project')}
        body={t('The identifier in the URL matches no project, or the API is not responding.')}
        primaryAction={{ label: t('See all projects'), onClick: () => { window.location.href = '/projects'; } }}
      />
    );
  }

  if (!data) {
    return (
      <div className="fs-screen">
        <Skeleton label={t('Loading the project')} width="40%" height="32px" />
        <Skeleton label={t('Loading the detail')} count={5} height="20px" />
      </div>
    );
  }

  const { project, memory, objectives, context } = data;

  return (
    <div className="fs-screen" data-testid="project">
      <div>
        <Link to="/projects" className="fs-tab" style={{ paddingInline: 0 }}>
          <ArrowLeft size={14} aria-hidden="true" /> {t('Projects')}
        </Link>
      </div>

      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{project.name}</h1>
          <p className="fs-screen__sub">{project.workspace ?? project.folder}</p>
        </div>
        <Button
          variant="primary"
          label={t('New work')}
          onClick={() => {
            window.location.href = '/?shell=legacy';
          }}
        />
      </header>

      <div className="fs-tabs" role="tablist" aria-label={t('Project sections')}>
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={tab === entry.id}
            className="fs-tab"
            data-testid={`project-tab-${entry.id}`}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set('tab', entry.id);
              setParams(next);
            }}
          >
            {t(entry.label)}
          </button>
        ))}
      </div>

      {tab === 'brief' && (
        <div className="fs-panel">
          <p className="fs-panel__label">Instrucciones</p>
          {project.instructions ? (
            <p className="fs-prose">{project.instructions}</p>
          ) : (
            <p className="fs-prose">
              Este proyecto no tiene instrucciones. Son lo que Faustus lee antes de
              cada respuesta dentro de él.
            </p>
          )}
          <p className="fs-file__meta" style={{ marginBlockStart: 'var(--fs-space-4)' }}>
            Creado {relativeTime(project.created_at)} · actualizado{' '}
            {relativeTime(project.updated_at)}
          </p>
        </div>
      )}

      {tab === 'contexto' && (
        <div>
          <p className="fs-panel__label">{t('What the model receives, literally')}</p>
          <p className="fs-prose" style={{ marginBlockEnd: 'var(--fs-space-3)' }}>
            {t('This block is what Faustus prepends to every conversation of this project. It was available in the API and no screen showed it: knowing what it knows before asking for anything is half of trusting it.')}
          </p>
          {context ? (
            <pre className="fs-context" data-testid="project-context">
              {context}
            </pre>
          ) : (
            <EmptyState
              icon={Eye}
              title={t('No context block')}
              body={t('This project prepends nothing yet. As soon as it has a folder, instructions or memory, it will appear here exactly as the model reads it.')}
            />
          )}
        </div>
      )}

      {tab === 'memoria' && (
        <div className="fs-panel">
          <p className="fs-panel__label">
            {memory ? memory.dir : t('Project memory')}
          </p>
          {memory && memory.files.length > 0 ? (
            <div className="fs-files">
              {memory.files.map((file) => (
                <div className="fs-file" key={file.name} data-testid="memory-file">
                  <span className="fs-file__name">{file.name}</span>
                  <span className="fs-file__meta">{formatBytes(file.size)}</span>
                  <span className="fs-file__meta">{relativeTime(file.modified)}</span>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={Brain}
              title={t('No memory yet')}
              body={t('The project memory is text files in its folder. Faustus reads them when it works here, and you can edit them like any other file.')}
            />
          )}
        </div>
      )}

      {tab === 'objetivos' && (
        <div className="fs-panel">
          <p className="fs-panel__label">Objetivos</p>
          {objectives.length > 0 ? (
            <div className="fs-files">
              {objectives.map((objective, index) => (
                <div className="fs-file" key={objective.id ?? index}>
                  <span className="fs-file__name" style={{ fontFamily: 'var(--fs-font-ui)' }}>
                    {objective.title ?? objective.name ?? t('Untitled objective')}
                  </span>
                  {objective.status && (
                    <span className="fs-file__meta">{objective.status}</span>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={Target}
              title={t('No objectives defined')}
              body={t('Objectives are what this project is trying to achieve, and they let Faustus know when a piece of work counts as done.')}
            />
          )}
        </div>
      )}
    </div>
  );
}
