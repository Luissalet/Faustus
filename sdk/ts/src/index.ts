/**
 * `faustus-sdk` — a minimal TypeScript client for the Faustus chat wire.
 * See README.md; CONTRATO_SDK_S2.md is the contract this package
 * implements.
 */
export { FaustusClient } from './client.js';
export type { Turn } from './turn.js';
export { DEFAULT_IDLE_TIMEOUT_MS, DEFAULT_MAX_RESUMES } from './turn.js';

export {
  CLIENT_API_VERSION,
  CLIENT_VERSION_HEADER,
  API_VERSION_HEADER,
  RUN_ID_HEADER,
  IDEMPOTENT_REPLAY_HEADER,
} from './http.js';

export { FaustusApiError, UpgradeRequiredError, QuestionConflictError, RunNotActiveError, StreamAbortedError } from './errors.js';

export { decode } from './sse.js';
export type { SseEvent, DeltaEvent, UnknownEvent } from './sse.js';

export { CORE_EVENT_TYPES, SCHEMA_VERSION } from './events.generated.js';
export type {
  CoreEventType,
  ToolStartEvent,
  ToolProgressEvent,
  ToolOutputEvent,
  ToolEffectEvent,
  AgentStepEvent,
  AskUserEvent,
  AskUserResolvedEvent,
  ToolApprovalResolvedEvent,
  RunActivityEvent,
  MetricsEvent,
  ChatTerminalEvent,
  AgentTerminalEvent,
  ErrorEvent,
  MessageSavedEvent,
  UncertainEvent,
  CapabilitiesChangedEvent,
  PlanUpdateEvent,
  ProgressUpdateEvent,
  HarnessSummaryEvent,
  ContextLedgerEvent,
  WebSourcesEvent,
  ContextReceiptsEvent,
  GitPolicyEvent,
  StrategyEvent,
  FallbackEvent,
} from './events.generated.js';

export type {
  ClientOptions,
  ServerVersion,
  CreateSessionInput,
  Session,
  SessionSummary,
  SessionUpdatePatch,
  SessionUpdateResult,
  SessionExportOptions,
  SessionExportResult,
  AutonomyPreset,
  ToolApprovalDecision,
  StopScope,
  TurnInput,
  AnswerQuestionInput,
  DecideToolApprovalInput,
  StreamOptions,
  TurnEndReason,
  TurnEnd,
  StopResult,
  StreamStatus,
  OpenQuestion,
  ApprovalPlanMeta,
  ApprovalCard,
  ArtifactQuery,
  ArtifactMeta,
  ArtifactManifestResult,
  ArtifactProvenanceResult,
} from './types.js';
