// The slash-command registry (screens/studio/commands.ts) and the hidden
// commands (lib/fun.ts). Bundled with esbuild on the fly; run by
// tests/test_studio_commands_js.py, or by hand:
//   node studio/checks/commands.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-cmd-'));

async function load(entry, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', entry)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const c = await load(join('screens', 'studio', 'commands.ts'), 'commands.mjs');
const fun = await load(join('lib', 'fun.ts'), 'fun.mjs');
const tours = await load(join('lib', 'tours.ts'), 'tours.mjs');

let failed = 0;
const assert = (cond, msg) => {
  if (!cond) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// ── The registry is well formed ──
{
  const names = new Set();
  let clashes = 0;
  for (const command of c.COMMANDS) {
    if (names.has(command.name)) clashes += 1;
    names.add(command.name);
    for (const alias of command.aliases ?? []) {
      // An alias may shadow nothing: the first definition wins, which is
      // what resolveCommand does, but a silent clash is a bug worth seeing.
      if (names.has(alias)) clashes += 1;
      names.add(alias);
    }
  }
  assert(clashes === 0, `no name or alias is defined twice (${clashes})`);
  assert(c.COMMANDS.every((x) => x.usage && x.help && x.category), 'every command has usage, help and category');
  assert(c.COMMANDS.every((x) => c.CATEGORIES.includes(x.category)), 'every category is one of the declared ones');
  assert(
    c.COMMANDS.every((x) => !x.subs || x.subs.every((s) => s.usage && s.help)),
    'every subcommand has usage and help',
  );
  assert(
    c.COMMANDS.every((x) => !x.defaultSub || (x.subs ?? []).some((s) => s.name === x.defaultSub)),
    'a defaultSub always exists among the subs',
  );
  assert(c.COMMANDS.filter((x) => x.route).every((x) => x.route.startsWith('/')), 'every route is a Studio path');
}

// ── Resolution ──
{
  const r = (name, rest = '') => c.resolveCommand(name, rest);
  assert(r('help')?.path === 'help', 'a bare command');
  assert(r('temperature')?.command.name === 'temp', 'an alias resolves to its command');
  assert(r('chats', 'mv Nombre')?.path === 'chats.rename', 'a subcommand alias');
  assert(r('chats', 'mv Nombre')?.args === 'Nombre', 'the arguments survive the subcommand');
  assert(r('chats')?.path === 'chats.info', 'a bare command with subs falls to its default');
  assert(r('new', 'Idea')?.path === 'chats.new' && r('new', 'Idea').args === 'Idea', 'the flat alias /new');
  assert(r('web')?.path === 'toggle.web', 'the flat alias /web');
  assert(r('forget', 'abc')?.path === 'memory.delete', 'the flat alias /forget');
  assert(r('status')?.path === 'toggle._show', 'the flat alias /status');
  assert(r('setup', 'groq gsk_x')?.path === 'setup.groq', 'a provider subcommand');
  assert(r('setup', 'groq gsk_x')?.args === 'gsk_x', 'and its argument');
  assert(r('workspace', 'D:\\algo')?.path === 'workspace' && r('workspace', 'D:\\algo').args === 'D:\\algo', 'a command with no subs keeps the whole tail');
  assert(r('nope') === null, 'an unknown name resolves to nothing');
  assert(r('8ball', 'seguro?')?.path === '8ball', 'a name that starts with a digit');
  // Every flat alias points somewhere real.
  const flats = ['new', 'create', 'mkdir', 'delete', 'del', 'rm', 'archive', 'tar', 'mv', 'favorite', 'star', 'pin', 'unfavorite', 'unstar', 'unpin', 'cp', 'switch', 'goto', 'cd', 'sort', 'info', 'stat', 'clear', 'cat', 'export-all', 'zip', 'web', 'bash', 'terminal', 'sidebar', 'status', 'plan', 'forget'];
  assert(flats.every((f) => r(f) !== null), 'every flat alias resolves');
}

// ── parseCommand ──
{
  assert(c.parseCommand('hola') === null, 'ordinary text is not a command');
  assert(c.parseCommand('/help')?.resolved?.path === 'help', 'a slash line parses');
  assert(c.parseCommand('  /temp 0.4  ')?.args === '0.4', 'surrounding space does not matter');
  assert(c.parseCommand('/home/user/file.txt') === null, 'a unix path is not a command');
  assert(c.parseCommand('/nope')?.resolved === null, 'an unknown command parses but resolves to nothing');
}

// ── Suggestions ──
{
  const insert = (prefix) => c.matchCommands(prefix).map((s) => s.insert);
  assert(insert('/').length > 0 && !insert('/').includes('flip'), 'a bare slash suggests the everyday commands, not the hidden ones');
  assert(insert('/fli').includes('flip'), 'a hidden command appears once you type it');
  assert(insert('/chats ex').join(',') === 'chats export,chats export-all', `subcommand suggestions: ${insert('/chats ex').join(',')}`);
  assert(insert('/temp').includes('temp'), 'a name prefix');
  assert(c.matchCommands('/help')[0].category === 'Chat', 'a suggestion carries its category');
  assert(c.matchCommands('/', 3).length === 3, 'the limit is respected');

  // A parent typed exactly answers with what it can do. `/setup` on its own
  // is a question — "which providers?" — and one row repeating the word is
  // not an answer.
  const setup = insert('/setup');
  assert(setup.length > 1, `an exact parent expands its subcommands: ${setup.join(',')}`);
  assert(setup.includes('setup local') && setup.includes('setup groq'), 'and the list is the providers');
  const all = c.matchCommands('/setup', 50).map((x) => x.insert);
  assert(all.includes('setup copilot') && all.includes('setup chatgpt-subscription'), 'including the ones you sign in to');
  assert(setup.every((x) => x.startsWith('setup ')), 'every row is a real subcommand, not the bare parent');
  const capped = c.matchCommands('/setup', 3).map((x) => x.insert);
  assert(capped.length === 3, 'the expansion respects the limit');
  // A command with no subcommands is unaffected.
  assert(insert('/temp').includes('temp'), 'a parentless command still suggests itself');
}

// ── /help ──
{
  const all = c.helpMarkdown();
  assert(all.includes('| `/help [name]` |'), 'the help table lists a command');
  assert(all.includes('\\|'), 'a pipe inside a usage line is escaped so the table survives');
  assert(c.CATEGORIES.every((cat) => all.includes(`#### ${cat}`)), 'every category has a heading');
  const one = c.helpMarkdown('chats');
  assert(one.startsWith('### /chats'), 'help for one command');
  assert(one.includes('`/chat`'), 'and it lists the aliases');
  assert(one.includes('`rename` `mv`'), 'and the subcommands with theirs');
}

// ── Generation knobs and delegation (unchanged, still guarded) ──
{
  assert(c.genFromArgs('temp', '0.4', {}).temperature === 0.4, '/temp');
  assert(c.genFromArgs('temp', '9', {}).temperature === 2, '/temp is clamped');
  assert(c.genFromArgs('temp', '', { temperature: 1 }).temperature === undefined, '/temp with no value clears it');
  assert(c.genFromArgs('gen', '', { top_k: 5 }).top_k === undefined, '/gen with no arguments clears everything');
  assert(c.genFromArgs('gen', 'top_k=40 num_ctx=8192', {}).top_k === 40, '/gen key=value');
  const d = c.parseDelegation('[a.py] uno | {qwen} dos --review');
  assert(typeof d !== 'string' && d.tasks.length === 2 && d.reviewer && d.tasks[0].files[0] === 'a.py' && d.tasks[1].model === 'qwen', '/agents parses files, model and --review');
  assert(typeof c.parseDelegation('a|b|c|d|e') === 'string', 'more than four sub-agents is refused');
}

// ── The hidden ones are pure ──
{
  const { values, sides } = fun.roll('3d20');
  assert(values.length === 3 && sides === 20 && values.every((v) => v >= 1 && v <= 20), '3d20');
  assert(fun.roll('999d999').values.length === 20, 'at most 20 dice');
  assert(fun.roll('').sides === 6, 'a bare /roll is a d6');
  assert(fun.banner('AB').split('\n').length === 5, 'the banner is five rows');
  assert(fun.banner('~').includes('#'), 'an unknown glyph falls back to a question mark');
  assert(fun.cowsay('hola').includes('< hola >'), 'the cow says it');
  assert(/^#[0-9a-f]{6}$/.test(fun.hexColour('f0a')), 'a three-digit hex expands');
  assert(/^#[0-9a-f]{6}$/.test(fun.hexColour('nonsense')), 'nonsense becomes a random colour');
  assert(fun.since(Date.now() - 3_723_000).startsWith('1h 2m'), `uptime reads as time: ${fun.since(Date.now() - 3_723_000)}`);
  const ball = fun.egg('8ball', '¿sí?', Date.now());
  assert(ball.text && ['yes', 'maybe', 'no'].includes(ball.tone), 'the eight ball answers with a tone');
  assert(fun.egg('flip', '', Date.now()).text.length === 1, 'the coin has one face');
}

// ── Tours ──
{
  const ids = tours.TOURS.map((x) => x.id);
  assert(new Set(ids).size === ids.length, 'no tour id is repeated');
  assert(tours.TOURS.every((x) => x.steps.length > 0 && x.title), 'every tour has a title and at least one step');
  assert(tours.TOURS.every((x) => x.steps.every((s) => s.target && s.text)), 'every step points at something and says something');
  assert(tours.TOURS.every((x) => x.steps[0].route), 'every tour says where it starts');
  assert(tours.TOURS.every((x) => x.route.startsWith('/')), 'every tour route is a path');
  assert(ids.every((id) => c.resolveCommand(id, '') !== null), 'every tour has its own /command');
  assert(tours.tourById('tour-brain')?.route === '/memory', 'a tour resolves by id');
  assert(tours.tourById('nope') === null, 'an unknown tour is nothing');
  assert(tours.tourForPath('/compare')?.id === 'tour-compare', 'a path finds its tour');
  assert(tours.tourForPath('/library', '?type=imagen')?.id === 'tour-gallery', 'the gallery is a library path with a query');
  assert(tours.tourForPath('/settings', '?s=appearance')?.id === 'tour-theme', 'the appearance editor has its own tour');
  assert(tours.tourForPath('/studio') === null, 'the whole-product tour never offers itself');
  assert(tours.tourForPath('/nowhere') === null, 'a path with no tour offers nothing');
  // Placement: below when it fits, above when it does not, then beside.
  const card = { width: 300, height: 140 };
  const view = { width: 1440, height: 900 };
  assert(tours.placeTooltip({ top: 100, left: 600, width: 200, height: 40 }, card, view).side === 'below', 'below when it fits');
  assert(tours.placeTooltip({ top: 700, left: 600, width: 200, height: 40 }, card, view).side === 'above', 'above when below does not fit');
  const squeezed = tours.placeTooltip({ top: 20, left: 20, width: 200, height: 600 }, card, { width: 1440, height: 700 });
  assert(squeezed.side === 'right', `beside when neither fits (${squeezed.side})`);
  const clamped = tours.placeTooltip({ top: 100, left: 0, width: 40, height: 40 }, card, view);
  assert(clamped.left >= 10, 'never off the left edge');
  const clampedRight = tours.placeTooltip({ top: 100, left: 1400, width: 40, height: 40 }, card, view);
  assert(clampedRight.left + card.width <= view.width - 10 + 1, 'never off the right edge');
}

// ── Every command that Studio runs itself has a branch ──
// A missing `case` is silent: the command resolves, nothing happens, and
// only trying it in a browser finds out. So the source is read here.
{
  const source = readFileSync(join(root, 'studio', 'src', 'screens', 'Studio.tsx'), 'utf8');
  const handled = new Set([...source.matchAll(/case '([a-z0-9._-]+)':/g)].map((m) => m[1]));
  const wanted = [];
  for (const command of c.COMMANDS) {
    if (command.route) continue;
    // `/setup <provider>` collapses onto one branch on purpose.
    if (command.name === 'setup') {
      wanted.push('setup');
      continue;
    }
    if (command.subs) for (const sub of command.subs) wanted.push(`${command.name}.${sub.name}`);
    else wanted.push(command.name);
  }
  const missing = wanted.filter((path) => !handled.has(path));
  assert(missing.length === 0, `every command Studio runs has a branch (missing: ${missing.join(', ') || 'none'})`);
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
