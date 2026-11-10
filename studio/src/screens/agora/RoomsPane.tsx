import { Check, Users, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { groupOptions, listGroupPresets, type GroupPreset, type GroupState } from '../../adapters/group';
import { listModels, type ModelRoute } from '../../adapters/chat';
import { listPresets, type Preset } from '../../adapters/presets';
import { Dialog, EmptyState, Skeleton, Toast } from '../../components';
import { RoomChat } from '../group/RoomChat';
import { RoomSetup } from '../group/RoomSetup';
import { t } from '../../i18n';
import '../group.css';

/**
 * A room of models inside a server: the same group chat the Agora always had
 * (several models talking with the person and each other), opened from the
 * server's channel list instead of its own screen.
 */
export function RoomsPane({ spaceId, projectId, roomId, onSelect, onChanged }: {
  spaceId: string; projectId?: string; roomId: string; onSelect: (id: string) => void; onChanged: () => void;
}) {
  const [routes, setRoutes] = useState<ModelRoute[] | null>(null);
  const [defaultRoute, setDefaultRoute] = useState<ModelRoute | null>(null);
  const [personas, setPersonas] = useState<Preset[]>([]);
  const [presets, setPresets] = useState<GroupPreset[]>([]);
  const [managing, setManaging] = useState<GroupState | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const timer = useRef<number | null>(null);
  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
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
  const creating = roomId === 'new';
  return (
    <div className="fs-agx-room" data-testid="agora-room">
      {!creating ? (
        <RoomChat key={roomId} groupId={roomId} projectId={projectId} say={say} onNewRoom={() => onSelect('new')}
          onManage={(state) => setManaging(state)} onClosed={() => { onChanged(); onSelect(''); }} />
      ) : routes === null ? (
        <Skeleton label={t('Loading models')} count={4} height="40px" />
      ) : routes.length === 0 ? (
        <EmptyState icon={Users} title={t('No models')} body={t('Add an endpoint in Settings → Models first.')} />
      ) : (
        <div className="fs-agora-rooms__setup">
          <h3>{t('New room')}</h3>
          <p className="fs-muted">{t('Several models in one conversation, each as itself or wearing a persona. They answer you and each other.')}</p>
          <RoomSetup key={spaceId + (projectId ?? '')} routes={routes} defaultRoute={defaultRoute} personas={personas} presets={presets}
            spaceId={spaceId} projectId={projectId} say={say} onPresetsChanged={setPresets}
            onStart={(state) => { onChanged(); onSelect(state.parentId); }} />
        </div>
      )}
      {managing && routes && (
        <Dialog open onOpenChange={(o) => !o && setManaging(null)} title={t('Manage members')} testId="group-members">
          <RoomSetup key={managing.parentId} routes={routes} defaultRoute={defaultRoute} personas={personas} presets={presets}
            spaceId={managing.space_id || spaceId} projectId={managing.projectId} existing={managing} say={say} onPresetsChanged={setPresets}
            onStart={() => { setManaging(null); onChanged(); }} />
        </Dialog>
      )}
      {notice && <Toast>{notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}</Toast>}
    </div>
  );
}
