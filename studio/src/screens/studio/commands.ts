import type { GenOverrides } from '../../adapters/composer';
import { t, tn } from '../../i18n';

/**
 * Slash commands.
 *
 * The previous interface kept forty-odd names, their aliases, their
 * subcommands and a second flat alias table in 7.300 lines of
 * `static/js/slashCommands.js`, where every handler also built its own HTML
 * and pushed it into the chat as a fake message.
 *
 * Here the registry is data: name, aliases, category, usage, help, and
 * either a `route` (the command goes to a screen) or nothing (Studio runs
 * it, in `Studio.tsx`). Subcommands are a nested table with their own
 * aliases, and the flat aliases the old interface accepted (`/new`,
 * `/web`, `/forget`…) resolve to `parent sub` so no muscle memory breaks.
 *
 * The whole vocabulary resolves. What changes is that the answer comes back
 * as Markdown through the transcript's reader instead of as glued HTML.
 */

export type Category = 'Chat' | 'Agent' | 'Model' | 'Memory' | 'Tools' | 'Settings' | 'Fun';

export interface Sub {
  name: string;
  aliases?: string[];
  usage: string;
  help: string;
}

export interface SlashCommand {
  name: string;
  aliases?: string[];
  category: Category;
  usage: string;
  help: string;
  /** Out of the suggestions until you type enough of its name. */
  hidden?: boolean;
  /** Where the command goes when Studio does not run it itself. */
  route?: string;
  subs?: Sub[];
  /** The sub used when the command is typed bare. */
  defaultSub?: string;
}

export const COMMANDS: SlashCommand[] = [
  /* ── Chat ── */
  { name: 'help', aliases: ['?', 'man', 'commands'], category: 'Chat', usage: '/help [name]', help: 'Every command, by group. With a name, just that one.' },
  {
    name: 'chats',
    aliases: ['chat', 'session', 'sessions'],
    category: 'Chat',
    usage: '/chats info | new | rename | fork | export …',
    help: 'Acts on the conversation: create, rename, fork, archive, export, switch.',
    defaultSub: 'info',
    subs: [
      { name: 'new', aliases: ['create', 'mkdir'], usage: '/chats new [name]', help: 'Starts a new conversation.' },
      { name: 'delete', aliases: ['del', 'rm'], usage: '/chats delete', help: 'Deletes this conversation.' },
      { name: 'archive', aliases: ['tar'], usage: '/chats archive', help: 'Archives this conversation.' },
      { name: 'rename', aliases: ['mv'], usage: '/chats rename name', help: 'Renames this conversation.' },
      { name: 'favorite', aliases: ['pin', 'important', 'star'], usage: '/chats favorite', help: 'Marks it as a favourite.' },
      { name: 'unfavorite', aliases: ['unpin', 'unimportant', 'unstar'], usage: '/chats unfavorite', help: 'Removes the favourite mark.' },
      { name: 'fork', aliases: ['cp'], usage: '/chats fork', help: 'Forks it: a copy with everything said so far.' },
      { name: 'truncate', usage: '/chats truncate N', help: 'Keeps the first N messages and deletes the rest.' },
      { name: 'switch', aliases: ['goto', 'cd'], usage: '/chats switch name', help: 'Goes to another conversation by name or id.' },
      { name: 'sort', usage: '/chats sort', help: 'Sorts the conversations into folders with the model.' },
      { name: 'info', aliases: ['stat'], usage: '/chats info', help: 'Tokens, timings and model of this conversation.' },
      { name: 'clear', usage: '/chats clear', help: 'Clears the screen; the conversation stays on the server.' },
      { name: 'export', aliases: ['cat'], usage: '/chats export md|pdf|docx|html|txt|json', help: 'Downloads this conversation.' },
      { name: 'export-all', aliases: ['zip'], usage: '/chats export-all [folder] [fmt]', help: 'Downloads a whole folder as a .zip.' },
    ],
  },
  { name: 'rename', category: 'Chat', usage: '/rename name', help: 'Renames the conversation.' },
  { name: 'fork', category: 'Chat', usage: '/fork', help: 'Forks the conversation: a copy with everything said so far.' },
  { name: 'truncate', category: 'Chat', usage: '/truncate N', help: 'Keeps the first N messages and deletes the rest (a version remains).' },
  { name: 'export', category: 'Chat', usage: '/export md|pdf|docx|html|txt|json', help: 'Downloads this conversation.' },
  { name: 'compact', category: 'Chat', usage: '/compact', help: 'Summarises the old messages into one.' },
  { name: 'versions', aliases: ['history-versions', 'undo-edit'], category: 'Chat', usage: '/versions', help: 'Previous versions of this chat (what an edit or a regenerate removed).' },
  { name: 'restore', category: 'Chat', usage: '/restore ID', help: 'Restores a version from /versions.' },
  { name: 'stats', aliases: ['df'], category: 'Chat', usage: '/stats', help: 'Tokens and timings of this conversation, and what the database holds.' },
  { name: 'incognito', category: 'Chat', usage: '/incognito [on|off]', help: 'Nobody mode: nothing is saved and the memory stays closed.' },
  { name: 'tts', category: 'Chat', usage: '/tts', help: 'Reads the last reply aloud.' },
  { name: 'find', aliases: ['search-history'], category: 'Chat', usage: '/find text', help: 'Searches every conversation.' },
  { name: 'search', aliases: ['websearch'], category: 'Chat', usage: '/search question', help: 'Asks with web search on, whatever the chip says.' },

  /* ── Agent ── */
  {
    name: 'agents',
    aliases: ['swarm', 'delegate'],
    category: 'Agent',
    usage: '/agents task one | task two [--review] [--serial]',
    help: 'Delegates each part to a sub-agent (up to 4). [f1, f2] before a task gives it those files exclusively; {model} picks its model.',
  },
  { name: 'workspace', aliases: ['ws'], category: 'Agent', usage: '/workspace [path | clear | pick]', help: 'The folder the agent works in.' },
  { name: 'checkpoints', aliases: ['checkpoint', 'snapshots'], category: 'Agent', usage: '/checkpoints', help: 'Checkpoints of the working folder (one per turn with changes).' },
  { name: 'remember', aliases: ['recuerda', 'note-to-agent'], category: 'Agent', usage: '/remember rule', help: 'Saves a standing rule in the project instructions (same as #).' },
  { name: 'agentsmd', aliases: ['agents-md', 'instructions'], category: 'Agent', usage: '/agentsmd [write]', help: 'Drafts an AGENTS.md for the working folder; "write" saves it.' },
  { name: 'backup', aliases: ['backups'], category: 'Agent', usage: '/backup [now | verify N]', help: 'Verified snapshots of the whole data folder: list, take one, or check one would restore.' },
  { name: 'scorecard', aliases: ['models-score', 'score'], category: 'Agent', usage: '/scorecard [days] [here]', help: 'Per-model reliability of your agent turns: verified rate, questions, tests, time.' },
  { name: 'researchfit', aliases: ['deepfit', 'researchpreset'], category: 'Agent', usage: '/researchfit [apply]', help: 'Deep Research settings matched to this machine, and what would make a run come back empty.' },
  { name: 'project', aliases: ['proj'], category: 'Agent', usage: '/project [list]', help: 'The project of this conversation: folder, instructions and memory.' },
  {
    name: 'toggle',
    aliases: ['t'],
    category: 'Agent',
    usage: '/toggle web | bash | research | doc | sidebar',
    help: 'Flips one of the composer switches.',
    defaultSub: '_show',
    subs: [
      { name: 'web', aliases: ['search', 'w'], usage: '/toggle web', help: 'Web search on or off.' },
      { name: 'bash', aliases: ['b', 'shell', 'terminal'], usage: '/toggle bash', help: 'Terminal on or off.' },
      { name: 'research', aliases: ['r'], usage: '/toggle research', help: 'Deep Research on or off.' },
      { name: 'doc', usage: '/toggle doc', help: 'The document panel on or off.' },
      { name: 'plan', usage: '/toggle plan', help: 'Proposal mode on or off.' },
      { name: 'rag', usage: '/toggle rag', help: 'Your indexed documents on or off.' },
      { name: 'sidebar', aliases: ['sb'], usage: '/toggle sidebar', help: 'Cycles the sidebar: full, rail, hidden.' },
      { name: '_show', aliases: ['status'], usage: '/toggle', help: 'Shows every switch as it stands.' },
    ],
  },
  { name: 'open', aliases: ['show'], category: 'Agent', usage: '/open path', help: 'Opens a file from the working folder in the side panel.' },
  { name: 'browser', category: 'Agent', usage: '/browser', help: 'Opens the panel with what the agent sees in the browser.' },
  { name: 'doc', category: 'Agent', usage: '/doc [title]', help: 'Opens the document panel (with a title, creates a new one).' },
  { name: 'sh', aliases: ['exec', 'run'], category: 'Agent', usage: '/sh command', help: 'Runs a shell command in the working folder and shows the output.' },

  /* ── Model ── */
  { name: 'models', category: 'Model', usage: '/models', help: 'Pick a model.' },
  { name: 'model', category: 'Model', usage: '/model [name]', help: 'The model of this chat; with a name, switches to it.' },
  { name: 'temp', aliases: ['temperature'], category: 'Model', usage: '/temp 0.4', help: 'Temperature of this chat (0-2). No value removes it.' },
  { name: 'maxtokens', aliases: ['max_tokens'], category: 'Model', usage: '/maxtokens 2048', help: 'Maximum reply tokens.' },
  { name: 'topp', aliases: ['top_p'], category: 'Model', usage: '/topp 0.9', help: 'top_p (0-1).' },
  { name: 'think', aliases: ['thinking'], category: 'Model', usage: '/think on|off', help: "The model's reasoning, if it supports it." },
  { name: 'gen', aliases: ['model-settings'], category: 'Model', usage: '/gen key=value …', help: 'Generation settings: top_k, num_ctx, temperature… No arguments clears them.' },
  { name: 'preset', category: 'Model', usage: '/preset [name|off]', help: 'Preset or persona (system prompt). Without a name it opens the list.' },
  { name: 'usage', aliases: ['sys', 'gpu'], category: 'Model', usage: '/usage [on|off]', help: 'Shows or hides the live usage (GPU, VRAM, model, RAM) in the header.' },
  { name: 'ping', aliases: ['pong'], category: 'Model', usage: '/ping', help: 'Checks that the model endpoints answer.' },
  { name: 'probe', aliases: ['test-models'], category: 'Model', usage: '/probe [endpoint]', help: 'Asks each model for one token to see which really answer.' },

  /* ── Memory ── */
  {
    name: 'memory',
    aliases: ['m'],
    category: 'Memory',
    usage: '/memory list | add text | delete id | search q',
    help: 'The memories the assistant keeps about you.',
    defaultSub: 'list',
    subs: [
      { name: 'list', aliases: ['ls'], usage: '/memory list', help: 'Lists the memories.' },
      { name: 'add', aliases: ['echo'], usage: '/memory add text', help: 'Saves a memory.' },
      { name: 'delete', aliases: ['del', 'rm', 'forget'], usage: '/memory delete id', help: 'Deletes a memory by id.' },
      { name: 'search', aliases: ['grep'], usage: '/memory search q', help: 'Searches the memories.' },
    ],
  },
  { name: 'note', aliases: ['n'], category: 'Memory', usage: '/note text', help: 'Saves a quick note in Notes.' },
  { name: 'skills', aliases: ['skill'], category: 'Memory', usage: '/skills [query]', help: 'The skills; with a query, the ones that match.' },
  { name: 'reload-skills', aliases: ['reload_skills'], category: 'Memory', usage: '/reload-skills', help: 'Re-reads the skills folder.' },
  {
    name: 'rag',
    category: 'Memory',
    usage: '/rag list | add path | remove path',
    help: 'The folders indexed for your documents.',
    defaultSub: 'list',
    subs: [
      { name: 'list', aliases: ['ls'], usage: '/rag list', help: 'Lists the indexed folders.' },
      { name: 'add', usage: '/rag add path', help: 'Indexes a folder.' },
      { name: 'remove', aliases: ['rm'], usage: '/rag remove path', help: 'Stops indexing a folder.' },
    ],
  },

  /* ── Tools ── */
  { name: 'projects', category: 'Tools', usage: '/projects', help: 'Go to Projects.', route: '/projects' },
  { name: 'library', aliases: ['docs', 'documents'], category: 'Tools', usage: '/library', help: 'Go to the Library.', route: '/library' },
  { name: 'gallery', aliases: ['photos'], category: 'Tools', usage: '/gallery', help: 'Go to the images.', route: '/library?type=imagen' },
  { name: 'tasks', category: 'Tools', usage: '/tasks', help: 'Go to Automations.', route: '/automations' },
  { name: 'activity', category: 'Tools', usage: '/activity', help: 'Go to Activity.', route: '/activity' },
  { name: 'notes', category: 'Tools', usage: '/notes', help: 'Go to Notes.', route: '/notes' },
  { name: 'calendar', category: 'Tools', usage: '/calendar', help: 'Go to the Calendar.', route: '/calendar' },
  { name: 'email', aliases: ['mail', 'inbox'], category: 'Tools', usage: '/email', help: 'Go to Mail.', route: '/email' },
  { name: 'brain', aliases: ['memories'], category: 'Tools', usage: '/brain', help: 'Go to the Memory.', route: '/memory' },
  { name: 'workers', category: 'Tools', usage: '/workers', help: 'Go to Agents: the Workers board.', route: '/agents' },
  { name: 'experts', category: 'Tools', usage: '/experts', help: 'Go to Agents: the Experts.', route: '/agents?t=experts' },
  { name: 'tournament', category: 'Tools', usage: '/tournament', help: 'Go to Agents: the Tournament.', route: '/agents?t=tournament' },
  { name: 'provenance', category: 'Tools', usage: '/provenance', help: 'Go to Memory: where each thing came from.', route: '/memory?t=provenance' },
  { name: 'research', category: 'Tools', usage: '/research [question]', help: 'Deep Research: several rounds of search and reading, then a report with sources.', route: '/research' },
  { name: 'group', category: 'Tools', usage: '/group', help: 'Group chat: several models (with personas) in one conversation.', route: '/group' },
  { name: 'compare', category: 'Tools', usage: '/compare', help: 'Compare: the same prompt to several models side by side, blind, with a vote.', route: '/compare' },
  { name: 'cookbook', aliases: ['cook'], category: 'Tools', usage: '/cookbook', help: 'Cookbook: what fits this machine, download and launch local models.', route: '/cookbook' },
  { name: 'todo', aliases: ['td'], category: 'Tools', usage: '/todo text', help: 'Adds a task to Notes.' },
  { name: 'event', aliases: ['ev'], category: 'Tools', usage: '/event tomorrow 14:00 Team call', help: 'Creates a calendar event in your words.' },

  /* ── Settings ── */
  { name: 'settings', aliases: ['cfg', 'preferences', 'config'], category: 'Settings', usage: '/settings [tab]', help: 'Go to Settings.', route: '/settings' },
  { name: 'mcp', category: 'Settings', usage: '/mcp', help: 'MCP servers (Settings, Integrations).', route: '/settings?s=integrations' },
  { name: 'theme', category: 'Settings', usage: '/theme [dark|light|system|name]', help: 'Appearance: the mode, or a saved theme by name.' },
  { name: 'shortcuts', aliases: ['keys', 'keybinds', 'bind'], category: 'Settings', usage: '/shortcuts', help: 'The keyboard shortcuts.' },
  {
    name: 'setup',
    aliases: ['su', 'seutp'],
    category: 'Settings',
    usage: '/setup local URL · /setup groq KEY',
    help: 'Adds a local server or an API key.',
    subs: [
      { name: 'local', usage: '/setup local http://localhost:8000/v1', help: 'A local server (vLLM, LM Studio, llama.cpp, Ollama).' },
      { name: 'openai', usage: '/setup openai sk-proj-…', help: 'OpenAI.' },
      { name: 'anthropic', usage: '/setup anthropic sk-ant-…', help: 'Anthropic.' },
      { name: 'deepseek', usage: '/setup deepseek sk-…', help: 'DeepSeek.' },
      { name: 'openrouter', usage: '/setup openrouter sk-or-…', help: 'OpenRouter.' },
      { name: 'groq', usage: '/setup groq gsk_…', help: 'Groq.' },
      { name: 'gemini', aliases: ['google'], usage: '/setup gemini AIza…', help: 'Google Gemini.' },
      { name: 'xai', aliases: ['grok'], usage: '/setup xai xai-…', help: 'xAI (Grok).' },
      { name: 'ollama', usage: '/setup ollama KEY', help: 'Ollama Cloud.' },
      { name: 'copilot', aliases: ['github'], usage: '/setup copilot', help: 'GitHub Copilot.' },
      { name: 'chatgpt-subscription', aliases: ['codex'], usage: '/setup chatgpt-subscription', help: 'ChatGPT subscription.' },
      { name: 'endpoint', usage: '/setup endpoint', help: 'Opens the endpoint manager.' },
    ],
  },

  /* ── Tours ── */
  { name: 'demo', aliases: ['tour'], category: 'Tools', usage: '/demo', help: 'The whole guided tour, screen by screen.' },
  { name: 'tour-compare', aliases: ['compare-tour'], category: 'Tools', hidden: true, usage: '/tour-compare', help: 'Tour: comparing models.' },
  { name: 'tour-cookbook', aliases: ['cookbook-tour'], category: 'Tools', hidden: true, usage: '/tour-cookbook', help: 'Tour: hardware, downloads and serving.' },
  { name: 'tour-research', aliases: ['research-tour'], category: 'Tools', hidden: true, usage: '/tour-research', help: 'Tour: Deep Research.' },
  { name: 'tour-library', aliases: ['library-tour', 'tour-doc', 'tour-document', 'doc-tour', 'document-tour'], category: 'Tools', hidden: true, usage: '/tour-library', help: 'Tour: the Library and the document editor.' },
  { name: 'tour-theme', aliases: ['theme-tour'], category: 'Tools', hidden: true, usage: '/tour-theme', help: 'Tour: the appearance editor.' },
  { name: 'tour-settings', aliases: ['tour-setting', 'settings-tour'], category: 'Tools', hidden: true, usage: '/tour-settings', help: 'Tour: models, integrations, appearance.' },
  { name: 'tour-gallery', aliases: ['gallery-tour'], category: 'Tools', hidden: true, usage: '/tour-gallery', help: 'Tour: photos, albums, the editor.' },
  { name: 'tour-brain', aliases: ['brain-tour', 'tour-memory', 'memory-tour'], category: 'Tools', hidden: true, usage: '/tour-brain', help: 'Tour: memories, rules, provenance.' },
  { name: 'tour-task-1', aliases: ['tour-task', 'tour-tasks', 'tour-tasks-1', 'tasks-tour', 'tasks-tour-1'], category: 'Tools', hidden: true, usage: '/tour-task-1', help: 'Tour: what an automation is.' },
  { name: 'tour-task-2', aliases: ['tour-tasks-2', 'tasks-tour-2'], category: 'Tools', hidden: true, usage: '/tour-task-2', help: 'Tour: making an automation.' },
  { name: 'tours', aliases: ['retour'], category: 'Tools', hidden: true, usage: '/tours [reset]', help: 'The tours there are; "reset" makes them offer themselves again.' },

  /* ── Fun (out of the list until you type them) ── */
  { name: 'flip', aliases: ['coin'], category: 'Fun', hidden: true, usage: '/flip', help: 'Flips a coin.' },
  { name: 'roll', aliases: ['dice'], category: 'Fun', hidden: true, usage: '/roll [NdN]', help: 'Rolls dice.' },
  { name: '8ball', aliases: ['8-ball'], category: 'Fun', hidden: true, usage: '/8ball question', help: 'Asks the eight ball.' },
  { name: 'fortune', aliases: ['cookie'], category: 'Fun', hidden: true, usage: '/fortune', help: 'A fortune cookie.' },
  { name: 'odyssey', aliases: ['homer', 'quote'], category: 'Fun', hidden: true, usage: '/odyssey', help: 'A line from the Odyssey.' },
  { name: 'ascii', aliases: ['banner'], category: 'Fun', hidden: true, usage: '/ascii [text]', help: 'Writes it big.' },
  { name: 'matrix', category: 'Fun', hidden: true, usage: '/matrix', help: 'Rain.' },
  { name: 'cowsay', aliases: ['moo', 'say'], category: 'Fun', hidden: true, usage: '/cowsay [text]', help: 'A cow says it.' },
  { name: 'wisdom', aliases: ['inspire'], category: 'Fun', hidden: true, usage: '/wisdom', help: 'Someone else already said it better.' },
  { name: 'uptime', category: 'Fun', hidden: true, usage: '/uptime', help: 'How long this session has been open.' },
  { name: 'color', aliases: ['colour'], category: 'Fun', hidden: true, usage: '/color [hex]', help: 'A colour swatch you can copy.' },
];

export const CATEGORIES: Category[] = ['Chat', 'Agent', 'Model', 'Memory', 'Tools', 'Settings', 'Fun'];

/** Flat names the previous interface accepted on their own. */
const FLAT: Record<string, string> = {
  new: 'chats new',
  create: 'chats new',
  mkdir: 'chats new',
  delete: 'chats delete',
  del: 'chats delete',
  rm: 'chats delete',
  archive: 'chats archive',
  tar: 'chats archive',
  mv: 'chats rename',
  favorite: 'chats favorite',
  important: 'chats favorite',
  star: 'chats favorite',
  pin: 'chats favorite',
  unfavorite: 'chats unfavorite',
  unimportant: 'chats unfavorite',
  unstar: 'chats unfavorite',
  unpin: 'chats unfavorite',
  cp: 'chats fork',
  switch: 'chats switch',
  goto: 'chats switch',
  cd: 'chats switch',
  sort: 'chats sort',
  info: 'chats info',
  stat: 'chats info',
  clear: 'chats clear',
  cat: 'chats export',
  'export-all': 'chats export-all',
  zip: 'chats export-all',
  web: 'toggle web',
  bash: 'toggle bash',
  terminal: 'toggle bash',
  sidebar: 'toggle sidebar',
  status: 'toggle _show',
  plan: 'toggle plan',
  forget: 'memory delete',
};

const byName = new Map<string, SlashCommand>();
for (const command of COMMANDS) {
  byName.set(command.name, command);
  for (const alias of command.aliases ?? []) if (!byName.has(alias)) byName.set(alias, command);
}

function findSub(command: SlashCommand, word: string): Sub | undefined {
  const needle = word.toLowerCase();
  return command.subs?.find((s) => s.name === needle || (s.aliases ?? []).includes(needle));
}

export interface Resolved {
  command: SlashCommand;
  sub?: Sub;
  /** `chats.new`, or `chats` when the command has no subcommands. */
  path: string;
  args: string;
}

/** `/chats mv Nombre` → the chats command, its rename sub, and `Nombre`. */
export function resolveCommand(name: string, rest: string): Resolved | null {
  const key = name.toLowerCase();
  const flat = FLAT[key];
  if (flat) {
    const [parent, sub] = flat.split(' ');
    const command = byName.get(parent);
    const found = command && findSub(command, sub);
    if (command && found) return { command, sub: found, path: `${command.name}.${found.name}`, args: rest.trim() };
  }
  const command = byName.get(key);
  if (!command) return null;
  if (!command.subs) return { command, path: command.name, args: rest.trim() };
  const match = /^(\S+)\s*([\s\S]*)$/.exec(rest.trim());
  const sub = match ? findSub(command, match[1]) : undefined;
  if (sub) return { command, sub, path: `${command.name}.${sub.name}`, args: (match?.[2] ?? '').trim() };
  const fallback = command.defaultSub ? findSub(command, command.defaultSub) : undefined;
  if (fallback && !rest.trim()) return { command, sub: fallback, path: `${command.name}.${fallback.name}`, args: '' };
  // A word we do not know: hand the whole thing to the command, which either
  // uses it (`/setup local URL`) or explains itself.
  return { command, path: command.name, args: rest.trim() };
}

export interface ParsedCommand {
  resolved: Resolved | null;
  name: string;
  args: string;
}

export function parseCommand(text: string): ParsedCommand | null {
  const match = /^\/([a-z0-9?_-]+)(?:\s+([\s\S]*))?$/i.exec(text.trim());
  if (!match) return null;
  const name = match[1].toLowerCase();
  const args = (match[2] ?? '').trim();
  return { resolved: resolveCommand(name, args), name, args };
}

export interface Suggestion {
  /** What goes into the composer, without the slash. */
  insert: string;
  usage: string;
  help: string;
  category: Category;
}

/**
 * Suggestions for what has been typed. Bare `/` shows the everyday commands
 * grouped; a prefix matches names, aliases and `parent sub` pairs, so
 * `/chats ex` finds `chats export` and `/for` finds both `/fortune` and the
 * flat `/forget`.
 */
export function matchCommands(prefix: string, limit = 9): Suggestion[] {
  const needle = prefix.replace(/^\//, '').toLowerCase().trim();
  const out: Suggestion[] = [];
  const seen = new Set<string>();
  const push = (insert: string, usage: string, help: string, category: Category) => {
    if (seen.has(insert) || out.length >= limit) return;
    seen.add(insert);
    out.push({ insert, usage, help, category });
  };
  const words = needle.split(/\s+/);
  // `/<parent> <partial sub>`
  if (words.length > 1) {
    const command = byName.get(words[0]);
    if (command?.subs) {
      const tail = words.slice(1).join(' ');
      for (const sub of command.subs) {
        if (sub.name.startsWith('_')) continue;
        if (sub.name.startsWith(tail) || (sub.aliases ?? []).some((a) => a.startsWith(tail))) {
          push(`${command.name} ${sub.name}`, sub.usage, sub.help, command.category);
        }
      }
      return out;
    }
  }
  const hit = (command: SlashCommand) =>
    command.name.startsWith(needle) || (command.aliases ?? []).some((a) => a.startsWith(needle));
  for (const command of COMMANDS) {
    if (needle === '' ? command.hidden : !hit(command)) continue;
    push(command.subs && command.defaultSub ? `${command.name} ` : command.name, command.usage, command.help, command.category);
  }
  // Flat aliases only surface once you have typed something.
  if (needle) {
    for (const [flat, target] of Object.entries(FLAT)) {
      if (!flat.startsWith(needle)) continue;
      const [parent, subName] = target.split(' ');
      const command = byName.get(parent);
      const sub = command && findSub(command, subName);
      if (command && sub) push(`${command.name} ${sub.name}`, `/${flat}`, sub.help, command.category);
    }
  }
  return out;
}

/** A `|` inside a usage line would end the table cell. */
const cell = (text: string) => text.replace(/\|/g, '\\|');

/** The `/help` answer, as Markdown for the transcript's reader. */
export function helpMarkdown(query?: string): string {
  const needle = (query ?? '').replace(/^\//, '').toLowerCase().trim();
  if (needle) {
    const found = resolveCommand(needle.split(/\s+/)[0], needle.split(/\s+/).slice(1).join(' '));
    if (!found) return t('I do not know /{name}. Type /help to see the commands.', { name: needle });
    const { command } = found;
    const lines = [`### /${command.name}`, '', t(command.help), '', `\`${command.usage}\``];
    if (command.aliases?.length) lines.push('', `${t('Also')}: ${command.aliases.map((a) => `\`/${a}\``).join(' · ')}`);
    if (command.subs?.length) {
      lines.push('', `| ${t('Subcommand')} | ${t('What it does')} |`, '| --- | --- |');
      for (const sub of command.subs) {
        if (sub.name.startsWith('_')) continue;
        const names = [sub.name, ...(sub.aliases ?? [])].map((n) => `\`${n}\``).join(' ');
        lines.push(`| ${names} | ${cell(t(sub.help))} |`);
      }
    }
    return lines.join('\n');
  }
  const lines: string[] = [`### ${tn(COMMANDS.length, '{n} command', '{n} commands')}`];
  for (const category of CATEGORIES) {
    const group = COMMANDS.filter((c) => c.category === category);
    if (!group.length) continue;
    lines.push('', `#### ${t(category)}`, '', `| ${t('Command')} | ${t('What it does')} |`, '| --- | --- |');
    for (const command of group) {
      lines.push(`| \`${cell(command.usage)}\` | ${cell(t(command.help))} |`);
    }
  }
  lines.push('', t('`/help name` explains one command, its aliases and its subcommands.'));
  return lines.join('\n');
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
