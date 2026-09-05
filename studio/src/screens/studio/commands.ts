import type { GenOverrides } from '../../adapters/composer';

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
  { name: 'help', usage: '/help', help: 'Lista los comandos.' },
  { name: 'models', usage: '/models', help: 'Elegir modelo.' },
  { name: 'compact', usage: '/compact', help: 'Resume los mensajes antiguos en uno.' },
  { name: 'truncate', usage: '/truncate N', help: 'Conserva los N primeros mensajes y borra el resto (queda una versión).' },
  { name: 'versions', usage: '/versions', help: 'Versiones anteriores de este chat (lo que borró una edición o un regenerar).' },
  { name: 'restore', usage: '/restore ID', help: 'Restaura una versión de /versions.' },
  { name: 'checkpoints', usage: '/checkpoints', help: 'Puntos de control de la carpeta de trabajo (uno por turno con cambios).' },
  { name: 'temp', usage: '/temp 0.4', help: 'Temperatura de este chat (0–2). Sin valor, la quita.' },
  { name: 'maxtokens', usage: '/maxtokens 2048', help: 'Máximo de tokens de respuesta.' },
  { name: 'topp', usage: '/topp 0.9', help: 'top_p (0–1).' },
  { name: 'think', usage: '/think on|off', help: 'Razonamiento del modelo, si lo soporta.' },
  { name: 'gen', usage: '/gen clave=valor …', help: 'Ajustes de generación: top_k, num_ctx, temperature…' },
  { name: 'remember', usage: '/remember regla', help: 'Guarda una regla permanente en las instrucciones del proyecto (igual que #).' },
  { name: 'export', usage: '/export md|pdf|docx|html|txt|json', help: 'Descarga esta conversación.' },
  { name: 'rename', usage: '/rename nombre', help: 'Renombra la conversación.' },
  { name: 'stats', usage: '/stats', help: 'Tokens y tiempos de esta conversación.' },
  {
    name: 'agents',
    usage: '/agents tarea uno | tarea dos [--review] [--serial]',
    help: 'Delega cada parte a un sub-agente (hasta 4). [f1, f2] antes de una tarea le da esos ficheros en exclusiva; {modelo} elige su modelo.',
  },
  { name: 'doc', usage: '/doc [título]', help: 'Abre el panel de documento (con título, crea uno nuevo).' },
  { name: 'browser', usage: '/browser', help: 'Abre el panel con lo que ve el agente en el navegador.' },
  { name: 'open', usage: '/open ruta', help: 'Abre un fichero de la carpeta de trabajo en el panel lateral.' },
  { name: 'incognito', usage: '/incognito [on|off]', help: 'Modo Nobody: no se guarda nada y la memoria queda cerrada.' },
  { name: 'preset', usage: '/preset [nombre|off]', help: 'Preset o personaje (prompt de sistema). Sin nombre abre la lista.' },
  { name: 'fork', usage: '/fork', help: 'Bifurca la conversación: una copia con todo lo dicho hasta ahora.' },
  { name: 'tts', usage: '/tts', help: 'Lee en voz alta la última respuesta.' },
  { name: 'projects', usage: '/projects', help: 'Ir a Proyectos.', route: '/projects' },
  { name: 'library', usage: '/library', help: 'Ir a la Biblioteca.', route: '/library' },
  { name: 'gallery', usage: '/gallery', help: 'Ir a las imágenes.', route: '/library?type=imagen' },
  { name: 'tasks', usage: '/tasks', help: 'Ir a Automatizaciones.', route: '/automations' },
  { name: 'activity', usage: '/activity', help: 'Ir a Actividad.', route: '/activity' },
  { name: 'notes', usage: '/notes', help: 'Ir a Notas.', route: '/notes' },
  { name: 'calendar', usage: '/calendar', help: 'Ir al Calendario.', route: '/calendar' },
  { name: 'email', usage: '/email', help: 'Ir al Correo.', route: '/email' },
  { name: 'brain', usage: '/brain', help: 'Ir a la Memoria.', route: '/memory' },
  { name: 'workers', usage: '/workers', help: 'Ir a Agentes: el tablero de Workers.', route: '/agents' },
  { name: 'experts', usage: '/experts', help: 'Ir a Agentes: los Expertos.', route: '/agents?t=experts' },
  { name: 'research', usage: '/research tema', help: 'Deep research (interfaz anterior).', route: '/?shell=legacy', legacy: true },
  { name: 'compare', usage: '/compare', help: 'Comparar modelos (interfaz anterior).', route: '/?shell=legacy', legacy: true },
  { name: 'mcp', usage: '/mcp', help: 'Servidores MCP (interfaz anterior).', route: '/?shell=legacy', legacy: true },
  { name: 'setup', usage: '/setup', help: 'Ir a Ajustes.', route: '/settings' },
  { name: 'usage', usage: '/usage', help: 'Uso de GPU (interfaz anterior).', route: '/?shell=legacy', legacy: true },
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
    return 'Uso: /agents tarea uno | tarea dos | tarea tres — cada parte es un sub-agente. [fichero1, fichero2] delante le da esos ficheros en exclusiva; {modelo} elige su modelo; --review añade un revisor; --serial los ejecuta uno tras otro.';
  }
  if (parts.length > 4) return 'Como mucho 4 sub-agentes por llamada. Junta tareas o repite /agents después.';
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
  const label = d.tasks.map((t) => `${t.model ? `{${t.model}} ` : ''}${t.files?.length ? `[${t.files.join(', ')}] ` : ''}${t.name}`).join(' | ');
  return `🤖 ${d.tasks.length} sub-agente${d.tasks.length === 1 ? '' : 's'}${d.reviewer ? ' + revisor' : ''}${d.parallel ? '' : ' (en serie)'}: ${label}`;
}
