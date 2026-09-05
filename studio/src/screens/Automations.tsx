import { AlertTriangle, Check, CheckSquare, CircleStop, Copy, History as HistoryIcon, Link2, Pause, Pencil, Play, Plus, RefreshCw, Search, Sparkles, Trash2, Workflow, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, Skeleton, StatusBadge, Toast } from '../components';
import {
  CACHE_LABELS,
  categoryOf,
  clearAutomationCache,
  deleteAutomation,
  describeAction,
  describeOutput,
  describeTrigger,
  draftFromText,
  ensureOnboarded,
  listAutomations,
  listRuns,
  pauseAutomation,
  PRESETS,
  regenerateWebhook,
  resumeAutomation,
  revertAutomation,
  runAutomation,
  stopAutomation,
  webhookUrl,
  type Automation,
  type TaskRun,
  type TaskType,
  type TriggerType,
} from '../adapters/automations';
import { relativeTime } from '../adapters/home';
import { AutomationForm } from './automations/Form';
import './projects.css';
import './automations.css';
import { locale, t, tn } from '../i18n';

/**
 * Automations (the previous interface's Tasks, `/automations`).
 *
 * A list of readable recipes — trigger, what it does, where it delivers —
 * and a pane with one of them in full: its state, the next and last run,
 * its history, and the editor. Creating one starts from a sentence the
 * assistant drafts, or from one of seven presets; the form shows every
 * field at once, with native date and time inputs.
 */

type Mode = { kind: 'view' } | { kind: 'new' } | { kind: 'form'; existing: Automation | null; seed?: Partial<Automation> | null; taskType?: TaskType; trigger?: TriggerType };

const CATEGORY_LABEL: Record<string, string> = { Chats: 'Chats', Documents: 'Documents', Memory: 'Memory', Research: 'Research', Calendar: 'Calendar', Email: 'Mail', Assistant: 'Assistant', Other: 'Other' };

function runState(status: string | null | undefined): 'succeeded' | 'failed' | 'running' | 'cancelled' | 'queued' {
  const v = String(status ?? '').toLowerCase();
  if (['success', 'succeeded', 'ok', 'done', 'completed'].includes(v)) return 'succeeded';
  if (['failed', 'error', 'failure'].includes(v)) return 'failed';
  if (['running', 'in_progress', 'started'].includes(v)) return 'running';
  if (['cancelled', 'canceled', 'stopped', 'aborted'].includes(v)) return 'cancelled';
  return 'queued';
}

/* ── One row ── */

function Row({ task, on, selecting, selected, onOpen, onToggle }: { task: Automation; on: boolean; selecting: boolean; selected: boolean; onOpen: () => void; onToggle: () => void }) {
  const active = task.status === 'active';
  return (
    <div className="fs-au__row-wrap" data-on={on || undefined}>
      {selecting && <input type="checkbox" className="fs-au__check" checked={selected} onChange={onToggle} aria-label={t('Select {name}', { name: task.name })} />}
      <button type="button" className="fs-au__item" onClick={onOpen} aria-current={on || undefined} data-state={active ? 'active' : 'paused'} data-testid="automation-row">
        <span className="fs-au__item-main">
          <span className="fs-au__name">
            {task.name}
            {task.is_builtin && <span className="fs-au__builtin" title={task.is_modified ? t('Built-in, edited') : t('Built-in')}>{task.is_modified ? t('built-in · edited') : t('built-in')}</span>}
          </span>
          <span className="fs-au__recipe">
            {describeTrigger(task)} → {describeAction(task)}
          </span>
          <span className="fs-au__meta">
            {[task.last_run ? t('last {when}', { when: relativeTime(task.last_run) }) : t('has never run'), task.run_count ? tn(task.run_count, '{n} run', '{n} runs') : null].filter(Boolean).join(' · ')}
          </span>
        </span>
        <span className="fs-au__side">
          {active && task.next_run && <span className="fs-au__next">{t('next {when}', { when: relativeTime(task.next_run) })}</span>}
          <StatusBadge status={active ? 'succeeded' : 'paused'} label={active ? t('Active') : t('Paused')} />
        </span>
      </button>
    </div>
  );
}

/* ── Run history, inside the pane ── */

function RunItem({ run }: { run: TaskRun }) {
  const [open, setOpen] = useState(false);
  const text = run.result || run.error || '';
  const long = text.length > 300;
  return (
    <li className="fs-au__run" data-state={runState(run.status)}>
      <div className="fs-au__run-head">
        <StatusBadge status={runState(run.status)} label={run.status} />
        {run.model && <span className="fs-au__run-model">{run.model.split('/').pop()}</span>}
        <span className="fs-au__run-when" title={run.started_at ?? ''}>
          {run.started_at ? new Date(run.started_at).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' }) : ''}
        </span>
      </div>
      {text && (
        <p className="fs-au__run-text" data-error={run.error ? '' : undefined}>
          {open || !long ? text : `${text.slice(0, 300)}…`}
          {long && (
            <button type="button" className="fs-au__more-btn" onClick={() => setOpen((v) => !v)}>
              {open ? t('Less') : t('More')}
            </button>
          )}
        </p>
      )}
    </li>
  );
}

function History({ id, refreshKey }: { id: string; refreshKey: number }) {
  const [runs, setRuns] = useState<TaskRun[] | null>(null);
  useEffect(() => {
    let live = true;
    setRuns(null);
    listRuns(id)
      .then((r) => live && setRuns(r))
      .catch(() => live && setRuns([]));
    return () => {
      live = false;
    };
  }, [id, refreshKey]);
  if (runs === null) return <Skeleton label={t('Loading the runs')} count={2} height="44px" />;
  if (!runs.length) return <p className="fs-au__hint">{t('No runs yet.')}</p>;
  return (
    <ul className="fs-au__runs">
      {runs.map((r) => (
        <RunItem key={r.id} run={r} />
      ))}
    </ul>
  );
}

/* ── The screen ── */

export function AutomationsScreen() {
  const [params, setParams] = useSearchParams();
  const [tasks, setTasks] = useState<Automation[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('all');
  const [state, setState] = useState<'all' | 'active' | 'paused'>('all');
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<Mode>({ kind: 'view' });
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: 'delete'; ids: string[] } | { kind: 'all'; verb: 'pause' | 'resume'; ids: string[] } | { kind: 'cache'; id: string; label: string } | { kind: 'revert'; id: string } | { kind: 'parallel'; id: string } | null>(null);
  const [historyKey, setHistoryKey] = useState(0);
  const [sentence, setSentence] = useState('');
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const reload = useCallback(async () => {
    try {
      setTasks(await listAutomations());
      setFailed(null);
    } catch (e) {
      setFailed((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void ensureOnboarded().then(reload);
  }, [reload]);

  // The list is what tells you a run finished: look again now and then.
  useEffect(() => {
    const id = window.setInterval(() => void reload(), 15000);
    return () => window.clearInterval(id);
  }, [reload]);

  const currentId = params.get('task');
  const current = useMemo(() => (currentId && tasks ? tasks.find((x) => x.id === currentId) ?? null : null), [currentId, tasks]);

  const open = (id: string | null) => {
    setMode({ kind: 'view' });
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (id) next.set('task', id);
        else next.delete('task');
        return next;
      },
      { replace: true },
    );
  };

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const x of tasks ?? []) counts.set(categoryOf(x), (counts.get(categoryOf(x)) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [tasks]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (tasks ?? []).filter((x) => {
      if (category !== 'all' && categoryOf(x) !== category) return false;
      if (state !== 'all' && (x.status === 'active' ? 'active' : 'paused') !== state) return false;
      if (q && !`${x.name} ${x.prompt ?? ''} ${x.action ?? ''} ${x.trigger_event ?? ''}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [tasks, query, category, state]);

  const act = async (key: string, fn: () => Promise<void>, done?: string) => {
    setBusy(key);
    try {
      await fn();
      await reload();
      setHistoryKey((k) => k + 1);
      if (done) say(done);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const runNow = (id: string, force = false) =>
    act(
      `run:${id}`,
      async () => {
        try {
          await runAutomation(id, force);
          say(force ? t('Started a second run beside the first') : t('Started'));
        } catch (e) {
          if ((e as { status?: number }).status === 409 && !force) {
            setConfirm({ kind: 'parallel', id });
            return;
          }
          throw e;
        }
      },
    );

  const remove = async (ids: string[]) => {
    setBusy('delete');
    let n = 0;
    for (const id of ids) {
      try {
        await deleteAutomation(id);
        n++;
      } catch {
        /* the count says */
      }
    }
    setConfirm(null);
    setSelecting(false);
    setSelected(new Set());
    if (currentId && ids.includes(currentId)) open(null);
    await reload();
    setBusy(null);
    say(tn(n, 'Deleted {n} automation', 'Deleted {n} automations'));
  };

  const toggleAll = () => {
    if (!tasks) return;
    const hasActive = tasks.some((x) => x.status === 'active');
    const ids = tasks.filter((x) => x.status === (hasActive ? 'active' : 'paused')).map((x) => x.id);
    if (!ids.length) return;
    setConfirm({ kind: 'all', verb: hasActive ? 'pause' : 'resume', ids });
  };

  const draft = async () => {
    if (!sentence.trim()) return;
    setBusy('draft');
    try {
      const d = await draftFromText(sentence.trim());
      // The server answers in local HH:MM; the form expects the stored (UTC) shape.
      const seed: Partial<Automation> = { ...(d as Partial<Automation>) };
      if (d.scheduled_time) {
        const [h, m] = d.scheduled_time.split(':').map(Number);
        const dt = new Date();
        dt.setHours(h || 0, m || 0, 0, 0);
        seed.scheduled_time = `${String(dt.getUTCHours()).padStart(2, '0')}:${String(dt.getUTCMinutes()).padStart(2, '0')}`;
      }
      setMode({ kind: 'form', existing: null, seed, taskType: (d.task_type as TaskType) || 'llm', trigger: (d.trigger_type as TriggerType) || 'schedule' });
      setSentence('');
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (failed && !tasks) {
    return (
      <div className="fs-screen fs-au" data-testid="automations">
        <EmptyState icon={Workflow} title={t('Could not read your automations')} body={failed} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />
      </div>
    );
  }

  const active = tasks?.filter((x) => x.status === 'active').length ?? 0;
  const anyActive = active > 0;
  const paneOpen = mode.kind !== 'view' || Boolean(current);

  return (
    <div className="fs-screen fs-au" data-testid="automations" data-selecting={selecting || undefined}>
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Automations')}</h1>
          <p className="fs-prose fs-au__lede">
            {tasks ? `${tn(active, '{n} active', '{n} active#')} ${t('of {total}. Each one says when it fires and what it does.', { total: tasks.length })}` : t('Each one says when it fires and what it does.')}
          </p>
        </div>
        <div className="fs-au__head-actions">
          {tasks && tasks.length > 0 && (
            <Button variant="ghost" size="sm" icon={anyActive ? Pause : Play} label={anyActive ? t('Pause all') : t('Resume all')} onClick={toggleAll} testId="automations-toggle-all" />
          )}
          <Button variant="primary" size="sm" icon={Plus} label={t('New automation')} onClick={() => setMode({ kind: 'new' })} testId="automations-new" />
        </div>
      </header>

      <div className="fs-au__toolbar">
        <label className="fs-au__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Search automations…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} data-testid="automations-search" />
        </label>
        <div className="fs-au__chips" role="group" aria-label={t('Category')}>
          <button type="button" className="fs-chip" data-on={category === 'all' || undefined} onClick={() => setCategory('all')}>
            {t('All')} <span className="fs-au__chip-n">{tasks?.length ?? 0}</span>
          </button>
          {categories.map(([c, n]) => (
            <button key={c} type="button" className="fs-chip" data-on={category === c || undefined} onClick={() => setCategory(category === c ? 'all' : c)}>
              {t(CATEGORY_LABEL[c] ?? c)} <span className="fs-au__chip-n">{n}</span>
            </button>
          ))}
        </div>
        <select className="fs-field" value={state} onChange={(e) => setState(e.target.value as typeof state)} aria-label={t('State')}>
          <option value="all">{t('Active and paused')}</option>
          <option value="active">{t('Active')}</option>
          <option value="paused">{t('Paused')}</option>
        </select>
        <span className="fs-au__spacer" />
        <Button
          variant="ghost"
          size="sm"
          icon={selecting ? X : CheckSquare}
          label={selecting ? t('Leave selection') : t('Select several')}
          onClick={() => {
            setSelecting((v) => !v);
            setSelected(new Set());
          }}
          testId="automations-select"
        />
      </div>

      {selecting && (
        <div className="fs-au__bulk" role="toolbar" aria-label={t('Selection')}>
          <label className="fs-switch">
            <input type="checkbox" checked={visible.length > 0 && visible.every((x) => selected.has(x.id))} onChange={(e) => setSelected(e.target.checked ? new Set(visible.map((x) => x.id)) : new Set())} />
            <span>{t('All')}</span>
          </label>
          <span className="fs-au__bulk-n">{tn(selected.size, '{n} selected', '{n} selected#')}</span>
          <span className="fs-au__spacer" />
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={!selected.size} onClick={() => setConfirm({ kind: 'delete', ids: [...selected] })} />
        </div>
      )}

      <div className="fs-au__layout" data-detail={paneOpen ? '' : undefined}>
        <div className="fs-au__list" role="list" aria-label={t('Automations')}>
          {tasks === null ? (
            <Skeleton label={t('Loading automations')} count={4} height="64px" />
          ) : visible.length === 0 ? (
            <EmptyState
              icon={Workflow}
              headingLevel={3}
              title={tasks.length === 0 ? t('No automations yet') : t('Nothing matches')}
              body={tasks.length === 0 ? t('An automation is a recipe: when it fires, what it does and where it delivers.') : t('Try another filter or search.')}
              primaryAction={tasks.length === 0 ? { label: t('New automation'), icon: Plus, onClick: () => setMode({ kind: 'new' }) } : { label: t('Show all'), onClick: () => { setCategory('all'); setState('all'); setQuery(''); } }}
            />
          ) : (
            visible.map((x) => <Row key={x.id} task={x} on={x.id === currentId && mode.kind === 'view'} selecting={selecting} selected={selected.has(x.id)} onOpen={() => (selecting ? toggle(x.id) : open(x.id))} onToggle={() => toggle(x.id)} />)
          )}
        </div>

        <div className="fs-au__pane">
          {mode.kind === 'new' && (
            <section className="fs-au__detail" aria-labelledby="fs-au-new-title" data-testid="automation-new">
              <header className="fs-au__detail-head">
                <div>
                  <h2 id="fs-au-new-title">{t('New automation')}</h2>
                  <p className="fs-au__desc">{t('Say it in a sentence and the assistant drafts the recipe, or start from a shape.')}</p>
                </div>
                <Button variant="ghost" size="sm" icon={X} label={t('Close')} onClick={() => setMode({ kind: 'view' })} />
              </header>
              <form
                className="fs-au__say"
                onSubmit={(e) => {
                  e.preventDefault();
                  void draft();
                }}
              >
                <input className="fs-field fs-au__grow" value={sentence} onChange={(e) => setSentence(e.target.value)} placeholder={t('every weekday at 8, summarise the AI news and mail it to me')} aria-label={t('Describe the automation')} data-testid="automation-sentence" />
                <Button type="submit" variant="secondary" size="sm" icon={Sparkles} label={t('Draft it')} loading={busy === 'draft'} disabled={!sentence.trim()} testId="automation-draft" />
              </form>
              <div className="fs-au__presets">
                {PRESETS.map((p) => (
                  <button key={p.label} type="button" className="fs-au__preset" onClick={() => setMode({ kind: 'form', existing: null, taskType: p.taskType, trigger: p.triggerType })} data-testid={`automation-preset-${p.taskType}-${p.triggerType}`}>
                    <span>{t(p.label)}</span>
                    <small>{t(p.desc)}</small>
                  </button>
                ))}
              </div>
            </section>
          )}

          {mode.kind === 'form' && (
            <section className="fs-au__detail" aria-labelledby="fs-au-form-title" data-testid="automation-form">
              <header className="fs-au__detail-head">
                <div>
                  <h2 id="fs-au-form-title">{mode.existing ? mode.existing.name : t('New automation')}</h2>
                  {mode.seed && <p className="fs-au__desc">{t('Drafted by the assistant — check it before creating.')}</p>}
                </div>
                <Button variant="ghost" size="sm" icon={X} label={t('Close')} onClick={() => setMode({ kind: 'view' })} />
              </header>
              <AutomationForm
                existing={mode.existing}
                seed={mode.seed}
                taskType={mode.taskType}
                trigger={mode.trigger}
                others={tasks ?? []}
                onCancel={() => setMode({ kind: 'view' })}
                onSaved={(created) => {
                  void reload().then(() => {
                    say(created ? t('Created') : t('Saved'));
                    if (created) open(created.id);
                    else setMode({ kind: 'view' });
                  });
                }}
              />
            </section>
          )}

          {mode.kind === 'view' && current && (
            <section className="fs-au__detail" aria-labelledby="fs-au-detail-title" data-testid="automation-detail">
              <div className="fs-au__back">
                <Button variant="ghost" size="sm" icon={X} label={t('All automations')} onClick={() => open(null)} />
              </div>
              <header className="fs-au__detail-head">
                <div className="fs-au__detail-title">
                  <h2 id="fs-au-detail-title">{current.name}</h2>
                  <p className="fs-au__sentence">
                    {describeTrigger(current)} · {describeAction(current)} · {describeOutput(current)}
                  </p>
                </div>
                <StatusBadge status={current.status === 'active' ? 'succeeded' : 'paused'} label={current.status === 'active' ? t('Active') : t('Paused')} size="md" />
              </header>

              <div className="fs-au__actions">
                <Button variant="primary" size="sm" icon={Play} label={t('Run now')} loading={busy === `run:${current.id}`} onClick={() => void runNow(current.id)} testId="automation-run" />
                {current.status === 'active' ? (
                  <Button variant="secondary" size="sm" icon={Pause} label={t('Pause')} loading={busy === `pause:${current.id}`} onClick={() => void act(`pause:${current.id}`, () => pauseAutomation(current.id), t('Paused'))} testId="automation-pause" />
                ) : (
                  <Button variant="secondary" size="sm" icon={Play} label={t('Resume')} loading={busy === `resume:${current.id}`} onClick={() => void act(`resume:${current.id}`, () => resumeAutomation(current.id), t('Active'))} testId="automation-resume" />
                )}
                <Button variant="ghost" size="sm" icon={Pencil} label={t('Edit')} onClick={() => setMode({ kind: 'form', existing: current })} testId="automation-edit" />
                <Button variant="ghost" size="sm" icon={CircleStop} label={t('Stop')} title={t('Stops the run in progress, if there is one')} onClick={() => void act(`stop:${current.id}`, () => stopAutomation(current.id), t('Stopped'))} />
                {current.is_builtin && current.is_modified && <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Revert to default')} onClick={() => setConfirm({ kind: 'revert', id: current.id })} />}
                {current.action && CACHE_LABELS[current.action] && <Button variant="ghost" size="sm" icon={Trash2} label={t('Clear cache')} onClick={() => setConfirm({ kind: 'cache', id: current.id, label: CACHE_LABELS[current.action!] })} />}
                <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={() => setConfirm({ kind: 'delete', ids: [current.id] })} testId="automation-delete" />
              </div>

              <dl className="fs-au__facts">
                <div>
                  <dt>{t('Next run')}</dt>
                  <dd>{current.status === 'active' && current.next_run ? `${relativeTime(current.next_run)} · ${new Date(current.next_run).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' })}` : current.status === 'active' ? t('When it is triggered') : t('Paused — none')}</dd>
                </div>
                <div>
                  <dt>{t('Last run')}</dt>
                  <dd>{current.last_run ? `${relativeTime(current.last_run)} · ${tn(current.run_count ?? 0, '{n} run in total', '{n} runs in total')}` : t('Never')}</dd>
                </div>
                {current.trigger_type === 'event' && (
                  <div>
                    <dt>{t('Counter')}</dt>
                    <dd>{t('{n} of {every} events', { n: current.trigger_counter ?? 0, every: current.trigger_count ?? 1 })}</dd>
                  </div>
                )}
                {current.model && (
                  <div>
                    <dt>{t('Model')}</dt>
                    <dd>{current.model}</dd>
                  </div>
                )}
                {current.then_task_id && (
                  <div>
                    <dt>{t('Then runs')}</dt>
                    <dd>{tasks?.find((x) => x.id === current.then_task_id)?.name ?? current.then_task_id}</dd>
                  </div>
                )}
                {current.character_id && (
                  <div>
                    <dt>{t('Persona')}</dt>
                    <dd>{current.character_id}</dd>
                  </div>
                )}
                <div>
                  <dt>{t('Notifications')}</dt>
                  <dd>{current.notifications_enabled === false ? t('Off') : t('On')}</dd>
                </div>
              </dl>

              {current.trigger_type === 'webhook' && (
                <div className="fs-au__webhook">
                  <span className="fs-au__label">
                    <Link2 size={13} aria-hidden="true" /> {t('Webhook URL')}
                  </span>
                  <code className="fs-au__url" data-testid="automation-webhook">{webhookUrl(current) || t('(no token yet)')}</code>
                  <div className="fs-au__row">
                    <Button
                      variant="secondary"
                      size="sm"
                      icon={Copy}
                      label={t('Copy')}
                      disabled={!current.webhook_token}
                      onClick={() => {
                        navigator.clipboard.writeText(webhookUrl(current)).then(
                          () => say(t('Copied')),
                          () => say(t('The browser refused the clipboard — select the result and copy it by hand.')),
                        );
                      }}
                    />
                    <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Issue a new URL')} title={t('The old one stops working')} loading={busy === `webhook:${current.id}`} onClick={() => void act(`webhook:${current.id}`, async () => void (await regenerateWebhook(current.id)), t('New URL issued'))} />
                  </div>
                </div>
              )}

              {(current.prompt || current.action) && current.task_type !== 'action' && (
                <div className="fs-au__block">
                  <h3>{current.task_type === 'research' ? t('Research question') : t('Prompt')}</h3>
                  <p>{current.prompt}</p>
                </div>
              )}

              <div className="fs-au__block">
                <h3>
                  <HistoryIcon size={13} aria-hidden="true" /> {t('History')}
                </h3>
                <History id={current.id} refreshKey={historyKey} />
              </div>
            </section>
          )}

          {mode.kind === 'view' && !current && (
            <div className="fs-au__blank">
              <Workflow size={28} aria-hidden="true" />
              <p className="fs-prose">{t('Pick an automation to run it, pause it, edit the recipe or read what its runs produced.')}</p>
            </div>
          )}
        </div>
      </div>

      {confirm?.kind === 'delete' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={tn(confirm.ids.length, 'Delete {n} automation?', 'Delete {n} automations?')}
          testId="automations-confirm-delete"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} loading={busy === 'delete'} onClick={() => void remove(confirm.ids)} testId="automations-confirm-delete-ok" />
            </>
          }
        >
          <p className="fs-prose">{t('Its runs go with it. This cannot be undone.')}</p>
        </Dialog>
      )}

      {confirm?.kind === 'all' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={confirm.verb === 'pause' ? tn(confirm.ids.length, 'Pause {n} active automation?', 'Pause all {n} active automations?') : tn(confirm.ids.length, 'Resume {n} paused automation?', 'Resume all {n} paused automations?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="primary"
                size="sm"
                label={confirm.verb === 'pause' ? t('Pause all') : t('Resume all')}
                loading={busy === 'all'}
                onClick={() =>
                  void act(
                    'all',
                    async () => {
                      for (const id of confirm.ids) await (confirm.verb === 'pause' ? pauseAutomation(id) : resumeAutomation(id));
                      setConfirm(null);
                    },
                    confirm.verb === 'pause' ? t('All paused') : t('All active'),
                  )
                }
              />
            </>
          }
        >
          <p className="fs-prose">{confirm.verb === 'pause' ? t('Nothing fires until you resume them.') : t('They pick up their schedules again.')}</p>
        </Dialog>
      )}

      {confirm?.kind === 'cache' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('Clear the cached {what}?', { what: t(confirm.label) })}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="primary"
                size="sm"
                label={t('Clear')}
                loading={busy === 'cache'}
                onClick={() =>
                  void act('cache', async () => {
                    const n = await clearAutomationCache(confirm.id);
                    setConfirm(null);
                    say(n ? t('Cleared {n} items', { n }) : t('Cleared'));
                  })
                }
              />
            </>
          }
        >
          <p className="fs-prose">{t('The next run rebuilds it from scratch.')}</p>
        </Dialog>
      )}

      {confirm?.kind === 'revert' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('Revert to the built-in default?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="primary"
                size="sm"
                label={t('Revert')}
                loading={busy === 'revert'}
                onClick={() =>
                  void act('revert', async () => {
                    await revertAutomation(confirm.id);
                    setConfirm(null);
                  }, t('Reverted'))
                }
              />
            </>
          }
        >
          <p className="fs-prose">{t('Name and schedule go back to what shipped; your edits are lost.')}</p>
        </Dialog>
      )}

      {confirm?.kind === 'parallel' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('It is already running')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Wait for it')} onClick={() => setConfirm(null)} />
              <Button
                variant="primary"
                size="sm"
                icon={AlertTriangle}
                label={t('Run another beside it')}
                onClick={() => {
                  const id = confirm.id;
                  setConfirm(null);
                  void runNow(id, true);
                }}
              />
            </>
          }
        >
          <p className="fs-prose">{t('A second run starts in parallel and both write their results.')}</p>
        </Dialog>
      )}

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}

