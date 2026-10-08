// OBJ-25: a report's own checks, read honestly.
//
// studio/src/lib/researchVerdicts.ts turns the citation check and §144's
// blind review into what the Research view shows. The rules that matter:
// "not checked" is never "could not be checked" and neither is verified; a
// source that backs one sentence and contradicts another is partial, never
// verified; a failed or missing review is never a grade. The adapter has to
// carry the new fields, and the view has to show a word and an icon, not
// only a colour, with the reason behind a real button.
// Run by tests/test_studio_research_verification_js.py, or by hand:
//   node studio/checks/research-verification.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-research-verification-'));
const out = join(dir, 'verdicts.mjs');
await build({ entryPoints: [join(root, 'studio/src/lib/researchVerdicts.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const v = await import(pathToFileURL(out).href);

// 1. one source's standing
const counts = (supported, not_supported, unverifiable) => ({ supported, not_supported, unverifiable });
assert.equal(v.sourceStanding({ citationVerdict: 'supported', citationCounts: counts(3, 0, 0) }), 'verified');
// the server's one word says "supported"; the counts say one sentence failed
assert.equal(v.sourceStanding({ citationVerdict: 'supported', citationCounts: counts(2, 1, 0) }), 'partial');
assert.equal(v.sourceStanding({ citationVerdict: 'supported', citationCounts: counts(1, 0, 2) }), 'partial');
assert.equal(v.sourceStanding({ citationVerdict: 'not_supported', citationCounts: counts(0, 1, 1) }), 'not_supported');
assert.equal(v.sourceStanding({ citationVerdict: 'unverifiable', citationCounts: counts(0, 0, 2) }), 'unverifiable');
assert.equal(v.sourceStanding({}), 'unchecked', 'no verdict at all is "not checked", not "could not be checked"');
// counts derived from the listed checks when the server sent no counts
assert.equal(v.sourceStanding({ citationChecks: [{ verdict: 'supported' }, { verdict: 'not_supported' }] }), 'partial');
// an older report: only the word, flagged as such
assert.equal(v.sourceStanding({ citationVerdict: 'supported' }), 'verified');
assert.equal(v.verdictIsSummaryOnly({ citationVerdict: 'supported' }), true);
assert.equal(v.verdictIsSummaryOnly({ citationVerdict: 'supported', citationCounts: counts(1, 0, 0) }), false);

// 2. parsing off the wire is strict
assert.equal(v.countsFrom({ supported: 0, not_supported: 0, unverifiable: 0 }), undefined, 'all-zero counts are no counts');
assert.deepEqual(v.countsFrom({ supported: 2, not_supported: '1', unverifiable: -3 }), counts(2, 0, 0));
assert.deepEqual(v.checksFrom([{ verdict: 'maybe', sentence: 'x' }, null, { verdict: 'not_supported', sentence: 'It is 48 %.', why: 'no 48', layer: 4 }]),
  [{ verdict: 'not_supported', sentence: 'It is 48 %.', why: 'no 48', layer: 4 }]);

// 3. the report summary
const sum = v.summarise([
  { citationVerdict: 'supported', citationCounts: counts(2, 0, 0) },
  { citationVerdict: 'supported', citationCounts: counts(1, 1, 0) },
  { citationVerdict: 'not_supported', citationCounts: counts(0, 2, 0) },
  { citationVerdict: 'unverifiable', citationCounts: counts(0, 0, 1) },
  {},
]);
assert.equal(sum.checked, true);
assert.deepEqual(sum.sources, { verified: 1, partial: 1, not_supported: 1, unverifiable: 1, unchecked: 1 });
assert.deepEqual(sum.sentences, counts(3, 3, 1));
const never = v.summarise([{}, {}]);
assert.equal(never.checked, false, 'a report the check never touched says so');
assert.equal(never.sentences, null);

// 4. blind review
assert.equal(v.blindReviewFrom(undefined), null, 'no review is "not run", never a grade');
assert.equal(v.blindReviewFrom(null), null);
const ok = v.blindReviewFrom({ model: 'reviewer-3b', overall: 2, scores: { answers_question: 3, completeness: 9 },
  weaknesses: ['thin evidence', ''], unsupported_claims: ['48 % still hurt'], calibration_gap: 0.42, duration_s: 7.1 });
assert.equal(ok.state, 'done');
assert.equal(ok.overall, 2);
assert.deepEqual(ok.scores, { answers_question: 3 }, 'an out-of-range score is dropped, not clamped into a grade');
assert.deepEqual(ok.findings.map((f) => [f.kind, f.severity]), [['unsupported_claim', 'high'], ['weakness', 'medium']],
  'unsupported claims first and graver than weaknesses');
assert.equal(v.reviewTone(ok.overall), 'poor');
assert.equal(v.calibrationReading(ok.calibrationGap), 'overconfident');
assert.equal(v.calibrationReading(-0.3), 'underconfident');
assert.equal(v.calibrationReading(0.1), 'agrees');
assert.equal(v.calibrationReading(null), 'unknown');
const failed = v.blindReviewFrom({ model: 'm', overall: null, scores: null, weaknesses: [], unsupported_claims: [], error: 'blind review timed out after 135s' });
assert.equal(failed.state, 'failed');
assert.equal(failed.error, 'blind review timed out after 135s');
const noGrade = v.blindReviewFrom({ model: 'm', weaknesses: ['x'] });
assert.equal(noGrade.state, 'failed', 'a review without a grade is not a review');
assert.equal(noGrade.partial, true, 'findings of a failed review are partial');
assert.equal(failed.partial, false);
// A failed review never shows a grade, a score or a calibration, even when the dict carries them.
const errWithScores = v.blindReviewFrom({ error: 'provider failed', overall: 4, scores: { answers_question: 5, evidence_support: 4 }, calibration_gap: 2 });
assert.equal(errWithScores.state, 'failed');
assert.equal(errWithScores.overall, null);
assert.deepEqual(errWithScores.scores, {});
assert.equal(errWithScores.calibrationGap, null);
assert.equal(errWithScores.raw.overall, 4, 'the original dict is kept for tracing');
const scoresNoOverall = v.blindReviewFrom({ scores: { answers_question: 5, evidence_support: 4 }, calibration_gap: 0.3, weaknesses: ['half'] });
assert.equal(scoresNoOverall.state, 'failed');
assert.deepEqual(scoresNoOverall.scores, {});
assert.equal(scoresNoOverall.calibrationGap, null);
assert.equal(scoresNoOverall.partial, true);
assert.equal(v.reviewTone(4), 'good');
assert.equal(v.reviewTone(3), 'mixed');

// 5. the adapter carries the new fields; the view keeps its promises
const adapter = readFileSync(join(root, 'studio/src/adapters/research.ts'), 'utf8');
assert.match(adapter, /citationCounts: countsFrom\(s\.citation_check_counts\)/);
assert.match(adapter, /citationChecks: checks\.length \? checks : undefined/);
assert.equal((adapter.match(/blindReview: blindReviewFrom\(raw\.blind_review\)/g) || []).length, 2, 'detail and result both read the review');
const view = readFileSync(join(root, 'studio/src/screens/research/Verification.tsx'), 'utf8');
assert.match(view, /<button\s+type="button"\s+className="fs-rs__verdict"/, 'the reason opens from a real button');
assert.match(view, /aria-expanded=\{open\}/);
assert.match(view, /aria-controls=\{open \? panelId : undefined\}/, 'aria-controls only names a panel that exists');
assert.match(view, /review\.state === 'done' && Object\.keys\(review\.scores\)/, 'scores only for a finished review');
assert.match(view, /review\.state === 'done' && calibrationText/, 'calibration only for a finished review');
assert.match(view, /review\.partial &&/, 'partial findings are labelled as such');
for (const s of ['verified', 'partial', 'not_supported', 'unverifiable', 'unchecked']) {
  assert.match(view, new RegExp(`${s}: \\{ icon: \\w+, word: '`), `${s} has an icon and a word`);
}
const css = readFileSync(join(root, 'studio/src/screens/research.css'), 'utf8');
assert.match(css, /\.fs-rs__verdict\[data-standing='unchecked'\][^{]*\{[^}]*border-style: dashed/, 'not checked is drawn apart');
assert.doesNotMatch(css, /\[data-standing='unchecked'\][^{]*\{[^}]*--fs-success/, 'not checked is never green');
const screen = readFileSync(join(root, 'studio/src/screens/research/Research.tsx'), 'utf8');
assert.equal((screen.match(/<VerificationSummary /g) || []).length, 2, 'the summary heads both report views');
assert.equal((screen.match(/<BlindReviewPanel /g) || []).length, 2);

console.log('ok research-verification');
