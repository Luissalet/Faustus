import { useEffect, useState } from 'react';
import { getJson } from './api';

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

export interface FitHints {
  vram: { supported: boolean; reason?: string };
  /** Keyed by the model tag as the picker spells it. */
  models: Record<string, ModelFit>;
}

const EMPTY: FitHints = { vram: { supported: false }, models: {} };

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
  return { vram: { supported: Boolean(vramRaw.supported), reason: typeof vramRaw.reason === 'string' ? vramRaw.reason : undefined }, models };
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
