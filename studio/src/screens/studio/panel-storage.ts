import {initialPanel, type PanelState} from './panel';

const prefix = 'faustus.panel.';
type StoragePort = Pick<Storage, 'getItem' | 'setItem'>;

export function readPanel(storage: StoragePort, key: string): PanelState {
  if (key.startsWith('private:')) return {...initialPanel};
  try {
    const raw = JSON.parse(storage.getItem(prefix + key) || 'null');
    if (raw && Array.isArray(raw.documents) && Array.isArray(raw.files) && raw.drafts && typeof raw.drafts === 'object') {
      return {...initialPanel, ...raw, live: false, frames: [], active: -1, streamDoc: null,
        doc: raw.doc ? {...raw.doc, streaming: false} : null};
    }
  } catch { /* Storage may be unavailable; the editor still works in memory. */ }
  return {...initialPanel};
}

/** Persist changed conversations, including callbacks arriving after navigation.
 * Never write incognito content or transient browser/stream frames to storage. */
export function persistPanels(storage: StoragePort, states: Record<string, PanelState>, written: Map<string, PanelState>): void {
  for (const [key, state] of Object.entries(states)) {
    if (key.startsWith('private:') || written.get(key) === state) continue;
    try {
      storage.setItem(prefix + key, JSON.stringify({...state, frames: [], active: -1, live: false, streamDoc: null,
        doc: state.doc ? {...state.doc, streaming: false} : null,
        documents: state.documents.map(doc => ({...doc, streaming: false}))}));
      written.set(key, state);
    } catch { /* Keep drafts in memory and retry on the next state change. */ }
  }
}
