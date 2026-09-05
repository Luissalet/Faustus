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
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

const STATUS_WORD: Record<string, string> = {
  queued: 'en cola',
  running: 'en marcha',
  verifying: 'verificando',
  done: 'hecho',
  partial: 'parcial',
  error: 'error',
  cancelling: 'cancelando',
  cancelled: 'cancelado',
  interrupted: 'interrumpido',
};

function VerificationBlock({ v }: { v: Verification }) {
  if (!v.ran) return <div className="fs-wk__verify" data-state="none">Sin verificar — {v.summary}</div>;
  const state = v.ok ? 'passed' : v.inconclusive ? 'inconclusive' : 'failed';
  const word = v.ok ? 'pasó' : v.inconclusive ? 'no concluyente' : 'falló';
  const pre = new Set(v.pre_existing);
  return (
    <div className="fs-wk__verify" data-state={state}>
      <b>Verificación: {word}</b> — {v.summary}
      {v.command && <code className="fs-wk__code">{v.command}</code>}
      {v.attempts > 1 && <span className="fs-wk__muted"> · {v.attempts} intentos</span>}
      {v.failures.length > 0 && (
        <ul className="fs-wk__fails">
          {v.failures.map((f, i) => (
            <li key={i}>
              {f}
              {pre.has(f) && <span className="fs-wk__muted"> (ya fallaba antes del trabajo)</span>}
            </li>
          ))}
        </ul>
      )}
      {!v.ok && v.output_tail && (
        <details className="fs-wk__tail">
          <summary>salida</summary>
          <pre>{v.output_tail.slice(-1500)}</pre>
        </details>
      )}
      {v.previous.length > 0 && (
        <div className="fs-wk__muted">
          Antes de la{v.previous.length > 1 ? 's rondas' : ' ronda'} de arreglo: {v.previous.map((p) => `${p.summary}${p.failures.length ? ' — ' + p.failures.slice(0, 3).join('; ') : ''}`).join(' · ')}
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
  const vword = v && v.ran ? (v.ok ? 'verificado' : v.inconclusive ? 'sin verificar' : 'verificación fallida') : /verification passed/.test(job.verdict) ? 'verificado' : /verification FAILED/.test(job.verdict) ? 'verificación fallida' : '';
  const progressNames = Object.keys(job.progress);
  const Chevron = expanded ? ChevronDown : ChevronRight;
  return (
    <div className="fs-wk__job" data-status={job.status} data-open={expanded || undefined} data-testid="wk-job">
      <div className="fs-wk__head">
        <button type="button" className="fs-wk__toggle" onClick={onToggle} aria-expanded={expanded} data-testid="wk-toggle">
          <Chevron size={14} aria-hidden="true" className="fs-wk__chev" />
          <span className="fs-wk__status" data-status={job.status}>{STATUS_WORD[job.status] ?? job.status}</span>
          <span className="fs-wk__title" title={job.verdict || job.title}>{job.title || 'Workers'}</span>
        </button>
        <span className="fs-wk__meta">
          {when(job.created)}
          {job.duration_s != null && ` · ${fmtDur(job.duration_s)}`}
          {changedCount > 0 && ` · ${changedCount} fichero${changedCount > 1 ? 's' : ''}`}
          {vword && (
            <>
              {' · '}
              <span className="fs-wk__vword" data-ok={vok ? 'yes' : 'no'}>{vword}</span>
            </>
          )}
          {res?.totals.errors ? ` · ${res.totals.errors} error${res.totals.errors > 1 ? 'es' : ''}` : ''}
        </span>
        <span className="fs-wk__actions">
          {job.session_id && <Button size="sm" variant="ghost" icon={MessageSquare} label="Tablero" onClick={onBoard} title="Abrir el chat de Workers: el tablero, dirigir / parar, las transcripciones" />}
          {live && <Button size="sm" variant="danger" icon={X} label="Cancelar" onClick={onCancel} />}
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
                  {job.ceiling_s ? ` · como mucho ${fmtDur(job.ceiling_s)} más` : ''}
                </div>
              )}
              <div className="fs-wk__progress">
                {progressNames.length === 0 && <span className="fs-wk__muted">arrancando…</span>}
                {progressNames.map((n) => {
                  const p = job.progress[n] ?? {};
                  return (
                    <div key={n} className="fs-wk__line">
                      <span className="fs-wk__wname">{n}</span> {p.last_event ?? '…'}
                      {p.round != null && ` · ronda ${p.round}`}
                      {(p.last_tool || p.tool) && ` · ${p.last_tool || p.tool}`}
                      {p.elapsed_s != null && ` · ${Math.round(p.elapsed_s)} s`}
                      {p.stalled && (
                        <>
                          {' · '}
                          <b>atascado</b>
                          {p.stall_reason ? ` (${p.stall_reason})` : ''}
                        </>
                      )}
                      {p.state && (
                        <span className="fs-wk__state" data-state={p.state} title={`${p.state.replace(/_/g, ' ')}${p.why ? ' — ' + p.why : ''} (se informa, no se mata)`}>
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
                  <b>Cambiado en disco</b> <span className="fs-wk__muted">({res.changes.source}{res.changes.truncated ? ', lista truncada' : ''})</span>:{' '}
                  {(['added', 'modified', 'deleted'] as const).filter((k) => res.changes![k].length).length === 0 && 'nada'}
                  {(['added', 'modified', 'deleted'] as const)
                    .filter((k) => res.changes![k].length)
                    .map((k) => (
                      <span key={k} className="fs-wk__chg" data-kind={k}>
                        {k === 'added' ? 'añadidos' : k === 'modified' ? 'modificados' : 'borrados'}: {res.changes![k].map((f) => <code key={f}>{f}</code>)}
                      </span>
                    ))}
                  {res.claimed_only.length > 0 && (
                    <div className="fs-wk__claimed">
                      Reclamado por un worker pero sin cambiar: {res.claimed_only.map((f) => <code key={f}>{f}</code>)}
                    </div>
                  )}
                </div>
              )}
              {v && <VerificationBlock v={v} />}
              {res.proof && res.proof.verdict && (
                <div className="fs-wk__proof" data-tone={PROOF_TONE[res.proof.verdict] ?? 'warn'} title={res.proof.uncertainty.length ? `por qué la confianza no es 1 — ${res.proof.uncertainty.map((u) => `${u.kind}: ${u.detail}`).join(' · ')}` : 'no queda nada sin justificar'}>
                  <b>Prueba: {res.proof.verdict}</b> <span className="fs-wk__muted">confianza {String(res.proof.confidence)}</span>
                  {PROOF_WORD[res.proof.verdict] && <span className="fs-wk__muted"> — {PROOF_WORD[res.proof.verdict]}</span>}
                  {res.proof.uncertainty[0] && (
                    <div className="fs-wk__proof-why">
                      {res.proof.uncertainty[0].kind}: {res.proof.uncertainty[0].detail}
                      {res.proof.uncertainty.length > 1 && <span className="fs-wk__muted"> (+{res.proof.uncertainty.length - 1} más)</span>}
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
                  {w.rounds} rondas · {w.tool_calls} herramientas{w.failed_calls ? ` (${w.failed_calls} fallidas)` : ''} · {w.input_tokens}/{w.output_tokens} tok{w.stop_reason && w.stop_reason !== 'complete' ? ` · ${w.stop_reason}` : ''}
                </span>
              </div>
              {w.error && <div className="fs-wk__error">{w.error}</div>}
              {w.files_changed.length > 0 && <div className="fs-wk__files">reclama: {w.files_changed.map((f) => <code key={f}>{f}</code>)}</div>}
              {w.summary && <div className="fs-wk__summary">{w.summary}</div>}
            </div>
          ))}
          {res && res.lock_conflicts.length > 0 && <div className="fs-wk__muted">Escrituras rechazadas por los bloqueos de fichero: {res.lock_conflicts.join('; ')}</div>}
          {res && res.dropped_tasks > 0 && <div className="fs-wk__error">{res.dropped_tasks} tarea(s) no corrieron (máximo 4 por trabajo) — lánzalas de nuevo.</div>}
          <details className="fs-wk__tasks">
            <summary>
              {job.tasks.length} tarea{job.tasks.length === 1 ? '' : 's'} · {job.workspace || 'sin carpeta'} · {job.model}
              {job.verify && job.verify !== 'auto' ? ` · verificar: ${job.verify}` : ''}
            </summary>
            {job.tasks.map((t, i) => (
              <div key={i} className="fs-wk__task-row">
                <b>{i + 1}.</b> {t.instruction}
                {t.files.length > 0 && <span className="fs-wk__muted"> [{t.files.join(', ')}]</span>}
                {t.runner && <span className="fs-wk__muted"> · runner {t.runner}</span>}
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
  const [modelHint, setModelHint] = useState('modelo de worker configurado');
  const [verifierHint, setVerifierHint] = useState('detectar el runner de tests');

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
      setVerifierHint('detectar el runner de tests');
      return;
    }
    const t = window.setTimeout(() => {
      dispatchConfig(ws)
        .then((cfg) => {
          const v = cfg.verifier;
          if (!v || !alive.current) return;
          setVerifierHint(v.error ? v.error : v.label ? `auto: ${v.label}` : 'no hay runner de tests aquí — da un comando');
        })
        .catch(() => {});
    }, 500);
    return () => window.clearTimeout(t);
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
      flash('Di qué carpeta pueden tocar los workers');
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
      flash(`${tasks.length} worker${tasks.length > 1 ? 's' : ''} en marcha`);
      await refresh();
    } catch (e) {
      flash(`No he podido arrancar: ${e instanceof Error ? e.message : String(e)}`);
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
      flash(`No he podido cancelar: ${e instanceof Error ? e.message : String(e)}`);
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
          placeholder="¿Qué tienen que hacer los workers? Di qué significa «hecho», p. ej. «En cart.py añade apply_discount(total, pct) con validación y un test en tests/test_cart.py; pytest -q tiene que pasar». Separa varias tareas con una línea en blanco o una lista (- / 1.) — un worker por tarea."
          data-testid="wk-task"
        />
        <div className="fs-wk__row">
          <label className="fs-wk__field">
            <span>Carpeta</span>
            <input type="text" className="fs-field" value={workspace} placeholder="D:\proyectos\app" required disabled={busy} onChange={(e) => setWorkspace(e.target.value)} data-testid="wk-workspace" />
          </label>
          <label className="fs-switch" title="Las tareas independientes corren a la vez (un worker cada una); apagado = una tras otra (una tarea posterior puede editar lo que escribió una anterior)">
            <input type="checkbox" checked={parallel} onChange={(e) => setParallel(e.target.checked)} />
            <span>en paralelo</span>
          </label>
          <label className="fs-switch" title="Añade un worker revisor después de los demás">
            <input type="checkbox" checked={reviewer} onChange={(e) => setReviewer(e.target.checked)} />
            <span>revisor</span>
          </label>
          <label className="fs-wk__field fs-wk__field--sm">
            <span>Modelo</span>
            <input type="text" className="fs-field" value={model} placeholder={modelHint} onChange={(e) => setModel(e.target.value)} />
          </label>
          <Button type="submit" variant="primary" icon={Play} label={busy ? 'Arrancando…' : 'Lanzar'} loading={busy} disabled={!tasks.length} testId="wk-run" />
        </div>
        <div className="fs-wk__row">
          <label className="fs-wk__field" title="Lo corre Faustus en la carpeta después de los workers — sus propias afirmaciones nunca son la prueba. Vacío = se detecta el runner de tests del proyecto (pytest, npm test, cargo, go, make test)">
            <span>Verificar con</span>
            <input type="text" className="fs-field" value={verify} placeholder={verifierHint} onChange={(e) => setVerify(e.target.value)} />
          </label>
          <label className="fs-wk__field fs-wk__field--xs" title="Cuando la verificación falla: como mucho cuántas veces un worker arreglador recibe la salida del fallo antes de que Faustus se rinda. Faustus para antes por sí solo cuando las rondas dejan de cambiar nada.">
            <span>Rondas de arreglo</span>
            <input type="number" className="fs-field" min={0} max={4} value={fixRounds} onChange={(e) => setFixRounds(parseInt(e.target.value, 10) || 0)} />
          </label>
          <label className="fs-wk__field fs-wk__field--sm" title="Slug de una definición de agente (pestaña Definiciones): el worker arranca bajo sus reglas">
            <span>Agente</span>
            <input type="text" className="fs-field" value={agent} placeholder="definición (opcional)" onChange={(e) => setAgent(e.target.value)} data-testid="wk-agent" />
          </label>
          <label className="fs-wk__field fs-wk__field--sm" title="Clave de un agent runner externo (pestaña Runners): ese agente hace el trabajo en vez del worker integrado">
            <span>Runner</span>
            <input type="text" className="fs-field" value={runner} placeholder="integrado" onChange={(e) => setRunner(e.target.value)} data-testid="wk-runner" />
          </label>
          <span className="fs-wk__muted fs-wk__count">
            {Math.max(1, tasks.length)} worker{tasks.length === 1 || tasks.length === 0 ? '' : 's'}
            {tasks.length > 1 ? ' (uno por tarea)' : ''}
          </span>
        </div>
        <p className="fs-wk__hint">
          Una línea en blanco o una marca de lista empieza otra tarea = otro worker (máximo 4). Los workers se quedan dentro de la carpeta; Faustus la fija antes, la compara después, corre la verificación él mismo y marca el trabajo <em>parcial</em> cuando algo no terminó. El trabajo tiene su propio chat <em>Workers</em> con el tablero. Ctrl+Intro lanza.
        </p>
      </form>

      {error && <div className="fs-wk__error">No he podido leer los trabajos: {error}</div>}
      {jobs === null ? (
        <div className="fs-wk__list">
          <Skeleton label="Cargando los trabajos" height="44px" count={2} radius="panel" />
        </div>
      ) : jobs.length === 0 ? (
        <EmptyState title="Todavía no hay trabajos" body="Describe una tarea arriba y pulsa Lanzar: los workers la hacen con los modelos locales, Faustus comprueba qué cambió y corre las pruebas, y tú lees el veredicto." />
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
