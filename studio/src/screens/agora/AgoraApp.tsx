import {
  Activity, Archive, BookOpen, Check, ExternalLink, GitPullRequest, Hash, Inbox, Kanban, Lock, MessagesSquare, PackageCheck, PanelLeft,
  Pencil, RefreshCw, Scale, Search, SquarePen, Users, Volume2, X, type LucideIcon,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import {
  getGuild, getServers, markSpaceSeen, pulse, setSpaceMeta, updateChannel,
  type AgoraGuild, type AgoraServer,
} from '../../adapters/agora';
import { listGroups, type GroupState } from '../../adapters/group';
import type { Project } from '../../adapters/projects';
import { EmptyState, IconButton, Menu, Skeleton, Toast } from '../../components';
import { t } from '../../i18n';
import { ChannelList, type ListAction } from './ChannelList';
import { ChatPane } from './ChatPane';
import { MembersPane } from './MembersPane';
import { RoomsPane } from './RoomsPane';
import { AddServerDialog, EditServerDialog, NewChannelDialog, NewTaskDialog, NewThreadDialog } from './ServerDialogs';
import { ServerRail } from './ServerRail';
import { ActivityView, BoardView, DecisionsView, DeliveriesView, InboxView, LocksView, SearchView, WelcomeView } from './Views';
import { findTaskByThread, parseView, recall, remember, THREAD_KIND_LABEL, viewKey, viewTarget, type View } from './util';
import './agora-app.css';

const VIEW_META: Record<string, { label: string; hint: string; icon: LucideIcon }> = {
  welcome: { label: 'welcome', hint: 'What this server is, the folders it reads and how agents join', icon: BookOpen },
  decisions: { label: 'decisions', hint: 'Debates and questions closed with their resolution in writing', icon: Scale },
  board: { label: 'board', hint: 'Every task of this server by state', icon: Kanban },
  deliveries: { label: 'deliveries', hint: 'What was sent to review, approved or closed, with commits and result', icon: PackageCheck },
  reviews: { label: 'reviews', hint: 'What waits for a reviewer or for changes', icon: GitPullRequest },
  activity: { label: 'activity', hint: 'Every message of this server, newest first', icon: Activity },
  locks: { label: 'locks', hint: 'Files, merges, models and apps taken right now', icon: Lock },
  inbox: { label: 'Your inbox', hint: 'What waits for you in every server', icon: Inbox },
  search: { label: 'Search', hint: 'Where something was said', icon: Search },
};

type DialogState = null | { kind: 'add-server' } | { kind: 'edit-server'; focus?: 'folders' } | { kind: 'new-channel'; category: string }
  | { kind: 'new-thread' } | { kind: 'new-task' };

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/**
 * The Agora as a chat server: projects are servers, each with its channels,
 * its task threads grouped by board column, its rooms of models and, on the
 * right, the agents that take part. `lockedSpace` pins it to one server (a
 * project's own Agora tab).
 */
export function AgoraApp({ lockedSpace, project, embedded = false }: { lockedSpace?: string; project?: Project; embedded?: boolean }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const spaceId = lockedSpace || params.get('space_id') || recall('agora:last-space') || 'ecosystem';
  const view = useMemo(() => parseView(params.get('c') || recall('agora:last-view:' + spaceId)), [params, spaceId]);
  const [servers, setServers] = useState<AgoraServer[] | null>(null);
  const [home, setHome] = useState('ecosystem');
  const [guild, setGuild] = useState<AgoraGuild | null>(null);
  const [error, setError] = useState('');
  const [tick, setTick] = useState(0);
  const [rooms, setRooms] = useState<GroupState[] | null>(null);
  const [roomTick, setRoomTick] = useState(0);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [membersOpen, setMembersOpen] = useState(() => recall('agora:members') !== '0' && window.innerWidth > 1180);
  const [channelsOpen, setChannelsOpen] = useState(false);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const noticeTimer = useRef<number | null>(null);

  const notify = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 3200);
  }, []);
  useEffect(() => () => { if (noticeTimer.current) window.clearTimeout(noticeTimer.current); }, []);

  const open = useCallback((next: View, space?: string) => {
    const target = space ?? spaceId;
    if (!(next.kind === 'view' && (next.id === 'inbox' || next.id === 'search'))) remember('agora:last-view:' + target, viewKey(next));
    if (!lockedSpace) remember('agora:last-space', target);
    setChannelsOpen(false);
    if (target !== spaceId && lockedSpace) { navigate('/agora?space_id=' + encodeURIComponent(target) + '&c=' + encodeURIComponent(viewKey(next))); return; }
    if (target !== spaceId) setGuild(null);
    setParams((old) => {
      const merged = new URLSearchParams(old);
      if (!lockedSpace) merged.set('space_id', target);
      merged.set('c', viewKey(next));
      merged.delete('mode'); merged.delete('s');
      if (!(next.kind === 'view' && next.id === 'search')) merged.delete('q');
      return merged;
    });
  }, [lockedSpace, navigate, setParams, spaceId]);

  const selectServer = useCallback((id: string) => {
    if (id === spaceId) return;
    setGuild(null);
    open(parseView(recall('agora:last-view:' + id)), id);
  }, [open, spaceId]);

  // The server view and the server list, refreshed when the Agora says something changed.
  useEffect(() => {
    let stop = false;
    const controller = new AbortController();
    setError('');
    void (async () => {
      let version = '';
      while (!stop) {
        try {
          const [nextGuild, listing] = await Promise.all([getGuild(spaceId, controller.signal), getServers(controller.signal)]);
          if (stop) return;
          setGuild(nextGuild); setServers(listing.servers); setHome(listing.default_space_id || 'ecosystem'); setError('');
          version = nextGuild.version;
        } catch (failure) {
          if (stop || controller.signal.aborted) return;
          setError((failure as Error).message);
          await sleep(5000);
          continue;
        }
        try { version = (await pulse(version, controller.signal)).version; }
        catch { if (stop || controller.signal.aborted) return; await sleep(5000); }
        // A burst of changes is one refresh, and a busy Agora (agents send heartbeats all the time) is read every
        // few seconds, not on every beat; a tab nobody looks at, much less often.
        await sleep(document.hidden ? 15000 : 2500);
      }
    })();
    return () => { stop = true; controller.abort(); };
  }, [spaceId, tick]);

  useEffect(() => {
    const controller = new AbortController();
    const load = () => listGroups(controller.signal, spaceId).then((all) => {
      if (!controller.signal.aborted) setRooms(all.filter((g) => (g.space_id || 'ecosystem') === spaceId || (project && g.projectId === project.id)));
    }).catch(() => { if (!controller.signal.aborted) setRooms([]); });
    void load();
    const timer = window.setInterval(load, 15000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [spaceId, project, roomTick]);

  const refresh = useCallback(() => setTick((n) => n + 1), []);
  const setQuery = useCallback((q: string) => {
    setParams((old) => {
      const merged = new URLSearchParams(old);
      if (!lockedSpace) merged.set('space_id', spaceId);
      merged.set('c', 'v:search'); merged.set('q', q);
      return merged;
    });
  }, [lockedSpace, setParams, spaceId]);
  const server = servers?.find((s) => s.id === spaceId);
  const ready = guild && guild.space.id === spaceId ? guild : null;

  const onAction = useCallback((action: ListAction, arg?: string) => {
    if (action === 'edit-server') setDialog({ kind: 'edit-server' });
    else if (action === 'folders') setDialog({ kind: 'edit-server', focus: 'folders' });
    else if (action === 'new-channel') setDialog({ kind: 'new-channel', category: arg || 'chat' });
    else if (action === 'new-thread') setDialog({ kind: 'new-thread' });
    else if (action === 'new-task') setDialog({ kind: 'new-task' });
    else if (action === 'new-room') open({ kind: 'room', id: 'new' });
    else if (action === 'mark-read') void markSpaceSeen(spaceId).then(() => { notify(t('Marked as read')); refresh(); }).catch((e: Error) => notify(e.message, 'warn'));
    else if (action === 'toggle-hidden') void setSpaceMeta(spaceId, { hidden: !server?.hidden }).then(refresh).catch((e: Error) => notify(e.message, 'warn'));
    else if (action === 'open-project' && ready?.space.project_id) navigate('/projects/' + encodeURIComponent(ready.space.project_id));
    else if (action === 'council') navigate('/agora?space_id=' + encodeURIComponent(spaceId) + '&mode=council');
    else if (action === 'classic') navigate('/agora?space_id=' + encodeURIComponent(spaceId) + '&mode=groups');
  }, [navigate, notify, open, ready, refresh, server?.hidden, spaceId]);

  const head = useMemo(() => {
    if (!ready) return { icon: Hash, title: '', hint: '' };
    if (view.kind === 'channel') {
      const channel = ready.channels.find((c) => c.slug === view.id);
      return { icon: Hash, title: channel?.title ?? view.id, hint: channel?.topic || (view.id === 'general' ? t('The conversation of the whole server') : '') };
    }
    if (view.kind === 'view') {
      const meta = VIEW_META[view.id] ?? VIEW_META.welcome;
      return { icon: meta.icon, title: t(meta.label), hint: t(meta.hint) };
    }
    if (view.kind === 'room') {
      const room = rooms?.find((r) => r.parentId === view.id);
      return { icon: Volume2, title: view.id === 'new' ? t('New room') : room?.name ?? t('Room'), hint: t('Several models talking with you and each other') };
    }
    const task = findTaskByThread(ready, Number(view.id));
    if (task) return { icon: SquarePen, title: '#' + task.id + ' ' + task.title, hint: '' };
    const thread = [...ready.threads, ...ready.waiting].find((th) => th.id === Number(view.id));
    return { icon: MessagesSquare, title: thread?.title ?? t('Thread {id}', { id: view.id }), hint: thread ? t(THREAD_KIND_LABEL[thread.kind] ?? thread.kind) : '' };
  }, [ready, rooms, view]);

  const channel = ready && view.kind === 'channel' ? ready.channels.find((c) => c.slug === view.id) : undefined;
  const channelMenu = channel && [
    { label: t('Change the topic…'), icon: Pencil, onSelect: () => {
      const topic = window.prompt(t('Topic of #{name}', { name: channel.slug }), channel.topic);
      if (topic !== null) void updateChannel(spaceId, channel.slug, { topic }).then(refresh).catch((e: Error) => notify(e.message, 'warn'));
    } },
    ...(channel.builtin ? [] : [{ label: t('Archive the channel'), icon: Archive, onSelect: () => {
      void updateChannel(spaceId, channel.slug, { archived: true }).then(() => { notify(t('Channel archived')); open({ kind: 'channel', id: 'general' }); refresh(); }).catch((e: Error) => notify(e.message, 'warn'));
    } }]),
  ];
  const target = viewTarget(view);
  const HeadIcon = head.icon;

  return (
    <div className="fs-agx" data-members={membersOpen || undefined} data-channels={channelsOpen || undefined} data-embedded={embedded || undefined}
      data-rail={lockedSpace ? undefined : true} data-testid="agora-app">
      {!lockedSpace && (
        servers
          ? <ServerRail servers={servers} current={view.kind === 'view' && view.id === 'inbox' ? '' : spaceId} home={home} onSelect={(id) => (id === spaceId ? open(parseView(recall('agora:last-view:' + id) || 'ch:general')) : selectServer(id))}
              onAdd={() => setDialog({ kind: 'add-server' })} showHidden={showHidden} onToggleHidden={() => setShowHidden(!showHidden)}
              onInbox={() => open({ kind: 'view', id: 'inbox' })} inboxOpen={view.kind === 'view' && view.id === 'inbox'} />
          : <nav className="fs-agx-rail" aria-label={t('Servers')} />
      )}
      {ready
        ? <ChannelList guild={ready} view={view} onOpen={open} onAction={onAction} rooms={rooms} hidden={Boolean(server?.hidden)} embedded={Boolean(lockedSpace)} />
        : <aside className="fs-agx-channels"><div className="fs-agx-channels__scroll"><Skeleton label={t('Loading channels')} count={8} height="28px" /></div></aside>}
      <main className="fs-agx-main">
        <header className="fs-agx-main__head">
          <IconButton size="sm" icon={PanelLeft} label={t('Channels')} onClick={() => setChannelsOpen(!channelsOpen)} testId="agora-toggle-channels" />
          <HeadIcon size={18} aria-hidden="true" className="fs-agx-main__icon" />
          <h1 data-testid="agora-title">{head.title}</h1>
          {head.hint && <p className="fs-agx-main__topic">{head.hint}</p>}
          <span className="fs-spacer" />
          <form className="fs-agx-main__search" role="search" onSubmit={(e) => { e.preventDefault(); const q = new FormData(e.currentTarget).get('q'); if (typeof q === 'string' && q.trim().length >= 2) { setQuery(q.trim()); e.currentTarget.reset(); } }}>
            <Search size={14} aria-hidden="true" /><input name="q" placeholder={t('Search')} aria-label={t('Search the Agora')} data-testid="agora-search-box" />
          </form>
          {channelMenu && <Menu align="end" items={channelMenu} trigger={<IconButton size="sm" icon={Pencil} label={t('Channel options')} />} />}
          <IconButton size="sm" icon={RefreshCw} label={t('Refresh')} onClick={refresh} />
          <a className="fs-agx-main__hub" href={'/api/hoard/agora/open?space_id=' + encodeURIComponent(spaceId)} target="_blank" rel="noopener" title={t('Open this server in the hub console')}>
            <ExternalLink size={15} aria-hidden="true" /><span className="fs-sr-only">{t('Open in the hub')}</span></a>
          <IconButton size="sm" icon={Users} label={membersOpen ? t('Hide members') : t('Show members')}
            onClick={() => { remember('agora:members', membersOpen ? '0' : '1'); setMembersOpen(!membersOpen); }} testId="agora-toggle-members" />
        </header>
        <div className="fs-agx-main__body">
          {!ready && error && (
            <EmptyState tone="error" title={t('The Agora is not answering')} body={error} primaryAction={{ label: t('Try again'), icon: RefreshCw, onClick: refresh }} />
          )}
          {!ready && !error && <Skeleton label={t('Loading the server')} count={6} height="52px" />}
          {ready && error && <p className="fs-notice" data-tone="warning" role="status">{t('Connection lost: {reason}. What you see may be out of date.', { reason: error })}</p>}
          {ready && target && (
            <ChatPane guild={ready} view={view} target={target} version={ready.version} onOpen={open} notify={notify}
              placeholder={view.kind === 'channel' ? t('Message #{name}', { name: head.title }) : t('Reply in this thread')} />
          )}
          {ready && view.kind === 'view' && (
            <div className="fs-agx-main__scroll">
              {view.id === 'board' && <BoardView guild={ready} onOpen={open} onNewTask={() => setDialog({ kind: 'new-task' })} notify={notify} />}
              {view.id === 'deliveries' && <DeliveriesView guild={ready} version={ready.version} onOpen={open} />}
              {view.id === 'reviews' && <DeliveriesView guild={ready} version={ready.version} onOpen={open} reviews />}
              {view.id === 'activity' && <ActivityView guild={ready} version={ready.version} onOpen={open} />}
              {view.id === 'decisions' && <DecisionsView guild={ready} version={ready.version} onOpen={open} />}
              {view.id === 'locks' && <LocksView version={ready.version} />}
              {view.id === 'inbox' && <InboxView servers={servers ?? []} version={ready.version} onOpen={open} />}
              {view.id === 'search' && <SearchView guild={ready} servers={servers ?? []} query={params.get('q') || ''} onQuery={setQuery} onOpen={open} />}
              {!['board', 'deliveries', 'reviews', 'activity', 'decisions', 'locks', 'inbox', 'search'].includes(view.id) && (
                <WelcomeView guild={ready} onEdit={() => setDialog({ kind: 'edit-server' })} notify={notify} onRefresh={refresh} />
              )}
            </div>
          )}
          {ready && view.kind === 'room' && (
            <RoomsPane spaceId={spaceId} projectId={project?.id || ready.space.project_id || undefined} roomId={view.id}
              onSelect={(id) => (id ? open({ kind: 'room', id }) : open({ kind: 'channel', id: 'general' }))} onChanged={() => setRoomTick((n) => n + 1)} />
          )}
        </div>
      </main>
      {ready && membersOpen && <MembersPane guild={ready} onOpen={open} />}
      {channelsOpen && <button type="button" className="fs-agx-scrim" aria-label={t('Close the channel list')} onClick={() => setChannelsOpen(false)} />}

      <AddServerDialog open={dialog?.kind === 'add-server'} servers={servers ?? []} onClose={() => setDialog(null)} notify={notify}
        onCreated={(id) => { refresh(); selectServer(id); }} />
      {ready && (
        <>
          <EditServerDialog open={dialog?.kind === 'edit-server'} guild={ready} focus={dialog?.kind === 'edit-server' ? dialog.focus : undefined}
            onClose={() => setDialog(null)} onSaved={refresh} notify={notify} />
          <NewChannelDialog open={dialog?.kind === 'new-channel'} guild={ready} category={dialog?.kind === 'new-channel' ? dialog.category : 'chat'}
            onClose={() => setDialog(null)} notify={notify} onCreated={(slug) => { refresh(); open({ kind: 'channel', id: slug }); }} />
          <NewThreadDialog open={dialog?.kind === 'new-thread'} guild={ready} onClose={() => setDialog(null)} notify={notify}
            onCreated={(thread) => { refresh(); open({ kind: 'thread', id: String(thread.id) }); }} />
          <NewTaskDialog open={dialog?.kind === 'new-task'} guild={ready} onClose={() => setDialog(null)} notify={notify}
            onCreated={(threadId) => { refresh(); if (threadId) open({ kind: 'thread', id: String(threadId) }); }} />
        </>
      )}
      {notice && <Toast>{notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}</Toast>}
    </div>
  );
}
