import { CircleAlert, CircleCheck, CircleHelp, CircleMinus, CircleX, Scale, type LucideIcon } from 'lucide-react';
import { useId, useState } from 'react';
import { locale, t } from '../../i18n';
import {
  calibrationReading,
  REVIEW_SCORE_KEYS,
  reviewTone,
  sourceCounts,
  sourceStanding,
  STANDING_ORDER,
  summarise,
  verdictIsSummaryOnly,
  type BlindReview,
  type CheckVerdict,
  type ReviewScoreKey,
  type SourceStanding,
  type VerdictSource,
} from '../../lib/researchVerdicts';

/**
 * OBJ-25: what the report's own checks found, where the reader reads it.
 * The citation check per source (a chip whose reason opens on demand), the
 * counts at the top of the report, and §144's blind review. Every state is
 * a word and an icon as well as a colour, and "not checked" is never drawn
 * like "verified" (see studio/src/lib/researchVerdicts.ts for the rules).
 */

const STANDING: Record<SourceStanding, { icon: LucideIcon; word: string; count: string }> = {
  verified: { icon: CircleCheck, word: 'verified#source', count: 'Verified: {n}' },
  partial: { icon: CircleAlert, word: 'partially verified', count: 'Partially: {n}' },
  not_supported: { icon: CircleX, word: 'not supported', count: 'Not supported: {n}' },
  unverifiable: { icon: CircleHelp, word: 'could not be checked', count: 'Could not be checked: {n}' },
  unchecked: { icon: CircleMinus, word: 'not checked', count: 'Not checked: {n}' },
};

const CHECK_WORD: Record<CheckVerdict, string> = {
  supported: 'matches the source',
  not_supported: 'not in the source',
  unverifiable: 'could not be checked',
};

function standingReason(standing: SourceStanding): string {
  switch (standing) {
    case 'verified':
      return t('Every sentence citing this source matched the text stored for it.');
    case 'partial':
      return t('Some sentences citing this source matched it; the others did not, or could not be checked.');
    case 'not_supported':
      return t('A sentence citing this source carries a figure or code that the text stored for it does not contain.');
    case 'unverifiable':
      return t('The sentences citing this source had nothing the checker can compare, such as figures or code, so they were neither confirmed nor refuted.');
    default:
      return t('No sentence citing this source was checked. It may not be cited in the report at all.');
  }
}

/** The chip beside one source, and its reason when asked for. */
export function SourceVerdict({ source }: { source: VerdictSource }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const standing = sourceStanding(source);
  const { icon: Icon, word } = STANDING[standing];
  const counts = sourceCounts(source);
  const checks = source.citationChecks ?? [];
  return (
    <>
      <button
        type="button"
        className="fs-rs__verdict"
        data-standing={standing}
        data-testid="research-citation-verdict"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
        title={t('Why: from this run\'s own citation check')}
      >
        <Icon size={12} aria-hidden="true" />
        <span>{t(word)}</span>
      </button>
      {open && (
        <div id={panelId} className="fs-rs__verdict-why" data-testid="research-citation-why">
          <p>{standingReason(standing)}</p>
          {verdictIsSummaryOnly(source) && (
            <p className="fs-muted">{t('This report was saved before per-sentence detail was kept: the word above is the best result among the sentences citing it, so one that failed would not show.')}</p>
          )}
          {counts && (
            <p className="fs-muted">
              {t('Citing sentences: {ok} matched, {bad} not in the source, {unk} could not be checked.', { ok: counts.supported, bad: counts.not_supported, unk: counts.unverifiable })}
            </p>
          )}
          {checks.length > 0 && (
            <ul className="fs-rs__checks">
              {checks.map((c, i) => (
                <li key={i} data-verdict={c.verdict}>
                  <span className="fs-rs__check-word" data-verdict={c.verdict}>{t(CHECK_WORD[c.verdict])}</span>
                  {c.sentence && <q className="fs-rs__check-sentence">{c.sentence}</q>}
                  {c.why && <span className="fs-rs__check-note">{t('Checker\'s note: {note}', { note: c.why })}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

function reviewLine(review: BlindReview | null): string {
  if (!review) return t('not run for this report');
  if (review.state === 'failed') return t('failed');
  const n = review.overall ?? 0;
  const tone = reviewTone(review.overall);
  if (tone === 'good') return t('{n} of 5 · good', { n });
  if (tone === 'mixed') return t('{n} of 5 · mixed', { n });
  return t('{n} of 5 · poor', { n });
}

/** The counts at the top of the report: citations by standing and the review's grade. */
export function VerificationSummary({ sources, review }: { sources: VerdictSource[]; review: BlindReview | null }) {
  const sum = summarise(sources);
  return (
    <section className="fs-rs__verify" aria-label={t('What the checks found')} data-testid="research-verification">
      <p className="fs-rs__verify-row">
        <span className="fs-rs__verify-label">{t('Sources')}</span>
        {sum.checked ? (
          STANDING_ORDER.filter((s) => sum.sources[s] > 0).map((s) => {
            const Icon = STANDING[s].icon;
            return (
              <span key={s} className="fs-rs__verify-count" data-standing={s} data-testid={`research-count-${s}`}>
                <Icon size={12} aria-hidden="true" />
                {t(STANDING[s].count, { n: sum.sources[s] })}
              </span>
            );
          })
        ) : (
          <span className="fs-rs__verify-count" data-standing="unchecked" data-testid="research-count-none">
            <CircleMinus size={12} aria-hidden="true" />
            {t('not checked for this report')}
          </span>
        )}
      </p>
      {sum.sentences && (
        <p className="fs-rs__verify-note">
          {t('Per citing sentence: {ok} matched, {bad} not in the source, {unk} could not be checked.', { ok: sum.sentences.supported, bad: sum.sentences.not_supported, unk: sum.sentences.unverifiable })}
        </p>
      )}
      <p className="fs-rs__verify-row">
        <span className="fs-rs__verify-label">{t('Blind review')}</span>
        <span className="fs-rs__verify-count" data-review={review ? (review.state === 'failed' ? 'failed' : reviewTone(review.overall)) : 'none'} data-testid="research-review-grade">
          {reviewLine(review)}
        </span>
      </p>
    </section>
  );
}

const SCORE_LABEL: Record<ReviewScoreKey, string> = {
  answers_question: 'Answers the question',
  evidence_support: 'Claims backed by its sources',
  source_quality: 'Source quality',
  internal_consistency: 'Internal consistency',
  completeness: 'Completeness',
};

function calibrationText(gap: number | null): string {
  const reading = calibrationReading(gap);
  const size = gap === null ? '' : Math.abs(gap).toLocaleString(locale(), { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (reading === 'overconfident') return t('The writer rated its own evidence higher than the reviewer did (gap {gap} on a 0-1 scale).', { gap: size });
  if (reading === 'underconfident') return t('The writer rated its own evidence lower than the reviewer did (gap {gap} on a 0-1 scale).', { gap: size });
  if (reading === 'agrees') return t('The writer\'s own rating of its evidence agrees with the reviewer\'s.');
  return '';
}

/** §144: a second model graded the report without seeing the plan, the trace or who wrote it. */
export function BlindReviewPanel({ review }: { review: BlindReview | null }) {
  const headingId = useId();
  if (!review) return null;
  return (
    <section className="fs-rs__review" aria-labelledby={headingId} data-state={review.state} data-testid="research-blind-review">
      <header className="fs-rs__review-head">
        <Scale size={14} aria-hidden="true" />
        <h4 id={headingId}>{t('Blind review')}</h4>
        <span className="fs-rs__verify-count" data-review={review.state === 'failed' ? 'failed' : reviewTone(review.overall)}>{reviewLine(review)}</span>
      </header>
      {review.state === 'done' ? (
        <p className="fs-muted">
          {review.model
            ? t('Reviewed by {model}, which saw only the question, the report and its sources — not the plan, the searches or which model wrote it.', { model: review.model })
            : t('A second model saw only the question, the report and its sources — not the plan, the searches or which model wrote it.')}
        </p>
      ) : (
        review.model && <p className="fs-muted">{t('Reviewer: {model}', { model: review.model })}</p>
      )}
      {review.state === 'failed' && (
        <p className="fs-notice" data-tone="warning" data-testid="research-review-error">
          {review.error ? t('The review failed: {reason}', { reason: review.error }) : t('The review returned no grade.')}
        </p>
      )}
      {Object.keys(review.scores).length > 0 && (
        <dl className="fs-rs__review-scores">
          {REVIEW_SCORE_KEYS.filter((k) => review.scores[k] !== undefined).map((k) => (
            <div key={k} className="fs-rs__stat">
              <dt>{t(SCORE_LABEL[k])}</dt>
              <dd>{t('{n} of 5', { n: review.scores[k] as number })}</dd>
            </div>
          ))}
        </dl>
      )}
      {calibrationText(review.calibrationGap) && <p className="fs-rs__verify-note">{calibrationText(review.calibrationGap)}</p>}
      {review.findings.length > 0 ? (
        <ul className="fs-rs__findings" aria-label={t('Findings')}>
          {review.findings.map((f, i) => (
            <li key={i} data-severity={f.severity}>
              <span className="fs-rs__severity" data-severity={f.severity}>
                {f.severity === 'high' ? t('High') : t('Medium')}
              </span>
              <span className="fs-rs__finding-kind">{f.kind === 'unsupported_claim' ? t('Unsupported claim') : t('Weakness')}</span>
              <span className="fs-rs__finding-text">{f.text}</span>
            </li>
          ))}
        </ul>
      ) : (
        review.state === 'done' && <p className="fs-rs__verify-note">{t('The reviewer listed no weaknesses or unsupported claims.')}</p>
      )}
      {review.findings.length > 0 && <p className="fs-rs__verify-note">{t('Severity follows the kind of finding: a claim its own sources do not back is high, a weakness is medium.')}</p>}
    </section>
  );
}
