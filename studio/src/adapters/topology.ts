import { ApiError, getJson, responseReason } from './api';
import { t } from '../i18n';

/**
 * B2 (OBJ-8, contract in scratchpad/CONTRATO_OBJ8_B.md) — the Studio half of
 * Lote A4's topology module (`docs/api/topology.md`, backend already
 * implemented and tested in commit 0017b70): Mermaid diagrams for a
 * workflow/future definition, profile+workflow lint findings, and a
 * workflow cost estimate. Same split `adapters/git.ts`/`board.ts` already
 * draw — this file only shapes the calls and surfaces what the server said
 * no to; nothing here duplicates the backend's own linter or estimator.
 *
 * `/api/workflows/mermaid` and `/api/workflows/estimate` are POST with a
 * `{definition}` body (the same shape `/api/workflows/validate` already
 * accepts) — a screen that has a run's definition (see
 * `adapters/activity.ts`'s `getWorkflowRunDefinition`) or a hand-built one
 * can call either. `definition` stays an opaque `Record<string, unknown>`
 * here on purpose: Studio has no `WorkflowDefinition` TS type of its own to
 * invent one against (grepped before writing this file), and the backend is
 * the only thing that actually parses/validates the shape
 * (`WorkflowDefinition.parse`) — inventing a partial client-side type would
 * only drift from it.
 */

export type LintSeverity = 'info' | 'warn' | 'error';

/** One lint result — `src/agent_profile_lint.py`'s `Finding.to_dict()`. */
export interface LintFinding {
  code: string;
  severity: LintSeverity;
  subject: string;
  message: string;
  hint: string;
}

export type ProfileKind = 'verification' | 'context' | 'budget' | 'collaboration' | 'output';

interface RawFinding {
  code?: unknown;
  severity?: unknown;
  subject?: unknown;
  message?: unknown;
  hint?: unknown;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);

function findingFrom(raw: RawFinding): LintFinding {
  const severity = raw.severity === 'error' || raw.severity === 'warn' ? raw.severity : 'info';
  return { code: str(raw.code), severity, subject: str(raw.subject), message: str(raw.message), hint: str(raw.hint) };
}

/** GET /api/agent-profiles/lint — every finding for the whole catalogue. */
export async function lintCatalog(signal?: AbortSignal): Promise<LintFinding[]> {
  const body = await getJson<{ ok?: boolean; findings?: RawFinding[] }>('/api/agent-profiles/lint', signal);
  return (body.findings ?? []).map(findingFrom);
}

/** A refusal from `GET /api/agent-profiles/lint/{kind}/{profile_id}` — same
 *  `{ok:false, error:{path,message}}` shape `agents.ts`'s
 *  `AgentProfileRefusal` already reads for the neighbouring routes. */
export class ProfileLintRefusal extends Error {
  readonly path: string;
  constructor(path: string, message: string) {
    super(message);
    this.name = 'ProfileLintRefusal';
    this.path = path;
  }
}

/** GET /api/agent-profiles/lint/{kind}/{profile_id} — one profile. */
export async function lintProfile(kind: ProfileKind, profileId: string, signal?: AbortSignal): Promise<LintFinding[]> {
  const body = await getJson<{ ok?: boolean; findings?: RawFinding[]; error?: { path?: unknown; message?: unknown } }>(
    `/api/agent-profiles/lint/${encodeURIComponent(kind)}/${encodeURIComponent(profileId)}`,
    signal,
  );
  if (body.ok === false) {
    throw new ProfileLintRefusal(str(body.error?.path) || '<root>', str(body.error?.message) || t('The server refused this request without saying why.'));
  }
  return (body.findings ?? []).map(findingFrom);
}

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    signal,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as T;
}

/** POST /api/workflows/mermaid — a `flowchart TD` of one workflow
 *  definition. A definition `WorkflowDefinition.parse()` refuses (a cycle,
 *  a missing dependency) surfaces as the same field-level `ApiError`
 *  message `/api/workflows/validate` gives. */
export async function workflowMermaid(definition: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
  const body = await postJson<{ ok?: boolean; mermaid?: unknown }>('/api/workflows/mermaid', { definition }, signal);
  if (body.ok !== true || typeof body.mermaid !== 'string') throw new ApiError(t('The server did not return a diagram.'), 502);
  return body.mermaid;
}

/** GET /api/futures/{future_id}/mermaid — a diagram of a branching future's
 *  strategy tree. Kept here for parity with the documented contract even
 *  though nothing in Studio calls it yet — there is no branching-futures
 *  screen in this tree to wire it into (see B2's report). */
export async function futureMermaid(futureId: string, signal?: AbortSignal): Promise<string> {
  const body = await getJson<{ ok?: boolean; mermaid?: unknown }>(`/api/futures/${encodeURIComponent(futureId)}/mermaid`, signal);
  if (body.ok !== true || typeof body.mermaid !== 'string') throw new ApiError(t('The server did not return a diagram.'), 502);
  return body.mermaid;
}

export interface EstimatePerNode {
  nodeId: string;
  type: string;
  model: string;
  callsMin: number;
  callsMax: number;
  usdMin: number;
  usdMax: number;
  note: string;
}

export interface UnboundedLoop {
  nodes: string[];
  assumedIterations: number;
}

/** `src/workflow_cost_estimate.py`'s `Estimate.to_dict()`. */
export interface WorkflowEstimate {
  totalUsdMin: number;
  totalUsdMax: number;
  callsMin: number;
  callsMax: number;
  perNode: EstimatePerNode[];
  unboundedLoops: UnboundedLoop[];
  unpricedModels: string[];
}

function estimateFrom(raw: Record<string, unknown>): WorkflowEstimate {
  const perNode = Array.isArray(raw.per_node) ? (raw.per_node as Record<string, unknown>[]) : [];
  const loops = Array.isArray(raw.unbounded_loops) ? (raw.unbounded_loops as Record<string, unknown>[]) : [];
  return {
    totalUsdMin: num(raw.total_usd_min),
    totalUsdMax: num(raw.total_usd_max),
    callsMin: num(raw.calls_min),
    callsMax: num(raw.calls_max),
    perNode: perNode.map((n) => ({
      nodeId: str(n.node_id),
      type: str(n.type),
      model: str(n.model),
      callsMin: num(n.calls_min),
      callsMax: num(n.calls_max),
      usdMin: num(n.usd_min),
      usdMax: num(n.usd_max),
      note: str(n.note),
    })),
    unboundedLoops: loops.map((l) => ({
      nodes: Array.isArray(l.nodes) ? l.nodes.map(String) : [],
      assumedIterations: num(l.assumed_iterations),
    })),
    unpricedModels: Array.isArray(raw.unpriced_models) ? raw.unpriced_models.map(String) : [],
  };
}

export interface ModelPriceInput {
  promptUsdPer1k: number;
  completionUsdPer1k: number;
}

/** POST /api/workflows/estimate — a min/max USD range for one run of
 *  `definition`. `prices` is optional and caller-supplied on purpose: the
 *  backend does not look pricing up from any catalogue of its own (see
 *  `docs/api/topology.md`'s "what is priced, and where the number comes
 *  from") — a `skill` node naming a model absent from `prices` comes back
 *  named in `unpricedModels` with $0 contributed, not guessed. */
export async function workflowEstimate(
  definition: Record<string, unknown>,
  prices?: Record<string, ModelPriceInput>,
  signal?: AbortSignal,
): Promise<WorkflowEstimate> {
  const wirePrices = prices
    ? Object.fromEntries(Object.entries(prices).map(([id, p]) => [id, { prompt_usd_per_1k: p.promptUsdPer1k, completion_usd_per_1k: p.completionUsdPer1k }]))
    : undefined;
  const body = await postJson<{ ok?: boolean; estimate?: Record<string, unknown> }>(
    '/api/workflows/estimate',
    wirePrices ? { definition, prices: wirePrices } : { definition },
    signal,
  );
  if (body.ok !== true || !body.estimate) throw new ApiError(t('The server did not return a cost estimate.'), 502);
  return estimateFrom(body.estimate);
}

/**
 * CMP-07 (W2-E, `docs/api/topology.md` §Simulate) — a structural,
 * round-by-round walk of a definition. Nothing here executes anything on
 * the server either: `POST /api/workflows/simulate` never calls a real
 * node handler, the same guarantee `workflowMermaid`/`workflowEstimate`
 * already document for their own endpoints.
 */
export interface SimulationRound {
  round: number;
  activated: string[];
  notTaken: string[];
  humanWaits: string[];
  newlyAwaitingChoice: string[];
}

/** Why a node cannot be simulated further: `blockedBy` is the one gate
 *  (a `condition` or `human_approval` id) a whole downstream chain points
 *  back at — never guessed past. */
export interface AwaitingChoice {
  kind: string;
  blockedBy: string;
  reason: string;
}

export interface SimulationResult {
  rounds: SimulationRound[];
  activated: string[];
  notTaken: string[];
  humanWaits: string[];
  awaitingChoice: Record<string, AwaitingChoice>;
  notReached: string[];
  roundsUsed: number;
  roundsMax: number;
  warnings: string[];
}

function awaitingChoiceFrom(raw: Record<string, unknown>): AwaitingChoice {
  return { kind: str(raw.kind), blockedBy: str(raw.blocked_by), reason: str(raw.reason) };
}

function simulationFrom(raw: Record<string, unknown>): SimulationResult {
  const rounds = Array.isArray(raw.rounds) ? (raw.rounds as Record<string, unknown>[]) : [];
  const rawAwaiting = raw.awaiting_choice && typeof raw.awaiting_choice === 'object'
    ? (raw.awaiting_choice as Record<string, Record<string, unknown>>)
    : {};
  const awaitingChoice: Record<string, AwaitingChoice> = {};
  for (const [nodeId, entry] of Object.entries(rawAwaiting)) awaitingChoice[nodeId] = awaitingChoiceFrom(entry);
  return {
    rounds: rounds.map((r) => ({
      round: num(r.round),
      activated: Array.isArray(r.activated) ? r.activated.map(String) : [],
      notTaken: Array.isArray(r.not_taken) ? r.not_taken.map(String) : [],
      humanWaits: Array.isArray(r.human_waits) ? r.human_waits.map(String) : [],
      newlyAwaitingChoice: Array.isArray(r.newly_awaiting_choice) ? r.newly_awaiting_choice.map(String) : [],
    })),
    activated: Array.isArray(raw.activated) ? raw.activated.map(String) : [],
    notTaken: Array.isArray(raw.not_taken) ? raw.not_taken.map(String) : [],
    humanWaits: Array.isArray(raw.human_waits) ? raw.human_waits.map(String) : [],
    awaitingChoice,
    notReached: Array.isArray(raw.not_reached) ? raw.not_reached.map(String) : [],
    roundsUsed: num(raw.rounds_used),
    roundsMax: num(raw.rounds_max),
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
  };
}

/** POST /api/workflows/simulate — `choices` assumes one outcome (`true`
 *  passes/approves, `false` fails/denies) for a `condition`/`human_approval`
 *  node while exploring; a node named in neither `choices` nor reachable at
 *  all comes back in `awaitingChoice`/`notReached`, never guessed past. */
export async function workflowSimulate(
  definition: Record<string, unknown>,
  options?: { choices?: Record<string, boolean>; roundsMax?: number },
  signal?: AbortSignal,
): Promise<SimulationResult> {
  const body: Record<string, unknown> = { definition };
  if (options?.choices) body.choices = options.choices;
  if (options?.roundsMax) body.rounds_max = options.roundsMax;
  const res = await postJson<{ ok?: boolean; simulation?: Record<string, unknown> }>('/api/workflows/simulate', body, signal);
  if (res.ok !== true || !res.simulation) throw new ApiError(t('The server did not return a simulation.'), 502);
  return simulationFrom(res.simulation);
}

/**
 * ADP-16 (`docs/api/topology.md` §Preflight) — connections, tools,
 * permissions, human waits, a token estimate and a cost estimate for a
 * definition, zero LLM/script/network effects. Kept as its own small
 * per-node row type here (`PreflightCostRow`) rather than importing
 * `EstimatePerNode` — the two lots touching this file this batch (CMP-07 and
 * CMP-08's estimate types) stay decoupled on purpose.
 */
export interface PreflightTokenRow {
  nodeId: string;
  model: string;
  tokensMin: number;
  tokensMax: number;
  modelInstalledLocally: boolean | null;
}

export interface PreflightOutput {
  nodeId: string;
  type: string;
  title: string;
}

export interface PreflightCostRow {
  nodeId: string;
  type: string;
  model: string;
  callsMin: number;
  callsMax: number;
  usdMin: number;
  usdMax: number;
  note: string;
}

export interface Preflight {
  connections: string[];
  tools: string[];
  permissionsRequired: string[];
  inputs: string[];
  outputs: PreflightOutput[];
  humanWaits: string[];
  tokenEstimate: { scenarioTokens: number; conservativeTokens: number; perNode: PreflightTokenRow[]; basis: string };
  cost: { scenario: number | 'unknown'; conservative: number | 'unknown'; unpriced: string[]; perNode: PreflightCostRow[]; basis: string };
  warnings: LintFinding[];
}

function preflightFrom(raw: Record<string, unknown>): Preflight {
  const tokenEstimate = (raw.token_estimate ?? {}) as Record<string, unknown>;
  const cost = (raw.cost ?? {}) as Record<string, unknown>;
  const tokenRows = Array.isArray(tokenEstimate.per_node) ? (tokenEstimate.per_node as Record<string, unknown>[]) : [];
  const costRows = Array.isArray(cost.per_node) ? (cost.per_node as Record<string, unknown>[]) : [];
  const outputs = Array.isArray(raw.outputs) ? (raw.outputs as Record<string, unknown>[]) : [];
  const scenario = cost.scenario === 'unknown' ? 'unknown' : num(cost.scenario);
  const conservative = cost.conservative === 'unknown' ? 'unknown' : num(cost.conservative);
  return {
    connections: Array.isArray(raw.connections) ? raw.connections.map(String) : [],
    tools: Array.isArray(raw.tools) ? raw.tools.map(String) : [],
    permissionsRequired: Array.isArray(raw.permissions_required) ? raw.permissions_required.map(String) : [],
    inputs: Array.isArray(raw.inputs) ? raw.inputs.map(String) : [],
    outputs: outputs.map((o) => ({ nodeId: str(o.node_id), type: str(o.type), title: str(o.title) })),
    humanWaits: Array.isArray(raw.human_waits) ? raw.human_waits.map(String) : [],
    tokenEstimate: {
      scenarioTokens: num(tokenEstimate.scenario_tokens),
      conservativeTokens: num(tokenEstimate.conservative_tokens),
      perNode: tokenRows.map((r) => ({
        nodeId: str(r.node_id), model: str(r.model), tokensMin: num(r.tokens_min), tokensMax: num(r.tokens_max),
        modelInstalledLocally: typeof r.model_installed_locally === 'boolean' ? r.model_installed_locally : null,
      })),
      basis: str(tokenEstimate.basis),
    },
    cost: {
      scenario, conservative,
      unpriced: Array.isArray(cost.unpriced) ? cost.unpriced.map(String) : [],
      perNode: costRows.map((r) => ({
        nodeId: str(r.node_id), type: str(r.type), model: str(r.model),
        callsMin: num(r.calls_min), callsMax: num(r.calls_max),
        usdMin: num(r.usd_min), usdMax: num(r.usd_max), note: str(r.note),
      })),
      basis: str(cost.basis),
    },
    warnings: Array.isArray(raw.warnings) ? (raw.warnings as RawFinding[]).map(findingFrom) : [],
  };
}

/** POST /api/workflows/preflight — `installedModels` only annotates
 *  `tokenEstimate.perNode[i].modelInstalledLocally`; it never changes what
 *  is priced or connected. */
export async function workflowPreflight(
  definition: Record<string, unknown>,
  options?: { prices?: Record<string, ModelPriceInput>; installedModels?: string[] },
  signal?: AbortSignal,
): Promise<Preflight> {
  const body: Record<string, unknown> = { definition };
  if (options?.prices) {
    body.prices = Object.fromEntries(
      Object.entries(options.prices).map(([id, p]) => [id, { prompt_usd_per_1k: p.promptUsdPer1k, completion_usd_per_1k: p.completionUsdPer1k }]),
    );
  }
  if (options?.installedModels) body.installed_models = options.installedModels;
  const res = await postJson<{ ok?: boolean; preflight?: Record<string, unknown> }>('/api/workflows/preflight', body, signal);
  if (res.ok !== true || !res.preflight) throw new ApiError(t('The server did not return a preflight report.'), 502);
  return preflightFrom(res.preflight);
}

/**
 * ADP-17 (`docs/api/topology.md` §Interchange) — round-tripping a
 * definition (`export_canonical`) and importing one drafted elsewhere
 * (`import_external`). `importWorkflowDefinition` is always a 200 on the
 * wire — an unrecognized format or an unsupported node type is a normal
 * outcome (`executable: false`), never thrown as an `ApiError`.
 */
export interface ImportResult {
  definition: Record<string, unknown> | null;
  designOnly: unknown[];
  rejected: unknown[];
  executable: boolean;
}

/** POST /api/workflows/import — the payload itself: this module's own
 *  canonical envelope, or an aigraphstudio-shaped `{schemaVersion, nodes,
 *  edges}` graph (paste-from-JSON in `WorkflowsScreen`). */
export async function importWorkflowDefinition(payload: unknown, signal?: AbortSignal): Promise<ImportResult> {
  const res = await postJson<Record<string, unknown>>('/api/workflows/import', payload, signal);
  return {
    definition: (res.definition as Record<string, unknown> | null | undefined) ?? null,
    designOnly: Array.isArray(res.design_only) ? res.design_only : [],
    rejected: Array.isArray(res.rejected) ? res.rejected : [],
    executable: res.executable === true,
  };
}

/** POST /api/workflows/export — the canonical envelope, optionally carrying
 *  a canvas `layout` (`node_id -> {x, y}`, unknown ids/non-numeric positions
 *  dropped server-side) so `PlanGraph`'s hand-adjusted node positions
 *  survive a round trip without becoming part of what actually runs. */
export async function exportWorkflowDefinition(
  definition: Record<string, unknown>,
  options?: { layout?: Record<string, { x: number; y: number }>; designOnly?: string[]; provenance?: Record<string, unknown> },
  signal?: AbortSignal,
): Promise<Record<string, unknown>> {
  const body: Record<string, unknown> = { definition };
  if (options?.layout) body.layout = options.layout;
  if (options?.designOnly) body.design_only = options.designOnly;
  if (options?.provenance) body.provenance = options.provenance;
  const res = await postJson<{ ok?: boolean; export?: Record<string, unknown> }>('/api/workflows/export', body, signal);
  if (res.ok !== true || !res.export) throw new ApiError(t('The server did not return an export.'), 502);
  return res.export;
}

/**
 * CMP-08 (`docs/api/topology.md` §Estimate, `estimate_detailed()`'s shape) —
 * separate `nodeActivations`/`modelCalls`/`externalOps`/`tokens` accounts
 * instead of `WorkflowEstimate`'s one blended `callsMin/callsMax`, a
 * structured price per model (`{amountPromptPer1m, amountCompletionPer1m,
 * unit, currency, source, asOf}` — never a bare number), and a
 * `structuralBounds`/`forecastWithAssumptions`/`measured` split. Fetched
 * with `POST /api/workflows/estimate?detail=1` — same body as
 * `workflowEstimate`, plus the optional inputs `estimate_detailed()` accepts
 * (`capabilityPricing`, `skillCallsProfiles`, `localLatency`, `runId`).
 */
export interface MinMax {
  min: number;
  max: number | 'unbounded';
}

export interface StructuredPrice {
  amountPromptPer1m: number;
  amountCompletionPer1m: number;
  unit: string;
  currency: string;
  source: string;
  asOf: string;
}

export interface DetailedEstimatePerNode {
  nodeId: string;
  type: string;
  structuralBounds: { min: number; max: number | 'unbounded' };
  activations: { min: number; max: number };
  isModelCall: boolean;
  isExternalOp: boolean;
  model: string;
  callsProfileSource: string;
  modelCalls: { min: number; max: number };
  externalOps: { min: number; max: number };
  tokensIn: { min: number; max: number };
  tokensOut: { min: number; max: number };
  usd: { min: number; max: number };
  note: string;
  latencyEstimate?: Record<string, unknown>;
}

export interface MeasuredUsage {
  toolCalls: number | null;
  activeSeconds: number | null;
  tokensIn: number | null;
  tokensOut: number | null;
  costUsd: number | null;
  note: string;
}

/** `src/workflow_cost_estimate.py`'s `DetailedEstimate.to_dict()`. */
export interface DetailedEstimate {
  nodeActivations: { min: number; max: number };
  modelCalls: { min: number; max: number };
  externalOps: { min: number; max: number };
  tokens: { in: { min: number; max: number }; out: { min: number; max: number } };
  costKnownUsd: { min: number; max: number };
  costUnestimable: string[];
  structuralBounds: Record<string, unknown>;
  forecastWithAssumptions: Record<string, unknown>;
  measured: MeasuredUsage | null;
  pricesUsed: Record<string, StructuredPrice>;
  perNode: DetailedEstimatePerNode[];
  assumptions: string[];
}

function minMax(raw: unknown): { min: number; max: number } {
  const r = (raw ?? {}) as Record<string, unknown>;
  return { min: num(r.min), max: num(r.max) };
}

function structuredPriceFrom(raw: Record<string, unknown>): StructuredPrice {
  return {
    amountPromptPer1m: num(raw.amount_prompt_per_1m),
    amountCompletionPer1m: num(raw.amount_completion_per_1m),
    unit: str(raw.unit), currency: str(raw.currency), source: str(raw.source), asOf: str(raw.as_of),
  };
}

function detailedEstimateFrom(raw: Record<string, unknown>): DetailedEstimate {
  const perNode = Array.isArray(raw.per_node) ? (raw.per_node as Record<string, unknown>[]) : [];
  const tokens = (raw.tokens ?? {}) as Record<string, unknown>;
  const measuredRaw = raw.measured as Record<string, unknown> | null | undefined;
  const pricesUsedRaw = (raw.prices_used ?? {}) as Record<string, unknown>;
  const pricesUsed: Record<string, StructuredPrice> = {};
  for (const [modelId, p] of Object.entries(pricesUsedRaw)) {
    if (p && typeof p === 'object') pricesUsed[modelId] = structuredPriceFrom(p as Record<string, unknown>);
  }
  return {
    nodeActivations: minMax(raw.node_activations),
    modelCalls: minMax(raw.model_calls),
    externalOps: minMax(raw.external_ops),
    tokens: { in: minMax(tokens.in), out: minMax(tokens.out) },
    costKnownUsd: minMax(raw.cost_known_usd),
    costUnestimable: Array.isArray(raw.cost_unestimable) ? raw.cost_unestimable.map(String) : [],
    structuralBounds: (raw.structural_bounds ?? {}) as Record<string, unknown>,
    forecastWithAssumptions: (raw.forecast_with_assumptions ?? {}) as Record<string, unknown>,
    measured: measuredRaw ? {
      toolCalls: typeof measuredRaw.tool_calls === 'number' ? measuredRaw.tool_calls : null,
      activeSeconds: typeof measuredRaw.active_seconds === 'number' ? measuredRaw.active_seconds : null,
      tokensIn: typeof measuredRaw.tokens_in === 'number' ? measuredRaw.tokens_in : null,
      tokensOut: typeof measuredRaw.tokens_out === 'number' ? measuredRaw.tokens_out : null,
      costUsd: typeof measuredRaw.cost_usd === 'number' ? measuredRaw.cost_usd : null,
      note: str(measuredRaw.note),
    } : null,
    pricesUsed,
    perNode: perNode.map((n) => ({
      nodeId: str(n.node_id), type: str(n.type),
      structuralBounds: {
        min: num((n.structural_bounds as Record<string, unknown> | undefined)?.min),
        max: (n.structural_bounds as Record<string, unknown> | undefined)?.max === 'unbounded'
          ? 'unbounded' as const : num((n.structural_bounds as Record<string, unknown> | undefined)?.max),
      },
      activations: minMax(n.activations),
      isModelCall: n.is_model_call === true, isExternalOp: n.is_external_op === true,
      model: str(n.model), callsProfileSource: str(n.calls_profile_source),
      modelCalls: minMax(n.model_calls), externalOps: minMax(n.external_ops),
      tokensIn: minMax(n.tokens_in), tokensOut: minMax(n.tokens_out),
      usd: minMax(n.usd), note: str(n.note),
      latencyEstimate: n.latency_estimate && typeof n.latency_estimate === 'object'
        ? (n.latency_estimate as Record<string, unknown>) : undefined,
    })),
    assumptions: Array.isArray(raw.assumptions) ? raw.assumptions.map(String) : [],
  };
}

export interface DetailedEstimateOptions {
  prices?: Record<string, ModelPriceInput>;
  capabilityPricing?: Record<string, Record<string, unknown>>;
  skillCallsProfiles?: Record<string, { modelCalls: number; externalOps: number; tokensIn: number; tokensOut: number }>;
  localLatency?: Record<string, Record<string, unknown>>;
  runId?: string;
}

function detailBody(definition: Record<string, unknown>, options?: DetailedEstimateOptions): Record<string, unknown> {
  const body: Record<string, unknown> = { definition };
  if (options?.prices) {
    body.prices = Object.fromEntries(
      Object.entries(options.prices).map(([id, p]) => [id, { prompt_usd_per_1k: p.promptUsdPer1k, completion_usd_per_1k: p.completionUsdPer1k }]),
    );
  }
  if (options?.capabilityPricing) body.capability_pricing = options.capabilityPricing;
  if (options?.skillCallsProfiles) {
    body.skill_calls_profiles = Object.fromEntries(
      Object.entries(options.skillCallsProfiles).map(([id, p]) => [id, {
        model_calls: p.modelCalls, external_ops: p.externalOps, tokens_in: p.tokensIn, tokens_out: p.tokensOut,
      }]),
    );
  }
  if (options?.localLatency) body.local_latency = options.localLatency;
  if (options?.runId) body.run_id = options.runId;
  return body;
}

/** POST /api/workflows/estimate?detail=1 */
export async function workflowEstimateDetailed(
  definition: Record<string, unknown>,
  options?: DetailedEstimateOptions,
  signal?: AbortSignal,
): Promise<DetailedEstimate> {
  const body = await postJson<{ ok?: boolean; estimate?: Record<string, unknown> }>(
    '/api/workflows/estimate?detail=1', detailBody(definition, options), signal,
  );
  if (body.ok !== true || !body.estimate) throw new ApiError(t('The server did not return a cost estimate.'), 502);
  return detailedEstimateFrom(body.estimate);
}

/**
 * CMP-08 — `plan_compare.compare`: several candidate definitions for the
 * same goal, one table, each cell tagged how sure the number is.
 */
export type PlanBasis = 'computed' | 'estimated' | 'unknown';

export interface PlanCell {
  value: number | string;
  basis: PlanBasis;
}

export interface PlanRow {
  metric: string;
  cells: Record<string, PlanCell>;
}

export interface PlanComparison {
  goal: string;
  planIds: string[];
  planLabels: Record<string, string>;
  rows: PlanRow[];
  reasons: Record<string, string[]>;
  detail: Record<string, DetailedEstimate>;
  errors: Record<string, string>;
}

function planComparisonFrom(raw: Record<string, unknown>): PlanComparison {
  const rows = Array.isArray(raw.rows) ? (raw.rows as Record<string, unknown>[]) : [];
  const rawDetail = (raw.detail ?? {}) as Record<string, unknown>;
  const detail: Record<string, DetailedEstimate> = {};
  for (const [planId, d] of Object.entries(rawDetail)) {
    if (d && typeof d === 'object') detail[planId] = detailedEstimateFrom(d as Record<string, unknown>);
  }
  const rawReasons = (raw.reasons ?? {}) as Record<string, unknown>;
  const reasons: Record<string, string[]> = {};
  for (const [planId, r] of Object.entries(rawReasons)) reasons[planId] = Array.isArray(r) ? r.map(String) : [];
  return {
    goal: str(raw.goal),
    planIds: Array.isArray(raw.plan_ids) ? raw.plan_ids.map(String) : [],
    planLabels: ((raw.plan_labels ?? {}) as Record<string, unknown>) as Record<string, string>,
    rows: rows.map((r) => {
      const cellsRaw = (r.cells ?? {}) as Record<string, unknown>;
      const cells: Record<string, PlanCell> = {};
      for (const [planId, c] of Object.entries(cellsRaw)) {
        const cell = (c ?? {}) as Record<string, unknown>;
        const basis = cell.basis === 'computed' || cell.basis === 'estimated' ? cell.basis : 'unknown';
        cells[planId] = { value: (typeof cell.value === 'number' || typeof cell.value === 'string') ? cell.value : 0, basis };
      }
      return { metric: str(r.metric), cells };
    }),
    reasons,
    detail,
    errors: ((raw.errors ?? {}) as Record<string, unknown>) as Record<string, string>,
  };
}

export interface PlanInput {
  id: string;
  label?: string;
  definition: Record<string, unknown>;
}

/** POST /api/workflows/compare-plans */
export async function comparePlans(
  goal: string,
  plans: PlanInput[],
  options?: DetailedEstimateOptions,
  signal?: AbortSignal,
): Promise<PlanComparison> {
  const body: Record<string, unknown> = {
    goal,
    plans: plans.map((p) => ({ id: p.id, label: p.label, definition: p.definition })),
  };
  const detailOpts = detailBody({}, options);
  delete detailOpts.definition;
  Object.assign(body, detailOpts);
  const res = await postJson<{ ok?: boolean; comparison?: Record<string, unknown> }>('/api/workflows/compare-plans', body, signal);
  if (res.ok !== true || !res.comparison) throw new ApiError(t('The server did not return a plan comparison.'), 502);
  return planComparisonFrom(res.comparison);
}
