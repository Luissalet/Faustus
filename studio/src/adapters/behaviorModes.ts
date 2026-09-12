import { ApiError, responseReason } from './api';
import { t } from '../i18n';
import type { Lang } from '../i18n';

/**
 * Lote B (CONTRATO_MODOS.md) — behaviour modes, a thin typed mirror of
 * Lote A's backend: `src/behavior_modes.py` (`Mode.to_dict()`,
 * `check_response()`) and `routes/behavior_mode_routes.py` (the routes and
 * request bodies this file must never drift from).
 *
 * A behaviour mode is a conversational STANCE the user picks ("how Faustus
 * argues"), never a change to what it is allowed to do — orthogonal to the
 * Chat/Agent mode, task presets, the model, the project and the skills.
 * `check_response`'s result is a heuristic DETECTOR, never a judge: "the
 * model did not follow the mode" is a fact about that turn, not a bug this
 * adapter (or the backend) tries to fix.
 *
 * Errors: every route here answers a failure with the FLAT
 * `{"error": str, "error_class": "modes.<reason>"}` body — same shape and
 * same reasons `adapters/sideThreads.ts` already reads directly rather than
 * through `responseReason` (kept only as the fallback for a response this
 * module did not itself shape, e.g. a raw 422 from FastAPI's own request
 * validation).
 */

export interface BehaviorModeText {
  en: string;
  es: string;
}

export interface BehaviorModeChecks {
  confidence_tags?: boolean;
  first_sentence?: 'challenge';
  forbidden_phrases?: string[];
  ends_with_question?: boolean;
  max_questions?: number;
  max_words?: number;
}

export interface BehaviorMode {
  id: string;
  builtin: boolean;
  version: number;
  name: BehaviorModeText;
  description: BehaviorModeText;
  prompt: string;
  checks: BehaviorModeChecks;
  /** User modes only (`builtin === false`). */
  owner?: string | null;
  updated_at?: string | null;
}

export interface ModeViolation {
  rule: string;
  detail: string;
}

/** `src/behavior_modes.py::check_response`'s exact shape. */
export interface ModeCheckResult {
  checked: string[];
  violations: ModeViolation[];
}

export interface ListModesResult {
  modes: BehaviorMode[];
  default: string;
}

export class BehaviorModesApiError extends ApiError {
  readonly errorClass: string | null;

  constructor(message: string, status: number, errorClass: string | null) {
    super(message, status);
    this.name = 'BehaviorModesApiError';
    this.errorClass = errorClass;
  }
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const errorClass = typeof payload.error_class === 'string' ? payload.error_class : null;
    const flatMessage = typeof payload.error === 'string' && payload.error.trim() ? payload.error : null;
    const message = flatMessage ?? (await responseReason(response, path));
    throw new BehaviorModesApiError(message, response.status, errorClass);
  }
  return (await response.json()) as T;
}

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
}

// ---------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------

export function listModes(signal?: AbortSignal): Promise<ListModesResult> {
  return get('/api/behavior-modes', signal);
}

export interface SaveModeInput {
  id: string;
  name: BehaviorModeText;
  description?: BehaviorModeText;
  prompt?: string;
  checks?: BehaviorModeChecks;
}

/** `POST /api/behavior-modes` — create or update one of the caller's own
 *  modes. 409 `modes.builtin` for an id that belongs to a built-in mode,
 *  400 `modes.invalid` for a bad slug, a missing name, or too long a
 *  prompt — surfaced as `BehaviorModesApiError.errorClass`. */
export function saveMode(input: SaveModeInput): Promise<{ mode: BehaviorMode }> {
  return post('/api/behavior-modes', {
    id: input.id,
    name: input.name,
    description: input.description ?? { en: '', es: '' },
    prompt: input.prompt ?? '',
    checks: input.checks ?? {},
  });
}

export function deleteMode(id: string): Promise<{ ok: true }> {
  return request(`/api/behavior-modes/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** `POST /api/behavior-modes/default` — admin-only: the global fallback
 *  every user falls back to, past their own session and any override. Only
 *  a BUILT-IN id is accepted server-side (nobody's private mode is
 *  guaranteed to exist for everybody else). */
export function setDefaultMode(id: string): Promise<{ default: string }> {
  return post('/api/behavior-modes/default', { mode: id });
}

// ---------------------------------------------------------------------------
// Per-session override
// ---------------------------------------------------------------------------

export interface SessionModeResult {
  /** The session's own stored override, or `null` when it has none (it then
   *  inherits the global `behavior_mode_default` setting). */
  mode: string | null;
  /** What actually applies right now — request > session > global default >
   *  `"default"` (`src/behavior_modes.py::resolve`'s own precedence). Use
   *  this for display: it is never ambiguous the way `mode` alone is. */
  effective: string;
}

export function getSessionMode(sessionId: string, signal?: AbortSignal): Promise<SessionModeResult> {
  return get(`/api/session/${encodeURIComponent(sessionId)}/behavior-mode`, signal);
}

/** `POST /api/session/{id}/behavior-mode` — `mode: null` clears the
 *  session's own override back to "inherit the global default"; Studio's
 *  own UI never sends `null` (picking "Default" pins the literal `default`
 *  mode id instead, so `/mode off` always means the same thing regardless
 *  of what an admin later sets as the global default), but the field stays
 *  nullable here because the route itself accepts it. */
export function setSessionMode(sessionId: string, mode: string | null): Promise<{ mode: string | null }> {
  return post(`/api/session/${encodeURIComponent(sessionId)}/behavior-mode`, { mode });
}

/** `POST /api/behavior-modes/check` — runs `check_response` against
 *  arbitrary text without saving anything; Settings' "Try it" preview. */
export function checkText(mode: string, text: string): Promise<ModeCheckResult> {
  return post('/api/behavior-modes/check', { mode, text });
}

// ---------------------------------------------------------------------------
// Pure presentation helpers — exercised by studio/checks/behavior_modes.check.mjs
// without a DOM.
// ---------------------------------------------------------------------------

/** The mode's display name in the UI's own language, falling back to
 *  English and then the raw id — never a blank label. */
export function modeLabel(mode: Pick<BehaviorMode, 'id' | 'name'> | null | undefined, lang: Lang): string {
  if (!mode) return t('Default');
  return mode.name[lang] || mode.name.en || mode.id;
}

export function modeDescription(mode: Pick<BehaviorMode, 'description'>, lang: Lang): string {
  return mode.description[lang] || mode.description.en || '';
}

/** "used a banned phrase: ..." · "no confidence tags (...)" — the server's
 *  own `detail` strings, joined for a tooltip; empty when nothing violated
 *  (or nothing was even checked). Never re-derives or rewords a violation:
 *  `check_response` is the one honest source for what actually happened. */
export function violationsLabel(check: ModeCheckResult | null | undefined): string {
  if (!check || !check.violations.length) return '';
  return check.violations.map((v) => v.detail).join(' · ');
}

/** `/mode <id|name>`: an exact id match first, then an exact name match (in
 *  the caller's language), then a loose "contains" match on the name — the
 *  same three-step lookup `/model`/`/preset` already use in `Studio.tsx`. */
export function findModeByIdOrName(modes: BehaviorMode[], needle: string, lang: Lang): BehaviorMode | undefined {
  const q = needle.trim().toLowerCase();
  if (!q) return undefined;
  return (
    modes.find((m) => m.id.toLowerCase() === q) ??
    modes.find((m) => modeLabel(m, lang).toLowerCase() === q) ??
    modes.find((m) => modeLabel(m, lang).toLowerCase().includes(q))
  );
}

export interface ModeCommandResult {
  kind: 'list' | 'set' | 'unknown';
  /** Set for `kind === 'set'` — always a concrete catalog id, including the
   *  literal `"default"` for `/mode off` (never `null`: see `setSessionMode`'s
   *  own doc comment for why "off" always means the SAME thing). */
  id?: string;
  /** Set for `kind === 'list'`/`'unknown'` — the Markdown to show. */
  markdown?: string;
}

const OFF_RE = /^(off|no|none|ninguno|default|por[\s_-]?defecto)$/i;

/** The pure half of `/mode` (Studio.tsx's `case 'mode'` supplies the actual
 *  session I/O): bare lists every mode with the active one marked, an id or
 *  a name (in either language, exact or partial) sets it, `off`/`default`
 *  (English or Spanish) always resolves to the literal `default` mode, and
 *  anything else is `'unknown'` with a one-line pointer back to `/mode`. */
export function resolveModeCommand(modes: BehaviorMode[], activeId: string, args: string, lang: Lang): ModeCommandResult {
  const trimmed = args.trim();
  if (!trimmed) {
    const lines = modes.map((m) => {
      const on = m.id === activeId;
      const label = modeLabel(m, lang);
      const desc = modeDescription(m, lang);
      return `${on ? '**' : ''}${label}${on ? '**' : ''}${desc ? ` — ${desc}` : ''}`;
    });
    return { kind: 'list', markdown: lines.join('\n') };
  }
  if (OFF_RE.test(trimmed)) return { kind: 'set', id: 'default' };
  const hit = findModeByIdOrName(modes, trimmed, lang);
  if (!hit) {
    return {
      kind: 'unknown',
      markdown: t('No mode called "{q}". /mode on its own lists them.', { q: trimmed }),
    };
  }
  return { kind: 'set', id: hit.id };
}
