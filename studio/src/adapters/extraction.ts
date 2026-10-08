import { ApiError, asArray, CLIENT_API_VERSION, CLIENT_VERSION_HEADER, getJson, responseReason } from './api';

async function sendJson(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(path, {
    method,
    signal,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(body !== undefined
        ? { 'Content-Type': 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION }
        : { [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  return JSON.parse(text);
}

async function postJson(path: string, body: unknown, signal?: AbortSignal): Promise<unknown> {
  return sendJson('POST', path, body, signal);
}

/**
 * Schema extraction (`/api/extract*`) shaped for Studio.
 *
 * Product rule: a value in ``dropped`` is never data. Paths match the API
 * (dot / bracket). Limits may be a list of {code,note}. route/profile are objects.
 */

const str = (value: unknown): string => (typeof value === 'string' ? value : '');
const flag = (value: unknown): boolean => value === true;

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** Page/part number from evidence.unit (integer) or legacy string. */
export function unitText(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'string') return value;
  return '';
}

function describeRoute(raw: unknown): string {
  if (typeof raw === 'string') return raw;
  const o = obj(raw);
  const parts = [str(o.purpose) || str(o.tier), str(o.model)].filter(Boolean);
  return parts.join(' · ');
}

export type ExtractEvidence = {
  path: string;
  quote: string;
  unit: string;
};

export type ExtractDropped = {
  path: string;
  value: unknown;
  why: string;
  code: string;
};

export type ExtractLimitItem = {
  code: string;
  note: string;
};

export type ExtractLimits = {
  message: string;
  detail: string;
  items: ExtractLimitItem[];
};

export type ExtractResult = {
  data: unknown;
  evidence: ExtractEvidence[];
  dropped: ExtractDropped[];
  inferred: string[];
  missingRequired: string[];
  conflicts: unknown[];
  errors: string[];
  schemaValid: boolean;
  limits: ExtractLimits | null;
  route: string;
  source: string;
};

export type SchemaProfile = {
  route: string;
  complexity: string;
  summary: string;
  raw: Record<string, unknown>;
};

export type SavedSchema = {
  name: string;
  /** Full schema object when loaded via getSchema; list endpoint only returns metadata. */
  schema: unknown;
  description?: string;
  tier?: string;
  updatedAt?: string;
};

export function evidenceFrom(raw: unknown): ExtractEvidence[] {
  return asArray<unknown>(raw).map((row) => {
    const o = obj(row);
    return { path: str(o.path), quote: str(o.quote), unit: unitText(o.unit) };
  }).filter((e) => e.path || e.quote);
}

export function droppedFrom(raw: unknown): ExtractDropped[] {
  return asArray<unknown>(raw).map((row) => {
    const o = obj(row);
    return {
      path: str(o.path),
      value: o.value,
      why: str(o.why) || str(o.reason),
      code: str(o.code),
    };
  }).filter((d) => d.path || d.why || d.code);
}

export function limitsFrom(raw: unknown): ExtractLimits | null {
  if (Array.isArray(raw)) {
    const items = raw.map((row) => {
      const o = obj(row);
      return { code: str(o.code), note: str(o.note) || str(o.message) || str(o.detail) };
    }).filter((i) => i.code || i.note);
    if (!items.length) return null;
    return {
      items,
      message: items.map((i) => i.note || i.code).join(' · '),
      detail: items.map((i) => i.code).filter(Boolean).join(', '),
    };
  }
  const o = obj(raw);
  if (!Object.keys(o).length) return null;
  const message = str(o.message) || str(o.summary) || str(o.note);
  const detail = str(o.detail) || str(o.explanation);
  if (!message && !detail) return null;
  return { message: message || 'limits', detail, items: [] };
}

export function resultFrom(raw: unknown): ExtractResult {
  const o = obj(raw);
  const errors = asArray<unknown>(o.errors).map((e) => (typeof e === 'string' ? e : JSON.stringify(e))).filter(Boolean);
  return {
    data: o.data,
    evidence: evidenceFrom(o.evidence),
    dropped: droppedFrom(o.dropped),
    inferred: asArray<unknown>(o.inferred).map(str).filter(Boolean),
    missingRequired: asArray<unknown>(o.missing_required ?? o.missingRequired).map(str).filter(Boolean),
    conflicts: asArray(o.conflicts),
    errors,
    schemaValid: flag(o.schema_valid ?? o.schemaValid),
    limits: limitsFrom(o.limits),
    route: describeRoute(o.route),
    source: str(o.source),
  };
}

export function profileFrom(raw: unknown): SchemaProfile {
  const o = obj(raw);
  const profile = obj(o.profile);
  const routeObj = obj(o.route);
  const tier = str(profile.tier) || str(routeObj.tier) || str(o.complexity);
  const purpose = str(routeObj.purpose);
  const model = str(routeObj.model);
  const reasons = asArray<unknown>(profile.reasons ?? routeObj.reasons).map(str).filter(Boolean);
  const route = describeRoute(o.route) || [purpose, model].filter(Boolean).join(' · ') || tier;
  const complexity = tier;
  let summary = str(o.summary) || str(o.note);
  if (!summary) {
    summary = reasons.length ? `${tier || 'profile'}: ${reasons.join('; ')}` : [complexity, route].filter(Boolean).join(' · ');
  }
  return { route, complexity, summary, raw: o };
}

/** Flatten ``data`` with API paths: ``nested.a``, ``lines[0].amount``. */
export function validRows(data: unknown, prefix = ''): Array<{ path: string; value: unknown }> {
  if (data === null || data === undefined) return [];
  if (Array.isArray(data)) {
    return data.flatMap((item, i) => {
      const path = prefix ? `${prefix}[${i}]` : `[${i}]`;
      return validRows(item, path);
    });
  }
  if (typeof data === 'object') {
    return Object.entries(data as Record<string, unknown>).flatMap(([key, value]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      if (value !== null && typeof value === 'object') return validRows(value, path);
      return [{ path, value }];
    });
  }
  return prefix ? [{ path: prefix, value: data }] : [];
}

/**
 * Rows the UI may treat as extracted answers. Paths that also appear in
 * ``dropped`` are stripped so a discarded value cannot be shown as valid.
 */
export function displayDataRows(result: ExtractResult): Array<{ path: string; value: unknown; evidence: ExtractEvidence | null; inferred: boolean }> {
  const droppedPaths = new Set(result.dropped.map((d) => d.path).filter(Boolean));
  const byPath = new Map(result.evidence.map((e) => [e.path, e]));
  const inferred = new Set(result.inferred);
  return validRows(result.data)
    .filter((row) => !droppedPaths.has(row.path))
    .map((row) => ({
      path: row.path,
      value: row.value,
      evidence: byPath.get(row.path) ?? null,
      inferred: inferred.has(row.path),
    }));
}

export function parseSchemaText(text: string): { ok: true; schema: unknown } | { ok: false; error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { ok: false, error: 'empty schema' };
  try {
    const schema = JSON.parse(trimmed);
    if (!schema || typeof schema !== 'object' || Array.isArray(schema)) {
      return { ok: false, error: 'schema must be a JSON object' };
    }
    return { ok: true, schema };
  } catch (exc) {
    return { ok: false, error: exc instanceof Error ? exc.message : 'invalid JSON' };
  }
}

export function schemaNameOk(name: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(name);
}

/** List endpoint returns metadata only (name/description/tier/…); schema body is not the user JSON Schema. */
export async function listSchemas(signal?: AbortSignal): Promise<SavedSchema[]> {
  const raw = await getJson<unknown>('/api/extract/schemas', signal);
  const rows = asArray<unknown>(obj(raw).schemas ?? raw);
  return rows.map((row) => {
    const o = obj(row);
    return {
      name: str(o.name),
      schema: null,
      description: str(o.description),
      tier: str(o.tier),
      updatedAt: str(o.updated_at ?? o.updatedAt),
    };
  }).filter((s) => s.name);
}

export async function getSchema(name: string, signal?: AbortSignal): Promise<SavedSchema> {
  const raw = await getJson<unknown>(`/api/extract/schemas/${encodeURIComponent(name)}`, signal);
  const o = obj(raw);
  const schema = o.schema !== undefined ? o.schema : raw;
  return {
    name: str(o.name) || name,
    schema,
    description: str(o.description),
    tier: str(o.tier),
    updatedAt: str(o.updated_at ?? o.updatedAt),
  };
}

/** API upserts with PUT (POST returns 405). */
export async function saveSchema(name: string, schema: unknown, signal?: AbortSignal): Promise<void> {
  await sendJson('PUT', `/api/extract/schemas/${encodeURIComponent(name)}`, { schema }, signal);
}

export async function deleteSchema(name: string, signal?: AbortSignal): Promise<void> {
  const response = await fetch(`/api/extract/schemas/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    signal,
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`delete schema ${response.status}`);
}

export async function fetchProfile(schema: unknown, signal?: AbortSignal): Promise<SchemaProfile> {
  const raw = await postJson('/api/extract/profile', { schema }, signal);
  return profileFrom(raw);
}

export async function runExtract(body: Record<string, unknown>, signal?: AbortSignal): Promise<ExtractResult> {
  const raw = await postJson('/api/extract', body, signal);
  return resultFrom(raw);
}
