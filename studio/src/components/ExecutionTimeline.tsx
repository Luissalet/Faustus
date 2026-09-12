import { timelineBars, type ExecutionMetrics, type MetricValue, type PhaseKey, type Timeline, type TimelineBar } from '../adapters/chat';
import { t } from '../i18n';

/**
 * INF-03 §08/§15 — "why did it take this long?"
 *
 * One bar per phase Faustus could actually account for (`timelineBars`,
 * `adapters/chat.ts`), scaled to the turn's own `total_ms`; a phase the
 * engine never reported and Faustus could not safely derive (`absent`)
 * never gets a bar at all — not even a zero-length one, which would read
 * as "this took 0ms" instead of "nobody knows" (the one rule the whole
 * contract answers to, `docs/api/execution_metrics.md`). Every bar carries
 * its `source` as a badge instead of asking the reader to trust a number
 * with no receipt.
 *
 * Two turn/run surfaces want the same rows: a Studio turn's own openable
 * disclosure (`variant="full"`, `Transcript.tsx`) and, once a run/turn
 * carries `execution` in its own metadata, a compact embed inside a detail
 * pane that is already open (`variant="compact"`) — see `Activity.tsx`'s
 * doc comment on why nothing there does yet. One component, so that day
 * does not need a second implementation of these rows.
 */
export interface ExecutionTimelineProps {
  execution: ExecutionMetrics;
  variant?: 'full' | 'compact';
  testId?: string;
}

function neutralPhaseLabel(phase: PhaseKey): string {
  switch (phase) {
    case 'queue_wait_ms':
      return t('Queue wait');
    case 'load_ms':
      return t('Model load');
    case 'prefill_ms':
      return t('Prefill');
    case 'generation_ms':
      return t('Generation');
    case 'tools_ms':
      return t('Tools');
    case 'total_ms':
      return t('Total');
  }
}

/**
 * §15: a label only claims what its own bar's evidence backs up. "Waiting
 * for capacity" only when the admission gate really measured a positive
 * wait; "Loading the model" only when the engine itself reported a load
 * time. A queue wait the gate measured as genuinely zero — evidence too,
 * just not a wait — gets the neutral name instead of a claim the number
 * does not support.
 */
function barLabel(bar: TimelineBar): string {
  if (bar.phase === 'queue_wait_ms' && bar.source === 'observed_client' && bar.valueMs > 0) return t('Waiting for capacity');
  if (bar.phase === 'load_ms' && bar.source === 'reported_engine') return t('Loading the model');
  return neutralPhaseLabel(bar.phase);
}

function sourceBadgeLabel(source: TimelineBar['source']): string {
  switch (source) {
    case 'observed_client':
      return t('observed');
    case 'reported_engine':
      return t('engine');
    case 'computed':
      return t('computed');
    case 'inferred':
      return t('inferred');
  }
}

function formatMs(ms: number): string {
  if (ms >= 1000) return t('{n} s', { n: (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) });
  return t('{n} ms', { n: Math.round(ms) });
}

function tokenLine(mv: MetricValue): string {
  if (mv.source === 'absent' || mv.value === null) return t('not observed');
  const badge = mv.source === 'reported_engine' ? t('engine') : mv.source === 'computed' ? t('computed') : sourceBadgeLabel(mv.source);
  return `${Math.round(mv.value).toLocaleString()} (${badge})`;
}

function TimelineRows({ timeline }: { timeline: Timeline }) {
  return (
    <>
      <ul className="fs-timeline__rows" data-testid="turn-timeline-rows">
        {timeline.bars.map((bar) => (
          <li key={bar.phase} data-testid={`turn-timeline-bar-${bar.phase}`}>
            <span className="fs-timeline__label">{barLabel(bar)}</span>
            <span className="fs-timeline__track" aria-hidden="true">
              {/* §15/docs/api/execution_metrics.md: the drawn box is capped at
                  100% for layout — a phase that truly overlapped another can
                  read over 100% (see `overlap` below) — but the number next
                  to it is never rescaled to match. */}
              <span style={{ inlineSize: `${Math.min(100, bar.widthPercent)}%` }} />
            </span>
            <span className="fs-timeline__value">{formatMs(bar.valueMs)}</span>
            <span className="fs-timeline__badge" data-source={bar.source}>
              {sourceBadgeLabel(bar.source)}
            </span>
          </li>
        ))}
        {timeline.absentPhases.map((phase) => (
          <li key={phase} className="fs-timeline__row--absent" data-testid={`turn-timeline-absent-${phase}`}>
            <span className="fs-timeline__label">{neutralPhaseLabel(phase)}</span>
            <span className="fs-timeline__absent">{t('The engine does not expose this metric')}</span>
          </li>
        ))}
      </ul>
      {timeline.notes.length > 0 && (
        <ul className="fs-timeline__notes" data-testid="turn-timeline-notes">
          {timeline.notes.map((note, i) => (
            <li key={i}>{note}</li>
          ))}
        </ul>
      )}
      <dl className="fs-timeline__tokens" data-testid="turn-timeline-tokens">
        <div>
          <dt>{t('Prompt tokens')}</dt>
          <dd>{tokenLine(timeline.tokens.prompt)}</dd>
        </div>
        <div>
          <dt>{t('Generated tokens')}</dt>
          <dd>{tokenLine(timeline.tokens.generated)}</dd>
        </div>
      </dl>
    </>
  );
}

export function ExecutionTimeline({ execution, variant = 'full', testId = 'turn-timeline' }: ExecutionTimelineProps) {
  const timeline = timelineBars(execution);

  if (variant === 'compact') {
    return (
      <div className="fs-timeline fs-timeline--compact" data-overlap={timeline.overlap || undefined} data-testid={testId}>
        <TimelineRows timeline={timeline} />
      </div>
    );
  }

  return (
    <details className="fs-timeline" data-overlap={timeline.overlap || undefined} data-testid={testId}>
      <summary>
        <span className="fs-timeline__title">{t('Why did it take this long?')}</span>
      </summary>
      <TimelineRows timeline={timeline} />
    </details>
  );
}
