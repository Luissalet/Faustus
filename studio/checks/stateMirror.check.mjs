// The State Mirror screen's reasoning (studio/src/adapters/stateMirror.ts).
//
// The mirror says what is true right now, when it was last looked at and how
// it is known. Everything the screen claims about that is derived here rather
// than inside a component, because each of these would be a lie of a
// different kind if it were wrong:
//
//   - which of four words describes a value's age, and the order they rank in,
//     so "older" never sorts above "newer";
//   - whether a value may be drawn as the CURRENT state, which only `fresh`
//     ever may -- a stale number presented as live is the one failure this
//     whole screen exists to prevent;
//   - which of the five questions a row answers, and that a live grouping is
//     never reached through a value we are not allowed to trust;
//   - that an entity id survives a round trip through a URL, since one carries
//     a scheme and slashes and cannot be interpolated raw;
//   - where a reconnection resumes: no repeated event, no cursor going
//     backwards.
//
// Bundled with esbuild on the fly; run by tests/test_studio_state_mirror_js.py,
// or by hand:
//   node studio/checks/stateMirror.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-state-'));

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

const s = await load(join('adapters', 'stateMirror.ts'), 'stateMirror.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

const NOW = Date.parse('2026-09-06T12:00:00Z');
const ago = (seconds) => new Date(NOW - seconds * 1000).toISOString();

const field = (over) => ({
  value: null, epistemic: 'observed', freshness: 'fresh',
  observed_at: ago(5), source: 'runs', ttl_seconds: 15, observation_id: 'obs_1', ...over,
});

const entity = (over) => s.entityFrom({
  id: 'run:///real/run_9f2c', kind: 'run', schema: 'run_state.v1',
  display_name: 'run_9f2c', fields: {}, conflicts: [], ...over,
});

// ── Four words, and they rank ──
//
// `unknown` is not an error and not the same as `stale`: one means "we looked
// and it was too long ago", the other "nobody has ever looked". A screen that
// collapsed them would offer a refresh for a value no adapter produces.
{
  assert(s.freshnessRank('fresh') < s.freshnessRank('aging'), 'fresh outranks ageing');
  assert(s.freshnessRank('aging') < s.freshnessRank('stale'), 'ageing outranks stale');
  assert(s.freshnessRank('stale') < s.freshnessRank('unknown'), 'and stale outranks never-observed');
  assert(s.freshnessRank('invented') >= s.freshnessRank('unknown'),
    'a rating this build never heard of ranks last, never first');

  assert(s.worstFreshness(['fresh', 'stale', 'aging']) === 'stale', 'the worst of a set is the least trustworthy in it');
  assert(s.worstFreshness([]) === 'unknown', 'and nothing at all is not evidence of freshness');
  assert(s.worstFreshness(['fresh', 'nonsense']) === 'fresh', 'a word off the list is skipped, not ranked into the answer');

  assert(s.combinedFreshness(['aging', 'aging']) === 'aging', 'fields that agree give the row their word');
  assert(s.combinedFreshness(['fresh', 'stale']) === 'mixed', 'and fields that disagree give `mixed`, not an average');
  assert(s.combinedFreshness([]) === 'unknown', 'a row with no fields at all is unknown');
  assert(s.worstFreshness(['fresh', 'stale']) === 'stale' && s.combinedFreshness(['fresh', 'stale']) === 'mixed',
    'worst and combined are different questions and give different answers');
}

// ── A stale value is never current ──
//
// The whole screen rests on this one predicate. `aging` failing it is not an
// oversight: orientation may use an ageing value and a decision may not.
{
  const fresh = s.fieldFrom('status', field({ freshness: 'fresh', value: 'running' }));
  const ageing = s.fieldFrom('status', field({ freshness: 'aging', value: 'running' }));
  const stale = s.fieldFrom('status', field({ freshness: 'stale', value: 'running' }));
  const never = s.fieldFrom('status', field({ freshness: 'unknown', value: null, observed_at: '' }));

  assert(s.isCurrent(fresh) === true, 'only a fresh value may be shown as the state now');
  assert(s.isCurrent(ageing) === false, 'an ageing value may not, however recently it was seen');
  assert(s.isCurrent(stale) === false, 'and a stale one certainly may not');
  assert(s.isCurrent(never) === false && s.isCurrent(null) === false, 'nor may one nothing ever observed');

  assert(s.isUsable(ageing) === true, 'an ageing value is still good enough to orient by');
  assert(s.isUsable(stale) === false, 'a stale one is not good enough for anything');

  const rumour = s.fieldFrom('status', field({ freshness: 'fresh', epistemic: 'reported' }));
  assert(s.isTrusted(fresh) === true, 'fresh and observed is trusted');
  assert(s.isTrusted(rumour) === false, 'a fresh rumour is still a rumour');
  assert(s.isTrusted(s.fieldFrom('x', field({ freshness: 'aging' }))) === false, 'and yesterday’s measurement is still yesterday’s');

  assert(s.freshnessReading('stale').tone === 'danger', 'stale is drawn as a danger');
  assert(s.freshnessReading('stale').label !== s.freshnessReading('unknown').label,
    'and it is told apart from never-observed by a word, not only by a colour');
  assert(s.freshnessReading('stale').current === false && s.freshnessReading('fresh').current === true,
    'the reading carries the same permission the predicate does');
  assert(s.freshnessReading('').value === 'unknown', 'a missing rating reads as never observed');

  const row = entity({ fields: { status: field({ freshness: 'stale' }), progress: field({ freshness: 'fresh' }) } });
  assert(s.currentFields(row).map((f) => f.name).join() === 'progress', 'only the fresh fields are offered as current');
  assert(s.agedFields(row).map((f) => f.name).join() === 'status', 'and the rest are still drawn, marked');
  assert(row.freshness === 'mixed' && row.worst === 'stale', 'the row says `mixed` and remembers its worst');
}

// ── The five questions, in the order a person asks them ──
{
  const running = entity({ fields: { status: field({ value: 'running', freshness: 'fresh' }) } });
  assert(s.situationOf(running) === 'running', 'a run observed just now saying `running` is running');

  const remembered = entity({ fields: { status: field({ value: 'running', freshness: 'stale' }) } });
  assert(s.situationOf(remembered) === 'stale',
    'the same row observed too long ago is STALE, not running: a live grouping is never reached through a value we may not trust');

  const ageingRun = entity({ fields: { status: field({ value: 'running', freshness: 'aging' }) } });
  assert(s.situationOf(ageingRun) === 'running', 'an ageing status still orients, and the field says so on the row');

  const waiting = entity({ fields: { status: field({ value: 'blocked' }), approval_pending: field({ value: true }) } });
  assert(s.situationOf(waiting) === 'waiting', 'an approval pending is somebody waiting for a person');

  const approval = s.entityFrom({
    id: 'approval:///real/apr_3', kind: 'approval', schema: 'approval_state.v1',
    fields: { status: field({ value: 'pending' }) },
  });
  assert(s.situationOf(approval) === 'waiting', 'and so is an approval whose status says pending');

  const disputed = entity({ conflicts: ['cfl_1'], fields: { status: field({ value: 'running' }) } });
  assert(s.situationOf(disputed) === 'conflict',
    'a row two sources disagree about is a conflict FIRST, so neither claim is drawn as though it had won');

  const device = s.entityFrom({
    id: 'device:///real/localhost', kind: 'device', schema: 'device_state.v1',
    fields: { cpu_percent: field({ value: 12 }) },
  });
  assert(s.situationOf(device) === 'machine', 'a machine reading nobody is waiting on is inventory');

  const empty = s.entityFrom({ id: 'service:///real/comfyui', kind: 'service', schema: 'service_state.v1', fields: {} });
  assert(s.situationOf(empty) === 'stale', 'a row nothing has ever observed is not quietly filed as healthy');
  assert(s.situationOf(null) === 'machine', 'and nothing at all does not throw');

  const groups = s.groupBySituation([running, waiting, remembered, disputed, device]);
  assert(groups.map((g) => g.situation).join() === s.SITUATIONS.join(),
    'the groups come back in the order the questions are asked, always');
  assert(groups.length === 5, 'including the empty ones, so the headings do not move under the reader');
  const by = Object.fromEntries(groups.map((g) => [g.situation, g.entities.length]));
  assert(by.running === 1 && by.waiting === 1 && by.stale === 1 && by.conflict === 1 && by.machine === 1,
    'and every row lands in exactly one of them');
  assert(s.groupBySituation(undefined).length === 5, 'nothing to group is five empty groups, not a crash');
  assert(s.groupBySituation([null, running])[0].entities.length === 1, 'and an unreadable row is skipped, not fatal');

  const ordered = s.groupBySituation([
    entity({ id: 'run:///real/a', fields: { status: field({ value: 'x', freshness: 'fresh' }) } }),
    entity({ id: 'run:///real/b', fields: { status: field({ value: 'x', freshness: 'unknown' }) } }),
  ]).find((g) => g.situation === 'machine');
  assert(ordered.entities.length === 1, 'a row with nothing usable is not filed under the machine');

  assert(s.situationLabel('stale').toLowerCase().includes('stale'), 'the stale group says the word in its heading');
  assert(s.situationLabel('running') !== s.situationLabel('waiting'), 'and every heading is its own sentence');
}

// ── Which field decides, and where it came from ──
{
  const service = s.entityFrom({
    id: 'service:///real/ollama', kind: 'service', schema: 'service_state.v1',
    fields: { health: field({ value: 'ok', source: 'services', observed_at: ago(30) }) },
  });
  assert(s.decidingField(service).name === 'health', 'a service is decided by its health, from the declared map');
  assert(s.decidingField(entity({ fields: {} })) === null, 'a row without that field decides nothing');
  assert(s.decidingField(s.entityFrom({ id: 'capability:///real/x', kind: 'capability' })) === null,
    'and a schema nothing declares a deciding field for falls through rather than guessing');

  const p = s.provenanceOf(s.fieldOf(service, 'health'), NOW);
  assert(p.source === 'services', 'the tooltip names the source that saw it');
  assert(Math.round(p.ageSeconds) === 30, 'and how long ago, to the second');
  assert(p.observedAt === ago(30), 'keeping the server’s own timestamp, unedited');
  assert(p.epistemic.label === 'observed', 'with how strongly it is known beside it');
  assert(s.provenanceOf(null).ageSeconds === null, 'nothing observed has no age, which is not an age of zero');
  assert(s.ageSeconds('not a time') === null, 'an unreadable timestamp is not a fresh one');
  assert(s.ageSeconds(new Date(NOW + 60000).toISOString(), NOW) === 0,
    'a clock running fast reads as just now, never as fresher than fresh');
}

// ── An entity id survives a URL ──
//
// `<kind>://<owner>/<namespace>/<identifier>`: a scheme, an empty owner on a
// single-user install, and slashes inside the identifier. Interpolated raw it
// would address a different row, or none.
{
  const ids = [
    'service:///real/comfyui',
    'artifact://alice/real/exports/2026/report.pdf',
    'model:///real/qwen3.5:9b',
    'run://alice/branch:b_17/run_9f2c',
    'project:///real/localai-faustus',
  ];
  for (const id of ids) {
    assert(s.decodeEntityId(s.encodeEntityId(id)) === id, `${id} survives the round trip`);
    assert(s.encodeEntityId(id).indexOf('/') < 0, `${id} carries no bare slash into the path`);
  }
  assert(s.kindOf('artifact://alice/real/exports/2026/report.pdf') === 'artifact', 'the kind is read off the front');
  assert(s.identifierOf('artifact://alice/real/exports/2026/report.pdf') === 'exports/2026/report.pdf',
    'and the identifier keeps every slash it had');
  assert(s.namespaceOf('run://alice/branch:b_17/run_9f2c') === 'branch:b_17', 'a branch namespace is read whole');
  assert(s.kindOf('not-an-id') === '' && s.identifierOf('not-an-id') === '', 'and a value that is not an id yields nothing, not a crash');
  assert(s.decodeEntityId('%E0%A4%A') === '%E0%A4%A', 'a malformed escape comes back as it went in');

  const row = s.entityFrom({ id: 'artifact://alice/real/exports/2026/report.pdf', fields: {} });
  assert(row.kind === 'artifact' && row.displayName === '',
    'a row the server did not name stays nameless here, so a merge can tell that from a name that happens to look like an id');
  assert(s.label(row) === 'exports/2026/report.pdf',
    'and it is drawn under its identifier, slashes and all');
  assert(s.label(s.entityFrom({ id: 'not-an-id' })) === 'not-an-id', 'with the whole id as the last resort');
  assert(s.label(null) === '', 'and nothing at all draws nothing');
}

// ── A refusal is a token, not a sentence ──
{
  assert(s.refusalOf({ ok: true, entities: [] }) === null, 'a success is not a refusal');
  assert(s.refusalOf(null) === null && s.refusalOf('nope') === null, 'nothing is not a refusal either');

  const off = s.refusalOf({
    ok: false, enabled: false,
    error: { path: 'refresh', code: 'state_mirror_disabled', message: 'the mirror is switched off' },
  });
  assert(off.code === 'state_mirror_disabled', 'the token survives');
  assert(off.path === 'refresh' && off.detail.includes('switched off'), 'and so do the field and the sentence');
  assert(off.enabled === false, 'and the flag, so the button can say why it is off');

  const bare = s.refusalOf({ ok: false });
  assert(bare.code === 'refused' && bare.detail.length > 0, 'a refusal with no token is still a refusal');

  const raised = new s.StateRefusal(off);
  assert(raised instanceof Error && raised.code === 'state_mirror_disabled', 'StateRefusal is a real Error carrying the token');
  assert(raised.message === off.detail, 'and its message is the server’s own sentence');
}

// ── Reconnection: no repeat, and never backwards ──
{
  const ev = (seq, over) => s.eventFrom({ id: `sev_${seq}`, seq, name: 'state_changed', payload: {}, ...over });

  const first = s.advanceCursor(0, [ev(1), ev(2), ev(3)]);
  assert(first.cursor === 3 && first.applied.length === 3, 'a fresh stream applies everything and lands on the last seq');
  assert(first.duplicates === 0, 'with nothing repeated');

  const replay = s.advanceCursor(3, [ev(2), ev(3), ev(4)]);
  assert(replay.applied.map((e) => e.seq).join() === '4', 'events at or before the cursor are dropped, not re-shown');
  assert(replay.duplicates === 2 && replay.cursor === 4, 'and the repeats are counted rather than hidden');

  const twice = s.advanceCursor(0, [ev(1), ev(1)]);
  assert(twice.applied.length === 1 && twice.duplicates === 1, 'the same event id twice is one event');

  const behind = s.advanceCursor(9, [ev(2)]);
  assert(behind.cursor === 9 && behind.applied.length === 0, 'a cursor never moves backwards');
  assert(s.advanceCursor(0, undefined).cursor === 0, 'an empty answer leaves the cursor where it was');

  const batch = s.changesFrom({
    cursor: 12,
    states: [{ entity_id: 'run:///real/a', schema: 'run_state.v1', fields: { status: field({ value: 'done' }) } }],
  });
  assert(batch.cursor === 12 && batch.states.length === 1, 'a change batch carries its next cursor with it');
  assert(batch.states[0].id === 'run:///real/a', 'and a materialised state is read by entity_id when there is no id');
}

// ── A changed row is replaced where it stands, and keeps its name ──
//
// In place, not appended: a row that went from fresh to stale under the
// reader must not also jump across the screen. And a change batch carries
// STATE and no entity row, so overwriting wholesale would blank the label the
// reader is looking at every time a number moved.
{
  const a = entity({ id: 'run:///real/a' });
  const b = entity({ id: 'run:///real/b', display_name: 'nightly build', kind: 'run' });
  const bAgain = s.entityFrom({
    entity_id: 'run:///real/b', schema: 'run_state.v1',
    fields: { status: field({ value: 'done', freshness: 'stale' }) },
  });
  const c = entity({ id: 'run:///real/c' });

  const merged = s.mergeEntities([a, b], [bAgain, c]);
  assert(merged.map((e) => e.id).join() === 'run:///real/a,run:///real/b,run:///real/c', 'the order the reader had is kept');
  assert(merged[1].worst === 'stale', 'the changed row is the new one');
  assert(merged[1].displayName === 'nightly build', 'and it keeps the name the state batch did not carry');
  assert(merged[1].kind === 'run', 'along with everything else only the entity row knows');
  assert(s.mergeEntities([], [a]).length === 1, 'and a first batch is simply the list');
  assert(s.mergeEntities(undefined, undefined).length === 0, 'with nothing on either side answering nothing');
}

// ── A listing row is a summary, and is read as one ──
//
// `/entities` answers counts and ONE combined rating per row, because two
// hundred rows carrying every field with its metadata would be a megabyte to
// draw a sidebar. The screen must not read that emptiness as "nothing has
// ever been observed", nor a `mixed` summary as something it may act on.
{
  const listed = s.entityFrom({
    id: 'service:///real/ollama', kind: 'service', display_name: 'ollama',
    schema: 'service_state.v1',
    state: { known: true, revision: 7, freshness: 'aging', fields: 6, unknown_fields: 2, conflicts: 0, updated_at: ago(90) },
  });
  assert(listed.freshness === 'aging' && listed.worst === 'aging', 'the summary’s own rating is kept');
  assert(listed.revision === 7, 'and the revision beside it');
  assert(listed.fields.length === 0, 'with no field values, because the server sent none');

  const disagreeing = s.entityFrom({
    id: 'service:///real/x', schema: 'service_state.v1',
    state: { known: true, revision: 1, freshness: 'mixed', fields: 3, conflicts: 0 },
  });
  assert(disagreeing.freshness === 'mixed', 'a summary that says its fields disagree says so');
  assert(disagreeing.worst === 'stale',
    'and its worst is read DOWN, because `mixed` cannot say which field is the bad one and guessing upward would present it as current');
  assert(s.situationOf(disagreeing) === 'stale', 'so nothing reaches a live grouping through it');

  const empty = s.entityFrom({
    id: 'service:///real/y', schema: 'service_state.v1',
    state: { known: false, revision: 0, freshness: 'unknown', fields: 0 },
  });
  assert(empty.worst === 'unknown', 'a row nothing has observed says exactly that');
}

// ── A conflict is stamped onto the row it is about ──
//
// A listing row carries a COUNT of its conflicts and not their ids, so without
// this a disputed entity would be grouped by whatever its fields happen to
// say — which is precisely the claim the reducer refused to make.
{
  const rows = [
    entity({ id: 'run:///real/a', fields: { status: field({ value: 'running' }) } }),
    entity({ id: 'run:///real/b', fields: { status: field({ value: 'running' }) } }),
  ];
  const open = [s.conflictFrom({
    id: 'cfl_1', entity_id: 'run:///real/a', field: 'status', status: 'reconciling',
    claims: [{ source: 'runs', value: 'running' }, { source: 'queue', value: 'done' }],
    next_check: 'ask the queue',
  })];

  const stamped = s.withConflicts(rows, open);
  assert(stamped[0].conflictIds.join() === 'cfl_1', 'the row named by the conflict carries its id');
  assert(stamped[1].conflictIds.length === 0, 'and the row nobody disputes is untouched');
  assert(s.situationOf(stamped[0]) === 'conflict' && s.situationOf(stamped[1]) === 'running',
    'so one is in conflict and the other is running, which is the whole point');

  const settled = s.withConflicts(rows, [s.conflictFrom({
    id: 'cfl_2', entity_id: 'run:///real/a', field: 'status', status: 'resolved',
    claims: [{ source: 'runs', value: 'running' }, { source: 'queue', value: 'done' }],
  })]);
  assert(settled[0].conflictIds.length === 0, 'a settled conflict does not keep disputing forever');
  assert(s.withConflicts(undefined, undefined).length === 0, 'and nothing on either side is not a crash');

  const c = s.conflictFrom({ id: 'cfl_3', entity_id: 'run:///real/a', field: 'status',
    claims: [{ source: 'runs', value: 'running', observed_at: ago(10) }, { source: 'queue', value: 'done' }] });
  assert(c.claims.length === 2, 'both claims are kept');
  assert(c.claims[0].source === 'runs' && c.claims[1].value === 'done', 'neither of them chosen');
  assert(c.status === 'reconciling', 'and an unstated status is the live one');
}

// ── The mirror's opinion of itself ──
//
// `adapters` is what this build can observe with; `sources` is what each of
// them last did. Only the two together are honest.
{
  const d = s.diagnosticsFrom({
    enabled: true, sweep_seconds: 30,
    situations: ['running_work', 'blocked_work'],
    store: { cursor: 42, counts: { state_entities: 9, state_observations: 120 } },
    adapters: [
      { name: 'hardware', available: true, detail: '' },
      { name: 'comfyui', available: false, detail: 'ImportError: no module' },
      { name: 'runs', available: true, detail: '' },
    ],
    sources: [
      { source: 'hardware', health: 'ok', last_ok_at: ago(5), observations: 12, failures: 0, detail: '' },
      { source: 'retired-thing', health: 'degraded', last_error_at: ago(600), observations: 3, failures: 2, detail: 'timed out' },
    ],
  });
  const by = Object.fromEntries(d.sources.map((row) => [row.name, row]));
  assert(d.entities === 9 && d.observations === 120 && d.cursor === 42, 'the store counts are read from where they live');
  assert(d.enabled === true && d.sweepSeconds === 30, 'and so are the flag and the interval');
  assert(by.hardware.health === 'ok' && by.hardware.observations === 12, 'a source that ran reports what it did');
  assert(by.runs.health === 'unknown', 'one that is registered and has never run is unknown, not ok');
  assert(by.comfyui.available === false && by.comfyui.health === 'down', 'and one that cannot even import is down, with its reason');
  assert(by.comfyui.detail.includes('ImportError'), 'which is exactly what a diagnostics panel exists to show');
  assert(by['retired-thing'] !== undefined,
    'a source no adapter answers to any more still appears: it is holding fields nothing can refresh');
  assert(d.situations.join() === 'running_work,blocked_work', 'the situation queries are listed for the caller');
  assert(s.diagnosticsFrom(null).sources.length === 0, 'and nothing at all is not a crash');
}

// -- A card must not contradict itself --
//
// Found on the real screen: a session row drew "nothing has been observed
// about this yet" directly above "1 field(s)". Both sentences were computed
// correctly and they cannot both be true. `decidingField` returns null when
// the schema's deciding field was never observed, which is not the same fact
// as an entity with no fields, and the card has to tell them apart -- on a
// screen whose whole purpose is being trusted about what is and is not known.
{
  const withOther = s.entityFrom({
    id: 'session://alice/real/s1', kind: 'session', schema: 'session_state.v1',
    state: { revision: 1, fields: { queued_position: {
      value: 2, epistemic: 'observed', freshness: 'fresh', observed_at: ago(3),
      source: 'sessions', ttl_seconds: 60 } } },
  });
  assert(s.decidingField(withOther) === null,
    'the deciding field of this schema was never observed');
  assert(withOther.fields.length === 1,
    'and yet the row plainly holds one field, so "nothing observed" would be false');
  assert((s.decidingField(withOther) ?? withOther.fields[0]) !== null,
    'the card falls back to the field it does have rather than denying it exists');

  const withNothing = s.entityFrom({
    id: 'session://alice/real/s2', kind: 'session', schema: 'session_state.v1',
  });
  assert(withNothing.fields.length === 0 && s.decidingField(withNothing) === null,
    'a row with no fields at all is the only one that may say nothing was observed');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nALL OK');
