import { Plus } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { getProjectCoordination, projectSpace, spaces, type CoordinationSpace } from '../../adapters/coordination';
import { createServerSpace, getServers } from '../../adapters/agora';
import type { Project } from '../../adapters/projects';
import { AgoraBoardView } from '../group/AgoraBoard';
import { AgoraApp } from '../agora/AgoraApp';
import { StudioScreen } from '../Studio';
import { t } from '../../i18n';
import '../group.css';

const asSpace = (s: { id: string; title: string; description: string; revision: number; project_id?: string; workspace?: string }): CoordinationSpace =>
  ({ id: s.id, title: s.title, description: s.description, revision: s.revision, members: [], project_id: s.project_id, workspace: s.workspace });

/** The project's own agora: the space bound to it, else the one its coordination uses. */
export function useProjectSpace(project: Project) {
  const [space, setSpace] = useState<CoordinationSpace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void (async () => {
      try {
        // The servers of the Agora first: that read also reaches a hub that is on another machine.
        const servers = await getServers(controller.signal).then((r) => r.servers).catch(() => null);
        const all = servers ? servers.map(asSpace) : await spaces(controller.signal);
        let selected = projectSpace(all, project.id, null);
        if (!selected) {
          const view = await getProjectCoordination(project.id, controller.signal).catch(() => null);
          selected = projectSpace(all, project.id, view?.template?.space_id);
        }
        if (!controller.signal.aborted) { setError(''); setSpace(selected); }
      } catch (failure) {
        if (!controller.signal.aborted) { setError((failure as Error).message); setSpace(null); }
      } finally { if (!controller.signal.aborted) setLoading(false); }
    })();
    return () => controller.abort();
  }, [project.id]);
  const create = useCallback(async () => {
    const created = asSpace(await createServerSpace(project.name, { project_id: project.id, workspace: project.workspace || '',
      description: t('Work in {name}: rooms, tasks, reviews and decisions of this project.', { name: project.name }) }));
    setSpace(created); setError('');
    return created;
  }, [project]);
  return { space, loading, error, create };
}

function CreateAgora({ project, create }: { project: Project; create: () => Promise<unknown> }) {
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
  const { space, loading, create } = useProjectSpace(project);
  if (loading) return <Skeleton label={t('Loading the agora')} count={3} height="56px" />;
  // The project's server: its channels, task threads by state, rooms and members.
  if (space) return <div className="fs-project-agora-shell" data-testid="project-agora"><AgoraApp lockedSpace={space.id} project={project} embedded /></div>;
  return (
    <div className="fs-project-agora-shell" data-testid="project-agora">
      <StudioScreen groupContext={{project, selected, onSelect, spaceId: null, ensureSpace: async () => (await create()).id}} />
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
