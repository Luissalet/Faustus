/**
 * Structured reply blocks: what a model writes into a ```choices``` or
 * ```decision``` fenced block, and the pure validator that turns the fence
 * text into something `UiBlock.tsx` draws with fixed components.
 *
 * The model never ships markup: it ships a small JSON object and the client
 * decides how it looks, so a reply can offer clickable answers or a
 * side-by-side decision without the sandboxed-HTML path. Invalid input is
 * always `{ok:false}` (the renderer then shows the plain code block), never a
 * throw, and every list and string is capped.
 */

export const UI_BLOCK_LANGS = new Set(['choices', 'faustus-choices', 'decision', 'faustus-decision']);

export const UI_MAX_OPTIONS = 8;
export const UI_MAX_DECISION_OPTIONS = 6;
export const UI_MAX_POINTS = 8;
export const UI_MAX_STRING = 200;

export interface ChoiceOption {
  label: string;
  detail?: string;
}

export interface ChoicesSpec {
  kind: 'choices';
  question?: string;
  options: ChoiceOption[];
  multi: boolean;
}

export interface DecisionOption {
  name: string;
  summary?: string;
  pros: string[];
  cons: string[];
  cost?: string;
  recommended: boolean;
}

export interface DecisionSpec {
  kind: 'decision';
  title?: string;
  options: DecisionOption[];
  verdict?: string;
}

export type UiBlockSpec = ChoicesSpec | DecisionSpec;
export type UiBlockValidation = { ok: true; spec: UiBlockSpec } | { ok: false; reason: string };

function str(value: unknown): string | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) value = String(value);
  if (typeof value !== 'string') return undefined;
  const s = value.replace(/\s+/g, ' ').trim();
  if (!s) return undefined;
  return s.length > UI_MAX_STRING ? `${s.slice(0, UI_MAX_STRING - 1)}…` : s;
}

function points(value: unknown): string[] {
  if (!Array.isArray(value)) {
    const one = str(value);
    return one ? [one] : [];
  }
  return value.map(str).filter((s): s is string => !!s).slice(0, UI_MAX_POINTS);
}

function parseChoices(obj: Record<string, unknown>): UiBlockValidation {
  const raw = Array.isArray(obj.options) ? obj.options : Array.isArray(obj.choices) ? obj.choices : null;
  if (!raw) return { ok: false, reason: 'choices needs an "options" list' };
  const seen = new Set<string>();
  const options: ChoiceOption[] = [];
  for (const item of raw) {
    const label = typeof item === 'object' && item !== null ? str((item as Record<string, unknown>).label ?? (item as Record<string, unknown>).name) : str(item);
    if (!label || seen.has(label.toLowerCase())) continue;
    seen.add(label.toLowerCase());
    const detail = typeof item === 'object' && item !== null ? str((item as Record<string, unknown>).detail ?? (item as Record<string, unknown>).description) : undefined;
    options.push(detail ? { label, detail } : { label });
    if (options.length >= UI_MAX_OPTIONS) break;
  }
  if (options.length < 2) return { ok: false, reason: 'choices needs at least two distinct options' };
  return { ok: true, spec: { kind: 'choices', question: str(obj.question ?? obj.title), options, multi: obj.multi === true || obj.multiple === true } };
}

function parseDecision(obj: Record<string, unknown>): UiBlockValidation {
  const raw = Array.isArray(obj.options) ? obj.options : null;
  if (!raw) return { ok: false, reason: 'decision needs an "options" list' };
  const options: DecisionOption[] = [];
  for (const item of raw) {
    if (typeof item !== 'object' || item === null) continue;
    const o = item as Record<string, unknown>;
    const name = str(o.name ?? o.label ?? o.title);
    if (!name) continue;
    options.push({ name, summary: str(o.summary ?? o.detail), pros: points(o.pros), cons: points(o.cons), cost: str(o.cost), recommended: o.recommended === true });
    if (options.length >= UI_MAX_DECISION_OPTIONS) break;
  }
  if (options.length < 2) return { ok: false, reason: 'decision needs at least two named options' };
  // One recommendation at most: the first one marked wins.
  let marked = false;
  for (const o of options) {
    if (o.recommended && marked) o.recommended = false;
    if (o.recommended) marked = true;
  }
  return { ok: true, spec: { kind: 'decision', title: str(obj.title ?? obj.question), options, verdict: str(obj.verdict ?? obj.recommendation) } };
}

/** `lang` is the fence language; it picks the kind unless the JSON names one. */
export function parseUiBlock(lang: string, raw: string): UiBlockValidation {
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return { ok: false, reason: 'not valid JSON' };
  }
  if (Array.isArray(obj)) obj = { options: obj };
  if (typeof obj !== 'object' || obj === null) return { ok: false, reason: 'expected a JSON object' };
  const o = obj as Record<string, unknown>;
  const fromLang = lang.trim().toLowerCase().replace(/^faustus-/, '');
  const kind = typeof o.kind === 'string' ? o.kind.toLowerCase() : fromLang;
  if (kind === 'choices') return parseChoices(o);
  if (kind === 'decision') return parseDecision(o);
  return { ok: false, reason: `unknown block kind "${kind}"` };
}
