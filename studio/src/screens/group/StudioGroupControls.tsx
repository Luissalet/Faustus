import { Users } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Button, Dialog, Popover } from '../../components';
import { groupOptions, groupSnapshot, listGroupPresets, updateGroup, type GroupPreset, type GroupSnapshot, type GroupState } from '../../adapters/group';
import { listPresets, type Preset } from '../../adapters/presets';
import type { ModelRoute } from '../../adapters/chat';
import { t } from '../../i18n';
import { MemberActivity } from './MemberActivity';
import { RoomSetup } from './RoomSetup';
import '../group.css';

/** Group controls occupy Studio's existing model-picker slot. */
export function StudioGroupControls({ snapshot, routes, spaceId, projectId, newSignal, onOpen, onChanged, say, navigation }: {
  snapshot: GroupSnapshot | null; routes: ModelRoute[]; spaceId: string; projectId?: string;
  newSignal: number; onOpen: (state: GroupState) => void; onChanged: () => void;
  say: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  navigation?: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [linking, setLinking] = useState(false);
  const [personas, setPersonas] = useState<Preset[]>([]);
  const [presets, setPresets] = useState<GroupPreset[]>([]);
  const [defaultRoute, setDefaultRoute] = useState<ModelRoute | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    listPresets(controller.signal).then((items) => setPersonas(items.filter((item) => item.own))).catch(() => undefined);
    listGroupPresets(controller.signal).then(setPresets).catch(() => undefined);
    groupOptions(controller.signal).then((options) => setDefaultRoute(options.defaultRoute)).catch(() => undefined);
    return () => controller.abort();
  }, []);
  useEffect(() => { if (newSignal) { setCreating(true); setEditing(true); } }, [newSignal]);
  const group = snapshot?.group;
  return <>
    <Popover trigger={<button type="button" className="fs-studio__chip" data-testid="studio-group-members">
      <Users size={14} aria-hidden="true" /><span>{t('Members')}{group ? ` (${group.participants.length})` : ''}</span>
    </button>}>
      {navigation}
      {snapshot && <MemberActivity snapshot={snapshot} />}
      {projectId && group && !group.projectId && <Button size="sm" label={t('Work in this project')} loading={linking}
        onClick={() => {
          setLinking(true);
          void groupSnapshot(group.parentId).then((fresh) => updateGroup(fresh.group, {projectId}))
            .then(() => onChanged()).catch((error: Error) => say(error.message, 'danger')).finally(() => setLinking(false));
        }} />}
      <Button size="sm" label={t('Edit members')} onClick={() => { setCreating(false); setEditing(true); }} disabled={!group} />
      <Button size="sm" label={t('New room')} onClick={() => { setCreating(true); setEditing(true); }} />
    </Popover>
    {editing && <Dialog open title={creating ? t('New room') : t('Edit members')} onOpenChange={(open) => !open && setEditing(false)}>
      <RoomSetup key={creating ? 'new' : group?.parentId} routes={routes} defaultRoute={defaultRoute}
        personas={personas} presets={presets} onPresetsChanged={setPresets}
        spaceId={spaceId} projectId={projectId} existing={creating ? undefined : group ?? undefined}
        say={(text, tone) => say(text, tone === 'warn' ? 'warning' : 'info')}
        onStart={(state) => { setEditing(false); onChanged(); onOpen(state); }} />
    </Dialog>}
  </>;
}
