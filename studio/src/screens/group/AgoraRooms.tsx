import { Check, MessageSquare, Plus, Search, Users, X } from 'lucide-react';
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Button, Dialog, EmptyState, IconButton, Skeleton, Toast } from '../../components';
import { listModels, type ModelRoute } from '../../adapters/chat';
import { groupOptions, listGroupPresets, listGroups, speakerName, type GroupPreset, type GroupState } from '../../adapters/group';
import { listPresets, type Preset } from '../../adapters/presets';
import { sessionActivity, useChatActivity } from '../../shell/activity';
import { t } from '../../i18n';
import { RoomChat } from './RoomChat';
import { RoomSetup } from './RoomSetup';
import '../group.css';

/**
 * The rooms of an agora and the open one, side by side like Studio's
 * conversations: a list on the left, the group chat filling the rest. Used
 * by a project's Agora tab (its own rooms) and by the Agora screen.
 */
export function AgoraRooms({ spaceId, projectId, selected, onSelect, emptyHint, sidebarFooter }: {
  /** The agora whose rooms these are; null when the project has none yet. */
  spaceId: string | null; projectId?: string; selected: string; onSelect: (id: string) => void; emptyHint?: string; sidebarFooter?: ReactNode;
}) {
  const [routes, setRoutes] = useState<ModelRoute[] | null>(null);
  const [defaultRoute, setDefaultRoute] = useState<ModelRoute | null>(null);
  const [personas, setPersonas] = useState<Preset[]>([]);
  const [presets, setPresets] = useState<GroupPreset[]>([]);
  const [groups, setGroups] = useState<GroupState[] | null>(null);
  const [filter, setFilter] = useState('');
  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState<GroupState | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [reload, setReload] = useState(0);
  const [roomsOpen, setRoomsOpen] = useState(() => window.innerWidth > 760);
  const roomBrowserId = useId();
  const noticeTimer = useRef<number | null>(null);
  const activity = useChatActivity();
  useEffect(() => {
    const toggle = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'b') {
        event.preventDefault(); setRoomsOpen((value) => !value);
      }
    };
    window.addEventListener('keydown', toggle);
    return () => window.removeEventListener('keydown', toggle);
  }, []);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);
  useEffect(() => () => { if (noticeTimer.current) window.clearTimeout(noticeTimer.current); }, []);

  useEffect(() => {
    const c = new AbortController();
    Promise.all([listModels(c.signal), groupOptions(c.signal)]).then(([models, options]) => {
      setDefaultRoute(options.defaultRoute);
      setRoutes(options.defaultRoute && !models.some((r) => r.model === options.defaultRoute?.model && r.endpointId === options.defaultRoute?.endpointId)
        ? [options.defaultRoute, ...models] : models);
    }).catch((error: Error) => { if (!c.signal.aborted) { setRoutes([]); say(error.message, 'warn'); } });
    listPresets(c.signal).then((all) => setPersonas(all.filter((p) => p.own))).catch(() => undefined);
    listGroupPresets(c.signal).then(setPresets).catch(() => undefined);
    return () => c.abort();
  }, [say]);

  useEffect(() => {
    const c = new AbortController();
    const load = () => listGroups(c.signal, projectId && !spaceId ? undefined : spaceId || undefined).then((all) => {
      if (c.signal.aborted) return;
      setGroups(all.filter((g) => (projectId
        ? g.projectId === projectId || (spaceId !== null && (g.space_id || 'ecosystem') === spaceId)
        : (g.space_id || 'ecosystem') === spaceId)));
    }).catch((error: Error) => { if (!c.signal.aborted) { setGroups([]); say(error.message, 'warn'); } });
    void load();
    const timer = window.setInterval(load, 5000);
    return () => { c.abort(); window.clearInterval(timer); };
  }, [spaceId, projectId, reload, say]);

  // Opening the agora lands in its latest room, like opening a chat app.
  const landed = useRef(false);
  useEffect(() => {
    if (landed.current || selected || creating || !groups?.length) return;
    landed.current = true;
    onSelect(groups[0].parentId);
  }, [groups, selected, creating, onSelect]);

  const visible = useMemo(() => (groups ?? []).filter((g) => g.name.toLowerCase().includes(filter.toLowerCase())), [groups, filter]);
  const open = selected && !creating ? selected : '';
  const home = spaceId || 'ecosystem';

  return (
    <div className="fs-agora-rooms" data-testid="agora-rooms" data-rooms-open={roomsOpen || undefined}>
      <aside id={roomBrowserId} className="fs-agora-rooms__list fs-studio__sessions" aria-label={t('Conversations')}>
        <div className="fs-studio__sessions-head">
          <span className="fs-panel__label">{t('Agora')}</span>
          <IconButton size="sm" icon={Plus} label={t('New room')} onClick={() => { setCreating(true); onSelect(''); if (window.innerWidth <= 760) setRoomsOpen(false); }} testId="agora-new-room" />
          <IconButton size="sm" icon={X} label={t('Close the conversation list')} onClick={() => setRoomsOpen(false)} />
        </div>
        <label className="fs-agora-rooms__search">
          <Search size={14} aria-hidden="true" />
          <input aria-label={t('Find a room')} placeholder={t('Find a room')} value={filter} onChange={(e) => setFilter(e.target.value)} />
        </label>
        {groups === null && <Skeleton label={t('Loading rooms…')} count={3} height="48px" />}
        {groups !== null && !groups.length && <p className="fs-muted fs-agora-rooms__hint">{emptyHint ?? t('No rooms in this space yet.')}</p>}
        <ul className="fs-agora-rooms__items fs-studio__list">
          {visible.map((g) => {
            const live = g.participants.some((p) => sessionActivity(activity, p.sessionId) !== null);
            return (
              <li key={g.parentId}>
                <button type="button" className="fs-studio__session" aria-current={open === g.parentId ? 'page' : undefined} onClick={() => { if (window.innerWidth <= 760) setRoomsOpen(false); setCreating(false); onSelect(g.parentId); }} data-testid="agora-room">
                  <span className="fs-studio__session-name">{live && <span className="fs-agora-rooms__live" aria-label={t('Working')} />}<Users size={11} aria-hidden="true" />{g.name}</span>
                  <span className="fs-studio__session-meta">{g.participants.map(speakerName).join(' · ')}</span>
                </button>
              </li>
            );
          })}
        </ul>
        {sidebarFooter}
      </aside>

      {roomsOpen && <button type="button" className="fs-agora-rooms__scrim" aria-label={t('Close the conversation list')} onClick={() => setRoomsOpen(false)} />}

      <section className="fs-agora-rooms__room">
        {open ? (
          <RoomChat key={open} groupId={open} projectId={projectId} say={say} conversationsOpen={roomsOpen} onToggleConversations={() => setRoomsOpen(!roomsOpen)}
            onNewRoom={() => { setCreating(true); onSelect(''); }}
            onManage={(state) => setManaging(state)}
            onClosed={() => { onSelect(''); setReload((n) => n + 1); }} />
        ) : routes === null || (groups === null && !creating) ? (
          <Skeleton label={t('Loading models')} count={4} height="40px" />
        ) : routes.length === 0 ? (
          <EmptyState icon={Users} title={t('No models')} body={t('Add an endpoint in Settings → Models first.')} />
        ) : (creating || !groups?.length) ? (
          <div className="fs-agora-rooms__setup">
            <Button variant="ghost" icon={MessageSquare} label={t('Conversations')} onClick={() => setRoomsOpen(!roomsOpen)} />
            <h3>{t('New room')}</h3>
            <p className="fs-muted">{t('Several models in one conversation, each as itself or wearing a persona. They answer you and each other.')}</p>
            <RoomSetup key={home + (projectId ?? '')} routes={routes} defaultRoute={defaultRoute} personas={personas} presets={presets}
              spaceId={home} projectId={projectId} say={say} onPresetsChanged={setPresets}
              onStart={(state) => { setCreating(false); setReload((n) => n + 1); onSelect(state.parentId); }} />
          </div>
        ) : (
          <div className="fs-agora-rooms__pick">
            <Users size={22} aria-hidden="true" />
            <p>{t('Open a room on the left or start a new one.')}</p>
          </div>
        )}
      </section>

      {managing && routes && (
        <Dialog open onOpenChange={(o) => !o && setManaging(null)} title={t('Manage members')} testId="group-members">
          <RoomSetup key={managing.parentId} routes={routes} defaultRoute={defaultRoute} personas={personas} presets={presets}
            spaceId={managing.space_id || home} projectId={managing.projectId} existing={managing} say={say} onPresetsChanged={setPresets}
            onStart={() => { setManaging(null); setReload((n) => n + 1); }} />
        </Dialog>
      )}
      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
