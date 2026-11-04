/** Prospero's Hoard as a speech provider: the voice engines it reports. */

export interface ProsperoEngine {
  id: string;
  name: string;
  ready: boolean;
  cloning: boolean;
  reason: string;
}

export interface ProsperoEngines {
  reachable: boolean;
  engines: ProsperoEngine[];
  reason?: string;
}

export async function loadProsperoEngines(): Promise<ProsperoEngines> {
  const r = await fetch('/api/tts/prospero/engines', { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as ProsperoEngines;
}
