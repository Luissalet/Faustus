import type { GenOverrides } from '../../adapters/composer';
import { t, tn } from '../../i18n';

/**
 * Slash commands.
 *
 * The legacy composer has forty-odd (`static/js/slashCommands.js`, 7k
 * lines). The ones that act on THIS conversation live here; the ones that
 * open another surface (notes, gallery, cookbook, the tours…) route to that
 * surface — in Studio when it exists, in the previous interface when it
 * does not — so no command disappears, it just says where it went.
 */

export interface SlashCommand {
  name: string;
  usage: string;
  help: string;
  /** Where the command goes when Studio does not run it itself. */
  route?: string;
  legacy?: boolean;
}

export const COMMANDS: SlashCommand[] = [
  { name: 'help', usage: '/help', help: 'Lists the commands.' },
  { name: 'models', usage: '/models', help: 'Pick a model.' },
  { name: 'compact', usage: '/compact', help: 'Summarises the old messages into one.' },
  { name: 'truncate', usage: '/truncate N', help: 'Keeps the first N messages and deletes the rest (a version remains).' },
  { name: 'versions', usage: '/versions', help: 'Previous versions of this chat (what an edit or a regenerate removed).' },
  { name: 'restore', usage: '/restore ID', help: 'Restores a version from /versions.' },
  { name: 'checkpoints', usage: '/checkpoints', help: 'Checkpoints of the working folder (one per turn with changes).' },
  { name: 'temp', usage: '/temp 0.4', help: 'Temperature of this chat (0–2). No value removes it.' },
  { name: 'maxtokens', usage: '/maxtokens 2048', help: 'Maximum reply tokens.' },
  { name: 'topp', usage: '/topp 0.9', help: 'top_p (0–1).' },
  { name: 'think', usage: '/think on|off', help: 'The model\'s reasoning, if it supports it.' },
  { name: 'gen', usage: '/gen key=value …', help: 'Generation settings: top_k, num_ctx, temperature…' },
  { name: 'remember', usage: '/remember rule', help: 'Saves a standing rule in the project instructions (same as #).' },
  { name: 'export', usage: '/export md|pdf|docx|html|txt|json', help: 'Downloads this conversation.' },
  { name: 'rename', usage: '/rename name', help: 'Renames the conversation.' },
  { name: 'stats', usage: '/stats', help: 'Tokens and timings of this conversation.' },
  {
    name: 'agents',
    usage: '/agents task one | task two [--review] [--serial]',
    help: 'Delegates each part to a sub-agent (up to 4). [f1, f2] before a task gives it those files exclusively; {model} picks its model.',
  },
  { name: 'doc', usage: '/doc [title]', help: 'Opens the document panel (with a title, creates a new one).' },
  { name: 'browser', usage: '/browser', help: 'Opens the panel with what the agent sees in the browser.' },
  { name: 'open', usage: '/open path', help: 'Opens a file from the working folder in the side panel.' },
  { name: 'incognito', usage: '/incognito [on|off]', help: 'Nobody mode: nothing is saved and the memory stays closed.' },
  { name: 'preset', usage: '/preset [name|off]', help: 'Preset or persona (system prompt). Without a name it opens the list.' },
  { name: 'fork', usage: '/fork', help: 'Forks the conversation: a copy with everything said so far.' },
  { name: 'tts', usage: '/tts', help: 'Reads the last reply aloud.' },
  { name: 'projects', usage: '/projects', help: 'Go to Projects.', route: '/projects' },
  { name: 'library', usage: '/library', help: 'Go to the Library.', route: '/library' },
  { name: 'gallery', usage: '/gallery', help: 'Go to the images.', route: '/library?type=imagen' },
  { name: 'tasks', usage: '/tasks', help: 'Go to Automations.', route: '/automations' },
  { name: 'activity', usage: '/activity', help: 'Go to Activity.', route: '/activity' },
  { name: 'notes', usage: '/notes', help: 'Go to Notes.', route: '/notes' },
  { name: 'calendar', usage: '/calendar', help: 'Go to the Calendar.', route: '/calendar' },
  { name: 'email', usage: '/email', help: 'Go to Mail.', route: '/email' },
  { name: 'brain', usage: '/brain', help: 'Go to the Memory.', route: '/memory' },
  { name: 'workers', usage: '/workers', help: 'Go to Agents: the Workers board.', route: '/agents' },
  { name: 'experts', usage: '/experts', help: 'Go to Agents: the Experts.', route: '/agents?t=experts' },
  { name: 'skills', usage: '/skills', help: 'Go to the Skills.', route: '/skills' },
  { name: 'research', usage: '/research [question]', help: 'Deep Research: several rounds of search and reading, then a report with sources.', route: '/research' },
  { name: 'compare', usage: '/compare', help: 'Compare: the same prompt to several models side by side, blind, with a vote.', route: '/compare' },
  { name: 'mcp', usage: '/mcp', help: 'MCP servers (Settings → Integrations).', route: '/settings?s=integrations' },
  { name: 'setup', usage: '/setup', help: 'Go to Settings.', route: '/settings' },
  { name: 'usage', usage: '/usage [on|off]', help: 'Shows or hides the live usage (GPU, VRAM, model, RAM) in the header.' },
];

export function matchCommands(prefix: string): SlashCommand[] {
  const needle = prefix.replace(/^\//, '').toLowerCase();
  return COMMANDS.filter((c) => c.name.startsWith(needle)).slice(0, 8);
}

export interface ParsedCommand {
  command: SlashCommand | null;
  name: string;
  args: string;
}

export function parseCommand(text: string): ParsedCommand | null {
  const match = /^\/([a-z-]+)(?:\s+([\s\S]*))?$/i.exec(text.trim());
  if (!match) return null;
  const name = match[1].toLowerCase();
  return { command: COMMANDS.find((c) => c.name === name) ?? null, name, args: (match[2] ?? '').trim() };
}

/** `/gen a=1 b=2` and the single-knob commands both land here. */
export function genFromArgs(name: string, args: string, current: GenOverrides): GenOverrides {
  const next: GenOverrides = { ...current };
  const num = (v: string) => (v === '' ? undefined : Number(v));
  switch (name) {
    case 'temp': {
      const v = num(args);
      if (v === undefined || Number.isNaN(v)) delete next.temperature;
      else next.temperature = Math.min(2, Math.max(0, v));
      return next;
    }
    case 'maxtokens': {
      const v = num(args);
      if (v === undefined || Number.isNaN(v)) delete next.max_tokens;
      else next.max_tokens = Math.max(0, Math.round(v));
      return next;
    }
    case 'topp': {
      const v = num(args);
      if (v === undefined || Number.isNaN(v)) delete next.top_p;
      else next.top_p = Math.min(1, Math.max(0.01, v));
      return next;
    }
    case 'think': {
      const v = args.toLowerCase();
      if (v === 'on' || v === 'true' || v === 'sí' || v === 'si') next.think = true;
      else if (v === 'off' || v === 'false' || v === 'no') next.think = false;
      else delete next.think;
      return next;
    }
    case 'gen': {
      if (!args) return {};
      for (const pair of args.split(/\s+/)) {
        const [key, raw] = pair.split('=');
        if (!key || raw === undefined) continue;
        const value = Number(raw);
        if (key === 'think') next.think = raw === 'on' || raw === 'true';
        else if (['temperature', 'max_tokens', 'top_p', 'top_k', 'num_ctx'].includes(key) && !Number.isNaN(value)) {
          (next as Record<string, number | boolean | undefined>)[key] = value;
        }
      }
      return next;
    }
    default:
      return next;
  }
}

/**
 * `/agents a | b | c --review --serial` → a delegation. Each part is one
 * worker; `[f1, f2]` in front gives it those files, `{model}` its model.
 * Returns an error text instead when the input cannot be delegated.
 */
export function parseDelegation(args: string): { tasks: { name: string; instruction: string; files?: string[]; model?: string }[]; parallel: boolean; reviewer: boolean } | string {
  let raw = args.trim();
  const flags = { reviewer: false, parallel: true };
  raw = raw
    .replace(/(^|\s)--(review|reviewer|serial|sequential)\b/g, (_m, _sp, f: string) => {
      if (f === 'review' || f === 'reviewer') flags.reviewer = true;
      else flags.parallel = false;
      return ' ';
    })
    .trim();
  const parts = raw
    .split(/\s*(?:\||;;|\n)\s*/)
    .map((p) => p.trim())
    .filter(Boolean);
  if (!parts.length) {
    return t('Usage: /agents task one | task two | task three — each part is a sub-agent. [file1, file2] in front gives it those files exclusively; {model} picks its model; --review adds a reviewer; --serial runs them one after another.');
  }
  if (parts.length > 4) return t('At most 4 sub-agents per call. Merge tasks or repeat /agents afterwards.');
  const tasks = parts.map((p) => {
    const model = /^\s*\{([^}]+)\}/.exec(p)?.[1]?.trim();
    const files = /^\s*(?:\{[^}]+\}\s*)?\[([^\]]+)\]/.exec(p)?.[1]
      ?.split(',')
      .map((f) => f.trim())
      .filter(Boolean);
    const bare = (/^\s*(?:\{[^}]+\}\s*)?(?:\[[^\]]+\]\s*)?([\s\S]*)$/.exec(p)?.[1] ?? p).trim() || p;
    return { name: bare.length > 40 ? `${bare.slice(0, 38)}…` : bare, instruction: p, files, model };
  });
  return { tasks, parallel: flags.parallel, reviewer: flags.reviewer };
}

/** The readable label the chat bubble shows for a delegation. */
export function delegationLabel(d: { tasks: { name: string; files?: string[]; model?: string }[]; reviewer: boolean; parallel: boolean }): string {
  const label = d.tasks.map((task) => `${task.model ? `{${task.model}} ` : ''}${task.files?.length ? `[${task.files.join(', ')}] ` : ''}${task.name}`).join(' | ');
  return `🤖 ${tn(d.tasks.length, '{n} sub-agent', '{n} sub-agents')}${d.reviewer ? t(' + reviewer') : ''}${d.parallel ? '' : t(' (in series)')}: ${label}`;
}
