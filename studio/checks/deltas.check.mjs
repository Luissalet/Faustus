// The Deltas screen's reasoning (studio/src/adapters/deltas.ts).
//
// A delta says what changed, whether it is what was asked for, and how well we
// know. Everything the screen claims about that is derived in the adapter
// rather than inside a component, because each of these would be a lie of a
// different kind if it were wrong:
//
//   - `preserved` drawn for a row nobody measured. "Not detected is not
//     preserved" is the whole promise of the subsystem, and this screen is
//     where the promise is either kept or visibly broken;
//   - `unknown` filed amongst the greens, which is the same lie wearing a
//     sorting function instead of a colour;
//   - coverage and confidence multiplied into one percentage, which hides both
//     the extractor that could not be certain and the region nobody looked at;
//   - an unmeasured coverage axis drawn as zero, so "we did not look" is
//     reported as "we looked and found nothing there";
//   - a blocking regression summarised into a count;
//   - two findings ordered so the cosmetic one comes first, or ordered
//     differently here than on the server, so two readers of one delta
//     disagree about what it said.
//
// Bundled with esbuild on the fly; run by tests/test_studio_deltas_js.py, or by
// hand:
//   node studio/checks/deltas.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-deltas-'));

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

const d = await load(join('adapters', 'deltas.ts'), 'deltas.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

const sha = (c) => c.repeat(64);

const revision = (over) => ({
  kind: 'checkpoint', ref: 'checkpoint:9f2c', hash: sha('a'), label: 'before',
  sensitivity: 'internal', ...over,
});

const assertion = (over) => ({
  id: 'assertion_1', path: 'character.jacket.color', operation: 'modified',
  classification: 'unknown', severity: 'info', confidence: 'medium',
  tier: 'algorithm', ...over,
});

const invariantResult = (over) => ({
  invariant_id: 'inv_face', status: 'unknown', confidence: 'unknown',
  tier: 'algorithm', severity: 'material', ...over,
});

const delta = (over) => d.deltaFrom({
  id: 'delta_1', request_id: 'delta_request_1', owner: 'alice', domain: 'image',
  source: revision(), target: revision({ hash: sha('b'), label: 'after' }),
  assessment: 'partial', assertions: [], invariants: [],
  coverage: { source_readable: true, target_readable: true, dimensions: {} },
  ...over,
});

// ── Two axes, and they are allowed to disagree ──
//
// `operation` is what the extractor SAW; `classification` is what it means
// against the frozen intent. `modified` + `requested` is success and
// `modified` + `regression` is a bug shipped: the same observation, opposite
// conclusions. Collapsing them into one list is what makes "it changed" and
// "it should not have changed" indistinguishable.
{
  const success = d.assertionFrom(assertion({ operation: 'modified', classification: 'requested' }));
  const shipped = d.assertionFrom(assertion({ operation: 'modified', classification: 'regression', severity: 'blocking' }));
  assert(success.operation === shipped.operation, 'the same observation reaches both rows');
  assert(success.classification !== shipped.classification, 'and they mean opposite things');
  assert(d.operationLabel('modified') === d.operationLabel(shipped.operation),
    'the operation is read by one function for both');
  assert(d.classificationLabel('requested') !== d.classificationLabel('regression'),
    'and the classification by another, so a screen cannot print one for the other');
  assert(d.operationLabel('reencoded') !== d.operationLabel('unchanged'),
    're-encoded is its own word: it changes the bytes and calling it unchanged hides a lost gradient');
  assert(d.CHANGE_OPERATIONS.indexOf('reencoded') >= 0, 'and it asserts a difference');
  assert(d.CHANGE_OPERATIONS.indexOf('unchanged') < 0,
    'while `unchanged` is a real observation and not a change');
  assert(d.operationLabel('invented').length > 0 && d.classificationLabel('invented') === d.classificationLabel('unknown'),
    'a word this build never heard of reads as not-checked, never as a conclusion');
}

// ── The ladders, and which way each of them runs ──
{
  assert(d.severityRank('blocking') > d.severityRank('material'), 'blocking outranks material');
  assert(d.severityRank('material') > d.severityRank('minor'), 'material outranks minor');
  assert(d.severityRank('minor') > d.severityRank('info'), 'and minor outranks info');
  assert(d.severityRank('invented') === d.severityRank('info'),
    'a severity nobody declared ranks lowest, so a typo cannot promote a cosmetic row to the top');

  assert(d.confidenceRank('exact') < d.confidenceRank('high'), 'exact beats high');
  assert(d.confidenceRank('high') < d.confidenceRank('medium'), 'high beats medium');
  assert(d.confidenceRank('medium') < d.confidenceRank('low'), 'medium beats low');
  assert(d.confidenceRank('low') < d.confidenceRank('unknown'), 'and low beats not knowing at all');
  assert(d.confidenceRank('invented') === d.confidenceRank('unknown'),
    'a confidence this build never heard of ranks WEAKEST, which is the direction that cannot launder a guess into a certainty');

  assert(d.weakestConfidence(['exact', 'unknown']) === 'unknown',
    'combining confidences is a min over the ladder: an average of exact and unknown would be `medium`, a number nobody observed');
  assert(d.weakestConfidence([]) === 'unknown', 'and nothing at all is not evidence of certainty');
  assert(d.strongestSeverity(['info', 'blocking', 'minor']) === 'blocking',
    'combining severities goes the other way: one blocking finding among forty cosmetic ones is a blocking delta');
  assert(d.strongestSeverity([]) === 'info', 'with nothing at all the strongest is the weakest word there is');

  assert(d.tierRank('hash') < d.tierRank('perceptual'), 'a hash is more determinate than a perceptual metric');
  assert(d.tierRank('invented') === d.tierRank('human'), 'and an unnamed method ranks with the last tier, never the first');
  assert(d.ceilingForTier('perceptual') === 'medium',
    'a perceptual metric may not claim better than medium, because a perceptual hash is not identity');
  assert(d.ceilingForTier('human') === 'high',
    'and a person is a strong witness, not a checksum');
  assert(d.ceilingForTier('nothing-declared') === 'low', 'an unknown tier is capped at low');
  assert(d.cappedByTier('medium', 'perceptual') === true,
    'a finding sitting exactly on its tier ceiling is known to be held there');
  assert(d.cappedByTier('low', 'perceptual') === false,
    'and one below it was held down by its own measurement, which is a different sentence');
}

// ── Rule one: not detected is not preserved ──
//
// `preserved` costs an observation and a threshold. The server refuses to
// build a row that claims it without one; this is what happens when such a row
// reaches the screen anyway -- from another build, a fixture, a migration, a
// hand-written record. It reads DOWN to `unknown`, never up.
{
  const earned = d.assertionFrom(assertion({
    classification: 'preserved', operation: 'unchanged', confidence: 'high', tier: 'parser',
  }));
  assert(d.effectiveClassification(earned) === 'preserved',
    'a preserved row with a measurement behind it keeps the word it earned');

  const unmeasured = d.assertionFrom(assertion({
    id: 'assertion_unmeasured', classification: 'preserved', operation: 'unchanged', confidence: 'unknown',
  }));
  assert(d.effectiveClassification(unmeasured) === 'unknown',
    'a preserved row with `unknown` confidence is read DOWN to not-checked: a detector that found nothing has found nothing');

  const contradictory = d.assertionFrom(assertion({
    id: 'assertion_contradictory', classification: 'preserved', operation: 'modified', confidence: 'exact',
  }));
  assert(d.effectiveClassification(contradictory) === 'unknown',
    'and one saying it stayed beside a word saying it moved is not resolved in favour of the friendlier half');

  assert(d.effectiveClassification(d.assertionFrom(assertion({ classification: 'invented' }))) === 'unknown',
    'a classification this build never heard of is not-checked, not a conclusion');
  assert(d.effectiveClassification(null) === 'unknown', 'and nothing at all does not throw');

  const hostile = delta({ assertions: [earned, unmeasured, contradictory].map((a) => ({
    id: a.id, path: a.path, operation: a.operation, classification: a.classification,
    severity: a.severity, confidence: a.confidence, tier: a.tier,
  })) });
  const groups = d.groupByClassification(hostile.assertions);
  assert(groups.preserved.length === 1 && groups.preserved[0].id === 'assertion_1',
    'only the earned row is filed under preserved');
  assert(groups.unknown.length === 2, 'and both unverifiable ones are filed under not-checked');
  assert(groups.unknown.some((a) => a.id === 'assertion_unmeasured'),
    'the one nobody measured is THERE, where a reader will look for it, rather than dropped');
  assert(d.unverifiedPreserved(hostile).length === 2,
    'and it is still reported as having CLAIMED preserved, so the correction is auditable rather than silent');
  assert(d.unverifiedPreserved(delta({})).length === 0, 'a delta with nothing to correct reports nothing');

  assert(d.unknownsAreNeverPreserved(hostile) === true,
    'the screen invariant holds even on hostile input: that is what it means for the screen to defend itself');
  assert(d.unknownsAreNeverPreserved(delta({})) === true, 'and on an empty delta');
  assert(d.unknownsAreNeverPreserved(null) === true, 'and on nothing at all');

  const c = d.counts(hostile);
  assert(c.preserved === 1 && c.unknowns === 2,
    'the counts a card prints agree with the buckets a reader opens');
  assert(c.preserved + c.unknowns === c.assertions, 'and no row is counted twice or lost between them');
}

// ── Every classification gets a heading, even an empty one ──
{
  const groups = d.groupByClassification([]);
  assert(Object.keys(groups).join() === d.CLASSIFICATION_ORDER.join(),
    'the groups come back in the order the questions are asked, always');
  assert(Object.keys(groups).indexOf('unknown') < Object.keys(groups).indexOf('preserved'),
    '`unknown` is drawn ABOVE the good news, not below it and not behind a `show more`');
  assert(Object.keys(groups)[0] === 'regression', 'and a regression is the first thing on the page');
  assert(Object.values(groups).every((rows) => rows.length === 0),
    'a delta with nothing in it says `0` under every heading rather than hiding the heading');
  assert(d.groupByClassification(undefined).unknown.length === 0, 'nothing to group is not a crash');
  assert(d.groupByClassification([null, d.assertionFrom(assertion({}))]).unknown.length === 1,
    'and an unreadable row is skipped, not fatal');
}

// ── Ordering: what a reader has to deal with comes first ──
//
// The same key `UniversalDelta.material_assertions` sorts by on the server, on
// purpose: every consumer reads a long delta down to its first rows, and the
// two sides must lose the SAME rows when they do.
{
  const rows = [
    d.assertionFrom(assertion({ id: 'a_info', path: 'z', severity: 'info', confidence: 'exact' })),
    d.assertionFrom(assertion({ id: 'a_blocking', path: 'm', severity: 'blocking', confidence: 'low' })),
    d.assertionFrom(assertion({ id: 'a_material_sure', path: 'b', severity: 'material', confidence: 'exact' })),
    d.assertionFrom(assertion({ id: 'a_material_unsure', path: 'a', severity: 'material', confidence: 'low' })),
  ];
  const ordered = d.orderedAssertions(rows);
  assert(ordered[0].id === 'a_blocking',
    'the blocking finding is first, however uncertain it is: a reader must have seen it');
  assert(ordered[1].id === 'a_material_sure' && ordered[2].id === 'a_material_unsure',
    'between two of equal severity the one we are certain about comes first');
  assert(ordered[3].id === 'a_info', 'and the cosmetic row is last');
  assert(d.orderedAssertions(rows).map((a) => a.id).join() === ordered.map((a) => a.id).join(),
    'the order is stable: sorting twice gives the same page');
  assert(rows[0].id === 'a_info', 'and the caller’s own array was not re-ordered underneath it');
  assert(d.orderedAssertions(undefined).length === 0, 'nothing to order is not a crash');

  const tie = d.orderedAssertions([
    d.assertionFrom(assertion({ id: 'b', path: 'zzz' })),
    d.assertionFrom(assertion({ id: 'a', path: 'aaa' })),
  ]);
  assert(tie[0].path === 'aaa', 'and rows alike in every other way fall back to their path, never to insertion order');
}

// ── Invariants: violated, then unknown, then preserved, then n/a ──
{
  const results = [
    d.invariantFrom(invariantResult({ invariant_id: 'i_na', status: 'not_applicable' })),
    d.invariantFrom(invariantResult({ invariant_id: 'i_held', status: 'preserved', confidence: 'high', observations: ['delta_e 0.4'] })),
    d.invariantFrom(invariantResult({ invariant_id: 'i_unchecked', status: 'unknown' })),
    d.invariantFrom(invariantResult({ invariant_id: 'i_broken', status: 'violated', severity: 'blocking' })),
  ];
  const ordered = d.orderedInvariants(results);
  assert(ordered.map((r) => r.invariantId).join() === 'i_broken,i_unchecked,i_held,i_na',
    'violated, then the ones nobody could check, then the ones that held, then the ones that do not apply');
  assert(d.invariantStatusRank('unknown') < d.invariantStatusRank('preserved'),
    'an `unknown` is never sorted in amongst the greens: that is the same lie as colouring it green');
  assert(d.invariantStatusRank('invented') === d.invariantStatusRank('unknown'),
    'and a status this build never heard of is filed as not-checked, never as held');
  assert(d.invariantTone('not_applicable') !== d.invariantTone('preserved'),
    '`does not apply` is not good news, it is no news, and the two get different tones');
  assert(d.invariantTone('unknown') === 'unknown' && d.invariantTone('violated') === 'bad',
    'not-checked is its own tone, distinct from a violation');
  assert(d.invariantStatusLabel('unknown') !== d.invariantStatusLabel('preserved'),
    'and the two are told apart by a word, not only by a colour');

  const withUnknownAfterPreserved = delta({ invariants: results.map((r) => ({
    invariant_id: r.invariantId, status: r.status, confidence: r.confidence,
    tier: r.tier, severity: r.severity, observations: r.observations,
  })) });
  assert(d.unknownsAreNeverPreserved(withUnknownAfterPreserved) === true,
    'the ordering keeps them apart, and the screen invariant says so');
  assert(d.orderedInvariants(undefined).length === 0, 'nothing to order is not a crash');
}

// ── Rule two, first half: an unmeasured axis is null, and null is not zero ──
//
// "We did not measure this" and "we measured it and covered none of it" lead
// to opposite next actions -- go and measure it, against go and look at what
// was missed. A default of zero turns every one of the first into one of the
// second, silently, for ever. This is the single check this file exists for.
{
  const coverage = d.coverageFrom({
    source_readable: true, target_readable: true,
    dimensions: { structural: 1, semantic: 0, spatial: 0.5 },
    excluded: ['the background'], notes: ['the alpha channel was ignored'],
  });
  const rows = d.coverageRows(coverage);
  const by = Object.fromEntries(rows.map((row) => [row.dimension, row]));

  assert(by.semantic.ratio === 0,
    'an axis measured at zero reports zero: we looked at it and covered none of it');
  assert(by.temporal.ratio === null,
    'an axis nobody measured reports NULL, and null is not zero: the screen has to be able to say "not measured"');
  assert(by.temporal.ratio !== 0 && by.semantic.ratio !== null,
    'and the two are never each other, which is the whole point of this file');
  assert(by.behavioral.ratio === null && by.identity.ratio === null,
    'every unmeasured axis, not only the first one');
  assert(by.structural.ratio === 1 && by.spatial.ratio === 0.5, 'the measured ones keep their number');

  assert(rows.length === d.COVERAGE_DIMENSIONS.length,
    'all six axes are returned, so a reader cannot mistake three rows for the three axes that exist');
  assert(rows.map((row) => row.dimension).join() === d.COVERAGE_DIMENSIONS.join(),
    'in the canonical order, so two deltas are read the same way round');
  assert(rows.every((row) => row.label && row.label !== row.dimension.toUpperCase()),
    'each with a word for a person, which `t()` translates');

  assert(d.coverageRatio(coverage, 'temporal') === null, 'the single-axis reader agrees');
  assert(d.coverageRatio(null, 'structural') === null, 'and nothing at all is not full coverage');
  assert(d.coverageRatio(coverage, 'invented') === null, 'nor is an axis nobody declared');

  const nonsense = d.coverageFrom({ dimensions: { structural: 1.4, semantic: 'lots', temporal: null } });
  assert(d.coverageRatio(nonsense, 'structural') === null,
    'a ratio outside 0..1 is an extractor’s arithmetic bug and reads as unmeasured, never as complete');
  assert(d.coverageRatio(nonsense, 'semantic') === null && d.coverageRatio(nonsense, 'temporal') === null,
    'and so does anything that is not a number');
  assert(d.coverageFrom(null).dimensions.structural === undefined, 'nothing at all is not a crash');
}

// ── What the coverage does not say, in sentences ──
{
  const unreadable = d.coverageFrom({ source_readable: false, target_readable: true, dimensions: { structural: 1 } });
  const gaps = d.describeCoverageGap(unreadable);
  assert(gaps.some((line) => line.toLowerCase().includes('source')),
    'an end that could not be read is said first: with one missing there was no comparison at all');
  assert(gaps.some((line) => line.toLowerCase().includes('not measured')),
    'and the axes nobody measured are named as not measured, never as zero');

  const nothing = d.describeCoverageGap(d.coverageFrom({ source_readable: true, target_readable: true }));
  assert(nothing.some((line) => line.toLowerCase().includes('no axis was measured')),
    'a coverage block with no axis at all says how much was compared is not known, rather than drawing six empty bars');

  const complete = d.describeCoverageGap(d.coverageFrom({
    source_readable: true, target_readable: true,
    dimensions: { structural: 1, semantic: 1, temporal: 1, identity: 1, spatial: 1, behavioral: 1 },
  }));
  assert(complete.length === 0, 'and a comparison with nothing missing claims nothing about what is missing');

  const excluded = d.describeCoverageGap(d.coverageFrom({
    source_readable: true, target_readable: true,
    dimensions: { structural: 1, semantic: 1, temporal: 1, identity: 1, spatial: 1, behavioral: 1 },
    excluded: ['the watermark'],
  }));
  assert(excluded.some((line) => line.toLowerCase().includes('excluded')),
    'an excluded region is a hole the reader is told about before treating the delta as complete');
  assert(d.describeCoverageGap(null).length >= 2, 'and nothing at all is two missing ends, not silence');
}

// ── Rule two, second half: coverage and confidence are never one number ──
//
// Full coverage from a perceptual metric at low confidence is a real and
// frequent situation: every region was looked at, by a method that cannot be
// certain about any of them. One percentage hides both halves, and the two
// call for different next actions.
{
  const everythingLookedAt = delta({
    coverage: {
      source_readable: true, target_readable: true,
      dimensions: { structural: 1, semantic: 1, temporal: 1, identity: 1, spatial: 1, behavioral: 1 },
    },
    assertions: [
      assertion({ id: 'a1', operation: 'modified', classification: 'incidental', confidence: 'low', tier: 'perceptual' }),
    ],
  });
  const summary = d.summarize(everythingLookedAt);
  const rows = d.coverageRows(everythingLookedAt.coverage);

  assert(rows.every((row) => row.ratio === 1), 'every axis says it was fully covered');
  assert(summary.confidence === 'low', 'and the confidence still says low');
  assert(typeof summary.confidence === 'string' && summary.counts.assertions === 1,
    'the confidence is a WORD off the ladder, not a number that could be multiplied by a ratio');
  assert(d.confidenceTone('low') === 'warn' && d.confidenceTone('unknown') === 'unknown',
    'a weak measurement and no measurement are different tones as well as different words');
  assert(Object.keys(summary).indexOf('confidence') >= 0 && Object.keys(summary.counts).indexOf('coverage') < 0,
    'nothing in a summary combines the two: there is no field for a coverage-times-confidence score');

  const sureAboutOneCorner = delta({
    coverage: { source_readable: true, target_readable: true, dimensions: { structural: 0.2 } },
    assertions: [assertion({ id: 'a1', operation: 'modified', classification: 'requested', confidence: 'exact', tier: 'hash' })],
  });
  assert(d.summarize(sureAboutOneCorner).confidence === 'exact',
    'the opposite situation -- a certain answer about one small region -- reports certainty');
  assert(d.coverageRatio(sureAboutOneCorner.coverage, 'structural') === 0.2,
    'while its coverage still says one fifth');
  assert(d.summarize(everythingLookedAt).confidence !== d.summarize(sureAboutOneCorner).confidence
    && d.coverageRatio(everythingLookedAt.coverage, 'structural') !== d.coverageRatio(sureAboutOneCorner.coverage, 'structural'),
    'the two situations are opposite on both axes, and the screen can show that because it never folded them together');
}

// ── Rule three: a blocking finding is never summarised ──
{
  const blocking = delta({
    assertions: [
      assertion({
        id: 'a_blocking', path: 'nodes.publish.permissions.network', operation: 'modified',
        classification: 'regression', severity: 'blocking', confidence: 'exact', tier: 'parser',
        before: 'deny', after: 'allow', method: 'workflow permission diff',
        invariant_refs: ['inv_network'], evidence_refs: [{ kind: 'json_pointer', ref: '/nodes/publish/permissions' }],
      }),
      assertion({ id: 'a_cosmetic', path: 'meta.title', operation: 'modified', classification: 'incidental', severity: 'info' }),
    ],
    invariants: [
      invariantResult({ invariant_id: 'inv_network', status: 'violated', severity: 'blocking', method: 'permission set compare', confidence: 'exact', tier: 'parser' }),
      invariantResult({ invariant_id: 'inv_face', status: 'unknown' }),
    ],
  });

  const found = d.blockingFindings(blocking);
  assert(found.assertions.length === 1 && found.assertions[0].id === 'a_blocking',
    'the blocking row is returned, and the cosmetic one is not');
  assert(found.assertions[0].method === 'workflow permission diff' && found.assertions[0].tier === 'parser',
    'WHOLE: with the method and the tier that produced it, not as a count');
  assert(found.assertions[0].before === 'deny' && found.assertions[0].after === 'allow',
    'with its before and its after, which is the sentence a reader actually needs');
  assert(found.assertions[0].evidenceRefs.length === 1,
    'and with its evidence, so the claim can be checked rather than believed');
  assert(found.invariants.length === 1 && found.invariants[0].invariantId === 'inv_network',
    'the invariant it broke comes with it');
  assert(found.invariants[0].method === 'permission set compare',
    'also whole, with the method that decided it');

  const attached = d.invariantsFor(blocking, found.assertions[0]);
  assert(attached.length === 1 && attached[0].invariantId === 'inv_network',
    'the row names its invariant and the screen can resolve it without guessing');
  assert(d.invariantsFor(blocking, blocking.assertions[1]).length === 0,
    'a row that names none resolves to none rather than to all of them');

  const c = d.counts(blocking);
  assert(c.blocking === 1 && c.material === 1 && c.regressions === 1,
    'the counts exist for the card, and the card is not where a blocking row is allowed to end');
  assert(d.orderedAssertions(blocking.assertions)[0].id === 'a_blocking',
    'and wherever the rows are drawn in full, the blocking one is at the top');
  assert(d.blockingFindings(null).assertions.length === 0, 'nothing at all is not a crash');
}

// ── The verdict about the change ──
{
  assert(d.assessmentTone('matched') === 'good', 'matched is good news');
  assert(d.assessmentTone('regressed') === 'bad' && d.assessmentTone('mismatched') === 'bad', 'and both failures are bad news');
  assert(d.assessmentTone('partial') === 'warn', 'partly matched is a caveat');
  assert(d.assessmentTone('inconclusive') === 'unknown',
    'inconclusive is NOT a caveat: an amber badge would read as "it mostly worked" and it means "we could not tell you"');
  assert(d.assessmentTone('partial') !== d.assessmentTone('inconclusive'),
    'so the two never share a colour');
  assert(d.assessmentTone('') === 'unknown' && d.assessmentTone('invented') === 'unknown',
    'a verdict this build never heard of is not read as a pass');
  assert(d.assessmentLabel('inconclusive') !== d.assessmentLabel('partial'),
    'and each verdict says its own sentence, so a reader without colour is still told');
  assert(d.severityTone('material') === 'bad' && d.severityTone('info') === 'good',
    'a severity that stops a caller and one that does not are drawn apart');
}

// ── Both ends of the comparison, named and pinned ──
//
// A label is what a person recognises; a hash is what makes the comparison
// mean anything. Two runs against `main` are two different comparisons and a
// card showing only the word `main` would present them as one.
{
  const before = d.revisionFrom(revision({ label: 'main', hash: sha('a') }));
  const after = d.revisionFrom(revision({ label: 'main', hash: sha('b') }));
  assert(d.revisionLabel(before) !== d.revisionLabel(after),
    'two revisions with the same label are told apart, which is the entire reason the hash is on the card');
  assert(d.shortHash(sha('a')) === 'aaaaaaa' && d.shortHash(sha('a')).length === 7,
    'seven characters of the digest, as the card asks for');
  assert(d.revisionLabel(before).indexOf('main') === 0 && d.revisionLabel(before).indexOf('aaaaaaa') > 0,
    'the label reads first and the digest follows it');

  const unnamed = d.revisionFrom({ kind: 'artifact', ref: 'artifact:art_9f2c', hash: sha('c') });
  assert(d.revisionLabel(unnamed).indexOf('artifact:art_9f2c') === 0,
    'a revision nobody labelled falls back to how its owning system names it');
  const bare = d.revisionFrom({ kind: '', ref: '', hash: sha('d') });
  assert(d.revisionLabel(bare) === 'ddddddd', 'and one that is only a hash is still drawn, under it');
  assert(d.shortHash('') === '' && d.revisionLabel(null) === '',
    'nothing at all draws nothing, rather than an invented identity');
  assert(d.revisionLabel(d.revisionFrom({ label: 'only a name', hash: '' })) === 'only a name',
    'a revision with no hash is not given one');
}

// ── A whole delta read down to a card ──
{
  const full = delta({
    id: 'delta_9f2c', project_id: 'proj_1', intent_contract_id: 'intent_1', created_at: '2026-09-06T10:00:00Z',
    assertions: [
      assertion({ id: 'a1', operation: 'modified', classification: 'requested', severity: 'info', confidence: 'exact', tier: 'parser' }),
      assertion({ id: 'a2', operation: 'modified', classification: 'regression', severity: 'material', confidence: 'low', tier: 'perceptual' }),
      assertion({ id: 'a3', operation: 'unchanged', classification: 'unknown', severity: 'info', confidence: 'unknown' }),
      assertion({ id: 'a4', operation: 'unchanged', classification: 'preserved', severity: 'info', confidence: 'high', tier: 'hash' }),
    ],
    invariants: [
      invariantResult({ invariant_id: 'i1', status: 'violated' }),
      invariantResult({ invariant_id: 'i2', status: 'unknown' }),
    ],
    evidence_refs: [{ kind: 'overlay', ref: 'artifact:art_overlay' }],
    limitations: ['the audio track was not compared'],
  });
  const s = d.summarize(full);

  assert(s.counts.assertions === 4 && s.counts.regressions === 1 && s.counts.material === 1,
    'the card counts what a card counts');
  assert(s.counts.unknowns === 1,
    'and it counts the UNKNOWNS explicitly: a card that reported only assertions and regressions would let a reader infer everything else was checked and fine');
  assert(s.counts.invariantsViolated === 1, 'with the invariants that broke beside them');
  assert(s.severity === 'material', 'the severity is the strongest finding in the delta, not the average');
  assert(s.confidence === 'low',
    'and the confidence is the weakest of the ones that CHANGED something: a delta is as believable as its weakest measurement');
  assert(s.sourceLabel !== s.targetLabel, 'both ends are named');
  assert(s.id === 'delta_9f2c' && s.domain === 'image' && s.projectId === 'proj_1' && s.intentContractId === 'intent_1',
    'and the card carries what it takes to open the detail and to filter the list');

  // The card's confidence answers "how much to believe what CHANGED", so a
  // delta in which nothing changed has no change to believe and reports
  // `unknown`. That is `UniversalDelta.summary()`'s own arithmetic
  // (`weakest_confidence([a.confidence for a in assertions if a.changed] or
  // ["unknown"])`), mirrored rather than improved on: a client that
  // under-claimed differently from its server would give two readers of one
  // delta two different cards, and the row itself still says `exact` beside
  // its `preserved`, where the certainty actually belongs.
  const still = delta({
    assertions: [assertion({ id: 'a1', operation: 'unchanged', classification: 'preserved', confidence: 'exact', tier: 'hash' })],
  });
  assert(d.summarize(still).confidence === 'unknown',
    'a delta in which nothing changed has no changed row to be confident about, exactly as the server computes it');
  assert(d.counts(still).preserved === 1 && still.assertions[0].confidence === 'exact',
    'while the earned `preserved` is still counted as earned and the row still says `exact`, so the certainty is not lost, only kept where it applies');
  assert(d.summarize(delta({})).confidence === 'unknown',
    'and a delta with no findings at all reports `unknown`, never `exact`: nothing observed is not a perfect match');

  const c = d.counts(full);
  assert(c.changed === 2, 'the changed rows are counted apart from the unchanged ones');
  assert(c.evidence === 1 && c.limitations === 1, 'and so are the evidence and the limitations');
  assert(c.invariantsUnknown === 1 && c.invariantsPreserved === 0,
    'an invariant nobody could check is never counted as one that held');
  assert(d.counts(null).assertions === 0, 'nothing at all counts nothing, rather than throwing');
}

// ── A refusal is a token, not a sentence ──
//
// A rejected request answers 200 with `ok:false`. A caller that only checked
// the HTTP status would draw an empty list and call it "no comparisons".
{
  assert(d.refusalOf({ ok: true, deltas: [] }) === null, 'a success is not a refusal');
  assert(d.refusalOf(null) === null && d.refusalOf('nope') === null, 'nothing is not a refusal either');

  const off = d.refusalOf({
    ok: false, enabled: false,
    error: { path: 'domain', code: 'delta_engine_disabled', message: 'new comparisons are switched off' },
  });
  assert(off.code === 'delta_engine_disabled', 'the token survives, and it is what a caller branches on');
  assert(off.path === 'domain' && off.message.includes('switched off'), 'and so do the field and the sentence');
  assert(off.enabled === false, 'and the flag, so the button can say why it is off');

  const bare = d.refusalOf({ ok: false });
  assert(bare.code === 'refused' && bare.message.length > 0, 'a refusal with no token is still a refusal');

  const raised = new d.DeltaRefusal(off);
  assert(raised instanceof Error && raised.code === 'delta_engine_disabled', 'DeltaRefusal is a real Error carrying the token');
  assert(raised.message === off.message, 'and its message is the server’s own sentence');
}

// ── A listing row is what the server computed, read as sent ──
{
  const row = d.summaryFrom({
    id: 'delta_1', domain: 'code', assessment: 'regressed', created_at: '2026-09-06T09:00:00Z',
    project_id: 'proj_1', intent_contract_id: 'intent_1',
    source_label: 'before · aaaaaaa', target_label: 'after · bbbbbbb',
    counts: { assertions: 12, material: 3, regressions: 1, unknowns: 4, invariants_violated: 1 },
    severity: 'blocking', confidence: 'medium',
  });
  assert(row.counts.unknowns === 4 && row.counts.invariantsViolated === 1,
    'the snake_case the server speaks is read into the shape the screen speaks');
  assert(row.severity === 'blocking' && row.confidence === 'medium',
    'and the two axes arrive as two fields, exactly as they left');
  assert(d.summaryFrom({}).assessment === 'inconclusive',
    'a row with no verdict is inconclusive, never matched');
  assert(d.summaryFrom({}).confidence === 'unknown', 'and a row with no confidence is not a certain one');
}

// ── What this build can extract, and why it cannot ──
{
  const rows = d.extractorsFrom({
    extractors: {
      image: { available: true, version: '1.2.0', module: 'src.delta_engine.adapters.image', reason: '' },
      video: { available: false, version: '', module: 'src.delta_engine.adapters.video', reason: 'ImportError: no module named av' },
    },
  });
  const by = Object.fromEntries(rows.map((row) => [row.domain, row]));
  assert(by.image.available === true && by.image.version === '1.2.0', 'an extractor that answers reports its version');
  assert(by.video.available === false && by.video.reason.includes('ImportError'),
    'and one that cannot even import says WHY, which is the only reason a status panel is worth drawing');
  assert(rows.map((row) => row.domain).join() === 'image,video', 'the domains come back in a stable order');
  assert(d.extractorsFrom(null).length === 0, 'nothing at all is not a crash');

  const diag = d.diagnosticsFrom({ enabled: false, counts: { deltas: 9, intents: 4 }, db_path: 'data/deltas.db', streams: ['delta_events'] });
  assert(diag.enabled === false && diag.counts.deltas === 9, 'the diagnostics carry the flag and the counts');
  assert(diag.dbPath === 'data/deltas.db' && diag.streams.join() === 'delta_events', 'and where the rows live');

  const config = d.configFrom({ enabled: true, domains: ['image'], severities: [] });
  assert(config.domains.join() === 'image', 'the server’s own vocabulary wins, so a build that knows a new domain offers it');
  assert(config.severities.join() === d.SEVERITIES.join(),
    'and a list the server did not send falls back to this build’s, rather than to an empty filter that hides everything');
}

// ── Reconnection, and evidence that belongs to one row ──
{
  const events = [
    d.eventFrom({ name: 'delta_created', delta_id: 'delta_1', cursor: 'c1' }),
    d.eventFrom({ name: 'delta_completed', delta_id: 'delta_1', cursor: 'c2' }),
  ];
  assert(d.advanceCursor('c0', events) === 'c2', 'the cursor lands on the last frame that carried one');
  assert(d.advanceCursor('c2', []) === 'c2', 'an empty answer leaves it where it was');
  assert(d.advanceCursor('c2', [d.eventFrom({ name: 'ping' })]) === 'c2',
    'and a frame with no cursor never blanks it: an empty cursor would replay the stream from the beginning');
  assert(d.eventFrom({ name: 'delta_completed', payload: { delta_id: 'delta_2' } }).deltaId === 'delta_2',
    'the id is read from the payload when the frame did not repeat it at the top');

  const row = d.assertionFrom(assertion({ id: 'a1', evidence_refs: [{ kind: 'region', ref: 'artifact:art_1' }] }));
  const attached = d.evidenceFor(row, [
    d.evidenceFrom({ kind: 'overlay', ref: 'artifact:art_2', assertion_id: 'a1' }),
    d.evidenceFrom({ kind: 'overlay', ref: 'artifact:art_3', assertion_id: 'a2' }),
    d.evidenceFrom({ kind: 'region', ref: 'artifact:art_1', assertion_id: 'a1' }),
  ]);
  assert(attached.length === 2, 'a row gets its own evidence plus what /evidence attributed to it, deduplicated');
  assert(attached.every((ref) => ref.ref !== 'artifact:art_3'),
    'and never another row’s, which would attach a proof to a claim it was not about');
  assert(d.evidenceFor(row).length === 1, 'with no second source it is simply what the row carried');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nALL OK');
