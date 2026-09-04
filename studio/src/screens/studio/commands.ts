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
  { name: 'versions', usage: '/versions', help: 'Versiones anteriores de este chat, con restaurar.' },
  { name: 'temp', usage: '/temp 0.4', help: 'Temperatura de este chat (0–2). Sin valor, la quita.' },
  { name: 'maxtokens', usage: '/maxtokens 2048', help: 'Máximo de tokens de respuesta.' },
  { name: 'topp', usage: '/topp 0.9', help: 'top_p (0–1).' },
  { name: 'think', usage: '/think on|off', help: 'Razonamiento del modelo, si lo soporta.' },
  { name: 'gen', usage: '/gen clave=valor …', help: 'Ajustes de generación: top_k, num_ctx, temperature…' },
  { name: 'remember', usage: '/remember regla', help: 'Guarda una regla permanente en las instrucciones del proyecto (igual que #).' },
  { name: 'export', usage: '/export md|pdf|docx|html|txt|json', help: 'Descarga esta conversación.' },
  { name: 'rename', usage: '/rename nombre', help: 'Renombra la conversación.' },
  { name: 'stats', usage: '/stats', help: 'Tokens y tiempos de esta conversación.' },
  { name: 'projects', usage: '/projects', help: 'Ir a Proyectos.', route: '/projects' },
  { name: 'library', usage: '/library', help: 'Ir a la Biblioteca.', route: '/library' },
  { name: 'gallery', usage: '/gallery', help: 'Ir a las imágenes.', route: '/library?type=imagen' },
  { name: 'tasks', usage: '/tasks', help: 'Ir a Automatizaciones.', route: '/automations' },
  { name: 'activity', usage: '/activity', help: 'Ir a Actividad.', route: '/activity' },
  { name: 'notes', usage: '/notes', help: 'Notas (interfaz anterior).', route: '/notes', legacy: true },
  { name: 'calendar', usage: '/calendar', help: 'Calendario (interfaz anterior).', route: '/calendar', legacy: true },
  { name: 'email', usage: '/email', help: 'Correo (interfaz anterior).', route: '/email', legacy: true },
  { name: 'brain', usage: '/brain', help: 'Memoria (interfaz anterior).', route: '/memory', legacy: true },
  { name: 'research', usage: '/research tema', help: 'Deep research (interfaz anterior).', route: '/?shell=legacy', legacy: true },
  { name: 'compare', usage: '/compare', help: 'Comparar modelos (interfaz anterior).', route: '/?shell=legacy', legacy: true },
  { name: 'mcp', usage: '/mcp', help: 'Servidores MCP (interfaz anterior).', route: '/?shell=legacy', legacy: true },
  { name: 'setup', usage: '/setup', help: 'Ajustes (interfaz anterior).', route: '/?shell=legacy', legacy: true },
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
