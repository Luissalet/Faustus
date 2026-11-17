import { useId, useRef, type CSSProperties } from 'react';
import { t } from '../../i18n';
import { BrandMark } from '../../shell/BrandMark';
import type { LoopPhase } from './loop';
import './agent-loop.css';

type RingPhase = 'plan' | 'act' | 'observe';

/** Where each phase sits on the ring, in degrees from the x axis (plan at the top, then clockwise). */
const ANGLE: Record<RingPhase, number> = { plan: -90, act: 30, observe: 150 };
const NODES: { id: RingPhase; label: string }[] = [
  { id: 'plan', label: 'Plan#loop' },
  { id: 'act', label: 'Act' },
  { id: 'observe', label: 'Observe' },
];
const R = 50;          // ring radius in the 120 x 120 drawing
const TAIL = 110;      // degrees of the lit arc behind its head

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
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${to - from > 180 ? 1 : 0} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

/**
 * The angle the lit arc points at after `prev`: the phase's place on the
 * ring, reached by travelling clockwise, so observe -> plan (a new lap) and
 * observe -> act keep moving forward instead of turning back.
 */
export function nextAngle(prev: number | null, phase: LoopPhase): number {
  const base = phase === 'done' ? 270 : ANGLE[phase === 'idle' ? 'plan' : phase];
  return prev === null ? base : prev + ((((base - prev) % 360) + 360) % 360);
}

/**
 * Plan -> act -> observe around the agent. A glowing arc travels clockwise
 * along the ring to the phase the last event put the agent in, with a halo
 * at its head while the turn is live; the lap is in the centre. Nothing moves
 * between events, and with reduced motion the arc jumps instead of sliding.
 */
export function AgentLoopRing({ phase, lap, size = 'md', live = true, agent }: {
  phase: LoopPhase;
  lap: number;
  size?: 'sm' | 'md';
  live?: boolean;
  /** Name shown in the centre of the large ring (Faustus's mark goes with it). */
  agent?: string;
}) {
  const label = `${t('Agent loop')}: ${t(PHASE_LABEL[phase])} · ${t('Lap {n}', { n: lap })}`;
  const last = useRef<number | null>(null);
  const angle = nextAngle(last.current, phase);
  last.current = angle;
  const current = phase === 'done' || phase === 'idle' ? null : phase;
  const order = NODES.findIndex((n) => n.id === current);
  const gid = `fs-loop-grad-${useId().replace(/[^\w-]/g, '')}`;
  return (
    <span
      className="fs-agent-loop-ring"
      data-size={size}
      data-phase={phase}
      data-live={live ? 'true' : 'false'}
      role="img"
      aria-label={label}
      data-testid="agent-loop-ring"
    >
      <svg viewBox="0 0 120 120" aria-hidden="true">{/* guard-ok: data-drawn ring, its lit arc follows the live phase */}
        <defs>
          <linearGradient id={gid} gradientUnits="userSpaceOnUse" x1={point(R, -TAIL)[0]} y1={point(R, -TAIL)[1]} x2={point(R, 0)[0]} y2={point(R, 0)[1]}>
            <stop offset="0" className="fs-agent-loop-ring__stop-from" />
            <stop offset="1" className="fs-agent-loop-ring__stop-to" />
          </linearGradient>
        </defs>
        <circle className="fs-agent-loop-ring__track" cx="60" cy="60" r={R} />
        {phase === 'done' && <circle className="fs-agent-loop-ring__full" cx="60" cy="60" r={R} />}
        {phase !== 'done' && (
          <g className="fs-agent-loop-ring__comet" style={{ transform: `rotate(${angle}deg)` } as CSSProperties}>
            <path className="fs-agent-loop-ring__tail" d={arc(R, -TAIL, 0)} stroke={`url(#${gid})`} />
            <circle className="fs-agent-loop-ring__halo" cx={60 + R} cy="60" r="7" />
            <circle className="fs-agent-loop-ring__head" cx={60 + R} cy="60" r="3.6" />
          </g>
        )}
        {NODES.map((node, i) => {
          const [x, y] = point(R, ANGLE[node.id]);
          const state = phase === 'done' || (order > i) ? 'passed' : node.id === current ? 'active' : 'ahead';
          return <circle key={node.id} className="fs-agent-loop-ring__node" data-state={state} cx={x} cy={y} r={size === 'md' ? 4.2 : 7} />;
        })}
      </svg>
      {size === 'md' && (
        <>
          {NODES.map((node) => (
            <span key={node.id} className="fs-agent-loop-ring__pill" data-at={node.id} data-active={node.id === current ? 'true' : 'false'}>
              {t(node.label)}
            </span>
          ))}
          <span className="fs-agent-loop-ring__centre">
            {agent && (
              <span className="fs-agent-loop-ring__agent">
                {agent === 'Faustus' && <BrandMark size={16} />}
                <span>{agent}</span>
              </span>
            )}
            <strong className="fs-agent-loop-ring__lap">{phase === 'done' ? '✓' : lap}</strong>
            <small>{phase === 'done' ? t('Done#loop') : t('Lap#loop')}</small>
          </span>
        </>
      )}
    </span>
  );
}
