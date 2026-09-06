import { ApiError } from './api';

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
