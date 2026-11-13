// The skills a connector carries: the adapter addresses the connector's own
// server id, reads one skill by slug (and file), and the skill library rows
// keep the connectors that also serve them.
//
// Run by tests/test_connector_skills_js.py, or by hand:
//   node studio/checks/connector-skills.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-conn-skills-'));
const load = async (entry, name) => {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, entry)], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
};
const c = await load('studio/src/adapters/connectors.ts', 'connectors.mjs');
const lib = await load('studio/src/adapters/skillLibrary.ts', 'library.mjs');

const seen = [];
globalThis.fetch = async (url) => {
  seen.push(String(url));
  let body = { skills: [] };
  if (String(url).includes('/api/skills/library')) {
    body = { count: 1, skills: [{ slug: 'api-design', name: 'API design', installed_for: true, served_by: [{ server_id: 'fd969aaa', server_name: "Ledger's Hoard" }] }] };
  } else if (/\/api\/connector-skills\/[^/?]+\/[^/?]+/.test(String(url))) {
    body = { ok: true, slug: 'recurring-payments', text: '# Procedure' };
  } else if (String(url).includes('/api/connector-skills/')) {
    body = { server_id: 'fd969aaa', server_name: "Ledger's Hoard", source: 'tool', skills: [{ slug: 'recurring-payments', name: 'Recurring payments', description: 'd', when_to_use: 'Use when x', source: 'tool' }] };
  } else if (String(url).includes('/api/connector-skills')) {
    body = { count: 1, connectors: [] };
  }
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
};

const one = await c.listConnectorSkills('fd969aaa');
assert.equal(one.skills[0].slug, 'recurring-payments');
assert.ok(seen.at(-1).endsWith('/api/connector-skills/fd969aaa'), seen.at(-1));
await c.listConnectorSkills('fd969aaa', true);
assert.ok(seen.at(-1).endsWith('/api/connector-skills/fd969aaa?refresh=1'), seen.at(-1));
await c.listAllConnectorSkills();
assert.ok(seen.at(-1).endsWith('/api/connector-skills'), seen.at(-1));
const text = await c.readConnectorSkill('fd969aaa', 'recurring-payments');
assert.equal(text.text, '# Procedure');
assert.ok(seen.at(-1).endsWith('/api/connector-skills/fd969aaa/recurring-payments'), seen.at(-1));
await c.readConnectorSkill('fd969aaa', 'recurring-payments', 'references/a.md');
assert.ok(seen.at(-1).endsWith('/api/connector-skills/fd969aaa/recurring-payments?file=references%2Fa.md'), seen.at(-1));

const rows = await lib.listSkillLibrary();
assert.equal(rows[0].served_by[0].server_name, "Ledger's Hoard");
assert.equal(rows[0].installed, true);
console.log('ok connector-skills');
