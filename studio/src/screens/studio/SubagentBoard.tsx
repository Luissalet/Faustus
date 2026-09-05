import { ExternalLink, Pencil, RotateCcw, Square } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Button, StatusBadge, type RunStatus } from '../../components';
import { steerWorker, stopWorker } from '../../adapters/agents';
import type { DelegationTask } from '../../adapters/chat';
import { workerLive, type Worker } from './model';
import { t, tn } from '../../i18n';

/**
 * One card per sub-agent of a delegate_agents call: what it is doing right
 * now, how long, how many tools, what it changed, and the two things you
 * can do to a running one — stop it alone, or steer it. Same information
 * as the legacy board (agentHarnessUI.js, v3), laid out as cards.
 */

export interface SubagentBoardProps {
  workers: Worker[];
  /** The parent turn is still streaming: Stop / Steer make sense, Re-run does not. */
  live: boolean;
  onRerun: (task: DelegationTask) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
}

const NO_SIGNAL_MS = 20000;

export function elapsedSeconds(w: Worker, now: number): number {
  if (w.startedAt && w.endedAt) return Math.max(0, w.endedAt - w.startedAt);
  if (!workerLive(w)) {
    if (w.durationS !== null) return w.durationS;
    if (w.startedAt && w.endedLocal) return Math.max(0, w.endedLocal / 1000 - w.startedAt);
    if (w.startedLocal && w.endedLocal) return Math.max(0, (w.endedLocal - w.startedLocal) / 1000);
  }
  if (w.tickElapsed !== null && w.tickAt) return w.tickElapsed + Math.max(0, now - w.tickAt) / 1000;
  if (w.startedAt) return Math.max(0, now / 1000 - w.startedAt);
  return Math.max(0, (now - (w.startedLocal ?? w.firstSeen)) / 1000);
}

export function formatDuration(seconds: number): string {
  const v = Math.max(0, Math.round(seconds));
  if (v < 60) return `${v} s`;
  if (v < 3600) return `${Math.floor(v / 60)} min ${String(v % 60).padStart(2, '0')} s`;
  return `${Math.floor(v / 3600)} h ${String(Math.floor((v % 3600) / 60)).padStart(2, '0')} min`;
}

const ACTIVITY: [RegExp, string][] = [
  [/^(read_file|ls|list_files|glob|grep|search_files|find_files|read_plan|project_context|repo_map|code_refs)$/, 'Reading files'],
  [/^(edit_file|write_file|apply_patch|replace_across_files|create_file|multi_edit)$/, 'Editing files'],
  [/^(bash|python|run_tests|shell|execute|subprocess)$/, 'Running a command'],
  [/^(web_search|web_fetch|fetch_url|mcp__builtin_browser__|browser_)/, 'Browsing'],
  [/^desktop_/, 'Using the desktop'],
  [/^delegate_agents$/, 'Delegating'],
  [/^(ask_user|update_plan|todowrite|save_todos)$/, 'Waiting for you'],
  [/^(manage_skills|memory|remember|recall)/, 'Using the memory'],
];

export function activity(w: Worker): string {
  if (w.status === 'queued') return t('Queued');
  if (!workerLive(w)) return '';
  if (w.stalled) return /loop/i.test(w.stallReason) ? t('Looping') : t('Stalled');
  if (w.toolInFlight && w.lastTool) {
    for (const [re, label] of ACTIVITY) if (re.test(w.lastTool)) return t(label);
    return t('Using {tool}', { tool: w.lastTool });
  }
  return t('Thinking');
}

function pill(w: Worker, now: number): { status: RunStatus; label: string } {
  if (w.status === 'queued') return { status: 'queued', label: t('queued') };
  if (w.status === 'running') {
    if (w.stalled) {
      if (/loop/i.test(w.stallReason)) return { status: 'waiting', label: t('loop') };
      if (w.idleS !== null) return { status: 'waiting', label: t('idle {n} s', { n: Math.round(w.idleS + Math.max(0, now - (w.stallAt ?? now)) / 1000) }) };
      return { status: 'waiting', label: w.stallReason || t('stalled') };
    }
    if (w.sawTick && now - w.lastEventAt > NO_SIGNAL_MS) return { status: 'waiting', label: t('no signal {n} s', { n: Math.round((now - w.lastEventAt) / 1000) }) };
    return { status: 'running', label: t('running') };
  }
  if (w.status === 'done') return { status: 'succeeded', label: t('done') };
  if (w.status === 'stopped') return { status: 'cancelled', label: t('stalled') };
  if (w.status === 'failed') return { status: 'failed', label: 'fallido' };
  return { status: 'paused', label: w.stopReason || 'parcial' };
}

const fmtTok = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v));

function WorkerCard({ w, live, now, onRerun, onNotice }: { w: Worker; live: boolean; now: number; onRerun: SubagentBoardProps['onRerun']; onNotice: SubagentBoardProps['onNotice'] }) {
  const [form, setForm] = useState<'steer' | 'rerun' | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [stopState, setStopState] = useState<'idle' | 'stopping' | 'stopped' | 'gone'>('idle');
  const alive = workerLive(w);
  const reviewer = w.role === 'reviewer';
  const name = reviewer ? `${t('Reviewer')} · ${w.name || 'reviewer'}` : `${(w.index ?? 0) + 1}. ${w.name || 'worker'}`;
  const p = pill(w, now);
  const act = activity(w);
  const instruction = w.instruction || w.instructionFull;

  let last = '';
  if (w.error) last = `✗ ${w.error}`;
  else if (!alive && w.finalText) last = w.finalText;
  else if (w.lastTool) {
    const mark = w.toolInFlight ? '▶' : w.lastToolOk === null ? '·' : w.lastToolOk ? '✓' : '✗';
    const cmd = w.toolInFlight ? w.lastCmd : w.lastCmd || w.lastOut;
    const el = w.toolInFlight && w.toolElapsed !== null ? ` (${formatDuration(w.toolElapsed)})` : '';
    last = `${mark} ${w.lastTool}${el} ${cmd}`.trim();
  } else if (w.note) last = w.note;

  const stop = async () => {
    setStopState('stopping');
    try {
      const stopped = await stopWorker(w.sessionId);
      setStopState(stopped ? 'stopped' : 'gone');
    } catch {
      setStopState('idle');
      onNotice(t('Could not stop the worker.'), 'danger');
    }
  };

  const submit = async () => {
    if (form === 'steer') {
      const msg = text.trim();
      if (!msg) return;
      setBusy(true);
      try {
        const ok = await steerWorker(w.sessionId, msg);
        // The server echoes the steer as a `steer` event, which paints the line.
        onNotice(ok ? t('Message sent to the worker; it reads it before its next round.') : t('That worker is no longer running.'), ok ? 'info' : 'warning');
      } catch {
        onNotice(t('Could not send the message.'), 'danger');
      } finally {
        setBusy(false);
        setForm(null);
        setText('');
      }
    } else if (form === 'rerun') {
      onRerun({ name: w.name, instruction: w.instructionFull || w.instruction, files: w.files, model: text.trim() || undefined });
      setForm(null);
      setText('');
    }
  };

  return (
    <article className="fs-sa" data-status={w.status} data-live={alive || undefined} data-stalled={w.stalled || undefined}>
      <header className="fs-sa__head">
        <strong className="fs-sa__name">{name}</strong>
        <span className="fs-sa__role" data-role={w.role}>{w.role}</span>
        {w.model && <code className="fs-sa__model" title={t('model')}>{w.model}</code>}
        <StatusBadge status={p.status} label={p.label} />
        {alive && act && <span className="fs-sa__activity">{act}</span>}
      </header>
      {instruction && <p className="fs-sa__instruction" title={w.instructionFull || w.instruction}>{instruction}</p>}
      <p className="fs-sa__stats">
        <span title={t('time')}>{formatDuration(elapsedSeconds(w, now))}</span>
        {(w.round !== null || w.rounds !== null) && (
          <span title={t('round')}>
            r{alive ? w.round : w.rounds ?? w.round}
            {w.maxRounds ? `/${w.maxRounds}` : ''}
          </span>
        )}
        <span>
          {w.toolCalls} {w.toolCalls === 1 ? 'herramienta' : 'herramientas'}
          {w.failedCalls ? ` (${w.failedCalls} fallidas)` : ''}
        </span>
        {(w.inTok !== null || w.outTok !== null) && <span title="tokens">{fmtTok(w.inTok ?? 0)} in · {fmtTok(w.outTok ?? 0)} out</span>}
        {!alive && w.mutations.length > 0 && <span>{tn(w.mutations.length, '{n} file changed', '{n} files changed')}</span>}
        {!alive && w.mutations.length === 0 && w.status !== 'queued' && <span className="fs-sa__muted">{t('no file changes')}</span>}
      </p>
      {last && <p className="fs-sa__last" title={last}>{last}</p>}
      {alive && w.tail && <pre className="fs-sa__tail">{w.tail}</pre>}
      {w.note && last !== w.note && <p className="fs-sa__muted">{w.note}</p>}
      {w.files.length > 0 && (
        <p className="fs-sa__files fs-sa__muted">
          posee {w.files.map((f) => <code key={f} title={f}>{f.split(/[\\/]/).pop()}</code>)}
        </p>
      )}
      {!alive && w.mutations.length > 0 && (
        <p className="fs-sa__files">
          {w.mutations.slice(0, 40).map((f) => <code key={f} title={f}>{f}</code>)}
        </p>
      )}
      {(w.steers.length > 0 || w.supervisor.length > 0) && (
        <div className="fs-sa__lines">
          {w.steers.map((st, i) => (
            <p key={`s${i}`}>→ dirigido{st.source && st.source !== 'user' ? ` (${st.source})` : ''}: {st.text}</p>
          ))}
          {w.supervisor.map((sv, i) => (
            <p key={`v${i}`}>{t('supervisor')}: {sv.action === 'nudge' ? t('nudge') : sv.action === 'stop' ? t('stopped') : sv.action || t('acted')}{sv.reason ? ` — ${sv.reason}` : ''}</p>
          ))}
        </div>
      )}
      <footer className="fs-sa__foot">
        {live && alive && w.sessionId && (
          <>
            <Button
              size="sm"
              variant="danger"
              icon={Square}
              label={stopState === 'stopping' ? t('Stopping…') : stopState === 'stopped' ? t('Stalled') : stopState === 'gone' ? t('Was not running') : t('Stop')}
              disabled={stopState !== 'idle'}
              onClick={() => void stop()}
            />
            <Button size="sm" icon={Pencil} label={t('Steer…')} onClick={() => setForm(form === 'steer' ? null : 'steer')} />
          </>
        )}
        {w.sessionId && (
          <Link className="fs-btn" data-size="sm" to={`/studio?s=${encodeURIComponent(w.sessionId)}`} title={w.sessionId}>
            <ExternalLink size={13} aria-hidden="true" /> <span>{t('Open its chat')}</span>
          </Link>
        )}
        {!alive && w.status !== 'done' && !reviewer && (w.instructionFull || w.instruction) && (
          <Button
            size="sm"
            icon={RotateCcw}
            label={t('Repeat…')}
            disabled={live}
            onClick={() => {
              setText(w.model);
              setForm(form === 'rerun' ? null : 'rerun');
            }}
          />
        )}
      </footer>
      {form && (
        <form
          className="fs-sa__form"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <input
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setForm(null);
            }}
            placeholder={form === 'steer' ? t('Message for this worker (it reads it before its next round)') : t('Model for this worker (empty = the chat\'s)')}
            aria-label={form === 'steer' ? t('Message for the worker') : t('Model')}
          />
          <Button size="sm" variant="primary" type="submit" label={form === 'steer' ? t('Send') : t('Repeat')} loading={busy} />
          <Button size="sm" label={t('Cancel')} onClick={() => setForm(null)} />
        </form>
      )}
    </article>
  );
}

export default function SubagentBoard({ workers, live, onRerun, onNotice }: SubagentBoardProps) {
  const anyLive = workers.some(workerLive);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!anyLive) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [anyLive]);

  const done = workers.filter((w) => !workerLive(w)).length;
  const running = workers.filter((w) => w.status === 'running').length;
  const queued = workers.filter((w) => w.status === 'queued').length;
  const stalled = workers.filter((w) => w.status === 'running' && w.stalled).length;
  const bits = [running ? `${running} en marcha` : '', queued ? `${queued} en cola` : '', stalled ? `${stalled} parados` : ''].filter(Boolean);

  return (
    <section className="fs-sa-board" data-testid="subagent-board" data-open={anyLive || undefined}>
      <header className="fs-sa-board__head">
        <strong>Sub-agentes</strong>
        <span className="fs-sa-board__count">{done}/{workers.length}</span>
        {bits.length > 0 && <span className="fs-sa__muted" data-stalled={stalled > 0 || undefined}> · {bits.join(' · ')}</span>}
      </header>
      <div className="fs-sa-board__cards">
        {workers.map((w) => (
          <WorkerCard key={`${w.delegation}|${w.id}`} w={w} live={live} now={now} onRerun={onRerun} onNotice={onNotice} />
        ))}
      </div>
    </section>
  );
}
