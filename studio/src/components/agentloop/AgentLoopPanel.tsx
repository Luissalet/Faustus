import { useEffect, useState } from 'react';
import { t } from '../../i18n';
import { AgentLoopRing, PHASE_LABEL } from './AgentLoopRing';
import type { LoopStep, LoopView, PracticeState } from './loop';

const STEP_LABEL: Record<LoopStep['kind'], string> = {
  plan: 'Plan#loop', tool: 'Tool', result: 'Result', commit: 'Commit', submit: 'Delivery', end: 'End',
};
const STATE_LABEL: Record<PracticeState, string> = {
  ok: 'Met', fail: 'Failing', unknown: 'Not settled', pending: 'Missing',
};

/** A clock that ticks once a second only while the loop is live. */
export function useLoopClock(live: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [live]);
  return now;
}

function fmtElapsed(s: number | null): string {
  if (s === null) return '';
  const total = Math.round(s);
  return total >= 60 ? `${Math.floor(total / 60)} min ${total % 60} s` : `${total} s`;
}

/** One line under the ring: phase, tool in flight, lap, time and tokens. */
export function LoopFacts({ view }: { view: LoopView }) {
  return (
    <span className="fs-agent-loop__facts">
      <strong data-phase={view.phase}>{t(PHASE_LABEL[view.phase])}</strong>
      {view.tool && <code>{view.tool}</code>}
      <span>{t('Lap {n}', { n: view.lap })}</span>
      {view.elapsedS !== null && <span>{fmtElapsed(view.elapsedS)}</span>}
      {view.tokens !== null && <span>{t('{n} tokens', { n: view.tokens.toLocaleString() })}</span>}
    </span>
  );
}

/**
 * The agent loop of a chat turn: ring, the steps taken and the practices to
 * review. Folded by default once the turn ends; the checklist then reads as
 * the turn's summary (what was met and what is missing).
 */
export function AgentLoopPanel({ view, live }: { view: LoopView; live: boolean }) {
  const ok = view.practices.filter((p) => p.state === 'ok').length;
  const fail = view.practices.filter((p) => p.state === 'fail').length;
  const open = view.practices.length - ok - fail;
  const acted = view.steps.some((s) => s.kind === 'tool');
  return (
    <details className="fs-agent-loop" open={live || undefined} data-testid="agent-loop">
      <summary>
        <AgentLoopRing phase={view.phase} lap={view.lap} size="sm" live={live} />
        <span>{t('Agent loop')}</span>
        <LoopFacts view={view} />
        {!live && acted && <small className="fs-agent-loop__score">{t('{ok} met · {fail} failing · {open} missing', { ok, fail, open })}</small>}
      </summary>
      <div className="fs-agent-loop__body">
        <AgentLoopRing phase={view.phase} lap={view.lap} live={live} />
        <section aria-label={t('Steps')}>
          <h4>{t('Steps')}</h4>
          {view.steps.length === 0 ? <p className="fs-muted">{t('No steps yet')}</p> : (
            <ol className="fs-agent-loop__steps" data-testid="agent-loop-steps">
              {view.steps.map((step, i) => (
                <li key={i} data-kind={step.kind} data-ok={step.ok === undefined ? undefined : String(step.ok)}>
                  <span className="fs-agent-loop__kind">{t(STEP_LABEL[step.kind])}</span>
                  <span className="fs-agent-loop__what">{step.kind === 'plan' || step.kind === 'end' ? t(step.label) : step.label}</span>
                  <small>{t('Lap {n}', { n: step.lap })}</small>
                </li>
              ))}
            </ol>
          )}
        </section>
        {acted && (
          <section aria-label={t('Practices to review')}>
            <h4>{t('Practices to review')}</h4>
            <ul className="fs-agent-loop__practices" data-testid="agent-loop-practices">
              {view.practices.map((p) => (
                <li key={p.id} data-state={p.state}>
                  <span className="fs-agent-loop__mark" aria-hidden="true">{p.state === 'ok' ? '✓' : p.state === 'fail' ? '✕' : p.state === 'unknown' ? '?' : '·'}</span>
                  <span>{t(p.label)}</span>
                  <small title={p.evidence || undefined}>{t(STATE_LABEL[p.state])}{p.evidence ? ` · ${p.evidence}` : ''}</small>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </details>
  );
}
