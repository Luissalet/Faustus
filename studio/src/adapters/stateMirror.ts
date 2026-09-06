import { asArray, getJson } from './api';

/**
 * State Mirror (`/api/state`), shaped for one screen.
 *
 * The mirror answers three questions about every value at once: what it is,
 * WHEN it was observed and by WHICH source. This layer exists so that the
 * third and fourth of those never get dropped on the way to a pixel, because
 * a number rendered without its age is a rumour with good posture — and the
 * one failure this screen must never commit is showing a stale value as if it
 * were the current state.
 *
 * Three consequences, and they are why every derivation below is a pure
 * exported function rather than a hook or a line inside a component:
 *
 * **Freshness is decided once, here.** `isCurrent` is the only predicate that
 * answers "may this be presented as the state now", and only `fresh` passes
 * it. A component that compared timestamps of its own would be a second
 * opinion about age, and the one that goes stale.
 *
 * **A situation is derived from the fields AND their freshness together.** A
 * run whose `status` says `running` but was last observed twenty minutes ago
 * is not running; it is a stale claim about a run. `situationOf` therefore
 * refuses to reach a live grouping through a value it may not trust, which is
 * exactly the check `studio/checks/stateMirror.check.mjs` drives.
 *
 * **An entity id is a URL, not a slug.** `service:///real/comfyui` carries a
 * scheme and slashes, so every id that goes into a path goes through
 * `encodeEntityId` and comes back through `decodeEntityId`. A screen that
 * interpolated one raw would build a request for a different row, or for no
 * row at all.
 */

/* ── small readers: the API is JSON out of SQLite, not a typed contract ── */

const str = (value: unknown): string => (typeof value === 'string' ? value : '');
const num = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
const flag = (value: unknown): boolean => value === true;

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

const strList = (value: unknown): string[] => asArray<unknown>(value).map(str).filter(Boolean);

/* ── the closed vocabularies, mirrored from src/state_mirror/contracts.py ── */

export const FRESH = 'fresh';
export const AGING = 'aging';
export const STALE = 'stale';
export const UNKNOWN = 'unknown';
/** Only a PROJECTION or an entity may be `mixed`; no single field ever is. */
export const MIXED = 'mixed';

/**
 * Ordered from most to least trustworthy, exactly as `FRESHNESS_RATINGS` is
 * on the server. `freshnessRank` reads this order and nothing else, so the
 * two cannot drift into disagreeing about which of two values is older.
 */
export const FRESHNESS_ORDER = [FRESH, AGING, STALE, UNKNOWN];

/**
 * The ratings a value may be shown with as the state NOW. One entry, and it
 * is the whole point of the screen: `aging` is good enough to orient by and
 * not good enough to act on, so it does not appear here.
 */
export const CURRENT_FRESHNESS = [FRESH];

/** Good enough to orient by. Section 6.3's middle rung. */
export const USABLE_FRESHNESS = [FRESH, AGING];

/** `EPISTEMICS`, ordered. Lower index, stronger claim. */
export const EPISTEMIC_ORDER = ['observed', 'reported', 'derived', 'inferred', 'unknown'];

/** The two that may be presented without a caveat: measurement and arithmetic over it. */
export const OBSERVED_EPISTEMICS = ['observed', 'derived'];

/**
 * The five questions the screen answers, in the order a person actually asks
 * them. This tuple IS the layout: `groupBySituation` returns its groups in
 * exactly this order and the screen renders them in the order it is handed.
 */
export const SITUATIONS = ['running', 'waiting', 'stale', 'conflict', 'machine'];

export type Situation = 'running' | 'waiting' | 'stale' | 'conflict' | 'machine';

/**
 * The one field that decides what an entity of each schema is DOING. Named
 * once here rather than guessed per kind, because a kind whose deciding field
 * nobody declared would silently fall through to the inventory and never be
 * reported as running or as waiting.
 */
export const DECIDING_FIELD: Record<string, string> = {
  'run_state.v1': 'status',
  'session_state.v1': 'turn_state',
  'council_state.v1': 'status',
  'service_state.v1': 'health',
  'model_state.v1': 'availability',
  'project_state.v1': 'workspace_available',
  'artifact_state.v1': 'exists',
  'connection_state.v1': 'reachable',
  'objective_state.v1': 'status',
  'approval_state.v1': 'status',
  'device_state.v1': 'cpu_percent',
};

/** Values of a deciding field that mean work is happening right now. */
export const RUNNING_VALUES = ['running', 'executing', 'active', 'in_progress', 'streaming', 'thinking'];

/** Values that mean the thing has stopped and is waiting for a person. */
export const WAITING_VALUES = ['pending', 'waiting', 'requested', 'awaiting_approval', 'needs_approval', 'review', 'blocked'];

/**
 * Kinds that describe the machine itself rather than work on it. Everything
 * that answers none of the first four questions lands in this group, so the
 * list is what the panel is TITLED after and not a filter that can hide a row.
 */
export const MACHINE_KINDS = ['device', 'model', 'service', 'connection', 'capability', 'resource'];

/* ── the refusal convention ─────────────────────────────────────────────── */

/**
 * A refused read or write answers 200 with a stable token and a sentence: the
 * token is what a caller branches on, the sentence is for the person reading.
 * Branching on the sentence is the habit this repository already paid for.
 */
export interface StateError {
  code: string;
  detail: string;
  /** The field the refusal is about, when the server named one. */
  path: string;
  /** Present on `state_mirror_disabled`: false, and the reason the button is off. */
  enabled?: boolean;
}

export class StateRefusal extends Error {
  readonly code: string;
  readonly path: string;
  readonly enabled?: boolean;

  constructor(error: StateError) {
    super(error.detail);
    this.name = 'StateRefusal';
    this.code = error.code;
    this.path = error.path;
    this.enabled = error.enabled;
  }
}

/** The refusal inside a 200 body, or null when the call succeeded. */
export function refusalOf(payload: unknown): StateError | null {
  const body = obj(payload);
  if (body.ok !== false) return null;
  const nested = obj(body.error);
  const code = str(nested.code).trim() || str(body.error).trim();
  const detail = str(nested.message).trim() || str(body.detail).trim();
  return {
    code: code || 'refused',
    detail: detail || 'the mirror refused this and did not say why',
    path: str(nested.path).trim(),
    enabled: typeof body.enabled === 'boolean' ? body.enabled : undefined,
  };
}

/* ── the shapes, as the server sends them ──────────────────────────────── */

/**
 * One value and the four things that qualify it. Nothing on this screen is
 * ever drawn from `value` alone: `observedAt` and `source` are what turn a
 * number into evidence, and `freshness` is what says whether the evidence is
 * still worth anything.
 */
export interface FieldValue {
  name: string;
  value: unknown;
  epistemic: string;
  freshness: string;
  observedAt: string;
  observationId: string;
  source: string;
  ttlSeconds: number;
  computedAt: string;
}

export interface StateEntity {
  id: string;
  kind: string;
  owner: string;
  namespace: string;
  projectId: string;
  displayName: string;
  labels: string[];
  schema: string;
  revision: number;
  updatedAt: string;
  retiredAt: string;
  fields: FieldValue[];
  /** Declared by the schema and never observed. An absence, listed. */
  unknownFields: string[];
  conflictIds: string[];
  /** One word for the whole row: a shared rating, or `mixed` when they differ. */
  freshness: string;
  /** The least trustworthy rating among the fields. Never `mixed`. */
  worst: string;
}

export interface Observation {
  id: string;
  entityId: string;
  schema: string;
  source: string;
  epistemic: string;
  sequence: number;
  observedAt: string;
  receivedAt: string;
  partial: boolean;
  state: Record<string, unknown>;
  evidenceRefs: string[];
  sourceRevision: string;
}

export interface Claim {
  source: string;
  value: unknown;
  epistemic: string;
  observedAt: string;
}

export interface Conflict {
  id: string;
  entityId: string;
  field: string;
  status: string;
  claims: Claim[];
  nextCheck: string;
  detectedAt: string;
  resolvedAt: string;
  resolution: string;
}

export interface SourceHealth {
  name: string;
  /** `ok | degraded | down | unknown`, as the store recorded it. */
  health: string;
  /** Whether the adapter can answer at all in this build. */
  available: boolean;
  lastSuccessAt: string;
  lastFailureAt: string;
  observations: number;
  failures: number;
  detail: string;
}

export interface Diagnostics {
  sources: SourceHealth[];
  entities: number;
  observations: number;
  cursor: number;
  sweepSeconds: number;
  enabled: boolean;
  situations: string[];
}

export function fieldFrom(name: string, raw: unknown): FieldValue {
  const f = obj(raw);
  return {
    name: str(name),
    value: 'value' in f ? f.value : null,
    epistemic: str(f.epistemic) || UNKNOWN,
    freshness: str(f.freshness) || UNKNOWN,
    observedAt: str(f.observed_at),
    observationId: str(f.observation_id),
    source: str(f.source),
    ttlSeconds: num(f.ttl_seconds),
    computedAt: str(f.computed_at),
  };
}

/**
 * One row, from whichever of the two shapes the route sends.
 *
 * Three shapes reach here and all three are the server's own: a materialised
 * state (`entity_id`, `fields`), the full answer of `GET /entities/{id}`
 * (`entity` and `state` beside each other), and a LISTING row, whose `state`
 * is a summary — counts and one combined rating, no values, because two
 * hundred rows carrying every field with its four pieces of metadata would be
 * a megabyte to draw a sidebar. Reading all three is one branch; guessing
 * which arrived from the keys that happen to be present would break the day a
 * field is added.
 *
 * A summarised row keeps the rating the server computed. Its `worst` cannot
 * be recovered from a `mixed` summary — that is what `mixed` means — so it is
 * read DOWN to `stale`: the reading that refuses to present the row as
 * current is the only safe one to guess.
 */
export function entityFrom(raw: unknown): StateEntity {
  const row = obj(raw);
  const inner = obj(row.state);
  const state = Object.keys(inner).length ? inner : row;
  const entity = obj(row.entity);
  const id = str(row.id) || str(row.entity_id) || str(entity.id) || str(state.entity_id);
  const fields = Object.entries(obj(state.fields)).map(([name, meta]) => fieldFrom(name, meta));
  fields.sort((a, b) => a.name.localeCompare(b.name));
  const ratings = fields.map((f) => f.freshness);
  const summarised = str(state.freshness);
  return {
    id,
    kind: str(row.kind) || str(entity.kind) || kindOf(id),
    owner: str(row.owner) || str(entity.owner),
    namespace: str(row.namespace) || str(entity.namespace) || str(state.namespace) || 'real',
    projectId: str(row.project_id) || str(entity.project_id) || str(state.project_id),
    // Empty when the server sent none, never filled in from the id here: a
    // merge has to be able to tell "this row has no name" from "this row is
    // named after its id", or a change batch blanks the label under the
    // reader. `label()` does the fallback, once, at the point of drawing.
    displayName: str(row.display_name) || str(entity.display_name),
    labels: strList(row.labels ?? entity.labels),
    schema: str(row.schema) || str(entity.schema) || str(state.schema),
    revision: num(state.revision ?? row.revision),
    updatedAt: str(state.updated_at) || str(row.updated_at),
    retiredAt: str(row.retired_at) || str(entity.retired_at),
    fields,
    unknownFields: strList(row.unknown_fields ?? state.unknown_fields),
    conflictIds: strList(row.conflicts ?? state.conflicts),
    freshness: fields.length || !summarised ? combinedFreshness(ratings) : summarised,
    worst: fields.length || !summarised
      ? worstFreshness(ratings)
      : (summarised === MIXED ? STALE : summarised),
  };
}

export function observationFrom(raw: unknown): Observation {
  const o = obj(raw);
  return {
    id: str(o.id),
    entityId: str(o.entity_id),
    schema: str(o.schema),
    source: str(o.source),
    epistemic: str(o.epistemic) || UNKNOWN,
    sequence: num(o.sequence),
    observedAt: str(o.observed_at),
    receivedAt: str(o.received_at),
    partial: o.partial !== false,
    state: obj(o.state),
    evidenceRefs: strList(o.evidence_refs),
    sourceRevision: str(o.source_revision),
  };
}

export function conflictFrom(raw: unknown): Conflict {
  const c = obj(raw);
  return {
    id: str(c.id),
    entityId: str(c.entity_id),
    field: str(c.field),
    status: str(c.status) || 'reconciling',
    claims: asArray<unknown>(c.claims).map((row) => {
      const claim = obj(row);
      return {
        source: str(claim.source),
        value: 'value' in claim ? claim.value : null,
        epistemic: str(claim.epistemic) || UNKNOWN,
        observedAt: str(claim.observed_at),
      };
    }),
    nextCheck: str(c.next_check),
    detectedAt: str(c.detected_at),
    resolvedAt: str(c.resolved_at),
    resolution: str(c.resolution),
  };
}

/**
 * The mirror's opinion of itself, from two tables that answer different halves.
 *
 * `adapters` is every source this BUILD can observe with, and whether it can
 * answer at all; `sources` is what each of them actually did last time. Only
 * the two together are honest: a build listing only what has run reports a
 * broken adapter as absent rather than as broken, and a build listing only
 * what is registered reports a source that has never answered as healthy.
 * An adapter with no row has never run, and that is `unknown` and not `ok`.
 */
export function diagnosticsFrom(raw: unknown): Diagnostics {
  const d = obj(raw);
  const store = obj(d.store);
  const counts = obj(store.counts);
  const ran = new Map<string, Record<string, unknown>>();
  for (const row of asArray<unknown>(d.sources)) {
    const s = obj(row);
    const name = str(s.source) || str(s.name);
    if (name && !ran.has(name)) ran.set(name, s);
  }
  const sources: SourceHealth[] = [];
  const seen = new Set<string>();
  const push = (name: string, available: boolean, detail: string) => {
    if (!name || seen.has(name)) return;
    seen.add(name);
    const s = ran.get(name) ?? {};
    sources.push({
      name,
      health: str(s.health) || (available ? UNKNOWN : 'down'),
      available,
      lastSuccessAt: str(s.last_ok_at),
      lastFailureAt: str(s.last_error_at),
      observations: num(s.observations),
      failures: num(s.failures),
      detail: str(s.detail) || detail,
    });
  };
  for (const row of asArray<unknown>(d.adapters)) {
    const a = obj(row);
    push(str(a.name), a.available === true, str(a.detail));
  }
  // A source that ran under a name this build no longer registers still has to
  // appear: it is holding fields nothing can refresh, which is exactly the
  // thing worth seeing.
  for (const name of ran.keys()) push(name, false, '');
  return {
    sources,
    entities: num(counts.state_entities),
    observations: num(counts.state_observations),
    cursor: num(store.cursor ?? d.cursor),
    sweepSeconds: num(d.sweep_seconds),
    enabled: flag(d.enabled),
    situations: strList(d.situations),
  };
}

/* ── ids: `<kind>://<owner>/<namespace>/<identifier>` ───────────────────── */

/**
 * An entity id is `<kind>://<owner>/<namespace>/<identifier>`, so it carries a
 * scheme, empty segments on a single-user install and slashes inside the
 * identifier itself (`artifact:///real/exports/2026/report.pdf`).
 *
 * That means it can never be interpolated into a path raw. `encodeEntityId`
 * percent-escapes the whole thing, including the separators, and the route
 * declares its parameter as `{entity_id:path}` so the decoded value arrives
 * whole. The pair below is what the check drives: whatever survives
 * `decodeEntityId(encodeEntityId(id))` is what the server will be asked about.
 */
export function encodeEntityId(id: string): string {
  return encodeURIComponent(str(id));
}

export function decodeEntityId(raw: string): string {
  const value = str(raw);
  try {
    return decodeURIComponent(value);
  } catch {
    // A malformed escape is the caller's typo, not a reason to lose the row
    // it was reaching for; the server refuses an id it cannot parse anyway.
    return value;
  }
}

/** The `kind` half of an id, or `""` when the value is not an id. */
export function kindOf(id: string): string {
  const value = str(id);
  const at = value.indexOf('://');
  return at > 0 ? value.slice(0, at) : '';
}

/** The identifier half: everything after `<kind>://<owner>/<namespace>/`. */
export function identifierOf(id: string): string {
  const value = str(id);
  const at = value.indexOf('://');
  if (at < 0) return '';
  const rest = value.slice(at + 3).split('/');
  return rest.length > 2 ? rest.slice(2).join('/') : '';
}

/** The namespace half. `real` is the machine; everything else is isolated. */
export function namespaceOf(id: string): string {
  const value = str(id);
  const at = value.indexOf('://');
  if (at < 0) return '';
  const rest = value.slice(at + 3).split('/');
  return rest.length > 1 ? rest[1] : '';
}

/* ── freshness: the one place age is decided ───────────────────────────── */

/**
 * Position in `FRESHNESS_ORDER`. A rating this build has never heard of ranks
 * LAST rather than raising, because the safe reading of "I do not know how
 * fresh this is" is "not fresh at all".
 */
export function freshnessRank(rating: string): number {
  const at = FRESHNESS_ORDER.indexOf(str(rating).trim());
  return at < 0 ? FRESHNESS_ORDER.length : at;
}

/**
 * May this be shown as the state NOW?
 *
 * The single most important predicate on this screen, and it is deliberately
 * strict: only `fresh` passes. Every other rating is drawn with its age said
 * in words, so a reader can never mistake a remembered value for a current
 * one. `aging` failing here is not an oversight — it is section 6.3's rule
 * that orientation may use it and a decision may not.
 */
export function isCurrent(field: FieldValue | null | undefined): boolean {
  return !!field && CURRENT_FRESHNESS.indexOf(field.freshness) >= 0;
}

/** Good enough to orient by: `fresh` or `aging`, never `stale` or `unknown`. */
export function isUsable(field: FieldValue | null | undefined): boolean {
  return !!field && USABLE_FRESHNESS.indexOf(field.freshness) >= 0;
}

/**
 * Fresh AND measured. Both halves are needed: a fresh rumour is still a
 * rumour, and yesterday's measurement is still yesterday's.
 */
export function isTrusted(field: FieldValue | null | undefined): boolean {
  if (!field || !isCurrent(field)) return false;
  return OBSERVED_EPISTEMICS.indexOf(field.epistemic) >= 0;
}

/** The least trustworthy rating in a set. Nothing at all answers `unknown`. */
export function worstFreshness(ratings: string[]): string {
  const seen = (ratings ?? []).map((r) => str(r).trim()).filter((r) => FRESHNESS_ORDER.indexOf(r) >= 0);
  if (!seen.length) return UNKNOWN;
  return seen.reduce((left, right) => (freshnessRank(right) > freshnessRank(left) ? right : left));
}

/**
 * One rating for a whole row, or `mixed` when the fields disagree.
 *
 * Not the same function as `worstFreshness`, and the difference is the point:
 * a row whose fields are all `aging` IS `aging`, and one that is half `fresh`
 * and half `stale` is `mixed` — a word that means "look at the fields", not
 * "average them". Collapsing that would hide either a usable field or a
 * dangerous one, and which of the two is hidden depends on the arithmetic.
 */
export function combinedFreshness(ratings: string[]): string {
  const seen: string[] = [];
  for (const rating of ratings ?? []) {
    const value = str(rating).trim();
    if (FRESHNESS_ORDER.indexOf(value) >= 0 && seen.indexOf(value) < 0) seen.push(value);
  }
  if (!seen.length) return UNKNOWN;
  if (seen.length === 1) return seen[0];
  return MIXED;
}

export type Tone = 'ok' | 'warn' | 'danger' | 'neutral';

export interface Reading {
  /** The server's own word, unedited, so a log and the screen agree. */
  value: string;
  /** The key `t()` translates. Never a colour, and never an icon on its own. */
  label: string;
  tone: Tone;
  /** Whether this value may be presented as the current state. */
  current: boolean;
}

/**
 * A freshness rating read to a word, a tone and a permission.
 *
 * `stale` is `danger` and says the word, because the whole screen is built
 * around a reader being unable to mistake one for a current value. `unknown`
 * is `neutral` and not an alarm: it means nothing has ever looked, the value
 * is drawn as an absence rather than as a number, and there is nothing there
 * to be alarmed about.
 */
export function freshnessReading(rating: string): Reading {
  const value = str(rating).trim();
  switch (value) {
    case FRESH: return { value, label: 'current', tone: 'ok', current: true };
    case AGING: return { value, label: 'ageing', tone: 'warn', current: false };
    case STALE: return { value, label: 'stale', tone: 'danger', current: false };
    case UNKNOWN: return { value, label: 'never observed', tone: 'neutral', current: false };
    case MIXED: return { value, label: 'mixed', tone: 'warn', current: false };
    default: return { value: value || UNKNOWN, label: 'never observed', tone: 'neutral', current: false };
  }
}

/**
 * How strongly a value is known, in a word. `reported` is separated from
 * `observed` on purpose: an actor saying "I finished" and a probe seeing it
 * finished are different facts, and only one of them is evidence.
 */
export function epistemicReading(epistemic: string): Reading {
  const value = str(epistemic).trim();
  switch (value) {
    case 'observed': return { value, label: 'observed', tone: 'ok', current: true };
    case 'derived': return { value, label: 'derived', tone: 'ok', current: true };
    case 'reported': return { value, label: 'reported, not checked', tone: 'warn', current: false };
    case 'inferred': return { value, label: 'inferred', tone: 'warn', current: false };
    default: return { value: value || UNKNOWN, label: 'not known', tone: 'neutral', current: false };
  }
}

/** Least trustworthy first: the rows a reader has to deal with come up top. */
export function byFreshness(left: FieldValue, right: FieldValue): number {
  const delta = freshnessRank(right.freshness) - freshnessRank(left.freshness);
  return delta !== 0 ? delta : left.name.localeCompare(right.name);
}

/** The same order, for whole rows, so a stale entity is not buried. */
export function entityByFreshness(left: StateEntity, right: StateEntity): number {
  const delta = freshnessRank(right.worst) - freshnessRank(left.worst);
  return delta !== 0 ? delta : label(left).localeCompare(label(right));
}

/* ── provenance: when, and by which source ─────────────────────────────── */

/** Seconds since `observedAt`, or null when the timestamp cannot be read. */
export function ageSeconds(observedAt: string, now?: number): number | null {
  const raw = str(observedAt).trim();
  if (!raw) return null;
  const seen = Date.parse(raw.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(raw) ? raw : `${raw}Z`);
  if (!Number.isFinite(seen)) return null;
  // A remote clock running fast reads as "just now", never as fresher than
  // fresh: a negative age would rate a future timestamp better than a present
  // one, which is the wrong direction for every decision downstream.
  return Math.max(0, ((typeof now === 'number' ? now : Date.now()) - seen) / 1000);
}

export interface Provenance {
  /** The rating, read to a word and a tone. */
  freshness: Reading;
  epistemic: Reading;
  /** ISO-8601, exactly as stored, for the `title` and the detail row. */
  observedAt: string;
  ageSeconds: number | null;
  source: string;
  ttlSeconds: number;
  observationId: string;
}

/**
 * Everything the screen has to be able to say about one value on hover.
 *
 * A rating a person cannot interrogate is a rating they will either
 * over-trust or ignore, and the two failures look the same from the outside.
 * So the tooltip carries the rating, the time, the source and the guarantee
 * that was given, and none of it is recomputed anywhere else.
 */
export function provenanceOf(field: FieldValue | null | undefined, now?: number): Provenance {
  const value = field ?? fieldFrom('', {});
  return {
    freshness: freshnessReading(value.freshness),
    epistemic: epistemicReading(value.epistemic),
    observedAt: value.observedAt,
    ageSeconds: ageSeconds(value.observedAt, now),
    source: value.source,
    ttlSeconds: value.ttlSeconds,
    observationId: value.observationId,
  };
}

/**
 * What to call this row on screen: its display name, then the identifier half
 * of its id, then the whole id. The fallback lives here and nowhere else, so
 * a row that arrives without a name is drawn under one and still merges as the
 * nameless row it is.
 */
export function label(entity: StateEntity | null | undefined): string {
  if (!entity) return '';
  return entity.displayName || identifierOf(entity.id) || entity.id;
}

/** The named field of a row, or null. Never throws on a row with no fields. */
export function fieldOf(entity: StateEntity | null | undefined, name: string): FieldValue | null {
  if (!entity) return null;
  const wanted = str(name);
  return entity.fields.find((f) => f.name === wanted) ?? null;
}

/**
 * The field that says what this entity is DOING, or null when its schema
 * declares none. Reading it through `DECIDING_FIELD` rather than by guessing
 * is what keeps a new kind from silently answering "not running" for ever.
 */
export function decidingField(entity: StateEntity | null | undefined): FieldValue | null {
  if (!entity) return null;
  const name = DECIDING_FIELD[entity.schema] ?? '';
  return name ? fieldOf(entity, name) : null;
}

/** Whether anything known about this row is even worth orienting by. */
export function hasUsable(entity: StateEntity | null | undefined): boolean {
  return !!entity && entity.fields.some(isUsable);
}

/** The fields that may be shown as the state now. The rest are shown aged. */
export function currentFields(entity: StateEntity | null | undefined): FieldValue[] {
  return (entity?.fields ?? []).filter(isCurrent);
}

/** The fields that must NOT be shown as the state now. Drawn, but marked. */
export function agedFields(entity: StateEntity | null | undefined): FieldValue[] {
  return (entity?.fields ?? []).filter((f) => !isCurrent(f)).sort(byFreshness);
}

/* ── the five questions, in the order a person asks them ───────────────── */

const lower = (value: unknown): string => str(value).trim().toLowerCase();

/**
 * Which of the five questions this row is an answer to.
 *
 * The rules are tried in this order, and the ORDER is the contract:
 *
 *  1. `conflict` — two sources disagree about a field. It comes first because
 *     the reducer deliberately did not choose, so grouping the row anywhere
 *     else would present one of the two claims as though it had won.
 *  2. `waiting` — something has stopped and is waiting for a person: an
 *     approval pending, or a deciding field that says so.
 *  3. `running` — work is happening RIGHT NOW. This rule may only be reached
 *     through a field that is still usable: a `status` of `running` observed
 *     twenty minutes ago is not a running job, it is a stale claim about one,
 *     and it falls through to rule 4 where it is drawn as such.
 *  4. `stale` — nothing known about this row is fresh or even ageing.
 *  5. `machine` — everything else: the inventory the mirror holds.
 */
export function situationOf(entity: StateEntity | null | undefined): Situation {
  if (!entity) return 'machine';
  if (entity.conflictIds.length) return 'conflict';

  const pending = fieldOf(entity, 'approval_pending');
  if (isUsable(pending) && pending?.value === true) return 'waiting';

  const deciding = decidingField(entity);
  if (isUsable(deciding) && deciding) {
    const word = lower(deciding.value);
    if (WAITING_VALUES.indexOf(word) >= 0) return 'waiting';
    if (RUNNING_VALUES.indexOf(word) >= 0) return 'running';
  }
  if (!hasUsable(entity)) return 'stale';
  return 'machine';
}

export interface SituationGroup {
  situation: Situation;
  entities: StateEntity[];
}

/**
 * Every row, in five groups, in `SITUATIONS` order and never in another.
 *
 * Empty groups are returned rather than dropped, so the screen's headings do
 * not move when a run finishes. Inside a group the least trustworthy row is
 * first: what needs a person's attention should not be below what does not.
 */
export function groupBySituation(entities: StateEntity[] | null | undefined): SituationGroup[] {
  const index = new Map<string, StateEntity[]>();
  for (const name of SITUATIONS) index.set(name, []);
  for (const entity of entities ?? []) {
    if (!entity || !entity.id) continue;
    const bucket = index.get(situationOf(entity));
    if (bucket) bucket.push(entity);
  }
  return SITUATIONS.map((situation) => ({
    situation: situation as Situation,
    entities: (index.get(situation) ?? []).slice().sort(entityByFreshness),
  }));
}

/** The heading each group is drawn under. A key `t()` translates, never a colour. */
export function situationLabel(situation: string): string {
  switch (situation) {
    case 'running': return 'Running right now';
    case 'waiting': return 'Waiting for you';
    case 'stale': return 'Stale or never observed';
    case 'conflict': return 'In conflict';
    default: return 'What the machine has';
  }
}

/* ── the change feed and the event stream ──────────────────────────────── */

export interface StateEvent {
  id: string;
  seq: number;
  name: string;
  entityId: string;
  at: string;
  payload: Record<string, unknown>;
}

export function eventFrom(raw: unknown): StateEvent {
  const e = obj(raw);
  const payload = obj(e.payload);
  return {
    id: str(e.id),
    seq: num(e.seq),
    name: str(e.name),
    entityId: str(e.entity_id) || str(payload.entity_id),
    at: str(e.at) || str(e.created_at),
    payload,
  };
}

export interface Resume {
  cursor: number;
  applied: StateEvent[];
  duplicates: number;
}

/**
 * Where a reconnection resumes, without a repeat and without a hole.
 *
 * The cursor means STRICTLY AFTER and it only ever moves forward. Two things
 * this function must therefore never do: move it backwards, which would make
 * a client ask for events it has already applied for ever, and silently drop
 * a repeat, which would hide the fact that a stream is re-sending. A repeat
 * is counted and discarded; the count is what a page can show.
 */
export function advanceCursor(cursor: number, events: StateEvent[] | null | undefined): Resume {
  const floor = num(cursor);
  let next = floor;
  const applied: StateEvent[] = [];
  const seen = new Set<string>();
  let duplicates = 0;
  for (const event of events ?? []) {
    if (!event) continue;
    const key = event.id || String(event.seq);
    if (event.seq <= floor || seen.has(key)) {
      duplicates += 1;
      continue;
    }
    seen.add(key);
    applied.push(event);
    if (event.seq > next) next = event.seq;
  }
  return { cursor: next, applied, duplicates };
}

export interface ChangeBatch {
  cursor: number;
  states: StateEntity[];
}

export function changesFrom(raw: unknown): ChangeBatch {
  const body = obj(raw);
  return {
    cursor: num(body.cursor),
    states: asArray<unknown>(body.states).map(entityFrom).filter((e) => e.id),
  };
}

/**
 * One row updated by a newer one, keeping what the newer one does not carry.
 *
 * A change batch carries materialised STATE and no entity row, so it knows the
 * fields and not the kind or the display name. Overwriting wholesale would
 * blank the label the reader is looking at every time a number moved, which is
 * a worse bug than a stale label because it is invisible in a screenshot.
 */
export function mergeEntity(current: StateEntity, incoming: StateEntity): StateEntity {
  return {
    ...incoming,
    kind: incoming.kind || current.kind,
    owner: incoming.owner || current.owner,
    projectId: incoming.projectId || current.projectId,
    displayName: incoming.displayName || current.displayName,
    labels: incoming.labels.length ? incoming.labels : current.labels,
    schema: incoming.schema || current.schema,
    retiredAt: incoming.retiredAt || current.retiredAt,
    unknownFields: incoming.unknownFields.length ? incoming.unknownFields : current.unknownFields,
  };
}

/**
 * The list after a batch of changes, with each changed row updated in place.
 *
 * In place, not appended and not re-sorted: a row that jumped from `fresh` to
 * `stale` while the reader was looking at it must not also jump across the
 * screen, or the thing they were reading is gone by the time they react.
 * Rows the batch has never mentioned are untouched; new ones go on the end.
 */
export function mergeEntities(current: StateEntity[], incoming: StateEntity[]): StateEntity[] {
  const byId = new Map<string, StateEntity>();
  for (const entity of incoming ?? []) {
    if (entity && entity.id) byId.set(entity.id, entity);
  }
  const out = (current ?? []).map((entity) => {
    const fresher = byId.get(entity.id);
    if (!fresher) return entity;
    byId.delete(entity.id);
    return mergeEntity(entity, fresher);
  });
  return out.concat(Array.from(byId.values()));
}

/**
 * Stamp the open conflicts onto the rows they are about.
 *
 * A listing row carries a COUNT of its conflicts and not their ids, so without
 * this a disputed entity would be grouped by whatever its fields happen to say
 * — which is precisely the claim the reducer refused to make. The conflicts
 * come from `/conflicts`, which is owner-scoped on the server, so nothing here
 * can attach one to a row the caller may not see.
 */
export function withConflicts(entities: StateEntity[], conflicts: Conflict[]): StateEntity[] {
  const byEntity = new Map<string, string[]>();
  for (const conflict of conflicts ?? []) {
    if (!conflict || !conflict.entityId || conflict.status !== 'reconciling') continue;
    const ids = byEntity.get(conflict.entityId) ?? [];
    if (conflict.id) ids.push(conflict.id);
    byEntity.set(conflict.entityId, ids);
  }
  return (entities ?? []).map((entity) => {
    const ids = byEntity.get(entity.id);
    if (!ids) return entity;
    const merged = entity.conflictIds.slice();
    for (const id of ids) if (merged.indexOf(id) < 0) merged.push(id);
    // An entity named by an open conflict is in conflict even when the row
    // itself carried no id: `conflictIds` is what `situationOf` reads, and an
    // empty list there would file a disputed row under a live grouping.
    return { ...entity, conflictIds: merged.length ? merged : [entity.id] };
  });
}

/* ── the API ───────────────────────────────────────────────────────────── */

const BASE = '/api/state';
const entityPath = (id: string, tail = ''): string => `${BASE}/entities/${encodeEntityId(id)}${tail}`;

/**
 * A read, with the refusal convention applied. A rejected READ is a 200 with
 * `ok: false`, so a caller that only checked the HTTP status would draw an
 * empty panel and call it "no state" — which is the one answer this screen is
 * not allowed to give by accident.
 */
async function read<T>(url: string, signal?: AbortSignal): Promise<T> {
  const payload = await getJson<unknown>(url, signal);
  const refusal = refusalOf(payload);
  if (refusal) throw new StateRefusal(refusal);
  return payload as T;
}

async function send<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: body === undefined
      ? { Accept: 'application/json' }
      : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => ({}))) as unknown;
  const refusal = refusalOf(payload);
  if (refusal) throw new StateRefusal(refusal);
  if (!response.ok) {
    const detail = str(obj(payload).detail) || `${url} responded ${response.status}`;
    throw new StateRefusal({
      code: response.status === 404 ? 'not_found' : 'refused', detail, path: '',
    });
  }
  return payload as T;
}

export interface EntityQuery {
  kind?: string;
  projectId?: string;
  namespace?: string;
  limit?: number;
}

export async function loadEntities(query: EntityQuery = {}, signal?: AbortSignal): Promise<StateEntity[]> {
  const params = new URLSearchParams();
  if (query.kind) params.set('kind', query.kind);
  if (query.projectId) params.set('project_id', query.projectId);
  if (query.namespace) params.set('namespace', query.namespace);
  if (query.limit) params.set('limit', String(query.limit));
  const suffix = params.toString();
  const payload = await read<unknown>(`${BASE}/entities${suffix ? `?${suffix}` : ''}`, signal);
  return asArray<unknown>(payload, 'entities').map(entityFrom).filter((e) => e.id);
}

export async function loadEntity(id: string, signal?: AbortSignal): Promise<StateEntity> {
  const payload = obj(await read<unknown>(entityPath(id), signal));
  return entityFrom('entity' in payload ? payload.entity : payload);
}

export async function loadHistory(id: string, limit = 50, signal?: AbortSignal): Promise<Observation[]> {
  const payload = await read<unknown>(`${entityPath(id, '/history')}?limit=${encodeURIComponent(String(limit))}`, signal);
  return asArray<unknown>(payload, 'observations').map(observationFrom);
}

export async function loadChanges(cursor: number, signal?: AbortSignal): Promise<ChangeBatch> {
  return changesFrom(await read<unknown>(`${BASE}/changes?cursor=${encodeURIComponent(String(cursor))}`, signal));
}

export async function loadConflicts(entityId = '', signal?: AbortSignal): Promise<Conflict[]> {
  const query = entityId ? `?entity_id=${encodeEntityId(entityId)}` : '';
  const payload = await read<unknown>(`${BASE}/conflicts${query}`, signal);
  return asArray<unknown>(payload, 'conflicts').map(conflictFrom);
}

export async function loadDiagnostics(signal?: AbortSignal): Promise<Diagnostics> {
  const payload = obj(await read<unknown>(`${BASE}/diagnostics`, signal));
  return diagnosticsFrom('diagnostics' in payload ? payload.diagnostics : payload);
}

/**
 * Look again, now. The one read on this screen that COSTS the machine, which
 * is why it is a POST and why the flag can refuse it: everything else here
 * reports what was already observed and stays answering whatever the switch
 * says.
 */
export async function refresh(entityId = '', source = ''): Promise<Record<string, unknown>> {
  return obj(await send<unknown>(`${BASE}/refresh`, { entity_id: entityId, source }));
}

/** Settle what two sources disagree about. A person asks for this, never a tool. */
export async function reconcile(): Promise<Record<string, unknown>> {
  return obj(await send<unknown>(`${BASE}/reconcile`));
}

export async function loadSituation(name: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const payload = obj(await read<unknown>(`${BASE}/situation/${encodeURIComponent(str(name))}`, signal));
  return obj('situation' in payload ? payload.situation : payload);
}

/**
 * Follow the mirror live.
 *
 * The frames are UNNAMED and carry the event name inside the JSON, which is
 * the dialect the rest of Faustus speaks: a NAMED SSE frame never reaches
 * `onmessage`, so a page written against the unnamed stream goes silently
 * deaf on a named one. The single named frame is `end`, and it is not a
 * failure: the stream has a deadline and is asking to be reopened from the
 * cursor the caller now holds.
 */
export function followState(
  since: number, onEvent: (event: StateEvent) => void, onFail: () => void, onEnd?: () => void,
): () => void {
  if (typeof EventSource === 'undefined') {
    onFail();
    return () => {};
  }
  let source: EventSource | null = null;
  try {
    source = new EventSource(`${BASE}/events?since=${encodeURIComponent(String(since))}`);
  } catch {
    onFail();
    return () => {};
  }
  const close = () => {
    if (!source) return;
    try {
      source.close();
    } catch {
      /* already closed */
    }
    source = null;
  };
  source.onmessage = (frame: MessageEvent<string>) => {
    try {
      onEvent(eventFrom(JSON.parse(frame.data) as unknown));
    } catch {
      /* one unreadable frame is not a dead stream */
    }
  };
  source.addEventListener('end', () => {
    close();
    if (onEnd) onEnd();
    else onFail();
  });
  source.onerror = () => {
    close();
    onFail();
  };
  return close;
}
