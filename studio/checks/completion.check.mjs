// The Completion screen's reasoning (studio/src/adapters/completion.ts).
//
// A completion decision says what a turn did beyond the literal ask, what it
// refused, and why it stopped. Everything the screen claims about that is
// derived in the adapter rather than inside a component, because each of these
// would be a lie of a different kind if it were wrong:
//
//   - a run that RAN OUT drawn as one that converged, which is the sentence
//     that stops anybody from ever raising the budget;
//   - a run that ended with work still open drawn as either of the other two,
//     which is the exact finding shadow mode exists to count;
//   - a shadow decision counted beside a real one, which ruins the measurement
//     shadow mode exists to produce;
//   - a budget line reported as spent when it was never opened, or an alias
//     line metered as a pot of its own, so five ceilings sum to more than the
//     total they came out of;
//   - a refusal drawn without the reason it was refused for, so a refusal
//     nobody justified cannot be told, a month later, from one nobody meant;
//   - work done beyond the ask folded into the core summary, so a reader
//     cannot tell what they asked for from what they got.
//
// Bundled with esbuild on the fly; run by tests/test_studio_completion_js.py,
// or by hand:
//   node studio/checks/completion.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-completion-'));

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

const c = await load(join('adapters', 'completion.ts'), 'completion.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

const candidate = (over) => ({
  id: 'improvement_1', title: 'add the missing regression test', layer: 'bonus',
  category: 'coverage', relation: 'adjacent', source: 'tests',
  expected_value: 0.5, estimated_cost: 0.2, risk: 0.1, confidence: 0.8,
  reversibility: 'full', status: 'done', ...over,
});

const budget = (over) => ({
  total: { rounds: 100 }, reserve_share: 0.15, bonus_share: 0.35, spent: {}, ...over,
});

const decision = (over) => c.decisionFrom({
  id: 'decision_1', contract_id: 'completion_1', mode: 'greedy',
  completed_layers: ['core'], stop_reason: 'converged',
  executed: [], rejected: [], deferred: [], budget: budget({}), ...over,
});

// ── Rule one: three endings, and no two of them are ever each other ──
//
// `converged` says there was nothing more worth doing. `budget` says the work
// was worth doing and the money ran out. `unfinished` says the turn ended with
// work still open and nothing stopped it. Accept it, raise the budget, go and
// find out: three sentences, three next actions, and a screen that drew any
// two of them alike would undo the contract the engine is built on.
{
  assert(c.endingForStop('converged') === 'finished', 'converged is an honest stop');
  assert(c.endingForStop('core_only') === 'finished',
    'and so is core_only: a literal run that did the ask has finished, it has not been cut short');
  assert(c.endingForStop('budget') === 'budget', 'running out is its own ending');
  assert(c.endingForStop('unfinished') === 'unfinished', 'and so is stopping with work open');

  assert(c.endingForStop('budget') !== c.endingForStop('converged'),
    'ran out and had nothing left to do are never the same ending');
  assert(c.endingForStop('unfinished') !== c.endingForStop('converged'),
    'nor are left work open and had nothing left to do');
  assert(c.endingForStop('unfinished') !== c.endingForStop('budget'),
    'nor are left work open and ran out: the first has money and stopped anyway');

  assert(c.endingTone('finished') !== c.endingTone('budget')
    && c.endingTone('finished') !== c.endingTone('unfinished')
    && c.endingTone('budget') !== c.endingTone('unfinished'),
    'and the three carry three different tones, so the distinction survives a reader who only skims the colour');
  assert(c.stopLabel('converged') !== c.stopLabel('budget')
    && c.stopLabel('budget') !== c.stopLabel('unfinished')
    && c.stopLabel('converged') !== c.stopLabel('unfinished'),
    'each says its own sentence too, so a reader without colour is still told which of the three it was');
  assert(c.stopLabel('core_only') !== c.stopLabel('converged'),
    'and the two honest stops are still two sentences: one ran out of ambition, the other ran out of mode');

  assert(c.endingForStop('scope') === 'interrupted' && c.endingForStop('risk') === 'interrupted'
    && c.endingForStop('blocked') === 'interrupted' && c.endingForStop('user') === 'interrupted'
    && c.endingForStop('cancelled') === 'interrupted' && c.endingForStop('failed') === 'interrupted',
    'every other stop is an interruption and is reported as one');
  assert(c.endingForStop('invented') === 'interrupted',
    'a stop reason this build never heard of is an interruption, NEVER finished: an unrecognised word must not be able to report itself as work that was completed');
  assert(c.endingForStop('') === 'interrupted' && c.endingForStop(null) === 'interrupted',
    'and nothing at all does not throw and does not pass');
  assert(c.HONEST_STOPS.indexOf('unfinished') < 0,
    '`unfinished` is deliberately not an honest stop: it is the word shadow mode exists to produce');
}

// ── A converged run whose own budget ran dry ──
//
// `CompletionDecision.parse` refuses to build one. This is what happens when
// one reaches the screen anyway -- from another build, a fixture, a migration,
// a hand-written row. It is drawn as `contested`, which claims NEITHER half,
// and the screen says why rather than correcting it silently.
{
  const dry = budget({ spent: { core: { rounds: 30 }, recovery: { rounds: 20 } } });
  const claimed = decision({ stop_reason: 'converged', budget: dry });

  assert(c.anyExhausted(claimed.budget) === true, 'the core pot is spent to its ceiling');
  assert(c.endingForStop(claimed.stopReason) === 'finished',
    'the record on its own says it finished');
  assert(c.endingOf(claimed) === 'contested',
    'and the screen draws it as contested, because a run that ran out did not converge');
  assert(c.endingOf(claimed) !== 'finished',
    'never as finished, which is the reading nobody ever goes back and checks');
  assert(c.honestStop(claimed) === false, 'so it is not counted as an honest stop either');
  assert(c.contestedStops([claimed, decision({})]).length === 1,
    'and it is reported as contested rather than only overruled, so the correction is auditable');

  const clean = decision({ stop_reason: 'converged' });
  assert(c.endingOf(clean) === 'finished' && c.honestStop(clean) === true,
    'a converged run with room left keeps the word it earned');

  // It never reads UP. The record's own word about how it ended is the one
  // thing this is not entitled to overrule in the direction of good news.
  const ranOut = decision({ stop_reason: 'budget', budget: budget({}) });
  assert(c.endingOf(ranOut) === 'budget',
    'a budget stop whose budget looks fine is still a budget stop');
  assert(c.endingOf(decision({ stop_reason: 'unfinished' })) === 'unfinished',
    'and an unfinished one is never promoted for having money left');
  assert(c.endingOf(null) === 'interrupted', 'nothing at all is not a finished run');

  assert(c.endingsAreNeverConflated(claimed) === true,
    'the screen invariant holds even on the hostile record: that is what it means for the screen to defend itself');
  assert(c.endingsAreNeverConflated(clean) === true, 'and on an honest one');
  assert(c.endingsAreNeverConflated(null) === true, 'and on nothing at all');
}

// ── The budget: spends, three pots, and ceilings that partition the total ──
//
// Every field is a SPEND and not a remainder. A remainder is a derived number
// that goes wrong the moment two writers disagree about the total, and the
// question a caller asks -- "may I spend this?" -- is answered from the spend
// and the ceiling together.
{
  const b = c.budgetFrom(budget({ spent: { core: { rounds: 30 }, recovery: { rounds: 20 } } }));

  assert(c.ceiling(b, 'verification', 'rounds') === 15, 'the reserve is its share of the TOTAL');
  assert(c.ceiling(b, 'bonus', 'rounds') === 35, 'the bonus share is of the total too, not of what is left');
  assert(c.ceiling(b, 'core', 'rounds') === 50, 'and core gets the remainder of the three');
  assert(c.ceiling(b, 'core', 'rounds') + c.ceiling(b, 'verification', 'rounds')
    + c.ceiling(b, 'bonus', 'rounds') === 100,
    'the three pots partition the total EXACTLY: metering the five lines apart made them sum to 185% of it');

  assert(c.ceiling(b, 'recovery', 'rounds') === c.ceiling(b, 'core', 'rounds'),
    'recovery reads the core ceiling, because repairing a regression IS core work');
  assert(c.ceiling(b, 'exploration', 'rounds') === c.ceiling(b, 'bonus', 'rounds'),
    'and exploration reads the bonus one: maximalist explores by spending its larger share');
  assert(c.fundingPot('recovery') === 'core' && c.fundingPot('exploration') === 'bonus',
    'the map says which lines are views of which pot');
  assert(c.isAlias('recovery') === true && c.isAlias('core') === false,
    'and which of them own a pot of their own, which is the fact that explains two identical rows');
  assert(c.fundingPot('invented') === 'invented',
    'an unknown line is returned untouched, so the value shows up in the message rather than being silently rehomed');

  assert(c.used(b, 'core', 'rounds') === 50,
    'the pot has spent what BOTH its lines booked to it: reading one key would approve the second request against money the first took');
  assert(c.used(b, 'recovery', 'rounds') === c.used(b, 'core', 'rounds'),
    'and asking through either alias gives the same answer');
  assert(c.remaining(b, 'core', 'rounds') === 0, 'so nothing is left in it');
  assert(c.used(b, 'bonus', 'rounds') === 0 && c.remaining(b, 'bonus', 'rounds') === 35,
    'while the bonus pot, which nothing was booked to, is untouched');

  assert(c.exhaustedUnits(b, 'core').join() === 'rounds', 'core has run out of rounds');
  assert(c.exhaustedLines(b).join() === 'core,recovery',
    'and both lines that draw on it say so, in BUDGET_LINES order');
  assert(c.anyExhausted(b) === true, 'which is what refuses a dishonest `converged`');
}

// ── Ran out and never started are different facts ──
//
// `literal` runs with `bonus_share = 0`. A bonus line reported as spent would
// make the server refuse `core_only` -- the one honest stop a literal run can
// have -- on the grounds that it ran out of a budget it was never given.
{
  const literal = c.budgetFrom(budget({
    total: { rounds: 10 }, bonus_share: 0, spent: { core: { rounds: 1 } },
  }));
  assert(c.ceiling(literal, 'bonus', 'rounds') === 0, 'a literal run has no bonus ceiling at all');
  assert(c.exhaustedUnits(literal, 'bonus').length === 0,
    'and a line that was never opened is NOT exhausted: ran out and never started are different facts');
  assert(c.anyExhausted(literal) === false, 'so nothing about it contests an honest stop');

  const coreOnly = decision({ stop_reason: 'core_only', mode: 'literal', budget: {
    total: { rounds: 10 }, bonus_share: 0, spent: { core: { rounds: 1 } },
  } });
  assert(c.endingOf(coreOnly) === 'finished',
    'so the one honest stop a literal run has survives, which is the whole point of the distinction');

  // A unit the turn declared no total for is not counted, and something that
  // is not counted cannot run out of itself.
  assert(c.ceiling(literal, 'core', 'tokens') === 0, 'an undeclared unit has no ceiling');
  assert(c.exhaustedUnits(literal, 'core').length === 0,
    'and is not reported as spent: `0 of 0` would report every uncounted unit as exhausted');
  const rows = c.budgetRows(literal);
  const core = rows.find((row) => row.line === 'core');
  assert(core.units.find((u) => u.unit === 'tokens').counted === false,
    'the table says `not counted` rather than drawing a zero');
  assert(core.units.find((u) => u.unit === 'rounds').counted === true, 'and counts what was counted');
  assert(rows.length === c.BUDGET_LINES.length,
    'all five lines are returned, so a reader cannot mistake three rows for the lines that exist');
  assert(rows.every((row) => row.units.length === c.SPENDABLE_UNITS.length),
    'each with all four units: they run out at different times and one number would hide which');
  assert(rows.find((row) => row.line === 'recovery').alias === true
    && rows.find((row) => row.line === 'core').alias === false,
    'and each says whether its numbers are a view of another row');

  assert(c.budgetRows(null).length === c.BUDGET_LINES.length, 'nothing at all still draws the lines');
  assert(c.anyExhausted(null) === false, 'and nothing at all has not run out of anything');
  assert(c.spendOf(null, 'rounds') === 0 && c.spendOf({ rounds: 3 }, 'invented') === 0,
    'an unknown unit reads as zero rather than NaN, which would poison every sum it touched');
}

// ── Rule two: shadow and real are never mixed silently ──
//
// Shadow mode measures what the engine WOULD have done. Counting those beside
// what it did ruins the measurement it exists to produce.
{
  const real = c.summaryFrom({ id: 'd1', mode: 'greedy', stop_reason: 'converged', shadow: false });
  const shade = c.summaryFrom({ id: 'd2', mode: 'greedy', stop_reason: 'converged', shadow: true });

  assert(real.shadow === false && shade.shadow === true, 'the flag survives the read');
  assert(c.mixesShadow([real, shade]) === true, 'both kinds on one screen is announced');
  assert(c.mixesShadow([real, real]) === false && c.mixesShadow([shade, shade]) === false,
    'and one kind alone is not: the warning is about MIXING, not about shadow existing');
  assert(c.mixesShadow([]) === false && c.mixesShadow(null) === false, 'nothing at all is not a crash');

  const split = c.partitionShadow([real, shade, shade]);
  assert(split.real.length === 1 && split.shadow.length === 2,
    'the two kinds come back apart, and neither list is ever folded into the other');
  assert(split.real.every((row) => !row.shadow) && split.shadow.every((row) => row.shadow),
    'with nothing filed on the wrong side');

  assert(c.summaryFrom({}).shadow === false,
    'a row with no flag reads as real -- the same default the server queries with, so the two agree about what a bare row is');
  assert(c.decisionFrom({ shadow: true }).shadow === true, 'and a whole decision carries it too');
  assert(c.summarize(c.decisionFrom({ id: 'd3', shadow: true, stop_reason: 'converged' })).shadow === true,
    'so a card built from a whole decision cannot lose the mark a card built from a row would have carried');
}

// ── What the server claimed about how it ended, versus what its own stop
//    reason supports ──
{
  const straight = c.summaryFrom({ id: 'd1', stop_reason: 'converged', honest_stop: true });
  assert(c.disputedHonesty(straight) === false, 'a row whose two fields agree is not flagged');

  const liar = c.summaryFrom({ id: 'd2', stop_reason: 'budget', honest_stop: true });
  assert(c.disputedHonesty(liar) === true,
    'a row calling a budget exhaustion an honest stop is flagged: something wrote it without going through the contract');
  const shy = c.summaryFrom({ id: 'd3', stop_reason: 'converged', honest_stop: false });
  assert(c.disputedHonesty(shy) === true, 'and so is the mirror image, which is just as much a disagreement');
  assert(c.disputedHonesty(null) === false, 'nothing at all disagrees with nothing');

  assert(c.summaryFrom({}).stopReason === 'unfinished',
    'a row that never said how it ended reads as unfinished, NEVER as converged: a record that did not tell us the work was done did not tell us the work was done');
  assert(c.decisionFrom({}).stopReason === 'unfinished', 'and so does a whole decision');
}

// ── Rule three: executed, refused and deferred stay three lists ──
{
  const run = decision({
    completed_layers: ['core', 'professional', 'bonus'],
    executed: [
      candidate({ id: 'i_core', layer: 'core', title: 'the thing that was asked for',
        evidence_refs: ['test:the_failing_one'], expected_value: 0.9 }),
      candidate({ id: 'i_pro', layer: 'professional', title: 'the error path nobody would ship without',
        evidence_refs: ['static:unhandled'], expected_value: 0.7 }),
      candidate({ id: 'i_bonus', layer: 'bonus', title: 'the same bug two files over', expected_value: 0.4 }),
      candidate({ id: 'i_explore', layer: 'exploratory', title: 'a faster algorithm', expected_value: 0.3 }),
    ],
    rejected: [
      candidate({ id: 'i_refused', layer: 'bonus', status: 'rejected', rejection_reason: 'out_of_scope',
        title: 'rewrite the neighbouring module' }),
      candidate({ id: 'i_bare', layer: 'bonus', status: 'rejected', title: 'nobody said why' }),
    ],
    deferred: [
      candidate({ id: 'i_later', layer: 'bonus', status: 'deferred', rejection_reason: 'round_full',
        title: 'wanted, and the round was full' }),
    ],
  });

  assert(run.executed.length === 4 && run.rejected.length === 2 && run.deferred.length === 1,
    'the three lists arrive as three lists and are never concatenated');
  assert(c.extras(run).map((x) => x.id).join() === 'i_bonus,i_explore',
    'the extras are exactly the two layers beyond the ask, so §30 can be answered honestly');
  assert(c.extras(run).every((x) => c.REQUIRED_LAYERS.indexOf(x.layer) < 0),
    'and nothing from the request is ever counted as an extra');
  assert(c.byLayer(run, 'core').length === 1 && c.byLayer(run, 'exploratory').length === 1,
    'each layer answers for itself, so what was asked for and what was added are never one list');

  const counted = c.counts(run);
  assert(counted.executed === 4 && counted.extras === 2, 'the counts a card prints agree with the lists a reader opens');
  assert(counted.rejected === 2 && counted.deferred === 1, 'refused and deferred are counted apart');
  assert(Object.keys(counted.perLayer).join() === c.LAYERS.join(),
    'all four layers get a count, zeros included: an absent heading is read as a zero anyway');
  assert(counted.perLayer.core + counted.perLayer.professional
    + counted.perLayer.bonus + counted.perLayer.exploratory === counted.executed,
    'and no executed row is counted twice or lost between the layers');
}

// ── A refusal nobody justified ──
//
// §1.8: a rejected opportunity must not reappear without new evidence, and a
// rejection nobody wrote down comes back every single round. The server
// refuses to build a rejected row without a reason; one that arrives here
// anyway is drawn as `nobody recorded why` rather than passing quietly.
{
  const explained = c.candidateFrom(candidate({ status: 'rejected', rejection_reason: 'below_threshold' }));
  assert(c.effectiveRejection(explained) === 'below_threshold', 'a recorded reason keeps its word');
  assert(c.rejectionLabel('below_threshold') !== c.rejectionLabel('round_full'),
    'and `not worth doing` and `wanted, and the round was full` are two different sentences: filing the second as the first tells the next reader that good work was judged worthless');

  const bare = c.candidateFrom(candidate({ status: 'rejected', rejection_reason: '' }));
  assert(c.effectiveRejection(bare) === c.UNRECORDED_REASON,
    'a refusal with no reason reads as `unrecorded`');
  const invented = c.candidateFrom(candidate({ status: 'rejected', rejection_reason: 'because_i_said_so' }));
  assert(c.effectiveRejection(invented) === c.UNRECORDED_REASON,
    'and so does a word this build has never heard of: printing the raw token would let it pass for a reason somebody gave');
  assert(c.REJECTION_REASONS.indexOf(c.UNRECORDED_REASON) < 0,
    '`unrecorded` is deliberately NOT one of the reasons: it is the absence of a word, not a word');
  assert(c.rejectionLabel(c.UNRECORDED_REASON) !== c.rejectionLabel('below_threshold'),
    'and it says so in its own sentence rather than borrowing one');

  const run = decision({ rejected: [
    candidate({ id: 'i_ok', status: 'rejected', rejection_reason: 'dominated' }),
    candidate({ id: 'i_bare', status: 'rejected' }),
  ] });
  assert(c.unexplainedRefusals(run).length === 1
    && c.unexplainedRefusals(run)[0].id === 'i_bare',
    'the unexplained ones are reported, so the reader can see which refusals nobody has to answer for');
  assert(c.counts(run).unexplained === 1 && c.counts(run).rejected === 2,
    'and they are counted apart from the total rather than folded into it');
  assert(c.unexplainedRefusals(decision({})).length === 0, 'a run with nothing to report reports nothing');

  const tally = c.rejectionTally(run);
  assert(tally.dominated === 1 && tally[c.UNRECORDED_REASON] === 1,
    'the tally counts the unexplained ones under their own key rather than dropping them');
  assert(c.refusalsAreAlwaysExplained(run) === true,
    'the screen invariant holds: every refusal is drawn under a reason, and none is lost between the lists');
  assert(c.refusalsAreAlwaysExplained(null) === true, 'and on nothing at all');

  // A deferral is allowed to carry no reason: deferring is a decision to
  // decide later, and §19 hands those on to be reconsidered.
  const later = decision({ deferred: [candidate({ id: 'i_l', status: 'deferred' })] });
  assert(c.unexplainedRefusals(later).length === 0,
    'a deferral with no reason is not an unexplained REFUSAL: they are different decisions');
}

// ── What the record holds after a person says no ──
//
// The route answers `{ok:true, recorded:<reason>}` without writing anything
// (a known defect, held as a strict xfail in
// tests/test_completion_engine_routes.py), so the screen re-reads and reports
// the status the store actually holds instead of the flag the response
// carried. Printing "recorded" on the strength of that flag would be exactly
// the kind of unbacked claim this screen exists to refuse.
{
  const run = decision({
    executed: [candidate({ id: 'i_done', layer: 'bonus', status: 'done' })],
    rejected: [candidate({ id: 'i_no', status: 'rejected', rejection_reason: 'risk' })],
    deferred: [candidate({ id: 'i_later', status: 'deferred', rejection_reason: 'round_full' })],
  });
  assert(c.statusOf(run, 'i_no') === 'rejected',
    'a candidate the store really did refuse reads back as refused');
  assert(c.statusOf(run, 'i_later') === 'deferred',
    'and one the write did not touch still reads back as deferred, which is what the screen says out loud');
  assert(c.statusOf(run, 'i_done') === 'done', 'the search covers all three lists, not only the refused one');
  assert(c.statusOf(run, 'i_missing') === '',
    'an id in no list answers with nothing rather than inventing a status');
  assert(c.statusOf(run, '') === '' && c.statusOf(null, 'i_no') === '', 'nothing at all is not a crash');
}

// ── A candidate's layer and the money behind it are two questions ──
{
  assert(c.lineForLayer('core') === 'core', 'core work spends the core line');
  assert(c.lineForLayer('professional') === 'core',
    'professional work has no line of its own: it is required work and it spends the core pot');
  assert(c.lineForLayer('bonus') === 'bonus', 'a bonus spends the bonus line');
  assert(c.lineForLayer('exploratory') === 'exploration', 'and exploration has its own line');
  assert(c.potForLayer('exploratory') === 'bonus',
    'which is a VIEW of the bonus pot, so the screen can print both halves rather than implying a fourth pot');
  assert(c.potForLayer('professional') === c.potForLayer('core'),
    'and the two required layers share one pot, which is the fact that explains two identical rows');
  assert(c.lineForLayer('invented') === 'exploration',
    'a layer this build never heard of funds from the most optional line there is, which cannot promote an unrecognised word into work that blocks the close');

  assert(c.layerRank('core') < c.layerRank('professional'), 'core is more obligatory than professional');
  assert(c.layerRank('professional') < c.layerRank('bonus'), 'and professional than bonus');
  assert(c.layerRank('bonus') < c.layerRank('exploratory'), 'and bonus than exploratory');
  assert(c.layerRank('invented') === c.layerRank('exploratory'),
    'and an unknown layer ranks LAST, the most optional, which is the safe direction');
  assert(c.layerLabel('bonus') !== c.layerLabel('core')
    && c.layerLabel('exploratory') !== c.layerLabel('core'),
    'the two layers beyond the ask say so in their own words, never as a shade of the core’s');
}

// ── Ordering: the request first, then what it was worth ──
{
  const rows = [
    c.candidateFrom(candidate({ id: 'a_explore', layer: 'exploratory', expected_value: 0.9, title: 'zzz' })),
    c.candidateFrom(candidate({ id: 'a_bonus_low', layer: 'bonus', expected_value: 0.1, title: 'bbb' })),
    c.candidateFrom(candidate({ id: 'a_bonus_high', layer: 'bonus', expected_value: 0.8, title: 'aaa' })),
    c.candidateFrom(candidate({ id: 'a_core', layer: 'core', expected_value: 0.2, title: 'ccc',
      evidence_refs: ['x'] })),
  ];
  const ordered = c.orderedCandidates(rows);
  assert(ordered[0].id === 'a_core',
    'the core row is first however little it was worth: it is the request, and the request is not competing with the extras');
  assert(ordered[1].id === 'a_bonus_high' && ordered[2].id === 'a_bonus_low',
    'within a layer the more valuable comes first');
  assert(ordered[3].id === 'a_explore', 'and the most optional layer is last, whatever it claimed to be worth');
  assert(c.orderedCandidates(rows).map((x) => x.id).join() === ordered.map((x) => x.id).join(),
    'the order is stable: sorting twice gives the same page');
  assert(rows[0].id === 'a_explore', 'and the caller’s own array was not re-ordered underneath it');
  assert(c.orderedCandidates(undefined).length === 0, 'nothing to order is not a crash');
  assert(c.orderedCandidates([null, c.candidateFrom(candidate({}))]).length === 1,
    'and an unreadable row is skipped, not fatal');

  const tie = c.orderedCandidates([
    c.candidateFrom(candidate({ id: 'b', title: 'zzz' })),
    c.candidateFrom(candidate({ id: 'a', title: 'aaa' })),
  ]);
  assert(tie[0].title === 'aaa', 'rows alike in every other way fall back to their title, never to insertion order');
}

// ── Proof: `referenced` is not `proved` ──
//
// A run whose proof refs are non-empty has pointed at a proof somebody else
// holds. It has not shown a verdict, and this screen holds none. Upgrading the
// first into the second would be the screen inventing the one word the whole
// engine is careful about.
{
  const pointed = decision({ proof_refs: ['proof:run_9f2c'] });
  assert(c.proofStatus(pointed) === 'referenced', 'a run that points at a proof says so');
  assert(c.proofStatus(decision({})) === 'none',
    'and one that points at nothing says `none`, which is not the same as unproved: unproved is a verdict somebody computed');
  assert(c.proofStatus(pointed) !== 'proved' && c.proofStatus(decision({})) !== 'proved',
    'neither of the two answers this function can give is `proved`: there is no path through it that claims a verdict');
  assert(c.proofStatus(null) === 'none', 'nothing at all has established nothing');
}

// ── What was not consulted ──
//
// A frontier computed without the Delta Engine could not see scope creep, and
// reporting that as a clean stop would be the same lie as a `preserved` with
// no observation behind it.
{
  const dark = decision({ degraded_integrations: ['delta_engine', 'prove'] });
  assert(dark.degradedIntegrations.length === 2, 'the systems that were down come through whole');
  assert(c.counts(dark).degraded === 2, 'and are counted, so the card can carry the caveat');
  assert(c.counts(decision({})).degraded === 0,
    'a healthy run counts none, which is a positive statement and not an absent one');
  assert(c.summarize(dark).degraded.join() === 'delta_engine,prove',
    'and a card built from the whole decision carries the same list the listing row would have');
}

// ── A refusal is a token, not a sentence ──
//
// A rejected request answers 200 with `ok:false`. A caller that only checked
// the HTTP status would draw an empty list and call it "nothing decided".
{
  assert(c.refusalOf({ ok: true, decisions: [] }) === null, 'a success is not a refusal');
  assert(c.refusalOf(null) === null && c.refusalOf('nope') === null, 'nothing is not a refusal either');

  const bad = c.refusalOf({
    ok: false,
    error: { path: 'shadow', code: 'invalid_argument', message: 'must be true, false or all' },
  });
  assert(bad.code === 'invalid_argument', 'the token survives, and it is what a caller branches on');
  assert(bad.path === 'shadow' && bad.message.includes('true, false or all'),
    'and so do the field that was wrong and the sentence for the person reading');

  const bare = c.refusalOf({ ok: false });
  assert(bare.code === 'refused' && bare.message.length > 0, 'a refusal with no token is still a refusal');

  const raised = new c.CompletionRefusal(bad);
  assert(raised instanceof Error && raised.code === 'invalid_argument',
    'CompletionRefusal is a real Error carrying the token');
  assert(raised.message === bad.message, 'and its message is the server’s own sentence');
}

// ── A listing row is what the server computed, read as sent ──
{
  const row = c.summaryFrom({
    id: 'decision_1', mode: 'maximalist', layers: ['core', 'professional', 'bonus'],
    executed: { core: 2, professional: 1, bonus: 3, exploratory: 1 },
    extras: 4, rejected: 5, deferred: 2,
    rejection_reasons: { out_of_scope: 3, round_full: 2 },
    stop_reason: 'budget', honest_stop: false, degraded: ['prove'], shadow: false,
    run_id: 'run_9f2c', project_id: 'proj_1', created_at: '2026-09-06T09:00:00Z',
  });
  assert(row.executed.bonus === 3 && row.extras === 4,
    'the snake_case the server speaks is read into the shape the screen speaks');
  assert(row.rejectionReasons.out_of_scope === 3,
    'and the rejection tally arrives keyed by the closed vocabulary');
  assert(row.runId === 'run_9f2c' && row.projectId === 'proj_1',
    'with what it takes to find the turn behind the decision');
  assert(c.summaryFrom({ executed: { core: 'lots' } }).executed.core === undefined,
    'a count that is not a number is dropped rather than coerced into one somebody could read as measured');
  assert(c.summaryFrom({}).extras === 0,
    'a row with no extras count reads as zero: a COUNT is a number the server computed, unlike a stop reason, where absence is a fact about the record');
}

// ── The server's own vocabularies win, and an empty list never does ──
{
  const config = c.configFrom({
    enabled: true, shadow_enabled: false,
    modes: ['literal', 'greedy'], stop_reasons: [], rejection_reasons: ['out_of_scope'],
    policies: { greedy: { policy_version: 'v3', bonus_budget_share: 0.35, requires_verification: true } },
  });
  assert(config.modes.join() === 'literal,greedy',
    'a build that knows a different set of modes offers its own in the filters');
  assert(config.stopReasons.join() === c.STOP_REASONS.join(),
    'and a list the server did not send falls back to this build’s, rather than to an empty filter that hides every row');
  assert(config.policies.greedy.bonusBudgetShare === 0.35 && config.policies.greedy.requiresVerification === true,
    'the policies are read, including the share whose denominator is the turn’s TOTAL');
  assert(config.enabled === true && config.shadowEnabled === false,
    'and the two switches are two fields: the engine being on says nothing about shadow');

  const modes = c.modesFrom({ enabled: false, modes: [], layers: [] });
  assert(modes.modes.join() === c.COMPLETION_MODES.join() && modes.layers.join() === c.LAYERS.join(),
    'an empty answer from /modes falls back to the four and the four rather than drawing nothing');

  const settings = c.settingsFrom({ enabled: true, shadow_enabled: true, verification_reserve: 0.15, max_bonus_rounds: 3 });
  assert(settings.verificationReserve === 0.15 && settings.maxBonusRounds === 3,
    'the reserve and the round cap are read as the numbers they are');
  assert(c.settingsFrom({}).enabled === false,
    'and a settings read that answered nothing does not report the engine as on');

  const diag = c.diagnosticsFrom({ enabled: true, counts: { decisions: 9 }, rejections: { out_of_scope: 2 },
    degraded: ['delta_engine'], db_path: 'data/completion.db', schemas: ['decisions'] });
  assert(diag.counts.decisions === 9 && diag.rejections.out_of_scope === 2, 'the diagnostics carry both tallies');
  assert(diag.degraded.join() === 'delta_engine' && diag.dbPath === 'data/completion.db',
    'and what was dark, and where the rows live');
}

// ── Reconnection: the cursor goes forward, or it stays where it was ──
{
  const events = [
    c.eventFrom({ id: 'e1', seq: 4, name: 'completion_candidate_discovered', run_id: 'run_1' }),
    c.eventFrom({ id: 'e2', seq: 9, name: 'completion_decision_recorded', run_id: 'run_1' }),
  ];
  assert(c.advanceCursor(1, events) === 9, 'the cursor lands on the highest frame it saw');
  assert(c.advanceCursor(9, []) === 9, 'an empty answer leaves it where it was');
  assert(c.advanceCursor(9, [c.eventFrom({ name: 'ping' })]) === 9,
    'a frame with no seq never blanks it: a cursor of zero would replay the stream and count one turn twice');
  assert(c.advanceCursor(9, [c.eventFrom({ seq: 4 })]) === 9,
    'and a frame that arrived out of order never drags it backwards');
  assert(c.advanceCursor(-5, []) === 0, 'a nonsense cursor floors at the beginning rather than going negative');

  assert(c.eventFrom({ name: 'completion_converged', payload: { run_id: 'run_2' } }).runId === 'run_2',
    'the run id is read from the payload when the frame did not repeat it at the top');
  assert(c.eventFrom(null).name === '' && c.eventFrom(null).seq === 0, 'nothing at all is not a crash');

  // §11's two stop names, and the reason there are two of them.
  assert(c.isStopEvent('completion_converged') && c.isStopEvent('completion_budget_exhausted'),
    'both stop names are recognised');
  assert(!c.isStopEvent('completion_candidate_discovered'),
    'and a frame about one candidate is not a frame about the whole run');
}

// ── A card built from a whole decision says what a listing row would ──
{
  const full = decision({
    id: 'decision_9f2c', mode: 'greedy', project_id: 'proj_1', run_id: 'run_1',
    created_at: '2026-09-06T10:00:00Z', completed_layers: ['core', 'bonus'],
    stop_reason: 'unfinished',
    executed: [
      candidate({ id: 'i1', layer: 'core', evidence_refs: ['x'] }),
      candidate({ id: 'i2', layer: 'bonus' }),
    ],
    rejected: [candidate({ id: 'i3', status: 'rejected', rejection_reason: 'risk' })],
    deferred: [candidate({ id: 'i4', status: 'deferred', rejection_reason: 'round_full' })],
  });
  const s = c.summarize(full);

  assert(s.id === 'decision_9f2c' && s.mode === 'greedy' && s.projectId === 'proj_1',
    'the card carries what it takes to open the detail and to filter the list');
  assert(s.extras === 1 && s.executed.core === 1 && s.executed.bonus === 1,
    'the extras are counted apart from the request, on the card as well as in the detail');
  assert(s.rejected === 1 && s.deferred === 1, 'and the refusals and the deferrals stay two numbers');
  assert(s.honestStop === false && c.endingForStop(s.stopReason) === 'unfinished',
    'an unfinished run is not an honest stop, and the card says so');
  assert(c.disputedHonesty(s) === false,
    'and the card it produces does not contradict itself, which is what makes the flag meaningful when it fires');
  assert(s.rejectionReasons.risk === 1, 'with the reasons tallied, ready for the list');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nALL OK');
