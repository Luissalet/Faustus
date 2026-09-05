import { Activity as ActivityIcon, Check, CircleStop, Copy, ExternalLink, FileText, MessageSquare, Play, Search, Trash2, Workflow, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { Button, EmptyState, Skeleton, StatusBadge, Toast, type RunStatus } from '../components';
import { cancelRender, decideApproval, duration, loadActivity, openRunInChat, reportUrl, type ActivityRun } from '../adapters/activity';
import { CACHE_LABELS, clearAutomationCache, runAutomation, stopAutomation } from '../adapters/automations';
import { relativeTime } from '../adapters/home';
import { Rich } from './rich';
import './projects.css';
import './home.css';
import './activity.css';
import { locale, t, tn } from '../i18n';

/**
 * Actividad (UI-050 / UI-051).
 *
 * Every kind of work in one list with one vocabulary. Not a log dump: the
 * raw output, tools and evidence stay in the run's own detail — which is
 * the pane on the right, where the run can also be acted on: approve or
 * deny, open the result in a chat, run again, stop, copy.
 */

const FILTERS: { id: string; label: string; match: (run: ActivityRun) => boolean }[] = [
  { id: 'todo', label: 'All', match: () => true },
  { id: 'accion', label: 'Needs action', match: (run) => run.status === 'waiting' },
  { id: 'activo', label: 'In progress', match: (run) => run.status === 'running' || run.status === 'queued' },
  { id: 'fallido', label: 'Failed', match: (run) => run.status === 'failed' },
];

type Kind = 'all' | 'task' | 'render' | 'approval' | 'notification';

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="fs-act__fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function ActivityScreen() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [runs, setRuns] = useState<ActivityRun[] | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<Kind>('all');
  const [busy, setBusy] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const filterId = params.get('status') ?? 'todo';
  const filter = FILTERS.find((entry) => entry.id === filterId) ?? FILTERS[0];
  const currentId = params.get('run');

  const reload = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await loadActivity(signal);
      setRuns(data.runs);
      setDegraded(data.degraded);
      setFailed(false);
    } catch {
      if (!signal?.aborted) setFailed(true);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  // Runs move on their own; look again while something is in flight.
  useEffect(() => {
    const live = runs?.some((r) => r.status === 'running' || r.status === 'queued' || r.status === 'waiting');
    const id = window.setInterval(() => void reload(), live ? 5000 : 30000);
    return () => window.clearInterval(id);
  }, [runs, reload]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (runs ?? []).filter((run) => {
      if (!filter.match(run)) return false;
      const isNotification = run.kind === 'task' && run.task?.outputTarget === 'notification';
      if (kind === 'notification') return isNotification;
      if (isNotification && kind === 'all') return false;
      if (kind !== 'all' && run.kind !== kind) return false;
      if (q && !`${run.title} ${run.detail ?? ''}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [runs, filter, kind, query]);

  const counts = useMemo(() => {
    const c = { all: 0, task: 0, render: 0, approval: 0, notification: 0 };
    for (const run of runs ?? []) {
      if (run.kind === 'task' && run.task?.outputTarget === 'notification') c.notification++;
      else {
        c.all++;
        c[run.kind]++;
      }
    }
    return c;
  }, [runs]);

  const waiting = useMemo(() => (runs ?? []).filter((run) => run.status === 'waiting').length, [runs]);
  const current = useMemo(() => (currentId ? (runs ?? []).find((r) => `${r.kind}-${r.id}` === currentId) ?? null : null), [currentId, runs]);

  const open = (run: ActivityRun | null) => {
    setReason('');
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (run) next.set('run', `${run.kind}-${run.id}`);
        else next.delete('run');
        return next;
      },
      { replace: true },
    );
  };

  const act = async (key: string, fn: () => Promise<void>, done?: string) => {
    setBusy(key);
    try {
      await fn();
      await reload();
      if (done) say(done);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (failed) {
    return (
      <div className="fs-screen fs-act" data-testid="activity">
        <EmptyState icon={ActivityIcon} title={t('Could not read the activity')} body={t('None of the subsystems responded.')} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />
      </div>
    );
  }

  return (
    <div className="fs-screen fs-act" data-testid="activity">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Activity')}</h1>
          <p className="fs-prose fs-act__lede">{waiting > 0 ? tn(waiting, '{n} thing is waiting for your decision.', '{n} things are waiting for your decision.') : t('Tasks, renders and approvals, in the same language for all.')}</p>
        </div>
      </header>

      <div className="fs-tabs" role="tablist" aria-label={t('Filter activity')}>
        {FILTERS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={entry.id === filter.id}
            className="fs-tab"
            data-testid={`activity-filter-${entry.id}`}
            onClick={() => {
              const next = new URLSearchParams(params);
              if (entry.id === 'todo') next.delete('status');
              else next.set('status', entry.id);
              setParams(next);
            }}
          >
            {t(entry.label)}
          </button>
        ))}
      </div>

      <div className="fs-act__toolbar">
        <label className="fs-act__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Filter the activity…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} data-testid="activity-search" />
        </label>
        <div className="fs-act__chips" role="group" aria-label={t('Kind')}>
          {(
            [
              ['all', t('All'), counts.all],
              ['task', t('Tasks'), counts.task],
              ['render', t('Renders'), counts.render],
              ['approval', t('Approvals'), counts.approval],
              ['notification', t('Notifications'), counts.notification],
            ] as [Kind, string, number][]
          )
            .filter(([k, , n]) => k === 'all' || n > 0)
            .map(([k, label, n]) => (
              <button key={k} type="button" className="fs-chip" data-on={kind === k || undefined} onClick={() => setKind(k)} data-testid={`activity-kind-${k}`}>
                {label} <span className="fs-act__chip-n">{n}</span>
              </button>
            ))}
        </div>
      </div>

      <div className="fs-act__layout" data-detail={current ? '' : undefined}>
        <div className="fs-act__list">
          {!runs && <Skeleton label={t('Loading the activity')} count={6} height="52px" />}

          {runs && visible.length === 0 && (
            <EmptyState
              icon={ActivityIcon}
              headingLevel={3}
              title={filterId === 'todo' && kind === 'all' && !query ? t('Nothing has run yet') : t('Nothing in this state')}
              body={filterId === 'todo' && kind === 'all' && !query ? t('When a task, a render or an agent does something, it will appear here with its state and how long it took.') : t('Try another filter: the one you chose has nothing right now.')}
            />
          )}

          {runs && visible.length > 0 && (
            <div className="fs-list fs-list--rail">
              {visible.map((run) => {
                const key = `${run.kind}-${run.id}`;
                return (
                  <button type="button" className="fs-run fs-act__row" key={key} data-state={run.status} aria-current={key === currentId || undefined} onClick={() => open(run)} data-testid="activity-run">
                    <span className="fs-run__kind" data-kind={run.kind}>
                      {t(run.kind)}
                    </span>
                    <span className="fs-run__main">
                      <span className="fs-row__name">
                        {run.title}
                        {run.repeats > 1 && <span className="fs-act__repeats" title={tn(run.repeats, '{n} identical row', '{n} identical rows')}>×{run.repeats}</span>}
                      </span>
                      {run.detail && <span className="fs-run__detail">{run.detail}</span>}
                      <span className="fs-row__meta">{[relativeTime(run.startedAt), duration(run.startedAt, run.finishedAt)].filter(Boolean).join(' · ')}</span>
                    </span>
                    <StatusBadge status={run.status as RunStatus} label={run.statusLabel} />
                  </button>
                );
              })}
            </div>
          )}

          {degraded.length > 0 && (
            <p className="fs-notice" data-tone="warning">
              {t('Could not read {what}. The rest of the list is real.', { what: degraded.join(', ') })}
            </p>
          )}
        </div>

        <div className="fs-act__pane">
          {current ? (
            <section className="fs-act__detail" aria-labelledby="fs-act-title" data-testid="activity-detail" data-kind={current.kind}>
              <div className="fs-act__back">
                <Button variant="ghost" size="sm" icon={X} label={t('All activity')} onClick={() => open(null)} />
              </div>
              <header className="fs-act__head">
                <div className="fs-act__title">
                  <span className="fs-run__kind" data-kind={current.kind}>
                    {t(current.kind)}
                  </span>
                  <h2 id="fs-act-title">{current.title}</h2>
                  <p className="fs-act__when">
                    {current.startedAt ? new Date(current.startedAt).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' }) : ''}
                    {duration(current.startedAt, current.finishedAt) ? ` · ${duration(current.startedAt, current.finishedAt)}` : ''}
                    {current.repeats > 1 ? ` · ${tn(current.repeats, '{n} identical row', '{n} identical rows')}` : ''}
                  </p>
                </div>
                <StatusBadge status={current.status as RunStatus} label={current.statusLabel} size="md" />
              </header>

              {current.kind === 'approval' && current.approval && (
                <>
                  <p className="fs-act__ask">{t('The agent wants to do this and is waiting for you. Nothing happens until you decide.')}</p>
                  <dl className="fs-act__facts">
                    <DetailRow label={t('Action')}>{current.approval.action || '—'}</DetailRow>
                    {current.approval.detail && <DetailRow label={t('Detail')}>{current.approval.detail}</DetailRow>}
                    {current.approval.skillId && <DetailRow label={t('Skill')}>{current.approval.skillId}</DetailRow>}
                    {current.approval.backend && <DetailRow label={t('Backend')}>{current.approval.backend}</DetailRow>}
                    {current.approval.recipients.length > 0 && <DetailRow label={t('Recipients')}>{current.approval.recipients.join(', ')}</DetailRow>}
                    {current.approval.costUnits !== null && <DetailRow label={t('Cost')}>{current.approval.costUnits}</DetailRow>}
                    {current.approval.secretNames.length > 0 && <DetailRow label={t('Secrets it would use')}>{current.approval.secretNames.join(', ')}</DetailRow>}
                    {current.approval.outputKinds.length > 0 && <DetailRow label={t('Produces')}>{current.approval.outputKinds.join(', ')}</DetailRow>}
                    {Object.keys(current.approval.permissions).length > 0 && <DetailRow label={t('Permissions')}>{JSON.stringify(current.approval.permissions)}</DetailRow>}
                    {current.approval.expiresAt && <DetailRow label={t('Expires')}>{relativeTime(current.approval.expiresAt)}</DetailRow>}
                    {current.approval.usesLeft !== 1 && <DetailRow label={t('Uses left')}>{current.approval.usesLeft}</DetailRow>}
                  </dl>
                  <label className="fs-act__field">
                    <span>{t('A note for the record (optional)')}</span>
                    <input className="fs-field" value={reason} onChange={(e) => setReason(e.target.value)} data-testid="activity-reason" />
                  </label>
                  <div className="fs-act__actions">
                    <Button variant="primary" size="sm" icon={Check} label={t('Approve')} loading={busy === 'grant'} onClick={() => void act('grant', () => decideApproval(current.approval!.approvalId, true, reason).then(() => open(null)), t('Approved'))} testId="activity-approve" />
                    <Button variant="danger" size="sm" icon={X} label={t('Deny')} loading={busy === 'deny'} onClick={() => void act('deny', () => decideApproval(current.approval!.approvalId, false, reason).then(() => open(null)), t('Denied'))} testId="activity-deny" />
                  </div>
                </>
              )}

              {current.kind === 'task' && current.task && (
                <>
                  <div className="fs-act__actions">
                    {(current.task.taskType === 'llm' || current.task.taskType === 'research') && current.task.result.trim() && current.status !== 'running' && current.status !== 'queued' && (
                      <Button variant="primary" size="sm" icon={MessageSquare} label={t('Open in a chat')} loading={busy === 'chat'} onClick={() => void act('chat', async () => navigate(`/studio?s=${encodeURIComponent(await openRunInChat(current))}`))} testId="activity-open-chat" />
                    )}
                    {reportUrl(current) && <Button variant="secondary" size="sm" icon={FileText} label={t('Open the report')} onClick={() => window.open(reportUrl(current), '_blank', 'noopener')} />}
                    {current.task.taskId && (current.status === 'running' || current.status === 'queued') && (
                      <Button variant="danger" size="sm" icon={CircleStop} label={t('Stop')} loading={busy === 'stop'} onClick={() => void act('stop', () => stopAutomation(current.task!.taskId), t('Stopped'))} />
                    )}
                    {current.task.taskId && current.status !== 'running' && current.status !== 'queued' && (
                      <Button variant="secondary" size="sm" icon={Play} label={t('Run again')} loading={busy === 'again'} onClick={() => void act('again', () => runAutomation(current.task!.taskId), t('Started'))} testId="activity-run-again" />
                    )}
                    {current.task.taskId && (current.status === 'running' || current.status === 'queued') && (
                      <Button variant="ghost" size="sm" icon={Play} label={t('Run another beside it')} loading={busy === 'force'} onClick={() => void act('force', () => runAutomation(current.task!.taskId, true), t('Started a second run beside the first'))} />
                    )}
                    {(current.task.result || current.task.error) && (
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={Copy}
                        label={t('Copy')}
                        onClick={() => {
                          navigator.clipboard.writeText(current.task!.result || current.task!.error).then(
                            () => say(t('Copied')),
                            () => say(t('The browser refused the clipboard — select the result and copy it by hand.')),
                          );
                        }}
                      />
                    )}
                    {current.task.action && CACHE_LABELS[current.task.action] && current.task.taskId && (
                      <Button variant="ghost" size="sm" icon={Trash2} label={t('Clear cache')} loading={busy === 'cache'} onClick={() => void act('cache', async () => void (await clearAutomationCache(current.task!.taskId)), t('Cleared'))} />
                    )}
                    {current.task.taskId && (
                      <Link className="fs-act__link" to={`/automations?task=${encodeURIComponent(current.task.taskId)}`}>
                        <Workflow size={13} aria-hidden="true" /> {t('The automation')}
                      </Link>
                    )}
                  </div>
                  <dl className="fs-act__facts">
                    {current.task.model && <DetailRow label={t('Model')}>{current.task.model.split('/').pop()}</DetailRow>}
                    {current.task.tokens !== null && current.task.tokens > 0 && <DetailRow label={t('Tokens')}>{current.task.tokens.toLocaleString(locale())}</DetailRow>}
                    <DetailRow label={t('Delivered')}>{current.task.outputTarget}</DetailRow>
                    {current.task.action && <DetailRow label={t('Action')}>{current.task.action.replace(/_/g, ' ')}</DetailRow>}
                  </dl>
                  {current.task.error && (
                    <p className="fs-act__error" role="alert">
                      {current.task.error}
                    </p>
                  )}
                  {current.task.result ? (
                    <div className="fs-act__result" data-testid="activity-result">
                      <Rich text={current.task.result} />
                    </div>
                  ) : (
                    !current.task.error && <p className="fs-act__hint">{current.status === 'queued' ? t('Queued — waiting for a free slot…') : current.status === 'running' ? t('Running…') : t('It produced no output.')}</p>
                  )}
                </>
              )}

              {current.kind === 'render' && current.render && (
                <>
                  <div className="fs-act__actions">
                    {(current.status === 'running' || current.status === 'queued') && <Button variant="danger" size="sm" icon={CircleStop} label={t('Cancel the render')} loading={busy === 'cancel'} onClick={() => void act('cancel', () => cancelRender(current.render!.runId), t('Cancelled'))} />}
                    <Link className="fs-act__link" to="/library?type=imagen">
                      <ExternalLink size={13} aria-hidden="true" /> {t('The images')}
                    </Link>
                  </div>
                  <dl className="fs-act__facts">
                    {Object.entries(current.render.record)
                      .filter(([k, v]) => !['id', 'run_id', 'created_at'].includes(k) && v !== null && v !== '' && typeof v !== 'object')
                      .map(([k, v]) => (
                        <DetailRow key={k} label={k.replace(/_/g, ' ')}>
                          {String(v)}
                        </DetailRow>
                      ))}
                  </dl>
                  {Object.entries(current.render.record).some(([, v]) => v && typeof v === 'object') && (
                    <pre className="fs-act__pre">{JSON.stringify(Object.fromEntries(Object.entries(current.render.record).filter(([, v]) => v && typeof v === 'object')), null, 2)}</pre>
                  )}
                </>
              )}
            </section>
          ) : (
            <div className="fs-act__blank">
              <ActivityIcon size={28} aria-hidden="true" />
              <p className="fs-prose">{t('Pick a row to read what it produced, approve or deny what is waiting, or run it again.')}</p>
            </div>
          )}
        </div>
      </div>

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
