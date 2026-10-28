import { AlertTriangle, Check, FlaskConical, RotateCcw, ShieldCheck, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  approveHarnessProposal,
  diffLines,
  harnessLog,
  harnessStatus,
  listHarnessProposals,
  proposeForSession,
  rejectHarnessProposal,
  setHarnessMode,
  startProbe,
  undoHarnessProposal,
  type HarnessAxis,
  type HarnessLogEntry,
  type HarnessProposal,
  type HarnessStatusInfo,
} from '../../adapters/harness_proposals';
import { t, tn } from '../../i18n';
import { LessonsCard, ProbesCard } from './harness_probes';
import '../settings.css';

/**
 * Harness refinement (`src/harness_refinement`). After a task the assistant
 * may suggest the smallest edit to its own harness state: the project
 * instructions, one skill, one memory entry or one subagent spec. The base
 * system prompt is never a target. With the probe on, each candidate is tried
 * on a repeat of the task and the winner is applied by itself; with auto-apply
 * off it waits for approval. An applied edit can always be undone, restoring
 * the exact previous content.
 */

function axisLabel(axis: HarnessAxis): string {
  if (axis === 'prompt_layer') return t('Project instructions');
  if (axis === 'skill') return t('Skill');
  if (axis === 'memory') return t('Memory');
  return t('Subagent spec');
}

function opLabel(op: string): string {
  if (op === 'create') return t('Create');
  if (op === 'delete') return t('Delete');
  return t('Update');
}

function statusLabel(status: string): string {
  if (status === 'pending') return t('Pending');
  if (status === 'applied') return t('Applied');
  if (status === 'rejected') return t('Rejected');
  return t('Undone');
}

function when(ts: number | null | undefined): string {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return '';
  }
}

function Diff({ diff }: { diff: string }) {
  const lines = diffLines(diff);
  if (!lines.length) return <p className="fs-set__help">{t('No textual change.')}</p>;
  return (
    <pre className="fs-hp__diff" data-testid="harness-diff">
      {lines.map((l, i) => (
        <span key={i} className="fs-hp__line" data-kind={l.kind}>
          {l.text}
          {'\n'}
        </span>
      ))}
    </pre>
  );
}

function ProposalCard({ p, busy, onApprove, onReject, onUndo }: { p: HarnessProposal; busy: string; onApprove: () => void; onReject: () => void; onUndo: () => void }) {
  const isUndo = Boolean(p.undo_of);
  return (
    <li className="fs-hp__card" data-testid={`harness-proposal-${p.id}`} data-status={p.status}>
      <div className="fs-hp__head">
        <span className="fs-chip" data-on>{axisLabel(p.axis)}</span>
        <span className="fs-chip">{opLabel(p.op)}</span>
        <code className="fs-hp__target">{p.target}</code>
        <span className="fs-hp__status" data-status={p.status}>{isUndo ? t('Undo') : statusLabel(p.status)}</span>
      </div>
      {p.rationale && <p className="fs-hp__why">{p.rationale}</p>}
      {p.probe_id && (
        <p className="fs-set__help" data-testid={`harness-probe-note-${p.id}`}>
          <FlaskConical size={12} aria-hidden="true" />{' '}
          {p.probe_result === 'discarded' ? t('Discarded after trying it') : p.applied_by === 'harness-probe' ? t('Applied by itself after trying it') : t('Won when tried; waiting for you')}
          {p.probe_reason ? ` · ${p.probe_reason}` : ''}
        </p>
      )}
      {p.probe_evidence && p.probe_evidence.length > 0 && (
        <details className="fs-hp__evidence">
          <summary>{tn(p.probe_evidence.length, '{n} cited moment', '{n} cited moments')}</summary>
          <ul>
            {p.probe_evidence.map((e, i) => (
              <li key={i}><code>{`${e.run} · ${t('step {n}', { n: e.step })}`}</code> {e.quote ? <q>{e.quote}</q> : null}</li>
            ))}
          </ul>
        </details>
      )}
      {p.stale && p.status === 'pending' && (
        <p className="fs-set__help" data-tone="warn" data-testid="harness-stale">
          <AlertTriangle size={12} aria-hidden="true" /> {t('The target changed since this was proposed. Approving is refused until you propose again.')}
        </p>
      )}
      {p.risk_flags.length > 0 && <p className="fs-set__help" data-tone="warn">{p.risk_flags.join(' · ')}</p>}
      <Diff diff={p.diff} />
      {p.evidence.length > 0 && (
        <details className="fs-hp__evidence">
          <summary>{tn(p.evidence.length, '{n} piece of evidence', '{n} pieces of evidence')}</summary>
          <ul>
            {p.evidence.map((e, i) => (
              <li key={e.id ?? i}>
                <span className="fs-set__help">{e.kind ?? ''}{typeof e.turn === 'number' ? ` · ${t('turn {n}', { n: e.turn })}` : ''}</span> {e.quote ? <q>{e.quote}</q> : null}
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="fs-hp__foot">
        <span className="fs-set__help">
          {p.trigger}
          {p.created_at ? ` · ${when(p.created_at)}` : ''}
          {p.applied_at ? ` · ${t('applied')} ${when(p.applied_at)}` : ''}
        </span>
        <span className="fs-set__row-actions">
          {p.status === 'pending' && (
            <>
              <Button variant="primary" size="sm" icon={Check} label={t('Approve')} loading={busy === `approve:${p.id}`} disabled={Boolean(p.stale)} onClick={onApprove} testId={`harness-approve-${p.id}`} />
              <Button variant="ghost" size="sm" icon={X} label={t('Reject')} loading={busy === `reject:${p.id}`} onClick={onReject} testId={`harness-reject-${p.id}`} />
            </>
          )}
          {p.status === 'applied' && !isUndo && (
            <Button variant="secondary" size="sm" icon={RotateCcw} label={t('Undo')} loading={busy === `undo:${p.id}`} onClick={onUndo} testId={`harness-undo-${p.id}`} />
          )}
        </span>
      </div>
    </li>
  );
}

export function HarnessProposalsPanel({ say }: { say: (msg: string) => void }) {
  const [items, setItems] = useState<HarnessProposal[] | null>(null);
  const [info, setInfo] = useState<HarnessStatusInfo | null>(null);
  const [log, setLog] = useState<HarnessLogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [probeTick, setProbeTick] = useState(0);

  const reload = useCallback(async () => {
    try {
      const [rows, st, lg] = await Promise.all([listHarnessProposals(), harnessStatus(), harnessLog(30)]);
      setItems(rows);
      setInfo(st);
      setLog(lg);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const run = async (key: string, fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(key);
    try {
      await fn();
      if (okMsg) say(okMsg);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
      await reload();
    }
  };

  const propose = () =>
    run('propose', async () => {
      const out = await proposeForSession(sessionId.trim());
      if (out.status === 'proposed') say(t('A proposal is waiting for your approval.'));
      else say(`${t('Nothing to propose')}${out.reason ? `: ${out.reason}` : ''}`);
    });

  const probeNow = () =>
    run('probe', async () => {
      const out = await startProbe(sessionId.trim(), true);
      say(t('The round started. It repeats the task in the background; the result appears below.'));
      setProbeTick((n) => n + 1);
      return out;
    });

  const setMode = (patch: Parameters<typeof setHarnessMode>[0]) =>
    run('mode', async () => {
      await setHarnessMode(patch);
    });

  if (error && !items) {
    return <EmptyState icon={ShieldCheck} title={t('Harness proposals could not be loaded')} body={error} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />;
  }

  const pending = (items ?? []).filter((p) => p.status === 'pending');
  const history = (items ?? []).filter((p) => p.status !== 'pending');

  return (
    <div className="fs-set__section" data-testid="skills-harness">
      <header className="fs-set__section-head">
        <p className="fs-prose">
          {t('After a task, the assistant can suggest the smallest change to its own working state: project instructions, a skill, a memory or a subagent spec. The base system prompt is never touched. A change is first tried on a repeat of the task; the one that wins is applied by itself, or waits for you if you turn that off. Every applied change can be undone.')}
        </p>
      </header>

      {info && (
        <div className="fs-set__card" data-testid="harness-status">
          <p className="fs-set__help">
            {info.enabled ? t('Automatic proposals after each task: on.') : t('Automatic proposals after each task: off. Turn on "Harness: propose refinements after a task" in Settings.')}
            {' · '}
            {Object.entries(info.counts).map(([k, v]) => `${statusLabel(k)}: ${v}`).join(' · ')}
          </p>
          <div className="fs-hp__modes">
            <label className="fs-set__check">
              <input type="checkbox" checked={info.enabled} disabled={busy === 'mode'} onChange={(e) => void setMode({ harness_refinement_enabled: e.target.checked })} data-testid="harness-toggle-enabled" />
              {t('Propose after each task')}
            </label>
            <label className="fs-set__check">
              <input type="checkbox" checked={info.probe} disabled={busy === 'mode'} onChange={(e) => void setMode({ harness_refinement_probe: e.target.checked })} data-testid="harness-toggle-probe" />
              {t('Try each change on a repeat of the task first')}
            </label>
            <label className="fs-set__check">
              <input type="checkbox" checked={info.auto_apply} disabled={busy === 'mode' || !info.probe} onChange={(e) => void setMode({ harness_refinement_auto_apply: e.target.checked })} data-testid="harness-toggle-auto" />
              {t('Apply the winner by itself')}
            </label>
          </div>
          {!info.auto_apply && info.probe && <p className="fs-set__help">{t('The winner will wait for your approval.')}</p>}
          <div className="fs-set__form fs-hp__run">
            <input className="fs-field" placeholder={t('Session id')} aria-label={t('Session id')} value={sessionId} onChange={(e) => setSessionId(e.target.value)} data-testid="harness-session" />
            <Button variant="secondary" size="sm" label={t('Review this session')} loading={busy === 'propose'} disabled={!sessionId.trim()} onClick={() => void propose()} testId="harness-propose" />
            <Button variant="primary" size="sm" icon={FlaskConical} label={t('Try and improve')} loading={busy === 'probe'} disabled={!sessionId.trim()} onClick={() => void probeNow()} testId="harness-probe-start" />
          </div>
        </div>
      )}

      <ProbesCard refreshKey={probeTick} />

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Waiting for your approval')}</h3>
        {items === null ? (
          <Skeleton label={t('Loading')} count={2} height="64px" />
        ) : pending.length === 0 ? (
          <p className="fs-set__help">{t('Nothing is waiting.')}</p>
        ) : (
          <ul className="fs-hp__list">
            {pending.map((p) => (
              <ProposalCard key={p.id} p={p} busy={busy} onApprove={() => void run(`approve:${p.id}`, () => approveHarnessProposal(p.id), t('Applied. You can undo it from the history.'))} onReject={() => void run(`reject:${p.id}`, () => rejectHarnessProposal(p.id), t('Rejected.'))} onUndo={() => undefined} />
            ))}
          </ul>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('History')}</h3>
        {history.length === 0 ? (
          <p className="fs-set__help">{t('No decisions yet.')}</p>
        ) : (
          <ul className="fs-hp__list">
            {history.map((p) => (
              <ProposalCard key={p.id} p={p} busy={busy} onApprove={() => undefined} onReject={() => undefined} onUndo={() => void run(`undo:${p.id}`, () => undoHarnessProposal(p.id), t('Undone. The previous content is back.'))} />
            ))}
          </ul>
        )}
      </div>

      <LessonsCard refreshKey={probeTick} />

      {log.length > 0 && (
        <details className="fs-set__card" data-testid="harness-log">
          <summary>{tn(log.length, '{n} log line', '{n} log lines')}</summary>
          <div className="fs-sk__log">
            {log.map((l, i) => (
              <div key={i} className="fs-sk__line">
                {`${when(l.ts)} ${l.event} ${l.trigger ?? ''} -> ${l.result ?? ''}`}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
