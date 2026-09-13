/**
 * Typed errors the client throws — CONTRATO_SDK_S2.md § S2.2.
 *
 * `FaustusApiError` carries whatever the server actually said (`detail`,
 * `errorClass`, the parsed body) instead of a message this library invented
 * by inspecting the text. See `http.ts`'s `describeFailure` for how that
 * detail is read — `detail` (string, or `{message}`), then `error`, then
 * `message`, never a substring match against the text (the same guard
 * `tests/test_studio_guards.py::test_no_substring_guessing_about_why_a_
 * request_failed` enforces on Studio).
 */

/** The server rejected a request; `detail` is exactly what it said. */
export class FaustusApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly errorClass?: string;
  readonly body?: unknown;

  constructor(message: string, status: number, opts: { detail?: string; errorClass?: string; body?: unknown } = {}) {
    super(message);
    this.name = 'FaustusApiError';
    this.status = status;
    this.detail = opts.detail ?? message;
    this.errorClass = opts.errorClass;
    this.body = opts.body;
  }
}

/** 426: this client identified itself (`X-Faustus-Client-Version`) as older
 *  than the server's floor. `detail` is the server's own actionable text. */
export class UpgradeRequiredError extends FaustusApiError {
  constructor(detail: string, body?: unknown) {
    super(detail, 426, { detail, body });
    this.name = 'UpgradeRequiredError';
  }
}

/** 409 `question_not_resolved` answering an `ask_user` question: the server
 *  refused this specific answer and said why (`reason` — `cancelled`,
 *  `stale_revision`, `already_answered`, `expired`, `not_found`, or an
 *  unrecognised value from a newer server). */
export class QuestionConflictError extends FaustusApiError {
  readonly reason: string;
  readonly questionId: string;

  constructor(reason: string, questionId: string, detail: string, body?: unknown) {
    super(detail || `question_not_resolved: ${reason}`, 409, { detail, body });
    this.name = 'QuestionConflictError';
    this.reason = reason;
    this.questionId = questionId;
  }
}

/** 404 from `GET /api/chat/resume/{sid}` or `GET /api/chat/stream_status/
 *  {sid}`: no run is currently active for this session — it finished, or
 *  died, before this call reached it. Not thrown by a `Turn`'s own internal
 *  reconnect (that ends the turn with `{reason: 'run_gone'}` instead — see
 *  `turn.ts`); only by the standalone `client.turns.resume()`/`status()`
 *  calls, which have no turn in progress to end. */
export class RunNotActiveError extends FaustusApiError {
  constructor(sessionId: string) {
    super(`No active run for session ${sessionId}`, 404, { detail: 'No active run for this session' });
    this.name = 'RunNotActiveError';
  }
}

/** The caller's own `AbortSignal` fired while a `Turn` was reading its
 *  stream. Never thrown across the public API — `Turn.done` resolves with
 *  `{reason: 'aborted'}` instead — but used internally to tell "the reader
 *  was cancelled on purpose" apart from "the connection broke". */
export class StreamAbortedError extends Error {
  constructor() {
    super('The stream was aborted by the caller.');
    this.name = 'StreamAbortedError';
  }
}
