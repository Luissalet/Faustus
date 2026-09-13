/**
 * Public request/response shapes — CONTRATO_SDK_S2.md § S2.2.
 *
 * Read-model interfaces (`SessionSummary`, `ArtifactMeta`, `ApprovalCard`,
 * `OpenQuestion`) are deliberately thin: field names mirror the server's
 * own JSON keys (snake_case) rather than reinventing a camelCase mapping
 * layer, since this package's job is to be a faithful client, not a second
 * copy of `studio/src/adapters/*.ts`'s UI-shaping. Command inputs
 * (`TurnInput`, `CreateSessionInput`, constructor options) use camelCase,
 * since those are this library's own ergonomic surface, not a passthrough
 * of a server response.
 */

export interface ClientOptions {
  /** Origin the server is reachable at, e.g. `https://faustus.example`. No
   *  trailing slash required — one is added if missing. */
  baseUrl: string;
  /** `Authorization: Bearer <token>` — an `ody_…` token with the `sessions`
   *  scope. Mutually usable with `cookie`; when `token` is set, no cookie
   *  credentials are sent (see the constructor's own doc comment). */
  token?: string;
  /** Sent verbatim as the `Cookie` request header — `'odysseus_session=…'`.
   *  Useful from Node, where there is no browser cookie jar. In a browser,
   *  pass `credentials` instead and let the browser manage the cookie. */
  cookie?: string;
  /** `RequestCredentials` for a browser `fetch` (e.g. `'include'`) — the
   *  browser owns the cookie jar there, this library does not read or set
   *  cookies itself in that environment. */
  credentials?: RequestCredentials;
  /** `X-Faustus-Client-Version`, sent on every request. Defaults to
   *  `CLIENT_API_VERSION` ("2.0") — the version this build of the SDK was
   *  written against. */
  clientVersion?: string;
  /** Defaults to the global `fetch` (Node ≥18, or a browser). Override for
   *  a custom agent, logging, or a test double. */
  fetch?: typeof fetch;
  /** Applied via `AbortSignal.timeout()` to every non-streaming request
   *  (session/question/approval/artifact calls, and the initial POST/GET
   *  that opens a turn's stream — NOT the stream's own idle timeout, which
   *  is `StreamOptions.idleTimeoutMs`). `undefined` (the default): no
   *  per-request timeout beyond whatever `opts.signal` the caller passed. */
  defaultTimeoutMs?: number;
}

export interface ServerVersion {
  version: string;
  build: { sha?: string; date?: string };
  served_studio: unknown;
  client_adaptation_notice: string | null;
}

/* ── Sessions ── */

export interface CreateSessionInput {
  name?: string;
  endpointUrl?: string;
  model?: string;
  rag?: boolean;
  /** Trust the caller and skip the server's `/v1/models` probe — needed for
   *  a session with no reachable endpoint yet, or a custom one. */
  skipValidation?: boolean;
  apiKey?: string;
  endpointId?: string;
}

/** `POST /api/session`'s response — `src/request_models.py::SessionResponse`. */
export interface Session {
  id: string;
  name: string;
  model: string;
  rag: boolean;
  archived: boolean;
}

/** One element of `GET /api/sessions`'s bare array response. */
export interface SessionSummary {
  id: string;
  name: string;
  folder: string | null;
  tokens: number;
  is_important: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_message_at: string | null;
  has_documents: boolean;
  has_images: boolean;
  mode: string | null;
  message_count: number;
}

export interface SessionUpdatePatch {
  name?: string;
  folder?: string;
  model?: string;
  endpointUrl?: string;
  endpointId?: string;
}

/**
 * `PATCH /api/session/{sid}`'s response — an echo of the fields that were
 * actually set, not a full `Session` (`routes/session_routes.py::
 * rename_session` builds `result` up field by field; see this SDK's final
 * report for why `update()` returns this rather than `Session`).
 */
export interface SessionUpdateResult {
  id: string;
  name?: string;
  folder?: string | null;
  model?: string;
  endpoint_url?: string;
  capabilities?: Record<string, unknown>;
  lost?: unknown[];
}

/* ── Turns ── */

export type AutonomyPreset = 'supervised' | 'bounded_autonomous' | 'read_only';
export type ToolApprovalDecision = 'approve' | 'approve_task' | 'deny';
export type StopScope = 'generation' | 'task' | 'work';

export interface TurnInput {
  message: string;
  mode: 'chat' | 'agent';
  planMode?: boolean;
  allowBash?: boolean;
  allowWebSearch?: boolean;
  useWeb?: boolean;
  useRag?: boolean;
  workspace?: string;
  /** Upload ids already on the server (`/api/upload`), not raw bytes. */
  attachments?: string[];
  selectedModel?: string;
  selectedEndpointUrl?: string;
  selectedEndpointId?: string;
  genOverrides?: Record<string, number | boolean | string>;
  autonomyPreset?: AutonomyPreset;
  behaviorMode?: string;
  presetId?: string;
  incognito?: boolean;
  noMemory?: boolean;
  noSkills?: boolean;
  inputTokenBudget?: number;
  /** Reuse an id instead of minting one with `crypto.randomUUID()` — pass
   *  the same value back to retry an unacknowledged send safely: the
   *  server reconnects to the turn it already started instead of running
   *  the message twice (`X-Faustus-Idempotent-Replay` on the response, and
   *  `Turn.replayed`, say which happened). */
  clientMessageId?: string;
}

export interface AnswerQuestionInput {
  questionId: string;
  revision?: number;
  optionIds?: string[];
  text?: string;
  /** ask_user only fires mid agent turn; defaults to `'agent'`. */
  mode?: 'chat' | 'agent';
}

export interface DecideToolApprovalInput {
  approvalId: string;
  decision: ToolApprovalDecision;
  mode?: 'chat' | 'agent';
}

export interface StreamOptions {
  signal?: AbortSignal;
  /** No bytes at all for this long ⇒ treat the connection as broken and
   *  reconnect (heartbeats arrive every 10s server-side, so this is
   *  normally only reached by a real stall). Default 60000. */
  idleTimeoutMs?: number;
  /** Reconnect attempts a single `Turn` will make over its whole lifetime
   *  before giving up with `{reason: 'error'}`. Default 5. */
  maxResumes?: number;
}

export type TurnEndReason = 'done' | 'stopped' | 'run_gone' | 'error' | 'aborted';

export interface TurnEnd {
  reason: TurnEndReason;
  lastSequence: number;
  error?: unknown;
}

/** `POST /api/chat/stop/{sid}`'s response — shape depends on `scope`
 *  (`routes/chat_routes.py::chat_stop`): no scope ⇒ only `stopped`;
 *  `'generation'` ⇒ `paused` instead of a real stop; `'task'`/`'work'` ⇒
 *  the full cleanup receipt. Every field the server can send is optional
 *  here rather than three separate response types, since a caller that
 *  didn't pass a scope still gets back whatever the server actually sent. */
export interface StopResult {
  scope?: StopScope;
  stopped: boolean;
  paused?: boolean;
  cancelled_at?: number;
  what_ran_before?: unknown;
  cleanup?: {
    own_process_tree?: string;
    subagents_stopped?: string[];
    workers_stopped?: string[][];
    questions_cancelled?: string[];
    approvals_retired?: string[];
    bg_jobs_cancelled?: string[];
  };
}

export interface StreamStatus {
  status: string;
  detached?: boolean;
}

/* ── Questions & approvals ── */

/** One element of `GET /api/questions`'s `questions` array. */
export interface OpenQuestion {
  question_id: string;
  session: string;
  question: string;
  options: unknown[];
  multi: boolean;
  expires_at: string | null;
  revision: number;
  opened_at: string;
}

/** `src/contracts/approval.py::ApprovalPlan.to_dict()`. */
export interface ApprovalPlanMeta {
  action: string;
  skill_id: string;
  skill_version: string;
  backend: string;
  recipients: string[];
  cost_units: number | null;
  secret_names: string[];
  permissions: Record<string, unknown>;
  output_kinds: string[];
  detail: string;
}

/** One card from `GET /api/approvals/pending|active`. */
export interface ApprovalCard {
  id: string;
  plan: ApprovalPlanMeta;
  plan_fingerprint: string;
  status: string;
  owner: string;
  requested_at: string;
  decided_at: string | null;
  decided_by: string;
  expires_at: string | null;
  uses_left: number;
  reason: string;
}

/* ── Artifacts ── */

export interface ArtifactQuery {
  projectId?: string;
  sessionId?: string;
  kind?: string;
  q?: string;
  since?: string;
  until?: string;
  limit?: number;
}

/** `routes/artifact_routes.py::_metadata()`. */
export interface ArtifactMeta {
  id: string;
  kind: string;
  label: string;
  sha256: string;
  byte_size: number;
  media_type: string;
  project_id: string;
  run_id: string;
  session_id: string;
  partial: boolean;
  created_at: string;
  download_url: string;
}

export interface ArtifactManifestResult {
  ok: boolean;
  artifact_id: string;
  manifest: unknown;
}

export interface ArtifactProvenanceResult {
  ok: boolean;
  artifact_id: string;
  provenance: {
    backend: string | null;
    recipe: string | null;
    recipe_fingerprint: string | null;
    inputs_digest: string | null;
    source_artifact_ids: unknown[];
    note: string;
  };
}
