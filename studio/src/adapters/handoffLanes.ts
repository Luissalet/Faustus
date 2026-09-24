import { getJson } from './api';

/**
 * Handoff lanes (lot E): the Studio half of `GET/PUT /api/handoff-lanes`,
 * `/graph` and `/test` (`docs/api/handoff_lanes.md`, `src/handoff_lanes.py`).
 * Same shape as `adapters/account.ts`'s small admin-endpoint wrappers —
 * `getJson` for reads, a thin `ok()` + `fetch` for the write.
 */

export type HandoffLanesMode = 'off' | 'shadow' | 'enforce';

export interface HandoffLane {
  id: string;
  from: string;
  to: string;
  tools_allow?: string[];
  tools_deny?: string[];
  max_depth?: number;
  note?: string;
}

export interface HandoffLanesState {
  lanes: HandoffLane[];
  mode: HandoffLanesMode;
}

export interface HandoffLanesGraph {
  nodes: { id: string; kind: 'agent' | 'pseudo' }[];
  edges: HandoffLane[];
  mode: HandoffLanesMode;
  mermaid: string;
}

export interface HandoffDecision {
  allowed: boolean;
  lane_id: string | null;
  disabled_tools: string[];
  reason: string;
}

async function ok(r: Response, what: string): Promise<Response> {
  if (r.ok) return r;
  let msg = `${what}: HTTP ${r.status}`;
  try {
    const d = (await r.json()) as { detail?: string };
    if (d.detail) msg = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail);
  } catch {
    /* not json */
  }
  throw new Error(msg);
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

export const getHandoffLanes = () => getJson<HandoffLanesState>('/api/handoff-lanes');

export async function saveHandoffLanes(lanes: HandoffLane[], mode: HandoffLanesMode): Promise<HandoffLanesState> {
  const r = await ok(
    await fetch('/api/handoff-lanes', {
      method: 'PUT',
      credentials: 'same-origin',
      headers: JSON_HEADERS,
      body: JSON.stringify({ lanes, mode }),
    }),
    'handoff-lanes',
  );
  return (await r.json()) as HandoffLanesState;
}

export const getHandoffLanesGraph = () => getJson<HandoffLanesGraph>('/api/handoff-lanes/graph');

export async function testHandoffLane(from: string, to: string, tools: string[], depth = 1): Promise<HandoffDecision> {
  const r = await ok(
    await fetch('/api/handoff-lanes/test', {
      method: 'POST',
      credentials: 'same-origin',
      headers: JSON_HEADERS,
      body: JSON.stringify({ from, to, tools, depth }),
    }),
    'handoff-lanes/test',
  );
  return (await r.json()) as HandoffDecision;
}
