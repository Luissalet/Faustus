import { getJson } from './api';

/**
 * Tool-catalogue footprint (FAUSTUS §277 / OBJ-56) — prompt cost by source,
 * name collisions, near-duplicate descriptions, and oversized descriptions.
 * Read-only: never changes the catalogue.
 */

export interface FootprintSourceBucket {
  source: string;
  tools: number;
  tokens: number;
  share?: number;
  avg_tokens?: number;
}

export interface FootprintCollision {
  short_name: string;
  tools: string[];
  sources: string[];
  tokens: number;
}

export interface FootprintNearDuplicate {
  a: string;
  b: string;
  similarity: number;
  same_source: boolean;
  tokens: number;
}

export interface FootprintOversized {
  name: string;
  source: string;
  description_tokens: number;
}

export interface FootprintHeaviest {
  name: string;
  source: string;
  tokens: number;
  description_tokens: number;
  schema_tokens: number;
  exposure?: string | null;
}

export interface ToolFootprintReport {
  model: string;
  count: number;
  total_tokens: number;
  offered_tokens: number;
  deferred_tokens: number;
  hidden_tokens: number;
  hidden_tools: number;
  by_source: FootprintSourceBucket[];
  by_exposure?: Record<string, { tools: number; tokens: number }>;
  heaviest: FootprintHeaviest[];
  oversized_descriptions: FootprintOversized[];
  name_collisions: FootprintCollision[];
  near_duplicates: FootprintNearDuplicate[];
  thresholds: {
    similarity: number;
    oversized_description_tokens: number;
    min_words_to_compare?: number;
  };
  fingerprint?: string;
  mcp_attached?: boolean;
  checked_at?: string;
}

export interface ToolFootprintFilter {
  source?: string;
  top?: number;
  similarity?: number;
  model?: string;
}

function qs(params: Record<string, string | number | undefined>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

/** GET /api/tools/footprint — admin catalogue audit. */
export function loadToolFootprint(
  filter: ToolFootprintFilter = {},
  signal?: AbortSignal,
): Promise<ToolFootprintReport> {
  return getJson<ToolFootprintReport>(
    `/api/tools/footprint${qs({
      source: filter.source,
      top: filter.top,
      similarity: filter.similarity,
      model: filter.model,
    })}`,
    signal,
  );
}

/** Human label for a footprint source id (`builtin` → Built-in). */
export function footprintSourceLabel(source: string): string {
  if (source === 'builtin') return 'Built-in';
  return source;
}
