import { ApiError, asArray, CLIENT_API_VERSION, CLIENT_VERSION_HEADER, getJson, responseReason } from './api';

async function postJson(path: string, body: unknown, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(path, {
    method: 'POST',
    signal,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return response.json();
}

/**
 * Schema extraction (`/api/extract*`) shaped for Studio.
 *
 * Product rule that must never be broken in a pixel: a value that the server
 * put in ``dropped`` is not data. ``validRows`` only walks ``data``; dropped
 * rows stay in their own list with ``why``/``code``. Limits are lexical
 * warnings, not proof that a value belongs to its field.
 */

const str = (value: unknown): string => (typeof value === 'string' ? value : '');
const flag = (value: unknown): boolean => value === true;

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
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

export type ExtractLimits = {
  message: string;
  detail: string;
};

export type ExtractResult = {
  data: unknown;
  evidence: ExtractEvidence[];
  dropped: ExtractDropped[];
  inferred: string[];
  missingRequired: string[];
  conflicts: unknown[];
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
  schema: unknown;
};

export function evidenceFrom(raw: unknown): ExtractEvidence[] {
  return asArray<unknown>(raw).map((row) => {
    const o = obj(row);
    return { path: str(o.path), quote: str(o.quote), unit: str(o.unit) };
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
  const o = obj(raw);
  if (!Object.keys(o).length) return null;
  const message = str(o.message) || str(o.summary) || str(o.note);
  const detail = str(o.detail) || str(o.explanation);
  if (!message && !detail) return null;
  return { message: message || 'limits', detail };
}

export function resultFrom(raw: unknown): ExtractResult {
  const o = obj(raw);
  return {
    data: o.data,
    evidence: evidenceFrom(o.evidence),
    dropped: droppedFrom(o.dropped),
    inferred: asArray<unknown>(o.inferred).map(str).filter(Boolean),
    missingRequired: asArray<unknown>(o.missing_required ?? o.missingRequired).map(str).filter(Boolean),
    conflicts: asArray(o.conflicts),
    schemaValid: flag(o.schema_valid ?? o.schemaValid),
    limits: limitsFrom(o.limits),
    route: str(o.route),
    source: str(o.source),
  };
}

export function profileFrom(raw: unknown): SchemaProfile {
  const o = obj(raw);
  const route = str(o.route) || str(obj(o.profile).route);
  const complexity = str(o.complexity) || str(obj(o.profile).complexity);
  const summary = str(o.summary) || str(o.note) || [complexity, route].filter(Boolean).join(' · ');
  return { route, complexity, summary, raw: o };
}

/** Flatten object ``data`` into path/value rows. Never includes dropped paths. */
export function validRows(data: unknown, prefix = ''): Array<{ path: string; value: unknown }> {
  if (data === null || data === undefined) return [];
  if (Array.isArray(data)) {
    return data.flatMap((item, i) => validRows(item, prefix ? `${prefix}/${i}` : String(i)));
  }
  if (typeof data === 'object') {
    return Object.entries(data as Record<string, unknown>).flatMap(([key, value]) => {
      const path = prefix ? `${prefix}/${key}` : key;
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

export async function listSchemas(signal?: AbortSignal): Promise<SavedSchema[]> {
  const raw = await getJson<unknown>('/api/extract/schemas', signal);
  const rows = asArray<unknown>(obj(raw).schemas ?? raw);
  return rows.map((row) => {
    const o = obj(row);
    return { name: str(o.name), schema: o.schema ?? o };
  }).filter((s) => s.name);
}

export async function saveSchema(name: string, schema: unknown, signal?: AbortSignal): Promise<void> {
  await postJson(`/api/extract/schemas/${encodeURIComponent(name)}`, { schema }, signal);
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
