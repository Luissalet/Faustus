import { Check, ChevronDown, Play, Plus, Save, Users, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button, Dialog, IconButton } from '../../components';
import { useCoordinationDraft } from '../../components/coordinationNavigation';
import type { ModelRoute } from '../../adapters/chat';
import {
  DEFAULT_DISCUSSION_ROUNDS, presetFrom, saveGroupPresets, speakerName, startGroup, updateGroup,
  type GroupMode, type GroupPreset, type GroupState, type Participant,
} from '../../adapters/group';
import type { Preset } from '../../adapters/presets';
import { hueIndex, initials } from '../../lib/mail';
import ModelPalette from '../ModelPalette';
import { t } from '../../i18n';

/** How many rounds the members may answer each other after each message. */
export const ROUND_CHOICES = [0, 1, 2, 3, 5, 8, 12];

function Avatar({ name }: { name: string }) {
  return (
    <span className="fs-avatar" data-hue={hueIndex(name)} data-size="sm" aria-hidden="true">
      {initials(name)}
    </span>
  );
}

/** Two seats by default, both on the configured model (the Sparks), or the first two routes. */
function defaultSeats(routes: ModelRoute[], defaultRoute: ModelRoute | null): Participant[] {
  const second = routes.find((route) => defaultRoute && route.model !== defaultRoute.model && !route.missing);
  const seats = defaultRoute ? [defaultRoute, second ?? defaultRoute] : routes.slice(0, 2);
  return seats.map((route) => ({ route, personaId: '', personaName: '', personaPrompt: '' }));
}

/** Set the table of a room: who sits, as whom, taking turns or all at once. */
export function RoomSetup({ routes, defaultRoute, personas, presets, onStart, onPresetsChanged, say, spaceId, projectId, existing }: {
  routes: ModelRoute[]; defaultRoute: ModelRoute | null; personas: Preset[]; presets: GroupPreset[];
  onStart: (state: GroupState) => void; onPresetsChanged: (p: GroupPreset[]) => void; say: (m: string, tone?: 'ok' | 'warn') => void;
  spaceId: string; projectId?: string; existing?: GroupState;
}) {
  const prefix = 'agora:setup:groups:' + spaceId + ':' + (projectId || '') + ':' + (existing?.parentId || 'new') + ':';
  const [participants, setParticipants] = useCoordinationDraft<Participant[]>(prefix + 'participants', existing?.participants ?? defaultSeats(routes, defaultRoute));
  const [executionMode, setExecutionMode] = useCoordinationDraft<'chat' | 'agent'>(prefix + 'execution', existing?.executionMode || 'agent');
  const [workspace, setWorkspace] = useCoordinationDraft(prefix + 'workspace', existing?.workspace || '');
  const [mode, setMode] = useCoordinationDraft<GroupMode>(prefix + 'mode', existing?.mode || 'round-robin');
  const [rounds, setRounds] = useCoordinationDraft<number>(prefix + 'rounds', existing?.discussionRounds ?? DEFAULT_DISCUSSION_ROUNDS);
  const [name, setName] = useCoordinationDraft(prefix + 'name', existing?.name || '');
  const [pick, setPick] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [saveName, setSaveName] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);

  const setPersona = (i: number, id: string) => {
    const p = personas.find((x) => x.id === id);
    setParticipants((cur) => cur.map((x, j) => (j === i ? { ...x, personaId: p?.id ?? '', personaName: p?.name ?? '', personaPrompt: p?.systemPrompt ?? '' } : x)));
  };

  const loadPreset = (g: GroupPreset) => {
    const list: Participant[] = [];
    for (const p of g.participants) {
      const route = routes.find((r) => r.model === p.modelId && (!p.endpointId || r.endpointId === p.endpointId));
      if (!route) continue;
      const persona = p.characterId ? personas.find((x) => x.id === p.characterId) : undefined;
      list.push({ route, personaId: persona?.id ?? '', personaName: persona?.name ?? p.characterName ?? '',
        personaPrompt: persona?.systemPrompt ?? p.personaPrompt ?? '', autonomyPreset: p.autonomyPreset, inputTokenBudget: p.inputTokenBudget });
    }
    if (!list.length) {
      say(t('None of that group’s models is available now.'), 'warn');
      return;
    }
    setParticipants(list);
    if (list.length < g.participants.length) say(t('{n} of {m} participants found; the rest are not available.', { n: list.length, m: g.participants.length }), 'warn');
  };

  const savePreset = async () => {
    const label = (saveName ?? '').trim();
    if (!label) return;
    const next = [...presets.filter((g) => g.name !== label), presetFrom(label, participants)];
    try {
      await saveGroupPresets(next);
      onPresetsChanged(next);
      say(t('Group «{name}» saved.', { name: label }));
      setSaveName(null);
    } catch (err) {
      say((err as Error).message, 'warn');
    }
  };

  const deletePreset = async (g: GroupPreset) => {
    const next = presets.filter((x) => x !== g);
    try {
      await saveGroupPresets(next);
      onPresetsChanged(next);
      say(t('Group «{name}» deleted.', { name: g.name }));
    } catch (err) {
      say((err as Error).message, 'warn');
    }
  };

  const start = async () => {
    setStarting(true);
    try {
      const created = existing
        ? await updateGroup(existing, {participants: participants as GroupState['participants'], mode, executionMode, workspace, name, discussionRounds: rounds})
        : await startGroup(participants, mode, executionMode, workspace, spaceId, name, { projectId, discussionRounds: rounds });
      if (mounted.current) onStart(created);
    } catch (err) {
      say((err as Error).message || t('Could not start the group.'), 'warn');
      setStarting(false);
    }
  };

  return (
    <section className="fs-grp__setup" aria-label={t('Set the table')}>
      <label className="fs-grp__option">{t('Room name')}<input className="fs-field" value={name} onChange={(event) => setName(event.target.value)} placeholder={t('Named after its members if empty')} /></label>
      {presets.length > 0 && (
        <div className="fs-grp__presets">
          <span className="fs-grp__label">{t('Saved groups')}</span>
          {presets.map((g) => (
            <span key={g.name} className="fs-grp__preset">
              <button type="button" className="fs-chip" onClick={() => loadPreset(g)} title={g.participants.map((p) => p.characterName || p.modelDisplay).join(', ')}>
                <Users size={11} aria-hidden="true" /> {g.name}
              </button>
              <IconButton icon={X} label={t('Delete group {name}', { name: g.name })} size="sm" onClick={() => void deletePreset(g)} />
            </span>
          ))}
        </div>
      )}
      <ul className="fs-grp__rows">
        {participants.map((p, i) => (
          <li key={i} className="fs-grp__row">
            <Avatar name={speakerName(p)} />
            <button type="button" className="fs-grp__pick" onClick={() => setPick(i)} data-testid="group-model">
              {p.route.model} <small>{p.route.endpointName}</small> <ChevronDown size={11} aria-hidden="true" />
            </button>
            <select className="fs-field fs-grp__persona" value={p.personaId} onChange={(e) => setPersona(i, e.target.value)} aria-label={t('Persona for {model}', { model: p.route.model })}>
              <option value="">{t('As itself')}</option>
              {personas.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
            <select className="fs-field fs-grp__persona" value={p.autonomyPreset ?? ''}
              aria-label={t('Tools for {model}', { model: p.route.model })}
              onChange={(event) => setParticipants((cur) => cur.map((member, j) => j === i ? { ...member, autonomyPreset: event.target.value as Participant['autonomyPreset'] || undefined } : member))}>
              <option value="">{t('Project defaults')}</option>
              <option value="supervised">{t('Supervised')}</option>
              <option value="bounded_autonomous">{t('Autonomous')}</option>
              <option value="read_only">{t('Read only')}</option>
            </select>
            <IconButton icon={X} label={t('Remove {name}', { name: speakerName(p) })} size="sm" onClick={() => setParticipants((cur) => cur.filter((_, j) => j !== i))} disabled={participants.length <= 1} />
          </li>
        ))}
      </ul>
      <div className="fs-inline">
        <Button variant="ghost" size="sm" icon={Plus} label={t('Add a participant')} onClick={() => setParticipants((cur) => [...cur, { route: defaultRoute ?? routes[cur.length % routes.length], personaId: '', personaName: '', personaPrompt: '' }])} disabled={!routes.length} testId="group-add" />
        <span className="fs-spacer" />
        <div className="fs-seg" role="radiogroup" aria-label={t('Turns')}>
          <button type="button" role="radio" aria-checked={mode === 'round-robin'} onClick={() => setMode('round-robin')}>{t('Taking turns')}</button>
          <button type="button" role="radio" aria-checked={mode === 'parallel'} onClick={() => setMode('parallel')}>{t('All at once')}</button>
        </div>
      </div>
      <p className="fs-muted">{mode === 'round-robin' ? t('Each participant answers after the previous one and sees what was said.') : t('Everyone answers at the same time, then each one reads the others and can answer them.')}</p>
      <div className="fs-grp__work-options">
        <label className="fs-grp__option">{t('Between themselves')}
          <select className="fs-field" value={rounds} onChange={(e) => setRounds(Number(e.target.value))} data-testid="group-rounds">
            {ROUND_CHOICES.map((n) => <option key={n} value={n}>{n === 0 ? t('Only answer me') : t('Up to {n} rounds of replies', { n })}</option>)}
          </select>
        </label>
        <label className="fs-grp__option">{t('Participants can')}
          <select className="fs-field" value={executionMode} onChange={(e) => setExecutionMode(e.target.value as 'chat' | 'agent')}>
            <option value="agent">{t('Use tools and work as agents')}</option>
            <option value="chat">{t('Discuss without tools')}</option>
          </select>
        </label>
        <label className="fs-grp__option">{t('Working folder')}
          <input className="fs-field" value={workspace} onChange={(e) => setWorkspace(e.target.value)} placeholder={projectId ? t('The project folder') : t('Use the configured working folder')} />
        </label>
      </div>
      <p className="fs-muted">{t('A member answers a peer with @name; the one mentioned speaks next. Whoever has nothing new to add stays quiet.')}</p>
      <div className="fs-inline">
        <Button variant="ghost" size="sm" icon={Save} label={t('Save as a group…')} onClick={() => setSaveName('')} disabled={participants.length < 2} />
        <span className="fs-spacer" />
        <Button variant="primary" size="sm" icon={existing ? Check : Play} label={t(existing ? 'Save members' : 'Open the room')} loading={starting} disabled={participants.length < 2} onClick={() => void start()} testId="group-start" />
      </div>
      <ModelPalette open={pick !== null} onOpenChange={(o) => !o && setPick(null)} routes={routes} current={pick !== null ? participants[pick]?.route ?? null : null} onPick={(r) => { if (pick !== null) setParticipants((cur) => cur.map((x, j) => (j === pick ? { ...x, route: r } : x))); setPick(null); }} />
      {saveName !== null && (
        <Dialog open onOpenChange={(o) => !o && setSaveName(null)} title={t('Save as a group')} testId="group-save" footer={<Button variant="primary" size="sm" label={t('Save')} disabled={!saveName.trim()} onClick={() => void savePreset()} />}>
          <input className="fs-field" value={saveName} onChange={(e) => setSaveName(e.target.value)} placeholder={t('Group name')} aria-label={t('Group name')} autoFocus />
        </Dialog>
      )}
    </section>
  );
}
