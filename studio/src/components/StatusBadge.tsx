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
import { t } from '../i18n';

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
  queued: { icon: Clock, label: 'Queued' },
  running: { icon: Loader2, label: 'Running' },
  waiting: { icon: AlertCircle, label: 'Awaiting approval' },
  paused: { icon: Pause, label: 'Paused' },
  succeeded: { icon: CheckCircle, label: 'Completed' },
  failed: { icon: XCircle, label: 'Failed' },
  cancelled: { icon: MinusCircle, label: 'Cancelled' },
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
  const preset = { icon: PRESET[status].icon, label: t(PRESET[status].label) };
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
