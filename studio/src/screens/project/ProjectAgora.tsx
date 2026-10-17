import { MessageSquare, Plus, Target, Users } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Button, Skeleton } from '../../components';
import { createSpace, getProjectCoordination, projectSpace, spaces, type CoordinationSpace } from '../../adapters/coordination';
import type { Project } from '../../adapters/projects';
import { AgoraBoardView } from '../group/AgoraBoard';
import { AgoraRooms } from '../group/AgoraRooms';
import { t } from '../../i18n';
import '../group.css';

/** The project's own agora: the space bound to it, else the one its coordination uses. */
export function useProjectSpace(project: Project) {
  const [space, setSpace] = useState<CoordinationSpace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    Promise.allSettled([spaces(controller.signal), getProjectCoordination(project.id, controller.signal)]).then(([all, view]) => {
      if (controller.signal.aborted) return;
      if (all.status === 'rejected') {
        setError((all.reason as Error).message);
        setSpace(null);
      } else {
        setError('');
        setSpace(projectSpace(all.value, project.id, view.status === 'fulfilled' ? view.value.template?.space_id : null));
      }
      setLoading(false);
    });
    return () => controller.abort();
  }, [project.id, tick]);
  const create = useCallback(async () => {
    await createSpace(project.name, { project_id: project.id, workspace: project.workspace || '',
      description: t('Work in {name}: rooms, tasks, reviews and decisions of this project.', { name: project.name }) });
    setTick((n) => n + 1);
  }, [project]);
  return { space, loading, error, create };
}

function CreateAgora({ project, create }: { project: Project; create: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <span className="fs-pj-agora__create">
      <Button size="sm" variant="secondary" icon={Plus} label={t('Create the agora of {name}', { name: project.name })} loading={busy}
        onClick={() => { setBusy(true); setError(''); void create().catch((e: Error) => setError(e.message)).finally(() => setBusy(false)); }}
        testId="project-agora-create" />
      {error && <small role="alert">{error}</small>}
    </span>
  );
}

/** Agora tab: the project's rooms as a group chat app filling the window. */
export function ProjectAgora({ project, selected, onSelect }: { project: Project; selected: string; onSelect: (id: string) => void }) {
  const { space, loading, error, create } = useProjectSpace(project);
  if (loading) return <Skeleton label={t('Loading the agora')} count={3} height="56px" />;
  return (
    <div className="fs-project-agora-shell" data-testid="project-agora">
      <AgoraRooms spaceId={space?.id ?? null} projectId={project.id} selected={selected} onSelect={onSelect}
        sidebarFooter={<nav className="fs-agora-rooms__project-links" aria-label={t('Project sections')}>
          <Link className="fs-chip" to={`/projects/${encodeURIComponent(project.id)}`}><MessageSquare size={13} aria-hidden="true" /> {project.name}</Link>
          <Link className="fs-chip" to={`/projects/${encodeURIComponent(project.id)}?tab=objetivos`}><Target size={13} aria-hidden="true" /> {t('Task board (Objectives)')}</Link>
          {space && <Link className="fs-chip" to={`/agora?space_id=${encodeURIComponent(space.id)}&mode=council`}><Users size={13} aria-hidden="true" /> {t('Deliberation')}</Link>}
          {!space && !error && <CreateAgora project={project} create={create} />}
          {error && <small role="alert">{error}</small>}
        </nav>}
        emptyHint={t('No rooms yet. Open one to talk with several models at once about this project.')} />
    </div>
  );
}

/** Objectives tab: the project's Agora task board, live, above its objectives. */
export function ProjectBoard({ project }: { project: Project }) {
  const { space, loading, error, create } = useProjectSpace(project);
  if (loading) return <Skeleton label={t('Loading the board')} count={2} height="80px" />;
  if (!space) {
    return (
      <section className="fs-pj-agora__noboard" data-testid="project-board-empty">
        <p className="fs-prose">{error || t('The task board lives in the project’s agora: rooms and agents put their tasks there and move them as they work.')}</p>
        {!error && <CreateAgora project={project} create={create} />}
      </section>
    );
  }
  return <AgoraBoardView spaceId={space.id} variant="full" title={t('Tasks of {name}', { name: space.title })} />;
}
