import { Square } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import {
  addMember, getConsole, launchWorker, pruneMembers, removeMember, stopWorker,
  type AgoraGuild, type AgoraMember, type ConsoleLeader, type ConsoleWorker,
} from '../../adapters/agora';
import { Button, Dialog, IconButton, Skeleton } from '../../components';
import { t, tn } from '../../i18n';
import { isOnline, ROLE_LABEL } from './util';

type Notify = (text: string, tone?: 'ok' | 'warn') => void;

const PROVIDERS = ['claude', 'codex', 'cursor', 'faustus', 'other'] as const;
const VALID_ID = /^[a-z][a-z0-9_.-]{1,40}$/;
const WORKER_STATE: Record<string, string> = {
  running: 'It is working', completed: 'It finished', failed: 'It failed', unavailable: 'It could not start', stopping: 'It is stopping', stopped: 'It was stopped',
};

function slug(text: string): string {
  return text.toLowerCase().normalize('NFKD').replace(/[^a-z0-9_.]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 30);
}

/**
 * Put an agent on a server. By name: it can be mentioned and given work
 * before it has said anything, and shows as invited until it does. Or as a
 * session the hub starts on the PC with the provider's own program, which
 * comes in by itself with the brief written here.
 */
export function AddAgentDialog({ open, guild, onClose, onDone, notify }: {
  open: boolean; guild: AgoraGuild; onClose: () => void; onDone: () => void; notify: Notify;
}) {
  const space = guild.space.id;
  const [mode, setMode] = useState<'name' | 'session'>('name');
  const [provider, setProvider] = useState<(typeof PROVIDERS)[number]>('codex');
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [leaders, setLeaders] = useState<ConsoleLeader[] | null>(null);
  const [workers, setWorkers] = useState<ConsoleWorker[]>([]);
  const [leader, setLeader] = useState('');
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [task, setTask] = useState('');
  const [consoleError, setConsoleError] = useState('');

  useEffect(() => {
    if (!open) return;
    setMode('name'); setProvider('codex'); setName(''); setNote(''); setError(''); setTask(''); setLeaders(null); setWorkers([]); setConsoleError('');
  }, [open, space]);

  // What the hub can start is asked once the person looks at it: it probes the programs installed on the PC.
  useEffect(() => {
    if (!open || mode !== 'session' || leaders) return;
    const controller = new AbortController();
    getConsole(space, controller.signal).then((data) => {
      // The hub starts a session only with a model and one of its effort levels: a model that offers none cannot start.
      const able = (data.leaders ?? []).map((l) => ({ ...l, catalog: (l.catalog ?? []).filter((m) => (m.efforts?.length ?? 0) > 0) }))
        .filter((l) => l.catalog.length > 0);
      setLeaders(able); setWorkers(data.workers ?? []);
      const first = able[0];
      if (first) { setLeader(first.id); const m = first.catalog.find((c) => c.id === first.model) ?? first.catalog[0]; setModel(m.id); setEffort(first.effort || m.default_effort || m.efforts?.[0] || ''); }
    }).catch((e: Error) => { if (!controller.signal.aborted) { setLeaders([]); setConsoleError(e.message); } });
    return () => controller.abort();
  }, [open, mode, leaders, space]);

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
    setLeader(next);
    const l = leaders?.find((x) => x.id === next);
    const m = l?.catalog.find((c) => c.id === l.model) ?? l?.catalog[0];
    setModel(m?.id ?? ''); setEffort(l?.effort || m?.default_effort || m?.efforts?.[0] || '');
  };
  const pickModel = (next: string) => {
    setModel(next);
    const m = chosen?.catalog.find((c) => c.id === next);
    setEffort((old) => (m?.efforts?.includes(old) ? old : m?.default_effort || m?.efforts?.[0] || ''));
  };

  const register = async () => {
    if (!VALID_ID.test(id)) return;
    setBusy(true); setError('');
    try {
      const made = await addMember(space, id, note.trim());
      notify(made.added ? t('{name} is on the server', { name: id }) : t('{name} was already on the server', { name: id }));
      onDone(); onClose();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const launch = async () => {
    if (!leader || !model || !task.trim()) return;
    setBusy(true); setError('');
    try {
      const made = await launchWorker(space, { leader_id: leader, model, effort, task: task.trim() });
      setWorkers((old) => [made.worker, ...old.filter((w) => w.id !== made.worker.id)]);
      if (made.warning || made.worker.state === 'unavailable' || made.worker.state === 'failed') {
        const why = made.worker.detail || made.warning || '';
        setError(t('The session could not start.') + (why ? ' ' + why : ''));
      } else {
        // Shown at once, as invited, until its first sign of life.
        try { await addMember(space, made.worker.agent_id, task.trim().slice(0, 200)); } catch { /* it joins by itself anyway */ }
        notify(t('Session started as {name}', { name: made.worker.agent_id }));
        onDone(); onClose();
      }
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
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
          : <Button variant="primary" label={t('Start the session')} loading={busy} disabled={!leader || !model || !task.trim()} onClick={() => void launch()} testId="agora-launch-agent-confirm" />}
      </>}>
      <div className="fs-agx-form">
        <div className="fs-seg" role="radiogroup" aria-label={t('How it joins')}>
          <button type="button" role="radio" aria-checked={mode === 'name'} onClick={() => { setMode('name'); setError(''); }} data-testid="agora-add-agent-by-name">{t('By name')}</button>
          <button type="button" role="radio" aria-checked={mode === 'session'} onClick={() => { setMode('session'); setError(''); }} data-testid="agora-add-agent-session">{t('Start a session')}</button>
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
                <input className="fs-field" autoFocus value={name} maxLength={40} onChange={(e) => setName(e.target.value)} placeholder={t('docs, tests, review…')} data-testid="agora-add-agent-name" /></label>
            </div>
            {id && <p className="fs-muted" data-testid="agora-add-agent-id">{already ? t('{name} is already on this server.', { name: id }) : VALID_ID.test(id) ? t('It will be {name}.', { name: '@' + id }) : t('The name needs at least two letters or digits.')}</p>}
            <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('What it is for (optional)')}</span>
              <input className="fs-field" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} /></label>
          </>
        ) : (
          <>
            <p className="fs-muted">{t('The hub starts it on the PC with the provider\'s own program. It comes in under a name of its own, reads the board and works on what you write here.')}</p>
            {leaders === null && <Skeleton label={t('Looking at what can be started')} count={3} height="36px" />}
            {leaders !== null && !leaders.length && <p className="fs-notice" data-tone="warning" role="status">{consoleError || t('The hub finds nothing it can start right now.')}</p>}
            {leaders !== null && leaders.length > 0 && (
              <>
                <div className="fs-agx-form__row">
                  <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Provider')}</span>
                    <select className="fs-field" value={leader} onChange={(e) => pickLeader(e.target.value)} data-testid="agora-launch-provider">
                      {leaders.map((l) => <option key={l.id} value={l.id}>{l.name || l.id}</option>)}
                    </select></label>
                  <label className="fs-agx-form__field fs-agx-form__field--grow"><span className="fs-agx-form__label">{t('Model')}</span>
                    <select className="fs-field" value={model} onChange={(e) => pickModel(e.target.value)} data-testid="agora-launch-model">
                      {(chosen?.catalog ?? []).map((m) => <option key={m.id} value={m.id}>{m.name || m.label || m.id}</option>)}
                    </select></label>
                  {efforts.length > 0 && (
                    <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('Effort')}</span>
                      <select className="fs-field" value={effort} onChange={(e) => setEffort(e.target.value)}>
                        {efforts.map((level) => <option key={level} value={level}>{level}</option>)}
                      </select></label>
                  )}
                </div>
                <label className="fs-agx-form__field"><span className="fs-agx-form__label">{t('What it should do here')}</span>
                  <textarea className="fs-field fs-agx-form__area" value={task} maxLength={12000} onChange={(e) => setTask(e.target.value)}
                    placeholder={t('Take the task about…, or review what is waiting, or…')} data-testid="agora-launch-task" /></label>
              </>
            )}
            {workers.length > 0 && (
              <section className="fs-agx-workers" aria-label={t('Sessions started here')}>
                <h4>{t('Sessions started here')}</h4>
                <ul>
                  {workers.slice(0, 6).map((w) => (
                    <li key={w.id} data-state={w.state}>
                      <span className="fs-agx-status" data-status={w.state === 'running' ? 'in_progress' : w.state === 'completed' ? 'done' : w.state === 'failed' || w.state === 'unavailable' ? 'blocked' : 'open'}>{t(WORKER_STATE[w.state] ?? w.state)}</span>
                      <span className="fs-agx-workers__text"><strong>{w.agent_id}</strong><span>{w.task.slice(0, 160)}</span></span>
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
