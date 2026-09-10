// Lote 55 item 3 — UX-09: the Spanish-typed spellings of the three
// personal-action commands (/nota, /recordatorio, /evento) resolve to the
// same commands Studio.tsx already knows how to run (note/reminder/event),
// exactly the way `/n` and `/ev` already do — same esbuild-and-import shape
// `studio/checks/commands.check.mjs` uses for this registry.
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-ux09-'));
const out = join(dir, 'commands.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'screens', 'studio', 'commands.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const c = await import(pathToFileURL(out).href);

// The three Spanish spellings resolve to the same command a fluent English
// speaker reaches with /note, /reminder (new — see below), /event — not a
// forked, second definition of "create a note"/"create an event".
assert.equal(c.parseCommand('/nota buy milk').resolved.command.name, 'note');
assert.equal(c.parseCommand('/recordatorio tomorrow 9am dentist').resolved.command.name, 'reminder');
assert.equal(c.parseCommand('/evento tomorrow 14:00 team call').resolved.command.name, 'event');

// /reminder is a real, standalone command (not just an alias table entry):
// it has to be resolvable under its own canonical name too, with its usage
// line intact for /help.
const reminder = c.parseCommand('/reminder').resolved;
assert.equal(reminder.command.name, 'reminder');
assert.match(reminder.command.usage, /\/reminder/);
assert.equal(reminder.args, '');

// Every alias this lote adds is unique across the whole registry — a
// silently-shadowed alias (COMUN rule 3: nothing gets to quietly break)
// would resolve to the WRONG command instead of failing loudly, so check it
// the same way commands.check.mjs's own "no clashes" pass does.
const owners = new Map();
for (const command of c.COMMANDS) {
  for (const key of [command.name, ...(command.aliases ?? [])]) {
    if (owners.has(key)) throw new Error(`"${key}" is claimed by both "${owners.get(key)}" and "${command.name}"`);
    owners.set(key, command.name);
  }
}
assert.equal(owners.get('nota'), 'note');
assert.equal(owners.get('recordatorio'), 'reminder');
assert.equal(owners.get('evento'), 'event');

console.log('ALL OK: /nota, /recordatorio and /evento resolve to note/reminder/event with no alias clash');
