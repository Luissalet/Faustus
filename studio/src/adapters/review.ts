import { ApiError, asArray, getJson } from './api';

/**
 * Review mode (VER-06, services/review_state.py + routes/workspace_routes.py
 * `/api/workspace/review/{message_id}`): per-turn accept/reject of files the
 * agent changed. A human's "accept" is a distinct fact from an automatic
 * test run — this adapter keeps them separate the way the backend does
 * (`humanApproved` vs `testsStatus`), and surfaces `stale` when the file
 * moved on after the approval was captured, so an old approval never reads
 * as still covering the current content.
 */

export interface ReviewApproval {
  path: string;
  at: number | null;
  diffSha256: string | null;
  /** True when the file's content no longer matches what was approved. */
  stale: boolean;
}

export interface ReviewTestsStatus {
  ran: boolean;
  ok: boolean;
  inconclusive: boolean;
}

export interface ReviewState {
  messageId: string;
  sessionId: string | null;
  workspace: string;
  checkpoint: string | null;
  pending: string[];
  accepted: string[];
  rejected: string[];
  approvals: ReviewApproval[];
  /** null when no automatic verification ran for this turn at all. */
  testsStatus: ReviewTestsStatus | null;
}

async function send(path: string, method: string, body?: unknown): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method,
    signal: AbortSignal.timeout(20000),
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try {
      detail = String(((await response.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      detail = '';
    }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function testsStatusFrom(raw: unknown): ReviewTestsStatus | null {
  if (!raw || typeof raw !== 'object') return null;
  const t = raw as Record<string, unknown>;
  if (!t.ran) return { ran: false, ok: false, inconclusive: false };
  return { ran: true, ok: Boolean(t.ok), inconclusive: Boolean(t.inconclusive) };
}

/** Exported for tests (studio/checks/l69b-ver06-review.check.mjs): pure parsing, no fetch. */
export function reviewStateFrom(raw: Record<string, unknown>): ReviewState {
  return stateFrom(raw);
}

function stateFrom(raw: Record<string, unknown>): ReviewState {
  return {
    messageId: String(raw.message_id ?? ''),
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : null,
    workspace: String(raw.workspace ?? ''),
    checkpoint: typeof raw.checkpoint === 'string' ? raw.checkpoint : null,
    pending: asArray<unknown>(raw.pending).map(String),
    accepted: asArray<unknown>(raw.accepted).map(String),
    rejected: asArray<unknown>(raw.rejected).map(String),
    approvals: asArray<Record<string, unknown>>(raw.approvals).map((a) => ({
      path: String(a.path ?? ''),
      at: typeof a.at === 'number' ? a.at : null,
      diffSha256: typeof a.diff_sha256 === 'string' ? a.diff_sha256 : null,
      stale: Boolean(a.stale),
    })),
    testsStatus: testsStatusFrom(raw.tests_status),
  };
}

const enc = encodeURIComponent;

/** Throws ApiError(404) when this message has no review entry (not in review mode, or nothing changed). */
export async function getReviewState(messageId: string, signal?: AbortSignal): Promise<ReviewState> {
  return stateFrom(await getJson<Record<string, unknown>>(`/api/workspace/review/${enc(messageId)}`, signal));
}

export type ReviewDecision = 'accept' | 'reject';

export async function decideReview(messageId: string, path: string, decision: ReviewDecision): Promise<ReviewState> {
  const raw = await send(`/api/workspace/review/${enc(messageId)}/decide`, 'POST', { path, decision });
  return stateFrom((raw.state ?? {}) as Record<string, unknown>);
}

export interface WorkspaceFileText {
  text: string;
  binary: boolean;
  exists: boolean;
}

/** The file's content right now. */
export async function readWorkspaceFile(workspace: string, path: string, signal?: AbortSignal): Promise<WorkspaceFileText> {
  const raw = await getJson<Record<string, unknown>>(`/api/workspace/file?workspace=${enc(workspace)}&path=${enc(path)}`, signal);
  return { text: String(raw.text ?? ''), binary: Boolean(raw.binary), exists: true };
}

/** The file's content at the turn's checkpoint (the "before" pane). */
export async function readCheckpointFile(workspace: string, checkpoint: string, path: string, signal?: AbortSignal): Promise<WorkspaceFileText> {
  const raw = await getJson<Record<string, unknown>>(`/api/workspace/checkpoint/file?workspace=${enc(workspace)}&sha=${enc(checkpoint)}&path=${enc(path)}`, signal);
  return { text: String(raw.text ?? ''), binary: Boolean(raw.binary), exists: Boolean(raw.exists) };
}
