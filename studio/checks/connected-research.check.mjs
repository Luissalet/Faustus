import assert from 'node:assert/strict';
import { build } from 'esbuild';

globalThis.window = { setTimeout, localStorage: { getItem: () => 'es' } };
const built = await build({ entryPoints: ['studio/src/adapters/research.ts'], bundle: true, platform: 'node', format: 'esm', write: false, logLevel: 'silent' });
const research = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));
const calls = [];
let response = {};
globalThis.fetch = async (url, init) => {
  calls.push({ url, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(init.body) : null });
  return { ok: true, status: 200, json: async () => response };
};
response = { session_id: 'rp-connected' };
await research.startResearch('Pregunta real', { maxRounds: 2, dataScope: 'connected', sourceIds: ['notes', 'mcp:links'], sourceFilters: { 'mcp:links': { tag: 'project' } }, clarification: 'Este mes', endpointId: 'global', model: 'configured', category: '', searchProvider: '' });
assert.deepEqual(calls.at(-1).body, { query: 'Pregunta real', max_rounds: 2, data_scope: 'connected', source_ids: ['notes', 'mcp:links'], source_filters: { 'mcp:links': { tag: 'project' } }, clarification: 'Este mes', endpoint_id: 'global', model: 'configured' });
await research.startResearch('Web stays available', { maxRounds: 1, category: '', searchProvider: '', endpointId: '', model: '' });
assert.equal(calls.at(-1).body.data_scope, undefined);
response = { plan: ['Read notes'], clarification: 'Which project?' };
await research.connectedPlan('Pregunta', ['notes'], {
  endpointId: 'chosen', model: 'chosen-model', sourceFilters: { notes: { tag: 'project' } }, clarification: 'Este mes',
});
assert.equal(calls.at(-1).body.endpoint_id, 'chosen');
assert.deepEqual(calls.at(-1).body.source_filters, { notes: { tag: 'project' } });
assert.equal(calls.at(-1).body.clarification, 'Este mes');
const timeline = [{ id: 1, source: 'notes', source_name: 'Notes', at: 1, query: 'project', lane: 'text', status: 'done', count: 1, message: '', tool: '' }];
response = { result: 'Cited report', sources: [{ title: 'Original', url: 'http://app.test/?open=12', app_id: 'darwin', source_id: 'mcp:darwin', excerpt: true }], connected_progress: { plan: 'Read records', timeline }, raw_findings: [] };
const result = await research.researchResult('rp-connected');
assert.equal(result.sources[0].appId, 'darwin');
assert.equal(result.sources[0].excerpt, true);
assert.deepEqual(result.connectedProgress.timeline, timeline);
assert.equal(result.connectedProgress.plan, 'Read records');
response.connected_progress.timeline = [null, { at: 1e200, source: 'bad', status: 'done' }, ...timeline];
assert.deepEqual((await research.researchResult('rp-connected')).connectedProgress.timeline, timeline);
response = { session_id: 'chat-with-original' };
assert.equal(await research.openResearchSource('rp-connected', 'http://app.test/?open=12'), 'chat-with-original');
assert.match(calls.at(-1).url, /rp-connected\/open-source$/);
assert.deepEqual(calls.at(-1).body, { url: 'http://app.test/?open=12' });
response = { result: '', sources: [], connected_progress: { timeline: [{ ...timeline[0], app_id: 'darwin' }] } };
assert.equal((await research.researchResult('rp-connected')).connectedProgress.timeline[0].app_id, 'darwin');

const ui = await build({ entryPoints: ['studio/src/screens/research/connectedSources.ts'], bundle: true, platform: 'node', format: 'esm', write: false });
const { connectedTitle, groupEntries, sourceGroup, sourceGroups, sourceLocation, splitConnectedBibliography } =
  await import('data:text/javascript;base64,' + Buffer.from(ui.outputFiles[0].text).toString('base64'));
const post = { title: 'A discovery\n\nThe post body', url: 'http://darwin.test/?open=12', appId: 'darwin', sourceId: 'mcp:one' };
const invoice = { title: 'Invoice [redacted] **reference**', url: 'http://kafka.test/?doc=3&page=5#/documents/3?page=5', appId: 'kafka' };
const file = { title: 'FAUSTUS.md', url: 'http://local.test/api/research/project-source?path=&line=128&chunk=0', sourceId: 'project_files' };
const sources = [post, invoice, file];
const rows = [
  '1. [' + post.title + '](' + post.url + ') — 127.0.0.1',
  '9. [' + invoice.title + '](' + invoice.url + ') — 127.0.0.1',
  '128. [' + file.title + '](' + file.url + ') — 127.0.0.1',
];
const report = '# Report\n\nClaim [9].\n\n## Fuentes\n\n' + rows.join('\n') + '\n\n## Disagreements\n\nKeep this.';
const split = splitConnectedBibliography(report, sources);
assert.deepEqual(split, { before: '# Report\n\nClaim [9].', after: '## Disagreements\n\nKeep this.',
  citations: sources.map((source, i) => ({ source, number: [1, 9, 128][i] })) });
const analysis = '## Sources\n\nThe connectors disagreed.';
assert.deepEqual(splitConnectedBibliography(analysis, sources), { before: analysis, after: '', citations: [] });
const inlineAnalysis = '## Sources\n\n' + rows[0] + '\n\nKeep this analysis.';
assert.equal(splitConnectedBibliography(inlineAnalysis, sources).before, inlineAnalysis);
assert.equal(connectedTitle(post), 'A discovery…');
assert.equal(connectedTitle(invoice), 'Invoice redacted reference');
assert.equal(Array.from(connectedTitle({ ...post, title: 'a'.repeat(300) })).length, 96);
assert.deepEqual(sourceLocation(file), { line: '128', chunk: '0' });
assert.deepEqual(sourceLocation(invoice), { page: '5' });
const events = [
  { source: 'mcp:one', source_name: 'Darwin test' },
  { source: 'project_files', source_name: 'Project files' },
  { source: 'mcp:two', app_id: 'darwin' },
];
const groups = sourceGroups(events, sources);
assert.equal(groups.get('mcp:one').name, 'Darwin');
assert.equal(groups.get('project_files').name, 'Project');
assert.deepEqual(groupEntries(events, e => groups.get(e.source)).map(g => g.entries.map(e => e.source)),
  [['mcp:one', 'mcp:two'], ['project_files']]);
assert.equal(sourceGroup('mcp:custom', undefined, 'Custom connector').name, 'Custom connector');
console.log('Connected research adapter and bibliography checks passed');
