import { Activity as ActivityIcon, ArrowUpToLine, Calculator, Check, CircleStop, Copy, Download, ExternalLink, FileText, Link2, ListOrdered, MessageSquare, Play, RefreshCw, Search, Trash2, Waypoints, Workflow, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, friendlyError, MermaidView, Skeleton, StatusBadge, Toast, type RunStatus } from '../components';
import { answerQuestion, artifactLinks, cancelRender, changeWorkflow, decideApproval, duration, getWorkflowRunDefinition, loadActivity, loadQueue, mergeAttention, normaliseStatus, openRunInChat, prioritizeQueueItem, reportUrl, retainUnavailableRuns, type ActivityRun, type ArtifactLink, type QuestionDetail, type QueueItem } from '../adapters/activity';
import { loadAttention, markAttentionRead, type AttentionRow } from '../adapters/attention';
import { CACHE_LABELS, clearAutomationCache, runAutomation, stopAutomation } from '../adapters/automations';
import { relativeTime } from '../adapters/home';
import { stopChat } from '../adapters/chat';
import { traceForCall, type CallTrace } from '../adapters/observability';
import { workflowEstimate, workflowMermaid, type WorkflowEstimate } from '../adapters/topology';
import { createActivityPoller } from '../lib/activity-poller';
import { emitForNewRuns } from '../shell/notifications';
import { Rich } from './rich';
import {ArtifactInfo} from './ArtifactInfo';
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
  // ADP-11: `status === 'waiting'` alone (approval_store cards, open
  // questions) is the ORIGINAL rule — kept so nothing that used to show up
  // here stops. `!!run.attention` adds the four other real "needs a
  // person" states `src/attention.py::classify` distinguishes (disconnected,
  // queued, waiting on a dependency, finished-but-unreviewed), which used to
  // have no home in this tray at all.
  { id: 'accion', label: 'Needs action', match: (run) => run.status === 'waiting' || !!run.attention },
  { id: 'activo', label: 'In progress', match: (run) => run.status === 'running' || run.status === 'queued' },
  { id: 'fallido', label: 'Failed', match: (run) => run.status === 'failed' },
];

type Kind = 'all' | 'task' | 'render' | 'approval' | 'notification' | 'chat' | 'workflow' | 'question';

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="fs-act__fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/**
 * ACT-03: answers an open `ask_user` question straight from the tray —
 * one click per option, a checklist with "Send" when several may apply
 * (`multi`), and always a line for a free-text answer, the same three ways
 * `QuestionCard` (Transcript.tsx) offers on the live card. Deliberately its
 * own small local `picked`/`own` state (mirroring `QuestionCard`'s), reset
 * per question by the `key={questionId}` the caller passes — answering one
 * question must never leave the next one pre-filled with the last one's pick.
 */
function QuestionAnswerPanel({
  question, busy, onAnswer,
}: {
  question: QuestionDetail;
  busy: boolean;
  onAnswer: (text: string, optionIds?: string[]) => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [own, setOwn] = useState('');
  const toggle = (label: string) => setPicked((cur) => (cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label]));
  const sendPicked = () => {
    if (!picked.length) return;
    const ids = question.options.filter((o) => picked.includes(o.label)).map((o) => o.id).filter((id): id is string => Boolean(id));
    onAnswer(picked.join('; '), ids.length ? ids : undefined);
  };
  const sendOwn = () => {
    const text = own.trim();
    if (text) onAnswer(text);
  };
  return (
    <>
      <p className="fs-act__ask">{question.multi ? t('The agent is waiting for you — pick all that apply.') : t('The agent is waiting for you.')}</p>
      <p className="fs-prose">{question.question}</p>
      {question.expiresAt && <p className="fs-act__hint">{t('Expires {time}', { time: relativeTime(question.expiresAt) })}</p>}
      {question.options.length > 0 && !question.multi && (
        <div className="fs-act__actions" role="group" data-testid="activity-question-options">
          {question.options.map((option) => (
            <Button
              key={option.label}
              size="sm"
              label={option.description ? `${option.label} — ${option.description}` : option.label}
              disabled={busy}
              onClick={() => onAnswer(option.label, option.id ? [option.id] : undefined)}
              testId="activity-question-option"
            />
          ))}
        </div>
      )}
      {question.options.length > 0 && question.multi && (
        <>
          <div className="fs-act__actions" role="group" data-testid="activity-question-options">
            {question.options.map((option) => (
              <label key={option.label} className="fs-act__field" data-testid="activity-question-check">
                <input type="checkbox" checked={picked.includes(option.label)} disabled={busy} onChange={() => toggle(option.label)} />
                <span>{option.description ? `${option.label} — ${option.description}` : option.label}</span>
              </label>
            ))}
          </div>
          <div className="fs-act__actions">
            <Button variant="primary" size="sm" icon={Check} label={picked.length ? t('Send {n} picked', { n: picked.length }) : t('Send')} disabled={busy || picked.length === 0} onClick={sendPicked} testId="activity-question-send" />
          </div>
        </>
      )}
      <label className="fs-act__field">
        <span>{t('Or write your own answer')}</span>
        <input className="fs-field" value={own} disabled={busy} onChange={(e) => setOwn(e.target.value)} data-testid="activity-question-own" />
      </label>
      <div className="fs-act__actions">
        <Button size="sm" label={t('Answer')} disabled={busy || !own.trim()} onClick={sendOwn} testId="activity-question-own-send" />
      </div>
    </>
  );
}

const QUEUE_KIND_LABEL: Record<QueueItem['kind'], string> = {
  agent_run: t('Chat'),
  bg_job: t('Command'),
  research: t('Research'),
  media_run: t('Render'),
};

/**
 * ACT-05: everything currently queued or running, across the four systems
 * that each keep their own queue (routes/queue_routes.py's module docstring
 * has the detail). A thin list next to — not folded into — the work list
 * above: a queue row and its matching activity row are the same run, this
 * is only the "what order" view of it, and it disappears on its own once
 * nothing is waiting rather than sitting there empty.
 */
function QueuePanel({
  items, busyId, onPrioritize,
}: {
  items: QueueItem[];
  busyId: string | null;
  onPrioritize: (item: QueueItem) => void;
}) {
  if (items.length === 0) return null;
  return (
    <section className="fs-act__queue" aria-labelledby="fs-act-queue-title" data-testid="activity-queue">
      <h2 id="fs-act-queue-title" className="fs-act__queue-title">
        <ListOrdered size={14} aria-hidden="true" /> {t('Queue')} <span className="fs-act__chip-n">{items.length}</span>
      </h2>
      <ul className="fs-list fs-act__queue-list">
        {items.map((item) => {
          const key = `${item.kind}-${item.id}`;
          const { status, label } = normaliseStatus(item.status);
          return (
            <li className="fs-run fs-act__queue-row" key={key} data-testid="activity-queue-row">
              <span className="fs-run__kind" data-kind={item.kind}>{QUEUE_KIND_LABEL[item.kind]}</span>
              <span className="fs-run__main">
                <span className="fs-row__name">{item.label}</span>
                <span className="fs-row__meta">
                  {[item.position ? t('Position {n}', { n: item.position }) : null, relativeTime(item.startedAt)].filter(Boolean).join(' · ')}
                </span>
              </span>
              <StatusBadge status={status} label={label} size="sm" />
              {item.reorderable && (
                <Button
                  variant="ghost"
                  size="sm"
                  icon={ArrowUpToLine}
                  label={t('Prioritize')}
                  disabled={busyId === key || item.position === 1}
                  loading={busyId === key}
                  onClick={() => onPrioritize(item)}
                  testId="activity-queue-prioritize"
                />
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * OBS-01: given a `call_id` (one tool call), shows everything it produced —
 * its trace events, the artifact(s) it wrote, and its command_guard receipt
 * — joined server-side by `GET /api/observability/trace/{call_id}`
 * (`src/agent_runs.py::trace_for_call`, lote 61). Nothing feeds this a
 * call_id today (Transcript.tsx does not yet carry one per tool event — see
 * the closure report's "Cambios necesarios en ficheros ajenos"), so it
 * works two ways at once: a `?trace=<call_id>&session=<sid>` deep link for
 * whenever that wiring lands, and a manual lookup form so it is already
 * useful standalone — paste a call_id seen in a log or an artifact's
 * `generator` field and see what it did.
 */
function TracePanel() {
  const [params, setParams] = useSearchParams();
  const paramCallId = params.get('trace') ?? '';
  const paramSessionId = params.get('session') ?? '';
  const [callId, setCallId] = useState(paramCallId);
  const [sessionId, setSessionId] = useState(paramSessionId);
  const [open, setOpen] = useState(!!paramCallId);
  const [trace, setTrace] = useState<CallTrace | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const lookup = useCallback(async (id: string, sid: string) => {
    if (!id.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setTrace(await traceForCall(id.trim(), sid.trim() || undefined));
    } catch (e) {
      setTrace(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  // A `?trace=` arriving (or changing) from outside — a future "Ver traza"
  // link — opens the panel and looks it up without the person touching the
  // form first.
  useEffect(() => {
    if (!paramCallId) return;
    setCallId(paramCallId);
    setSessionId(paramSessionId);
    setOpen(true);
    void lookup(paramCallId, paramSessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramCallId, paramSessionId]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (callId.trim()) next.set('trace', callId.trim());
      else next.delete('trace');
      if (sessionId.trim()) next.set('session', sessionId.trim());
      else next.delete('session');
      return next;
    }, { replace: true });
    void lookup(callId, sessionId);
  };

  return (
    <details className="fs-act__trace" open={open} onToggle={(e) => setOpen(e.currentTarget.open)} data-testid="activity-trace">
      <summary className="fs-act__trace-title"><Link2 size={14} aria-hidden="true" /> {t('Trace a tool call')}</summary>
      <form className="fs-act__trace-form" onSubmit={submit}>
        <label className="fs-act__field">
          <span>{t('Call ID')}</span>
          <input className="fs-field" value={callId} onChange={(ev) => setCallId(ev.target.value)} placeholder={t('Paste a call_id…')} data-testid="activity-trace-callid" />
        </label>
        <label className="fs-act__field">
          <span>{t('Session ID (optional — narrows the search to a session you own)')}</span>
          <input className="fs-field" value={sessionId} onChange={(ev) => setSessionId(ev.target.value)} data-testid="activity-trace-session" />
        </label>
        <Button type="submit" size="sm" icon={Search} label={t('Look up')} loading={busy} disabled={!callId.trim() || busy} testId="activity-trace-submit" />
      </form>

      {error && <p className="fs-act__error" role="alert">{error}</p>}

      {trace && !trace.found && !error && (
        <p className="fs-act__hint" data-testid="activity-trace-notfound">{t('Nothing on record under this call ID — check it was copied in full, or that this session had access to it.')}</p>
      )}

      {trace && trace.found && (
        <div className="fs-act__trace-result" data-testid="activity-trace-result">
          {trace.events.length > 0 && (
            <section>
              <h3>{tn(trace.events.length, '{n} event', '{n} events')}</h3>
              <ol className="fs-act__trace-list">
                {trace.events.map((ev, i) => (
                  <li key={i} className="fs-act__trace-item">
                    <div className="fs-act__step-head">
                      <strong>{ev.type}{ev.tool ? ` · ${ev.tool}` : ''}</strong>
                      {ev.round !== null && <span className="fs-act__when">{t('Round {n}', { n: ev.round })}</span>}
                    </div>
                    <pre className="fs-act__pre">{JSON.stringify(ev.raw, null, 2)}</pre>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {trace.artifacts.length > 0 && (
            <section>
              <h3>{tn(trace.artifacts.length, '{n} artifact', '{n} artifacts')}</h3>
              <ul className="fs-act__trace-list">
                {trace.artifacts.map((a) => (
                  <li key={a.occurrenceId || `${a.manifestId}-${a.version}`} className="fs-act__trace-item">
                    <div className="fs-act__step-head"><strong>{a.label || a.manifestId}</strong><span className="fs-act__when">{a.state}</span></div>
                    <dl className="fs-act__facts">
                      <DetailRow label={t('Format')}>{a.format || '—'}</DetailRow>
                      <DetailRow label={t('Size')}>{a.byteSize !== null ? `${a.byteSize.toLocaleString(locale())} B` : '—'}</DetailRow>
                      <DetailRow label={t('SHA-256')}>{a.sha256 || '—'}</DetailRow>
                      <DetailRow label={t('Generator')}>{a.generator || '—'}</DetailRow>
                      <DetailRow label={t('Created')}>{a.createdAt ? new Date(a.createdAt).toLocaleString(locale()) : '—'}</DetailRow>
                      {a.version !== null && <DetailRow label={t('Version')}>{a.version}</DetailRow>}
                    </dl>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {trace.receipt && (
            <section>
              <h3>{t('Permission receipt')}</h3>
              <pre className="fs-act__pre">{JSON.stringify(trace.receipt, null, 2)}</pre>
            </section>
          )}

          {trace.events.length === 0 && trace.artifacts.length === 0 && !trace.receipt && (
            <p className="fs-act__hint">{t('Found this call ID, but it has no events, artifacts or receipt on record.')}</p>
          )}
        </div>
      )}
    </details>
  );
}

export function ActivityScreen() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [runs, setRuns] = useState<ActivityRun[] | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [failed, setFailed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<Kind>('all');
  const [busy, setBusy] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const detailTitle = useRef<HTMLHeadingElement | null>(null);
  const rowButtons = useRef(new Map<string, HTMLButtonElement>());
  const focusAfterSelection = useRef<'detail' | string | null>(null);
  // ACT-02: which runs this tab has already reported to the notification
  // tray (routes/notifications_routes.py dedupes server-side too, so this is
  // only about not spending a network round trip every 5s poll tick for the
  // same still-pending approval).
  const notifiedRuns = useRef(new Set<string>());

  // ACT-05: the Queue section (QueuePanel below) — polled separately from
  // the work feed since it changes on its own faster cadence and a failure
  // to read it should never blank out the rest of the activity screen.
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [queueBusy, setQueueBusy] = useState<string | null>(null);

  // ADP-11: priority/reason/unread per run session (src/attention.py),
  // polled on its own cadence like the Queue section above — merged onto
  // `runs` below (`merged`) rather than folded into the main poller, so a
  // failed attention read degrades to "no reason shown" instead of blanking
  // the whole screen.
  const [attentionRows, setAttentionRows] = useState<AttentionRow[]>([]);
  const [attentionUnread, setAttentionUnread] = useState(0);

  // B2 (OBJ-8): "Ver diagrama"/"Estimar coste" for a workflow run's detail
  // pane. Both need the run's definition, which the activity list never
  // carries (see getWorkflowRunDefinition's own comment) — fetched fresh
  // on each open rather than cached on the run, since a run's own list row
  // already goes stale on its own five-second poll.
  const [diagramFor, setDiagramFor] = useState<string | null>(null);
  const [diagramCode, setDiagramCode] = useState<string | null>(null);
  const [diagramBusy, setDiagramBusy] = useState(false);
  const [diagramError, setDiagramError] = useState<string | null>(null);

  const [estimateFor, setEstimateFor] = useState<string | null>(null);
  const [estimateResult, setEstimateResult] = useState<WorkflowEstimate | null>(null);
  const [estimateBusy, setEstimateBusy] = useState(false);
  const [estimateError, setEstimateError] = useState<string | null>(null);

  const closeDiagram = useCallback(() => {
    setDiagramFor(null);
    setDiagramCode(null);
    setDiagramError(null);
  }, []);

  const openDiagram = useCallback(async (runId: string) => {
    setDiagramFor(runId);
    setDiagramCode(null);
    setDiagramError(null);
    setDiagramBusy(true);
    try {
      const definition = await getWorkflowRunDefinition(runId);
      setDiagramCode(await workflowMermaid(definition));
    } catch (e) {
      setDiagramError(e instanceof Error ? e.message : String(e));
    } finally {
      setDiagramBusy(false);
    }
  }, []);

  const closeEstimate = useCallback(() => {
    setEstimateFor(null);
    setEstimateResult(null);
    setEstimateError(null);
  }, []);

  const openEstimate = useCallback(async (runId: string) => {
    setEstimateFor(runId);
    setEstimateResult(null);
    setEstimateError(null);
    setEstimateBusy(true);
    try {
      const definition = await getWorkflowRunDefinition(runId);
      setEstimateResult(await workflowEstimate(definition));
    } catch (e) {
      setEstimateError(e instanceof Error ? e.message : String(e));
    } finally {
      setEstimateBusy(false);
    }
  }, []);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const filterId = params.get('status') ?? 'todo';
  const filter = FILTERS.find((entry) => entry.id === filterId) ?? FILTERS[0];
  const currentId = params.get('run');

  const poller = useMemo(() => createActivityPoller({
    load: loadActivity,
    live: (data) => data.unavailableKinds.length > 0 || data.runs.some((r) => ['running', 'queued', 'waiting', 'paused'].includes(r.status)),
    visible: () => document.visibilityState === 'visible',
    data: (data) => {
      setRuns((previous) => retainUnavailableRuns(previous ?? [], data));
      setDegraded(data.degraded);
      setFailed(false);
      setUpdatedAt(Date.now());
      void emitForNewRuns(data.runs, notifiedRuns.current);
    },
    error: () => setFailed(true),
    refreshing: setRefreshing,
    schedule: (fn, delay) => window.setTimeout(fn, delay),
    clear: (id) => window.clearTimeout(id),
  }), []);
  const reload = poller.refresh;

  const queuePoller = useMemo(() => createActivityPoller({
    load: loadQueue,
    live: (items) => items.length > 0,
    visible: () => document.visibilityState === 'visible',
    data: setQueue,
    error: () => {}, // ACT-05 is supplementary; a failed poll just leaves the last known queue showing
    refreshing: () => {},
    schedule: (fn, delay) => window.setTimeout(fn, delay),
    clear: (id) => window.clearTimeout(id),
  }), []);

  const attentionPoller = useMemo(() => createActivityPoller({
    load: (signal) => loadAttention(50, signal),
    live: (feed) => feed.rows.length > 0,
    visible: () => document.visibilityState === 'visible',
    data: (feed) => { setAttentionRows(feed.rows); setAttentionUnread(feed.unreadCount); },
    error: () => {}, // supplementary, like ACT-05's queue: a failed poll leaves the last known reasons showing
    refreshing: () => {},
    schedule: (fn, delay) => window.setTimeout(fn, delay),
    clear: (id) => window.clearTimeout(id),
  }), []);

  useEffect(() => {
    poller.start();
    queuePoller.start();
    attentionPoller.start();
    document.addEventListener('visibilitychange', poller.visibilityChanged);
    document.addEventListener('visibilitychange', queuePoller.visibilityChanged);
    document.addEventListener('visibilitychange', attentionPoller.visibilityChanged);
    return () => {
      poller.dispose();
      queuePoller.dispose();
      attentionPoller.dispose();
      if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
      document.removeEventListener('visibilitychange', poller.visibilityChanged);
      document.removeEventListener('visibilitychange', queuePoller.visibilityChanged);
      document.removeEventListener('visibilitychange', attentionPoller.visibilityChanged);
    };
  }, [poller, queuePoller, attentionPoller]);

  const prioritizeQueued = useCallback(async (item: QueueItem) => {
    const key = `${item.kind}-${item.id}`;
    setQueueBusy(key);
    try {
      await prioritizeQueueItem(item.kind, item.id);
      await queuePoller.refresh();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setQueueBusy(null);
    }
  }, [queuePoller, say]);

  // ADP-11: `runs` stays exactly what `loadActivity` returned (the poller's
  // own `data` callback above is untouched); `merged` is the one place
  // attention rows are attached, so every consumer below sees the same
  // reason/priority/unread a raw `runs` read never carried.
  const merged = useMemo(() => (runs ? mergeAttention(runs, attentionRows) : null), [runs, attentionRows]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (merged ?? [])
      .filter((run) => {
        if (!filter.match(run)) return false;
        const isNotification = run.kind === 'task' && run.task?.outputTarget === 'notification';
        if (q && !`${run.title} ${run.detail ?? ''} ${run.chat?.model ?? ''}`.toLowerCase().includes(q)) return false;
        if (kind === 'notification') return isNotification;
        if (isNotification && kind === 'all') return false;
        if (kind !== 'all' && run.kind !== kind) return false;
        return true;
      })
      // ADP-11: within whatever the filter/search already kept, a run that
      // needs a person sorts by how urgently (`classify`'s fixed priority);
      // runs with no attention state (no `.attention`) keep their existing
      // relative order (stable sort — Array.prototype.sort is one since ES2019).
      .sort((a, b) => (a.attention?.priority ?? Number.MAX_SAFE_INTEGER) - (b.attention?.priority ?? Number.MAX_SAFE_INTEGER));
  }, [merged, filter, kind, query]);

  const counts = useMemo(() => {
    const c = { all: 0, task: 0, render: 0, approval: 0, notification: 0, chat: 0, workflow: 0, question: 0 };
    for (const run of merged ?? []) {
      if (run.kind === 'task' && run.task?.outputTarget === 'notification') c.notification++;
      else {
        c.all++;
        c[run.kind]++;
      }
    }
    return c;
  }, [merged]);

  const waiting = useMemo(() => (merged ?? []).filter((run) => run.status === 'waiting' || !!run.attention).length, [merged]);
  const current = useMemo(() => (currentId ? (merged ?? []).find((r) => `${r.kind}-${r.id}` === currentId) ?? null : null), [currentId, merged]);
  const currentStale = failed || !!current?.stale;

  useEffect(() => {
    const target = focusAfterSelection.current;
    if (!target) return;
    focusAfterSelection.current = null;
    if (target === 'detail') detailTitle.current?.focus();
    else rowButtons.current.get(target)?.focus();
  }, [currentId]);

  const open = (run: ActivityRun | null) => {
    if (window.matchMedia('(max-width: 899px)').matches) {
      focusAfterSelection.current = run ? 'detail' : currentId;
    }
    // ADP-11: opening a run marks it read — this only silences the unread
    // badge (optimistic local update, confirmed by the next poll); it never
    // resolves an approval or answers a question, which still go through
    // the exact same routes they always did.
    if (run?.attention) {
      const sid = run.kind === 'chat' ? run.chat?.sessionId : run.kind === 'question' ? run.question?.session : undefined;
      if (sid) {
        void markAttentionRead([sid]);
        setAttentionRows((prev) => prev.map((row) => (row.sessionId === sid ? { ...row, unread: false } : row)));
      }
    }
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
    if (currentStale || busy) return;
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

  if (failed && !runs) {
    return (
      <div className="fs-screen fs-act" data-testid="activity">
        <EmptyState tone="error" icon={ActivityIcon} title={t('Could not read the activity')} body={t('None of the subsystems responded.')} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />
      </div>
    );
  }

  return (
    <div className="fs-screen fs-act" data-testid="activity">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Activity')}</h1>
          <p className="fs-prose fs-act__lede">{waiting > 0 ? tn(waiting, '{n} thing is waiting for your decision.', '{n} things are waiting for your decision.') : t('Conversations, tasks, renders and approvals. Work continues when you leave a chat.')}</p>
        </div>
        <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Refresh')} loading={refreshing} onClick={() => void reload()} testId="activity-refresh" />
      </header>

      {failed && (
        <p className="fs-notice" data-tone="warning" role="status">
          {t('Connection interrupted. Showing the last known activity; it may have changed. Use Refresh to try again.')}
        </p>
      )}
      {!failed && degraded.length > 0 && (
        <p className="fs-notice" data-tone="warning" role="status">
          {t('Could not refresh {what}. Any retained rows are last known activity, not current status. Use Refresh to try again.', { what: degraded.join(', ') })}
        </p>
      )}
      {updatedAt && <p className="fs-act__freshness">{t('Last checked {time}', { time: new Date(updatedAt).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit', second: '2-digit' }) })}</p>}

      <QueuePanel items={queue} busyId={queueBusy} onPrioritize={(item) => void prioritizeQueued(item)} />

      <TracePanel />

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
            {/* ADP-11: the unread counter the ficha asks for, on the one tab
                it actually describes — server-computed (`unread_count`,
                `GET /api/attention`), not re-derived from `attentionRows`
                here, so it matches whatever `mark_read` last settled. */}
            {entry.id === 'accion' && attentionUnread > 0 && (
              <span className="fs-act__unread-count" data-testid="activity-unread-count">{attentionUnread}</span>
            )}
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
              ['question', t('Questions'), counts.question],
              ['chat', t('Conversations'), counts.chat],
              ['task', t('Tasks'), counts.task],
              ['render', t('Renders'), counts.render],
              ['workflow', t('Workflows'), counts.workflow],
              ['approval', t('Approvals'), counts.approval],
              ['notification', t('Notifications'), counts.notification],
            ] as [Kind, string, number][]
          )
            .filter(([k, , n]) => k === 'all' || k === kind || n > 0)
            .map(([k, label, n]) => (
              <button key={k} type="button" className="fs-chip" aria-pressed={kind === k} data-on={kind === k || undefined} onClick={() => setKind(k)} data-testid={`activity-kind-${k}`}>
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
              tone={degraded.length > 0 || failed ? 'error' : 'empty'}
              title={degraded.length > 0 || failed ? t('Some activity is unavailable') : filterId === 'todo' && kind === 'all' && !query ? t('Nothing has run yet') : t('Nothing in this state')}
              body={degraded.length > 0 || failed ? t('The unavailable sources may contain work. Refresh to check again.') : filterId === 'todo' && kind === 'all' && !query ? t('When a task, a render or an agent does something, it will appear here with its state and how long it took.') : t('Try another filter: the one you chose has nothing right now.')}
            />
          )}

          {runs && visible.length > 0 && (
            <div className="fs-list fs-list--rail">
              {visible.map((run) => {
                const key = `${run.kind}-${run.id}`;
                return (
                  <button type="button" className="fs-run fs-act__row" key={key} ref={(node) => { if (node) rowButtons.current.set(key, node); else rowButtons.current.delete(key); }} data-state={run.status} aria-current={key === currentId || undefined} onClick={() => open(run)} data-testid="activity-run">
                    <span className="fs-run__kind" data-kind={run.kind}>
                      {t(run.kind)}
                    </span>
                    <span className="fs-run__main">
                      <span className="fs-row__name">
                        {run.title}
                        {run.repeats > 1 && <span className="fs-act__repeats" title={tn(run.repeats, '{n} identical row', '{n} identical rows')}>×{run.repeats}</span>}
                      </span>
                      {run.detail && <span className="fs-run__detail">{run.detail}</span>}
                      {/* ADP-11: the REASON this run is in "Needs action" — never
                          just a status word. `detail` here is the extra, untranslated
                          context classify() carried (a queue position, a dependency's
                          name), shown as-is next to the translated reason. */}
                      {run.attention?.reason && (
                        <span className="fs-run__detail fs-act__attention-reason">
                          {t(run.attention.reason)}
                          {run.attention.detail ? ` · ${run.attention.detail}` : ''}
                          {run.attention.since ? ` — ${relativeTime(run.attention.since)}` : ''}
                        </span>
                      )}
                      <span className="fs-row__meta">{[relativeTime(run.startedAt), duration(run.startedAt, run.finishedAt)].filter(Boolean).join(' · ')}</span>
                      {(failed || run.stale) && <span className="fs-row__meta">{t('Last known activity')}</span>}
                    </span>
                    {run.attention?.unread && <span className="fs-act__unread-dot" aria-hidden="true" data-testid="activity-unread-dot" />}
                    <StatusBadge status={run.status as RunStatus} label={run.statusLabel} />
                  </button>
                );
              })}
            </div>
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
                  <h2 id="fs-act-title" ref={detailTitle} tabIndex={-1}>{current.title}</h2>
                  <p className="fs-act__when">
                    {current.startedAt ? new Date(current.startedAt).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' }) : ''}
                    {duration(current.startedAt, current.finishedAt) ? ` · ${duration(current.startedAt, current.finishedAt)}` : ''}
                    {current.repeats > 1 ? ` · ${tn(current.repeats, '{n} identical row', '{n} identical rows')}` : ''}
                  </p>
                </div>
                <StatusBadge status={current.status as RunStatus} label={current.statusLabel} size="md" />
              </header>
              {currentStale && <p className="fs-notice" data-tone="warning">{t('Last known activity. Refresh before taking action.')}</p>}

              {current.workflow && <>
                <p className="fs-prose">{t('Started workflows continue in the background. Timed waits resume automatically; human approvals still need your decision.')}</p>
                {current.detail && <p className="fs-act__hint">{current.detail}</p>}
                {['running', 'queued', 'paused', 'waiting'].includes(current.status) && <div className="fs-act__actions">
                  {current.status === 'queued' && <Button icon={Play} label={t('Start workflow')} disabled={currentStale || !!busy} loading={busy === 'workflow-start'}
                    onClick={() => void act('workflow-start', () => changeWorkflow(current.id, 'advance'))} />}
                  <Button variant="danger" icon={CircleStop} label={t('Cancel workflow')} disabled={currentStale || !!busy} loading={busy === 'workflow-cancel'}
                    onClick={() => void act('workflow-cancel', () => changeWorkflow(current.id, 'cancel'))} />
                </div>}
                <div className="fs-act__actions">
                  <Button variant="secondary" size="sm" icon={Waypoints} label={t('View diagram')} disabled={currentStale} loading={diagramBusy && diagramFor === current.id}
                    onClick={() => void openDiagram(current.id)} testId="activity-workflow-diagram" />
                  <Button variant="secondary" size="sm" icon={Calculator} label={t('Estimate cost')} disabled={currentStale} loading={estimateBusy && estimateFor === current.id}
                    onClick={() => void openEstimate(current.id)} testId="activity-workflow-estimate" />
                </div>
                <dl className="fs-act__facts">
                  <DetailRow label={t('Workflow')}>{current.workflow.recipe}</DetailRow>
                  {current.workflow.projectId && <DetailRow label={t('Project')}>{current.workflow.projectId}</DetailRow>}
                </dl>
                <h3>{t('Steps')}</h3>
                <ol className="fs-act__workflow" data-testid="activity-workflow-steps">
                  {current.workflow.nodes.map((node) => <li key={node.id}>
                    <div className="fs-act__step-head"><strong>{node.title}</strong><StatusBadge status={node.status === 'paused' && node.approvalId ? 'waiting' : normaliseStatus(node.status).status} label={node.status === 'pending' ? t('Not started') : normaliseStatus(node.status).label} /></div>
                    {node.reason && <p>{node.reason}</p>}
                    <ArtifactDownloads items={node.artifacts} />
                    {node.needs.length > 0 && <p>{t('After')}: {node.needs.map((id) => current.workflow!.nodes.find((n) => n.id === id)?.title || id).join(', ')}</p>}
                    {node.wakeAt && Number.isFinite(Date.parse(node.wakeAt)) && node.status === 'paused' && <p>{t('Resumes at {time}', { time: new Date(node.wakeAt).toLocaleString(locale()) })}</p>}
                    {node.status === 'paused' && node.approvalId && current.status === 'waiting' && <div className="fs-act__actions">
                      <Link className="fs-act__link" to={`/activity?run=approval-${encodeURIComponent(node.approvalId)}`}>{t('Review approval')}</Link>
                      <Button size="sm" variant="secondary" icon={RefreshCw} label={t('Check decision')} disabled={currentStale || !!busy} loading={busy === `resume-${node.id}`}
                        onClick={() => void act(`resume-${node.id}`, () => changeWorkflow(current.id, 'advance', node.id))} />
                    </div>}
                  </li>)}
                </ol>
              </>}

              {current.kind === 'chat' && current.chat && (
                <>
                  <p className="fs-act__chat-phase" role="status">{current.detail}</p>
                  <p className="fs-prose">{current.status === 'waiting'
                    ? t('Open this conversation to review and answer its permission request.')
                    : t('This work runs on the server. You can use other chats while it continues.')}</p>
                  <div className="fs-act__actions">
                    <Button variant="primary" icon={MessageSquare} label={t('Open conversation')}
                      onClick={() => navigate(`/studio?s=${encodeURIComponent(current.chat!.sessionId)}`)} testId="activity-open-conversation" />
                    {current.chat.runId && <Button variant="danger" size="sm" icon={CircleStop} label={t('Stop this run')}
                      loading={busy === 'stop-chat'} disabled={!!busy || currentStale}
                      onClick={() => void act('stop-chat', async () => {
                        const stopped = await stopChat(current.chat!.sessionId, current.chat!.runId);
                        if (!stopped) throw new Error(t('Could not stop this run. It may have ended; refresh its status before trying again.'));
                      }, t('Stopped'))} testId="activity-stop-conversation" />}
                  </div>
                  <dl className="fs-act__facts">
                    {current.chat.model && <DetailRow label={t('Model')}>{current.chat.model}</DetailRow>}
                    {current.chat.progress && <>
                      <DetailRow label={t('Elapsed')}>{duration(current.startedAt, new Date((current.chat.progress.startedAt || 0) + current.chat.progress.elapsedS * 1000).toISOString()) || '—'}</DetailRow>
                      {current.chat.progress.lastEventAt > 0 && <DetailRow label={t('Last progress')}>{new Date(current.chat.progress.lastEventAt).toLocaleTimeString(locale())}</DetailRow>}
                      {current.chat.progress.round > 0 && <DetailRow label={t('Round')}>{current.chat.progress.round}</DetailRow>}
                    </>}
                  </dl>
                  {current.status === 'running' && <p className="fs-act__hint">{t('A quiet model is not necessarily stuck. Last progress refers to model or tool output, not a connection heartbeat.')}</p>}
                </>
              )}

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
                    <Button variant="primary" size="sm" icon={Check} label={t('Approve')} disabled={currentStale || !!busy} loading={busy === 'grant'} onClick={() => void act('grant', () => decideApproval(current.approval!.approvalId, true, reason).then(() => open(null)), t('Approved'))} testId="activity-approve" />
                    <Button variant="danger" size="sm" icon={X} label={t('Deny')} disabled={currentStale || !!busy} loading={busy === 'deny'} onClick={() => void act('deny', () => decideApproval(current.approval!.approvalId, false, reason).then(() => open(null)), t('Denied'))} testId="activity-deny" />
                  </div>
                </>
              )}

              {current.kind === 'question' && current.question && (
                <QuestionAnswerPanel
                  key={current.question.questionId}
                  question={current.question}
                  busy={currentStale || !!busy}
                  onAnswer={(text, optionIds) =>
                    void act('answer', () => answerQuestion(current.question!, text, optionIds).then(() => open(null)), t('Answered'))
                  }
                />
              )}

              {current.kind === 'task' && current.task && (
                <>
                  <div className="fs-act__actions">
                    {(current.task.taskType === 'llm' || current.task.taskType === 'research') && current.task.result.trim() && current.status !== 'running' && current.status !== 'queued' && (
                      <Button variant="primary" size="sm" icon={MessageSquare} label={t('Open in a chat')} disabled={currentStale || !!busy} loading={busy === 'chat'} onClick={() => void act('chat', async () => navigate(`/studio?s=${encodeURIComponent(await openRunInChat(current))}`))} testId="activity-open-chat" />
                    )}
                    {reportUrl(current) && <Button variant="secondary" size="sm" icon={FileText} label={t('Open the report')} onClick={() => window.open(reportUrl(current), '_blank', 'noopener')} />}
                    {current.task.taskId && (current.status === 'running' || current.status === 'queued') && (
                      <Button variant="danger" size="sm" icon={CircleStop} label={t('Stop')} disabled={currentStale || !!busy} loading={busy === 'stop'} onClick={() => void act('stop', () => stopAutomation(current.task!.taskId), t('Stopped'))} />
                    )}
                    {current.task.taskId && current.status !== 'running' && current.status !== 'queued' && (
                      <Button variant="secondary" size="sm" icon={Play} label={t('Run again')} disabled={currentStale || !!busy} loading={busy === 'again'} onClick={() => void act('again', () => runAutomation(current.task!.taskId), t('Started'))} testId="activity-run-again" />
                    )}
                    {current.task.taskId && (current.status === 'running' || current.status === 'queued') && (
                      <Button variant="ghost" size="sm" icon={Play} label={t('Run another beside it')} disabled={currentStale || !!busy} loading={busy === 'force'} onClick={() => void act('force', () => runAutomation(current.task!.taskId, true), t('Started a second run beside the first'))} />
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
                      <Button variant="ghost" size="sm" icon={Trash2} label={t('Clear cache')} disabled={currentStale || !!busy} loading={busy === 'cache'} onClick={() => void act('cache', async () => void (await clearAutomationCache(current.task!.taskId)), t('Cleared'))} />
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
                  {current.task.error && (() => {
                    // UX-08: `current.task.error` is sometimes the literal
                    // §34.5 error object / `_stream_error_chunk` payload
                    // stored as the failure reason — not raw text, so it
                    // must never reach the screen as an unparsed JSON blob.
                    const friendly = friendlyError(current.task!.error);
                    return (
                      <p className="fs-act__error" role="alert">
                        {friendly.category
                          ? t('{title}: {message} — {action}', { title: friendly.title, message: friendly.message, action: friendly.action })
                          : friendly.message}
                      </p>
                    );
                  })()}
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
                  <ArtifactDownloads items={artifactLinks(current.render.record.artifacts || current.render.record.artifact_ids)} />
                  <div className="fs-act__actions">
                    {(current.status === 'running' || current.status === 'queued') && <Button variant="danger" size="sm" icon={CircleStop} label={t('Cancel the render')} disabled={currentStale || !!busy} loading={busy === 'cancel'} onClick={() => void act('cancel', () => cancelRender(current.render!.runId), t('Cancelled'))} />}
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

      {diagramFor && (
        <Dialog open onOpenChange={(next) => { if (!next) closeDiagram(); }} title={t('Workflow diagram')} testId="activity-diagram-dialog">
          {diagramBusy && <Skeleton label={t('Rendering the diagram')} height="120px" count={2} />}
          {diagramError && <p className="fs-act__error" role="alert">{diagramError}</p>}
          {diagramCode && !diagramBusy && <MermaidView code={diagramCode} filename={`${diagramFor}.mmd`} />}
        </Dialog>
      )}

      {estimateFor && (
        <Dialog open onOpenChange={(next) => { if (!next) closeEstimate(); }} title={t('Workflow cost estimate')} testId="activity-estimate-dialog">
          {estimateBusy && <Skeleton label={t('Estimating the cost')} height="52px" count={3} />}
          {estimateError && <p className="fs-act__error" role="alert">{estimateError}</p>}
          {estimateResult && !estimateBusy && <WorkflowEstimateView estimate={estimateResult} />}
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

/**
 * B2 (OBJ-8): `POST /api/workflows/estimate`'s `Estimate`
 * (`docs/api/topology.md`'s "Cost estimate" section) — a min/max range, not
 * one number, because a `condition` branch or a cycle genuinely might or
 * might not run. `unpricedModels` and each `skill` node's own blank `note`
 * are shown rather than folded away: a model missing from the caller's
 * price map contributes $0 to the total, and that is a fact about the
 * estimate the person needs to see, not a rounding error to hide.
 */
function usd(value: number): string {
  return value.toLocaleString(locale(), { style: 'currency', currency: 'USD', maximumFractionDigits: 4 });
}

function WorkflowEstimateView({ estimate }: { estimate: WorkflowEstimate }) {
  return (
    <div className="fs-estimate" data-testid="workflow-estimate">
      <p className="fs-estimate__total">
        {t('Estimated total: {range} · {calls} calls', {
          range: estimate.totalUsdMin === estimate.totalUsdMax ? usd(estimate.totalUsdMin) : `${usd(estimate.totalUsdMin)} – ${usd(estimate.totalUsdMax)}`,
          calls: estimate.callsMin === estimate.callsMax ? String(estimate.callsMin) : `${estimate.callsMin}–${estimate.callsMax}`,
        })}
      </p>
      <p className="fs-act__hint">{t('Only skill nodes are priced; every other node type is structural and costs nothing by itself.')}</p>
      <div className="fs-estimate__table-wrap">
        <table className="fs-estimate__table">
          <thead>
            <tr>
              <th>{t('Node')}</th>
              <th>{t('Type')}</th>
              <th>{t('Model')}</th>
              <th>{t('Calls')}</th>
              <th>{t('Cost (USD)')}</th>
            </tr>
          </thead>
          <tbody>
            {estimate.perNode.map((n) => (
              <tr key={n.nodeId}>
                <td><code>{n.nodeId}</code></td>
                <td>{n.type}</td>
                <td>{n.model || '—'}</td>
                <td>{n.callsMin === n.callsMax ? n.callsMin : `${n.callsMin}–${n.callsMax}`}</td>
                <td>
                  {n.usdMin === n.usdMax ? usd(n.usdMin) : `${usd(n.usdMin)} – ${usd(n.usdMax)}`}
                  {n.note && <span className="fs-estimate__note"> — {n.note}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {estimate.unboundedLoops.length > 0 && (
        <div className="fs-notice" data-tone="warning" role="status">
          <strong>{t('Unbounded loops')}:</strong>{' '}
          {estimate.unboundedLoops.map((loop, i) => (
            <span key={i}>
              {i > 0 && '; '}
              {loop.nodes.join(' → ')} ({tn(loop.assumedIterations, 'assumed {n} iteration for the maximum', 'assumed {n} iterations for the maximum')})
            </span>
          ))}
        </div>
      )}
      {estimate.unpricedModels.length > 0 && (
        <p className="fs-act__hint">{t('Models without a price (contribute $0 to this estimate)')}: {estimate.unpricedModels.join(', ')}</p>
      )}
    </div>
  );
}

function ArtifactDownloads({ items }: { items: ArtifactLink[] }) {
  if (!items.length) return null;
  return <div className="fs-act__outputs" aria-label={t('Generated files')}>
    {items.map((item) => <div key={item.id}><a className="fs-act__link" href={item.url} download
      aria-label={t('Download {name}', { name: item.label })}>
      <Download size={16} aria-hidden="true" /><span>{item.label}</span>
    </a><ArtifactInfo id={item.id}/></div>)}
  </div>;
}
