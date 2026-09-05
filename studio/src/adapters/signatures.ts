import { ApiError, asArray, getJson } from './api';

/** Saved signatures (routes/signature_routes): PNG data URLs the PDF pane stamps into fields. */
export interface Signature {
  id: string;
  name: string;
  dataUrl: string;
  width: number;
  height: number;
  createdAt: string | null;
}

function from(raw: Record<string, unknown>): Signature {
  return { id: String(raw.id ?? ''), name: String(raw.name ?? ''), dataUrl: String(raw.data_url ?? ''), width: Number(raw.width) || 0, height: Number(raw.height) || 0, createdAt: typeof raw.created_at === 'string' ? raw.created_at : null };
}

export async function listSignatures(): Promise<Signature[]> {
  const raw = await getJson<Record<string, unknown>>('/api/signatures');
  return asArray<Record<string, unknown>>(raw, 'signatures').map(from);
}

export async function createSignature(input: { dataUrl: string; width: number; height: number; name: string }): Promise<Signature> {
  const res = await fetch('/api/signatures', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ data: input.dataUrl, width: input.width, height: input.height, name: input.name }) });
  if (!res.ok) throw new ApiError((await res.text().catch(() => '')) || `signatures responded ${res.status}`, res.status);
  const raw = (await res.json()) as Record<string, unknown>;
  return from((raw.signature as Record<string, unknown>) ?? raw);
}

export async function deleteSignature(id: string): Promise<void> {
  const res = await fetch(`/api/signatures/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' });
  if (!res.ok) throw new ApiError(`signatures responded ${res.status}`, res.status);
}

const LAST_KEY = 'fs-signature-last';

export function lastSignatureId(): string | null {
  try {
    return localStorage.getItem(LAST_KEY);
  } catch {
    return null;
  }
}

export function rememberSignature(id: string): void {
  try {
    localStorage.setItem(LAST_KEY, id);
  } catch {
    /* private mode */
  }
}
