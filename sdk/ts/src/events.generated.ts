/**
 * GENERATED — do not edit by hand.
 *
 * Produced by `sdk/ts/scripts/gen-events.mjs` from
 * `docs/api/sse_events.json` (schema_version 2.0).
 * Run `npm run gen:events` after that file changes; `npm run check` fails
 * the build if this file has drifted from it.
 */

/** `tool_start` (core).  */
export interface ToolStartEvent {
  type: 'tool_start';
  tool: string;
  command?: string;
  full_command?: string;
  round?: number;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `tool_progress` (core).  */
export interface ToolProgressEvent {
  type: 'tool_progress';
  tool?: string;
  message?: string;
  event?: string;
  subagent?: Record<string, unknown>;
  swarm?: Record<string, unknown>;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `tool_output` (core).  */
export interface ToolOutputEvent {
  type: 'tool_output';
  tool: string;
  command?: string;
  output?: string;
  exit_code?: number;
  diff?: string;
  doc_id?: string;
  screenshot?: string;
  argument_errors?: unknown[];
  repairs?: unknown[];
  evidence_refs?: unknown[];
  execution_target?: Record<string, unknown>;
  call_id?: string;
  duration_ms?: number;
  turn_elapsed_ms?: number;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `tool_effect` (core).  */
export interface ToolEffectEvent {
  type: 'tool_effect';
  call_id: string;
  tool: string;
  effect_class: string;
  state: 'pending' | 'confirmed' | 'failed';
  idempotency_key: string;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `agent_step` (core).  */
export interface AgentStepEvent {
  type: 'agent_step';
  round: number;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `ask_user` (core). answered by the next POST /api/chat_stream: question_id+option_ids/message+revision, or tool_approval_id+tool_approval_decision (approve|approve_task|deny) */
export interface AskUserEvent {
  type: 'ask_user';
  data: {
    question: string;
    options: unknown[];
    multi?: boolean;
    kind: 'question' | 'tool_approval';
    approval_id?: string;
    question_id?: string;
    revision?: number;
    allow_free_text?: boolean;
  };
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `ask_user_resolved` (core).  */
export interface AskUserResolvedEvent {
  type: 'ask_user_resolved';
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `tool_approval_resolved` (core).  */
export interface ToolApprovalResolvedEvent {
  type: 'tool_approval_resolved';
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `run_activity` (core). also the heartbeat: synthesized after 10 s of silence */
export interface RunActivityEvent {
  type: 'run_activity';
  data: {
    phase: string;
    phase_since?: number;
    tool?: string;
    detail?: string;
    model_state?: string;
    round?: number;
  };
  heartbeat?: number;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `metrics` (core).  */
export interface MetricsEvent {
  type: 'metrics';
  data: Record<string, unknown>;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `chat_terminal` (core). clean end of a chat turn */
export interface ChatTerminalEvent {
  type: 'chat_terminal';
  data: Record<string, unknown>;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `agent_terminal` (core). end of an agent turn, failed when `failure` is present */
export interface AgentTerminalEvent {
  type: 'agent_terminal';
  data: {
    failure?: { message: string; error_class: string; };
  };
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `error` (core).  */
export interface ErrorEvent {
  type: 'error';
  text?: string;
  error?: string;
  message?: string;
  detail?: string;
  error_class?: string;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `message_saved` (core).  */
export interface MessageSavedEvent {
  type: 'message_saved';
  id: string;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `uncertain` (core). idempotent replay could not find the run but its outbox row is still accepted/running */
export interface UncertainEvent {
  type: 'uncertain';
  status: string;
  detail?: string;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `capabilities_changed` (core).  */
export interface CapabilitiesChangedEvent {
  type: 'capabilities_changed';
  data: {
    from_model: string;
    to_model: string;
    lost: unknown[];
  };
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `plan_update` (core).  */
export interface PlanUpdateEvent {
  type: 'plan_update';
  data: {
    plan: unknown;
    steps: unknown[];
    revision: number;
    warnings: unknown[];
  };
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `progress_update` (core).  */
export interface ProgressUpdateEvent {
  type: 'progress_update';
  todos: unknown[];
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `harness_summary` (core).  */
export interface HarnessSummaryEvent {
  type: 'harness_summary';
  data: Record<string, unknown>;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `context_ledger` (core).  */
export interface ContextLedgerEvent {
  type: 'context_ledger';
  data: Record<string, unknown>;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `web_sources` (core).  */
export interface WebSourcesEvent {
  type: 'web_sources';
  data: unknown[];
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `context_receipts` (core).  */
export interface ContextReceiptsEvent {
  type: 'context_receipts';
  data: unknown[];
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `git_policy` (core).  */
export interface GitPolicyEvent {
  type: 'git_policy';
  action: 'branch' | 'commit' | 'push';
  ok: boolean;
  branch?: string;
  sha?: string;
  detail?: string;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `strategy` (core).  */
export interface StrategyEvent {
  type: 'strategy';
  data: Record<string, unknown>;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** `fallback` (core).  */
export interface FallbackEvent {
  type: 'fallback';
  answered_by?: string;
  selected_model?: string;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

export const CORE_EVENT_TYPES = [
  'tool_start',
  'tool_progress',
  'tool_output',
  'tool_effect',
  'agent_step',
  'ask_user',
  'ask_user_resolved',
  'tool_approval_resolved',
  'run_activity',
  'metrics',
  'chat_terminal',
  'agent_terminal',
  'error',
  'message_saved',
  'uncertain',
  'capabilities_changed',
  'plan_update',
  'progress_update',
  'harness_summary',
  'context_ledger',
  'web_sources',
  'context_receipts',
  'git_policy',
  'strategy',
  'fallback',
] as const;

export type CoreEventType = (typeof CORE_EVENT_TYPES)[number];

export const SCHEMA_VERSION = '2.0';
