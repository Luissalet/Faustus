import { asArray, getJson } from './api';

/**
 * Greedy Completion Engine (`/api/completion`), shaped for one screen.
 *
 * A completion decision answers "what did this turn do beyond the literal ask,
 * what did it refuse, and why did it stop". This layer exists so that the last
 * of those never gets rounded off on the way to a pixel, because a run that
 * ran out of money and a run that had nothing left worth doing look identical
 * once you stop printing the difference.
 *
 * Three rules of PRODUCT, not of style, and every one of them is a pure
 * function below rather than a line inside a component:
 *
 * **`converged` is never drawn as the same ending as `budget` or
 * `unfinished`.** `converged` and `core_only` are the honest stops -- the work
 * was actually done. `budget` means it ran out. `unfinished` means the turn
 * ended with work this mode calls for still open, and neither the budget nor
 * the scope stopped it. Three different next actions: accept, raise the
 * budget, go and find out why it stopped. `endingOf` keeps them apart, and it
 * refuses an honest stop that its OWN budget contradicts.
 *
 * **Shadow and real are never mixed silently.** Shadow mode measures what the
 * engine WOULD have done; counting those beside what it did ruins the
 * measurement it exists to produce. The list defaults to real, `mixesShadow`
 * reports when both are on screen, and every shadow row says so.
 *
 * **Executed, rejected and deferred are three lists and stay three lists.**
 * A refusal carries its reason from the closed vocabulary; a refusal that
 * arrived without one reads as `unrecorded` and is reported, never quietly
 * drawn as an ordinary refusal.
 *
 * The vocabularies below mirror `src/completion_engine/contracts.py`. They are
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

/** A `Record<string, number>`, dropping anything that is not one. */
function countMap(value: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [name, raw] of Object.entries(obj(value))) {
    if (typeof raw === 'number' && Number.isFinite(raw)) out[name] = raw;
  }
  return out;
}

/* -- the closed vocabularies, from src/completion_engine/contracts.py ----- */

/** The four modes, from least to most ambitious. `greedy` is the default. */
export const COMPLETION_MODES = ['literal', 'professional', 'greedy', 'maximalist'];

/**
 * The four layers, obligatory first. `core` is the request taken literally
 * plus whatever makes it TRUE; `professional` is what a professional would not
 * ship without; `bonus` is adjacent work with a real return; `exploratory` is
 * ambition, and only `maximalist` opens it.
 */
export const LAYERS = ['core', 'professional', 'bonus', 'exploratory'];

/** The layers whose failure is the run's failure. A failed bonus is not one. */
export const REQUIRED_LAYERS = ['core', 'professional'];

/** What was added BEYOND the ask. §30: never folded into the core summary. */
export const EXTRA_LAYERS = ['bonus', 'exploratory'];

/** Every way a run can end. Exactly one of these per decision. */
export const STOP_REASONS = [
  'converged', 'unfinished', 'budget', 'scope', 'risk', 'blocked',
  'user', 'cancelled', 'core_only', 'failed',
];

/**
 * The stops that mean "there was nothing more worth doing". Everything else
 * is an interruption and is reported as one.
 *
 * `unfinished` is deliberately NOT here, and it is the word shadow mode exists
 * to produce: a turn that ended with admitted, affordable work still on the
 * frontier neither converged nor ran out.
 */
export const HONEST_STOPS = ['converged', 'core_only'];

/** Why a candidate did not run. Closed, because a rejection nobody wrote down
 *  comes back next round and is rejected again, forever (§1.8). */
export const REJECTION_REASONS = [
  'out_of_scope', 'no_permission', 'forbidden_effect', 'below_threshold',
  'dominated', 'duplicate', 'resolved', 'quarantined', 'budget',
  'round_full', 'risk', 'stale', 'superseded',
  // The only one of the fourteen a machine may not write. The other thirteen
  // are verdicts this engine reached about a candidate; `declined` is a verdict
  // a PERSON reached about the engine, and the screen keeps them apart for the
  // same reason the contract does -- "we judged this not worth it" and "you
  // were told not to" are different facts about the same unrun improvement.
  'declined',
];

/** The rejection that came from a person rather than from the engine. */
export const HUMAN_REJECTION = 'declined';

/** Where a candidate stands. `deferred` is worth doing and not now. */
export const CANDIDATE_STATUSES = [
  'candidate', 'selected', 'executing', 'done', 'rejected', 'deferred', 'failed',
];

/** How far a candidate sits from what was asked. A structural judgement. */
export const RELATIONS = [
  'direct', 'adjacent', 'downstream', 'similar_case', 'opportunistic', 'unrelated',
];

/** How hard it would be to undo. Only `full` qualifies for §12's act-unasked. */
export const REVERSIBILITY = ['full', 'partial', 'none'];

/* -- the budget: five lines, three pots ---------------------------------- */

/** The lines a spend can be booked to. `verification` is never borrowed from. */
export const BUDGET_LINES = ['core', 'verification', 'bonus', 'exploration', 'recovery'];

/**
 * Which POT each line actually spends from. Only three of the five hold money.
 *
 * A line and a pot are different things and the difference is the whole reason
 * this is a map rather than prose: a CALLER wants to say "this spend was
 * recovery work", because that is what the closeout reports, while the ACCOUNT
 * has to know that recovery and core come out of the same money. Metering the
 * five apart made five ceilings sum to 185% of a total that was supposed to be
 * all of it (`contracts.py::FUNDING_LINES`).
 */
export const FUNDING_LINES: Record<string, string> = {
  core: 'core',
  verification: 'verification',
  bonus: 'bonus',
  exploration: 'bonus',
  recovery: 'core',
};

/** The three that hold money. Their ceilings sum to the total, exactly. */
export const FUNDED_LINES = ['core', 'verification', 'bonus'];

/** What is counted. Four and not one: they run out at different times, and a
 *  run out of rounds with tokens to spare must not be told to continue. */
export const SPENDABLE_UNITS = ['rounds', 'tool_calls', 'tokens', 'seconds'];

/**
 * Which budget line a candidate on each layer is funded from.
 *
 * `professional` has no line of its own, and that is a FACT about the engine
 * rather than a gap in this table: professional work is required work and it
 * spends the core pot. `exploratory` maps to `exploration`, which is a view of
 * the bonus pot -- `maximalist` explores by spending its larger bonus share
 * rather than out of money of its own. The screen prints both the line and the
 * pot, because a reader seeing `core` and `recovery` with identical numbers
 * and no note would reasonably conclude the run had two of them.
 *
 * A layer this build has never heard of funds from `exploration`, the most
 * optional line there is -- the same direction `contracts.py::layer_rank`
 * takes, and the one that cannot turn an unrecognised word into work that
 * blocks the close.
 */
export const LAYER_LINES: Record<string, string> = {
  core: 'core',
  professional: 'core',
  bonus: 'bonus',
  exploratory: 'exploration',
};

/* -- the refusal convention ---------------------------------------------- */

/**
 * A rejected request answers 200 with `{ok:false, error:{path,message,code}}`.
 * The token is what a caller branches on, the sentence is for the person
 * reading and the path is the field that was wrong. Branching on the sentence
 * is the habit this repository has already paid for once.
 */
export interface CompletionError {
  code: string;
  message: string;
  path: string;
}

export class CompletionRefusal extends Error {
  readonly code: string;
  readonly path: string;

  constructor(error: CompletionError) {
    super(error.message);
    this.name = 'CompletionRefusal';
    this.code = error.code;
    this.path = error.path;
  }
}

/** The refusal inside a 200 body, or null when the call succeeded. */
export function refusalOf(payload: unknown): CompletionError | null {
  const body = obj(payload);
  if (body.ok !== false) return null;
  const nested = obj(body.error);
  const code = str(nested.code).trim() || str(body.error).trim();
  const message = str(nested.message).trim() || str(body.detail).trim();
  return {
    code: code || 'refused',
    message: message || 'the completion engine refused this and did not say why',
    path: str(nested.path).trim(),
  };
}

/* -- the shapes, as the server sends them -------------------------------- */

/** What has been spent, per unit. NEVER a remainder -- a remainder is derived
 *  and goes wrong the moment two writers disagree about the total. */
export interface Spend {
  rounds: number;
  toolCalls: number;
  tokens: number;
  seconds: number;
}

export interface Budget {
  total: Spend;
  reserveShare: number;
  bonusShare: number;
  /** Keyed by BUDGET_LINE. What was SPENT against each, never what is left. */
  spent: Record<string, Spend>;
}

/**
 * One improvement the engine considered.
 *
 * `requiredPermissions` says what this would NEED and there is no field that
 * could grant one; `evidenceRefs` is what separates a candidate from a wish.
 */
export interface Candidate {
  id: string;
  title: string;
  layer: string;
  category: string;
  relation: string;
  source: string;
  expectedValue: number;
  estimatedCost: number;
  risk: number;
  confidence: number;
  reversibility: string;
  status: string;
  /** From `REJECTION_REASONS`, or `""` when nobody wrote one down. */
  rejectionReason: string;
  detail: string;
  resources: string[];
  requiredPermissions: string[];
  requiredEffects: string[];
  dependencies: string[];
  verification: string[];
  evidenceRefs: string[];
  dedupeKey: string;
  createdAt: string;
}

/** One turn's decision, whole. */
export interface Decision {
  id: string;
  contractId: string;
  scopeEnvelopeId: string;
  mode: string;
  policyVersion: string;
  completedLayers: string[];
  stopReason: string;
  stopDetail: string;
  executed: Candidate[];
  rejected: Candidate[];
  deferred: Candidate[];
  budget: Budget;
  proofRefs: string[];
  deltaRefs: string[];
  changesetRefs: string[];
  /** Systems that were switched off when this ran. A frontier computed without
   *  the Delta Engine could not see scope creep, and saying so is the point. */
  degradedIntegrations: string[];
  shadow: boolean;
  owner: string;
  projectId: string;
  runId: string;
  sessionId: string;
  correlationId: string;
  createdAt: string;
  schemaVersion: number;
}

/**
 * One row in the listing: `CompletionDecision.summary()` plus what it takes to
 * open and filter it. The counts arrive computed, because a listing that
 * shipped every candidate to draw a card would be a megabyte for a sidebar.
 */
export interface Summary {
  id: string;
  mode: string;
  layers: string[];
  /** Executed count per layer, so extras are visible without opening the row. */
  executed: Record<string, number>;
  extras: number;
  rejected: number;
  deferred: number;
  rejectionReasons: Record<string, number>;
  stopReason: string;
  /** What the SERVER claimed. `honestStop()` is what the screen believes. */
  honestStop: boolean;
  degraded: string[];
  shadow: boolean;
  runId: string;
  projectId: string;
  createdAt: string;
}

export interface DecisionPage {
  enabled: boolean;
  shadowEnabled: boolean;
  decisions: Summary[];
  nextCursor: string;
}

export interface DecisionDetail {
  decision: Decision;
  /** The closeout, rendered by the server. Read, never re-derived here. */
  closeout: string;
  summary: Summary;
}

/** The two switches and the two numbers, read live. */
export interface Settings {
  enabled: boolean;
  shadowEnabled: boolean;
  verificationReserve: number;
  maxBonusRounds: number;
}

export interface Policy {
  policyVersion: string;
  description: string;
  exploreFrontier: boolean;
  bonusBudgetShare: number;
  stopOnCoreProved: boolean;
  maxExtraLayers: number;
  requiresVerification: boolean;
}

export interface Modes {
  enabled: boolean;
  shadowEnabled: boolean;
  modes: string[];
  layers: string[];
  policies: Record<string, Policy>;
}

/** The closed vocabularies as THIS BUILD OF THE SERVER has them. */
export interface Config {
  enabled: boolean;
  shadowEnabled: boolean;
  modes: string[];
  layers: string[];
  relations: string[];
  stopReasons: string[];
  rejectionReasons: string[];
  categories: string[];
  sources: string[];
  budgetLines: string[];
  units: string[];
  events: string[];
  policies: Record<string, Policy>;
}

export interface Diagnostics {
  enabled: boolean;
  shadowEnabled: boolean;
  counts: Record<string, number>;
  rejections: Record<string, number>;
  degraded: string[];
  dbPath: string;
  schemas: string[];
}

/* -- readers ------------------------------------------------------------- */

export function spendFrom(raw: unknown): Spend {
  const s = obj(raw);
  return {
    rounds: num(s.rounds),
    toolCalls: num(s.tool_calls),
    tokens: num(s.tokens),
    seconds: num(s.seconds),
  };
}

export function budgetFrom(raw: unknown): Budget {
  const b = obj(raw);
  const spent: Record<string, Spend> = {};
  for (const [line, value] of Object.entries(obj(b.spent))) spent[line] = spendFrom(value);
  return {
    total: spendFrom(b.total),
    reserveShare: num(b.reserve_share),
    bonusShare: num(b.bonus_share),
    spent,
  };
}

/**
 * One candidate.
 *
 * Every defaulted field defaults DOWNWARDS: a missing layer is `exploratory`
 * (the most optional), a missing status is `candidate`, and a missing
 * rejection reason stays EMPTY rather than being filled with a plausible one.
 * An invented reason is the thing §1.8 exists to prevent, one layer up.
 */
export function candidateFrom(raw: unknown): Candidate {
  const c = obj(raw);
  return {
    id: str(c.id),
    title: str(c.title),
    layer: str(c.layer) || 'exploratory',
    category: str(c.category),
    relation: str(c.relation) || 'unrelated',
    source: str(c.source),
    expectedValue: num(c.expected_value),
    estimatedCost: num(c.estimated_cost),
    risk: num(c.risk),
    confidence: num(c.confidence),
    reversibility: str(c.reversibility) || 'none',
    status: str(c.status) || 'candidate',
    rejectionReason: str(c.rejection_reason),
    detail: str(c.detail),
    resources: strList(c.resources),
    requiredPermissions: strList(c.required_permissions),
    requiredEffects: strList(c.required_effects),
    dependencies: strList(c.dependencies),
    verification: strList(c.verification),
    evidenceRefs: strList(c.evidence_refs),
    dedupeKey: str(c.dedupe_key),
    createdAt: str(c.created_at),
  };
}

/**
 * One decision.
 *
 * A missing `stop_reason` reads as `unfinished` and NOT as `converged`. That
 * is the whole shape of this file in one default: a record that never said how
 * it ended did not tell us the work was done.
 */
export function decisionFrom(raw: unknown): Decision {
  const d = obj(raw);
  return {
    id: str(d.id),
    contractId: str(d.contract_id),
    scopeEnvelopeId: str(d.scope_envelope_id),
    mode: str(d.mode),
    policyVersion: str(d.policy_version),
    completedLayers: strList(d.completed_layers),
    stopReason: str(d.stop_reason) || 'unfinished',
    stopDetail: str(d.stop_detail),
    executed: asArray<unknown>(d.executed).map(candidateFrom),
    rejected: asArray<unknown>(d.rejected).map(candidateFrom),
    deferred: asArray<unknown>(d.deferred).map(candidateFrom),
    budget: budgetFrom(d.budget),
    proofRefs: strList(d.proof_refs),
    deltaRefs: strList(d.delta_refs),
    changesetRefs: strList(d.changeset_refs),
    degradedIntegrations: strList(d.degraded_integrations),
    shadow: flag(d.shadow),
    owner: str(d.owner),
    projectId: str(d.project_id),
    runId: str(d.run_id),
    sessionId: str(d.session_id),
    correlationId: str(d.correlation_id),
    createdAt: str(d.created_at),
    schemaVersion: num(d.schema_version),
  };
}

/**
 * A listing row.
 *
 * `shadow` reads as `false` only when the field says so, and `honestStop` is
 * carried through as what the SERVER claimed rather than as a fact -- the
 * screen believes `honestStop()`, which reads the stop reason off the closed
 * vocabulary instead.
 */
export function summaryFrom(raw: unknown): Summary {
  const s = obj(raw);
  return {
    id: str(s.id),
    mode: str(s.mode),
    layers: strList(s.layers),
    executed: countMap(s.executed),
    extras: num(s.extras),
    rejected: num(s.rejected),
    deferred: num(s.deferred),
    rejectionReasons: countMap(s.rejection_reasons),
    stopReason: str(s.stop_reason) || 'unfinished',
    honestStop: flag(s.honest_stop),
    degraded: strList(s.degraded),
    shadow: flag(s.shadow),
    runId: str(s.run_id),
    projectId: str(s.project_id),
    createdAt: str(s.created_at),
  };
}

export function policyFrom(raw: unknown): Policy {
  const p = obj(raw);
  return {
    policyVersion: str(p.policy_version),
    description: str(p.description),
    exploreFrontier: flag(p.explore_frontier),
    bonusBudgetShare: num(p.bonus_budget_share),
    stopOnCoreProved: flag(p.stop_on_core_proved),
    maxExtraLayers: num(p.max_extra_layers),
    requiresVerification: flag(p.requires_verification),
  };
}

function policiesFrom(raw: unknown): Record<string, Policy> {
  const out: Record<string, Policy> = {};
  for (const [mode, value] of Object.entries(obj(raw))) out[mode] = policyFrom(value);
  return out;
}

export function modesFrom(raw: unknown): Modes {
  const m = obj(raw);
  const modes = strList(m.modes);
  const layers = strList(m.layers);
  return {
    enabled: flag(m.enabled),
    shadowEnabled: flag(m.shadow_enabled),
    modes: modes.length ? modes : COMPLETION_MODES,
    layers: layers.length ? layers : LAYERS,
    policies: policiesFrom(m.policies),
  };
}

/**
 * The server's own vocabularies.
 *
 * Read from the server rather than assumed, so a build whose engine knows a
 * stop reason this bundle has never heard of still offers it in the filters;
 * the constants above are the fallback for a server that answered nothing. A
 * list that came back EMPTY falls back too, because an empty filter hides
 * every row rather than showing all of them.
 */
export function configFrom(raw: unknown): Config {
  const c = obj(raw);
  const list = (key: string, fallback: string[]): string[] => {
    const values = strList(c[key]);
    return values.length ? values : fallback;
  };
  return {
    enabled: flag(c.enabled),
    shadowEnabled: flag(c.shadow_enabled),
    modes: list('modes', COMPLETION_MODES),
    layers: list('layers', LAYERS),
    relations: list('relations', RELATIONS),
    stopReasons: list('stop_reasons', STOP_REASONS),
    rejectionReasons: list('rejection_reasons', REJECTION_REASONS),
    categories: list('categories', []),
    sources: list('sources', []),
    budgetLines: list('budget_lines', BUDGET_LINES),
    units: list('units', SPENDABLE_UNITS),
    events: list('events', []),
    policies: policiesFrom(c.policies),
  };
}

export function settingsFrom(raw: unknown): Settings {
  const s = obj(raw);
  return {
    enabled: flag(s.enabled),
    shadowEnabled: flag(s.shadow_enabled),
    verificationReserve: num(s.verification_reserve),
    maxBonusRounds: num(s.max_bonus_rounds),
  };
}

export function diagnosticsFrom(raw: unknown): Diagnostics {
  const d = obj(raw);
  return {
    enabled: flag(d.enabled),
    shadowEnabled: flag(d.shadow_enabled),
    counts: countMap(d.counts),
    rejections: countMap(d.rejections),
    degraded: strList(d.degraded),
    dbPath: str(d.db_path),
    schemas: strList(d.schemas),
  };
}

/* -- the budget arithmetic, mirrored from CompletionBudget --------------- */

const round3 = (value: number): number => Math.round(value * 1000) / 1000;

/** One unit off a spend. An unknown unit is 0, never NaN. */
export function spendOf(spend: Spend | null | undefined, unit: string): number {
  switch (str(unit).trim()) {
    case 'rounds': return num(spend?.rounds);
    case 'tool_calls': return num(spend?.toolCalls);
    case 'tokens': return num(spend?.tokens);
    case 'seconds': return num(spend?.seconds);
    default: return 0;
  }
}

/** The pot a line spends from. An unknown line is returned untouched, exactly
 *  as `budgeting.funding_line` does, so the value shows up in the message. */
export function fundingPot(line: string): string {
  const name = str(line).trim();
  return FUNDING_LINES[name] ?? name;
}

/** Whether a line is a VIEW of another line's pot rather than a pot of its own. */
export function isAlias(line: string): boolean {
  const name = str(line).trim();
  return Boolean(FUNDING_LINES[name]) && FUNDING_LINES[name] !== name;
}

/**
 * How much of `unit` the pot behind `line` holds, out of the total.
 *
 * Three pots and their ceilings add up to exactly the total: the reserve, the
 * bonus share, and core with whatever is left.
 */
export function ceiling(budget: Budget | null | undefined, line: string, unit: string): number {
  if (!budget) return 0;
  const pot = fundingPot(line);
  const total = spendOf(budget.total, unit);
  if (total <= 0) return 0;
  const clamp = (value: number): number => Math.max(0, Math.min(1, num(value)));
  const reserve = total * clamp(budget.reserveShare);
  const bonus = total * clamp(budget.bonusShare);
  if (pot === 'verification') return round3(reserve);
  if (pot === 'bonus') return round3(bonus);
  return round3(Math.max(0, total - reserve - bonus));
}

/**
 * What the pot behind `line` has spent -- every line that draws on it.
 *
 * Summing across the aliases rather than reading one key is the whole point:
 * a spend booked to `recovery` and one booked to `core` come out of one pot,
 * so a reader of either has to see both.
 */
export function used(budget: Budget | null | undefined, line: string, unit: string): number {
  if (!budget) return 0;
  const pot = fundingPot(line);
  let total = 0;
  for (const [name, spend] of Object.entries(budget.spent ?? {})) {
    if (fundingPot(name) === pot) total += spendOf(spend, unit);
  }
  return round3(total);
}

export function remaining(budget: Budget | null | undefined, line: string, unit: string): number {
  return round3(ceiling(budget, line, unit) - used(budget, line, unit));
}

/**
 * Which units of a line have been spent to zero. Empty means room.
 *
 * A line whose CEILING is zero is not exhausted -- it was never opened, and
 * the distinction is not pedantry. `literal` runs with `bonus_share = 0`, so
 * without this every literal run would report its bonus line as spent, and the
 * server would then refuse `core_only` -- the one honest stop a literal run
 * can have -- on the grounds that it ran out of a budget it was never given.
 * Ran out and never started are different facts.
 */
export function exhaustedUnits(budget: Budget | null | undefined, line: string): string[] {
  const out: string[] = [];
  if (!budget) return out;
  for (const unit of SPENDABLE_UNITS) {
    if (spendOf(budget.total, unit) <= 0) continue;
    if (ceiling(budget, line, unit) <= 0) continue;
    if (remaining(budget, line, unit) <= 0) out.push(unit);
  }
  return out;
}

/** Every line with no room left, in `BUDGET_LINES` order. Resolved through the
 *  pot, so `exploration` is exhausted exactly when `bonus` is. */
export function exhaustedLines(budget: Budget | null | undefined): string[] {
  return BUDGET_LINES.filter((line) => exhaustedUnits(budget, line).length > 0);
}

/** Whether any pot that HAD a ceiling has run out. Over the three funded pots
 *  and not the five lines: asking a pot twice would answer identically. */
export function anyExhausted(budget: Budget | null | undefined): boolean {
  return FUNDED_LINES.some((line) => exhaustedUnits(budget, line).length > 0);
}

export interface BudgetUnitRow {
  unit: string;
  ceiling: number;
  used: number;
  remaining: number;
  /** False when the turn declared no total for this unit. Not the same as zero:
   *  the unit is not being counted at all, so nothing can run out of it. */
  counted: boolean;
}

export interface BudgetRow {
  line: string;
  fundedBy: string;
  alias: boolean;
  units: BudgetUnitRow[];
  exhausted: string[];
}

/**
 * The budget as rows, spends and ceilings side by side and never a bare
 * remainder: a remainder is only meaningful next to the limit it came from,
 * and two numbers that must be read together end up read apart.
 */
export function budgetRows(budget: Budget | null | undefined): BudgetRow[] {
  return BUDGET_LINES.map((line) => ({
    line,
    fundedBy: fundingPot(line),
    alias: isAlias(line),
    units: SPENDABLE_UNITS.map((unit) => ({
      unit,
      ceiling: ceiling(budget, line, unit),
      used: used(budget, line, unit),
      remaining: remaining(budget, line, unit),
      counted: spendOf(budget?.total, unit) > 0,
    })),
    exhausted: exhaustedUnits(budget, line),
  }));
}

/* -- rule one: three endings, and they are never each other -------------- */

/**
 * What KIND of ending a run had. The distinction the whole screen exists for.
 *
 * - `finished`: `converged` or `core_only`. There was nothing more worth doing
 *   and the work was actually done. Accept it.
 * - `budget`: a line ran out. The work was worth doing and there was no money.
 *   Raise the budget.
 * - `unfinished`: the turn ended with work this mode calls for still open, and
 *   neither the budget nor the scope stopped it. Go and find out why.
 * - `interrupted`: something outside the engine ended it -- scope, risk, a
 *   block, a person, a cancellation, a core that failed.
 * - `contested`: the record CLAIMS an honest stop and its own budget says a
 *   line ran out. Both cannot be true, and this screen says so rather than
 *   picking the friendlier half.
 *
 * Three of these five lead to three different next actions and one of them
 * leads to a question. A screen that drew any two of them alike would undo the
 * contract the engine is built on.
 */
export type Ending = 'finished' | 'budget' | 'unfinished' | 'interrupted' | 'contested';

/**
 * The ending a stop reason implies, on its own.
 *
 * A stop reason this build has never heard of is `interrupted`, never
 * `finished`: an unrecognised word must not be able to report itself as work
 * that was completed, because that is the reading nobody ever goes back and
 * checks.
 */
export function endingForStop(stopReason: string): Ending {
  const name = str(stopReason).trim();
  if (HONEST_STOPS.indexOf(name) >= 0) return 'finished';
  if (name === 'budget') return 'budget';
  if (name === 'unfinished') return 'unfinished';
  return 'interrupted';
}

/**
 * The ending this record may actually be DRAWN as.
 *
 * `converged` beside an exhausted budget line is a contradiction the server
 * refuses to build (`CompletionDecision.parse`). This is what happens when one
 * reaches the screen anyway -- from another build, a fixture, a migration, a
 * hand-written row: it is drawn as `contested`, which claims neither half, and
 * `contestedStops` lets the screen say WHY rather than correcting it silently.
 *
 * It never reads UP. A `budget` stop whose budget looks fine is still `budget`:
 * the record's own word about how it ended is the one thing this function is
 * not entitled to overrule in the direction of good news.
 */
export function endingOf(decision: Decision | null | undefined): Ending {
  if (!decision) return 'interrupted';
  const ending = endingForStop(decision.stopReason);
  if (ending === 'finished' && anyExhausted(decision.budget)) return 'contested';
  return ending;
}

export type Tone = 'good' | 'warn' | 'bad' | 'unknown';

/**
 * An ending, as a tone. Five endings, four tones, and no two of the three that
 * matter share one.
 *
 * `budget` is `warn` and `unfinished` is `bad`, which is the way round it
 * looks wrong until you ask what each one asks of the reader: running out of
 * money is an ordinary, fixable fact with an obvious next step, while a turn
 * that stopped with affordable, admitted work still open is the failure the
 * shadow mode was built to count. `interrupted` is `unknown` -- something
 * outside the engine decided, and neither good nor bad is the honest colour
 * for that.
 */
export function endingTone(ending: Ending): Tone {
  switch (ending) {
    case 'finished': return 'good';
    case 'budget': return 'warn';
    case 'unfinished': return 'bad';
    case 'contested': return 'bad';
    default: return 'unknown';
  }
}

/** The tone for a stop reason read on its own. */
export function stopTone(stopReason: string): Tone {
  return endingTone(endingForStop(stopReason));
}

/**
 * Whether the run stopped because there was nothing more worth doing.
 *
 * Computed from the closed vocabulary AND from the budget, never taken from
 * the row's own `honest_stop` field: a stored boolean is a claim, and this is
 * the one claim the whole subsystem is about.
 */
export function honestStop(decision: Decision | null | undefined): boolean {
  return endingOf(decision) === 'finished';
}

/**
 * Whether a listing row's own `honest_stop` flag disagrees with its stop
 * reason.
 *
 * Reported rather than only overruled. A silent correction is still a
 * correction the reader cannot audit, and a row whose two fields contradict
 * each other is worth knowing about for its own sake -- it means something
 * wrote that row without going through `CompletionDecision`.
 */
export function disputedHonesty(row: Summary | null | undefined): boolean {
  if (!row) return false;
  return row.honestStop !== (endingForStop(row.stopReason) === 'finished');
}

/** The decisions that claimed an honest stop beside an exhausted budget line. */
export function contestedStops(decisions: Decision[] | null | undefined): Decision[] {
  return (decisions ?? []).filter((decision) => endingOf(decision) === 'contested');
}

/* -- rule two: shadow and real are never mixed silently ------------------ */

/**
 * Whether both kinds are on screen at once.
 *
 * Shadow mode is a measurement of what the engine WOULD have done. Counting
 * those beside what it did ruins the measurement it exists to produce, so the
 * list defaults to real and this is what makes the mixed view announce itself.
 */
export function mixesShadow(rows: Summary[] | null | undefined): boolean {
  const list = rows ?? [];
  return list.some((row) => row.shadow) && list.some((row) => !row.shadow);
}

/** The two kinds, apart. Neither list is ever folded into the other. */
export function partitionShadow(rows: Summary[] | null | undefined): {
  real: Summary[];
  shadow: Summary[];
} {
  const list = rows ?? [];
  return {
    real: list.filter((row) => !row.shadow),
    shadow: list.filter((row) => row.shadow),
  };
}

/* -- rule three: executed, rejected and deferred stay three lists -------- */

/** Position in `LAYERS`; an unknown layer ranks LAST, which is the most
 *  optional -- the direction that cannot promote an unrecognised word into
 *  work that blocks the close. */
export function layerRank(layer: string): number {
  const at = LAYERS.indexOf(str(layer).trim());
  return at < 0 ? LAYERS.length - 1 : at;
}

/** The budget line a candidate on this layer is funded from. */
export function lineForLayer(layer: string): string {
  return LAYER_LINES[str(layer).trim()] ?? 'exploration';
}

/** The pot behind that line. `professional` and `core` share one; so do
 *  `exploratory` and `bonus`, and the screen prints both halves. */
export function potForLayer(layer: string): string {
  return fundingPot(lineForLayer(layer));
}

/** Candidates, obligatory first, then by what they were worth, then by title.
 *  Stable, and it never re-orders the caller's own array. */
export function orderedCandidates(rows: Candidate[] | null | undefined): Candidate[] {
  return (rows ?? []).filter(Boolean).slice().sort((left, right) => {
    const byLayer = layerRank(left.layer) - layerRank(right.layer);
    if (byLayer !== 0) return byLayer;
    const byValue = num(right.expectedValue) - num(left.expectedValue);
    if (byValue !== 0) return byValue;
    return str(left.title).localeCompare(str(right.title));
  });
}

/** The sentinel for a refusal nobody wrote a reason for. Deliberately NOT one
 *  of `REJECTION_REASONS`: it is the absence of a word, not a word. */
export const UNRECORDED_REASON = 'unrecorded';

/**
 * The rejection reason this row may actually be DRAWN under.
 *
 * A word this build has never heard of, and a missing word, both read as
 * `unrecorded`. The alternative -- printing the raw token, or picking the
 * nearest known reason -- would let a refusal nobody justified pass, a month
 * later, for one somebody meant. That sentence is the reason the write on this
 * screen demands a typed reason before it will submit.
 */
export function effectiveRejection(candidate: Candidate | null | undefined): string {
  const reason = str(candidate?.rejectionReason).trim();
  return REJECTION_REASONS.indexOf(reason) >= 0 ? reason : UNRECORDED_REASON;
}

/**
 * The REJECTED candidates that arrived without a reason from the vocabulary.
 *
 * `rejected` only. A `deferred` row is allowed to have none -- deferring is a
 * decision to decide later and the engine files those with `round_full` or
 * `below_threshold` when it has one -- but `ImprovementCandidate.parse`
 * refuses to build a rejected row without a reason, so one here means the row
 * did not come through the contract.
 */
export function unexplainedRefusals(decision: Decision | null | undefined): Candidate[] {
  return (decision?.rejected ?? []).filter(
    (candidate) => effectiveRejection(candidate) === UNRECORDED_REASON,
  );
}

/** Every candidate that ran on a layer beyond the ask. §30: never hidden
 *  inside the core summary, so this is a function and not a filter inline. */
export function extras(decision: Decision | null | undefined): Candidate[] {
  return (decision?.executed ?? []).filter(
    (candidate) => EXTRA_LAYERS.indexOf(str(candidate.layer).trim()) >= 0,
  );
}

/** What ran on one layer. */
export function byLayer(decision: Decision | null | undefined, layer: string): Candidate[] {
  return orderedCandidates(
    (decision?.executed ?? []).filter((candidate) => str(candidate.layer).trim() === str(layer).trim()),
  );
}

/**
 * Where one candidate now stands in a decision, or `""` when it is not in it.
 *
 * Used wherever the screen needs to say where a candidate stands rather than
 * assume it. Note what it does NOT do: it is not how the screen confirms a
 * refusal. A refusal is stored in its own table, keyed by the candidate's
 * structural key, and the decision is deliberately left exactly as the turn
 * produced it -- so after a successful refusal this still reports `deferred`,
 * and that is correct. The record of an improvement having been OFFERED and
 * turned down is the interesting one a month later, and rewriting the decision
 * would have destroyed half of it. `RejectedImprovement.stored` is what
 * confirms the write.
 */
export function statusOf(decision: Decision | null | undefined, candidateId: string): string {
  const wanted = str(candidateId).trim();
  if (!wanted) return '';
  const all = [
    ...(decision?.executed ?? []), ...(decision?.rejected ?? []), ...(decision?.deferred ?? []),
  ];
  for (const row of all) if (row && row.id === wanted) return str(row.status);
  return '';
}

/** How many of each rejection reason, `unrecorded` amongst them. */
export function rejectionTally(decision: Decision | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const candidate of decision?.rejected ?? []) {
    const reason = effectiveRejection(candidate);
    out[reason] = (out[reason] ?? 0) + 1;
  }
  return out;
}

/* -- counts, proof and summaries ----------------------------------------- */

export interface DecisionCounts {
  executed: number;
  extras: number;
  rejected: number;
  deferred: number;
  /** Refusals with no reason from the vocabulary. Never folded into `rejected`. */
  unexplained: number;
  /** Executed count per layer, all four returned, zeros included. */
  perLayer: Record<string, number>;
  degraded: number;
  exhausted: string[];
}

/** Every count a detail view draws. Computed here, stored nowhere. */
export function counts(decision: Decision | null | undefined): DecisionCounts {
  const perLayer: Record<string, number> = {};
  for (const layer of LAYERS) {
    perLayer[layer] = (decision?.executed ?? []).filter(
      (candidate) => str(candidate.layer).trim() === layer,
    ).length;
  }
  return {
    executed: (decision?.executed ?? []).length,
    extras: extras(decision).length,
    rejected: (decision?.rejected ?? []).length,
    deferred: (decision?.deferred ?? []).length,
    unexplained: unexplainedRefusals(decision).length,
    perLayer,
    degraded: (decision?.degradedIntegrations ?? []).length,
    exhausted: exhaustedLines(decision?.budget),
  };
}

/**
 * What the record says about proof, and nothing more.
 *
 * `referenced` is NOT `proved`. A run whose `proof_refs` are non-empty has
 * pointed at a proof somebody else holds; it has not shown a verdict, and this
 * screen holds none -- `closeout.py::_proof_text` is where a verdict is read,
 * and it comes to us already rendered inside the closeout text. Upgrading "has
 * references" to "proved" here would be the screen inventing the one word the
 * whole engine is careful about.
 */
export type ProofStatus = 'referenced' | 'none';

export function proofStatus(decision: Decision | null | undefined): ProofStatus {
  return (decision?.proofRefs ?? []).length > 0 ? 'referenced' : 'none';
}

/**
 * A whole decision read down to a listing row, exactly as the server
 * summarises it.
 *
 * So that a card built from a full decision and a card built from a listing
 * row cannot say different things about the same run. `honestStop` is set from
 * the closed vocabulary here, which is what the server's `summary()` does too.
 */
export function summarize(decision: Decision): Summary {
  const c = counts(decision);
  return {
    id: decision.id,
    mode: decision.mode,
    layers: decision.completedLayers.slice(),
    executed: c.perLayer,
    extras: c.extras,
    rejected: c.rejected,
    deferred: c.deferred,
    rejectionReasons: rejectionTally(decision),
    stopReason: decision.stopReason,
    honestStop: HONEST_STOPS.indexOf(str(decision.stopReason).trim()) >= 0,
    degraded: decision.degradedIntegrations.slice(),
    shadow: decision.shadow,
    runId: decision.runId,
    projectId: decision.projectId,
    createdAt: decision.createdAt,
  };
}

/* -- the screen's own invariant, made checkable -------------------------- */

/**
 * Does this decision, AS THIS SCREEN WILL DRAW IT, keep the three endings
 * apart?
 *
 * Computed over the derived ending and never over the raw `stop_reason`, so it
 * is a guard on the derivation rather than on the payload: if `endingOf` ever
 * loses its contest rule, or `endingForStop` starts reading an unknown word as
 * `finished`, this returns false and `studio/checks/completion.check.mjs`
 * fails -- which is the only way a rule written in a docstring survives the
 * third person to edit the file.
 *
 * Driven with hostile input on purpose: a decision claiming `converged` with a
 * dry budget line must still answer true here, because the screen defends
 * itself against it. What must never happen is such a run being drawn as
 * finished, and that is what these four clauses say.
 */
export function endingsAreNeverConflated(decision: Decision | null | undefined): boolean {
  if (!decision) return true;
  const ending = endingOf(decision);

  // 1. Nothing drawn as finished may have a pot that ran out.
  if (ending === 'finished' && anyExhausted(decision.budget)) return false;
  // 2. Nothing drawn as finished may carry a stop reason outside HONEST_STOPS.
  if (ending === 'finished' && HONEST_STOPS.indexOf(str(decision.stopReason).trim()) < 0) return false;
  // 3. `budget` and `unfinished` are never the same ending and never the same
  //    tone: one says raise the ceiling, the other says find out what stopped.
  if (endingForStop('budget') === endingForStop('unfinished')) return false;
  if (endingTone('budget') === endingTone('unfinished')) return false;
  // 4. And neither of them is ever the ending an honest stop gets.
  for (const reason of HONEST_STOPS) {
    if (endingForStop(reason) === endingForStop('budget')) return false;
    if (endingForStop(reason) === endingForStop('unfinished')) return false;
  }
  return true;
}

/**
 * Does this decision keep every refusal accountable?
 *
 * Every rejected candidate is drawn under a reason -- one from the closed
 * vocabulary, or the `unrecorded` sentinel -- and none is lost between the
 * three lists. The tally counts exactly the rejected rows and no others.
 */
export function refusalsAreAlwaysExplained(decision: Decision | null | undefined): boolean {
  const rejected = decision?.rejected ?? [];
  const tally = rejectionTally(decision);
  let counted = 0;
  for (const value of Object.values(tally)) counted += value;
  if (counted !== rejected.length) return false;
  for (const candidate of rejected) {
    const reason = effectiveRejection(candidate);
    if (reason !== UNRECORDED_REASON && REJECTION_REASONS.indexOf(reason) < 0) return false;
  }
  // A row cannot be executed and refused at the same time.
  const executedIds = new Set((decision?.executed ?? []).map((c) => c.id).filter(Boolean));
  for (const candidate of rejected) if (candidate.id && executedIds.has(candidate.id)) return false;
  for (const candidate of decision?.deferred ?? []) {
    if (candidate.id && executedIds.has(candidate.id)) return false;
  }
  return true;
}

/* -- words: keys `t()` translates, never a colour on its own ------------- */

/** How the run ended, in a sentence a person understands. The raw enum never
 *  appears alone on the screen, because `core_only` is not English. */
export function stopLabel(stopReason: string): string {
  switch (str(stopReason).trim()) {
    case 'converged': return 'finished: nothing left was worth doing';
    case 'core_only': return 'finished the ask, and this mode goes no further';
    case 'budget': return 'ran out of budget';
    case 'unfinished': return 'ended with work still open';
    case 'scope': return 'the next useful thing was out of scope';
    case 'risk': return 'a blocking risk stopped it';
    case 'blocked': return 'blocked on something outside this run';
    case 'user': return 'a person stopped it';
    case 'cancelled': return 'the run was cancelled';
    case 'failed': return 'the core did not complete';
    default: return 'ended for a reason this build cannot name';
  }
}

/** What each ending asks of the reader. One sentence, and it is a next action. */
export function endingLede(ending: Ending): string {
  switch (ending) {
    case 'finished': return 'The work was done and nothing left cleared the bar.';
    case 'budget': return 'The work was worth doing and the money ran out. Raise the budget.';
    case 'unfinished': return 'The turn ended with work this mode calls for still open, and neither the budget nor the scope stopped it.';
    case 'contested': return 'This record claims it finished and its own budget says a line ran out. Both cannot be true.';
    default: return 'Something outside the engine ended this run.';
  }
}

/** What a layer IS. `bonus` and `exploratory` say they were beyond the ask. */
export function layerLabel(layer: string): string {
  switch (str(layer).trim()) {
    case 'core': return 'what was asked';
    case 'professional': return 'what a professional would not ship without';
    case 'bonus': return 'beyond the ask: adjacent work with a return';
    case 'exploratory': return 'beyond the ask: ambition';
    default: return 'a layer this build cannot name';
  }
}

/** Why a candidate did not run, in words. `unrecorded` says so plainly. */
export function rejectionLabel(reason: string): string {
  switch (str(reason).trim()) {
    case 'out_of_scope': return 'outside the envelope';
    case 'no_permission': return 'the run did not hold the permission';
    case 'forbidden_effect': return 'the effect itself is denied here';
    case 'below_threshold': return 'the value did not clear the bar';
    case 'dominated': return 'another candidate was better on every axis';
    case 'duplicate': return 'already covered by something selected';
    case 'resolved': return 'something else fixed it on the way past';
    case 'quarantined': return 'depends on a capability that is not healthy';
    case 'budget': return 'would not fit';
    case 'round_full': return 'wanted, and this round had taken its batch';
    case 'risk': return 'a blocking risk, whatever the value';
    case 'stale': return 'the evidence aged out and did not revalidate';
    case 'superseded': return 'the state it was about has moved';
    // Second person, deliberately. Every other sentence here reports what the
    // engine concluded; this one reports what the reader themselves said, and
    // phrasing it as a judgement would let the engine take credit for a
    // decision it was handed.
    case 'declined': return 'you said no to it, and it will not be offered again';
    default: return 'nobody recorded why';
  }
}

/* -- the API ------------------------------------------------------------- */

const BASE = '/api/completion';

/**
 * A read, with the refusal convention applied.
 *
 * A rejected read is a 200 with `ok: false`, so a caller that only checked the
 * HTTP status would draw an empty list and call it "nothing decided" -- the one
 * answer this screen is not allowed to give by accident.
 */
async function read<T>(url: string, signal?: AbortSignal): Promise<T> {
  const payload = await getJson<unknown>(url, signal);
  const refusal = refusalOf(payload);
  if (refusal) throw new CompletionRefusal(refusal);
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
  if (refusal) throw new CompletionRefusal(refusal);
  if (!response.ok) {
    // 4xx is a malformed body, 403 is a model trying to do a person's job and
    // 404 is somebody else's decision; a validation rejection never gets here,
    // because it is a 200 the block above caught.
    const detail = str(obj(payload).detail) || `${url} responded ${response.status}`;
    throw new CompletionRefusal({
      code: response.status === 404 ? 'not_found'
        : response.status === 403 ? 'forbidden' : 'refused',
      message: detail,
      path: '',
    });
  }
  return payload as T;
}

export interface DecisionQuery {
  /** `true`, `false` or `all`. Three states and not a boolean: the third has
   *  to be reachable, and a missing boolean would default to one of the other
   *  two silently. */
  shadow?: string;
  mode?: string;
  projectId?: string;
  stopReason?: string;
  limit?: number;
  cursor?: string;
}

/**
 * The listing.
 *
 * `enabled` and `shadow_enabled` come back on every page and are carried
 * through rather than dropped: they are what the screen needs to say "the
 * engine is switched off" while still drawing every decision already recorded,
 * which is literally what the server does.
 */
export async function loadDecisions(query: DecisionQuery = {}, signal?: AbortSignal): Promise<DecisionPage> {
  const params = new URLSearchParams();
  params.set('shadow', query.shadow || 'false');
  if (query.mode) params.set('mode', query.mode);
  if (query.projectId) params.set('project_id', query.projectId);
  if (query.stopReason) params.set('stop_reason', query.stopReason);
  if (query.limit) params.set('limit', String(query.limit));
  if (query.cursor) params.set('cursor', query.cursor);
  const payload = obj(await read<unknown>(`${BASE}?${params.toString()}`, signal));
  return {
    enabled: flag(payload.enabled),
    shadowEnabled: flag(payload.shadow_enabled),
    decisions: asArray<unknown>(payload.decisions).map(summaryFrom).filter((row) => row.id),
    nextCursor: str(payload.next_cursor),
  };
}

/** One decision, with the closeout the server rendered for it. */
export async function loadDecision(id: string, signal?: AbortSignal): Promise<DecisionDetail> {
  const payload = obj(await read<unknown>(`${BASE}/${encodeURIComponent(str(id))}`, signal));
  const decision = decisionFrom(payload.decision ?? payload);
  return {
    decision,
    closeout: str(payload.closeout),
    // Derived from the whole decision rather than read off `payload.summary`,
    // which is the same arithmetic run on the server and carries none of the
    // identifying fields a card needs. Two cards for one run that disagreed
    // would be worse than one card computed twice.
    summary: summarize(decision),
  };
}

export async function loadModes(signal?: AbortSignal): Promise<Modes> {
  return modesFrom(await read<unknown>(`${BASE}/modes`, signal));
}

export async function loadConfig(signal?: AbortSignal): Promise<Config> {
  return configFrom(await read<unknown>(`${BASE}/config`, signal));
}

export async function loadSettings(signal?: AbortSignal): Promise<Settings> {
  return settingsFrom(await read<unknown>(`${BASE}/settings`, signal));
}

export async function loadDiagnostics(signal?: AbortSignal): Promise<Diagnostics> {
  return diagnosticsFrom(await read<unknown>(`${BASE}/diagnostics`, signal));
}

export interface RejectedImprovement {
  decisionId: string;
  candidateId: string;
  /** The structural key the refusal was filed under -- not the candidate id.
   *  The id belongs to one decision; the improvement outlives it and is
   *  rediscovered tomorrow under a new one, so a refusal keyed by id would be
   *  enforced exactly once, against the row the person was looking at rather
   *  than against the thing they were refusing. */
  candidateKey: string;
  /** The reason, as the server echoed it back. */
  recorded: string;
  actor: string;
  /** Whether anything was actually written. The screen believes THIS and not
   *  `ok`, because the route answered `ok: true` while storing nothing for a
   *  few hours after it was written. */
  stored: boolean;
}

/**
 * A person says no to one improvement, with a reason. §12.
 *
 * The reason is required by the route and required here, and not out of
 * ceremony: §1.8's rule is that a rejected opportunity must not reappear
 * without new evidence, and a refusal nobody justified is indistinguishable
 * next month from one nobody meant. Sending an empty one would earn a 200
 * rejection from the server, which is the right answer and a worse place to
 * find out.
 *
 * The route is `require_human`: a model that could reject its own improvements
 * could also quietly delete the record of having been told to do them.
 */
export async function rejectImprovement(
  decisionId: string, candidateId: string, reason: string,
): Promise<RejectedImprovement> {
  const payload = obj(await send<unknown>(
    `${BASE}/${encodeURIComponent(str(decisionId))}/reject-improvement`,
    { candidate_id: candidateId, reason: str(reason).trim() },
  ));
  return {
    decisionId: str(payload.decision_id),
    candidateId: str(payload.candidate_id),
    candidateKey: str(payload.candidate_key),
    recorded: str(payload.recorded),
    actor: str(payload.actor),
    stored: payload.stored === true,
  };
}

/* -- the event stream ---------------------------------------------------- */

export interface CompletionEvent {
  id: string;
  /** The cursor. An integer, and it only ever goes up. */
  seq: number;
  name: string;
  runId: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

export function eventFrom(raw: unknown): CompletionEvent {
  const e = obj(raw);
  return {
    id: str(e.id),
    seq: num(e.seq),
    name: str(e.name),
    runId: str(e.run_id) || str(obj(e.payload).run_id),
    payload: obj(e.payload),
    createdAt: str(e.created_at),
  };
}

export interface EventPage {
  events: CompletionEvent[];
  cursor: number;
  /** True when the stream dropped frames before the ones returned. Said out
   *  loud on the screen: a hole nobody mentions is read as quiet. */
  gap: boolean;
  enabled: boolean;
  shadowEnabled: boolean;
}

/** The poll. The floor under the stream, and the whole thing when there is no
 *  `EventSource` to be had. */
export async function loadEvents(since = 0, limit = 200, signal?: AbortSignal): Promise<EventPage> {
  const params = new URLSearchParams();
  params.set('since', String(Math.max(0, Math.trunc(num(since)))));
  params.set('limit', String(limit));
  const payload = obj(await read<unknown>(`${BASE}/events?${params.toString()}`, signal));
  return {
    events: asArray<unknown>(payload.events).map(eventFrom),
    cursor: num(payload.cursor),
    gap: flag(payload.gap),
    enabled: flag(payload.enabled),
    shadowEnabled: flag(payload.shadow_enabled),
  };
}

/**
 * Where a reconnection resumes.
 *
 * It moves FORWARD or it stays where it was. A cursor that went backwards, or
 * that a frame carrying no `seq` blanked to zero, would ask the stream to
 * replay from the beginning, and the page would apply every event it has
 * already applied -- which for this screen means counting one run's decisions
 * twice.
 */
export function advanceCursor(cursor: number, events: CompletionEvent[] | null | undefined): number {
  let next = Math.max(0, Math.trunc(num(cursor)));
  for (const event of events ?? []) {
    const seq = Math.trunc(num(event?.seq));
    if (seq > next) next = seq;
  }
  return next;
}

/** The two names that report a stop, and they are never the same one. §11. */
export function isStopEvent(name: string): boolean {
  const value = str(name).trim();
  return value === 'completion_converged'
    || value === 'completion_budget_exhausted'
    || value === 'completion_decision_recorded';
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
export function followCompletion(
  cursor: number,
  onEvent: (event: CompletionEvent) => void,
  onFail: () => void,
  onEnd?: () => void,
): () => void {
  if (typeof EventSource === 'undefined') {
    onFail();
    return () => {};
  }
  let source: EventSource | null = null;
  try {
    const since = Math.max(0, Math.trunc(num(cursor)));
    source = new EventSource(`${BASE}/events?stream=1&since=${encodeURIComponent(String(since))}`);
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
