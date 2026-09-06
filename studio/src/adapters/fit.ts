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
