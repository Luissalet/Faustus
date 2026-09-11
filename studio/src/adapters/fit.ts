import { useEffect, useState } from 'react';
import { getJson } from './api';
import { fmtGb } from './localModels';
import { t } from '../i18n';

/**
 * Will this model fit on this card?
 *
 * Picking a local model that does not fit is not an error — Ollama loads it
 * and pushes the overflow onto the CPU, so it simply runs at a tenth of the
 * speed and nothing says why. `/api/models/fit` measures the weights against
 * the free VRAM and answers per model.
 *
 * `state` is ABSENT whenever the server cannot tell (no nvidia-smi, a remote
 * endpoint, an unknown size). Nothing is drawn in that case: an invented
 * verdict is worse than no verdict, because people act on it.
 *
 * The answer is cached server-side and again here for the life of the page:
 * it changes when a model is loaded or unloaded, not between keystrokes, and
 * this is read every time the picker opens.
 */

export type FitState = 'fits' | 'tight' | 'over';

export interface ModelFit {
  /** Absent when the server could not tell. */
  state?: FitState;
  sizeBytes?: number;
  /** The server's own sentence, for the row's tooltip. */
  note?: string;
  /**
   * The blob digest, when Ollama gave one. Two tags with the SAME digest are
   * the same weights under two names — `qwen3.8:latest` and
   * `qwen3.8:27b-q8_0` — and a picker that lists both as separate models is
   * asking a question with no answer. A name resemblance is not enough:
   * `q4_K_M` and `q8_0` look alike and are genuinely different.
   */
  digest?: string;
}

export interface FitVram {
  supported: boolean;
  reason?: string;
  /** What a model's weights can take right now, KV cache not included. */
  budgetBytes?: number;
  totalBytes?: number;
  /** Cards the budget is spread over: with more than one, Ollama pools them. */
  count?: number;
  name?: string;
}

export interface FitHints {
  vram: FitVram;
  /**
   * The endpoints these verdicts are about: the Ollama servers on THIS
   * machine, which is the only card we can measure. A LAN or tailnet
   * endpoint can serve a tag with the very same name off somebody else's
   * GPU, and a row that borrowed our answer would be a confident lie.
   */
  endpointIds: string[];
  /** Keyed by the model tag as the picker spells it. */
  models: Record<string, ModelFit>;
}

const EMPTY: FitHints = { vram: { supported: false }, endpointIds: [], models: {} };

const STATES: FitState[] = ['fits', 'tight', 'over'];

function parse(raw: Record<string, unknown>): FitHints {
  const vramRaw = (raw.vram ?? {}) as Record<string, unknown>;
  const modelsRaw = (raw.models ?? {}) as Record<string, Record<string, unknown>>;
  const models: Record<string, ModelFit> = {};
  for (const [name, m] of Object.entries(modelsRaw)) {
    const state = m?.state;
    models[name] = {
      state: STATES.includes(state as FitState) ? (state as FitState) : undefined,
      sizeBytes: typeof m?.size_bytes === 'number' ? m.size_bytes : undefined,
      note: typeof m?.note === 'string' ? m.note : undefined,
      digest: typeof m?.digest === 'string' && m.digest ? m.digest : undefined,
    };
  }
  const num = (v: unknown): number | undefined => (typeof v === 'number' && v > 0 ? v : undefined);
  return {
    vram: {
      supported: Boolean(vramRaw.supported),
      reason: typeof vramRaw.reason === 'string' ? vramRaw.reason : undefined,
      budgetBytes: num(vramRaw.budget_bytes),
      totalBytes: num(vramRaw.total_bytes),
      count: num(vramRaw.count),
      name: typeof vramRaw.name === 'string' ? vramRaw.name : undefined,
    },
    endpointIds: Array.isArray(raw.endpoint_ids) ? raw.endpoint_ids.map(String) : [],
    models,
  };
}

let cached: Promise<FitHints> | null = null;

export function fitHints(refresh = false): Promise<FitHints> {
  if (refresh) cached = null;
  if (!cached) {
    cached = getJson<Record<string, unknown>>(`/api/models/fit${refresh ? '?refresh=true' : ''}`)
      .then(parse)
      .catch(() => EMPTY);
  }
  return cached;
}

/**
 * Read once when the picker opens.
 *
 * A failed read keeps whatever was there rather than blanking the badges:
 * "we could not ask just now" is not "it does not fit".
 */
export function useFitHints(active: boolean): FitHints {
  const [hints, setHints] = useState<FitHints>(EMPTY);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    void fitHints().then((h) => {
      if (alive) setHints(h);
    });
    return () => {
      alive = false;
    };
  }, [active]);
  return hints;
}

/** The word for a state. Never a colour on its own. */
export const FIT_WORD: Record<FitState, string> = {
  fits: 'fits',
  tight: 'tight',
  over: 'no room',
};

/**
 * What we know about the model on THIS row, or nothing at all.
 *
 * Gated on the endpoint, not on the name: `/api/models/fit` measures the
 * Ollama servers running on this machine and says which endpoint ids those
 * are. A row served from another box keeps its name and gets no annotation —
 * neither a size nor a verdict — because both would be about our card.
 */
export function fitOf(route: { model: string; endpointId: string }, hints: FitHints): ModelFit | undefined {
  if (!hints.endpointIds.includes(route.endpointId)) return undefined;
  return hints.models[route.model];
}

/**
 * The weights on disk, the number every verdict is actually about.
 *
 * This is the answer to "no room compared to WHAT?", and it is a fact even
 * when there is no card to judge it against, so it is drawn whenever Ollama
 * gave it — with or without a verdict beside it. Empty when unknown.
 */
export function fitSize(fit?: ModelFit): string {
  return fit?.sizeBytes ? fmtGb(fit.sizeBytes) : '';
}

/**
 * "16.4 GB · no room" — size and verdict as one string, for the places that
 * can only render text (a native `<select>` option). Either half may be
 * missing; neither is ever invented.
 */
export function fitSummary(fit?: ModelFit): string {
  const word = fit?.state ? t(FIT_WORD[fit.state]) : '';
  return [fitSize(fit), word].filter(Boolean).join(' · ');
}

/**
 * SET-02: privacy + cost, per endpoint (`GET /api/models/endpoint-profile`,
 * routes/local_models_routes.py — the picker's other two informative
 * badges, next to fit above and capabilities below). Same "read once when
 * the picker opens, keep the last answer on a failed refresh" shape as
 * `useFitHints`.
 */
export type ModelCost = 'free_local' | 'paid' | 'unconfigured';

export interface EndpointProfile {
  isLocal: boolean;
  cost: ModelCost;
  hasApiKey: boolean;
}

let profileCached: Promise<Record<string, EndpointProfile>> | null = null;

export function endpointProfiles(refresh = false): Promise<Record<string, EndpointProfile>> {
  if (refresh) profileCached = null;
  if (!profileCached) {
    profileCached = getJson<{ endpoints?: Record<string, { is_local?: boolean; cost?: ModelCost; has_api_key?: boolean }> }>('/api/models/endpoint-profile')
      .then((raw) => {
        const out: Record<string, EndpointProfile> = {};
        for (const [id, p] of Object.entries(raw.endpoints ?? {})) {
          out[id] = { isLocal: Boolean(p.is_local), cost: p.cost ?? 'unconfigured', hasApiKey: Boolean(p.has_api_key) };
        }
        return out;
      })
      .catch(() => ({}));
  }
  return profileCached;
}

export function useEndpointProfiles(active: boolean): Record<string, EndpointProfile> {
  const [profiles, setProfiles] = useState<Record<string, EndpointProfile>>({});
  useEffect(() => {
    if (!active) return;
    let alive = true;
    void endpointProfiles().then((p) => {
      if (alive) setProfiles(p);
    });
    return () => {
      alive = false;
    };
  }, [active]);
  return profiles;
}

/** "no per-token cost" / "billed by the provider" / "no key stored — calls will likely fail". */
export function costLabel(cost: ModelCost): string {
  if (cost === 'free_local') return t('Local — no per-token cost');
  if (cost === 'paid') return t('Billed by the provider');
  return t('No API key stored — calls will likely fail');
}

/**
 * SET-02: tested capabilities (`GET /api/models/{name}/capabilities`,
 * routes/local_models_routes.py — MOD-01/MOD-02's manifest). Deliberately
 * NOT fetched for every row when the picker opens: each call is a live
 * round trip to that model's endpoint (an `/api/show`), so this is exposed
 * as a plain on-demand function — the picker calls it once per row, on
 * hover/focus, and caches the answer (see ModelPalette.tsx) — rather than a
 * hook that would fire it for every row the moment the palette mounts.
 */
export interface TestedCapability {
  ok: boolean | null;
  testedAt?: string;
}

export interface ModelCapabilityManifest {
  /** Present only for a key this install has actually run a probe for. */
  tested: Partial<Record<'tool_calling' | 'vision' | 'json_mode', TestedCapability>>;
}

const TESTED_KEYS = ['tool_calling', 'vision', 'json_mode'] as const;

export async function fetchModelCapabilities(model: string, endpointId: string): Promise<ModelCapabilityManifest> {
  const raw = await getJson<{ tested?: Record<string, { ok?: boolean | null; tested_at?: string }> }>(
    `/api/models/${encodeURIComponent(model)}/capabilities?endpoint_id=${encodeURIComponent(endpointId)}`,
  );
  const tested: ModelCapabilityManifest['tested'] = {};
  for (const key of TESTED_KEYS) {
    const entry = raw.tested?.[key];
    if (entry && entry.ok !== undefined) tested[key] = { ok: entry.ok ?? null, testedAt: entry.tested_at };
  }
  return { tested };
}

/**
 * CMP-11 (`GET /api/models/fit-explain`, `routes/model_routes.py`): the
 * picker's OTHER capability badge, and the one this lote adds. Distinct from
 * `fetchModelCapabilities` above (MOD-01/MOD-02's raw tested/failed pair) in
 * exactly the way `src/model_capabilities.py::explain_fit`'s docstring
 * describes: a capability is never reduced to yes/no. It comes back as one
 * of four states — `tested` (this exact model+connection was probed and it
 * worked), `announced` (claimed, never verified here), `unknown` (no
 * evidence either way) or `missing` (proven absent) — each with the WHY in
 * plain words and, when the model falls short, which other already-
 * evidenced models on this endpoint (or installed) DO meet it. `unknown`
 * still renders a badge: unlike the VRAM verdict above, silence here would
 * be exactly the "requirement dropped without a word" CMP-11 exists to
 * rule out.
 */
export type FitExplainState = 'tested' | 'announced' | 'unknown' | 'missing';

export interface FitReason {
  capability: string;
  state: FitExplainState;
  /** The server's own sentence: why this state, for the badge's tooltip. */
  message: string;
  /** Other model ids, on this endpoint or installed, that DO meet it. */
  alternatives: string[];
}

export interface FitExplain {
  ok: boolean;
  reasons: FitReason[];
}

const FIT_EXPLAIN_STATES: FitExplainState[] = ['tested', 'announced', 'unknown', 'missing'];
const EMPTY_FIT_EXPLAIN: FitExplain = { ok: true, reasons: [] };

/** The capabilities the model picker asks about for every row — the exact
 *  set INFORME V2 §3.10 names: tools, structured output, vision, image
 *  editing. A caller that cares about a narrower or wider set (a workflow
 *  node inspector, say) passes its own `needs` instead. */
export const PICKER_CAPABILITY_NEEDS = ['tools', 'json', 'vision', 'images_edit'];

/** Short badge word per state — never a colour on its own (same rule as
 *  FIT_WORD above). */
export const FIT_EXPLAIN_WORD: Record<FitExplainState, string> = {
  tested: 'tested',
  announced: 'announced',
  unknown: 'unknown',
  missing: 'missing',
};

/** Short label per capability token, for the badge itself (the tooltip
 *  carries the full sentence). */
export const FIT_EXPLAIN_CAP_LABEL: Record<string, string> = {
  tool_call: 'tools',
  json_mode: 'structured output',
  vision: 'vision',
  image_editing: 'image edit',
};

export async function fetchFitExplain(model: string, endpointId: string, needs: string[] = PICKER_CAPABILITY_NEEDS): Promise<FitExplain> {
  try {
    const raw = await getJson<{ ok?: boolean; reasons?: Array<Record<string, unknown>> }>(
      `/api/models/fit-explain?model=${encodeURIComponent(model)}&endpoint_id=${encodeURIComponent(endpointId)}&needs=${encodeURIComponent(needs.join(','))}`,
    );
    const reasons: FitReason[] = (raw.reasons ?? []).map((r) => ({
      capability: String(r.capability ?? ''),
      state: FIT_EXPLAIN_STATES.includes(r.state as FitExplainState) ? (r.state as FitExplainState) : 'unknown',
      message: typeof r.message === 'string' ? r.message : '',
      alternatives: Array.isArray(r.alternatives) ? r.alternatives.map(String) : [],
    }));
    return { ok: raw.ok !== false, reasons };
  } catch {
    // A failed read leaves the row with no badges, not a wrong one — same
    // "keep silent, never invent" rule as fitHints's own .catch above.
    return EMPTY_FIT_EXPLAIN;
  }
}

/**
 * The other tags that are the same weights as this one.
 *
 * Empty when there is no digest: not knowing is the honest answer, and
 * guessing from the name is how `q4_K_M` gets called an alias of `q8_0`.
 */
export function aliasesOf(model: string, hints: FitHints): string[] {
  const digest = hints.models[model]?.digest;
  if (!digest) return [];
  return Object.entries(hints.models)
    .filter(([name, m]) => name !== model && m.digest === digest)
    .map(([name]) => name)
    .sort();
}
