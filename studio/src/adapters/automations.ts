import { asArray, getJson } from './api';

export interface Automation {
  id: string;
  name: string;
  task_type?: string | null;
  action?: string | null;
  prompt?: string | null;
  schedule?: string | null;
  cron_expression?: string | null;
  scheduled_time?: string | null;
  trigger_type?: string | null;
  trigger_event?: string | null;
  trigger_count?: number | null;
  next_run?: string | null;
  last_run?: string | null;
  status?: string | null;
  run_count?: number | null;
  output_target?: string | null;
  is_builtin?: boolean;
}

export function listAutomations(signal?: AbortSignal): Promise<Automation[]> {
  return getJson<unknown>('/api/tasks', signal).then((value) =>
    asArray<Automation>(value, 'tasks'),
  );
}

/**
 * The recipe in one readable line.
 *
 * The product document is explicit that the primary view of an automation is
 * the sentence, not the node graph: "Cada lunes · Preparar resumen · Próxima
 * ejecución 09:00". The editor is an advanced inspection, and it can stay
 * where it is until it earns a screen.
 */
export function describeTrigger(task: Automation): string {
  if (task.trigger_type === 'event' && task.trigger_event) {
    const times = task.trigger_count && task.trigger_count > 1 ? ` ×${task.trigger_count}` : '';
    return `Cuando ocurre ${task.trigger_event}${times}`;
  }
  if (task.cron_expression) return `cron ${task.cron_expression}`;
  if (task.schedule && task.scheduled_time) return `${task.schedule} a las ${task.scheduled_time}`;
  if (task.schedule) return String(task.schedule);
  return 'Solo a mano';
}

export function describeAction(task: Automation): string {
  if (task.action) return task.action.replace(/_/g, ' ');
  if (task.prompt) return task.prompt.slice(0, 90);
  return task.task_type ?? 'acción';
}
