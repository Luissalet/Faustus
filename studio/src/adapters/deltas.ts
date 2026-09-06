import { asArray, getJson } from './api';

/**
 * Universal Delta Engine (`/api/deltas`), shaped for one screen.
 *
 * A delta answers "what changed, was it what you asked for, and how well do we
 * know". This layer exists so that the third of those never gets dropped on
 * the way to a pixel, because a list of differences rendered without its
 * coverage and its confidence is a claim of completeness nobody made.
 *
 * Three rules of PRODUCT, not of style, and every one of them is a pure
 * function below rather than a line inside a component:
 *
 * **`unknown` is never drawn as `preserved`.** `preserved` costs an
 * observation and a threshold (`contracts.py`, rule 1: "not detected is not
 * preserved"). A row that claims it without a measurement behind it is read
 * DOWN to `unknown` by `effectiveClassification`, and `groupByClassification`
 * files it there. "We did not check" is an answer and it keeps its own place
 * on the page.
 *
 * **Coverage and confidence are two numbers and stay two numbers.** Full
 * structural coverage from a `perceptual` tier at `low` confidence is a real
 * and common situation. Nothing here multiplies them; `coverageRows` reports
 * the axes and `summarize` reports the confidence, separately, always.
 *
 * **A blocking finding is never summarised away.** `blockingFindings` returns
 * the whole assertion and the whole invariant result -- method, tier,
 * evidence and all -- so the screen has no excuse to draw a count instead.
 *
 * The vocabularies below mirror `src/delta_engine/contracts.py`. They are
 * closed there and closed here, and where this file has to guess about a word
 * it has never heard, it always guesses in the direction that under-claims.
 */

/* -- small readers: the API is JSON out of SQLite, not a typed contract -- */

const str = (value: unknown): string => (typeof value === 'string' ? value : '');
const num = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
const flag = (value: unknown): boolean => value === true;

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

const strList = (value: unknown): string[] => asArray<unknown>(value).map(str).filter(Boolean);

/**
 * A 0..1 ratio, or null when the value is not one.
 *
 * Null and not zero, and this is the single most important reader in the file:
 * "we did not measure this axis" and "we measured it and covered none of it"
 * are different answers, and defaulting the first to the second is exactly the
 * lie this subsystem exists not to tell. A number outside 0..1 is an
 * extractor's arithmetic bug and reads as unmeasured rather than as complete.
 */
function ratio(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (value < 0 || value > 1) return null;
  return value;
}

/* -- the closed vocabularies, mirrored from src/delta_engine/contracts.py -- */

export const DOMAINS = [
  'code', 'document', 'image', 'video', 'audio', 'workflow', 'skill', 'state', 'binary',
];

/**
 * What a `RevisionRef` points at. `literal` is a value handed in by the caller
 * and still requires its hash, so two runs comparing "the same" literal can be
 * told apart when they were not.
 */
export const REVISION_KINDS = [
  'checkpoint', 'file', 'artifact', 'document', 'state', 'workflow', 'skill', 'blob', 'literal',
];

/** What the extractor SAW. Not what it means -- that is the classification. */
export const OPERATIONS = ['added', 'missing', 'modified', 'moved', 'reencoded', 'unchanged'];

/** The operations that assert a difference. `reencoded` is one of them. */
export const CHANGE_OPERATIONS = ['added', 'missing', 'modified', 'moved', 'reencoded'];

/** What the observation MEANS against the frozen intent. The second axis. */
export const CLASSIFICATIONS = [
  'requested', 'required', 'incidental', 'regression', 'preserved', 'unknown',
];

/** The classes a caller has to read before acting. */
export const MATERIAL_CLASSIFICATIONS = ['regression', 'incidental', 'unknown'];

/** Weakest to strongest. `severityRank` reads this order and nothing else. */
export const SEVERITIES = ['info', 'minor', 'material', 'blocking'];

/** What stops a caller. Neither of these may ever be summarised away. */
export const BLOCKING_SEVERITIES = ['material', 'blocking'];

/** Strongest to weakest, on purpose: combining confidences is a `min`. */
export const CONFIDENCE = ['exact', 'high', 'medium', 'low', 'unknown'];

/** The determinism ladder, most trustworthy first. */
export const EXTRACTION_TIERS = ['hash', 'parser', 'algorithm', 'perceptual', 'model', 'human'];

/**
 * The best confidence a finding from each tier may claim (§3.2). The server
 * applies this in `DeltaAssertion.parse`; it is repeated here so the screen can
 * SAY that a number was capped, rather than only inheriting the cap silently.
 */
export const TIER_CEILING: Record<string, string> = {
  hash: 'exact', parser: 'exact', algorithm: 'high',
  perceptual: 'medium', model: 'medium', human: 'high',
};

export const INVARIANT_STATUSES = ['preserved', 'violated', 'unknown', 'not_applicable'];

export const COVERAGE_DIMENSIONS = [
  'structural', 'semantic', 'temporal', 'identity', 'spatial', 'behavioral',
];

/** The verdict about the CHANGE. Not `prove.VERDICTS`, and never becomes it. */
export const ASSESSMENTS = ['matched', 'partial', 'mismatched', 'regressed', 'inconclusive'];

/**
 * The order the classifications are drawn in, and it is the layout.
 *
 * `regression` first because it is why anyone opened the delta. `unknown`
 * SECOND -- above the good news, not below it and not behind a "show more" --
 * because "we did not check this" is the answer a reader is most likely to
 * assume they were not given. `preserved` last: it is the cheapest to read and
 * the least urgent, and it is the one word that had to be earned.
 */
export const CLASSIFICATION_ORDER = [
  'regression', 'unknown', 'incidental', 'required', 'requested', 'preserved',
];

/**
 * The order invariant results are drawn in. Violated first, then the ones
 * nobody could check, and only then the ones that held. An `unknown` sorted in
 * amongst the greens is the same lie as an `unknown` coloured green.
 */
export const INVARIANT_ORDER = ['violated', 'unknown', 'preserved', 'not_applicable'];

/* -- the refusal convention ---------------------------------------------- */

/**
 * A rejected request answers 200 with `{ok:false, error:{path,message,code}}`.
 * The token is what a caller branches on, the sentence is for the person
 * reading and the path is the field that was wrong. Branching on the sentence
 * is the habit this repository has already paid for once.
 */
export interface DeltaError {
  code: string;
  message: string;
  path: string;
  /** Present on a `disabled` refusal: false, and the reason the button is off. */
  enabled?: boolean;
}

export class DeltaRefusal extends Error {
  readonly code: string;
  readonly path: string;
  readonly enabled?: boolean;

  constructor(error: DeltaError) {
    super(error.message);
    this.name = 'DeltaRefusal';
    this.code = error.code;
    this.path = error.path;
    this.enabled = error.enabled;
  }
}

/** The refusal inside a 200 body, or null when the call succeeded. */
export function refusalOf(payload: unknown): DeltaError | null {
  const body = obj(payload);
  if (body.ok !== false) return null;
  const nested = obj(body.error);
  const code = str(nested.code).trim() || str(body.error).trim();
  const message = str(nested.message).trim() || str(body.detail).trim();
  return {
    code: code || 'refused',
    message: message || 'the delta engine refused this and did not say why',
    path: str(nested.path).trim(),
    enabled: typeof body.enabled === 'boolean' ? body.enabled : undefined,
  };
}

/* -- the shapes, as the server sends them -------------------------------- */

/** One immutable end of a comparison. The hash is what makes it immutable. */
export interface RevisionRef {
  kind: string;
  ref: string;
  hash: string;
  label: string;
  at: string;
  sensitivity: string;
}

/**
 * A pointer to what was looked at. Never the evidence itself -- an overlay is
 * an artifact with a hash and a retention policy, and inlining it would make a
 * delta expensive to read, which is how a delta stops being read at all.
 */
export interface EvidenceRef {
  kind: string;
  ref: string;
  detail: string;
  hash: string;
  sensitivity: string;
  /** Set by `GET /{id}/evidence`, which says which assertion each belongs to. */
  assertionId: string;
}

export interface Alignment {
  relation: string;
  sourceElement: string;
  targetElement: string;
  confidence: string;
  method: string;
  tier: string;
}

export interface Assertion {
  id: string;
  path: string;
  /** What was SEEN. */
  operation: string;
  /** What it MEANS against the frozen intent. A different question. */
  classification: string;
  severity: string;
  confidence: string;
  tier: string;
  before: string;
  after: string;
  method: string;
  detail: string;
  alignment: Alignment | null;
  evidenceRefs: EvidenceRef[];
  intentRefs: string[];
  invariantRefs: string[];
  limitations: string[];
}

export interface InvariantResult {
  invariantId: string;
  status: string;
  confidence: string;
  tier: string;
  severity: string;
  observations: string[];
  threshold: Record<string, unknown>;
  method: string;
  evidenceRefs: EvidenceRef[];
  limitations: string[];
}

/**
 * How much of the comparison could actually be made.
 *
 * `dimensions` holds only the axes that were REPORTED. An axis nobody measured
 * is absent here and comes back as `null` from `coverageRatio`, never as 0.
 */
export interface Coverage {
  sourceReadable: boolean;
  targetReadable: boolean;
  dimensions: Record<string, number>;
  regionsAnalyzed: string[];
  excluded: string[];
  notes: string[];
}

export interface Delta {
  id: string;
  requestId: string;
  owner: string;
  domain: string;
  source: RevisionRef;
  target: RevisionRef;
  assessment: string;
  intentContractId: string;
  intentFingerprint: string;
  assertions: Assertion[];
  invariants: InvariantResult[];
  coverage: Coverage;
  evidenceRefs: EvidenceRef[];
  extractorVersions: Record<string, unknown>;
  limitations: string[];
  projectId: string;
  sessionId: string;
  runId: string;
  correlationId: string;
  proofRef: string;
  elapsedMs: number;
  createdAt: string;
  schemaVersion: number;
}

/**
 * The five counts a card carries. `unknowns` is one of them and is never
 * folded into another: a card that reported only assertions and regressions
 * would let a reader infer that everything else was checked and fine.
 */
export interface SummaryCounts {
  assertions: number;
  material: number;
  regressions: number;
  unknowns: number;
  invariantsViolated: number;
}

/** One row in the listing. Everything here is derivable from the delta. */
export interface Summary {
  id: string;
  domain: string;
  assessment: string;
  createdAt: string;
  projectId: string;
  intentContractId: string;
  sourceLabel: string;
  targetLabel: string;
  counts: SummaryCounts;
  severity: string;
  confidence: string;
}

/** Everything a detail view counts. Computed, never stored. */
export interface DeltaCounts extends SummaryCounts {
  changed: number;
  incidental: number;
  preserved: number;
  requested: number;
  required: number;
  blocking: number;
  invariantsUnknown: number;
  invariantsPreserved: number;
  invariantsNotApplicable: number;
  evidence: number;
  limitations: number;
}

export interface ExtractorStatus {
  domain: string;
  available: boolean;
  version: string;
  module: string;
  /** Why it is not available. The only reason a status panel is worth drawing. */
  reason: string;
}

export interface Diagnostics {
  enabled: boolean;
  counts: Record<string, number>;
  dbPath: string;
  streams: string[];
}

/** The closed vocabularies as THIS BUILD OF THE SERVER has them. */
export interface Config {
  enabled: boolean;
  domains: string[];
  assessments: string[];
  classifications: string[];
  operations: string[];
  severities: string[];
  confidence: string[];
  tiers: string[];
  invariantClasses: string[];
  invariantStatuses: string[];
  coverageDimensions: string[];
  evidenceKinds: string[];
  conditionKinds: string[];
}

export interface IntentCompilation {
  intent: Record<string, unknown>;
  fingerprint: string;
  /**
   * The fragments the compiler could not turn into a checkable condition.
   * Listed rather than dropped: a contract with unknowns can still run, and
   * what it cannot do is come back `matched`.
   */
  unknowns: string[];
}

export interface DeltaDetail {
  delta: Delta;
  explanation: string;
  reclassifications: Record<string, unknown>[];
  cached: boolean;
}

export interface DeltaPage {
  enabled: boolean;
  deltas: Summary[];
  nextCursor: string;
}

export interface CreatedDelta {
  requestId: string;
  intentContractId: string;
  /** Present when the request asked to `run`; otherwise the delta is pending. */
  delta: Delta | null;
}

/* -- readers ------------------------------------------------------------- */

export function revisionFrom(raw: unknown): RevisionRef {
  const r = obj(raw);
  return {
    kind: str(r.kind),
    ref: str(r.ref),
    hash: str(r.hash),
    label: str(r.label),
    at: str(r.at),
    sensitivity: str(r.sensitivity) || 'internal',
  };
}

export function evidenceFrom(raw: unknown): EvidenceRef {
  const e = obj(raw);
  return {
    kind: str(e.kind),
    ref: str(e.ref),
    detail: str(e.detail),
    hash: str(e.hash),
    sensitivity: str(e.sensitivity) || 'internal',
    assertionId: str(e.assertion_id),
  };
}

export function alignmentFrom(raw: unknown): Alignment | null {
  const a = obj(raw);
  if (!Object.keys(a).length) return null;
  return {
    relation: str(a.relation) || 'uncertain',
    sourceElement: str(a.source_element),
    targetElement: str(a.target_element),
    confidence: str(a.confidence) || 'unknown',
    method: str(a.method),
    tier: str(a.tier) || 'algorithm',
  };
}

/**
 * One assertion.
 *
 * Every defaulted field defaults DOWNWARDS: a missing confidence is `unknown`
 * and a missing classification is `unknown`, never the friendlier reading. A
 * row that arrived without a word is a row nobody said anything about.
 */
export function assertionFrom(raw: unknown): Assertion {
  const a = obj(raw);
  return {
    id: str(a.id),
    path: str(a.path),
    operation: str(a.operation) || 'unchanged',
    classification: str(a.classification) || 'unknown',
    severity: str(a.severity) || 'info',
    confidence: str(a.confidence) || 'unknown',
    tier: str(a.tier) || 'algorithm',
    before: str(a.before),
    after: str(a.after),
    method: str(a.method),
    detail: str(a.detail),
    alignment: alignmentFrom(a.alignment),
    evidenceRefs: asArray<unknown>(a.evidence_refs).map(evidenceFrom),
    intentRefs: strList(a.intent_refs),
    invariantRefs: strList(a.invariant_refs),
    limitations: strList(a.limitations),
  };
}

export function invariantFrom(raw: unknown): InvariantResult {
  const i = obj(raw);
  return {
    invariantId: str(i.invariant_id),
    status: str(i.status) || 'unknown',
    confidence: str(i.confidence) || 'unknown',
    tier: str(i.tier) || 'algorithm',
    severity: str(i.severity) || 'material',
    observations: strList(i.observations),
    threshold: obj(i.threshold),
    method: str(i.method),
    evidenceRefs: asArray<unknown>(i.evidence_refs).map(evidenceFrom),
    limitations: strList(i.limitations),
  };
}

/**
 * Coverage, keeping the difference between unmeasured and zero.
 *
 * An axis whose value is not a ratio is DROPPED rather than stored as 0. It
 * comes back from `coverageRatio` as null and is drawn as "not measured", and
 * that is the whole reason this reader is not a spread of the raw object.
 */
export function coverageFrom(raw: unknown): Coverage {
  const c = obj(raw);
  const dims: Record<string, number> = {};
  for (const [name, value] of Object.entries(obj(c.dimensions))) {
    const measured = ratio(value);
    if (measured !== null) dims[name] = measured;
  }
  return {
    sourceReadable: flag(c.source_readable),
    targetReadable: flag(c.target_readable),
    dimensions: dims,
    regionsAnalyzed: strList(c.regions_analyzed),
    excluded: strList(c.excluded),
    notes: strList(c.notes),
  };
}

export function deltaFrom(raw: unknown): Delta {
  const d = obj(raw);
  return {
    id: str(d.id),
    requestId: str(d.request_id),
    owner: str(d.owner),
    domain: str(d.domain),
    source: revisionFrom(d.source),
    target: revisionFrom(d.target),
    assessment: str(d.assessment) || 'inconclusive',
    intentContractId: str(d.intent_contract_id),
    intentFingerprint: str(d.intent_fingerprint),
    assertions: asArray<unknown>(d.assertions).map(assertionFrom),
    invariants: asArray<unknown>(d.invariants).map(invariantFrom),
    coverage: coverageFrom(d.coverage),
    evidenceRefs: asArray<unknown>(d.evidence_refs).map(evidenceFrom),
    extractorVersions: obj(d.extractor_versions),
    limitations: strList(d.limitations),
    projectId: str(d.project_id),
    sessionId: str(d.session_id),
    runId: str(d.run_id),
    correlationId: str(d.correlation_id),
    proofRef: str(d.proof_ref),
    elapsedMs: num(d.elapsed_ms),
    createdAt: str(d.created_at),
    schemaVersion: num(d.schema_version),
  };
}

/**
 * A listing row, as the server sends it.
 *
 * The counts arrive already computed because a listing that shipped every
 * assertion to draw a card would be a megabyte for a sidebar. What this reader
 * must not do is invent one it was not sent: a missing count reads as 0
 * because a count IS a number the server computed, unlike a coverage ratio,
 * where absence is a fact about the measurement rather than about the row.
 */
export function summaryFrom(raw: unknown): Summary {
  const s = obj(raw);
  const c = obj(s.counts);
  return {
    id: str(s.id),
    domain: str(s.domain),
    assessment: str(s.assessment) || 'inconclusive',
    createdAt: str(s.created_at),
    projectId: str(s.project_id),
    intentContractId: str(s.intent_contract_id),
    sourceLabel: str(s.source_label),
    targetLabel: str(s.target_label),
    counts: {
      assertions: num(c.assertions),
      material: num(c.material),
      regressions: num(c.regressions),
      unknowns: num(c.unknowns),
      invariantsViolated: num(c.invariants_violated),
    },
    severity: str(s.severity) || 'info',
    confidence: str(s.confidence) || 'unknown',
  };
}

export function extractorsFrom(raw: unknown): ExtractorStatus[] {
  const rows = obj(obj(raw).extractors ?? raw);
  return Object.entries(rows)
    .map(([domain, value]) => {
      const e = obj(value);
      return {
        domain,
        available: flag(e.available),
        version: str(e.version),
        module: str(e.module),
        reason: str(e.reason),
      };
    })
    .sort((left, right) => left.domain.localeCompare(right.domain));
}

export function diagnosticsFrom(raw: unknown): Diagnostics {
  const d = obj(raw);
  const counts: Record<string, number> = {};
  for (const [name, value] of Object.entries(obj(d.counts))) counts[name] = num(value);
  return {
    enabled: flag(d.enabled),
    counts,
    dbPath: str(d.db_path),
    streams: strList(d.streams),
  };
}

/**
 * The server's own vocabularies.
 *
 * Read from the server rather than assumed, so that a build whose engine knows
 * a domain this bundle has never heard of still offers it in the filters; the
 * constants above are the fallback for a server that answered nothing.
 */
export function configFrom(raw: unknown): Config {
  const c = obj(raw);
  const list = (key: string, fallback: string[]): string[] => {
    const values = strList(c[key]);
    return values.length ? values : fallback;
  };
  return {
    enabled: flag(c.enabled),
    domains: list('domains', DOMAINS),
    assessments: list('assessments', ASSESSMENTS),
    classifications: list('classifications', CLASSIFICATIONS),
    operations: list('operations', OPERATIONS),
    severities: list('severities', SEVERITIES),
    confidence: list('confidence', CONFIDENCE),
    tiers: list('tiers', EXTRACTION_TIERS),
    invariantClasses: list('invariant_classes', []),
    invariantStatuses: list('invariant_statuses', INVARIANT_STATUSES),
    coverageDimensions: list('coverage_dimensions', COVERAGE_DIMENSIONS),
    evidenceKinds: list('evidence_kinds', []),
    conditionKinds: list('condition_kinds', []),
  };
}

/* -- the ladders: how two findings compare ------------------------------- */

/**
 * Position in `SEVERITIES`. Higher is worse.
 *
 * A word this build has never heard of ranks as `info` -- the same reading
 * `contracts.py::severity_rank` gives it. Ranking an unknown word HIGH would
 * let a typo in an extractor promote a cosmetic finding to the top of the
 * page, which is the direction that costs a reader their attention for
 * nothing; ranking it low costs at most one row read late.
 */
export function severityRank(severity: string): number {
  const at = SEVERITIES.indexOf(str(severity).trim());
  return at < 0 ? 0 : at;
}

/**
 * Position in `CONFIDENCE`. Higher is WEAKER -- the list runs downhill,
 * exactly as it does on the server, because combining confidences is a `max`
 * over this rank and never an average. An average of `exact` and `unknown` is
 * `medium`, which is a number nobody observed.
 *
 * A word this build has never heard of ranks LAST, which is the direction that
 * cannot launder a guess into a certainty.
 */
export function confidenceRank(confidence: string): number {
  const at = CONFIDENCE.indexOf(str(confidence).trim());
  return at < 0 ? CONFIDENCE.length - 1 : at;
}

/** Position in `EXTRACTION_TIERS`. An unknown method ranks last, never first. */
export function tierRank(tier: string): number {
  const at = EXTRACTION_TIERS.indexOf(str(tier).trim());
  return at < 0 ? EXTRACTION_TIERS.length - 1 : at;
}

/** The strongest of several severities: how a set of findings rolls up. */
export function strongestSeverity(values: string[]): string {
  const seen = (values ?? []).filter((v) => SEVERITIES.indexOf(str(v).trim()) >= 0);
  if (!seen.length) return 'info';
  return SEVERITIES[Math.max(...seen.map(severityRank))];
}

/**
 * The weakest of several confidences. The propagation rule, once.
 *
 * A finding is exactly as believable as the weakest link that produced it --
 * the alignment, the extraction, the threshold -- so this is a `min` over the
 * ladder. Nothing at all is `unknown`, not `exact`.
 */
export function weakestConfidence(values: string[]): string {
  if (!values || !values.length) return 'unknown';
  return CONFIDENCE[Math.max(...values.map(confidenceRank))];
}

/** The best confidence a finding from this tier may claim. Unknown -> `low`. */
export function ceilingForTier(tier: string): string {
  return TIER_CEILING[str(tier).trim()] ?? 'low';
}

/**
 * Whether a finding's confidence was held down by the tier that produced it.
 *
 * Worth a word on screen: "medium, and it could not have been more, because a
 * perceptual hash is not identity" is a different sentence from "medium,
 * because the measurement was borderline", and a reader deciding whether to
 * look closer needs to know which one they are being told.
 */
export function cappedByTier(confidence: string, tier: string): boolean {
  return confidenceRank(confidence) === confidenceRank(ceilingForTier(tier))
    && confidenceRank(ceilingForTier(tier)) > 0;
}

export type Tone = 'good' | 'warn' | 'bad' | 'unknown';

/**
 * The verdict about the change, as a tone.
 *
 * `inconclusive` is `unknown` and NOT `warn`: an amber badge would read as "it
 * mostly worked", and what it means is "we could not tell you". Those are
 * different next actions -- accept with a caveat, or go and look yourself --
 * and a tone that conflates them makes the second one never happen.
 */
export function assessmentTone(assessment: string): Tone {
  switch (str(assessment).trim()) {
    case 'matched': return 'good';
    case 'partial': return 'warn';
    case 'mismatched': return 'bad';
    case 'regressed': return 'bad';
    case 'inconclusive': return 'unknown';
    default: return 'unknown';
  }
}

/** A severity, as a tone. `material` and `blocking` are both `bad`: both stop a caller. */
export function severityTone(severity: string): Tone {
  switch (str(severity).trim()) {
    case 'blocking': return 'bad';
    case 'material': return 'bad';
    case 'minor': return 'warn';
    case 'info': return 'good';
    default: return 'unknown';
  }
}

/**
 * A confidence, as a tone. `low` is `warn` and `unknown` is `unknown`: one
 * says the measurement was weak, the other that there was no measurement, and
 * the screen never lets the second wear the colour of the first.
 */
export function confidenceTone(confidence: string): Tone {
  switch (str(confidence).trim()) {
    case 'exact': return 'good';
    case 'high': return 'good';
    case 'medium': return 'warn';
    case 'low': return 'warn';
    default: return 'unknown';
  }
}

/** An invariant status, as a tone. `not_applicable` is neutral, not good. */
export function invariantTone(status: string): Tone {
  switch (str(status).trim()) {
    case 'preserved': return 'good';
    case 'violated': return 'bad';
    case 'not_applicable': return 'unknown';
    default: return 'unknown';
  }
}

/* -- rule one: not detected is not preserved ----------------------------- */

/**
 * The classification this row may actually be DRAWN under.
 *
 * `preserved` is a conclusion that costs an observation and a threshold. The
 * server refuses to build a row that claims it without one -- `preserved` with
 * `confidence: unknown`, or `preserved` beside an operation that says the
 * thing changed -- and this function is what keeps the screen honest if such a
 * row ever reaches it anyway: from another build, from a fixture, from a
 * migration, from a hand-written record in the database.
 *
 * It reads DOWN to `unknown`, never up. "We did not check" is the only answer
 * that is safe to give when the row contradicts itself, and it is the answer
 * this whole screen exists to be able to give out loud.
 */
export function effectiveClassification(assertion: Assertion | null | undefined): string {
  if (!assertion) return 'unknown';
  const claimed = str(assertion.classification).trim() || 'unknown';
  if (claimed !== 'preserved') {
    return CLASSIFICATIONS.indexOf(claimed) >= 0 ? claimed : 'unknown';
  }
  if (str(assertion.confidence).trim() === 'unknown' || !assertion.confidence) return 'unknown';
  if (CHANGE_OPERATIONS.indexOf(str(assertion.operation).trim()) >= 0) return 'unknown';
  return 'preserved';
}

/**
 * The rows that CLAIMED `preserved` and had nothing behind the claim.
 *
 * Returned rather than only downgraded, so the screen can say why a row it was
 * told was fine is filed under "not checked". A silent correction is still a
 * correction the reader cannot audit.
 */
export function unverifiedPreserved(delta: Delta | null | undefined): Assertion[] {
  return (delta?.assertions ?? []).filter(
    (a) => str(a.classification).trim() === 'preserved' && effectiveClassification(a) !== 'preserved',
  );
}

/* -- ordering ------------------------------------------------------------ */

/**
 * Assertions, worst first.
 *
 * `(-severity, confidence, path)` -- the same key
 * `UniversalDelta.material_assertions` sorts by on the server, deliberately:
 * every consumer reads a long delta down to its first rows, and the client and
 * the server must lose the SAME rows when they do, or two readers of one delta
 * disagree about what it said.
 *
 * Second key is confidence, strongest first: between two blocking findings,
 * the one we are certain about is the one to act on.
 */
export function orderedAssertions(assertions: Assertion[] | null | undefined): Assertion[] {
  return (assertions ?? []).slice().sort((left, right) => {
    const bySeverity = severityRank(right.severity) - severityRank(left.severity);
    if (bySeverity !== 0) return bySeverity;
    const byConfidence = confidenceRank(left.confidence) - confidenceRank(right.confidence);
    if (byConfidence !== 0) return byConfidence;
    return str(left.path).localeCompare(str(right.path));
  });
}

/**
 * Position in `INVARIANT_ORDER`. A status this build has never heard of ranks
 * with `unknown` and never with `preserved`: the safe reading of a word we
 * cannot interpret is "nobody told us this held", not "it held".
 */
export function invariantStatusRank(status: string): number {
  const at = INVARIANT_ORDER.indexOf(str(status).trim());
  return at < 0 ? INVARIANT_ORDER.indexOf('unknown') : at;
}

/**
 * Invariant results: violated, then unknown, then preserved, then n/a.
 *
 * The order is the product rule. An `unknown` sorted in amongst the preserved
 * ones is the same lie as an `unknown` coloured green, and it is the easier of
 * the two to commit by accident.
 */
export function orderedInvariants(results: InvariantResult[] | null | undefined): InvariantResult[] {
  return (results ?? []).slice().sort((left, right) => {
    const byStatus = invariantStatusRank(left.status) - invariantStatusRank(right.status);
    if (byStatus !== 0) return byStatus;
    const bySeverity = severityRank(right.severity) - severityRank(left.severity);
    if (bySeverity !== 0) return bySeverity;
    const byConfidence = confidenceRank(left.confidence) - confidenceRank(right.confidence);
    if (byConfidence !== 0) return byConfidence;
    return str(left.invariantId).localeCompare(str(right.invariantId));
  });
}

/**
 * Every assertion in its group, in `CLASSIFICATION_ORDER` and never in another.
 *
 * Two decisions carry the product rule:
 *
 * - The grouping key is `effectiveClassification`, so a row that claimed
 *   `preserved` without a measurement lands under `unknown`. There is no path
 *   through this function that puts one in the preserved bucket.
 * - Empty groups are RETURNED rather than dropped, so a delta with nothing
 *   unknown says "unknown: 0" instead of leaving the reader to notice an
 *   absent heading. An absence nobody can see gets read as a zero anyway; this
 *   way it is read as the zero it actually is.
 */
export function groupByClassification(
  assertions: Assertion[] | null | undefined,
): Record<string, Assertion[]> {
  const groups: Record<string, Assertion[]> = {};
  for (const name of CLASSIFICATION_ORDER) groups[name] = [];
  for (const assertion of assertions ?? []) {
    if (!assertion) continue;
    const name = effectiveClassification(assertion);
    (groups[name] ??= []).push(assertion);
  }
  for (const name of Object.keys(groups)) groups[name] = orderedAssertions(groups[name]);
  return groups;
}

/* -- rule two: coverage is not confidence -------------------------------- */

export interface CoverageRow {
  dimension: string;
  /** null means NOT MEASURED. It is never 0, and 0 is never it. */
  ratio: number | null;
  /** The key `t()` translates. A word, never a colour and never a number. */
  label: string;
}

/** The word each axis is drawn under. */
const DIMENSION_LABEL: Record<string, string> = {
  structural: 'structure',
  semantic: 'meaning',
  temporal: 'time',
  identity: 'identity',
  spatial: 'space',
  behavioral: 'behaviour',
};

/**
 * The ratio for one axis, or null when it was not reported.
 *
 * The one function in this file that must never be written with `?? 0`.
 * "We did not measure this axis" and "we measured it and covered none of it"
 * lead to opposite next actions -- go and measure it, against go and look at
 * what was missed -- and a default of zero turns every one of the first into
 * one of the second, silently, for ever.
 */
export function coverageRatio(coverage: Coverage | null | undefined, dimension: string): number | null {
  const value = coverage?.dimensions?.[str(dimension)];
  return typeof value === 'number' ? value : null;
}

/**
 * Every axis, in the canonical order, measured or not.
 *
 * All six are returned and not only the reported ones, because a table that
 * listed three axes would let a reader believe those three were the axes that
 * exist. The unmeasured ones come back with `ratio: null` and the screen draws
 * them as "not measured" -- not as an empty bar, which is a zero wearing a
 * different hat.
 */
export function coverageRows(coverage: Coverage | null | undefined): CoverageRow[] {
  return COVERAGE_DIMENSIONS.map((dimension) => ({
    dimension,
    ratio: coverageRatio(coverage, dimension),
    label: DIMENSION_LABEL[dimension] ?? dimension,
  }));
}

/**
 * What the coverage does NOT say, in sentences, as `t()` keys.
 *
 * A coverage block that only drew bars would be read as a score. These are the
 * sentences that stop that: an unreadable end means there was no comparison at
 * all, no reported axis means the question "how much was compared" has no
 * answer, and an excluded region is a hole a reader must know about before
 * treating the delta as complete.
 */
export function describeCoverageGap(coverage: Coverage | null | undefined): string[] {
  const gaps: string[] = [];
  const rows = coverageRows(coverage);
  if (!coverage || !coverage.sourceReadable) {
    gaps.push('The source could not be read, so nothing here is a comparison.');
  }
  if (!coverage || !coverage.targetReadable) {
    gaps.push('The target could not be read, so nothing here is a comparison.');
  }
  const measured = rows.filter((row) => row.ratio !== null);
  if (!measured.length) {
    gaps.push('No axis was measured, so how much of this could be compared is not known.');
  } else if (measured.length < rows.length) {
    gaps.push('Some axes were not measured at all. They are shown as not measured, never as zero.');
  }
  if (measured.some((row) => (row.ratio ?? 0) < 1)) {
    gaps.push('At least one axis was only partly covered, so an absence of findings on it is not evidence of no change.');
  }
  if (coverage && coverage.excluded.length) {
    gaps.push('Some regions were excluded from the comparison and nothing is claimed about them.');
  }
  return gaps;
}

/* -- counts, summaries and labels ---------------------------------------- */

/** Every count a detail view draws. Computed here, stored nowhere. */
export function counts(delta: Delta | null | undefined): DeltaCounts {
  const assertions = delta?.assertions ?? [];
  const invariants = delta?.invariants ?? [];
  const by = (name: string): number =>
    assertions.filter((a) => effectiveClassification(a) === name).length;
  const withStatus = (name: string): number =>
    invariants.filter((i) => str(i.status).trim() === name).length;
  return {
    assertions: assertions.length,
    changed: assertions.filter((a) => CHANGE_OPERATIONS.indexOf(a.operation) >= 0).length,
    material: assertions.filter((a) => BLOCKING_SEVERITIES.indexOf(a.severity) >= 0).length,
    blocking: assertions.filter((a) => a.severity === 'blocking').length,
    regressions: by('regression'),
    incidental: by('incidental'),
    unknowns: by('unknown'),
    preserved: by('preserved'),
    requested: by('requested'),
    required: by('required'),
    invariantsViolated: withStatus('violated'),
    invariantsUnknown: withStatus('unknown'),
    invariantsPreserved: withStatus('preserved'),
    invariantsNotApplicable: withStatus('not_applicable'),
    evidence: (delta?.evidenceRefs ?? []).length,
    limitations: (delta?.limitations ?? []).length,
  };
}

/** The first seven characters of a sha256, or `""` when there is no hash. */
export function shortHash(hash: string): string {
  const value = str(hash).trim().toLowerCase();
  return value ? value.slice(0, 7) : '';
}

/**
 * What to call one end of the comparison: its label, and the head of its hash.
 *
 * Both halves, always, and the hash is why. A label is what a person recognises
 * and a hash is what makes the comparison mean anything -- two runs against
 * "main" are two different comparisons, and a card that showed only the word
 * `main` would present them as one. A revision that carries only a hash is
 * still drawn, under it.
 */
export function revisionLabel(ref: RevisionRef | null | undefined): string {
  if (!ref) return '';
  const name = str(ref.label).trim() || str(ref.ref).trim() || str(ref.kind).trim();
  const short = shortHash(ref.hash);
  if (!name) return short;
  return short ? `${name} · ${short}` : name;
}

/**
 * A whole delta read down to a card, exactly as `/api/deltas` would summarise it.
 *
 * Same arithmetic as `UniversalDelta.summary()` on the server: the severity is
 * the STRONGEST of the findings and the confidence the WEAKEST of the ones that
 * changed something. Opposite directions, and both are the honest reading -- a
 * delta with one blocking permission change and forty cosmetic ones is a
 * blocking delta, and a delta whose weakest measurement was a guess is a delta
 * to be careful with however many exact rows sit beside it.
 *
 * The screen uses this so a card built from a full delta and a card built from
 * a listing row cannot say different things about the same comparison.
 */
export function summarize(delta: Delta): Summary {
  const c = counts(delta);
  const changed = (delta.assertions ?? []).filter(
    (a) => CHANGE_OPERATIONS.indexOf(a.operation) >= 0,
  );
  return {
    id: delta.id,
    domain: delta.domain,
    assessment: delta.assessment,
    createdAt: delta.createdAt,
    projectId: delta.projectId,
    intentContractId: delta.intentContractId,
    sourceLabel: revisionLabel(delta.source),
    targetLabel: revisionLabel(delta.target),
    counts: {
      assertions: c.assertions,
      material: c.material,
      regressions: c.regressions,
      unknowns: c.unknowns,
      invariantsViolated: c.invariantsViolated,
    },
    severity: strongestSeverity((delta.assertions ?? []).map((a) => a.severity)),
    confidence: weakestConfidence(
      changed.length ? changed.map((a) => a.confidence) : ['unknown'],
    ),
  };
}

/* -- rule three: a blocking finding is never summarised ------------------ */

export interface BlockingFindings {
  assertions: Assertion[];
  invariants: InvariantResult[];
}

/**
 * Everything a reader must have SEEN, whole, before accepting this delta.
 *
 * Returned as the objects themselves and never as a count, because the point
 * of the rule is that the screen has nothing to render but the full row: its
 * path, its before and after, the method and tier that produced it, the
 * invariants it is attached to and the evidence behind it. A count would let
 * the page collapse the one thing it must not collapse.
 *
 * A `material` assertion is in here beside a `blocking` one: `BLOCKING_SEVERITIES`
 * holds both, because "must not be summarised away" and "stops the caller" are
 * the same rule at two strengths.
 */
export function blockingFindings(delta: Delta | null | undefined): BlockingFindings {
  const assertions = orderedAssertions(
    (delta?.assertions ?? []).filter((a) => BLOCKING_SEVERITIES.indexOf(a.severity) >= 0),
  );
  const invariants = orderedInvariants(
    (delta?.invariants ?? []).filter((i) => str(i.status).trim() === 'violated'),
  );
  return { assertions, invariants };
}

/** The invariant results an assertion names, in the order they will be drawn. */
export function invariantsFor(delta: Delta | null | undefined, assertion: Assertion): InvariantResult[] {
  const wanted = new Set(assertion.invariantRefs ?? []);
  if (!wanted.size) return [];
  return orderedInvariants((delta?.invariants ?? []).filter((i) => wanted.has(i.invariantId)));
}

/**
 * The evidence for one assertion: the refs it carries, plus the ones
 * `GET /{id}/evidence` attributed to it by `assertion_id`.
 *
 * Deduplicated on `kind:ref:hash`, because the two sources overlap by design
 * and the same overlay listed twice reads as two separate observations.
 */
export function evidenceFor(assertion: Assertion, extra: EvidenceRef[] = []): EvidenceRef[] {
  const seen = new Set<string>();
  const out: EvidenceRef[] = [];
  const push = (ref: EvidenceRef): void => {
    const key = `${ref.kind}:${ref.ref}:${ref.hash}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(ref);
  };
  for (const ref of assertion.evidenceRefs ?? []) push(ref);
  for (const ref of extra ?? []) if (ref.assertionId && ref.assertionId === assertion.id) push(ref);
  return out;
}

/* -- the screen's own invariant, made checkable -------------------------- */

/**
 * Does this delta, AS THIS SCREEN WILL DRAW IT, keep `unknown` out of
 * `preserved`?
 *
 * Deliberately computed over the derived groups and never over the raw fields.
 * That makes it a guard on the derivation rather than on the payload: if
 * `groupByClassification` is ever rewritten to group by the raw
 * `classification`, or `effectiveClassification` loses one of its two
 * downgrades, this returns false and `studio/checks/deltas.check.mjs` fails --
 * which is the only way a rule written in a docstring survives the third
 * person to edit the file.
 *
 * It is driven with hostile input on purpose: a row that claims `preserved`
 * with `confidence: unknown` must still answer true here, because the screen
 * defends itself against it. What must never happen is such a row appearing
 * amongst the greens, and that is what these four checks say.
 */
export function unknownsAreNeverPreserved(delta: Delta | null | undefined): boolean {
  const assertions = delta?.assertions ?? [];
  const groups = groupByClassification(assertions);
  const preserved = groups.preserved ?? [];

  // 1. Nothing in the preserved bucket may lack the measurement the word costs.
  for (const assertion of preserved) {
    if (str(assertion.confidence).trim() === 'unknown') return false;
    if (CHANGE_OPERATIONS.indexOf(str(assertion.operation).trim()) >= 0) return false;
  }
  // 2. Every row that claimed `preserved` without one is in the unknown bucket,
  //    where a reader will look for it, rather than dropped.
  const unknown = groups.unknown ?? [];
  for (const assertion of unverifiedPreserved(delta)) {
    if (unknown.indexOf(assertion) < 0) return false;
  }
  // 3. No row is in two buckets, and none has been lost between them.
  let filed = 0;
  for (const name of Object.keys(groups)) filed += groups[name].length;
  if (filed !== assertions.length) return false;

  // 4. An `unknown` invariant never sorts into the run of preserved ones.
  let seenPreserved = false;
  for (const result of orderedInvariants(delta?.invariants ?? [])) {
    const status = str(result.status).trim();
    if (status === 'preserved') seenPreserved = true;
    else if (seenPreserved && (status === 'unknown' || INVARIANT_STATUSES.indexOf(status) < 0)) return false;
  }
  return true;
}

/* -- words: keys `t()` translates, never a colour on its own ------------- */

/** The verdict, in a word. */
export function assessmentLabel(assessment: string): string {
  switch (str(assessment).trim()) {
    case 'matched': return 'matched what was asked';
    case 'partial': return 'partly matched';
    case 'mismatched': return 'did not match';
    case 'regressed': return 'regressed';
    case 'inconclusive': return 'could not be determined';
    default: return 'could not be determined';
  }
}

/** What the classification means, spelled out rather than implied by a tint. */
export function classificationLabel(classification: string): string {
  switch (str(classification).trim()) {
    case 'requested': return 'Asked for';
    case 'required': return 'Required by the change';
    case 'incidental': return 'Changed without being asked';
    case 'regression': return 'Regression';
    case 'preserved': return 'Held still, and checked';
    case 'unknown': return 'Not checked';
    default: return 'Not checked';
  }
}

/** What the extractor saw. The other axis, and never merged with the first. */
export function operationLabel(operation: string): string {
  switch (str(operation).trim()) {
    case 'added': return 'added';
    case 'missing': return 'missing';
    case 'modified': return 'modified';
    case 'moved': return 'moved';
    case 'reencoded': return 're-encoded';
    case 'unchanged': return 'unchanged';
    default: return 'not stated';
  }
}

export function invariantStatusLabel(status: string): string {
  switch (str(status).trim()) {
    case 'preserved': return 'held';
    case 'violated': return 'violated';
    case 'not_applicable': return 'does not apply here';
    default: return 'not checked';
  }
}

/** How the finding was produced: the method's name and how far it can be trusted. */
export function tierLabel(tier: string): string {
  switch (str(tier).trim()) {
    case 'hash': return 'byte identity';
    case 'parser': return 'parsed structure';
    case 'algorithm': return 'algorithm';
    case 'perceptual': return 'perceptual metric';
    case 'model': return 'a model';
    case 'human': return 'a person';
    default: return 'an unnamed method';
  }
}

/* -- the API ------------------------------------------------------------- */

const BASE = '/api/deltas';

/**
 * A read, with the refusal convention applied.
 *
 * A rejected read is a 200 with `ok: false`, so a caller that only checked the
 * HTTP status would draw an empty list and call it "no comparisons" -- the one
 * answer this screen is not allowed to give by accident.
 */
async function read<T>(url: string, signal?: AbortSignal): Promise<T> {
  const payload = await getJson<unknown>(url, signal);
  const refusal = refusalOf(payload);
  if (refusal) throw new DeltaRefusal(refusal);
  return payload as T;
}

async function send<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: body === undefined
      ? { Accept: 'application/json' }
      : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => ({}))) as unknown;
  const refusal = refusalOf(payload);
  if (refusal) throw new DeltaRefusal(refusal);
  if (!response.ok) {
    // 4xx is a malformed body and 404 is somebody else's id; a validation
    // rejection never gets here, because it is a 200 the block above caught.
    const detail = str(obj(payload).detail) || `${url} responded ${response.status}`;
    throw new DeltaRefusal({
      code: response.status === 404 ? 'not_found' : 'refused', message: detail, path: '',
    });
  }
  return payload as T;
}

export interface DeltaQuery {
  domain?: string;
  projectId?: string;
  assessment?: string;
  limit?: number;
  cursor?: string;
}

/**
 * The listing.
 *
 * `enabled` comes back on every page and is carried through rather than
 * dropped: it is what the screen needs to say "new comparisons are switched
 * off" while still drawing every delta already stored, which is literally what
 * the server does.
 */
export async function loadDeltas(query: DeltaQuery = {}, signal?: AbortSignal): Promise<DeltaPage> {
  const params = new URLSearchParams();
  if (query.domain) params.set('domain', query.domain);
  if (query.projectId) params.set('project_id', query.projectId);
  if (query.assessment) params.set('assessment', query.assessment);
  if (query.limit) params.set('limit', String(query.limit));
  if (query.cursor) params.set('cursor', query.cursor);
  const suffix = params.toString();
  const payload = obj(await read<unknown>(`${BASE}${suffix ? `?${suffix}` : ''}`, signal));
  return {
    enabled: flag(payload.enabled),
    deltas: asArray<unknown>(payload.deltas).map(summaryFrom).filter((row) => row.id),
    nextCursor: str(payload.next_cursor),
  };
}

export async function loadConfig(signal?: AbortSignal): Promise<Config> {
  return configFrom(await read<unknown>(`${BASE}/config`, signal));
}

export async function loadExtractors(signal?: AbortSignal): Promise<ExtractorStatus[]> {
  return extractorsFrom(await read<unknown>(`${BASE}/extractors/status`, signal));
}

export async function loadDiagnostics(signal?: AbortSignal): Promise<Diagnostics> {
  return diagnosticsFrom(await read<unknown>(`${BASE}/diagnostics`, signal));
}

/**
 * One delta, with the sentence that explains it and the reclassifications
 * somebody has already made against it.
 *
 * `cached` is kept because it is the honest answer to "when was this measured":
 * a delta served from the §22 cache was computed against the same source,
 * target, intent and extractor versions, and the screen says so rather than
 * implying the work was redone just now.
 */
export async function loadDelta(id: string, signal?: AbortSignal): Promise<DeltaDetail> {
  const payload = obj(await read<unknown>(`${BASE}/${encodeURIComponent(str(id))}`, signal));
  return {
    delta: deltaFrom(payload.delta ?? payload),
    explanation: str(payload.explanation),
    reclassifications: asArray<unknown>(payload.reclassifications).map(obj),
    cached: flag(payload.cached),
  };
}

export async function loadEvidence(id: string, signal?: AbortSignal): Promise<EvidenceRef[]> {
  const payload = await read<unknown>(`${BASE}/${encodeURIComponent(str(id))}/evidence`, signal);
  return asArray<unknown>(payload, 'evidence').map(evidenceFrom);
}

export interface IntentRequest {
  domain: string;
  text?: string;
  requested?: unknown[];
  invariants?: unknown[];
  scope?: unknown;
  tolerances?: unknown;
  acceptance?: string[];
  evaluationProfile?: string;
  projectId?: string;
}

/**
 * Turn a sentence into a contract that can be checked, WITHOUT running
 * anything.
 *
 * Separate from creating the delta on purpose: the compiler's `unknowns` are
 * the fragments it could not turn into a checkable condition, and a person has
 * to be able to read them before the comparison is frozen against them. A
 * contract with unknowns still runs; what it cannot do is come back `matched`.
 */
export async function compileIntent(request: IntentRequest): Promise<IntentCompilation> {
  const body: Record<string, unknown> = { domain: request.domain };
  if (request.text) body.text = request.text;
  if (request.requested) body.requested = request.requested;
  if (request.invariants) body.invariants = request.invariants;
  if (request.scope) body.scope = request.scope;
  if (request.tolerances) body.tolerances = request.tolerances;
  if (request.acceptance) body.acceptance = request.acceptance;
  if (request.evaluationProfile) body.evaluation_profile = request.evaluationProfile;
  if (request.projectId) body.project_id = request.projectId;
  const payload = obj(await send<unknown>(`${BASE}/intent/compile`, body));
  return {
    intent: obj(payload.intent),
    fingerprint: str(payload.fingerprint),
    unknowns: strList(payload.unknowns),
  };
}

export interface CreateRequest {
  domain: string;
  source: { kind: string; ref: string; hash: string; label?: string };
  target: { kind: string; ref: string; hash: string; label?: string };
  intentContractId?: string;
  intentText?: string;
  requested?: unknown[];
  invariants?: unknown[];
  evaluationProfile?: string;
  projectId?: string;
  workspace?: string;
  /** Run it now, rather than recording the request and stopping. */
  run?: boolean;
}

/**
 * Ask for a comparison.
 *
 * A frozen contract id and raw intent material are mutually exclusive on the
 * server -- they are two different asks and picking one for the caller would
 * discard the other silently -- so only one of the two is ever sent.
 */
export async function createDelta(request: CreateRequest): Promise<CreatedDelta> {
  const body: Record<string, unknown> = {
    domain: request.domain,
    source: request.source,
    target: request.target,
  };
  if (request.intentContractId) {
    body.intent_contract_id = request.intentContractId;
  } else {
    if (request.intentText) body.intent_text = request.intentText;
    if (request.requested) body.requested = request.requested;
    if (request.invariants) body.invariants = request.invariants;
  }
  if (request.evaluationProfile) body.evaluation_profile = request.evaluationProfile;
  if (request.projectId) body.project_id = request.projectId;
  if (request.workspace) body.workspace = request.workspace;
  if (request.run !== undefined) body.run = request.run;
  const payload = obj(await send<unknown>(BASE, body));
  return {
    requestId: str(payload.request_id),
    intentContractId: str(payload.intent_contract_id),
    delta: payload.delta ? deltaFrom(payload.delta) : null,
  };
}

/** Run a comparison that was recorded but not run, or run one again. */
export async function runDelta(id: string): Promise<Delta> {
  const payload = obj(await send<unknown>(`${BASE}/${encodeURIComponent(str(id))}/run`));
  return deltaFrom(payload.delta ?? payload);
}

/**
 * A person disagreeing with the classifier, on the record.
 *
 * The reason is required by the route and required here: a reclassification
 * without one is an unexplained edit to the only record of what was decided,
 * and the audit trail keeps both the original and this.
 */
export async function reclassify(
  id: string, assertionId: string, classification: string, reason: string,
): Promise<Delta> {
  const payload = obj(await send<unknown>(`${BASE}/${encodeURIComponent(str(id))}/reclassify`, {
    assertion_id: assertionId, classification, reason,
  }));
  return deltaFrom(payload.delta ?? payload);
}

/** Mark a delta as no longer describing its two revisions. */
export async function invalidate(id: string, reason = ''): Promise<boolean> {
  const payload = obj(await send<unknown>(
    `${BASE}/${encodeURIComponent(str(id))}/invalidate`, reason ? { reason } : {},
  ));
  return flag(payload.superseded);
}

/* -- the event stream ---------------------------------------------------- */

export interface DeltaEvent {
  name: string;
  deltaId: string;
  cursor: string;
  at: string;
  payload: Record<string, unknown>;
}

export function eventFrom(raw: unknown): DeltaEvent {
  const e = obj(raw);
  const payload = obj(e.payload);
  return {
    name: str(e.name) || str(e.event),
    deltaId: str(e.delta_id) || str(payload.delta_id),
    cursor: str(e.cursor) || str(e.id),
    at: str(e.at) || str(e.created_at),
    payload,
  };
}

/**
 * Where a reconnection resumes.
 *
 * The cursor is opaque here -- a string the server hands back -- so the only
 * rule this can enforce is the one that matters: it moves forward or it stays
 * where it was, and it is never blanked by a frame that carried none. A cursor
 * that went empty would ask the stream to replay from the beginning, and the
 * page would apply every event it has already applied.
 */
export function advanceCursor(cursor: string, events: DeltaEvent[] | null | undefined): string {
  let next = str(cursor);
  for (const event of events ?? []) {
    if (event && event.cursor) next = event.cursor;
  }
  return next;
}

/**
 * Follow the engine live.
 *
 * The frames are UNNAMED and carry the event name inside the JSON, which is
 * the dialect the rest of Faustus speaks: a NAMED SSE frame never reaches
 * `onmessage`, so a page written against the unnamed stream goes silently deaf
 * on a named one. The single named frame is `end`, and it is not a failure:
 * the stream has a deadline and is asking to be reopened from the cursor the
 * caller now holds.
 */
export function followDeltas(
  cursor: string,
  onEvent: (event: DeltaEvent) => void,
  onFail: () => void,
  onEnd?: () => void,
): () => void {
  if (typeof EventSource === 'undefined') {
    onFail();
    return () => {};
  }
  let source: EventSource | null = null;
  try {
    source = new EventSource(`${BASE}/events?cursor=${encodeURIComponent(str(cursor))}`);
  } catch {
    onFail();
    return () => {};
  }
  const close = (): void => {
    if (!source) return;
    try {
      source.close();
    } catch {
      /* already closed */
    }
    source = null;
  };
  source.onmessage = (frame: MessageEvent<string>) => {
    try {
      onEvent(eventFrom(JSON.parse(frame.data) as unknown));
    } catch {
      /* one unreadable frame is not a dead stream */
    }
  };
  source.addEventListener('end', () => {
    close();
    if (onEnd) onEnd();
    else onFail();
  });
  source.onerror = () => {
    close();
    onFail();
  };
  return close;
}
