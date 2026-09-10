import { t } from '../i18n';

/**
 * UX-08: what an `error_class` means, and what to do about it.
 *
 * Before this, a provider failure reached the transcript as whatever raw
 * string the backend happened to raise — sometimes literally a JSON blob —
 * because nothing on this side read the one field that already says what
 * kind of failure it was. `error_class` is OBS-03's ten-category taxonomy
 * (`src/contracts/errors.py`'s `ERROR_CATEGORIES`/`_DEFAULTS`, `category.
 * subcode`, e.g. `transport.llm_service_error`) and every `event: error` SSE
 * chunk has carried it since CALL-06 (`llm_core.py`'s `_stream_error_chunk`).
 * This table is the client-side mirror of that Python module's `_DEFAULTS`:
 * one action per category, in both languages, so a screen can show "Retry"
 * instead of a stack of provider prose. Keep the two in step if the backend
 * taxonomy ever grows an eleventh category.
 */

export const ERROR_CATEGORIES = [
  'capability', 'schema', 'permission', 'resource', 'transport',
  'timeout', 'cancelled', 'conflict', 'verification', 'unknown',
] as const;

export type ErrorCategory = (typeof ERROR_CATEGORIES)[number];

function isErrorCategory(value: string): value is ErrorCategory {
  return (ERROR_CATEGORIES as readonly string[]).includes(value);
}

interface CategoryInfo {
  /** Short label for the failure itself. */
  title: () => string;
  /** What the person can do about it — mirrors `_DEFAULTS`' `next_action`. */
  action: () => string;
  retryable: boolean;
}

const CATEGORY_INFO: Record<ErrorCategory, CategoryInfo> = {
  capability: { title: () => t('Not supported by this model'), action: () => t('Change model'), retryable: false },
  schema: { title: () => t('Invalid request'), action: () => t('Fix and retry'), retryable: false },
  permission: { title: () => t('Needs permission'), action: () => t('Request permission'), retryable: false },
  resource: { title: () => t('Not found'), action: () => t('Check the resource'), retryable: false },
  transport: { title: () => t('Connection problem'), action: () => t('Retry'), retryable: true },
  timeout: { title: () => t('Timed out'), action: () => t('Retry'), retryable: true },
  cancelled: { title: () => t('Stopped'), action: () => t('Resume'), retryable: false },
  conflict: { title: () => t('Out of date'), action: () => t('Reload and reconcile'), retryable: false },
  verification: { title: () => t('Could not verify'), action: () => t('Verify again'), retryable: false },
  unknown: { title: () => t('Something went wrong'), action: () => t('Report the issue'), retryable: false },
};

export interface ErrorDescription {
  /** `null` when `errorClass` is absent or not one of the ten categories —
   *  an older build, or a tool's own opaque code (`ErrorInfo.code` is
   *  deliberately not restricted to the taxonomy; see errors.py). */
  category: ErrorCategory | null;
  subcode: string | null;
  title: string;
  action: string;
  retryable: boolean;
}

/**
 * Describes an `error_class` for display. Never returns the raw code or a
 * blank: a category this table has not met yet still reads as "something
 * went wrong" with a generic "retry" rather than showing nothing, and a
 * missing `errorClass` altogether falls back the same way — a client on a
 * build before this lote, or before `adapters/chat.ts` forwards the field,
 * behaves exactly as it always did.
 */
export function describeError(errorClass: string | undefined | null): ErrorDescription {
  const code = (errorClass ?? '').trim();
  const dot = code.indexOf('.');
  const head = dot === -1 ? code : code.slice(0, dot);
  const info = isErrorCategory(head) ? CATEGORY_INFO[head] : null;
  return {
    category: info ? (head as ErrorCategory) : null,
    subcode: info && dot !== -1 ? code.slice(dot + 1) : null,
    title: info ? info.title() : t('Something went wrong'),
    action: info ? info.action() : t('Retry'),
    retryable: info ? info.retryable : true,
  };
}

/**
 * `describeError` for a value that might BE the raw backend text rather
 * than an already-extracted `error_class` — a task/automation failure
 * reason is sometimes stored as the literal §34.5 error object or
 * `_stream_error_chunk` payload (`{"error": "...", "error_class":
 * "transport.llm_service_error", ...}`), which is exactly the "provider
 * error is a JSON blob on screen" UX-08 names. Plain text (no leading `{`,
 * or JSON without `error_class`) passes through unchanged with `category:
 * null`, so a caller can choose to show just `message` in that case.
 */
export function friendlyError(raw: string | null | undefined): ErrorDescription & { message: string } {
  const text = (raw ?? '').trim();
  let errorClass: string | undefined;
  let message = text;
  if (text.startsWith('{')) {
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>;
      if (typeof parsed.error_class === 'string') errorClass = parsed.error_class;
      const inner = parsed.text ?? parsed.error ?? parsed.message;
      if (typeof inner === 'string' && inner) message = inner;
    } catch {
      /* not JSON after all: show the text as-is */
    }
  }
  return { ...describeError(errorClass), message };
}
