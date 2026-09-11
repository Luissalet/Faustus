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
