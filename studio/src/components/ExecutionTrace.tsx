import { ChevronDown } from 'lucide-react';
import { useState } from 'react';
import type { RunStatus } from './StatusBadge';

export interface TraceStep {
  id: string;
  label: string;
  state: RunStatus;
  /** Right-hand column: duration, progress, cost. Tabular figures. */
  meta?: string;
}

export interface ExecutionTraceProps {
  steps: TraceStep[];
  /** Finished steps beyond this many collapse into one openable line. */
  collapseAfter?: number;
  testId?: string;
}

/**
 * The signature element (DESIGN.md).
 *
 * A rail threading context → steps → artifact, shown wherever work
 * happens: Studio, Activity and the artifact card. It exists because
 * Faustus is the workspace that shows its execution honestly instead of
 * burying it in a chat transcript.
 *
 * The active node breathes in opacity only, never position or size, so
 * `prefers-reduced-motion` simply stops it without losing the signal:
 * colour, icon and text still say which step is running.
 */
export function ExecutionTrace({
  steps,
  collapseAfter = 3,
  testId = 'execution-trace',
}: ExecutionTraceProps) {
  const [expanded, setExpanded] = useState(false);

  const leadingDone = steps.findIndex((step) => step.state !== 'succeeded');
  const doneCount = leadingDone === -1 ? steps.length : leadingDone;
  const shouldCollapse = !expanded && doneCount > collapseAfter;
  const visible = shouldCollapse ? steps.slice(doneCount) : steps;

  return (
    <div className="fs-trace" data-testid={testId}>
      {shouldCollapse && (
        <button
          type="button"
          className="fs-trace__collapsed"
          onClick={() => setExpanded(true)}
          data-testid="trace-expand"
          aria-expanded={false}
        >
          <span aria-hidden="true" />
          <span>
            <ChevronDown size={13} aria-hidden="true" /> {doneCount} pasos completados
          </span>
        </button>
      )}
      {visible.map((step) => (
        <div
          key={step.id}
          className="fs-trace__step"
          data-state={step.state}
          data-testid={`trace-step-${step.id}`}
        >
          <span className="fs-trace__node" aria-hidden="true" />
          <span className="fs-trace__label">{step.label}</span>
          {step.meta && <span className="fs-trace__meta">{step.meta}</span>}
        </div>
      ))}
    </div>
  );
}
