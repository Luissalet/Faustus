import type { RunStatus } from '../../components';
import type { AskUser, ChatEvent, TurnMetrics, WebSource } from '../../adapters/chat';
import type { Attachment } from '../../adapters/composer';

/**
 * The transcript's data model and the one reducer that applies a stream
 * event to the assistant turn being written. Pure: no React, no fetch, so
 * it can be reasoned about (and tested) as a function of events.
 */

export interface Step {
  id: string;
  tool: string;
  label: string;
  state: RunStatus;
  meta?: string;
  command?: string;
  output?: string;
  round: number;
}

export interface Turn {
  id: string;
  /** The message's database id, when the server has one; edits need it. */
  dbId?: string;
  role: 'user' | 'assistant';
  text: string;
  thinking: string;
  steps: Step[];
  rounds: number;
  metrics?: TurnMetrics;
  sources: WebSource[];
  images: string[];
  attachments: Attachment[];
  ask?: AskUser;
  note?: string;
  error?: string;
  edited?: boolean;
  streaming: boolean;
}

let counter = 0;
export const uid = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}-${(counter++).toString(36)}`;

export function blankTurn(role: Turn['role'], text = ''): Turn {
  return {
    id: uid(role),
    role,
    text,
    thinking: '',
    steps: [],
    rounds: 1,
    sources: [],
    images: [],
    attachments: [],
    streaming: role === 'assistant',
  };
}

/** A tool name the model uses → the words a person reads on the rail. */
const TOOL_WORDS: Record<string, string> = {
  bash: 'Terminal',
  python: 'Python',
  read_file: 'Leer',
  write_file: 'Escribir',
  edit_file: 'Editar',
  apply_patch: 'Parche',
  ls: 'Listar',
  glob: 'Buscar ficheros',
  grep: 'Buscar en ficheros',
  web_search: 'Buscar en la web',
  web_fetch: 'Abrir URL',
  fetch_url: 'Abrir URL',
  browser: 'Navegador',
  create_document: 'Crear documento',
  edit_document: 'Editar documento',
  update_document: 'Actualizar documento',
  generate_image: 'Generar imagen',
  delegate_agents: 'Delegar',
  ask_user: 'Preguntar',
  update_plan: 'Plan',
  todowrite: 'Tareas',
  manage_memory: 'Memoria',
};

export function stepLabel(tool: string, command: string): string {
  const word = TOOL_WORDS[tool] ?? tool.replace(/_/g, ' ');
  const brief = command.trim().split('\n')[0].slice(0, 96);
  return brief ? `${word} · ${brief}` : word;
}

export function formatMetrics(m: TurnMetrics): string {
  const parts: string[] = [];
  if (m.model) parts.push(m.model);
  if (m.outputTokens !== undefined) parts.push(`${m.outputTokens} tok`);
  if (m.tokensPerSecond !== undefined) parts.push(`${m.tokensPerSecond.toFixed(1)} tok/s`);
  if (m.responseTime !== undefined) parts.push(`${m.responseTime.toFixed(1)} s`);
  if (m.contextPercent !== undefined) parts.push(`contexto ${Math.round(m.contextPercent)}%`);
  return parts.join(' · ');
}

function lastRunning(steps: Step[], tool: string): number {
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].state === 'running' && steps[i].tool === tool) return i;
  }
  return -1;
}

/** Applies one stream event to the assistant turn at the end of the list. */
export function apply(turn: Turn, event: ChatEvent): Turn {
  switch (event.type) {
    case 'delta':
      return event.thinking
        ? { ...turn, thinking: turn.thinking + event.text }
        : { ...turn, text: turn.text + event.text };
    case 'tool_start': {
      // After an approval the server replays the same tool's start: the
      // step that was waiting becomes the one that runs, not a twin.
      const held = turn.steps.findIndex((s) => s.state === 'waiting' && s.tool === event.tool);
      if (held !== -1) {
        const steps = turn.steps.slice();
        steps[held] = { ...steps[held], state: 'running', meta: undefined };
        return { ...turn, steps };
      }
      return {
        ...turn,
        rounds: Math.max(turn.rounds, event.round),
        steps: [
          ...turn.steps,
          {
            id: uid('step'),
            tool: event.tool,
            label: stepLabel(event.tool, event.command),
            state: 'running',
            command: event.fullCommand ?? event.command,
            round: event.round,
          },
        ],
      };
    }
    case 'tool_progress': {
      const index = lastRunning(turn.steps, event.tool);
      if (index === -1) return turn;
      const steps = turn.steps.slice();
      steps[index] = { ...steps[index], meta: event.message.slice(0, 60) };
      return { ...turn, steps };
    }
    case 'tool_output': {
      const index = lastRunning(turn.steps, event.tool);
      const finished: Step = {
        id: index === -1 ? uid('step') : turn.steps[index].id,
        tool: event.tool,
        label: index === -1 ? stepLabel(event.tool, event.command) : turn.steps[index].label,
        state: event.exitCode === null || event.exitCode === 0 ? 'succeeded' : 'failed',
        meta: event.exitCode !== null && event.exitCode !== 0 ? `exit ${event.exitCode}` : undefined,
        command: index === -1 ? event.command : turn.steps[index].command,
        output: event.output,
        round: index === -1 ? turn.rounds : turn.steps[index].round,
      };
      const steps = turn.steps.slice();
      if (index === -1) steps.push(finished);
      else steps[index] = finished;
      return { ...turn, steps };
    }
    case 'round':
      return { ...turn, rounds: Math.max(turn.rounds, event.round) };
    case 'ask_user':
      return {
        ...turn,
        ask: event.ask,
        steps: turn.steps.map((s) => (s.state === 'running' ? { ...s, state: 'waiting' } : s)),
      };
    case 'metrics':
      return { ...turn, metrics: { ...turn.metrics, ...event.metrics } };
    case 'sources':
      return { ...turn, sources: event.sources };
    case 'image':
      return { ...turn, images: [...turn.images, event.url] };
    case 'fallback':
      return {
        ...turn,
        note: `${event.selected || 'El modelo elegido'} no ha respondido; ha contestado ${event.answeredBy}.`,
      };
    case 'terminal':
      return event.failed ? { ...turn, error: event.message ?? 'El modelo ha fallado.' } : turn;
    case 'error':
      return { ...turn, error: event.message };
    case 'done':
      return {
        ...turn,
        streaming: false,
        steps: turn.steps.map((s) => (s.state === 'running' ? { ...s, state: 'cancelled' } : s)),
      };
  }
}

/** The user's text as it was typed, without the file blocks the server
 *  inlines for the model (the legacy renderer strips the same markers). */
export function cleanUserText(text: string, hasAttachments: boolean): string {
  let out = text.replace(
    /\n*\[Image: [^\]]+\]\n[\s\S]*?(?=\n*\[Image: |\n*\[Image attached: |\n*=== File: |\n*\[PDF content\]:|$)/g,
    '',
  );
  if (hasAttachments) {
    out = out
      .replace(/\n*=== File: .+? ===\n\[Type: .+?\]\n+```[\s\S]*?```/g, '')
      .replace(/\n*=== File: .+? ===\n\[Type: .+?\]\n+[\s\S]*?(?=\n*=== File:|$)/g, '')
      .replace(/\n*\[PDF content\]:[\s\S]*?(?=\n*\[PDF content\]|\n*=== File:|$)/g, '')
      .replace(/\n*\[Image attached: [^\]]+\]/g, '')
      .replace(/\n*\[Attached (?:document|non-text) file\]/g, '');
  }
  return out.replace(/\s*\[\d+ attachment\(s\)\]$/, '').trim();
}
