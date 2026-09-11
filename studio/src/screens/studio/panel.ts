import type { BrowserFrame, ChatEvent, DocSuggestion } from '../../adapters/chat';
import { pingGitRefresh } from '../../adapters/git';
import { pingBoardRefresh } from '../../adapters/board';
import { t } from '../../i18n';

/**
 * The side panel next to the transcript: what the agent sees (browser and
 * desktop frames), the document it is writing, a file from the workspace,
 * (Lote 86) the workspace's git repository, live, and (Lote 93) the
 * project's work board. State and reducer only; SidePanel.tsx paints it.
 */

export type PanelTab = 'outputs' | 'sources' | 'agents' | 'browser' | 'doc' | 'file' | 'git' | 'board';
export interface PanelDraft {text:string; base:string; revision?:string}

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
  documents: DocState[];
  files: {workspace:string;path:string}[];
  drafts: Record<string,PanelDraft>;
  width: number;
  streamDoc: DocState | null;
  /** Lote 93: the issue open in the Board tab's detail view, or `null` for
   *  the list (ready + in_progress). Set by the Board tab's own row clicks,
   *  and by an issue-id chip elsewhere in the chat (Transcript.tsx) via the
   *  `board-issue` action below, so both land on the same tab+state. */
  boardIssue: string | null;
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
  tab: 'outputs',
  frames: [],
  active: -1,
  live: false,
  doc: null,
  file: null,
  documents: [], files: [], drafts: {}, width: 520, streamDoc: null,
  boardIssue: null,
};

export type PanelAction =
  | {type:'draft';key:string;draft:PanelDraft|null}
  | {type:'draft-saved';key:string;submitted:PanelDraft;base:string;revision?:string}
  | {type:'width';width:number}
  | {type:'forget';key:string}
  | {type:'doc-saved';doc:DocState}
  | {type:'doc-renamed';id:string;title:string}
  | { type: 'event'; event: ChatEvent; busy: boolean }
  | { type: 'open'; tab?: PanelTab }
  | { type: 'close' }
  | { type: 'tab'; tab: PanelTab }
  | { type: 'show'; index: number }
  | { type: 'turn-start' }
  | { type: 'turn-end' }
  | { type: 'file'; workspace: string; path: string }
  | { type: 'doc'; doc: DocState | null }
  | { type: 'suggestions'; docId?:string|null; suggestions: DocSuggestion[] }
  | { type: 'session-switch' }
  /** Lote 93: open the Board tab, optionally straight to one issue's
   *  detail (an issue-id chip elsewhere in the chat) — `id: null` opens
   *  the list. */
  | { type: 'board-issue'; id: string | null };

function reducePanel(state: PanelState, action: PanelAction): PanelState {
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
      return { ...initialPanel };
    case 'board-issue':
      return { ...state, open: true, tab: 'board', boardIssue: action.id };
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
    default: return state;
  }
}

export const fileKey=(file:{workspace:string;path:string})=>'file:'+JSON.stringify([file.workspace,file.path]);
export const docKey=(doc:DocState)=>'doc:'+(doc.id || 'streaming');
export function panelReducer(state:PanelState,action:PanelAction):PanelState {
  // Lote 86: "the panel refreshes as the turn ends" (a `git_policy` event, or
  // the turn's own end) — a UI-only DOM ping, not part of the state shape,
  // so `SourceControlPanel` (fixed props: no room for a dedicated signal
  // prop) can refetch status live wherever it happens to be mounted.
  if(action.type==='turn-end'||(action.type==='event'&&action.event.type==='git_policy')) pingGitRefresh();
  // Lote 93: any turn may have used board_* tools — same "ping on turn-end"
  // rule as git, one dedicated event so a mounted board panel and a
  // mounted git panel each refresh only their own concern.
  if(action.type==='turn-end') pingBoardRefresh();
  if(action.type==='suggestions' && action.docId !== undefined) {
    const update=(doc:DocState)=>doc.id===action.docId?{...doc,suggestions:action.suggestions}:doc;
    return {...state,documents:state.documents.map(update),doc:state.doc?update(state.doc):null};
  }
  if(action.type==='doc-renamed') {
    const rename=(doc:DocState)=>doc.id===action.id?{...doc,title:action.title}:doc;
    return {...state,documents:state.documents.map(rename),doc:state.doc?rename(state.doc):null};
  }
  if(action.type==='draft-saved') {
    const current=state.drafts[action.key];
    if(!current||current.base!==action.submitted.base||current.revision!==action.submitted.revision)return state;
    const drafts={...state.drafts};
    if(current.text===action.submitted.text)delete drafts[action.key];
    else drafts[action.key]={...current,base:action.base,revision:action.revision};
    return {...state,drafts};
  }
  if(action.type==='doc-saved') return {...state,documents:[...state.documents.filter(d=>d.id!==action.doc.id),action.doc],doc:state.doc?.id===action.doc.id?action.doc:state.doc};
  if(action.type==='draft') {const drafts={...state.drafts};if(action.draft)drafts[action.key]=action.draft;else delete drafts[action.key];return {...state,drafts};}
  if(action.type==='width') return {...state,width:Math.max(320,Math.min(900,action.width))};
  if(action.type==='forget') {
    if(state.drafts[action.key]) return state;
    const documents=state.documents.filter(d=>docKey(d)!==action.key),files=state.files.filter(f=>fileKey(f)!==action.key);
    const closingDoc=Boolean(state.doc&&docKey(state.doc)===action.key),closingFile=Boolean(state.file&&fileKey(state.file)===action.key);
    return {...state,documents,files,doc:closingDoc?null:state.doc,file:closingFile?null:state.file,tab:(closingDoc&&state.tab==='doc')||(closingFile&&state.tab==='file')?'outputs':state.tab};
  }
  const streamingEvent=action.type==='event'&&action.event.type==='doc_delta';
  const next=reducePanel(streamingEvent?{...state,doc:state.streamDoc}:state,action);
  if(next===state)return state;
  if(action.type==='event'&&(action.event.type==='doc_open'||action.event.type==='doc_delta')) next.streamDoc=next.doc;
  if(action.type==='event'&&action.event.type==='doc_update') next.streamDoc=null;
  if(next.doc?.id) next.documents=[...next.documents.filter(d=>d.id!==next.doc?.id),next.doc];
  if(next.file) next.files=[...next.files.filter(f=>fileKey(f)!==fileKey(next.file!)),next.file];
  // Background output is recorded without navigating away from the person's
  // selected resource. Updates to that same document still show its latest base.
  if(action.type==='event'&&state.open&&state.tab!=='outputs') {
    next.tab=state.tab;
    if(state.doc?.id&&next.doc?.id!==state.doc.id) next.doc=state.doc;
  }
  return next;
}
