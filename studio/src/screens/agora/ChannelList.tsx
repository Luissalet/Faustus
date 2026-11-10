import {
  Activity, AlertTriangle, Ban, BookOpen, CheckCheck, CheckCircle2, ChevronDown, ChevronRight, Circle, CircleDot, Eye, EyeOff,
  FolderKanban, FolderOpen, GitPullRequest, Hash, Kanban, Landmark, Lock, MessageCircleQuestion, MessagesSquare, PackageCheck,
  Pencil, Plus, Scale, Settings2, SquarePen, Users, Volume2, type LucideIcon,
  Pin, PinOff,
} from 'lucide-react';
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { BOARD_COLUMNS, type AgoraGuild, type AgoraTaskRow, type BoardColumn } from '../../adapters/agora';
import type { GroupState } from '../../adapters/group';
import { speakerName } from '../../adapters/group';
import { IconButton, Menu } from '../../components';
import { t, tn } from '../../i18n';
import { agentColor, agentInitials, COLUMN_LABEL, recall, remember, THREAD_KIND_LABEL, viewKey, type View } from './util';

export type ListAction = 'edit-server' | 'folders' | 'new-channel' | 'new-thread' | 'new-task' | 'new-room' | 'mark-read' | 'toggle-hidden' | 'toggle-pin' | 'open-project' | 'council' | 'classic';

const TASK_ICON: Record<string, LucideIcon> = {
  open: Circle, claimed: CircleDot, in_progress: CircleDot, blocked: AlertTriangle, changes: AlertTriangle,
  review: GitPullRequest, approved: CheckCheck, done: CheckCircle2, dropped: Ban,
};
const VIEW_ICON: Record<string, LucideIcon> = {
  welcome: BookOpen, decisions: Scale, board: Kanban, deliveries: PackageCheck, reviews: GitPullRequest,
  activity: Activity, locks: Lock,
};
const SHOWN_PER_COLUMN = 60;

/** A count the eye reads as a number and a screen reader as what it counts. */
export function CountBadge({ n, mentions = false }: { n: number; mentions?: boolean }) {
  return (
    <span className="fs-agx-badge" data-tone={mentions ? 'alert' : undefined}>
      <span aria-hidden="true">{n > 99 ? '99+' : n}</span>
      <span className="fs-sr-only">{mentions ? tn(n, '{n} mention', '{n} mentions') : tn(n, '{n} unread', '{n} unread')}</span>
    </span>
  );
}

function Row({ current, icon: Icon, label, onClick, unread = 0, mentions = 0, count, tone, title, lead, testId }: {
  current: boolean; icon: LucideIcon; label: string; onClick: () => void; unread?: number; mentions?: number; count?: number;
  tone?: 'alert' | 'muted' | 'ok' | 'warn'; title?: string; lead?: ReactNode; testId?: string;
}) {
  return (
    <li>
      <button type="button" className="fs-agx-row" aria-current={current ? 'page' : undefined} data-unread={unread > 0 || undefined}
        data-tone={tone} onClick={onClick} title={title ?? label} data-testid={testId}>
        <Icon size={16} aria-hidden="true" className="fs-agx-row__icon" />
        <span className="fs-agx-row__label">{label}</span>
        {lead}
        {mentions > 0 && <CountBadge n={mentions} mentions />}
        {mentions === 0 && unread > 0 && <CountBadge n={unread} />}
        {count !== undefined && mentions === 0 && unread === 0 && count > 0 && <span className="fs-agx-row__count">{count}</span>}
      </button>
    </li>
  );
}

function Section({ id, title, count, folded, onFold, action, children }: {
  id: string; title: string; count?: number; folded: boolean; onFold: (id: string) => void; action?: ReactNode; children: ReactNode;
}) {
  return (
    <section className="fs-agx-cat" data-folded={folded || undefined} data-testid={'agora-cat-' + id}>
      <header className="fs-agx-cat__head">
        <button type="button" className="fs-agx-cat__toggle" aria-expanded={!folded} onClick={() => onFold(id)}>
          {folded ? <ChevronRight size={12} aria-hidden="true" /> : <ChevronDown size={12} aria-hidden="true" />}
          <span>{title}</span>
          {count !== undefined && <span className="fs-agx-cat__count">{count}</span>}
        </button>
        {action}
      </header>
      {!folded && <ul className="fs-agx-cat__items">{children}</ul>}
    </section>
  );
}

export function ChannelList({ guild, view, onOpen, onAction, rooms, hidden, embedded }: {
  guild: AgoraGuild; view: View; onOpen: (view: View) => void; onAction: (action: ListAction, arg?: string) => void;
  rooms: GroupState[] | null; hidden: boolean; embedded: boolean;
}) {
  const space = guild.space;
  const storeKey = 'agora:folded:' + space.id;
  const [folded, setFolded] = useState<Set<string>>(() => {
    const saved = recall(storeKey);
    return new Set(saved ? saved.split(',').filter(Boolean) : ['tasks-done']);
  });
  useEffect(() => {
    const saved = recall(storeKey);
    setFolded(new Set(saved ? saved.split(',').filter(Boolean) : ['tasks-done']));
  }, [storeKey]);
  const fold = (id: string) => setFolded((old) => {
    const next = new Set(old);
    if (next.has(id)) next.delete(id); else next.add(id);
    remember(storeKey, [...next].join(',') || '-');
    return next;
  });
  const current = viewKey(view);
  const is = (kind: View['kind'], id: string | number) => current === viewKey({ kind, id: String(id) });

  const categories = useMemo(() => {
    const map = new Map<string, typeof guild.channels>();
    for (const channel of guild.channels) {
      const key = channel.category || 'chat';
      map.set(key, [...(map.get(key) ?? []), channel]);
    }
    return [...map.entries()].sort(([a], [b]) => (a === 'chat' ? -1 : b === 'chat' ? 1 : a.localeCompare(b)));
  }, [guild.channels]);

  const taskRow = (task: AgoraTaskRow) => (
    <Row key={task.id} current={is('thread', task.thread_id ?? 0)} icon={TASK_ICON[task.status] ?? Circle}
      label={'#' + task.id + ' ' + task.title} unread={task.unread} mentions={task.mentions}
      tone={task.status === 'blocked' || task.status === 'changes' ? 'warn' : task.status === 'dropped' ? 'muted' : task.status === 'approved' ? 'ok' : undefined}
      title={'#' + task.id + ' · ' + task.title + (task.owner ? ' · ' + task.owner : '') + (task.waiting_on.length ? ' · ' + t('waits for') + ' ' + task.waiting_on.map((i) => '#' + i).join(', ') : '')}
      onClick={() => task.thread_id && onOpen({ kind: 'thread', id: String(task.thread_id) })} testId={'agora-task-' + task.id}
      lead={task.owner ? <span className="fs-agx-mini" style={{ '--agx-c': agentColor(task.owner) } as CSSProperties} title={task.owner}>{agentInitials(task.owner)}</span> : undefined} />
  );

  const menu = [
    { label: t('Edit server…'), icon: Pencil, onSelect: () => onAction('edit-server') },
    { label: t('Context folders…'), icon: FolderOpen, onSelect: () => onAction('folders') },
    null,
    { label: t('New channel…'), icon: Hash, onSelect: () => onAction('new-channel', 'chat') },
    { label: t('New thread…'), icon: MessagesSquare, onSelect: () => onAction('new-thread') },
    { label: t('New task…'), icon: SquarePen, onSelect: () => onAction('new-task') },
    { label: t('New room of models…'), icon: Volume2, onSelect: () => onAction('new-room') },
    null,
    { label: t('Mark everything as read'), icon: CheckCheck, onSelect: () => onAction('mark-read') },
    ...(space.project_id ? [{ label: t('Open its project'), icon: FolderKanban, onSelect: () => onAction('open-project') }] : []),
    { label: t('Deliberation rooms'), icon: Landmark, onSelect: () => onAction('council') },
    { label: t('Classic room view'), icon: Users, onSelect: () => onAction('classic') },
    ...(embedded ? [] : [null,
      // Servers go by what moved last; a pinned one stays at the top of the list, in the order they were pinned.
      space.meta.position === null
        ? { label: t('Pin this server to the top'), icon: Pin, onSelect: () => onAction('toggle-pin') }
        : { label: t('Unpin this server'), icon: PinOff, onSelect: () => onAction('toggle-pin') },
      { label: hidden ? t('Keep this server on view') : t('Hide this server'), icon: hidden ? Eye : EyeOff, onSelect: () => onAction('toggle-hidden') }]),
  ];

  return (
    <aside className="fs-agx-channels" aria-label={t('Channels of {name}', { name: space.title })} data-testid="agora-channels">
      <Menu align="start" testId="agora-server-menu" items={menu} trigger={
        <button type="button" className="fs-agx-channels__head" data-testid="agora-server-head">
          <span className="fs-agx-channels__title">{space.title}</span>
          <Settings2 size={15} aria-hidden="true" />
        </button>} />
      <div className="fs-agx-channels__scroll">
        <Section id="info" title={t('Information')} folded={folded.has('info')} onFold={fold}>
          <Row current={is('view', 'welcome')} icon={VIEW_ICON.welcome} label={t('welcome')} onClick={() => onOpen({ kind: 'view', id: 'welcome' })} testId="agora-view-welcome" />
          <Row current={is('view', 'decisions')} icon={VIEW_ICON.decisions} label={t('decisions')} count={guild.views.decisions?.count}
            onClick={() => onOpen({ kind: 'view', id: 'decisions' })} testId="agora-view-decisions" />
        </Section>

        {categories.map(([category, channels]) => (
          <Section key={category} id={'cat-' + category} title={category === 'chat' ? t('Text channels') : category} folded={folded.has('cat-' + category)} onFold={fold}
            action={<IconButton size="sm" icon={Plus} label={t('New channel')} onClick={() => onAction('new-channel', category)} testId={'agora-new-channel-' + category} />}>
            {channels.map((channel) => (
              <Row key={channel.slug} current={is('channel', channel.slug)} icon={Hash} label={channel.title} unread={channel.unread}
                mentions={channel.mentions} title={channel.topic || channel.title} onClick={() => onOpen({ kind: 'channel', id: channel.slug })}
                testId={'agora-channel-' + channel.slug} />
            ))}
          </Section>
        ))}

        <Section id="work" title={t('Work')} folded={folded.has('work')} onFold={fold}
          action={<IconButton size="sm" icon={Plus} label={t('New task')} onClick={() => onAction('new-task')} testId="agora-new-task" />}>
          <Row current={is('view', 'board')} icon={VIEW_ICON.board} label={t('board')} onClick={() => onOpen({ kind: 'view', id: 'board' })}
            count={(guild.column_counts.open ?? 0) + (guild.column_counts.in_progress ?? 0) + (guild.column_counts.review ?? 0)} testId="agora-view-board" />
          <Row current={is('view', 'deliveries')} icon={VIEW_ICON.deliveries} label={t('deliveries')} count={guild.views.deliveries?.count}
            title={t('What was sent to review, approved or closed')} onClick={() => onOpen({ kind: 'view', id: 'deliveries' })} testId="agora-view-deliveries" />
          <Row current={is('view', 'reviews')} icon={VIEW_ICON.reviews} label={t('reviews')} count={guild.views.reviews?.count}
            tone={guild.views.reviews?.count ? 'warn' : undefined} onClick={() => onOpen({ kind: 'view', id: 'reviews' })} testId="agora-view-reviews" />
          <Row current={is('view', 'activity')} icon={VIEW_ICON.activity} label={t('activity')} onClick={() => onOpen({ kind: 'view', id: 'activity' })} testId="agora-view-activity" />
          <Row current={is('view', 'locks')} icon={VIEW_ICON.locks} label={t('locks')} count={guild.views.locks?.count} onClick={() => onOpen({ kind: 'view', id: 'locks' })} testId="agora-view-locks" />
        </Section>

        {guild.waiting.length > 0 && (
          <Section id="waiting" title={t('Waiting for you')} count={guild.waiting.length} folded={folded.has('waiting')} onFold={fold}>
            {guild.waiting.map((thread) => (
              <Row key={thread.id} current={is('thread', thread.id)} icon={MessageCircleQuestion} label={thread.title} tone="alert"
                title={(thread.escalated_by ? thread.escalated_by + ': ' : '') + (thread.question || thread.title)}
                onClick={() => onOpen({ kind: 'thread', id: String(thread.id) })} testId={'agora-waiting-' + thread.id} />
            ))}
          </Section>
        )}

        <Section id="threads" title={t('Threads')} count={guild.threads.length} folded={folded.has('threads')} onFold={fold}
          action={<IconButton size="sm" icon={Plus} label={t('New thread')} onClick={() => onAction('new-thread')} testId="agora-new-thread" />}>
          {guild.threads.filter((thread) => thread.status !== 'escalated').map((thread) => (
            <Row key={thread.id} current={is('thread', thread.id)} icon={MessagesSquare} label={thread.title} unread={thread.unread} mentions={thread.mentions}
              title={t(THREAD_KIND_LABEL[thread.kind] ?? thread.kind) + ' · ' + thread.title + (thread.last_author ? ' · ' + thread.last_author : '')}
              onClick={() => onOpen({ kind: 'thread', id: String(thread.id) })} testId={'agora-thread-' + thread.id} />
          ))}
          {!guild.threads.length && <li className="fs-agx-cat__empty">{t('No open threads.')}</li>}
        </Section>

        {BOARD_COLUMNS.map((column: BoardColumn) => {
          const tasks = guild.tasks[column] ?? [];
          const total = guild.column_counts[column] ?? tasks.length;
          return (
            <Section key={column} id={'tasks-' + column} title={t(COLUMN_LABEL[column])} count={total} folded={folded.has('tasks-' + column)} onFold={fold}
              action={column === 'open' ? <IconButton size="sm" icon={Plus} label={t('New task')} onClick={() => onAction('new-task')} /> : undefined}>
              {tasks.slice(0, SHOWN_PER_COLUMN).map(taskRow)}
              {!tasks.length && <li className="fs-agx-cat__empty">{t('Nothing here.')}</li>}
              {(total > Math.min(tasks.length, SHOWN_PER_COLUMN)) && (
                <li><button type="button" className="fs-agx-cat__more" onClick={() => onOpen({ kind: 'view', id: 'board' })}>
                  {t('See all {n} on the board', { n: total })}</button></li>
              )}
            </Section>
          );
        })}

        <Section id="rooms" title={t('Rooms of models')} count={rooms?.length} folded={folded.has('rooms')} onFold={fold}
          action={<IconButton size="sm" icon={Plus} label={t('New room')} onClick={() => onAction('new-room')} testId="agora-new-room" />}>
          {(rooms ?? []).map((room) => (
            <Row key={room.parentId} current={is('room', room.parentId)} icon={Volume2} label={room.name}
              title={room.participants.map(speakerName).join(' · ')} onClick={() => onOpen({ kind: 'room', id: room.parentId })} testId={'agora-room-' + room.parentId} />
          ))}
          {rooms !== null && !rooms.length && <li className="fs-agx-cat__empty">{t('No rooms yet: several models talking with you and each other.')}</li>}
          <Row current={false} icon={Landmark} label={t('deliberation')} onClick={() => onAction('council')} title={t('Deliberation rooms')} />
        </Section>
      </div>
    </aside>
  );
}
