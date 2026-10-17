import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Button, Skeleton } from '../../components';
import { rooms, type CoordinationRoom } from '../../adapters/coordination';
import { projectAudit, type AuditEntry, type Project } from '../../adapters/projects';
import { relativeTime } from '../../adapters/home';
import { useProjectSpace } from './ProjectAgora';
import { t } from '../../i18n';

export function ProjectOverviewActivity({ projectId, liveChats, onOpen }: { projectId: string; liveChats: number; onOpen: () => void }) {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { let live = true; projectAudit(projectId).then((data) => { if (live) setEntries(data.slice(0, 3)); }).catch((e: Error) => { if (live) setError(e.message); }); return () => { live = false; }; }, [projectId]);
  return <section className="fs-panel" data-testid="project-overview-activity"><div className="fs-pj__card-head"><h2>{t('Recent activity')}</h2><Button size="sm" variant="ghost" label={t('Agent activity')} onClick={onOpen} /></div>
    <p>{t('{n} active chats', { n: liveChats })}</p>
    {error ? <p role="alert">{error}</p> : !entries ? <Skeleton label={t('Loading the activity')} count={2} /> : !entries.length ? <p className="fs-pj__muted">{t('No agent turn has changed files in this project yet.')}</p> : <ul className="fs-project-workspace__list">{entries.map((entry, i) => <li key={i}><div><strong>{entry.request || t('Agent activity')}</strong><p>{entry.ts ? relativeTime(entry.ts) : ''}</p></div></li>)}</ul>}
  </section>;
}

export function ProjectOverviewAgora({ project, onOpen }: { project: Project; onOpen: (room?: string) => void }) {
  const { space, loading, error } = useProjectSpace(project);
  const [items, setItems] = useState<CoordinationRoom[] | null>(null);
  const [roomError, setRoomError] = useState('');
  useEffect(() => { const controller = new AbortController(); if (space) void rooms(space.id, controller.signal).then(setItems).catch((e: Error) => { if (!controller.signal.aborted) setRoomError(e.message); }); return () => controller.abort(); }, [space]);
  return <section className="fs-panel" data-testid="project-overview-agora"><div className="fs-pj__card-head"><h2>{t('Agora')}</h2><Button size="sm" variant="secondary" label={t('Open Agora')} onClick={() => onOpen()} /></div>
    {loading ? <Skeleton label={t('Loading the agora')} count={2} /> : error || roomError ? <p role="alert">{error || roomError}</p> : !space ? <p className="fs-prose">{t('Open Agora to create the project space and work with its agents.')}</p> : !items ? <Skeleton label={t('Loading')} count={2} /> : <ul className="fs-project-workspace__list">{items.map((room) => <li key={room.id}>{room.kind === 'council' ? <Link className="fs-link" to={`/agora?space_id=${encodeURIComponent(space.id)}&mode=council&room=${encodeURIComponent(room.id)}`}>{room.title}</Link> : <Button variant="ghost" size="sm" label={room.title} onClick={() => onOpen(room.id)} />}</li>)}{!items.length && <li>{t('No rooms yet. Open Agora to start one.')}</li>}</ul>}
  </section>;
}
