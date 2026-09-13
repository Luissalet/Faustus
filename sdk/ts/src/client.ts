/**
 * `FaustusClient` — the public entry point. CONTRATO_SDK_S2.md § S2.2.
 */
import { FaustusApiError, QuestionConflictError, UpgradeRequiredError } from './errors.js';
import { HttpContext, IDEMPOTENT_REPLAY_HEADER, RUN_ID_HEADER, describeFailure } from './http.js';
import { publicResume, rawStop, rawStreamStatus, TurnImpl, type Turn } from './turn.js';
import type {
  AnswerQuestionInput,
  ApprovalCard,
  ArtifactManifestResult,
  ArtifactMeta,
  ArtifactProvenanceResult,
  ArtifactQuery,
  ClientOptions,
  CreateSessionInput,
  DecideToolApprovalInput,
  OpenQuestion,
  ServerVersion,
  Session,
  SessionSummary,
  SessionUpdatePatch,
  SessionUpdateResult,
  StopResult,
  StopScope,
  StreamOptions,
  StreamStatus,
  ToolApprovalDecision,
  TurnInput,
} from './types.js';

function newClientMessageId(): string {
  const c = (globalThis as unknown as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  // Node 18 has `globalThis.crypto` too, but a custom `fetch`/test double
  // might run this outside any of that — fall back rather than throw.
  return `cid_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

/** `TurnInput` plus the fields only `answerQuestion`/`decideToolApproval`
 *  set — kept out of the public `TurnInput` type since a plain
 *  `turns.create()` caller has no business setting them. */
interface InternalTurnFields extends TurnInput {
  questionId?: string;
  optionIds?: string[];
  revision?: number;
  toolApprovalId?: string;
  toolApprovalDecision?: ToolApprovalDecision;
}

/** Builds the `/api/chat_stream` request body — multipart when attachments
 *  are present (the server only reads `attachments` off `form_data`, never
 *  the JSON body — see this SDK's final report), JSON otherwise, per
 *  CONTRATO_SDK_S2.md's "usa JSON salvo que haya adjuntos". */
function buildTurnRequestInit(sessionId: string, fields: InternalTurnFields, clientMessageId: string): RequestInit {
  const hasAttachments = Boolean(fields.attachments && fields.attachments.length);

  if (hasAttachments) {
    const fd = new FormData();
    fd.append('session', sessionId);
    fd.append('message', fields.message);
    fd.append('mode', fields.mode);
    fd.append('client_message_id', clientMessageId);
    if (fields.planMode) fd.append('plan_mode', 'true');
    if (fields.allowBash) fd.append('allow_bash', 'true');
    if (fields.allowWebSearch) fd.append('allow_web_search', 'true');
    if (fields.useWeb) fd.append('use_web', 'true');
    if (fields.useRag) fd.append('use_rag', 'true');
    if (fields.workspace) fd.append('workspace', fields.workspace);
    if (fields.attachments?.length) fd.append('attachments', JSON.stringify(fields.attachments));
    if (fields.selectedModel) fd.append('selected_model', fields.selectedModel);
    if (fields.selectedEndpointUrl) fd.append('selected_endpoint_url', fields.selectedEndpointUrl);
    if (fields.selectedEndpointId) fd.append('selected_endpoint_id', fields.selectedEndpointId);
    if (fields.genOverrides && Object.keys(fields.genOverrides).length) {
      fd.append('gen_overrides', JSON.stringify(fields.genOverrides));
    }
    if (fields.autonomyPreset) fd.append('autonomy_preset', fields.autonomyPreset);
    if (fields.behaviorMode) fd.append('behavior_mode', fields.behaviorMode);
    if (fields.presetId) fd.append('preset_id', fields.presetId);
    if (fields.incognito) fd.append('incognito', 'true');
    if (fields.noMemory) fd.append('no_memory', 'true');
    if (fields.noSkills) fd.append('no_skills', 'true');
    if (fields.inputTokenBudget != null) fd.append('input_token_budget', String(fields.inputTokenBudget));
    if (fields.questionId) fd.append('question_id', fields.questionId);
    if (fields.optionIds?.length) fd.append('option_ids', JSON.stringify(fields.optionIds));
    if (fields.revision != null) fd.append('revision', String(fields.revision));
    if (fields.toolApprovalId) fd.append('tool_approval_id', fields.toolApprovalId);
    if (fields.toolApprovalDecision) fd.append('tool_approval_decision', fields.toolApprovalDecision);
    fd.append('compare_mode', 'false');
    return { method: 'POST', body: fd };
  }

  const json: Record<string, unknown> = {
    session: sessionId,
    message: fields.message,
    mode: fields.mode,
    client_message_id: clientMessageId,
    // Always the literal string "false" (never a JSON boolean, never
    // omitted) — CONTRATO_SDK_S2.md: "compare_mode (envía \"false\"
    // siempre, el modo detached)". The server accepts either shape
    // (`str(value).lower() == "true"`), but the string keeps both wire
    // paths (this one and the multipart one below) byte-identical.
    compare_mode: 'false',
  };
  if (fields.planMode) json.plan_mode = true;
  if (fields.allowBash) json.allow_bash = true;
  if (fields.allowWebSearch) json.allow_web_search = true;
  if (fields.useWeb) json.use_web = true;
  if (fields.useRag) json.use_rag = true;
  if (fields.workspace) json.workspace = fields.workspace;
  if (fields.selectedModel) json.selected_model = fields.selectedModel;
  if (fields.selectedEndpointUrl) json.selected_endpoint_url = fields.selectedEndpointUrl;
  if (fields.selectedEndpointId) json.selected_endpoint_id = fields.selectedEndpointId;
  if (fields.genOverrides && Object.keys(fields.genOverrides).length) json.gen_overrides = fields.genOverrides;
  if (fields.autonomyPreset) json.autonomy_preset = fields.autonomyPreset;
  if (fields.behaviorMode) json.behavior_mode = fields.behaviorMode;
  if (fields.presetId) json.preset_id = fields.presetId;
  if (fields.incognito) json.incognito = true;
  if (fields.noMemory) json.no_memory = true;
  if (fields.noSkills) json.no_skills = true;
  if (fields.inputTokenBudget != null) json.input_token_budget = fields.inputTokenBudget;
  if (fields.questionId) json.question_id = fields.questionId;
  if (fields.optionIds?.length) json.option_ids = fields.optionIds;
  if (fields.revision != null) json.revision = fields.revision;
  if (fields.toolApprovalId) json.tool_approval_id = fields.toolApprovalId;
  if (fields.toolApprovalDecision) json.tool_approval_decision = fields.toolApprovalDecision;
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json) };
}

async function postTurn(
  http: HttpContext,
  sessionId: string,
  fields: InternalTurnFields,
  opts: StreamOptions = {},
): Promise<Turn> {
  const clientMessageId = fields.clientMessageId ?? newClientMessageId();
  const init = buildTurnRequestInit(sessionId, fields, clientMessageId);
  const response = await http.request('/api/chat_stream', init, opts.signal);

  if (response.status === 426) {
    const { detail, body } = await describeFailure(response);
    throw new UpgradeRequiredError(detail, body);
  }
  if (response.status === 409) {
    type QuestionConflictBody = { error?: unknown; reason?: unknown; question_id?: unknown; detail?: unknown };
    let payload: QuestionConflictBody | null = null;
    try {
      payload = (await response.clone().json()) as QuestionConflictBody;
    } catch {
      payload = null;
    }
    if (payload && payload.error === 'question_not_resolved') {
      throw new QuestionConflictError(
        String(payload.reason ?? 'unknown'),
        String(payload.question_id ?? ''),
        String(payload.detail ?? ''),
        payload,
      );
    }
  }
  if (!response.ok || !response.body) {
    const info = await describeFailure(response);
    throw new FaustusApiError(info.message, response.status, info);
  }

  const runId = response.headers.get(RUN_ID_HEADER);
  const replayed = response.headers.get(IDEMPOTENT_REPLAY_HEADER) === '1';
  return new TurnImpl({ http, sessionId, runId, replayed, startSequence: 0, options: opts }, response.body);
}

function sessionCreateForm(input: CreateSessionInput): URLSearchParams {
  const p = new URLSearchParams();
  p.set('name', input.name ?? '');
  p.set('endpoint_url', input.endpointUrl ?? '');
  p.set('model', input.model ?? '');
  if (input.rag != null) p.set('rag', input.rag ? 'true' : 'false');
  if (input.skipValidation) p.set('skip_validation', 'true');
  p.set('api_key', input.apiKey ?? '');
  if (input.endpointId) p.set('endpoint_id', input.endpointId);
  return p;
}

function sessionUpdateForm(patch: SessionUpdatePatch): URLSearchParams {
  const p = new URLSearchParams();
  if (patch.name !== undefined) p.set('name', patch.name);
  if (patch.folder !== undefined) p.set('folder', patch.folder);
  if (patch.model !== undefined) p.set('model', patch.model);
  if (patch.endpointUrl !== undefined) p.set('endpoint_url', patch.endpointUrl);
  if (patch.endpointId !== undefined) p.set('endpoint_id', patch.endpointId);
  return p;
}

function artifactQueryString(q?: ArtifactQuery): string {
  if (!q) return '';
  const p = new URLSearchParams();
  if (q.projectId) p.set('project_id', q.projectId);
  if (q.sessionId) p.set('session_id', q.sessionId);
  if (q.kind) p.set('kind', q.kind);
  if (q.q) p.set('q', q.q);
  if (q.since) p.set('since', q.since);
  if (q.until) p.set('until', q.until);
  if (q.limit != null) p.set('limit', String(q.limit));
  const s = p.toString();
  return s ? `?${s}` : '';
}

export class FaustusClient {
  private readonly http: HttpContext;

  readonly sessions: {
    create(input: CreateSessionInput): Promise<Session>;
    list(): Promise<SessionSummary[]>;
    update(id: string, patch: SessionUpdatePatch): Promise<SessionUpdateResult>;
    remove(id: string): Promise<void>;
  };

  readonly turns: {
    create(sessionId: string, input: TurnInput, opts?: StreamOptions): Promise<Turn>;
    resume(sessionId: string, opts?: { cursor?: number } & StreamOptions): Promise<Turn>;
    stop(sessionId: string, opts?: { runId?: string; scope?: StopScope }): Promise<StopResult>;
    answerQuestion(sessionId: string, input: AnswerQuestionInput, opts?: StreamOptions): Promise<Turn>;
    decideToolApproval(sessionId: string, input: DecideToolApprovalInput, opts?: StreamOptions): Promise<Turn>;
    status(sessionId: string): Promise<StreamStatus | null>;
  };

  readonly questions: { list(): Promise<OpenQuestion[]> };

  readonly approvals: {
    pending(): Promise<ApprovalCard[]>;
    active(): Promise<ApprovalCard[]>;
  };

  readonly artifacts: {
    list(query?: ArtifactQuery): Promise<ArtifactMeta[]>;
    get(id: string): Promise<ArtifactMeta>;
    download(id: string): Promise<Uint8Array>;
    manifest(id: string): Promise<ArtifactManifestResult>;
    provenance(id: string): Promise<ArtifactProvenanceResult>;
  };

  constructor(opts: ClientOptions) {
    const http = new HttpContext(opts);
    this.http = http;

    this.sessions = {
      create: (input) => http.createSession(sessionCreateForm(input)),
      list: () => http.listSessions(),
      update: (id, patch) => http.updateSession(id, sessionUpdateForm(patch)),
      remove: (id) => http.removeSession(id),
    };

    this.turns = {
      create: (sessionId, input, opts) => postTurn(http, sessionId, input, opts),
      resume: (sessionId, opts = {}) => publicResume(http, sessionId, opts),
      stop: (sessionId, opts = {}) => rawStop(http, sessionId, opts.runId, opts.scope),
      answerQuestion: (sessionId, input, opts) =>
        postTurn(
          http,
          sessionId,
          {
            message: input.text ?? '',
            mode: input.mode ?? 'agent',
            questionId: input.questionId,
            optionIds: input.optionIds,
            revision: input.revision,
          },
          opts,
        ),
      decideToolApproval: (sessionId, input, opts) =>
        postTurn(
          http,
          sessionId,
          {
            message: '',
            mode: input.mode ?? 'agent',
            toolApprovalId: input.approvalId,
            toolApprovalDecision: input.decision,
          },
          opts,
        ),
      status: async (sessionId) => (await rawStreamStatus(http, sessionId)) as StreamStatus | null,
    };

    this.questions = {
      list: async () => {
        const r = await http.requestJson<{ questions: OpenQuestion[]; count: number }>('/api/questions');
        return r.questions;
      },
    };

    this.approvals = {
      pending: async () => {
        const r = await http.requestJson<{ checked_at: string; pending: ApprovalCard[]; count: number }>(
          '/api/approvals/pending',
        );
        return r.pending;
      },
      active: async () => {
        const r = await http.requestJson<{ checked_at: string; active: ApprovalCard[]; count: number }>(
          '/api/approvals/active',
        );
        return r.active;
      },
    };

    this.artifacts = {
      list: async (query) => {
        const r = await http.requestJson<{ ok: boolean; artifacts: ArtifactMeta[] }>(
          `/api/artifacts${artifactQueryString(query)}`,
        );
        return r.artifacts;
      },
      get: async (id) => {
        const r = await http.requestJson<{ ok: boolean; artifact: ArtifactMeta }>(`/api/artifacts/${encodeURIComponent(id)}`);
        return r.artifact;
      },
      download: async (id) => {
        const response = await http.request(`/api/artifacts/${encodeURIComponent(id)}/download`);
        if (!response.ok) {
          const info = await describeFailure(response);
          throw new FaustusApiError(info.message, response.status, info);
        }
        return new Uint8Array(await response.arrayBuffer());
      },
      manifest: (id) => http.requestJson<ArtifactManifestResult>(`/api/artifacts/${encodeURIComponent(id)}/manifest`),
      provenance: (id) => http.requestJson<ArtifactProvenanceResult>(`/api/artifacts/${encodeURIComponent(id)}/provenance`),
    };
  }

  version(): Promise<ServerVersion> {
    return this.http.version();
  }
}
