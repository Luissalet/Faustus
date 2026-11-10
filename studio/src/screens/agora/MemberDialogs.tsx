import { RefreshCw, Square } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  addMember, getConsole, launchWorker, pruneMembers, removeMember, setConsolePreferences, setMemberRank, stopWorker, suggestWorkerName,
  type AgoraGuild, type AgoraMember, type ConsoleLeader, type ConsoleModel, type ConsoleWorker,
} from '../../adapters/agora';
import { Button, Dialog, IconButton, Skeleton } from '../../components';
import { t, tn } from '../../i18n';
import { DEFAULT_RANK, isOnline, RANK_LABEL, RANKS, recall, remember, ROLE_LABEL } from './util';

type Notify = (text: string, tone?: 'ok' | 'warn') => void;

const PROVIDERS = ['claude', 'codex', 'cursor', 'faustus', 'other'] as const;
const VALID_ID = /^[a-z][a-z0-9_.-]{1,40}$/;
const WORKER_STATE: Record<string, string> = {
  running: 'It is working', completed: 'It finished', failed: 'It failed', unavailable: 'It could not start', stopping: 'It is stopping', stopped: 'It was stopped',
};

/** `default` is the provider's own program left as the person has it set: no model or effort is forced on it. */
export const modelLabel = (m: Pick<ConsoleModel, 'id' | 'name' | 'label'>) => (m.id === 'default' ? t('Whatever it is set to') : m.name || m.label || m.id);
/** Each level a provider lists gets a name of its own: one of them offers `xhigh`, `max` and `ultra` side by side. */
const EFFORT_LABEL: Record<string, string> = {
  default: 'Whatever it is set to', none: 'Off#effort', minimal: 'Minimal#effort', low: 'Low#effort', medium: 'Medium#effort',
  high: 'High#effort', xhigh: 'Very high#effort', max: 'Maximum#effort', ultra: 'Ultra#effort',
};
export const effortLabel = (level: string) => (EFFORT_LABEL[level] ? t(EFFORT_LABEL[level]) : level);
/** A model and its effort on one line, as a session or a provider shows them. */
export function modelLine(catalog: ConsoleModel[] | undefined, model: string | null | undefined, effort: string | null | undefined): string {
  if (!model) return '';
  const row = catalog?.find((m) => m.id === model);
  const name = row ? modelLabel(row) : model === 'default' ? t('Whatever it is set to') : model.split('::').pop() || model;
  return effort && effort !== 'default' ? name + ' · ' + effortLabel(effort) : name;
}
/** The effort a model starts with: the one asked for when the model offers it, else its own default; none when it offers none.
 *  A model with a single level leaves nothing to choose, so no selector is shown for it and that level goes as it is. */
export function effortFor(model: ConsoleModel | undefined, wanted: string | null | undefined): string {
  const offered = model?.efforts ?? [];
  if (!offered.length) return '';
  return wanted && offered.includes(wanted) ? wanted : model?.default_effort && offered.includes(model.default_effort) ? model.default_effort : offered[0];
}
const LAST_PROVIDER = 'agora:last-launch-provider';
const SIGNED_OUT = /failed to authenticate|authentication_error|token has expired|re-authenticate/i;
/** Why the hub cannot start a provider, in the person's words where the hub says what it is. */
export function unavailableWhy(leader: Pick<ConsoleLeader, 'auth_state'>): string {
  if (leader.auth_state === 'expired') return t('Its program on the PC is signed out: open it there once and sign in again.');
  if (leader.auth_state === 'logged_out') return t('Its program on the PC has no account signed in.');
  return t('The hub cannot start it right now.');
}
/** Why a session ended badly: the provider's own words, unless they say its program is signed out. */
export function failureWhy(worker: Pick<ConsoleWorker, 'result' | 'detail'>): string {
  const said = [worker.result, worker.detail].filter(Boolean).join(' ');
  return SIGNED_OUT.test(said) ? t('Its program on the PC is signed out: open it there once and sign in again.') : (worker.result || worker.detail || '').slice(0, 300);
}

function slug(text: string): string {
  return text.toLowerCase().normalize('NFKD').replace(/[^a-z0-9_.]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 30);
}

/**
 * Put an agent on a server. As a session the hub starts on the PC with the
 * provider's own program, on the model and effort chosen here, which comes in
 * by itself with the brief written here. Or by name: it can be mentioned and
 * given work before it has said anything, and shows as invited until it does.
 */
/** The ranks to choose from, highest first, in the person's words. */
export function RankOptions() {
  return <>{RANKS.map((r) => <option key={r} value={r}>{t(RANK_LABEL[r])}</option>)}</>;
}

export function AddAgentDialog({ open, guild, onClose, onDone, notify, preset }: {
  open: boolean; guild: AgoraGuild; onClose: () => void; onDone: () => void; notify: Notify;
  /** The provider the dialog opens on (the + of its group in the list); the last one used when not given. */
  preset?: string;
}) {
  const space = guild.space.id;
  const [mode, setMode] = useState<'name' | 'session'>('session');
  const [provider, setProvider] = useState<(typeof PROVIDERS)[number]>('codex');
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [leaders, setLeaders] = useState<ConsoleLeader[] | null>(null);
  /** The providers the hub cannot start right now, to say which and why instead of leaving them out in silence. */
  const [unable, setUnable] = useState<ConsoleLeader[]>([]);
  const [workers, setWorkers] = useState<ConsoleWorker[]>([]);
  const [leader, setLeader] = useState('');
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [task, setTask] = useState('');
  const [consoleError, setConsoleError] = useState('');
  /** The name the session comes in under: the hub proposes a free one, and the person may write another. */
  const [sessionName, setSessionName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [nameAsked, setNameAsked] = useState(0);
  const [rank, setRank] = useState<string>(DEFAULT_RANK);
  /** Which showing of the dialog this is: what was still under way when it closed or changed server is left alone. */
  const showing = useRef(0);
  useEffect(() => { showing.current += 1; }, [open, space]);

  useEffect(() => {
    if (!open) return;
    setMode('session'); setProvider((PROVIDERS as readonly string[]).includes(preset ?? '') ? (preset as (typeof PROVIDERS)[number]) : 'codex');
    setName(''); setNote(''); setError(''); setTask(''); setLeaders(null); setUnable([]); setWorkers([]); setConsoleError(''); setBusy(false);
    setSessionName(''); setNameTouched(false); setRank(DEFAULT_RANK);
  }, [open, space, preset]);

  // What the hub can start is asked once the person looks at it: it probes the programs installed on the PC.
  useEffect(() => {
    if (!open || mode !== 'session' || leaders) return;
    const controller = new AbortController();
    getConsole(space, controller.signal).then((data) => {
      // A provider with nothing in its catalog cannot be started right now (its program is missing or signed out).
      const able = (data.leaders ?? []).filter((l) => (l.catalog ?? []).length > 0);
      setLeaders(able); setUnable((data.leaders ?? []).filter((l) => !(l.catalog ?? []).length)); setWorkers(data.workers ?? []);
      // The provider asked for when the hub can start it; when it cannot, that provider is added by name instead.
      if (preset && !able.some((l) => l.id === preset)) setMode('name');
      const first = able.find((l) => l.id === preset) ?? able.find((l) => l.id === recall(LAST_PROVIDER)) ?? able[0];
      if (first) { setLeader(first.id); const m = first.catalog.find((c) => c.id === first.model) ?? first.catalog[0]; setModel(m.id); setEffort(effortFor(m, first.effort)); }
    }).catch((e: Error) => { if (!controller.signal.aborted) { setLeaders([]); setConsoleError(e.message); } });
    return () => controller.abort();
  }, [open, mode, leaders, space, preset]);

  // A name is proposed for the provider and model chosen, until the person writes their own.
  useEffect(() => {
    if (!open || !leader || nameTouched) return;
    const controller = new AbortController();
    suggestWorkerName(space, leader, model, controller.signal).then((out) => setSessionName(out.name ?? ''))
      .catch(() => { /* an older hub proposes none: the session still gets a name of its own */ });
    return () => controller.abort();
  }, [open, space, leader, model, nameTouched, nameAsked]);
  const sessionId = sessionName.trim().toLowerCase();
  const nameProblem = !sessionId ? '' : !VALID_ID.test(sessionId) ? t('The name needs at least two letters or digits.')
    : /[-_.]$/.test(sessionId) ? t('The name must end with a letter or a digit.')
    : !sessionId.startsWith(leader + '-') ? t('The name must start with {prefix}', { prefix: leader + '-' })
      : guild.members.some((m) => m.id === sessionId) ? t('{name} is already on this server.', { name: sessionId }) : '';

  const id = useMemo(() => {
    const tail = slug(name);
    if (!tail) return '';
    return provider === 'other' || tail === provider || tail.startsWith(provider + '-') ? tail : provider + '-' + tail;
  }, [name, provider]);
  const already = guild.members.some((m) => m.id === id);
  const chosen = leaders?.find((l) => l.id === leader);
  const chosenModel = chosen?.catalog.find((c) => c.id === model);
  const efforts = chosenModel?.efforts ?? [];

  const pickLeader = (next: string) => {
    // A name the person wrote goes with them to the other provider: what they wrote stays, its beginning changes.
    if (nameTouched && leader && sessionId.startsWith(leader + '-')) setSessionName(next + sessionId.slice(leader.length));
    setLeader(next);
    const l = leaders?.find((x) => x.id === next);
    const m = l?.catalog.find((c) => c.id === l.model) ?? l?.catalog[0];
    setModel(m?.id ?? ''); setEffort(effortFor(m, l?.effort));
  };
  const pickModel = (next: string) => {
    setModel(next);
    const m = chosen?.catalog.find((c) => c.id === next);
    setEffort((old) => effortFor(m, old));
  };

  const register = async () => {
    if (!VALID_ID.test(id)) return;
    setBusy(true); setError('');
    try {
      const made = await addMember(space, id, note.trim());
      // The agent is on the server by now: a role that could not be saved is said, and the list still shows it.
      let roleError = '';
      if (rank !== DEFAULT_RANK) { try { await setMemberRank(space, id, rank); } catch (e) { roleError = (e as Error).message; } }
      notify(made.added ? t('{name} is on the server', { name: id }) : t('{name} was already on the server', { name: id }));
      if (roleError) notify(t('Its role could not be saved: {why}', { why: roleError }), 'warn');
      onDone(); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  // After a refusal the hub knows what a provider cannot do: the lists are asked again, and what the person had
  // chosen stays chosen while it can still be started.
  const reloadProviders = async (mine: number) => {
    try {
      const data = await getConsole(space);
      if (mine !== showing.current) return;
      const able = (data.leaders ?? []).filter((l) => (l.catalog ?? []).length > 0);
      setLeaders(able); setUnable((data.leaders ?? []).filter((l) => !(l.catalog ?? []).length)); setWorkers(data.workers ?? []);
      if (!able.some((l) => l.id === leader)) {
        const first = able[0];
        const m = first?.catalog.find((c) => c.id === first.model) ?? first?.catalog[0];
        setLeader(first?.id ?? ''); setModel(m?.id ?? ''); setEffort(effortFor(m, first?.effort));
      }
    } catch { /* the lists stay as they were */ }
  };
  const launch = async () => {
    if (!leader || !model || !task.trim() || nameProblem) return;
    const mine = showing.current;
    setBusy(true); setError('');
    try {
      const made = await launchWorker(space, {
        leader_id: leader, model, effort: efforts.length ? effort : '', task: task.trim(),
        ...(sessionId ? { name: sessionId } : {}), ...(rank !== DEFAULT_RANK ? { rank } : {}),
      });
      if (mine !== showing.current) return;
      let worker = made.worker;
      // A provider that refuses (its program signed out, a model it no longer has) does so in its first seconds:
      // that is not a session started, and it is said here instead of leaving a member that never comes in.
      // The wait is by the clock, since asking the hub takes its own time. One whose session is known to have run out
      // is given longer: its refusal is the likely answer, and it can take a while.
      const until = Date.now() + (chosen?.live_state?.state === 'auth_expired' ? 20000 : 6000);
      while (Date.now() < until && worker.state === 'running' && !made.warning) {
        await new Promise((resolve) => { window.setTimeout(resolve, 1500); });
        try { worker = (await getConsole(space)).workers?.find((w) => w.id === made.worker.id) ?? worker; } catch { break; }
        if (mine !== showing.current) return;
      }
      const started = worker;
      setWorkers((old) => [started, ...old.filter((w) => w.id !== started.id)]);
      if (made.warning || started.state === 'unavailable' || started.state === 'failed') {
        const why = failureWhy(started) || made.warning || '';
        setError(t('The session could not start.') + (why ? ' ' + why : ''));
        if (started.state === 'failed') await reloadProviders(mine);
      } else {
        remember(LAST_PROVIDER, leader);
        // Shown at once, as invited, until its first sign of life.
        try { await addMember(space, started.agent_id, task.trim().slice(0, 200)); } catch { /* it joins by itself anyway */ }
        if (mine !== showing.current) return;
        notify(t('Session started as {name}', { name: started.agent_id }));
        onDone(); onClose();
      }
    } catch (e) { if (mine === showing.current) setError((e as Error).message); }
    finally { if (mine === showing.current) setBusy(false); }
  };
  const stop = (worker: ConsoleWorker) => {
    void stopWorker(space, worker.id).then(() => { setWorkers((old) => old.map((w) => (w.id === worker.id ? { ...w, state: 'stopping' } : w))); notify(t('Stopping {name}', { name: worker.agent_id })); })
      .catch((e: Error) => notify(e.message, 'warn'));
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('Add an agent')} testId="agora-add-agent-dialog" className="fs-agx-dialog"
      description={t('To {server}. An agent also joins by itself the first time it says something here.', { server: guild.space.title })}
      footer={<>
        <Button label={t('Cancel')} onClick={onClose} />
        {mode === 'name'
          ? <Button variant="primary" label={t('Add to the server')} loading={busy} disabled={!VALID_ID.test(id) || already} onClick={() => void register()} testId="agora-add-agent-confirm" />
          : <Button variant="primary" label={t('Start the session')} loading={busy} disabled={!leader || !model || !task.trim() || Boolean(nameProblem)} onClick={() => void launch()} testId="agora-launch-agent-confirm" />}
      </>}>
      <div className="fs-agx-form">
        <div className="fs-seg" role="radiogroup" aria-label={t('How it joins')}>
          <button type="button" role="radio" aria-checked={mode === 'session'} onClick={() => { setMode('session'); setError(''); }} data-testid="agora-add-agent-session">{t('Start a session')}</button>
          <button type="button" role="radio" aria-checked={mode === 'name'} onClick={() => { setMode('name'); setError(''); }} data-testid="agora-add-agent-by-name">{t('By name')}</button>
        </div>
        {mode === 'name' ? (
          <>
            <p className="fs-muted">{t('It can be mentioned and given tasks from now on, and shows as invited until its first sign of life.')}</p>
            <div className="fs-agx-form__row">
              <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Provider')}</span>
                <select className="fs-field" value={provider} onChange={(e) => setProvider(e.target.value as (typeof PROVIDERS)[number])} data-testid="agora-add-agent-provider">
                  {PROVIDERS.map((p) => <option key={p} value={p}>{t(ROLE_LABEL[p] ?? p)}</option>)}
                </select></label>
              <label className="fs-agx-form__field fs-agx-form__field--grow"><span className="fs-agx-form__label">{t('Name')}</span>
                <input className="fs-field" value={name} maxLength={40} onChange={(e) => setName(e.target.value)} placeholder={t('docs, tests, review…')} data-testid="agora-add-agent-name" /></label>
            </div>
            {id && <p className="fs-muted" data-testid="agora-add-agent-id">{already ? t('{name} is already on this server.', { name: id }) : VALID_ID.test(id) ? t('It will be {name}.', { name: '@' + id }) : t('The name needs at least two letters or digits.')}</p>}
            <div className="fs-agx-form__row">
              <label className="fs-agx-form__field fs-agx-form__field--grow"><span className="fs-agx-form__label">{t('What it is for (optional)')}</span>
                <input className="fs-field" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} /></label>
              <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Role')}</span>
                <select className="fs-field" value={rank} onChange={(e) => setRank(e.target.value)} data-testid="agora-add-agent-rank"><RankOptions /></select></label>
            </div>
          </>
        ) : (
          <>
            <p className="fs-muted">{t('The hub starts it on the PC with the provider\'s own program, on the model and effort you choose. It comes in under a name of its own, reads the board and works on what you write here.')}</p>
            {leaders === null && <Skeleton label={t('Looking at what can be started')} count={3} height="36px" />}
            {leaders !== null && !leaders.length && <p className="fs-notice" data-tone="warning" role="status">{consoleError || t('The hub finds nothing it can start right now.')}</p>}
            {leaders !== null && leaders.length > 0 && (
              <>
                <div className="fs-agx-form__row">
                  <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Provider')}</span>
                    <select className="fs-field" value={leader} onChange={(e) => pickLeader(e.target.value)} data-testid="agora-launch-provider">
                      {leaders.map((l) => <option key={l.id} value={l.id}>{t(ROLE_LABEL[l.id] ?? (l.name || l.id))}</option>)}
                    </select></label>
                  <label className="fs-agx-form__field fs-agx-form__field--grow"><span className="fs-agx-form__label">{t('Model')}</span>
                    <select className="fs-field" value={model} onChange={(e) => pickModel(e.target.value)} data-testid="agora-launch-model">
                      {(chosen?.catalog ?? []).map((m) => <option key={m.id} value={m.id}>{modelLabel(m)}</option>)}
                    </select></label>
                  {efforts.length > 1 && (
                    <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Effort')}</span>
                      <select className="fs-field" value={effort} onChange={(e) => setEffort(e.target.value)} data-testid="agora-launch-effort">
                        {efforts.map((level) => <option key={level} value={level}>{effortLabel(level)}</option>)}
                      </select></label>
                  )}
                </div>
                <div className="fs-agx-form__row">
                  <label className="fs-agx-form__field fs-agx-form__field--grow"><span className="fs-agx-form__label">{t('Name')}</span>
                    <input className="fs-field" value={sessionName} maxLength={41} spellCheck={false}
                      onChange={(e) => { setSessionName(e.target.value); setNameTouched(true); }} data-testid="agora-launch-name" /></label>
                  <IconButton size="sm" icon={RefreshCw} label={t('Propose a name')} onClick={() => { setNameTouched(false); setNameAsked((n) => n + 1); }} testId="agora-launch-name-again" />
                  <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Role')}</span>
                    <select className="fs-field" value={rank} onChange={(e) => setRank(e.target.value)} data-testid="agora-launch-rank"><RankOptions /></select></label>
                </div>
                {nameProblem && <p className="fs-muted" data-testid="agora-launch-name-problem">{nameProblem}</p>}
                {chosen?.live_state?.state === 'auth_expired' && (
                  <p className="fs-notice" data-tone="warning" role="status" data-testid="agora-launch-signed-out">{t('The hub sees its program signed out on the PC: it will not start until you sign in there again.')}</p>
                )}
                <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('What it should do here')}</span>
                  <textarea className="fs-field fs-agx-form__area" value={task} maxLength={12000} onChange={(e) => setTask(e.target.value)}
                    placeholder={t('Take the task about…, or review what is waiting, or…')} data-testid="agora-launch-task" /></label>
              </>
            )}
            {unable.map((l) => (
              <p key={l.id} className="fs-muted" title={l.detail || undefined} data-testid={'agora-launch-unable-' + l.id}>
                <strong>{t(ROLE_LABEL[l.id] ?? (l.name || l.id))}</strong>{': '}{unavailableWhy(l)}
              </p>
            ))}
            {workers.length > 0 && (
              <section className="fs-agx-workers" aria-label={t('Sessions started here')}>
                <h4>{t('Sessions started here')}</h4>
                <ul>
                  {workers.slice(0, 6).map((w) => (
                    <li key={w.id} data-state={w.state}>
                      <span className="fs-agx-status" data-status={w.state === 'running' ? 'in_progress' : w.state === 'completed' ? 'done' : w.state === 'failed' || w.state === 'unavailable' ? 'blocked' : 'open'}>{t(WORKER_STATE[w.state] ?? w.state)}</span>
                      <span className="fs-agx-workers__text"><strong>{w.agent_id}</strong><span>{[modelLine(leaders?.find((l) => l.id === w.parent_id)?.catalog, w.model, w.effort), w.task.slice(0, 160)].filter(Boolean).join(' · ')}</span></span>
                      {w.state === 'running' && <IconButton size="sm" icon={Square} label={t('Stop {name}', { name: w.agent_id })} onClick={() => stop(w)} />}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}

/** Take an agent off a server: what it held is freed or handed to another. */
export function RemoveAgentDialog({ member, guild, onClose, onDone, notify }: {
  member: AgoraMember | null; guild: AgoraGuild; onClose: () => void; onDone: () => void; notify: Notify;
}) {
  const space = guild.space.id;
  const [to, setTo] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { setTo(''); setReason(''); setError(''); }, [member?.id]);
  const heirs = guild.members.filter((m) => m.role !== 'person' && isOnline(m) && m.presence !== 'invited' && m.id !== member?.id).slice(0, 30);
  const remove = async () => {
    if (!member) return;
    setBusy(true); setError('');
    try {
      const out = await removeMember(space, member.id, { to, reason: reason.trim() });
      const parts = [t('{name} is off the server', { name: out.removed })];
      if (out.handed_tasks.length) parts.push(tn(out.handed_tasks.length, '{n} task goes to {name}', '{n} tasks go to {name}', { name: out.handed_to ?? '' }));
      if (out.freed_tasks.length) parts.push(tn(out.freed_tasks.length, '{n} task is free again', '{n} tasks are free again'));
      notify(parts.join(' · ')); onDone(); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Dialog open={Boolean(member)} onOpenChange={(next) => { if (!next) onClose(); }} title={member ? t('Take {name} off the server', { name: member.id }) : ''} testId="agora-remove-agent-dialog" className="fs-agx-dialog"
      description={t('It leaves {server}. Its leases are dropped; if it shows up here again, it simply joins again.', { server: guild.space.title })}
      footer={<><Button label={t('Cancel')} onClick={onClose} /><Button variant="danger" label={t('Take off the server')} loading={busy} onClick={() => void remove()} testId="agora-remove-agent-confirm" /></>}>
      <div className="fs-agx-form">
        {member && member.tasks.length > 0 && (
          <>
            <p>{tn(member.tasks.length, 'It holds {n} task here:', 'It holds {n} tasks here:')} {member.tasks.map((task) => '#' + task.id).join(', ')}</p>
            <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Its work')}</span>
              <select className="fs-field" value={to} onChange={(e) => setTo(e.target.value)} data-testid="agora-remove-agent-heir">
                <option value="">{t('Goes back to open, for anyone to take')}</option>
                {heirs.map((m) => <option key={m.id} value={m.id}>{t('Goes to {name}', { name: m.id })}</option>)}
              </select></label>
          </>
        )}
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Why (optional)')}</span>
          <input className="fs-field" value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)} /></label>
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}

/** Take off the server everyone who stopped coming and holds nothing: first who would go, then the word. */
export function PruneAgentsDialog({ open, guild, onClose, onDone, notify }: {
  open: boolean; guild: AgoraGuild; onClose: () => void; onDone: () => void; notify: Notify;
}) {
  const space = guild.space.id;
  const [days, setDays] = useState(30);
  const [plan, setPlan] = useState<{ removed: string[]; kept: { id: string; why: string }[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { if (open) { setDays(30); setPlan(null); setError(''); } }, [open, space]);
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setPlan(null);
    const timer = window.setTimeout(() => {
      pruneMembers(space, days, true).then((out) => { if (alive) { setPlan(out); setError(''); } }).catch((e: Error) => { if (alive) setError(e.message); });
    }, 250);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [open, space, days]);
  const WHY: Record<string, string> = { tasks: 'holds tasks', reviews: 'is reviewing', leases: 'holds leases' };
  const run = async () => {
    setBusy(true); setError('');
    try {
      const out = await pruneMembers(space, days, false);
      notify(tn(out.removed.length, '{n} agent taken off the server', '{n} agents taken off the server')); onDone(); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('Take off those who no longer come')} testId="agora-prune-dialog" className="fs-agx-dialog"
      description={t('Agents that have given no sign of life for a while and hold neither tasks nor leases here. Whoever shows up again joins again.')}
      footer={<><Button label={t('Cancel')} onClick={onClose} />
        <Button variant="danger" label={plan?.removed.length ? tn(plan.removed.length, 'Take {n} off the server', 'Take {n} off the server') : t('Take off the server')} loading={busy}
          disabled={!plan || !plan.removed.length} onClick={() => void run()} testId="agora-prune-confirm" /></>}>
      <div className="fs-agx-form">
        <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Silent for more than (days)')}</span>
          <input className="fs-field fs-agx-form__glyph" type="number" min={1} max={3650} value={days} onChange={(e) => setDays(Math.max(1, Math.min(3650, Number(e.target.value) || 1)))} data-testid="agora-prune-days" /></label>
        {!plan && !error && <Skeleton label={t('Looking at who would go')} count={2} height="28px" />}
        {plan && (
          <div data-testid="agora-prune-plan">
            <p><strong>{plan.removed.length ? tn(plan.removed.length, '{n} agent would go', '{n} agents would go') : t('Nobody would go.')}</strong></p>
            {plan.removed.length > 0 && <p className="fs-muted fs-agx-prune__names">{plan.removed.slice(0, 40).join(', ')}{plan.removed.length > 40 ? ' …' : ''}</p>}
            {plan.kept.length > 0 && <p className="fs-muted">{t('They stay')}: {plan.kept.slice(0, 20).map((k) => k.id + ' (' + t(WHY[k.why] ?? k.why) + ')').join(', ')}</p>}
          </div>
        )}
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}


/**
 * The model and effort each provider's agents use on a server: what a session
 * started from here begins with, and what the provider answers with from the
 * hub's console. A chat already open in the provider's own app keeps the
 * model chosen there; nothing here reaches into it.
 */
export function AgentModelsDialog({ open, guild, onClose, onSaved, notify }: {
  open: boolean; guild: AgoraGuild; onClose: () => void; onSaved: () => void; notify: Notify;
}) {
  const space = guild.space.id;
  const [leaders, setLeaders] = useState<ConsoleLeader[] | null>(null);
  const [choice, setChoice] = useState<Record<string, { model: string; effort: string }>>({});
  /** What each provider had when the dialog opened: only what the person moves is saved. */
  const [initial, setInitial] = useState<Record<string, { model: string; effort: string }>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    setLeaders(null); setChoice({}); setInitial({}); setError('');
    const controller = new AbortController();
    getConsole(space, controller.signal).then((data) => {
      const rows = data.leaders ?? [];
      setLeaders(rows);
      const found = Object.fromEntries(rows.filter((l) => (l.catalog ?? []).length > 0).map((l) => {
        const m = l.catalog.find((c) => c.id === l.model) ?? l.catalog[0];
        return [l.id, { model: m.id, effort: effortFor(m, l.effort) }];
      }));
      setChoice(found); setInitial(found);
    }).catch((e: Error) => { if (!controller.signal.aborted) { setLeaders([]); setError(e.message); } });
    return () => controller.abort();
  }, [open, space]);

  const changed = (leaders ?? []).filter((l) => {
    const now = choice[l.id]; const was = initial[l.id];
    return now && was && (now.model !== was.model || now.effort !== was.effort);
  });
  const pick = (leader: ConsoleLeader, model: string, effort?: string) => {
    const m = leader.catalog.find((c) => c.id === model);
    setChoice((old) => ({ ...old, [leader.id]: { model, effort: effortFor(m, effort ?? old[leader.id]?.effort) } }));
  };
  const save = async () => {
    setBusy(true); setError('');
    try {
      let waits = false;
      for (const leader of changed) {
        const saved = choice[leader.id];
        const out = await setConsolePreferences(space, { leader_id: leader.id, ...saved });
        setInitial((old) => ({ ...old, [leader.id]: saved }));       // saved: if a later one fails, this one is not sent again
        if (out.applies_to === 'queued_until_native_session_idle') waits = true;
      }
      notify(waits ? t('Saved. Faustus takes it as soon as its session is free.') : t('Saved'));
      onSaved(); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={t('Model and effort of the agents')} testId="agora-models-dialog" className="fs-agx-dialog"
      description={t('On {server}. The sessions you start here begin with it, and each provider answers with it from the hub. A chat you already have open in its own app keeps the model chosen there.', { server: guild.space.title })}
      footer={<><Button label={t('Cancel')} onClick={onClose} />
        <Button variant="primary" label={t('Save')} loading={busy} disabled={!changed.length} onClick={() => void save()} testId="agora-models-save" /></>}>
      <div className="fs-agx-form">
        {leaders === null && <Skeleton label={t('Looking at what each provider offers')} count={4} height="36px" />}
        {(leaders ?? []).map((leader) => {
          const now = choice[leader.id];
          const efforts = leader.catalog?.find((c) => c.id === now?.model)?.efforts ?? [];
          return (
            <div key={leader.id} className="fs-agx-form__row fs-agx-tune" data-testid={'agora-models-' + leader.id}>
              <strong className="fs-agx-tune__name">{t(ROLE_LABEL[leader.id] ?? (leader.name || leader.id))}</strong>
              {now ? (
                <>
                  <label className="fs-agx-form__field fs-agx-form__field--grow"><span className="fs-agx-form__label">{t('Model')}</span>
                    <select className="fs-field" value={now.model} onChange={(e) => pick(leader, e.target.value)} data-testid={'agora-models-model-' + leader.id}>
                      {leader.catalog.map((m) => <option key={m.id} value={m.id}>{modelLabel(m)}</option>)}
                    </select></label>
                  {efforts.length > 1 && (
                    <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Effort')}</span>
                      <select className="fs-field" value={now.effort} onChange={(e) => pick(leader, now.model, e.target.value)} data-testid={'agora-models-effort-' + leader.id}>
                        {efforts.map((level) => <option key={level} value={level}>{effortLabel(level)}</option>)}
                      </select></label>
                  )}
                </>
              ) : <span className="fs-muted fs-agx-tune__off" title={leader.detail || undefined}>{unavailableWhy(leader)}</span>}
            </div>
          );
        })}
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}
