// The Context Engine screen's arithmetic (studio/src/adapters/context.ts).
//
// Everything the panel claims is derived here rather than inside a component,
// because a diagnostic whose numbers cannot be checked is decoration. Five
// things in particular, each of which would be a lie of a different kind if it
// were wrong:
//
//   - the diagnostics body, normalised, including the branches where the
//     server answers `{"error": …}` because a diagnostic may never raise;
//   - omissions grouped by reason, from either shape the API can hand over;
//   - tokens per section as a share of the packet;
//   - a verdict read to its label, where nothing but `proved` is a success;
//   - the refusal convention: 200 with `{"ok": false, "error": {path, message}}`.
//
// Bundled with esbuild on the fly; run by tests/test_studio_context_js.py, or
// by hand:
//   node studio/checks/context.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-context-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({
    entryPoints: [join(root, 'studio', 'src', rel)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: out,
    logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const ctx = await load(join('adapters', 'context.ts'), 'context.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

// ── A refusal is an answer, not a failure ──
//
// The engine answers a rejection with 200 and `{"ok": false, "error": …}`.
// A reader that treated that as a success would render the block it just
// refused to store as if it were saved.
{
  assert(ctx.refusalOf({ ok: true, block: { id: 'b1' } }) === null, 'a success is not a refusal');
  assert(ctx.refusalOf(null) === null && ctx.refusalOf('nope') === null, 'nothing is not a refusal either');

  const secret = ctx.refusalOf({
    ok: false,
    error: {
      path: 'block.content',
      message: "block.content looks like it carries a credential (matched 'api_key')",
    },
  });
  assert(secret !== null && secret.path === 'block.content', 'the refused field survives');
  assert(secret.message.includes('api_key'), 'and so does the pattern the server named');

  const bare = ctx.refusalOf({ ok: false });
  assert(bare !== null && bare.path === '<root>', 'a refusal with no field is still a refusal');
  assert(bare.message.length > 0, 'and it never renders as an empty sentence');

  const blank = ctx.refusalOf({ ok: false, error: { path: '   ', message: '  ' } });
  assert(blank.path === '<root>' && blank.message.length > 0, 'whitespace is not an explanation');

  const conflict = ctx.refusalOf({
    ok: false,
    error: { path: 'block', message: 'block b1 is at revision 7, not 5', revision: 7 },
  });
  assert(conflict.revision === 7, 'a conflict carries the revision you have to re-read');

  const refusal = new ctx.ContextRefusal(secret);
  assert(refusal instanceof Error && refusal.path === 'block.content', 'ContextRefusal is a real Error with the field on it');
  assert(refusal.message === secret.message, 'and its message is the server sentence, unedited');
}

// ── Omissions, grouped by reason ──
//
// "Why did it not read that file?" is the second question the whole subsystem
// exists to answer, and it arrives in two shapes: rows from a compile, and the
// ledger's counts once the manifest has been evicted.
{
  const fromCounts = ctx.omissionsByReason({ budget: 3, policy: 1 });
  assert(fromCounts.length === 2 && fromCounts[0].reason === 'budget', 'the commonest reason comes first');
  assert(fromCounts[0].count === 3 && fromCounts[0].pct === 75, 'with its share of everything left out');
  assert(fromCounts[1].pct === 25, 'and the rest adds up');

  const fromRows = ctx.omissionsByReason([
    { reason: 'budget', source_ref: 'doc:1' },
    { reason: 'budget', source_ref: 'doc:2' },
    { reason: 'trust', source_ref: 'doc:3' },
  ]);
  assert(fromRows.length === 2 && fromRows[0].count === 2, 'rows group the same way counts do');

  const unnamed = ctx.omissionsByReason([{ source_ref: 'doc:9' }]);
  assert(unnamed[0].reason === 'unknown', 'an omission with no reason is named, not dropped');

  assert(ctx.omissionsByReason({}).length === 0, 'nothing omitted is an empty list, not a zero row');
  assert(ctx.omissionsByReason(undefined).length === 0, 'and an absent field does not throw');
  assert(ctx.omissionsByReason({ budget: 0 }).length === 0, 'a reason that never fired is not a reason');

  const tie = ctx.omissionsByReason({ zeta: 2, alpha: 2 });
  assert(tie[0].reason === 'alpha', 'a tie is broken by name, so the order never jitters between renders');
}

// ── Tokens per section, as a share of the packet ──
//
// Of the packet and not of the window, because the question a section table
// answers is "what crowded out what".
{
  const shares = ctx.sectionShares({ recent_messages: 600, project_rules: 300, tool_guidance: 100 });
  assert(shares.length === 3 && shares[0].kind === 'recent_messages', 'the biggest section is first');
  assert(shares[0].pct === 60 && shares[1].pct === 30 && shares[2].pct === 10, 'and each carries its share');
  assert(shares.reduce((total, row) => total + row.pct, 0) === 100, 'the shares add up to the whole');

  assert(ctx.sectionShares({}).length === 0, 'no sections is an empty table');
  const zero = ctx.sectionShares({ system_constraints: 0 });
  assert(zero.length === 1 && zero[0].pct === 0, 'an empty packet divides by nothing and says 0%');

  assert(ctx.pct(1, 3) === 33.3, 'a share keeps one decimal, as the server rounds it');
  assert(ctx.pct(5, 0) === 0, 'and an unknown window is 0%, never NaN');
}

// ── A verdict is never painted as a success ──
{
  const proved = ctx.verdictReading('proved');
  assert(proved.tone === 'proved' && proved.trusted === true, 'only proved is trusted');

  for (const verdict of ['partial', 'unproved', 'contradicted']) {
    const reading = ctx.verdictReading(verdict);
    assert(reading.tone === verdict, `${verdict} keeps its own tone`);
    assert(reading.trusted === false, `${verdict} is not a success`);
  }

  const labels = new Set(
    ['proved', 'partial', 'unproved', 'contradicted', ''].map((v) => ctx.verdictReading(v).label),
  );
  assert(labels.size === 5, 'every verdict reads as a different word, so colour is never the only signal');

  const missing = ctx.verdictReading('');
  assert(missing.tone === 'unknown' && missing.trusted === false, 'no verdict is not a good verdict');
  assert(ctx.verdictReading('nonsense').tone === 'unknown', 'and neither is one nobody defined');
  assert(ctx.verdictReading('  PROVED ').tone === 'proved', 'case and stray space do not change a verdict');
  assert(ctx.verdictReading(null).trusted === false, 'a missing field is not a success');
}

// ── The diagnostics body, normalised ──
{
  const full = ctx.normalizeDiagnostics({
    ok: true,
    compiler: {
      owner: 'ana',
      packets: 10,
      degraded: 2,
      degraded_pct: 20,
      avg_tokens: 1400,
      avg_budget_pct: 42.5,
      receipts: 4,
      last_packet_at: '2026-09-06T10:00:00Z',
      by_intent: { code: 6, chat: 4 },
      omissions: { budget: 3 },
    },
    store: { bytes: 2048, tables: { context_packets: 10, experiences: 3 } },
    maintenance: { last_run: { vacuum: { ran_at: '2026-09-01T00:00:00Z', ok: true, changed: 0, detail: 'done' } }, due: ['prune_packets'] },
    cache: { hits: 3, misses: 1, evictions: 0, entries: 4, bytes: 900, hit_rate: 0.75 },
    sources: { declared: ['memory', 'rag'], built: ['memory'], unavailable: ['rag'] },
  });
  assert(full.packets === 10 && full.degraded === 2 && full.degradedPct === 20, 'the compiler numbers come through');
  assert(full.storeBytes === 2048 && full.tables.experiences === 3, 'so do the store and its tables');
  assert(full.cache.hitRate === 0.75, 'the cache hit rate is the one the server measured');
  assert(full.sources.unavailable.join() === 'rag', 'an unavailable source is named, never silently missing');
  assert(full.omissions.length === 1 && full.omissions[0].reason === 'budget', 'omissions arrive grouped');
  assert(full.lastRun.length === 1 && full.lastRun[0].name === 'vacuum', 'the maintenance history is a list, keyed by task');
  assert(full.due.join() === 'prune_packets', 'and what is due survives');

  // A diagnostic may never raise, so the server answers with an error field
  // instead of failing. The screen has to show that, not a confident zero.
  const broken = ctx.normalizeDiagnostics({
    ok: true,
    cache: { error: 'the working set could not be read' },
    sources: { error: 'the source registry could not be read' },
  });
  assert(broken.cache.error.length > 0 && broken.cache.hitRate === 0, 'an unreadable cache says so instead of claiming 0% by measurement');
  assert(broken.sources.error.length > 0 && broken.sources.unavailable.length === 0, 'an unreadable registry does not invent a list of broken sources');

  const derived = ctx.normalizeDiagnostics({ cache: { hits: 3, misses: 1 } });
  assert(derived.cache.hitRate === 0.75, 'a missing hit_rate is derived from the counters');

  const nothing = ctx.normalizeDiagnostics({});
  assert(nothing.packets === 0 && nothing.tables && nothing.lastRun.length === 0, 'an empty body normalises instead of throwing');
  assert(ctx.normalizeDiagnostics(undefined).packets === 0, 'and so does no body at all');
}

// ── The ledger row, and how old the oldest one is ──
{
  const packet = ctx.packetFrom({
    packet_id: 'ctxpkt_1',
    created_at: '2026-09-06T09:00:00Z',
    model: 'llama',
    intent: 'code',
    tokens: 900,
    input_budget: 3000,
    items: 12,
    degraded: 1,
    section_tokens: { project_rules: 400, recent_messages: 500 },
    omission_counts: { budget: 2, trust: 1 },
  });
  assert(packet.id === 'ctxpkt_1' && packet.budgetPct === 30, 'a packet knows what share of its budget it spent');
  assert(packet.degraded === true, 'SQLite writes 1, not true, and degradation may never be lost in the cast');
  assert(packet.omissions === 3, 'the row says how many things were left out, all reasons together');

  const empty = ctx.packetFrom({});
  assert(empty.budgetPct === 0 && empty.omissions === 0 && empty.degraded === false, 'a row with nothing in it reads as zeros');

  const rows = [
    ctx.packetFrom({ packet_id: 'a', created_at: '2026-09-06T09:00:00Z' }),
    ctx.packetFrom({ packet_id: 'b', created_at: '2026-09-01T09:00:00Z' }),
  ];
  const oldest = ctx.oldestPacket(rows, 200);
  assert(oldest.at === '2026-09-01T09:00:00Z', 'the oldest row in the page is the oldest one');
  assert(oldest.capped === false, 'a short page saw the whole ledger');
  assert(ctx.oldestPacket(rows, 2).capped === true, 'a full page did not, and must not claim it did');
  assert(ctx.oldestPacket([], 200).at === '', 'an empty ledger has no oldest packet');
}

// ── The manifest: retained, and honestly missing ──
{
  const retained = ctx.manifestFrom({
    ok: true,
    packet_id: 'ctxpkt_1',
    retained: true,
    manifest: [
      {
        context_item_id: 'i1',
        section: 'project_rules',
        source_type: 'block',
        source_ref: 'block:b1',
        retrieval_lanes: ['mandatory'],
        transformation: 'verbatim',
        tokens: 300,
        reason: 'always-loaded block',
      },
      {
        context_item_id: 'i2',
        section: 'recent_messages',
        source_type: 'conversation',
        source_ref: 'msg:9',
        transformation: 'summary',
        generated: true,
        tokens: 100,
        reason: 'the last question',
      },
    ],
    summary: { omissions: { by_reason: { budget: 4 } }, warnings: ['the window was not known'], degraded: true },
  });
  assert(retained.retained === true && retained.items.length === 2, 'a retained manifest carries one row per injected item');
  assert(retained.items[0].reason === 'always-loaded block', 'and every row says why it is there');
  assert(retained.items[1].generated === true, 'a generated item is marked as generated, not as its source');
  assert(retained.sections[0].kind === 'project_rules' && retained.sections[0].pct === 75, 'sections are derived from the rows themselves, so they cannot disagree with them');
  assert(retained.omissions[0].reason === 'budget' && retained.omissions[0].count === 4, 'the omission counts come from the summary');
  assert(retained.warnings.length === 1, 'and the compiler warnings survive, because a degraded packet is nothing without them');

  const evicted = ctx.manifestFrom({
    ok: true,
    packet_id: 'ctxpkt_2',
    retained: false,
    manifest: null,
    section_tokens: { retrieved_memory: 200, recent_messages: 200 },
    omission_counts: { budget: 1 },
    note: 'the ledger keeps counts, not rows',
  });
  assert(evicted.retained === false && evicted.items.length === 0, 'an evicted manifest has no rows, and says so rather than pretending');
  assert(evicted.note.length > 0, 'the note that explains the miss is kept');
  assert(evicted.sections.length === 2 && evicted.sections[0].pct === 50, 'the ledger still knows where the window went');
  assert(evicted.omissions[0].count === 1, 'and still knows what was left out, by reason');
  assert(evicted.warnings.length === 0, 'what it does not know it does not invent');
}

// ── The always-loaded ration ──
//
// "Why is my block not loading?" has to be a list, not a mystery.
{
  const audit = ctx.auditFrom({
    ok: true,
    audit: {
      blocks: 9,
      always_loaded: 7,
      always_loaded_granted: 5,
      always_loaded_chars: 9000,
      over_ration: [
        { scope: 'project', owner: 'ana', project_id: 'p1', blocks: 6, chars: 9000, max_blocks: 5, max_chars: 8000, demoted: ['b6', 'b7'] },
        { scope: 'owner', owner: 'ana', project_id: '', blocks: 2, chars: 100, max_blocks: 5, max_chars: 8000, demoted: ['b6'] },
      ],
      oversized: [{ id: 'b1', title: 'rules', chars: 5000, max_chars: 3000 }],
      duplicates: [{ digest: 'd1', ids: ['b2', 'b3'] }],
      contradictions: [{ type: 'active_goal', scope: 'project', project_id: 'p1', ids: ['b4', 'b5'] }],
      ok: false,
    },
  });
  assert(audit.alwaysLoadedGranted === 5 && audit.alwaysLoaded === 7, 'the ration says how many of the always-loaded blocks are actually served');
  assert(audit.demoted.join() === 'b6,b7', 'and names the ones being left out, once each across scopes');
  assert(audit.maxBlocks === 5 && audit.maxChars === 8000, 'the cap comes from the server, not from a number typed into the screen');
  assert(audit.oversized.length === 1 && audit.duplicates.length === 1 && audit.contradictions.length === 1, 'the three other problems survive too');
  assert(audit.ok === false, 'and the report knows it is not clean');

  const clean = ctx.auditFrom({ ok: true, audit: { blocks: 2, always_loaded: 1, always_loaded_granted: 1, ok: true } });
  assert(clean.demoted.length === 0 && clean.maxBlocks === 0, 'with nothing over the ration the screen has no cap to quote, and does not invent one');
}

// ── A block, as the screen has to show it ──
{
  const block = ctx.blockFrom({
    id: 'b1',
    type: 'project_rules',
    scope: 'project',
    title: 'House rules',
    content: 'x'.repeat(4000),
    max_chars: 3000,
    priority: 70,
    always_loaded: 1,
    revision: 3,
  });
  assert(block.chars === 4000 && block.truncated === true, 'a block longer than its cap enters prompts as a prefix, and the screen says so');
  assert(block.alwaysLoaded === true, 'always_loaded survives SQLite writing it as 1');
  assert(ctx.blockFrom({ id: 'b2', content: 'short', max_chars: 3000 }).truncated === false, 'a short one is not cut');
}

// ── The supersedes chain ──
{
  const rows = [
    ctx.findingFrom({ id: 'f3', claim: 'the third word on it', supersedes: 'f2' }),
    ctx.findingFrom({ id: 'f2', claim: 'the second', supersedes: 'f1' }),
    ctx.findingFrom({ id: 'f1', claim: 'the first' }),
  ];
  const byId = new Map(rows.map((row) => [row.id, row]));
  assert(ctx.supersedeChain('f3', byId).join('>') === 'f3>f2>f1', 'a correction is read back as the chain it made');
  assert(ctx.supersedeChain('f1', byId).join('>') === 'f1', 'a finding that corrects nothing is a chain of one');

  // The board serves current rows only, so an ancestor is usually not loaded.
  const partial = new Map([['f3', rows[0]]]);
  assert(ctx.supersedeChain('f3', partial).join('>') === 'f3>f2', 'an ancestor nobody loaded is still named, as an id you can look up');

  const loop = new Map([
    ['a', ctx.findingFrom({ id: 'a', supersedes: 'b' })],
    ['b', ctx.findingFrom({ id: 'b', supersedes: 'a' })],
  ]);
  assert(ctx.supersedeChain('a', loop).length === 2, 'a corrupted pointer costs the chain, never the render');
}

// ── The code index: stale, empty, and fresh are three different things ──
{
  const now = Date.parse('2026-09-06T12:00:00Z');
  const fresh = { files: 40, lastIndexedAt: '2026-09-06T11:50:00Z' };
  const old = { files: 40, lastIndexedAt: '2026-09-06T10:00:00Z' };

  assert(ctx.codeIndexStale(fresh, now) === false, 'an index refreshed ten minutes ago is current');
  assert(ctx.codeIndexStale(old, now) === true, 'one that has not been touched in two hours is out of date');
  assert(ctx.codeIndexStale({ files: 0, lastIndexedAt: '' }, now) === false, 'an empty index is empty, not stale: saying otherwise sends people hunting a problem that is not there');
  assert(ctx.codeIndexStale({ files: 10, lastIndexedAt: '' }, now) === true, 'rows with no timestamp are not evidence of freshness');
  assert(ctx.CODE_INDEX_STALE_S === 1800, 'the threshold is the interval the server itself uses');

  const status = ctx.codeIndexStatusFrom({
    ok: true,
    status: { workspace: '/w', files: 3, symbols: 40, edges: 12, by_kind: { function: 30 }, languages: { python: 3 }, last_indexed_at: '2026-09-06T11:00:00Z' },
  });
  assert(status.symbols === 40 && status.files === 3, 'the status counts come through');
  assert(ctx.codeIndexStatusFrom({}).symbols === 0, 'and an unreadable one is zeros, not a crash');

  const symbol = ctx.symbolFrom({ id: 's1', path: 'src/app.py', qualname: 'app.serve_index', kind: 'function', start_line: 40, end_line: 58 });
  assert(ctx.symbolRef(symbol) === 'src/app.py#L40-L58', 'a hit points at exactly the range a person can open');
  assert(ctx.symbolRef({ path: '', startLine: 0, endLine: 0 }) === '', 'and a symbol with no path points at nothing');

  const report = ctx.refreshReportFrom({ ok: true, refresh: { scanned: 120, reindexed: 4, removed: 1, symbols: 900, elapsed_ms: 700, truncated: true } });
  assert(report.scanned === 120 && report.reindexed === 4 && report.removed === 1, 'a refresh says what it cost');
  assert(report.truncated === true, 'and admits when the file budget ran out before the walk did');
}

// ── "Scanned 0" is not an answer to a field nobody applied ──
//
// A refresh walks the APPLIED workspace, the one in the URL. Refreshing a path
// that was typed and never applied walks nothing, and the server answers
// "scanned 0 · reindexed 0 · 0 symbols" — the same sentence it sends for a
// repository with nothing in it. One is a broken indexer, the other is one
// keystroke, and the screen has to be able to tell you which.
{
  assert(ctx.refreshBlocker('/repo', '/repo') === '', 'an applied workspace can be refreshed');
  assert(ctx.refreshBlocker('/repo', '/somewhere/else') === '', 'and stays refreshable while a new path is being typed');
  assert(ctx.refreshBlocker('', '/repo') === 'unapplied', 'a path typed and not applied is named as exactly that');
  assert(ctx.refreshBlocker('', '   ') === 'unset', 'whitespace is not a path');
  assert(ctx.refreshBlocker('', '') === 'unset', 'and an empty field needs one before anything can be walked');
  assert(ctx.refreshBlocker('   ', '/repo') === 'unapplied', 'a whitespace workspace is no workspace');
  assert(ctx.refreshBlocker(undefined, undefined) === 'unset', 'nothing readable never claims a workspace');
}

// ── Which mode the engine is in ──
{
  assert(ctx.engineMode({ agent_context_engine: true, agent_context_engine_shadow: true }) === 'live', 'live wins over shadow: what the model is really told is the thing this must not get wrong');
  assert(ctx.engineMode({ agent_context_engine: false, agent_context_engine_shadow: true }) === 'shadow', 'shadow does not wait for the other flag');
  assert(ctx.engineMode({}) === 'off', 'off is the default, as it is on the server');
  assert(ctx.engineMode({ agent_context_engine: 'true' }) === 'live', 'a settings file that stored the flag as a string still means on');
  assert(ctx.engineMode(null) === 'off', 'and nothing readable is not "on"');
}

// ── An experience, and the two halves of Knowledge ──
{
  const anti = ctx.experienceFrom({
    id: 'exp1',
    problem: 'retry storm on 429',
    verdict: 'contradicted',
    result: 'failure',
    role: 'anti_pattern',
    failure_modes: ['it made the rate limit worse'],
    helpful: 0,
    harmful: 3,
  });
  assert(anti.role === 'anti_pattern', 'an anti-pattern keeps the role the server gave it');
  assert(ctx.verdictReading(anti.verdict).trusted === false, 'and a contradicted verdict can never be read as a success');

  const finding = ctx.findingFrom({ id: 'f1', kind: 'result', claim: 'the parser drops CRLF', evidence_refs: ['run:9'], tags: ['parser'], status: 'open' });
  assert(finding.evidenceRefs.length === 1 && finding.tags.join() === 'parser', 'a finding keeps its evidence and its tags');
  assert(ctx.findingFrom({}).claim === '', 'and an unreadable row is empty rather than a crash');
}

// ── Bytes, as a person reads them ──
{
  assert(ctx.formatBytes(0) === '0 B', 'nothing is 0 B');
  assert(ctx.formatBytes(900) === '900 B', 'under a kilobyte stays in bytes');
  assert(ctx.formatBytes(1536) === '1.5 KB', 'and above it gets one decimal while it is small');
  assert(ctx.formatBytes(20 * 1024 * 1024) === '20 MB', 'a big round number does not need the decimal');
  assert(ctx.formatBytes(-5) === '0 B', 'a nonsense size is 0, not "-5 B"');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nALL OK');
