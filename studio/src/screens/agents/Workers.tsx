import { ChevronDown, ChevronRight, MessageSquare, Play, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import {
  cancelJob,
  dispatchConfig,
  followJob,
  getJob,
  isLive,
  listJobs,
  parseTasks,
  PROOF_TONE,
  PROOF_WORD,
  startJob,
  type DispatchJob,
  type DispatchRequest,
  type Verification,
} from '../../adapters/workers';
import { locale, t, tn } from '../../i18n';

/**
 * Workers: the dispatch board (workers.js). Describe the tasks, name the
 * folder, Run — the local models do the mechanical work, Faustus checkpoints
 * the folder before, diffs it after, runs the verification itself and
 * marks the job partial when anything did not finish. Live jobs are followed
 * over SSE when the server streams, polled every 3 s otherwise.
 */

const FOLDER_KEY = 'odysseus-workers-folder';

function fmtDur(s: number): string {
  const n = Math.round(s);
  return n < 90 ? `${n} s` : n < 3600 ? `${Math.round(n / 60)} min` : `${(n / 3600).toFixed(1)} h`;
}

function when(ts: number): string {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

const STATUS_WORD: Record<string, string> = {
  queued: 'queued',
  running: 'running',
  verifying: 'verifying',
  done: 'done',
  partial: 'partial',
  error: 'error',
  cancelling: 'cancelling',
  cancelled: 'cancelled',
  interrupted: 'interrupted',
};

function VerificationBlock({ v }: { v: Verification }) {
  if (!v.ran) return <div className="fs-wk__verify" data-state="none">{t('Not verified')} — {v.summary}</div>;
  const state = v.ok ? 'passed' : v.inconclusive ? 'inconclusive' : 'failed';
  const word = v.ok ? t('passed') : v.inconclusive ? t('inconclusive') : t('failed');
  const pre = new Set(v.pre_existing);
  return (
    <div className="fs-wk__verify" data-state={state}>
      <b>{t('Verification')}: {word}</b> — {v.summary}
      {v.command && <code className="fs-wk__code">{v.command}</code>}
      {v.attempts > 1 && <span className="fs-wk__muted"> · {t('{n} attempts', { n: v.attempts })}</span>}
      {v.failures.length > 0 && (
        <ul className="fs-wk__fails">
          {v.failures.map((f, i) => (
            <li key={i}>
              {f}
              {pre.has(f) && <span className="fs-wk__muted"> {t('(failed before the job too)')}</span>}
            </li>
          ))}
        </ul>
      )}
      {!v.ok && v.output_tail && (
        <details className="fs-wk__tail">
          <summary>{t('output')}</summary>
          <pre>{v.output_tail.slice(-1500)}</pre>
        </details>
      )}
      {v.previous.length > 0 && (
        <div className="fs-wk__muted">
          {tn(v.previous.length, 'Before the fix round:', 'Before the fix rounds:')} {v.previous.map((p) => `${p.summary}${p.failures.length ? ' — ' + p.failures.slice(0, 3).join('; ') : ''}`).join(' · ')}
        </div>
      )}
    </div>
  );
}

function JobRow({ job, expanded, onToggle, onCancel, onBoard }: { job: DispatchJob; expanded: boolean; onToggle: () => void; onCancel: () => void; onBoard: () => void }) {
  const live = isLive(job.status);
  const res = job.result;
  const v = res?.verification ?? null;
  const vm = job.verdict.match(/(\d+) files? changed on disk/);
  const changedCount = res ? res.files_changed.length : vm ? parseInt(vm[1], 10) : 0;
  const vok = v && v.ran ? v.ok : /verification passed/.test(job.verdict);
  const vword = v && v.ran ? (v.ok ? t('verified') : v.inconclusive ? t('unverified') : t('verification failed')) : /verification passed/.test(job.verdict) ? t('verified') : /verification FAILED/.test(job.verdict) ? t('verification failed') : '';
  const progressNames = Object.keys(job.progress);
  const Chevron = expanded ? ChevronDown : ChevronRight;
  return (
    <div className="fs-wk__job" data-status={job.status} data-open={expanded || undefined} data-testid="wk-job">
      <div className="fs-wk__head">
        <button type="button" className="fs-wk__toggle" onClick={onToggle} aria-expanded={expanded} data-testid="wk-toggle">
          <Chevron size={14} aria-hidden="true" className="fs-wk__chev" />
          <span className="fs-wk__status" data-status={job.status}>{STATUS_WORD[job.status] ? t(STATUS_WORD[job.status]) : job.status}</span>
          <span className="fs-wk__title" title={job.verdict || job.title}>{job.title || 'Workers'}</span>
        </button>
        <span className="fs-wk__meta">
          {when(job.created)}
          {job.duration_s != null && ` · ${fmtDur(job.duration_s)}`}
          {changedCount > 0 && ` · ${tn(changedCount, '{n} file', '{n} files')}`}
          {vword && (
            <>
              {' · '}
              <span className="fs-wk__vword" data-ok={vok ? 'yes' : 'no'}>{vword}</span>
            </>
          )}
          {res?.totals.errors ? ` · ${tn(res.totals.errors, '{n} error', '{n} errors')}` : ''}
        </span>
        <span className="fs-wk__actions">
          {job.session_id && <Button size="sm" variant="ghost" icon={MessageSquare} label={t('Board')} onClick={onBoard} title={t('Open the Workers chat: the control board, steer / stop, the transcripts')} />}
          {live && <Button size="sm" variant="danger" icon={X} label={t('Cancel')} onClick={onCancel} />}
        </span>
      </div>
      {expanded && (
        <div className="fs-wk__body">
          {job.error && <div className="fs-wk__error">{job.error}</div>}
          {job.verdict && !live && <div className="fs-wk__verdict">{job.verdict}</div>}
          {live && (
            <>
              {job.phase && (
                <div className="fs-wk__muted">
                  {job.phase}
                  {job.ceiling_s ? ` · ${t('at most {time} more', { time: fmtDur(job.ceiling_s) })}` : ''}
                </div>
              )}
              <div className="fs-wk__progress">
                {progressNames.length === 0 && <span className="fs-wk__muted">{t('starting…')}</span>}
                {progressNames.map((n) => {
                  const p = job.progress[n] ?? {};
                  return (
                    <div key={n} className="fs-wk__line">
                      <span className="fs-wk__wname">{n}</span> {p.last_event ?? '…'}
                      {p.round != null && ` · ${t('round')} ${p.round}`}
                      {(p.last_tool || p.tool) && ` · ${p.last_tool || p.tool}`}
                      {p.elapsed_s != null && ` · ${Math.round(p.elapsed_s)} s`}
                      {p.stalled && (
                        <>
                          {' · '}
                          <b>{t('stalled')}</b>
                          {p.stall_reason ? ` (${p.stall_reason})` : ''}
                        </>
                      )}
                      {p.state && (
                        <span className="fs-wk__state" data-state={p.state} title={`${p.state.replace(/_/g, ' ')}${p.why ? ' — ' + p.why : ''} ${t('(reported, not killed)')}`}>
                          {p.state.replace(/_/g, ' ')}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
          {!live && res && (
            <>
              {res.changes && (
                <div className="fs-wk__changes">
                  <b>{t('Changed on disk')}</b> <span className="fs-wk__muted">({res.changes.source}{res.changes.truncated ? t(', list truncated') : ''})</span>:{' '}
                  {(['added', 'modified', 'deleted'] as const).filter((k) => res.changes![k].length).length === 0 && t('nothing')}
                  {(['added', 'modified', 'deleted'] as const)
                    .filter((k) => res.changes![k].length)
                    .map((k) => (
                      <span key={k} className="fs-wk__chg" data-kind={k}>
                        {k === 'added' ? t('added') : k === 'modified' ? t('modified') : t('deleted')}: {res.changes![k].map((f) => <code key={f}>{f}</code>)}
                      </span>
                    ))}
                  {res.claimed_only.length > 0 && (
                    <div className="fs-wk__claimed">
                      {t('Claimed by a worker but not changed')}: {res.claimed_only.map((f) => <code key={f}>{f}</code>)}
                    </div>
                  )}
                </div>
              )}
              {v && <VerificationBlock v={v} />}
              {res.proof && res.proof.verdict && (
                <div className="fs-wk__proof" data-tone={PROOF_TONE[res.proof.verdict] ?? 'warn'} title={res.proof.uncertainty.length ? `${t('why the confidence is not 1')} — ${res.proof.uncertainty.map((u) => `${u.kind}: ${u.detail}`).join(' · ')}` : t('nothing is left unaccounted for')}>
                  <b>{t('Proof')}: {res.proof.verdict}</b> <span className="fs-wk__muted">{t('confidence')} {String(res.proof.confidence)}</span>
                  {PROOF_WORD[res.proof.verdict] && <span className="fs-wk__muted"> — {t(PROOF_WORD[res.proof.verdict])}</span>}
                  {res.proof.uncertainty[0] && (
                    <div className="fs-wk__proof-why">
                      {res.proof.uncertainty[0].kind}: {res.proof.uncertainty[0].detail}
                      {res.proof.uncertainty.length > 1 && <span className="fs-wk__muted"> (+{res.proof.uncertainty.length - 1} {t('more')})</span>}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
          {res?.workers.map((w, i) => (
            <div key={i} className="fs-wk__worker">
              <div className="fs-wk__line">
                <span className="fs-wk__status" data-status={w.status}>{w.status}</span>
                <span className="fs-wk__wname">
                  {w.name}
                  {w.role !== 'worker' && <span className="fs-wk__muted"> ({w.role})</span>}
                </span>
                <span className="fs-wk__muted">
                  {tn(w.rounds, '{n} round', '{n} rounds')} · {tn(w.tool_calls, '{n} tool', '{n} tools')}{w.failed_calls ? ` (${t('{n} failed', { n: w.failed_calls })})` : ''} · {w.input_tokens}/{w.output_tokens} tok{w.stop_reason && w.stop_reason !== 'complete' ? ` · ${w.stop_reason}` : ''}
                </span>
              </div>
              {w.error && <div className="fs-wk__error">{w.error}</div>}
              {w.files_changed.length > 0 && <div className="fs-wk__files">{t('claims')}: {w.files_changed.map((f) => <code key={f}>{f}</code>)}</div>}
              {w.summary && <div className="fs-wk__summary">{w.summary}</div>}
            </div>
          ))}
          {res && res.lock_conflicts.length > 0 && <div className="fs-wk__muted">{t('Writes refused by the file locks')}: {res.lock_conflicts.join('; ')}</div>}
          {res && res.dropped_tasks > 0 && <div className="fs-wk__error">{t('{n} task(s) were not run (max 4 per job) — run them again.', { n: res.dropped_tasks })}</div>}
          <details className="fs-wk__tasks">
            <summary>
              {tn(job.tasks.length, '{n} task', '{n} tasks')} · {job.workspace || t('no folder')} · {job.model}
              {job.verify && job.verify !== 'auto' ? ` · ${t('verify')}: ${job.verify}` : ''}
            </summary>
            {job.tasks.map((task, i) => (
              <div key={i} className="fs-wk__task-row">
                <b>{i + 1}.</b> {task.instruction}
                {task.files.length > 0 && <span className="fs-wk__muted"> [{task.files.join(', ')}]</span>}
                {task.runner && <span className="fs-wk__muted"> · runner {task.runner}</span>}
              </div>
            ))}
          </details>
        </div>
      )}
    </div>
  );
}

export interface WorkersProps {
  /** A definition slug picked in Definiciones ("Usar en una tarea"). */
  agent?: string;
  /** A runner key picked in Runners. */
  runner?: string;
}

export function Workers({ agent: agentParam, runner: runnerParam }: WorkersProps) {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<DispatchJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [text, setText] = useState('');
  const [workspace, setWorkspace] = useState(() => {
    try {
      return localStorage.getItem(FOLDER_KEY) || localStorage.getItem('odysseus-workspace') || '';
    } catch {
      return '';
    }
  });
  const [parallel, setParallel] = useState(true);
  const [reviewer, setReviewer] = useState(false);
  const [model, setModel] = useState('');
  const [verify, setVerify] = useState('');
  const [fixRounds, setFixRounds] = useState(1);
  const [agent, setAgent] = useState(agentParam ?? '');
  const [runner, setRunner] = useState(runnerParam ?? '');
  const [modelHint, setModelHint] = useState(t('configured worker model'));
  const [verifierHint, setVerifierHint] = useState(t('auto-detect the test runner'));

  useEffect(() => {
    if (agentParam) setAgent(agentParam);
  }, [agentParam]);
  useEffect(() => {
    if (runnerParam) setRunner(runnerParam);
  }, [runnerParam]);

  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const streams = useRef(new Map<string, () => void>());
  const noStream = useRef(false);
  const pollTimer = useRef<number | null>(null);
  const refreshTimer = useRef<number | null>(null);
  const alive = useRef(true);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 3000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const rows = await listJobs(50);
      const want = rows.filter((j) => expandedRef.current.has(j.id) || isLive(j.status));
      const full = await Promise.all(want.map((j) => getJob(j.id).catch(() => j)));
      const byId = new Map(full.map((j) => [j.id, j]));
      if (!alive.current) return;
      setJobs(rows.map((j) => byId.get(j.id) ?? j));
      setError(null);
    } catch (e) {
      if (!alive.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setJobs((j) => j ?? []);
    }
  }, []);

  const scheduleRefresh = useCallback(
    (ms = 300) => {
      if (refreshTimer.current) return;
      refreshTimer.current = window.setTimeout(() => {
        refreshTimer.current = null;
        void refresh();
      }, ms);
    },
    [refresh],
  );

  useEffect(() => {
    alive.current = true;
    void refresh();
    dispatchConfig()
      .then((cfg) => {
        if (!alive.current) return;
        if (cfg.model) setModelHint(`${cfg.model}${cfg.server ? ' @ ' + cfg.server : ''}`);
        else if (cfg.error) setModelHint(cfg.error);
      })
      .catch(() => {});
    return () => {
      alive.current = false;
      for (const close of streams.current.values()) close();
      streams.current.clear();
      if (pollTimer.current) window.clearInterval(pollTimer.current);
      if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
    };
  }, [refresh]);

  /* The verifier Faustus would run in that folder, as the placeholder. */
  useEffect(() => {
    const ws = workspace.trim();
    if (!ws) {
      setVerifierHint(t('auto-detect the test runner'));
      return;
    }
    const timer = window.setTimeout(() => {
      dispatchConfig(ws)
        .then((cfg) => {
          const v = cfg.verifier;
          if (!v || !alive.current) return;
          setVerifierHint(v.error ? v.error : v.label ? `auto: ${v.label}` : t('no test runner found here — give a command'));
        })
        .catch(() => {});
    }, 500);
    return () => window.clearTimeout(timer);
  }, [workspace]);

  /* Streams for the live jobs; a poll while anything live is not streamed. */
  useEffect(() => {
    if (!jobs) return;
    const live = jobs.filter((j) => isLive(j.status));
    const ids = new Set(live.map((j) => j.id));
    for (const [id, close] of streams.current) {
      if (!ids.has(id)) {
        close();
        streams.current.delete(id);
      }
    }
    if (!noStream.current) {
      for (const j of live) {
        if (streams.current.has(j.id)) continue;
        const close = followJob(
          j.id,
          () => scheduleRefresh(),
          () => {
            streams.current.delete(j.id);
            scheduleRefresh(0);
          },
          () => {
            streams.current.delete(j.id);
            noStream.current = true;
            scheduleRefresh(0);
          },
        );
        streams.current.set(j.id, close);
      }
    }
    const needPoll = live.length > 0 && (noStream.current || live.some((j) => !streams.current.has(j.id)));
    if (needPoll && !pollTimer.current) pollTimer.current = window.setInterval(() => void refresh(), 3000);
    if (!needPoll && pollTimer.current) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, [jobs, refresh, scheduleRefresh]);

  const tasks = useMemo(() => parseTasks(text), [text]);

  const run = async () => {
    if (!tasks.length) return;
    const ws = workspace.trim();
    if (!ws) {
      flash(t('Say which folder the workers may touch'));
      return;
    }
    const body: DispatchRequest = { tasks, workspace: ws, parallel, reviewer, fix_rounds: Math.max(0, Math.min(4, fixRounds || 0)) };
    if (verify.trim()) body.verify = verify.trim();
    if (model.trim()) body.model = model.trim();
    if (agent.trim()) body.agent = agent.trim();
    if (runner.trim()) body.runner = runner.trim();
    setBusy(true);
    try {
      const job = await startJob(body);
      setExpanded((s) => new Set(s).add(job.id));
      setText('');
      try {
        localStorage.setItem(FOLDER_KEY, ws);
      } catch {
        /* private mode */
      }
      flash(tn(tasks.length, '{n} worker started', '{n} workers started'));
      await refresh();
    } catch (e) {
      flash(`${t('Could not start')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (id: string) => {
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    scheduleRefresh(0);
  };

  const cancel = async (id: string) => {
    try {
      await cancelJob(id);
      await refresh();
    } catch (e) {
      flash(`${t('Could not cancel')}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div className="fs-wk" data-testid="workers">
      <form
        className="fs-wk__form"
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <textarea
          className="fs-wk__task"
          rows={4}
          value={text}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
              e.preventDefault();
              void run();
            }
          }}
          placeholder={t('What should the workers do? Say what "done" means, e.g. "In cart.py add apply_discount(total, pct) with validation and a test in tests/test_cart.py; pytest -q must pass". Separate several tasks with a blank line or a list (- / 1.) — one worker each.')}
          data-testid="wk-task"
        />
        <div className="fs-wk__row">
          <label className="fs-wk__field">
            <span>{t('Folder')}</span>
            <input type="text" className="fs-field" value={workspace} placeholder="D:\projects\app" required disabled={busy} onChange={(e) => setWorkspace(e.target.value)} data-testid="wk-workspace" />
          </label>
          <label className="fs-switch" title={t('Independent tasks run at the same time (one worker each); off = one after another (a later task may edit what an earlier one wrote)')}>
            <input type="checkbox" checked={parallel} onChange={(e) => setParallel(e.target.checked)} />
            <span>{t('in parallel')}</span>
          </label>
          <label className="fs-switch" title={t('Add a reviewer worker after the others')}>
            <input type="checkbox" checked={reviewer} onChange={(e) => setReviewer(e.target.checked)} />
            <span>{t('reviewer')}</span>
          </label>
          <label className="fs-wk__field fs-wk__field--sm">
            <span>{t('Model')}</span>
            <input type="text" className="fs-field" value={model} placeholder={modelHint} onChange={(e) => setModel(e.target.value)} />
          </label>
          <Button type="submit" variant="primary" icon={Play} label={busy ? t('Starting…') : t('Run')} loading={busy} disabled={!tasks.length} testId="wk-run" />
        </div>
        <div className="fs-wk__row">
          <label className="fs-wk__field" title={t('Run by Faustus in the folder after the workers — their own claims are never the proof. Empty = the project\'s test runner is detected (pytest, npm test, cargo, go, make test)')}>
            <span>{t('Verify with')}</span>
            <input type="text" className="fs-field" value={verify} placeholder={verifierHint} onChange={(e) => setVerify(e.target.value)} />
          </label>
          <label className="fs-wk__field fs-wk__field--xs" title={t('When the verification fails: at most how many times one fixer worker gets the failure output before Faustus gives up. Faustus stops earlier by itself when the rounds stop changing anything.')}>
            <span>{t('Fix rounds')}</span>
            <input type="number" className="fs-field" min={0} max={4} value={fixRounds} onChange={(e) => setFixRounds(parseInt(e.target.value, 10) || 0)} />
          </label>
          <label className="fs-wk__field fs-wk__field--sm" title={t('Slug of an agent definition (Definitions tab): the worker starts under its rules')}>
            <span>{t('Agent')}</span>
            <input type="text" className="fs-field" value={agent} placeholder={t('definition (optional)')} onChange={(e) => setAgent(e.target.value)} data-testid="wk-agent" />
          </label>
          <label className="fs-wk__field fs-wk__field--sm" title={t('Key of an external agent runner (Runners tab): that agent does the work instead of the built-in worker')}>
            <span>Runner</span>
            <input type="text" className="fs-field" value={runner} placeholder={t('built-in')} onChange={(e) => setRunner(e.target.value)} data-testid="wk-runner" />
          </label>
          <span className="fs-wk__muted fs-wk__count">
            {tn(Math.max(1, tasks.length), '{n} worker', '{n} workers')}
            {tasks.length > 1 ? t(' (one per task)') : ''}
          </span>
        </div>
        <p className="fs-wk__hint">
          {t('A blank line or a list marker starts a new task = one worker (max 4). The workers are confined to the folder; Faustus checkpoints it before, diffs it after, runs the verification itself and marks the job')} <em>{t('partial')}</em> {t('when anything did not finish. The job gets its own')} <em>Workers</em> {t('chat with the control board. Ctrl+Enter runs.')}
        </p>
      </form>

      {error && <div className="fs-wk__error">No he podido leer los trabajos: {error}</div>}
      {jobs === null ? (
        <div className="fs-wk__list">
          <Skeleton label={t('Loading the jobs')} height="44px" count={2} radius="panel" />
        </div>
      ) : jobs.length === 0 ? (
        <EmptyState title={t('No jobs yet')} body={t('Describe a task above and press Run: the workers do it on the local models, Faustus checks what changed and runs the tests, and you read the verdict.')} />
      ) : (
        <div className="fs-wk__list">
          {jobs.map((j) => (
            <JobRow key={j.id} job={j} expanded={expanded.has(j.id)} onToggle={() => toggle(j.id)} onCancel={() => void cancel(j.id)} onBoard={() => navigate(`/studio?s=${encodeURIComponent(j.session_id)}`)} />
          ))}
        </div>
      )}
      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
