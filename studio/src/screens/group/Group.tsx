import { Check, ChevronDown, ExternalLink, Play, Plus, Save, Send, Square, Trash2, Users, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import {useCoordinationParams, useCoordinationDraft, coordinationAttempt, acknowledgeCoordinationAttempt} from '../../components/coordinationNavigation';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton, Toast } from '../../components';
import { listModels, loadHistory, type ModelRoute } from '../../adapters/chat';
import {
  archiveGroup, groupOptions, groupSnapshot, listGroups, listGroupPresets, openGroup,
  presetFrom, questionFrom, resumeGroup, sendGroup, stopGroup, saveGroupPresets, speakerName, startGroup,
  updateGroup, type GroupMode, type GroupPreset, type GroupSnapshot, type GroupState, type GroupToolResult, type Participant,
} from '../../adapters/group';
import { listPresets, type Preset } from '../../adapters/presets';
import { hueIndex, initials } from '../../lib/mail';
import ModelPalette from '../ModelPalette';
import { Rich } from '../rich';
import { AskCard } from '../studio/Transcript';
import { t, tn } from '../../i18n';
import '../group.css';
import '../studio.css';
import {CoordinationSpaces} from '../../components/CoordinationSpaces';

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
  tools?: GroupToolResult[];
}

function Avatar({ name }: { name: string }) {
  return (
    <span className="fs-avatar" data-hue={hueIndex(name)} data-size="sm" aria-hidden="true">
      {initials(name)}
    </span>
  );
}

/* ── Setup ── */

function Setup({ routes, defaultRoute, personas, presets, onStart, onPresetsChanged, say, spaceId, existing }: { routes: ModelRoute[]; defaultRoute: ModelRoute | null; personas: Preset[]; presets: GroupPreset[]; onStart: (state: GroupState) => void; onPresetsChanged: (p: GroupPreset[]) => void; say: (m: string, tone?: 'ok' | 'warn') => void; spaceId: string; existing?: GroupState }) {
  const prefix = 'agora:setup:groups:' + spaceId + ':' + (existing?.parentId || 'new') + ':';
  const [participants, setParticipants] = useCoordinationDraft<Participant[]>(prefix + 'participants', existing?.participants ?? (defaultRoute ? [defaultRoute, defaultRoute] : routes.slice(0, 2)).map((route) => ({ route, personaId: '', personaName: '', personaPrompt: '' })));
  const [executionMode, setExecutionMode] = useCoordinationDraft<'chat' | 'agent'>(prefix + 'execution', existing?.executionMode || 'agent');
  const [workspace, setWorkspace] = useCoordinationDraft(prefix + 'workspace', existing?.workspace || '');
  const [mode, setMode] = useCoordinationDraft<GroupMode>(prefix + 'mode', existing?.mode || 'round-robin');
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
      const created = existing
        ? await updateGroup(existing, {participants: participants as GroupState['participants'], mode, executionMode, workspace, name})
        : await startGroup(participants, mode, executionMode, workspace, spaceId, name);
      if (mounted.current) onStart(created);
    } catch (err) {
      say((err as Error).message || t('Could not start the group.'), 'warn');
      setStarting(false);
    }
  };

  return (
    <section className="fs-grp__setup" aria-label={t('Set the table')}>
      <label className="fs-grp__option">{t('Room name')}<input className="fs-field" value={name} onChange={(event) => setName(event.target.value)} /></label>
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
          <button type="button" role="radio" aria-checked={mode === 'round-robin'} onClick={() => setMode('round-robin')}>
            {t('Taking turns')}
          </button>
          <button type="button" role="radio" aria-checked={mode === 'parallel'} onClick={() => setMode('parallel')}>
            {t('All at once')}
          </button>
        </div>
      </div>
      <p className="fs-muted">{mode === 'round-robin' ? t('Each participant answers after the previous one and sees what was said.') : t('Everyone answers at the same time; they see each other’s replies from the next message on.')}</p>
      <div className="fs-grp__work-options">
        <label className="fs-grp__option">{t('Participants can')}
          <select className="fs-field" value={executionMode} onChange={(e) => setExecutionMode(e.target.value as 'chat' | 'agent')}>
            <option value="agent">{t('Use tools and work as agents')}</option>
            <option value="chat">{t('Discuss without tools')}</option>
          </select>
        </label>
        <label className="fs-grp__option">{t('Working folder')}
          <input className="fs-field" value={workspace} onChange={(e) => setWorkspace(e.target.value)} placeholder={t('Use the configured working folder')} />
        </label>
      </div>
      <div className="fs-inline">
        <Button variant="ghost" size="sm" icon={Save} label={t('Save as a group…')} onClick={() => setSaveName('')} disabled={participants.length < 2} />
        <span className="fs-spacer" />
        <Button variant="primary" size="sm" icon={existing ? Check : Play} label={t(existing ? 'Save members' : 'Start')} loading={starting} disabled={participants.length < 2} onClick={() => void start()} testId="group-start" />
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

export function GroupScreen({embedded = false}: {embedded?: boolean} = {}) {
  const navigate = useNavigate();
  const [params, setParams] = useCoordinationParams('groups');
  const spaceId = params.get('space_id') || 'ecosystem';
  const [routes, setRoutes] = useState<ModelRoute[] | null>(null);
  const [personas, setPersonas] = useState<Preset[]>([]);
  const [presets, setPresets] = useState<GroupPreset[]>([]);
  const [defaultRoute, setDefaultRoute] = useState<ModelRoute | null>(null);
  const [groups, setGroups] = useState<GroupState[]>([]);
  const [state, setState] = useState<GroupState | null>(null);
  const [opening, setOpening] = useState(Boolean(params.get('s')));
  const [editing, setEditing] = useState(false);
  const [recipient, setRecipient] = useState('');
  const [snapshot, setSnapshot] = useState<GroupSnapshot | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useCoordinationDraft('agora:draft:groups:' + spaceId + ':' + (params.get('s') || ''), '');
  const [sending, setSending] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const sendTarget = useRef('');
  sendTarget.current = spaceId + ':' + (params.get('s') || '');
  const busy = snapshot?.busy ?? false;
  const endRef = useRef<HTMLDivElement>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);

  useEffect(() => {
    const c = new AbortController();
    Promise.all([listModels(c.signal), groupOptions(c.signal), listGroups(c.signal, spaceId)]).then(([models, options, saved]) => {
      setDefaultRoute(options.defaultRoute);
      setRoutes(options.defaultRoute && !models.some((r) => r.model === options.defaultRoute?.model && r.endpointId === options.defaultRoute?.endpointId)
        ? [options.defaultRoute, ...models] : models);
      setGroups(saved);
    }).catch((error: Error) => { if (!c.signal.aborted) { setRoutes([]); say(error.message, 'warn'); } });
    listPresets(c.signal).then((all) => setPersonas(all.filter((p) => p.own))).catch(() => undefined);
    listGroupPresets(c.signal).then(setPresets).catch(() => undefined);
    return () => { c.abort(); if (noticeTimer.current) window.clearTimeout(noticeTimer.current); };
  }, [say, spaceId]);

  const selectedId = params.get('s');
  useEffect(() => {setRecipient(''); setEditing(false); setSending(false);}, [selectedId, spaceId]);
  useEffect(() => {
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let first = true;
    setState(null); setSnapshot(null); setEntries([]); setOpening(Boolean(selectedId)); setConnectionError('');
    if (!selectedId) return () => c.abort();
    const refresh = async () => {
      try {
        const current = first ? await openGroup(selectedId, c.signal) : await groupSnapshot(selectedId, c.signal);
        first = false;
        const history = await loadHistory(selectedId, c.signal).catch(() => ({history: []}));
        if (c.signal.aborted) return;
        // Historical pre-driver messages remain available. Durable group turns
        // and replies come from canonical coordination, not the model context.
        const rows: Entry[] = history.history.filter((m) => m.role !== 'system' && !m.metadata.group_turn_id && !m.metadata.group_step_id).map((m) => ({
          id: String(m.metadata.group_step_id ?? m.metadata.group_turn_id ?? m.metadata.group_answer_id ?? `history-${m.index}`),
          role: m.role === 'user' ? 'user' : 'participant', speaker: m.role === 'user' ? t('You') : String(m.metadata.group_model ?? m.metadata.model ?? ''),
          model: String(m.metadata.model ?? ''), text: m.content, streaming: false, error: String(m.metadata.group_error ?? ''),
          tools: Array.isArray(m.metadata.group_tool_results) ? m.metadata.group_tool_results as GroupToolResult[] : [],
        }));
        for (const turn of current.turns) {
          rows.push({ id: turn.id, role: 'user', speaker: turn.status === 'queued' ? t('Queued') : t('You'), model: '', text: turn.message, streaming: false, error: '' });
          for (const step of current.steps.filter((s) => s.turn_id === turn.id)) {
            if (rows.some((r) => r.id === step.id) || step.status === 'queued') continue;
            const member = (turn.participants ?? current.group.participants).find((p) => p.sessionId === step.member_id);
            if (!member) continue;
            rows.push({ id: step.id, role: 'participant', speaker: speakerName(member), model: member.route.model,
              text: step.text, streaming: step.status === 'running', error: step.error || (step.status === 'stopped' ? t('Stopped.') : ''), tools: step.detail.tool_results });
          }
        }
        setState(current.group); setSnapshot(current); setEntries(rows); setOpening(false); setConnectionError('');
      } catch (error) {
        if (!c.signal.aborted) { setConnectionError((error as Error).message); setOpening(false); }
      } finally {
        if (!c.signal.aborted) timer = setTimeout(() => void refresh(), 1000);
      }
    };
    void refresh();
    // The browser owns only its observer. Leaving never stops the saved work.
    return () => { c.abort(); clearTimeout(timer); };
  }, [selectedId, spaceId]);

  const transcriptText = entries.map((e) => e.text).join('');
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }); }, [transcriptText]);

  const send = async () => {
    const message = draft.trim();
    if (!message || !state || sending) return;
    const scope = spaceId + ':' + state.parentId;
    const attemptScope = 'groups:' + scope;
    const request = coordinationAttempt(attemptScope, message, recipient);
    setSending(true);
    try {
      const answer = await sendGroup(state.parentId, message, request.id, recipient ? [recipient] : []);
      if (!answer?.turn_id) throw new Error(t('Message not confirmed. Retry the same send.'));
      acknowledgeCoordinationAttempt(attemptScope, request.id);
      setDraft((current) => current.trim() === message ? '' : current);
    } catch (error) { say((error as Error).message, 'warn'); }
    finally { if (sendTarget.current === scope) setSending(false); }
  };
  const stop = async () => {
    if (!state) return;
    try { await stopGroup(state.parentId); } catch (error) { say((error as Error).message, 'warn'); }
  };
  const leave = () => {
    setState(null); setEntries([]); setSnapshot(null); setParams({space_id: spaceId}, { replace: true });
    void listGroups().then(setGroups).catch((error: Error) => say(error.message, 'warn'));
  };
  const forget = async () => {
    if (!state) return;
    try { await archiveGroup(state.parentId); leave(); say(t('Group closed. Its transcript stays in Studio.')); }
    catch (error) { say((error as Error).message, 'warn'); }
  };
  const answer = async (message: string, fields: Record<string, unknown>) => {
    if (!state || sending) return;
    setSending(true);
    try { await resumeGroup(state.parentId, message, fields); }
    catch (error) { say((error as Error).message, 'warn'); }
    finally { setSending(false); }
  };
  const pending = snapshot?.steps.find((s) => questionFrom(s));
  const ask = pending ? questionFrom(pending) : null;

  const participants = useMemo(() => state?.participants ?? [], [state]);

  if (routes === null) return <Skeleton label={t('Loading models')} count={4} height="40px" />;

  return (
    <div className="fs-screen fs-grp" data-testid="group">
      {!embedded && <CoordinationSpaces value={spaceId} onChange={(id) => setParams({space_id: id}, {replace: true})} />}
      <header className="fs-screen__head">
        <div>
          <h2 className="fs-screen__title">{state ? state.name : t('Conversation')}</h2>
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
                { label: t('Manage members'), icon: Users, onSelect: () => setEditing(true) },
                { label: t('Close this group (keep the transcript)'), icon: Trash2, variant: 'danger', onSelect: () => void forget() },
              ]}
            />
          </div>
        )}
      </header>

      {connectionError && <p className="fs-notice" data-tone="danger" role="alert">{connectionError} {t('Reconnecting…')}</p>}
      {opening && <Skeleton label={t('Opening group')} count={2} height="40px" />}
      {!embedded && !state && !opening && groups.length > 0 && <div className="fs-grp__presets" aria-label={t('Open groups')}>
        <span className="fs-grp__label">{t('Open groups')}</span>
        {groups.map((g) => <button key={g.parentId} type="button" className="fs-chip" onClick={() => setParams({ s: g.parentId, space_id: spaceId })}>{g.name}</button>)}
      </div>}
      {!state && !opening && routes.length === 0 && <EmptyState icon={Users} title={t('No models')} body={t('Add an endpoint in Settings → Models first.')} />}
      {!state && !opening && routes.length > 0 && <Setup key={spaceId} routes={routes} defaultRoute={defaultRoute} personas={personas} presets={presets} spaceId={spaceId} onStart={(s) => { setState(s); setEntries([]); setParams({ s: s.parentId, space_id: spaceId }, { replace: true }); }} onPresetsChanged={setPresets} say={say} />}

      {state && (
        <>
          <div className="fs-grp__table" aria-label={t('Participants')}>
            {participants.map((p) => (
              <span key={p.sessionId} className="fs-grp__seat" title={p.route.model}>
                <Avatar name={speakerName(p)} />
                {speakerName(p)}
                {p.autonomyPreset === 'read_only' && <small>{t('Read only')}</small>}
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
                {Boolean(e.tools?.length) && <details className="fs-grp__tools">
                  <summary>{t('Tool results')} · {e.tools?.length}</summary>
                  {e.tools?.map((tool, i) => <div key={tool.call_id ?? i}>
                    <p className="fs-muted">{tool.tool} · {tool.command} {tool.exit_code === undefined ? '' : `(${tool.exit_code})`}</p>
                    {tool.result_status === 'outcome_unknown' && <p className="fs-notice">{t('Outcome not confirmed')} {tool.uncertainty ? JSON.stringify(tool.uncertainty) : ''}</p>}
                    <pre className="fs-grp__tool-output"><code>{String(tool.output ?? '')}</code></pre>
                  </div>)}
                </details>}
              </article>
            ))}
            <div ref={endRef} />
          </section>
          {ask && <AskCard ask={ask} busy={sending}
            onAnswer={(text, optionIds) => void answer(text, { question_id: ask.questionId, question_revision: ask.revision, option_ids: optionIds })}
            onApproval={(decision) => void answer('', { tool_approval_id: ask.approvalId, tool_approval_decision: decision })} />}
          {busy && <p className="fs-muted" role="status">{ask ? t('Waiting for your answer') : t('Working. New messages join the queue; you can leave and return.')}</p>}
          <footer className="fs-grp__composer">
            <textarea
              className="fs-grp__textarea"
              aria-label={t('Message to the group')}
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
            {busy && <Button variant="danger" size="sm" icon={Square} label={t('Stop')} onClick={() => void stop()} />}
            <label className="fs-inline">{t('To')}<select className="fs-field" value={recipient} onChange={(event) => setRecipient(event.target.value)} aria-label={t('Message recipients')}>
              <option value="">{t('Everyone')}</option>{participants.map((p) => <option key={p.sessionId} value={p.sessionId}>{speakerName(p)}</option>)}
            </select></label>
            <Button variant="primary" size="sm" icon={Send} label={busy ? t('Queue message') : t('Send')} loading={sending} disabled={!draft.trim() || sending} onClick={() => void send()} testId="group-send" />
          </footer>
        </>
      )}

      {editing && state && <Dialog open onOpenChange={setEditing} title={t('Manage members')} testId="group-members">
        <Setup key={state.parentId} routes={routes} defaultRoute={defaultRoute} personas={personas} presets={presets}
          spaceId={spaceId} existing={state} onStart={(next) => {setState(next); setEditing(false);}}
          onPresetsChanged={setPresets} say={say} />
      </Dialog>}
      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
