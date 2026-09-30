import { useEffect, useState } from 'react';
import { fetchTurnCost, type CostPhase, type CostPhaseKey, type TurnCost } from '../adapters/runReport';
import { t } from '../i18n';

/**
 * Where did this turn's time and tokens go, grouped by cause.
 *
 * One row per phase the run actually went through (main rounds, tools,
 * compaction, recovery, advisor, workers, retries), in the order the server
 * reports them. A value nobody reported is shown as "unknown"; it is never
 * rendered as 0 and never added into a total. A phase whose cost is only
 * partly known says so ("at least"). Nothing here is fetched until the
 * disclosure is opened.
 */
export interface TurnCostBreakdownProps {
  sessionId: string;
  runId: string;
  testId?: string;
}

export function phaseLabel(key: CostPhaseKey): string {
  switch (key) {
    case 'main_rounds':
      return t('Model rounds');
    case 'tools':
      return t('Tools');
    case 'compaction':
      return t('Compaction');
    case 'recovery':
      return t('Recovery');
    case 'advisor':
      return t('Advisor');
    case 'workers':
      return t('Workers');
    case 'retries':
      return t('Retries');
    case 'other_model':
      return t('Other model calls');
  }
  return String(key);
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return t('unknown');
  if (ms >= 1000) return t('{n} s', { n: (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) });
  return t('{n} ms', { n: Math.round(ms) });
}

export function formatTokens(value: number | null, unknownCalls: number): string {
  if (value === null) return t('unknown');
  const base = Math.round(value).toLocaleString();
  return unknownCalls > 0 ? t('at least {n}', { n: base }) : base;
}

export function formatCost(phase: CostPhase): string {
  if (phase.costUsd === null || phase.costState === 'unknown') return t('unknown');
  const base = `$${phase.costUsd.toFixed(phase.costUsd < 0.01 ? 4 : 2)}`;
  return phase.costState === 'partial' ? t('at least {n}', { n: base }) : base;
}

function PhaseRow({ phase }: { phase: CostPhase }) {
  return (
    <li className="fs-turncost__phase" data-testid={`turn-cost-phase-${phase.key}`} data-cost-state={phase.costState}>
      <div className="fs-turncost__phase-head">
        <span className="fs-turncost__label">{phaseLabel(phase.key)}</span>
        <span className="fs-turncost__calls">{t('{n} calls', { n: phase.calls })}</span>
        {phase.errors > 0 && <span className="fs-turncost__badge" data-kind="error">{t('{n} failed', { n: phase.errors })}</span>}
        {phase.estimatedCalls > 0 && <span className="fs-turncost__badge" data-kind="estimated">{t('estimated')}</span>}
      </div>
      <dl className="fs-turncost__figures">
        <div>
          <dt>{t('Time')}</dt>
          <dd data-unknown={phase.durationMs === null || undefined}>
            {phase.durationMs === null
              ? t('unknown')
              : phase.durationUnknownCalls > 0
                ? t('at least {n}', { n: formatDuration(phase.durationMs) })
                : formatDuration(phase.durationMs)}
          </dd>
        </div>
        <div>
          <dt>{t('Tokens in')}</dt>
          <dd data-unknown={phase.inputTokens === null || undefined}>{formatTokens(phase.inputTokens, phase.inputTokensUnknownCalls)}</dd>
        </div>
        <div>
          <dt>{t('Tokens out')}</dt>
          <dd data-unknown={phase.outputTokens === null || undefined}>{formatTokens(phase.outputTokens, phase.outputTokensUnknownCalls)}</dd>
        </div>
        <div>
          <dt>{t('Cost')}</dt>
          <dd data-unknown={phase.costState === 'unknown' || undefined}>{formatCost(phase)}</dd>
        </div>
      </dl>
      {phase.items.length > 0 && (
        <ul className="fs-turncost__items">
          {phase.items.map((item, i) => (
            <li key={`${item.label}-${i}`}>
              <span className="fs-turncost__label">{item.label || t('unnamed')}</span>
              <span>{item.calls === null ? '' : t('{n} calls', { n: item.calls })}</span>
              <span>{formatDuration(item.durationMs)}</span>
              {item.unknownEffect > 0 && <span className="fs-turncost__badge" data-kind="unknown-effect">{t('effect unknown')}</span>}
              {item.failed > 0 && <span className="fs-turncost__badge" data-kind="error">{t('{n} failed', { n: item.failed })}</span>}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Body({ cost }: { cost: TurnCost }) {
  if (!cost.found) {
    return <p className="fs-turncost__empty" data-testid="turn-cost-empty">{t('No cost record was kept for this turn.')}</p>;
  }
  return (
    <>
      <p className="fs-turncost__total" data-testid="turn-cost-total">
        {t('Turn time')}: {formatDuration(cost.totalMs)}
        {cost.unaccountedMs !== null && cost.unaccountedMs > 0 && (
          <>
            {' · '}
            <span data-testid="turn-cost-unaccounted">{t('not attributed to a phase: {n}', { n: formatDuration(cost.unaccountedMs) })}</span>
          </>
        )}
      </p>
      <ul className="fs-turncost__phases" data-testid="turn-cost-phases">
        {cost.phases.map((phase) => (
          <PhaseRow key={phase.key} phase={phase} />
        ))}
      </ul>
      {cost.notes.length > 0 && (
        <ul className="fs-turncost__notes" data-testid="turn-cost-notes">
          {cost.notes.map((note, i) => (
            <li key={i}>{note}</li>
          ))}
        </ul>
      )}
    </>
  );
}

export function TurnCostBreakdown({ sessionId, runId, testId = 'turn-cost' }: TurnCostBreakdownProps) {
  const [open, setOpen] = useState(false);
  const [cost, setCost] = useState<TurnCost | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || cost) return;
    const controller = new AbortController();
    setError(null);
    fetchTurnCost(sessionId, runId, controller.signal)
      .then(setCost)
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
      });
    return () => controller.abort();
  }, [open, cost, sessionId, runId]);

  return (
    <details
      className="fs-turncost"
      data-testid={testId}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>
        <span className="fs-turncost__title">{t('Where did the cost go?')}</span>
      </summary>
      {open && !cost && !error && <p className="fs-turncost__empty">{t('Loading')}…</p>}
      {error && <p className="fs-turncost__error" role="alert" data-testid="turn-cost-error">{error}</p>}
      {cost && <Body cost={cost} />}
    </details>
  );
}
