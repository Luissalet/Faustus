import { ApiError, asArray } from './api';

/**
 * The "no room in VRAM — unload which?" question a loader asks before it
 * loads a model that does not fit next to what is resident (OBJ-1). The
 * loader publishes it as a progress event with `phase: "vram_blocked"`; the
 * screen shows the residents; the click goes back through `resolveAdmission`
 * and the loader continues — or not.
 */

export interface VramResident {
  name: string;
  inVramBytes: number;
  totalBytes: number;
  spillBytes: number;
  ctx: number;
}

export interface VramBlocked {
  ticket: string;
  model: string;
  measured: boolean;
  sizeBytes: number;
  kvBytes: number;
  needBytes: number;
  budgetAlongsideBytes: number;
  budgetIfUnloadedBytes: number;
  shortfallBytes: number;
  vramTotalBytes: number;
  gpuCount: number;
  gpuName: string;
  residents: VramResident[];
  suggestion: string[];
  suggestionEnough: boolean;
}

export type AdmissionAction = 'unload' | 'proceed' | 'cancel';

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

/** Parse a `vram_blocked` progress event; undefined for anything else. */
export function vramBlockedFrom(raw: Record<string, unknown>): VramBlocked | undefined {
  if (raw.phase !== 'vram_blocked' || typeof raw.ticket !== 'string' || !raw.ticket) return undefined;
  return {
    ticket: raw.ticket,
    model: String(raw.model ?? ''),
    measured: raw.measured === true,
    sizeBytes: num(raw.size_bytes),
    kvBytes: num(raw.kv_bytes),
    needBytes: num(raw.need_bytes),
    budgetAlongsideBytes: num(raw.budget_alongside_bytes),
    budgetIfUnloadedBytes: num(raw.budget_if_unloaded_bytes),
    shortfallBytes: num(raw.shortfall_bytes),
    vramTotalBytes: num(raw.vram_total_bytes),
    gpuCount: num(raw.gpu_count) || 1,
    gpuName: String(raw.gpu_name ?? ''),
    residents: asArray<Record<string, unknown>>(raw, 'residents').map((r) => ({
      name: String(r.name ?? ''),
      inVramBytes: num(r.in_vram_bytes),
      totalBytes: num(r.total_bytes),
      spillBytes: num(r.spill_bytes),
      ctx: num(r.ctx),
    })).filter((r) => r.name),
    suggestion: asArray<unknown>(raw, 'suggestion').map(String),
    suggestionEnough: raw.suggestion_enough === true,
  };
}

export async function resolveAdmission(ticket: string, action: AdmissionAction, names: string[] = []): Promise<void> {
  const res = await fetch(`/api/local-models/admission/${encodeURIComponent(ticket)}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, names }),
  });
  if (!res.ok) {
    let detail = '';
    try {
      detail = String(((await res.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      /* not json */
    }
    throw new ApiError(detail || `admission responded ${res.status}`, res.status);
  }
}
