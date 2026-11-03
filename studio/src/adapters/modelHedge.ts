import { ApiError, getJson, responseReason } from './api';

/**
 * Hedged first token (`/api/model-hedge`, backend `src/model_hedge.py`).
 * When the chat model releases no first token within the delay, the same
 * request also goes to the hedge target and the stream that answers first
 * is used; the other one is closed.
 */

export interface HedgeTargetOption {
  value: string;
  label: string;
  endpoint_id?: string;
  model?: string;
}

export interface HedgeRace {
  ts: number;
  /** null for a re-send that was due but skipped (`outcome: 'skipped'`). */
  winner: 'primary' | 'hedge' | null;
  outcome: 'first_token' | 'error' | 'empty' | 'skipped';
  /** `reissue`: the same request re-sent to the same server. */
  hedge_kind?: string;
  /** For a skipped re-send: `limit` (turn budget spent) or `saturated` (server queue). */
  skip_reason?: string;
  server_waiting?: number | null;
  after_s: number;
  delay_s: number;
  hedge_started_s: number;
  loser_cancelled: boolean;
  primary_model: string;
  primary_endpoint_label?: string;
  hedge_model?: string;
  hedge_endpoint_label?: string;
  answered_by?: string;
  answered_by_endpoint_label?: string;
}

export interface HedgeStatus {
  enabled: boolean;
  delay_s: number;
  target: string;
  delay_overrides: Record<string, number>;
  /** Re-sends to the same server allowed per turn (target `reissue`). */
  reissue_max: number;
  reissue_overrides: Record<string, number>;
  min_delay_s: number;
  max_delay_s: number;
  max_reissue_max?: number;
  current: null | {
    model: string;
    endpoint_label?: string;
    delay_s: number;
    reissue_max?: number;
    hedge: null | { kind: string; model: string; endpoint_label?: string; delay_s: number };
  };
  targets: HedgeTargetOption[];
  recent: HedgeRace[];
  stats: {
    races: number; hedge_wins: number; primary_wins?: number; avg_hedge_answer_s?: number | null;
    reissue_wins?: number; reissue_skips?: number; reissue_skips_saturated?: number;
  };
}

export type HedgePatch = Partial<Pick<HedgeStatus,
  'enabled' | 'delay_s' | 'target' | 'delay_overrides' | 'reissue_max' | 'reissue_overrides'>>;

export function getHedgeStatus(signal?: AbortSignal): Promise<HedgeStatus> {
  return getJson<HedgeStatus>('/api/model-hedge', signal);
}

export async function updateHedge(patch: HedgePatch): Promise<HedgeStatus> {
  const path = '/api/model-hedge';
  const response = await fetch(path, {
    method: 'PATCH',
    credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as HedgeStatus;
}
