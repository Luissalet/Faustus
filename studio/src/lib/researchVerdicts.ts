/**
 * OBJ-25: what a Deep Research report's own checks say about it, reduced to
 * words a reader can act on. Two independent checks travel with a finished
 * report (`src/research_handler.py`):
 *
 *  - the citation check (`src/research_citations.py::check_claims`): every
 *    sentence that cites a source is checked against the text stored for
 *    that source. Per sentence it is `supported`, `not_supported` (a figure
 *    or code literal the source demonstrably lacks) or `unverifiable`
 *    (nothing in the sentence survives paraphrase, or nothing stored for the
 *    page). Per source the server keeps one word plus, since OBJ-25, the
 *    counts and the sentences with the checker's reason.
 *  - the blind review (§144, `src/research_review.py`): a second model that
 *    grades the report without seeing how it was made.
 *
 * Honesty rules this module enforces, so no view has to remember them:
 *  - "not checked" (no verdict at all: the pass never looked at this source)
 *    is never shown as "could not be checked" (it looked and found nothing
 *    checkable), and neither is ever shown as verified;
 *  - a source that backs one sentence and contradicts another is "partial",
 *    never "verified" — the one-word server verdict lets `supported` win;
 *  - a blind review that failed is a failure with its reason, and a missing
 *    one is "not run"; neither is a passing grade.
 *
 * Pure: no React, no i18n, no fetch. Driven by
 * studio/checks/research-verification.check.mjs.
 */

export type CheckVerdict = 'supported' | 'not_supported' | 'unverifiable';

/** One sentence that cites a source and what the checker made of it. */
export interface CitationCheck {
  verdict: CheckVerdict;
  sentence: string;
  /** The checker's own note (English, from `src/claim_verify.py`). */
  why: string;
  layer: number | null;
}

export interface CheckCounts {
  supported: number;
  not_supported: number;
  unverifiable: number;
}

/** The fields of a source this module reads (a subset of ResearchSource). */
export interface VerdictSource {
  citationVerdict?: CheckVerdict;
  citationCounts?: CheckCounts;
  citationChecks?: CitationCheck[];
}

/** What one source's citations amount to, for its chip. */
export type SourceStanding = 'verified' | 'partial' | 'not_supported' | 'unverifiable' | 'unchecked';

export const STANDING_ORDER: SourceStanding[] = ['verified', 'partial', 'not_supported', 'unverifiable', 'unchecked'];

const VERDICTS: CheckVerdict[] = ['supported', 'not_supported', 'unverifiable'];

export function isCheckVerdict(v: unknown): v is CheckVerdict {
  return typeof v === 'string' && (VERDICTS as string[]).includes(v);
}

function count(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
}

/** The counts off the wire; undefined unless at least one sentence is counted. */
export function countsFrom(raw: unknown): CheckCounts | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const r = raw as Record<string, unknown>;
  const c = { supported: count(r.supported), not_supported: count(r.not_supported), unverifiable: count(r.unverifiable) };
  return c.supported + c.not_supported + c.unverifiable > 0 ? c : undefined;
}

export function checksFrom(raw: unknown): CitationCheck[] {
  if (!Array.isArray(raw)) return [];
  const out: CitationCheck[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const r = item as Record<string, unknown>;
    if (!isCheckVerdict(r.verdict)) continue;
    out.push({
      verdict: r.verdict,
      sentence: typeof r.sentence === 'string' ? r.sentence : '',
      why: typeof r.why === 'string' ? r.why : '',
      layer: typeof r.layer === 'number' ? r.layer : null,
    });
  }
  return out;
}

/** Counts from the server when it sent them; otherwise from the listed checks. */
export function sourceCounts(s: VerdictSource): CheckCounts | undefined {
  if (s.citationCounts) return s.citationCounts;
  const checks = s.citationChecks ?? [];
  if (checks.length === 0) return undefined;
  const c: CheckCounts = { supported: 0, not_supported: 0, unverifiable: 0 };
  for (const check of checks) c[check.verdict]++;
  return c;
}

/**
 * One source's standing. With per-sentence counts (reports saved since
 * OBJ-25): all checked out → verified; some did and the rest failed or could
 * not be checked → partial; none did but one failed → not supported; none
 * could be checked → unverifiable. With only the one-word verdict (older
 * reports) that word is all there is. Nothing at all → unchecked.
 */
export function sourceStanding(s: VerdictSource): SourceStanding {
  const c = sourceCounts(s);
  if (c) {
    if (c.supported > 0) return c.not_supported + c.unverifiable > 0 ? 'partial' : 'verified';
    if (c.not_supported > 0) return 'not_supported';
    return 'unverifiable';
  }
  if (s.citationVerdict === 'supported') return 'verified';
  if (s.citationVerdict === 'not_supported') return 'not_supported';
  if (s.citationVerdict === 'unverifiable') return 'unverifiable';
  return 'unchecked';
}

/** True when the report predates per-sentence detail: the one word may hide a failure. */
export function verdictIsSummaryOnly(s: VerdictSource): boolean {
  return !!s.citationVerdict && !sourceCounts(s);
}

export interface VerificationSummary {
  /** Did the citation-checking pass leave anything on this report at all? */
  checked: boolean;
  /** Sources by standing. */
  sources: Record<SourceStanding, number>;
  /** Citing sentences by verdict, when the report carries per-sentence counts. */
  sentences: CheckCounts | null;
}

export function summarise(sources: VerdictSource[]): VerificationSummary {
  const bySource: Record<SourceStanding, number> = { verified: 0, partial: 0, not_supported: 0, unverifiable: 0, unchecked: 0 };
  let sentences: CheckCounts | null = null;
  for (const s of sources) {
    bySource[sourceStanding(s)]++;
    const c = sourceCounts(s);
    if (c) {
      sentences = sentences ?? { supported: 0, not_supported: 0, unverifiable: 0 };
      sentences.supported += c.supported;
      sentences.not_supported += c.not_supported;
      sentences.unverifiable += c.unverifiable;
    }
  }
  return { checked: sources.some((s) => sourceStanding(s) !== 'unchecked'), sources: bySource, sentences };
}

/* ── Blind review ─────────────────────────────────────────────────────── */

export const REVIEW_SCORE_KEYS = ['answers_question', 'evidence_support', 'source_quality', 'internal_consistency', 'completeness'] as const;
export type ReviewScoreKey = (typeof REVIEW_SCORE_KEYS)[number];

export type FindingSeverity = 'high' | 'medium';

/** A finding of the reviewer. Severity comes from its kind: the reviewer
 *  names unsupported claims and weaknesses but grades neither, and a claim
 *  its own sources do not back is the graver of the two. */
export interface ReviewFinding {
  kind: 'unsupported_claim' | 'weakness';
  severity: FindingSeverity;
  text: string;
}

export interface BlindReview {
  /** 'done' with a grade, or 'failed' with the reason it recorded. */
  state: 'done' | 'failed';
  model: string;
  /** 1-5, or null when the review failed before grading. */
  overall: number | null;
  scores: Partial<Record<ReviewScoreKey, number>>;
  findings: ReviewFinding[];
  /** Writer's self-score minus the reviewer's, both on 0..1. Positive: the
   *  writer was more confident than the reviewer. Null when either is missing. */
  calibrationGap: number | null;
  durationS: number | null;
  error: string;
}

function strings(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string' && x.trim() !== '').map((x) => x.trim()) : [];
}

function grade(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 1 && v <= 5 ? Math.round(v) : null;
}

/** The persisted `blind_review` dict, or null when the review never ran. */
export function blindReviewFrom(raw: unknown): BlindReview | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const error = typeof r.error === 'string' ? r.error.trim() : '';
  const overall = grade(r.overall);
  const scores: Partial<Record<ReviewScoreKey, number>> = {};
  if (r.scores && typeof r.scores === 'object') {
    for (const k of REVIEW_SCORE_KEYS) {
      const g = grade((r.scores as Record<string, unknown>)[k]);
      if (g !== null) scores[k] = g;
    }
  }
  const findings: ReviewFinding[] = [
    ...strings(r.unsupported_claims).map((text) => ({ kind: 'unsupported_claim' as const, severity: 'high' as const, text })),
    ...strings(r.weaknesses).map((text) => ({ kind: 'weakness' as const, severity: 'medium' as const, text })),
  ];
  const gap = typeof r.calibration_gap === 'number' && Number.isFinite(r.calibration_gap) ? r.calibration_gap : null;
  return {
    // A result without a grade is not a review, whatever else it carries.
    state: error || overall === null ? 'failed' : 'done',
    model: typeof r.model === 'string' ? r.model : '',
    overall,
    scores,
    findings,
    calibrationGap: gap,
    durationS: typeof r.duration_s === 'number' && Number.isFinite(r.duration_s) ? r.duration_s : null,
    // The server's own words; empty for a failed review that gave no reason
    // (the view says "no grade" in the reader's language).
    error,
  };
}

/** How the overall grade reads: 4-5 good, 3 mixed, 1-2 poor. */
export function reviewTone(overall: number | null): 'good' | 'mixed' | 'poor' | 'none' {
  if (overall === null) return 'none';
  if (overall >= 4) return 'good';
  if (overall === 3) return 'mixed';
  return 'poor';
}

/** Which way the writer's confidence and the reviewer's grade disagree;
 *  a gap under a quarter of the scale (one step of the 1-5 grade) is agreement. */
export function calibrationReading(gap: number | null): 'overconfident' | 'underconfident' | 'agrees' | 'unknown' {
  if (gap === null) return 'unknown';
  if (gap >= 0.25) return 'overconfident';
  if (gap <= -0.25) return 'underconfident';
  return 'agrees';
}
