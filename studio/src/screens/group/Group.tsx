import { Check, ChevronDown, ExternalLink, Play, Plus, Save, Send, Square, Trash2, Users, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton, Toast } from '../../components';
import { listModels, loadHistory, sendTurn, stopChat, type ModelRoute } from '../../adapters/chat';
import {
  forgetGroupState,
  listGroupPresets,
  loadGroupState,
  presetFrom,
  recordReply,
  recordUser,
  saveGroupPresets,
  saveGroupState,
  speakerName,
  startGroup,
  type GroupMode,
  type GroupPreset,
  type GroupState,
  type Participant,
} from '../../adapters/group';
import { listPresets, type Preset } from '../../adapters/presets';
import { hueIndex, initials } from '../../lib/mail';
import ModelPalette from '../ModelPalette';
import { Rich } from '../rich';
import { t, tn } from '../../i18n';
import '../group.css';

/**
 * Group chat: several models talk with you and with each other. Set the
 * table (who sits, wearing which persona, all at once or taking turns),
 * then talk. The parent session keeps the transcript, so it also shows in
 * Studio's list with a group mark.
 */

interface Entry {
  id: string;
  role: 'user' | 'participant';
  speaker: string;
  model: string;
  text: string;
  streaming: boolean;
  error: string;
}

const uid = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;

function Avatar({ name }: { name: string }) {
  return (
    <span className="fs-avatar" data-hue={hueIndex(name)} data-size="sm" aria-hidden="true">
      {initials(name)}
    </span>
  );
}

/* ── Setup ── */

function Setup({ routes, personas, presets, onStart, onPresetsChanged, say }: { routes: ModelRoute[]; personas: Preset[]; presets: GroupPreset[]; onStart: (state: GroupState) => void; onPresetsChanged: (p: GroupPreset[]) => void; say: (m: string, tone?: 'ok' | 'warn') => void }) {
  const [participants, setParticipants] = useState<Participant[]>(() => routes.slice(0, 2).map((route) => ({ route, personaId: '', personaName: '', personaPrompt: '' })));
  const [mode, setMode] = useState<GroupMode>('round-robin');
  const [pick, setPick] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [saveName, setSaveName] = useState<string | null>(null);

  const setPersona = (i: number, id: string) => {
    const p = personas.find((x) => x.id === id);
    setParticipants((cur) => cur.map((x, j) => (j === i ? { ...x, personaId: p?.id ?? '', personaName: p?.name ?? '', personaPrompt: p?.systemPrompt ?? '' } : x)));
  };

  const loadPreset = (g: GroupPreset) => {
    const list: Participant[] = [];
    for (const p of g.participants) {
      const route = routes.find((r) => r.model === p.modelId && (!p.endpointId || r.endpointId === p.endpointId)) ?? routes.find((r) => r.model === p.modelId);
      if (!route) continue;
      const persona = p.characterId ? personas.find((x) => x.id === p.characterId) : undefined;
      list.push({ route, personaId: persona?.id ?? '', personaName: persona?.name ?? p.characterName ?? '', personaPrompt: persona?.systemPrompt ?? '' });
    }
    if (!list.length) {
      say(t('None of that group’s models is available now.'), 'warn');
      return;
    }
    setParticipants(list);
    if (list.length < g.participants.length) say(t('{n} of {m} participants found; the rest are not available.', { n: list.length, m: g.participants.length }), 'warn');
  };

  const savePreset = async () => {
    const name = (saveName ?? '').trim();
    if (!name) return;
    const next = [...presets.filter((g) => g.name !== name), presetFrom(name, participants)];
    try {
      await saveGroupPresets(next);
      onPresetsChanged(next);
      say(t('Group «{name}» saved.', { name }));
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
      onStart(await startGroup(participants, mode));
    } catch (err) {
      say((err as Error).message || t('Could not start the group.'), 'warn');
      setStarting(false);
    }
  };

  return (
    <section className="fs-grp__setup" aria-label={t('Set the table')}>
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
              {personas.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name}
                </option>
              ))}
            </select>
            <IconButton icon={X} label={t('Remove {name}', { name: speakerName(p) })} size="sm" onClick={() => setParticipants((cur) => cur.filter((_, j) => j !== i))} disabled={participants.length <= 1} />
          </li>
        ))}
      </ul>
      <div className="fs-inline">
        <Button variant="ghost" size="sm" icon={Plus} label={t('Add a participant')} onClick={() => setParticipants((cur) => [...cur, { route: routes[cur.length % routes.length], personaId: '', personaName: '', personaPrompt: '' }])} disabled={participants.length >= 8 || !routes.length} testId="group-add" />
        <span className="fs-spacer" />
        <div className="fs-seg" role="radiogroup" aria-label={t('Turns')}>
          <button type="button" role="radio" aria-checked={mode === 'round-robin'} onClick={() => setMode('round-robin')}>
            {t('Taking turns')}
          </button>
          <button type="button" role="radio" aria-checked={mode === 'parallel'} onClick={() => setMode('parallel')}>
            {t('All at once')}
          </button>
        </div>
      </div>
      <p className="fs-muted">{mode === 'round-robin' ? t('Each participant answers after the previous one and sees what was said.') : t('Everyone answers at the same time; they see each other’s replies from the next message on.')}</p>
      <div className="fs-inline">
        <Button variant="ghost" size="sm" icon={Save} label={t('Save as a group…')} onClick={() => setSaveName('')} disabled={participants.length < 2} />
        <span className="fs-spacer" />
        <Button variant="primary" size="sm" icon={Play} label={t('Start')} loading={starting} disabled={participants.length < 2} onClick={() => void start()} testId="group-start" />
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

/* ── Screen ── */

export function GroupScreen() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [routes, setRoutes] = useState<ModelRoute[] | null>(null);
  const [personas, setPersonas] = useState<Preset[]>([]);
  const [presets, setPresets] = useState<GroupPreset[]>([]);
  const [state, setState] = useState<GroupState | null>(() => {
    const s = params.get('s');
    return s ? loadGroupState(s) : null;
  });
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const controllers = useRef<AbortController[]>([]);
  const busyStopped = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);

  useEffect(() => {
    const c = new AbortController();
    listModels(c.signal).then(setRoutes).catch(() => setRoutes([]));
    listPresets(c.signal).then((all) => setPersonas(all.filter((p) => p.own))).catch(() => undefined);
    listGroupPresets(c.signal).then(setPresets).catch(() => undefined);
    return () => c.abort();
  }, []);

  /* A group opened from the list: the parent's history is the transcript. */
  useEffect(() => {
    if (!state) return;
    const c = new AbortController();
    loadHistory(state.parentId, c.signal)
      .then((h) =>
        setEntries(
          h.history.map((m) => ({
            id: uid(),
            role: m.role === 'user' ? 'user' : 'participant',
            speaker: m.role === 'user' ? t('You') : String(m.metadata.group_model ?? m.metadata.model ?? ''),
            model: String(m.metadata.model ?? ''),
            text: m.content,
            streaming: false,
            error: '',
          })),
        ),
      )
      .catch(() => undefined);
    return () => c.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.parentId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [entries]);

  const patch = (id: string, p: Partial<Entry>) => setEntries((cur) => cur.map((e) => (e.id === id ? { ...e, ...p } : e)));

  const speak = async (g: GroupState, p: GroupState['participants'][number], message: string) => {
    const entry: Entry = { id: uid(), role: 'participant', speaker: speakerName(p), model: p.route.model, text: '', streaming: true, error: '' };
    setEntries((cur) => [...cur, entry]);
    const c = new AbortController();
    controllers.current.push(c);
    let text = '';
    try {
      for await (const ev of sendTurn({ sessionId: p.sessionId, message, mode: 'chat', route: p.route, useRag: false, signal: c.signal })) {
        if (ev.type === 'delta' && !ev.thinking) {
          text += ev.text;
          patch(entry.id, { text });
        } else if (ev.type === 'error') throw new Error(ev.message);
        else if (ev.type === 'done') break;
      }
    } catch (err) {
      patch(entry.id, { error: c.signal.aborted ? t('Stopped.') : (err as Error).message || t('No answer.') });
    } finally {
      patch(entry.id, { streaming: false });
      controllers.current = controllers.current.filter((x) => x !== c);
    }
    if (text.trim()) await recordReply(g, p, text);
    return text;
  };

  const send = async () => {
    const message = draft.trim();
    if (!message || !state || busy) return;
    setDraft('');
    setBusy(true);
    setEntries((cur) => [...cur, { id: uid(), role: 'user', speaker: t('You'), model: '', text: message, streaming: false, error: '' }]);
    await recordUser(state, message);
    if (state.mode === 'parallel') await Promise.all(state.participants.map((p) => speak(state, p, message)));
    else {
      const order = [...state.participants].sort(() => Math.random() - 0.5);
      for (const p of order) {
        if (controllers.current.length === 0 && busyStopped.current) break;
        await speak(state, p, message);
      }
    }
    busyStopped.current = false;
    setBusy(false);
  };

  const stop = () => {
    busyStopped.current = true;
    for (const c of controllers.current) c.abort();
    if (state) for (const p of state.participants) void stopChat(p.sessionId);
  };

  const leave = () => {
    stop();
    setState(null);
    setEntries([]);
    setParams({}, { replace: true });
  };

  const forget = () => {
    if (state) forgetGroupState(state.parentId);
    leave();
    say(t('Group closed. Its transcript stays in Studio.'));
  };

  const participants = useMemo(() => state?.participants ?? [], [state]);

  if (routes === null) return <Skeleton label={t('Loading models')} count={4} height="40px" />;

  return (
    <div className="fs-screen fs-grp" data-testid="group">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{state ? state.name : t('Group chat')}</h1>
          <p className="fs-prose fs-grp__lede">
            {state
              ? `${state.mode === 'parallel' ? t('All at once') : t('Taking turns')} · ${tn(participants.length, '{n} participant', '{n} participants')}`
              : t('Several models in one conversation, each as itself or wearing a persona. They answer you and each other.')}
          </p>
        </div>
        {state && (
          <div className="fs-inline">
            <Button variant="ghost" size="sm" icon={ExternalLink} label={t('Transcript in Studio')} onClick={() => navigate(`/studio?s=${encodeURIComponent(state.parentId)}`)} />
            <Menu
              trigger={<Button variant="secondary" size="sm" icon={Users} label={t('Group')} />}
              align="end"
              items={[
                { label: t('New group'), icon: Plus, onSelect: leave },
                { label: t('Close this group (keep the transcript)'), icon: Trash2, variant: 'danger', onSelect: forget },
              ]}
            />
          </div>
        )}
      </header>

      {!state && routes.length === 0 && <EmptyState icon={Users} title={t('No models')} body={t('Add an endpoint in Settings → Models first.')} />}
      {!state && routes.length > 0 && <Setup routes={routes} personas={personas} presets={presets} onStart={(s) => { setState(s); setEntries([]); setParams({ s: s.parentId }, { replace: true }); }} onPresetsChanged={setPresets} say={say} />}

      {state && (
        <>
          <div className="fs-grp__table" aria-label={t('Participants')}>
            {participants.map((p) => (
              <span key={p.sessionId} className="fs-grp__seat" title={p.route.model}>
                <Avatar name={speakerName(p)} />
                {speakerName(p)}
              </span>
            ))}
          </div>
          <section className="fs-grp__chat" aria-live="polite">
            {entries.length === 0 && <p className="fs-muted fs-grp__empty">{t('Say something to the table.')}</p>}
            {entries.map((e) => (
              <article key={e.id} className="fs-grp__msg" data-role={e.role} data-hue={e.role === 'participant' ? hueIndex(e.speaker) : undefined}>
                <header className="fs-grp__msg-head">
                  {e.role === 'participant' && <Avatar name={e.speaker} />}
                  <b>{e.speaker}</b>
                  {e.model && e.speaker !== e.model && <small>{e.model.split('/').pop()}</small>}
                </header>
                {e.role === 'user' ? <p className="fs-grp__user">{e.text}</p> : e.text ? <Rich text={e.text} /> : e.streaming ? <p className="fs-cmp__waiting"><span className="fs-studio__pulse" /> {t('Thinking…')}</p> : null}
                {e.error && <p className="fs-notice" data-tone="danger">{e.error}</p>}
              </article>
            ))}
            <div ref={endRef} />
          </section>
          <footer className="fs-grp__composer">
            <textarea
              className="fs-grp__textarea"
              rows={2}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t('Talk to the group…')}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              data-testid="group-draft"
            />
            {busy ? <Button variant="danger" size="sm" icon={Square} label={t('Stop')} onClick={stop} /> : <Button variant="primary" size="sm" icon={Send} label={t('Send')} disabled={!draft.trim()} onClick={() => void send()} testId="group-send" />}
          </footer>
        </>
      )}

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
