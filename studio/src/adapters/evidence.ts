import { ApiError, responseReason } from './api';

/**
 * BENCH-03 — the evidence inspector's one call: hand back an EvidenceRef the
 * caller already holds (routes/evidence_routes.py mirrors
 * `src/contracts/tool.py::EvidenceRef.to_mapping()` exactly, so no reshaping
 * happens on the way there or back) and get today's state of that same
 * range, with a plain answer on whether it still matches.
 */

export interface EvidenceLocator {
  kind: string;
  value: string;
}

/** Wire shape of `EvidenceRef.to_mapping()` — every field the contract
 *  carries, kept as-is rather than narrowed, so a field this screen does
 *  not use yet is not silently dropped on a round trip. */
export interface EvidenceRef {
  schema_version: string;
  evidence_id: string;
  owner_id: string;
  project_id: string | null;
  source_type: string;
  source_ref: string;
  source_revision: string;
  content_sha256: string | null;
  captured_at: string;
  locator: EvidenceLocator;
  derived_from: string[];
  retention: string;
}

export interface EvidenceResolution {
  evidence: EvidenceRef;
  currentAvailable: boolean;
  currentContent: string | null;
  /** null = not checkable from here; true/false once a real comparison ran. */
  stillValid: boolean | null;
  reason: string | null;
}

function resolutionFrom(raw: Record<string, unknown>): EvidenceResolution {
  return {
    evidence: raw.evidence as EvidenceRef,
    currentAvailable: raw.current_available === true,
    currentContent: typeof raw.current_content === 'string' ? raw.current_content : null,
    stillValid: typeof raw.still_valid === 'boolean' ? raw.still_valid : null,
    reason: typeof raw.reason === 'string' ? raw.reason : null,
  };
}

/** `workspace` is required only to re-read `source_type === 'file'`
 *  evidence; omit it for other source types (the resolver says plainly
 *  that it cannot re-check them rather than guessing). */
export async function resolveEvidence(evidence: EvidenceRef, workspace?: string, signal?: AbortSignal): Promise<EvidenceResolution> {
  const response = await fetch('/api/evidence/resolve', {
    method: 'POST',
    credentials: 'same-origin',
    signal,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ evidence, workspace: workspace ?? null }),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, '/api/evidence/resolve'), response.status);
  return resolutionFrom(await response.json());
}
