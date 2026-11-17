import { t } from '../../i18n';
import type { LoopPhase } from './loop';
import './agent-loop.css';

const SEGMENTS: { id: 'plan' | 'act' | 'observe'; label: string; from: number }[] = [
  { id: 'plan', label: 'Plan#loop', from: -90 },
  { id: 'act', label: 'Act', from: 30 },
  { id: 'observe', label: 'Observe', from: 150 },
];
const GAP = 6;

export const PHASE_LABEL: Record<LoopPhase, string> = {
  plan: 'Plan#loop', act: 'Act', observe: 'Observe', done: 'Done#loop', idle: 'Idle',
};

function point(r: number, deg: number): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [60 + r * Math.cos(rad), 60 + r * Math.sin(rad)];
}

function arc(r: number, from: number, to: number): string {
  const [x1, y1] = point(r, from);
  const [x2, y2] = point(r, to);
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

/**
 * Plan -> act -> observe around the agent. The lit segment is the phase the
 * last event put the agent in; a light runs along it while the turn is live.
 * Nothing moves between events.
 */
export function AgentLoopRing({ phase, lap, size = 'md', live = true }: {
  phase: LoopPhase;
  lap: number;
  size?: 'sm' | 'md';
  live?: boolean;
}) {
  const label = `${t('Agent loop')}: ${t(PHASE_LABEL[phase])} · ${t('Lap {n}', { n: lap })}`;
  return (
    <span className="fs-agent-loop-ring" data-size={size} data-phase={phase} data-live={live ? 'true' : 'false'} role="img" aria-label={label} data-testid="agent-loop-ring">
      <svg viewBox={size === 'md' ? '-26 -4 172 128' : '0 0 120 120'} aria-hidden="true">{/* guard-ok: data-drawn ring, its arcs follow the live phase */}
        {SEGMENTS.map((seg) => {
          const d = arc(44, seg.from + GAP / 2, seg.from + 120 - GAP / 2);
          const active = seg.id === phase;
          const [lx, ly] = point(58, seg.from + 60);
          return (
            <g key={seg.id} className="fs-agent-loop-ring__segment" data-active={active ? 'true' : 'false'}>
              <path className="fs-agent-loop-ring__track" d={d} />
              {active && <path className="fs-agent-loop-ring__sweep" d={d} pathLength={100} />}
              {size === 'md' && (
                <text className="fs-agent-loop-ring__label" x={lx} y={ly + 3} textAnchor="middle">{t(seg.label)}</text>
              )}
            </g>
          );
        })}
        <circle className="fs-agent-loop-ring__core" cx="60" cy="60" r={size === 'md' ? 24 : 30} />
        <text className="fs-agent-loop-ring__lap" x="60" y={size === 'md' ? 66 : 71} textAnchor="middle">{phase === 'done' ? '✓' : lap}</text>
      </svg>
    </span>
  );
}
