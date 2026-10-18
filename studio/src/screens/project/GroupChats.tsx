import { Users } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { listGroups, speakerName, type GroupState } from '../../adapters/group';
import type { Project } from '../../adapters/projects';
import { useProjectSpace } from './ProjectAgora';
import { t } from '../../i18n';
export function ProjectGroupChats({ project, onOpen }: { project: Project; onOpen: (room?: string) => void }) {
  const { space, loading, error: spaceError } = useProjectSpace(project);
  const [groups, setGroups] = useState<GroupState[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (loading || spaceError) return;
    const controller = new AbortController();
    listGroups(controller.signal).then(all => { if (!controller.signal.aborted) setGroups(all.filter(group => group.projectId === project.id || Boolean(space && group.space_id === space.id))); })
      .catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, [project.id, space?.id, loading, spaceError]);
  return <section className="fs-panel" data-testid="project-group-chats">
    <div className="fs-pj__card-head"><h2>{t('Agora')}</h2><Button icon={Users} label={t('Open Agora')} onClick={() => onOpen()} /></div>
    <p className="fs-muted">{t('Talk with several models in one conversation. Open a room to continue its history, manage members or start a debate.')}</p>
    {(spaceError || error) && <p role="alert">{spaceError || error}</p>}
    {groups === null && !error && !spaceError && <Skeleton label={t('Loading rooms…')} count={2} height="48px" />}
    {groups && <ul className="fs-project-workspace__list">{groups.map(group => <li key={group.parentId}><button type="button" className="fs-project-group-link" data-testid="project-group-chat" onClick={() => onOpen(group.parentId)}><Users size={16} aria-hidden="true" /><span><strong>{group.name}</strong><small>{group.participants.map(speakerName).join(' · ')}</small></span></button></li>)}</ul>}
    {groups?.length === 0 && <p>{t('No rooms yet. Open Agora to start one.')}</p>}
  </section>;
}
