import { ApiError, getJson } from './api';

/**
 * The two things a person can do to a running sub-agent (delegate_agents
 * worker) besides watching it: stop it alone, or steer it — a message the
 * worker reads before its next round. Same endpoints the legacy board uses
 * (routes/chat_routes.py, /api/chat/subagent/*).
 */

export async function stopWorker(childSessionId: string): Promise<boolean> {
  const response = await fetch(`/api/chat/subagent/stop/${encodeURIComponent(childSessionId)}`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  if (!response.ok) throw new ApiError(`stop responded ${response.status}`, response.status);
  const body = (await response.json().catch(() => ({}))) as { stopped?: boolean };
  return Boolean(body.stopped);
}

/** Resolves to false when the worker is no longer running (404). */
export async function steerWorker(childSessionId: string, text: string): Promise<boolean> {
  const response = await fetch(`/api/chat/subagent/steer/${encodeURIComponent(childSessionId)}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  if (response.status === 404) return false;
  if (!response.ok) throw new ApiError(`steer responded ${response.status}`, response.status);
  return true;
}

/* ── resource leases (/api/agents/leases) ─────────────────────────────────
 *
 * PLAN-03: who holds which resource right now, and any conflicting claim a
 * delegation's acquire refused — src/resource_ownership.py's process-wide
 * registry, read-only. "Who edits what" for the Workers panel.
 */

export interface AgentLease {
  resource: string;
  kind: string;
  ownerAgent: string;
  taskId: string;
  since: number;
  expiresAt: number;
  ttlSeconds: number;
  activity: string[];
}

export interface LeaseConflict {
  resource: string;
  kind: string;
  holderAgent: string;
  holderTaskId: string;
  requesterAgent: string;
  requesterTaskId: string;
  at: number;
}

export interface LeasesSnapshot {
  leases: AgentLease[];
  conflicts: LeaseConflict[];
}

function leaseFromRow(row: Record<string, unknown>): AgentLease {
  return {
    resource: asString(row.resource),
    kind: asString(row.kind),
    ownerAgent: asString(row.owner_agent),
    taskId: asString(row.task_id),
    since: asCount(row.since) ?? 0,
    expiresAt: asCount(row.expires_at) ?? 0,
    ttlSeconds: asCount(row.ttl_seconds) ?? 0,
    activity: asList(row.activity),
  };
}

function conflictFromRow(row: Record<string, unknown>): LeaseConflict {
  return {
    resource: asString(row.resource),
    kind: asString(row.kind),
    holderAgent: asString(row.holder_agent),
    holderTaskId: asString(row.holder_task_id),
    requesterAgent: asString(row.requester_agent),
    requesterTaskId: asString(row.requester_task_id),
    at: asCount(row.at) ?? 0,
  };
}

/** Every active lease and any refused/conflicting claim, right now. */
export async function loadLeases(signal?: AbortSignal): Promise<LeasesSnapshot> {
  const body = asObject(await getJson<unknown>('/api/agents/leases', signal));
  const leases = (Array.isArray(body.leases) ? body.leases : []).map((r) => leaseFromRow(asObject(r)));
  const conflicts = (Array.isArray(body.conflicts) ? body.conflicts : []).map((r) => conflictFromRow(asObject(r)));
  return { leases, conflicts };
}

/* ── agent profiles (/api/agent-profiles) ─────────────────────────────────
 *
 * The orthogonal half of a definition: how far a mission pushes, which
 * versioned policies it references, and — the one that matters — what the
 * EFFECTIVE configuration would be before anything runs.
 *
 * Two things this layer is careful about, both of them the server's own
 * promises rather than conveniences:
 *
 * **A refusal is an answer.** `routes/agent_profiles_routes.py` returns one as
 * a 200 with `{"ok": false, "error": {"path", "message"}}`, so the reader here
 * raises `AgentProfileRefusal` carrying both halves: the screen shows the
 * sentence the server wrote next to the field it names.
 *
 * **The preview takes no identity.** `owner` and `project_id` come from the
 * session; sending them changes nothing and the answer lists them back in
 * `ignoredFields`. Nothing here sends them.
 */

export interface ProfileRefs {
  verification: string;
  context: string;
  budget: string;
  collaboration: string;
  output: string;
}

/** The fields the profiles plan added to a definition. */
export interface AgentProfile {
  slug: string;
  defaultCompletionMode: string;
  capabilities: string[];
  specialties: string[];
  tags: string[];
  preferredTasks: string[];
  avoidTasks: string[];
  profiles: ProfileRefs;
  extendsSlug: string;
  inherits: string[];
}

/** One row of the table `explain()` prints: value, level, and what lost. */
export interface EffectiveRow {
  field: string;
  value: string;
  level: string;
  discarded: { level: string; value: string; why: string }[];
}

export interface EffectiveConfig {
  agent: string;
  revision: string;
  source: string;
  identity: string;
  completionMode: string;
  completionSource: string;
  completionReason: string;
  model: string;
  endpointId: string;
  runner: string;
  maxRounds: number | null;
  timeoutS: number | null;
  profiles: ProfileRefs;
  tools: string[];
  deny: string[];
  workRoots: string[];
  effects: string[];
  permissionRules: string[];
  rows: EffectiveRow[];
  caveats: string[];
  degraded: string[];
  ignoredFields: string[];
}

/** A refusal, raised so a caller cannot mistake it for a success. */
export class AgentProfileRefusal extends Error {
  readonly path: string;

  constructor(path: string, message: string) {
    super(message);
    this.name = 'AgentProfileRefusal';
    this.path = path;
  }
}

const asString = (value: unknown): string => (typeof value === 'string' ? value : '');
const asList = (value: unknown): string[] =>
  Array.isArray(value) ? value.map(asString).filter(Boolean) : [];

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function refusalOf(body: Record<string, unknown>): AgentProfileRefusal | null {
  if (body.ok !== false) return null;
  const error = asObject(body.error);
  return new AgentProfileRefusal(
    asString(error.path) || '<root>',
    asString(error.message) || 'the server refused the request without saying why',
  );
}

function profileRefs(row: Record<string, unknown>): ProfileRefs {
  return {
    verification: asString(row.verification_profile),
    context: asString(row.context_profile),
    budget: asString(row.budget_profile),
    collaboration: asString(row.collaboration_profile),
    output: asString(row.output_contract),
  };
}

/**
 * Every definition's profile fields, by slug.
 *
 * A Map rather than a list because the caller already has the definitions
 * (`listDefs`, the definitions API) and needs to look these up beside them:
 * two lists to zip is how a card ends up showing another agent's capabilities.
 */
export async function loadAgentProfiles(signal?: AbortSignal): Promise<Map<string, AgentProfile>> {
  const response = await fetch('/api/agent-profiles', {
    signal,
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ApiError(`/api/agent-profiles responded ${response.status}`, response.status);
  }
  const body = asObject(await response.json());
  const refusal = refusalOf(body);
  if (refusal) throw refusal;
  const out = new Map<string, AgentProfile>();
  for (const raw of Array.isArray(body.agents) ? body.agents : []) {
    const row = asObject(raw);
    const slug = asString(row.slug);
    if (!slug) continue;
    out.set(slug, {
      slug,
      defaultCompletionMode: asString(row.default_completion_mode),
      capabilities: asList(row.capabilities),
      specialties: asList(row.specialties),
      tags: asList(row.tags),
      preferredTasks: asList(row.preferred_tasks),
      avoidTasks: asList(row.avoid_tasks),
      profiles: profileRefs(row),
      extendsSlug: asString(row.extends),
      inherits: asList(row.inherits),
    });
  }
  return out;
}

function rowsOf(value: unknown): EffectiveRow[] {
  return (Array.isArray(value) ? value : []).map((raw) => {
    const row = asObject(raw);
    const value_ = row.value;
    return {
      field: asString(row.field),
      value: Array.isArray(value_)
        ? value_.map(asString).join(', ')
        : value_ === null || value_ === undefined || value_ === ''
          ? ''
          : String(value_),
      level: asString(row.level),
      discarded: (Array.isArray(row.discarded) ? row.discarded : []).map((item) => {
        const lost = asObject(item);
        return {
          level: asString(lost.level),
          value: Array.isArray(lost.value)
            ? lost.value.map(asString).join(', ')
            : lost.value === null || lost.value === undefined
              ? ''
              : String(lost.value),
          why: asString(lost.why),
        };
      }),
    };
  });
}

/**
 * The effective configuration of one agent, as a preview.
 *
 * Nothing is run and nothing is stored: this is `POST /resolve`, whose whole
 * job is to answer "what would this start from" before anything starts.
 */
export async function resolveEffective(
  slug: string,
  task?: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<EffectiveConfig> {
  const response = await fetch('/api/agent-profiles/resolve', {
    method: 'POST',
    signal,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    // Deliberately no owner and no project_id: both come from the session.
    body: JSON.stringify(task ? { agent: slug, task } : { agent: slug }),
  });
  if (!response.ok) {
    throw new ApiError(`/api/agent-profiles/resolve responded ${response.status}`, response.status);
  }
  const body = asObject(await response.json());
  const refusal = refusalOf(body);
  if (refusal) throw refusal;
  const resolution = asObject(body.resolution);
  const agent = asObject(resolution.agent);
  const route = asObject(resolution.model_route);
  const completion = asObject(resolution.completion);
  const permissions = asObject(resolution.permissions);
  const profiles = asObject(resolution.profiles);
  return {
    agent: asString(agent.slug),
    revision: asString(agent.definition_revision),
    source: asString(agent.source),
    identity: asString(body.identity),
    completionMode: asString(completion.mode),
    completionSource: asString(completion.source),
    completionReason: asString(completion.reason),
    model: asString(route.model),
    endpointId: asString(route.endpoint_id),
    runner: asString(route.runner),
    maxRounds: asCount(resolution.max_rounds),
    timeoutS: asCount(resolution.timeout_s),
    profiles: {
      verification: asString(profiles.verification),
      context: asString(profiles.context),
      budget: asString(profiles.budget),
      collaboration: asString(profiles.collaboration),
      output: asString(profiles.output),
    },
    tools: asList(permissions.tools),
    deny: asList(permissions.deny),
    workRoots: asList(permissions.work_roots),
    effects: asList(permissions.effects),
    permissionRules: asList(permissions.permission_rules),
    rows: rowsOf(body.rows),
    caveats: asList(resolution.caveats),
    degraded: asList(resolution.degraded_integrations),
    ignoredFields: asList(body.ignored_fields),
  };
}

/* ── sub-agent transcript (GET /api/chat/subagent/transcript/{id}) ─────────
 *
 * Read-only: what one delegate_agents worker said and did — messages, every
 * tool call with its result — plus its token use, per recorded model call
 * when call tracing kept them. Ownership is checked server-side exactly like
 * stop/steer; another user's worker answers 404.
 */

export interface TranscriptToolEvent {
  tool: string;
  round: number | null;
  input: string;
  inputTruncated: boolean;
  output: string;
  outputTruncated: boolean;
  outputChars: number;
  exitCode: number | null;
  durationMs: number | null;
  model: string | null;
}

export interface TranscriptMessage {
  index: number;
  role: string;
  content: string;
  contentTruncated: boolean;
  contentChars: number;
  timestamp: string | null;
  toolEvents: TranscriptToolEvent[];
  toolEventsOmitted: number;
  tokens: { input: number; output: number } | null;
  model: string | null;
}

export interface TranscriptCall {
  seq: number | null;
  model: string | null;
  durationMs: number | null;
  input: number;
  output: number;
  cached: number;
  /** 0..1, this call's total tokens relative to the heaviest call. */
  heat: number;
}

export interface SubagentTranscript {
  sessionId: string;
  name: string;
  model: string;
  offset: number;
  total: number;
  hasMoreAfter: boolean;
  messages: TranscriptMessage[];
  usage: {
    source: 'trace' | 'messages' | 'none';
    inputTokens: number;
    outputTokens: number;
    totalTokens: number;
    calls: TranscriptCall[];
  };
}

const tNum = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const tStr = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));
const tRec = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' ? (v as Record<string, unknown>) : {});

export function subagentTranscriptFrom(raw: Record<string, unknown>): SubagentTranscript {
  const messages = (Array.isArray(raw.messages) ? raw.messages : []).map((m): TranscriptMessage => {
    const r = tRec(m);
    const tok = tRec(r.tokens);
    return {
      index: tNum(r.index) ?? 0,
      role: tStr(r.role),
      content: tStr(r.content),
      contentTruncated: Boolean(r.content_truncated),
      contentChars: tNum(r.content_chars) ?? 0,
      timestamp: r.timestamp == null ? null : tStr(r.timestamp),
      toolEvents: (Array.isArray(r.tool_events) ? r.tool_events : []).map((e): TranscriptToolEvent => {
        const ev = tRec(e);
        return {
          tool: tStr(ev.tool),
          round: tNum(ev.round),
          input: tStr(ev.input),
          inputTruncated: Boolean(ev.input_truncated),
          output: tStr(ev.output),
          outputTruncated: Boolean(ev.output_truncated),
          outputChars: tNum(ev.output_chars) ?? 0,
          exitCode: tNum(ev.exit_code),
          durationMs: tNum(ev.duration_ms),
          model: ev.model == null ? null : tStr(ev.model),
        };
      }).filter((e) => e.tool || e.input),
      toolEventsOmitted: tNum(r.tool_events_omitted) ?? 0,
      tokens: r.tokens ? { input: tNum(tok.input) ?? 0, output: tNum(tok.output) ?? 0 } : null,
      model: r.model == null ? null : tStr(r.model),
    };
  });
  const u = tRec(raw.usage);
  const src = tStr(u.source);
  return {
    sessionId: tStr(raw.session_id),
    name: tStr(raw.name),
    model: tStr(raw.model),
    offset: tNum(raw.offset) ?? 0,
    total: tNum(raw.total) ?? messages.length,
    hasMoreAfter: Boolean(raw.has_more_after),
    messages,
    usage: {
      source: src === 'trace' || src === 'messages' ? src : 'none',
      inputTokens: tNum(u.input_tokens) ?? 0,
      outputTokens: tNum(u.output_tokens) ?? 0,
      totalTokens: tNum(u.total_tokens) ?? 0,
      calls: (Array.isArray(u.per_call) ? u.per_call : []).map((c): TranscriptCall => {
        const r = tRec(c);
        return {
          seq: tNum(r.seq),
          model: r.model == null ? null : tStr(r.model),
          durationMs: tNum(r.duration_ms),
          input: tNum(r.input) ?? 0,
          output: tNum(r.output) ?? 0,
          cached: tNum(r.cached) ?? 0,
          heat: Math.max(0, Math.min(1, tNum(r.heat) ?? 0)),
        };
      }),
    },
  };
}

export async function fetchSubagentTranscript(childSessionId: string, offset = 0, signal?: AbortSignal): Promise<SubagentTranscript> {
  const raw = await getJson<Record<string, unknown>>(
    `/api/chat/subagent/transcript/${encodeURIComponent(childSessionId)}?offset=${Math.max(0, Math.floor(offset))}`,
    signal,
  );
  return subagentTranscriptFrom(raw);
}
