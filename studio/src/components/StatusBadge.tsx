import {
  AlertCircle,
  CheckCircle,
  Clock,
  Loader2,
  MinusCircle,
  Pause,
  XCircle,
  type LucideIcon,
} from 'lucide-react';

/** The seven run states, normalised in the frontend (UI-050). */
export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting'
  | 'paused'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

const PRESET: Record<RunStatus, { icon: LucideIcon; label: string }> = {
  queued: { icon: Clock, label: 'En cola' },
  running: { icon: Loader2, label: 'Ejecutando' },
  waiting: { icon: AlertCircle, label: 'Esperando aprobación' },
  paused: { icon: Pause, label: 'En pausa' },
  succeeded: { icon: CheckCircle, label: 'Completado' },
  failed: { icon: XCircle, label: 'Fallido' },
  cancelled: { icon: MinusCircle, label: 'Cancelado' },
};

export interface StatusBadgeProps {
  status: RunStatus;
  label?: string;
  size?: 'sm' | 'md';
}

/**
 * Icon plus text. Colour is the third signal, never the only one: a
 * red dot and a green dot are the same dot to a good part of the users.
 */
export function StatusBadge({ status, label, size = 'sm' }: StatusBadgeProps) {
  const preset = PRESET[status];
  const Icon = preset.icon;

  return (
    <span
      className="fs-status"
      data-status={status}
      data-size={size}
      data-testid={`status-${status}`}
    >
      <Icon
        size={size === 'sm' ? 13 : 15}
        aria-hidden="true"
        className={status === 'running' ? 'fs-status__spin' : undefined}
      />
      {label ?? preset.label}
    </span>
  );
}
