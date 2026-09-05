import type { BrowserFrame, ChatEvent, DocSuggestion } from '../../adapters/chat';
import { t } from '../../i18n';

/**
 * The side panel next to the transcript: what the agent sees (browser and
 * desktop frames), the document it is writing, and a file from the
 * workspace. State and reducer only; SidePanel.tsx paints it.
 */

export type PanelTab = 'browser' | 'doc' | 'file';

export interface DocState {
  /** Set while the agent streams the document; the id arrives at the end. */
  streaming: boolean;
  id: string | null;
  title: string;
  language: string;
  content: string;
  version: number;
  suggestions: DocSuggestion[];
}

export interface PanelState {
  open: boolean;
  tab: PanelTab;
  frames: BrowserFrame[];
  active: number;
  /** A browser action happened in the turn streaming now. */
  live: boolean;
  doc: DocState | null;
  file: { workspace: string; path: string } | null;
}

export const MAX_FRAMES = 8;
const AUTO_KEY = 'odysseus.browserView.auto'; // shared with the legacy panel

export function autoOpenEnabled(): boolean {
  try {
    const v = localStorage.getItem(AUTO_KEY);
    return v === null ? true : v !== '0';
  } catch {
    return true;
  }
}

export function setAutoOpen(on: boolean): void {
  try {
    localStorage.setItem(AUTO_KEY, on ? '1' : '0');
  } catch {
    /* private mode */
  }
}

export const initialPanel: PanelState = {
  open: false,
  tab: 'browser',
  frames: [],
  active: -1,
  live: false,
  doc: null,
  file: null,
};

export type PanelAction =
  | { type: 'event'; event: ChatEvent; busy: boolean }
  | { type: 'open'; tab?: PanelTab }
  | { type: 'close' }
  | { type: 'tab'; tab: PanelTab }
  | { type: 'show'; index: number }
  | { type: 'turn-start' }
  | { type: 'turn-end' }
  | { type: 'file'; workspace: string; path: string }
  | { type: 'doc'; doc: DocState | null }
  | { type: 'suggestions'; suggestions: DocSuggestion[] }
  | { type: 'session-switch' };

export function panelReducer(state: PanelState, action: PanelAction): PanelState {
  switch (action.type) {
    case 'open':
      return { ...state, open: true, tab: action.tab ?? state.tab };
    case 'close':
      return { ...state, open: false };
    case 'tab':
      return { ...state, tab: action.tab, open: true };
    case 'show':
      return action.index >= 0 && action.index < state.frames.length ? { ...state, active: action.index } : state;
    case 'turn-start':
      return { ...state, live: false };
    case 'turn-end':
      return { ...state, live: false, doc: state.doc?.streaming ? { ...state.doc, streaming: false } : state.doc };
    case 'session-switch':
      return { ...state, live: false, doc: null, file: null };
    case 'file':
      return { ...state, open: true, tab: 'file', file: { workspace: action.workspace, path: action.path } };
    case 'doc':
      return { ...state, open: action.doc ? true : state.open, tab: action.doc ? 'doc' : state.tab, doc: action.doc };
    case 'suggestions':
      return state.doc ? { ...state, doc: { ...state.doc, suggestions: action.suggestions } } : state;
    case 'event': {
      const ev = action.event;
      if (ev.type === 'frame' || (ev.type === 'tool_output' && ev.screenshot)) {
        const frame: BrowserFrame =
          ev.type === 'frame'
            ? ev.frame
            : { src: ev.screenshot as string, url: '', title: /^desktop_/.test(ev.tool) ? t('Desktop') : ev.tool, tool: ev.tool, source: /^desktop_/.test(ev.tool) ? 'desktop' : 'browser', at: Date.now() };
        const frames = [...state.frames, frame].slice(-MAX_FRAMES);
        const first = !state.live;
        return {
          ...state,
          frames,
          active: frames.length - 1,
          live: action.busy,
          open: state.open || (first && autoOpenEnabled()),
          tab: first && autoOpenEnabled() && !state.open ? 'browser' : state.tab,
        };
      }
      if (ev.type === 'doc_open') {
        return {
          ...state,
          open: true,
          tab: 'doc',
          doc: { streaming: true, id: null, title: ev.title || t('Document'), language: ev.language, content: '', version: 1, suggestions: [] },
        };
      }
      if (ev.type === 'doc_delta') {
        const doc = state.doc ?? { streaming: true, id: null, title: t('Document'), language: '', content: '', version: 1, suggestions: [] };
        return { ...state, doc: { ...doc, streaming: true, content: ev.content } };
      }
      if (ev.type === 'doc_update') {
        return {
          ...state,
          open: true,
          tab: 'doc',
          doc: {
            streaming: false,
            id: ev.doc.id,
            title: ev.doc.title || state.doc?.title || t('Document'),
            language: ev.doc.language || state.doc?.language || '',
            content: ev.doc.content,
            version: ev.doc.version,
            suggestions: state.doc?.id === ev.doc.id ? state.doc.suggestions : [],
          },
        };
      }
      if (ev.type === 'doc_suggestions') {
        if (!ev.suggestions.length) return state;
        const doc = state.doc && (!ev.docId || state.doc.id === ev.docId) ? state.doc : null;
        if (!doc) {
          // Suggestions for a document that is not open: open it by id, the
          // panel fetches it.
          return {
            ...state,
            open: true,
            tab: 'doc',
            doc: { streaming: false, id: ev.docId || null, title: '', language: '', content: '', version: 0, suggestions: ev.suggestions },
          };
        }
        const known = new Set(doc.suggestions.map((s) => s.id));
        return { ...state, open: true, tab: 'doc', doc: { ...doc, suggestions: [...doc.suggestions, ...ev.suggestions.filter((s) => !known.has(s.id))] } };
      }
      if (ev.type === 'tool_output' && ev.docId && ['create_document', 'update_document', 'edit_document'].includes(ev.tool)) {
        // The doc_update event normally follows; if it does not, the tool
        // result still names the document, and the panel fetches it.
        if (state.doc?.id === ev.docId) return state;
        return { ...state, open: true, tab: 'doc', doc: { streaming: false, id: ev.docId, title: '', language: '', content: '', version: 0, suggestions: [] } };
      }
      return state;
    }
  }
}
