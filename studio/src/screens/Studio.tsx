import { FolderKanban, MessageSquare, PanelRight, X } from 'lucide-react';
import {withImageReferences} from '../lib/image-references';
import {MessageNavigator} from './studio/MessageNavigator';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { Button, Dialog, IconButton, Skeleton } from '../components';
import {
  createSession,
  listModels,
  listSessions,
  loadHistory,
  metricsFrom,
  pauseChat,
  pendingOutboxFor,
  resumeTurn,
  steerChat,
  streamFailureMessage,
  sendTurn,
  stopChat,
  type ChatSession,
  type Delegation,
  type DelegationTask,
  type ModelRoute,
} from '../adapters/chat';
import { listProjects, type Project } from '../adapters/projects';
import { createDoc } from '../adapters/documents';
import type { EvidenceRef } from '../adapters/evidence';
import {
  attachmentsFromMetadata,
  getRagActive,
  getWorkspace,
  pickNative,
  rememberRule,
  setRagActive,
  setWorkspace as persistWorkspace,
  uploadFiles,
  vetWorkspace,
  type Attachment,
  type GenOverrides,
} from '../adapters/composer';
import {
  archiveSession,
  autoSortSessions,
  compactSession,
  deleteMessages,
  deleteSession,
  editMessage,
  downloadExport,
  downloadExportZip,
  forkSession,
  listVersions,
  renameSession,
  restoreVersion,
  setSessionImportant,
  truncateSession,
  type ExportFormat,
  EXPORT_FORMATS,
} from '../adapters/sessions';
import { relativeTime } from '../adapters/home';
import { getKeybinds, KEYBIND_LABELS, matchesCombo } from '../adapters/settings';
import { listCheckpoints } from '../adapters/workspace';
import { addMemory, deleteMemory, listMemories } from '../adapters/memory';
import { createNote } from '../adapters/notes';
import { startTour } from '../shell/store';
import { refreshActivity } from '../shell/activity';
import { TOURS, resetTours, seenTours } from '../lib/tours';
import { getTheme as getMode, setTheme as setMode, type ThemeChoice } from '../shell/theme';
import { BrandMark } from '../shell/BrandMark';
import { useSpotlight } from '../shell/useSpotlight';
import { ModelPicker } from './ModelPicker';
import { PresetPicker } from './PresetPicker';
import { delegationLabel, genFromArgs, helpMarkdown, parseCommand, parseDelegation, resolveCommand } from './studio/commands';
import * as cmd from '../adapters/commands';
import { egg as makeEgg, type Egg as EggData, type EggKind } from '../lib/fun';
import { Egg } from './studio/Egg';
import { Rich } from './rich';
import { knownGroupParents, stripGroupPrefix } from '../adapters/group';
import { Composer, type Knobs } from './studio/Composer';
import { apply, beginApproval, blankTurn, cleanUserText, closeApproval, restoreFromMetadata, type Turn } from './studio/model';
import { useChatPanel } from './studio/useChatPanel';
import ChatTeam from './studio/ChatTeam';
import { SessionsPane } from './studio/SessionsPane';
import { Transcript, type Decision } from './studio/Transcript';
import { VramAdmissionDialog } from './VramAdmissionDialog';
import { Vitals } from './studio/Vitals';
import './projects.css';
import './home.css';
import './studio.css';
import { t, tn } from '../i18n';
import { useDisplay } from '../shell/display';

/* Rare, and the eager bundle has a budget: the folder picker and the side
   panel (browser frames, document editor, file viewer) arrive when opened. */
const WorkspaceDialog = lazy(() => import('./studio/WorkspaceDialog'));
const SidePanel = lazy(() => import('./studio/SidePanel'));
// Lote 50 (BENCH-03 wiring): only fetched once a tool card's "ver evidencia"
// button is actually clicked — same rare-on-demand posture as the two above.
const EvidenceInspector = lazy(() => import('./Evidence'));
import {selectionImage,selectionPrompt} from './studio/FrameSelection';
import ProjectVisualReferences from './studio/ProjectVisualReferences';
import StyleLab from './studio/StyleLab';
import LocalVideo from './studio/LocalVideo';
const VoicePanel = lazy(() => import('../voice/VoicePanel'));

/* The speech adapter (TTS/STT with browser fallbacks) loads on first use. */
const speak = (text: string) => import('../adapters/speech').then((m) => m.speak(text));
const stopSpeaking = () => void import('../adapters/speech').then((m) => m.stopSpeaking());

const ROUTE_KEY = 'faustus_studio_route';
const KNOBS_KEY = 'faustus_studio_knobs';
const GEN_KEY = 'faustus_studio_gen';
const PRESET_KEY = 'faustus_studio_preset';
const PANE_KEY = 'faustus_studio_pane';
/* Sessions opened in Nobody mode, deleted when the mode ends or the page
   comes back. NOT the previous interface's key (`ody-incognito-sessions`):
   its sessions.js still runs underneath the pilot and deletes whatever is
   in that list except the session IT has on screen — which is never the
   Studio one. Sharing the key cost a test conversation. */
const INCOGNITO_KEY = 'faustus_studio_incognito';

function incognitoIds(): string[] {
  try {
    const raw = JSON.parse(sessionStorage.getItem(INCOGNITO_KEY) || '[]') as unknown;
    return Array.isArray(raw) ? raw.map(String) : [];
  } catch {
    return [];
  }
}

function rememberIncognito(id: string): void {
  try {
    const ids = incognitoIds();
    if (!ids.includes(id)) sessionStorage.setItem(INCOGNITO_KEY, JSON.stringify([...ids, id]));
  } catch {
    /* private mode */
  }
}

/** Deletes every Nobody-mode session except `keep` (which stays listed
 *  only if it already was one — this never turns a normal session into a
 *  Nobody one). */
function cleanupIncognito(keep: string | null): void {
  const all = incognitoIds();
  const doomed = all.filter((id) => id !== keep);
  try {
    sessionStorage.setItem(INCOGNITO_KEY, JSON.stringify(all.filter((id) => id === keep)));
  } catch {
    /* private mode */
  }
  for (const id of doomed) void deleteSession(id).catch(() => undefined);
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...(JSON.parse(raw) as T) } : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode: the choice simply does not persist */
  }
}

/* The unsent text of each conversation, so it survives a visit elsewhere.
   One slot per session id; a chat that has no id yet uses `new`. Empty text
   removes the slot: the store never grows with blank drafts. */
const DRAFT_PREFIX = 'faustus_studio_draft:';

function draftKeyFor(sessionId: string | null): string {
  return `${DRAFT_PREFIX}${sessionId ?? 'new'}`;
}

function readDraftFor(sessionId: string | null): string {
  try {
    return localStorage.getItem(draftKeyFor(sessionId)) ?? '';
  } catch {
    return '';
  }
}

function writeDraftFor(sessionId: string | null, text: string) {
  try {
    if (text.trim()) localStorage.setItem(draftKeyFor(sessionId), text);
    else localStorage.removeItem(draftKeyFor(sessionId));
  } catch {
    /* private mode: the draft lives only as long as the screen */
  }
}

/* QA-35 (UX-01/MEDIA-03): the composer's already-uploaded-but-unsent
   attachments, same "one slot per session, empty removes the slot" shape as
   the draft text above — a reload used to bring the text back but drop the
   pictures/files you had already attached to it, which read as data loss
   (docs/spec/v2/MAPA_REUTILIZACION.md's UX-01 gap). Only the small
   {id, name, mime, size, ...} record is kept: the bytes already live on the
   server under `id` (uploadFiles already POSTed them), so this is exactly as
   cheap as the text draft, not a second copy of the file. */
const ATTACHMENTS_PREFIX = 'faustus_studio_draft_attachments:';

function attachmentsKeyFor(sessionId: string | null): string {
  return `${ATTACHMENTS_PREFIX}${sessionId ?? 'new'}`;
}

function readAttachmentsFor(sessionId: string | null): Attachment[] {
  try {
    const raw = localStorage.getItem(attachmentsKeyFor(sessionId));
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(parsed) ? parsed.filter((a): a is Attachment => Boolean(a && typeof a === 'object' && typeof (a as Attachment).id === 'string')) : [];
  } catch {
    return [];
  }
}

/** `incognito` must be passed explicitly (not read from anywhere) so a
 *  caller can never forget it — Nobody mode's whole point is that nothing
 *  about the conversation, attachments included, outlives the tab. */
function writeAttachmentsFor(sessionId: string | null, attachments: Attachment[], incognito: boolean) {
  try {
    if (incognito) return;
    if (attachments.length) localStorage.setItem(attachmentsKeyFor(sessionId), JSON.stringify(attachments));
    else localStorage.removeItem(attachmentsKeyFor(sessionId));
  } catch {
    /* private mode: attachments live only as long as the screen */
  }
}

const SUGGESTIONS = [
  'Explain this repository to me as if I had just joined the team',
  'Search the web for what changed this week on the topic I give you',
  'Write a script that sorts my downloads by type',
  'Review the last commit and tell me what could break',
];

/** When this screen was opened: `/uptime` counts from here. */
const OPENED_AT = Date.now();

/** The names `/toggle` and `/toggle` on its own put in the table. */
const SWITCH_LABEL: Record<string, string> = {
  web: 'Web search',
  bash: 'Terminal',
  plan: 'Proposal mode',
  rag: 'Your documents',
  research: 'Deep Research',
  incognito: 'Nobody mode',
};

interface Notice {
  text: string;
  tone: 'info' | 'warning' | 'danger';
  /** A command's answer: Markdown, drawn by the transcript's reader. */
  rich?: boolean;
  /** A hidden command's answer, drawn by `studio/Egg.tsx`. */
  egg?: EggData;
}

/**
 * Studio (UI-030/031/032).
 *
 * Where the work happens. One transcript, one composer, and the execution
 * rail threaded through every agent turn so the tools the model ran are
 * visible in the reply instead of buried behind a "show details" toggle.
 * This file wires the pieces; the pieces live in ./studio/.
 */
export function StudioScreen() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const sessionId = params.get('s');

  const [sessions, setSessions] = useState<ChatSession[] | null>(null);
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [routeId, setRouteId] = useState<string | null>(() => readJson<{ id: string | null }>(ROUTE_KEY, { id: null }).id);
  const [knobs, setKnobsState] = useState<Knobs>(() => ({
    ...readJson<Knobs>(KNOBS_KEY, { mode: 'agent', web: false, bash: true, plan: false, rag: false, incognito: false, research: false }),
    rag: getRagActive(),
    // Research is one turn's worth of work, never a standing mode.
    research: false,
    // Nobody mode never survives a reload: a fresh page is a normal page.
    incognito: false,
  }));
  const [preset, setPreset] = useState<{ id: string; name: string } | null>(() => {
    const p = readJson<{ id?: string; name?: string }>(PRESET_KEY, {});
    return p.id && p.name ? { id: p.id, name: p.name } : null;
  });
  const [refreshingModels, setRefreshingModels] = useState(false);
  const [presetSignal, setPresetSignal] = useState(0);
  /* Commands that need handlers declared further down (fork, tts): the
     handlers are stored here after they exist, and read at call time. */
  const extrasRef = useRef<{ fork: () => void; tts: () => void }>({ fork: () => undefined, tts: () => undefined });
  const [turns, setTurns] = useState<Turn[] | null>(sessionId ? null : []);
  const [title, setTitle] = useState('');
  // The draft outlives the screen. Leaving to check something in another chat
  // or a project and coming back used to hand you an empty box: the route
  // body remounts per path, and one `useState('')` was all there was. Now the
  // text is kept per conversation (a fresh chat has its own slot) and comes
  // back with it; a sent message clears its slot.
  const [draft, setDraft] = useState(() => readDraftFor(sessionId));
  const draftSession = useRef<string | null>(sessionId);
  useEffect(() => {
    if (draftSession.current === sessionId) return;
    draftSession.current = sessionId;
    setDraft(readDraftFor(sessionId));
  }, [sessionId]);
  useEffect(() => {
    // Only once the draft on screen belongs to this session — the swap above
    // runs in this same commit, and writing the previous chat's text under
    // the new chat's key would be exactly the mix-up this avoids.
    if (draftSession.current !== sessionId) return;
    writeDraftFor(sessionId, draft);
  }, [draft, sessionId]);
  const [attachments, setAttachments] = useState<Attachment[]>(() => readAttachmentsFor(sessionId));
  useEffect(() => {
    // Same session-boundary guard as the draft-text effect above: only write
    // once the attachments on screen actually belong to this session (the
    // restore in the history-load effect runs in the same commit as the
    // sessionId change), so a fast switch never writes chat A's pending
    // files under chat B's key.
    if (draftSession.current !== sessionId) return;
    writeAttachmentsFor(sessionId, attachments, knobs.incognito);
  }, [attachments, sessionId, knobs.incognito]);
  const sendingMessage = useRef(false);
  const [preparingMessage, setPreparingMessage] = useState(false);
  const [busy, setBusy] = useState(false);
  const display = useDisplay();
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  // The VRAM ticket already answered in this screen: its dialog must not
  // come back while the server is still unloading and the turn's event
  // keeps saying `vram_blocked`.
  const [vramAnswered, setVramAnswered] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Wide layouts hide the conversations column instead of sliding a drawer
  // (the previous shell's Ctrl+Alt+B); remembered across visits.
  const [paneHidden, setPaneHidden] = useState(() => readJson<{ hidden?: boolean }>(PANE_KEY, {}).hidden === true);
  const [filter, setFilter] = useState('');
  const [workspace, setWorkspaceState] = useState(() => getWorkspace());
  const [wsOpen, setWsOpen] = useState(false);
  const [gen, setGen] = useState<GenOverrides>({});
  const [modelSignal, setModelSignal] = useState(0);
  const [panel, panelDispatch] = useChatPanel(sessionId,knobs.incognito);
  const [teamEnabled,setTeamEnabled] = useState(false);
  // Lote 50 (BENCH-03 wiring): the evidence inspector opens as its own
  // lightweight dialog, independent of `panel`'s file/doc tab state machine
  // — a tool card's "ver evidencia" is a quick, read-only lookup, not
  // another side-panel surface competing for the same tab.
  const [evidenceRef, setEvidenceRef] = useState<EvidenceRef | null>(null);

  const controllerRef = useRef<AbortController | null>(null);
  const runEpoch = useRef(0);
  /* The run's opaque id, from the header the server answers with. Stop is
     fail-closed: without it, `POST /api/chat/stop` refuses to cancel. */
  const runIdRef = useRef<string | null>(null);
  /* Rejoining a live run is declared further down (it needs patchLast and
     the history loader); the effect that opens a session calls it through
     this ref so the dependency never has to travel up the file. */
  const rejoinRef = useRef<(sid: string) => void>(() => undefined);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const pinnedRef = useRef(true);
  /* Two handlers a slash command needs that are declared further down: the
     ref keeps the dependency out of runCommand's closure. */
  const laterRef = useRef<{ openSession: (id: string | null) => void; toggleSidebar: () => void }>({
    openSession: () => undefined,
    toggleSidebar: () => undefined,
  });
  const freshRef = useRef<string | null>(null);
  const spotlight = useSpotlight();

  const route = useMemo(() => routes.find((r) => r.id === routeId) ?? routes[0] ?? null, [routes, routeId]);
  const current = useMemo(() => sessions?.find((s) => s.id === sessionId) ?? null, [sessions, sessionId]);
  const visibleSession = useRef(sessionId);
  visibleSession.current = sessionId;
  const [voiceSession, setVoiceSession] = useState<string | null>(null);
  useEffect(() => { setVoiceSession(id => id === sessionId ? id : null); }, [sessionId]);

  const say = useCallback((text: string, tone: Notice['tone'] = 'info') => {
    setNotice({ text, tone });
  }, []);

  /** A command's answer, as Markdown. */
  const report = useCallback((markdown: string, tone: Notice['tone'] = 'info') => {
    setNotice({ text: markdown, tone, rich: true });
  }, []);

  /** Runs an adapter call and reports whichever of the two it produces. */
  const reportFrom = useCallback(
    async (work: () => Promise<string>) => {
      try {
        report(await work());
      } catch (error) {
        say((error as Error).message, 'danger');
      }
    },
    [report, say],
  );

  const setKnobs = useCallback(
    (update: (k: Knobs) => Knobs) => {
      const next = update(knobs);
      if (next.rag !== knobs.rag) setRagActive(next.rag);
      if (knobs.incognito && !next.incognito && sessionId && incognitoIds().includes(sessionId)) {
        // Leaving Nobody mode on a Nobody session: that session goes away
        // with the mode, so the screen moves to a fresh conversation.
        navigate('/studio');
      }
      setKnobsState(next);
    },
    [knobs, sessionId, navigate],
  );

  const setWorkspace = useCallback((path: string) => {
    persistWorkspace(path);
    setWorkspaceState(path);
  }, []);

  /* The project this conversation belongs to (by folder), as projects.js
     resolved it on every session switch: its working folder becomes the
     workspace, and a chip in the header says where you are. A folder chosen
     by hand survives; only the one a project applied gets cleared. */
  const [projects, setProjects] = useState<Project[]>([]);
  useEffect(() => {
    listProjects().then(setProjects).catch(() => setProjects([]));
  }, [sessionId]);
  const project = useMemo(() => (current?.folder ? projects.find((p) => p.folder === current.folder) ?? null : null), [projects, current]);
  useEffect(() => {
    const AUTO = 'odysseus-project-workspace';
    let applied = '';
    try {
      applied = localStorage.getItem(AUTO) ?? '';
    } catch {
      /* private mode */
    }
    if (project?.workspace) {
      if (getWorkspace() !== project.workspace) setWorkspace(project.workspace);
      try {
        localStorage.setItem(AUTO, project.workspace);
      } catch {
        /* private mode */
      }
      return;
    }
    if (applied && getWorkspace() === applied) setWorkspace('');
    try {
      localStorage.removeItem(AUTO);
    } catch {
      /* private mode */
    }
  }, [project, setWorkspace]);

  /* The folder chip opens the OS's own dialog (Explorer on Windows) when the
   * browser runs on the server's machine; the in-page browser is only the
   * fallback for remote browsers or hosts without a display. */
  const [picking, setPicking] = useState(false);
  const pickWorkspace = useCallback(async () => {
    if (picking) return;
    setPicking(true);
    try {
      const res = await pickNative('folder', workspace);
      if (res.status === 'ok' && res.path) {
        setWorkspace(res.path);
        say(`Carpeta: ${res.path}`);
      } else if (res.status === 'unavailable') {
        setWsOpen(true);
      } else if (res.detail) {
        say(res.detail, 'warning');
      }
    } catch (err) {
      say(err instanceof Error ? err.message : String(err), 'danger');
    } finally {
      setPicking(false);
    }
  }, [picking, workspace, setWorkspace, say]);

  const refreshSessions = useCallback(() => {
    listSessions().then(setSessions).catch(() => undefined);
  }, []);

  /* Sessions and models: once. */
  useEffect(() => {
    const controller = new AbortController();
    listSessions(controller.signal).then(setSessions).catch(() => setSessions([]));
    listModels(controller.signal).then(setRoutes).catch(() => setRoutes([]));
    return () => controller.abort();
  }, []);

  /* Per-session generation knobs. */
  useEffect(() => {
    setGen(sessionId ? readJson<GenOverrides>(`${GEN_KEY}_${sessionId}`, {}) : {});
  }, [sessionId]);
  useEffect(() => {
    if (sessionId) writeJson(`${GEN_KEY}_${sessionId}`, gen);
  }, [gen, sessionId]);

  const turnsFromHistory = useCallback(
    async (sid: string, signal?: AbortSignal) => {
      const result = await loadHistory(sid, signal);
      // A tool approval is stored as its own short assistant message ("Allow
      // this task to continue?") right before the real answer. Reading it
      // back as a bubble of its own is noise; the answer that follows is the
      // turn.
      const kept = result.history
        .map((m, historyIndex) => ({ m, historyIndex }))
        .filter(({ m }, i, all) => {
          if (m.role !== 'assistant') return true;
          const next = all[i + 1]?.m;
          const text = m.content.trim();
          // …but a message that carries the tool rail is not noise: dropping
          // it threw away every step of the turn that led to the gate, and
          // left the answer looking as if it had come from nowhere. What is
          // noise is the bare question, and that one the turn now renders as
          // an answered permission rather than as prose.
          const hasSteps = Array.isArray((m.metadata as { tool_events?: unknown }).tool_events)
            && ((m.metadata as { tool_events?: unknown[] }).tool_events?.length ?? 0) > 0;
          return !(next && next.role === 'assistant' && text.length < 160 && text.endsWith('?') && !hasSteps);
        });
      const mapped = kept.map(({ m, historyIndex }) => {
        const atts = m.role === 'user' ? attachmentsFromMetadata(m.metadata) : [];
        const turn = blankTurn(m.role, m.role === 'user' ? cleanUserText(m.content, atts.length > 0) : m.content);
        turn.historyIndex = historyIndex;
        turn.streaming = false;
        turn.attachments = atts;
        turn.dbId = typeof m.metadata._db_id === 'string' ? m.metadata._db_id : undefined;
        turn.edited = Boolean(m.metadata.edited);
        if (m.role === 'assistant') {
          turn.metrics = metricsFrom(m.metadata);
          return restoreFromMetadata(turn, m.metadata);
        }
        return turn;
      });
      return { ...result, turns: mapped };
    },
    [],
  );

  /* History: whenever the session in the URL changes — except the session
     this screen just created for a first message, whose stream is live. */
  useEffect(() => {
    if (sessionId && freshRef.current === sessionId) {
      freshRef.current = null;
      return;
    }
    controllerRef.current?.abort();
    controllerRef.current = null;
    setBusy(false);
    setLoadError(null);
    setNotice(null);
    // QA-35: restore this session's pending attachments (mirrors the draft
    // text restore below) instead of unconditionally blanking them — a
    // switch away and back must not silently drop what was already attached.
    setAttachments(readAttachmentsFor(sessionId));
    if (!sessionId) {
      setTurns([]);
      setTitle('');
      return;
    }
    setTurns(null);
    const controller = new AbortController();
    turnsFromHistory(sessionId, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        setTitle(result.name);
        setTurns(result.turns);
        pinnedRef.current = true;
        if (result.model) {
          const match = (r: ModelRoute) => r.model === result.model;
          setRouteId((id) => routes.find(match)?.id ?? id);
        }
        // History is what was SAVED. If the turn is still going, the server
        // still has it: rejoin so the conversation carries on in front of
        // you instead of looking frozen at its last saved line.
        if (!controller.signal.aborted) rejoinRef.current(sessionId);
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoadError(t('Could not open this conversation.'));
      });
    return () => {
      controller.abort();
      // Leaving a conversation drops OUR view of the turn, never the turn:
      // the run is detached and lives on (the lists keep saying so).
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
    // routes is read once at load on purpose: the picker must not jump later.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  /* The gallery sends a picture to a conversation: ?image=<url>&name=. */
  useEffect(() => {
    const imageParam = params.get('image');
    if (!imageParam) return;
    const name = params.get('name') || 'image.png';
    const next = new URLSearchParams(params);
    next.delete('image');
    next.delete('name');
    setParams(next, { replace: true });
    void (async () => {
      try {
        const {galleryAttachment} = await import('../lib/gallery-attachment');
        const file = await galleryAttachment(imageParam, name, window.location.origin);
        if (visibleSession.current !== sessionId) return;
        const uploaded = await uploadFiles([file], sessionId);
        if (visibleSession.current !== sessionId) return;
        setAttachments((list) => [...list, ...uploaded]);
        say(t('Image attached'));
      } catch (err) {
        if (visibleSession.current !== sessionId) return;
        say(err instanceof Error ? err.message : String(err), 'danger');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  /* A project's agent activity links to the exact answer: ?m=<message id>. */
  useEffect(() => {
    const m = params.get('m');
    if (!m || !turns || turns.length === 0) return;
    const next = new URLSearchParams(params);
    next.delete('m');
    setParams(next, { replace: true });
    requestAnimationFrame(() => {
      const el = document.querySelector<HTMLElement>(`.fs-turn[data-db-id="${CSS.escape(m)}"]`);
      if (!el) return;
      el.scrollIntoView({ block: 'center' });
      el.setAttribute('data-flash', '');
      window.setTimeout(() => el.removeAttribute('data-flash'), 2400);
    });
  }, [params, setParams, turns]);

  /* The palette's "Buscar conversaciones" lands on the filter. */
  useEffect(() => {
    if (!params.has('buscar')) return;
    const next = new URLSearchParams(params);
    next.delete('buscar');
    setParams(next, { replace: true });
    setDrawerOpen(true);
    requestAnimationFrame(() => searchRef.current?.focus());
  }, [params, setParams]);

  /* The Library opens a document in the side panel: ?doc=<id>. */
  useEffect(() => {
    const docParam = params.get('doc');
    if (!docParam) return;
    panelDispatch({ type: 'doc', doc: { streaming: false, id: docParam, title: '', language: '', content: '', version: 0, suggestions: [] } });
    const next = new URLSearchParams(params);
    next.delete('doc');
    setParams(next, { replace: true });
  }, [params, setParams]);

  /* Inicio's quick starts arrive with the sentence begun: ?draft=…
     Notas adds &mode=agent&send=1&note=<id>: run it now, in agent mode, and
     link the conversation back to the note ("Resolver con el agente"). */
  const autoSendRef = useRef<{ text: string; noteId: string | null; mode: 'agent' | 'chat' | null } | null>(null);
  useEffect(() => {
    const draftParam = params.get('draft');
    if (draftParam === null) return;
    setDraft(draftParam);
    const mode = params.get('mode');
    if (mode === 'agent' || mode === 'chat') setKnobsState((k) => ({ ...k, mode }));
    if (params.get('send') === '1') autoSendRef.current = { text: draftParam, noteId: params.get('note'), mode: mode === 'agent' || mode === 'chat' ? mode : null };
    const next = new URLSearchParams(params);
    next.delete('draft');
    next.delete('mode');
    next.delete('send');
    next.delete('note');
    setParams(next, { replace: true });
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    });
  }, [params, setParams]);

  useEffect(() => writeJson(KNOBS_KEY, { ...knobs, incognito: false }), [knobs]);
  useEffect(() => {
    if (routeId) writeJson(ROUTE_KEY, { id: routeId });
  }, [routeId]);
  useEffect(() => writeJson(PRESET_KEY, preset ?? {}), [preset]);

  /* Nobody mode: its sessions live only while the mode is on. Leaving the
     mode, changing session, or coming back to the page deletes them — the
     current one survives while it is on screen in the mode. */
  useEffect(() => {
    cleanupIncognito(knobs.incognito ? sessionId : null);
  }, [knobs.incognito, sessionId]);

  /* Follow the stream unless the reader scrolled up to look at something. */
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [turns]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  const patchLast = useCallback((fn: (turn: Turn) => Turn) => {
    setTurns((list) => {
      if (!list || list.length === 0) return list;
      const last = list[list.length - 1];
      if (last.role !== 'assistant') return list;
      return [...list.slice(0, -1), fn(last)];
    });
  }, []);

  /** After a turn lands, borrow the server's ids so edit/delete can work. */
  const syncIds = useCallback(
    async (sid: string) => {
      const epoch = runEpoch.current;
      try {
        const result = await turnsFromHistory(sid);
        if (visibleSession.current !== sid || runEpoch.current !== epoch) return;
        setTurns((list) => {
          if (!list) return list;
          const out = list.slice();
          // Match from the end: our last turns are the server's last turns.
          for (let i = 0, j = 1; i < out.length && j <= result.turns.length; i++, j++) {
            const mine = out[out.length - j];
            const theirs = result.turns[result.turns.length - j];
            if (!mine || !theirs || mine.role !== theirs.role) break;
            out[out.length - j] = {
              ...mine,
              dbId: theirs.dbId,
              historyIndex: theirs.historyIndex,
              edited: theirs.edited,
              attachments: mine.attachments.length ? mine.attachments : theirs.attachments,
            };
          }
          return out;
        });
      } catch {
        /* ids stay unknown; the actions simply hide */
      }
    },
    [turnsFromHistory],
  );

  const run = useCallback(
    async (
      sid: string,
      message: string,
      options: {
        approval?: { id: string; decision: Decision };
        attachments?: Attachment[];
        delegation?: Delegation;
        /** CALL-07/TASK-04: answering a specific ask_user question — paired
         *  with `optionIds` (the picked `AskOption.id`s, absent for free text). */
        questionId?: string;
        optionIds?: string[];
      } = {},
    ) => {
      const controller = new AbortController();
      runEpoch.current++;
      controllerRef.current = controller;
      setBusy(true);
      pinnedRef.current = true;
      panelDispatch({ type: 'turn-start' });
      if (options.approval) {
        // Keep the sealed question until the server accepts the control.
        // A failed request must leave an actionable card, not a silent stall.
        patchLast((t) => beginApproval(t));
      } else {
        const user = blankTurn('user', message);
        user.attachments = options.attachments ?? [];
        setTurns((list) => [...(list ?? []), user, blankTurn('assistant')]);
      }
      const research = knobs.research && !options.approval && !options.delegation;
      // Like the previous interface: the toggle is spent by the turn it starts.
      if (research) setKnobsState((k) => ({ ...k, research: false }));
      try {
        for await (const event of sendTurn({
          sessionId: sid,
          message,
          useResearch: research,
          // A delegation only makes sense with tools: it forces agent mode.
          mode: options.delegation || teamEnabled ? 'agent' : knobs.mode,
          planMode: knobs.mode === 'agent' && knobs.plan && !research,
          allowBash: (knobs.mode === 'agent' || Boolean(options.delegation) || teamEnabled) && knobs.bash,
          allowWebSearch: knobs.web,
          useRag: knobs.rag,
          noMemory: Boolean(knobs.noMemory),
          noSkills: Boolean(knobs.noSkills),
          inputTokenBudget: knobs.inputTokenBudget,
          workspace: knobs.mode === 'agent' || options.delegation || teamEnabled ? workspace || undefined : undefined,
          route,
          attachments: options.attachments?.map((a) => a.id),
          genOverrides: Object.keys(gen).length ? (gen as Record<string, number | boolean>) : undefined,
          approval: options.approval,
          questionId: options.questionId,
          optionIds: options.optionIds,
          delegateTasks: options.delegation,
          incognito: knobs.incognito,
          autonomyPreset: knobs.autonomyPreset,
          presetId: preset?.id,
          activeDocId: panel.doc && !panel.doc.streaming ? panel.doc.id ?? undefined : undefined,
          onRunId: (id) => {
            if (controller.signal.aborted || controllerRef.current !== controller) return;
            runIdRef.current = id;
            if (options.approval) {
              patchLast((t) => closeApproval(t, options.approval!.decision));
            } else {
              setTurns((list) => list?.map((t) => closeApproval(t, 'superseded')) ?? list);
            }
            // The lists say "this one is working" from the first moment,
            // not after the next poll.
            refreshActivity();
          },
          signal: controller.signal,
        })) {
          if (controller.signal.aborted || controllerRef.current !== controller) break;
          patchLast((t) => apply(t, event));
          panelDispatch({ type: 'event', event, busy: true });
        }
      } catch (error) {
        if (!controller.signal.aborted && controllerRef.current === controller) {
          patchLast((t) => apply(t, { type: 'error', message: streamFailureMessage(error) }));
          patchLast((t) => apply(t, { type: 'done' }));
        }
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          runIdRef.current = null;
          setBusy(false);
          panelDispatch({ type: 'turn-end' });
          void syncIds(sid);
        }
        refreshSessions();
        refreshActivity();
      }
    },
    [knobs, workspace, route, gen, patchLast, refreshSessions, syncIds, preset, panel.doc,teamEnabled,panelDispatch],
  );

  /**
   * Rejoins a turn that is still running server-side.
   *
   * Leaving a conversation does not end its turn: the run is detached and
   * keeps going (`src/agent_runs.py`), and the server replays its whole
   * buffer to whoever subscribes next. Without this, coming back showed only
   * what had been written to the database — which mid-approval is the
   * question with no card under it, and reads as a hung chat.
   *
   * Yields nothing when there is no live run, which is the usual case.
   */
  /**
   * Retries a send this tab never learned the outcome of — a reload or a
   * dropped connection between the POST and its response, not a live run
   * (that's `rejoin`, below, and it always runs first).
   *
   * Reuses the outbox's id, never a new one: the server's own outbox
   * (src/chat_outbox.py) recognises it and answers from what it already has
   * — a reconnect to a run still going, or the saved result of one that
   * finished without this tab ever hearing about it — rather than opening a
   * second turn. Either way `turnsFromHistory` below is what actually
   * settles the bubble: the server's saved record is the truth, this
   * screen's reconstruction of it is only what shows while checking.
   */
  const reconcileOutbox = useCallback(
    async (sid: string) => {
      const pending = pendingOutboxFor(sid);
      if (!pending || !pending.text.trim()) return;
      const controller = new AbortController();
      controllerRef.current = controller;
      setBusy(true);
      pinnedRef.current = true;
      panelDispatch({ type: 'turn-start' });
      setNotice({ text: t("This message's outcome is uncertain — checking…"), tone: 'info' });
      const user = blankTurn('user', pending.text);
      setTurns((list) => [...(list ?? []), user, blankTurn('assistant')]);
      try {
        for await (const event of sendTurn({
          sessionId: sid,
          message: pending.text,
          mode: 'chat',
          clientMessageId: pending.id,
          attachments: pending.attachments,
          onRunId: (id) => {
            if (controller.signal.aborted || controllerRef.current !== controller) return;
            runIdRef.current = id;
          },
          signal: controller.signal,
        })) {
          if (controller.signal.aborted || controllerRef.current !== controller) break;
          patchLast((t) => apply(t, event));
          panelDispatch({ type: 'event', event, busy: true });
        }
      } catch (error) {
        if (!controller.signal.aborted && controllerRef.current === controller) {
          patchLast((t) => apply(t, { type: 'error', message: streamFailureMessage(error) }));
          patchLast((t) => apply(t, { type: 'done' }));
        }
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          runIdRef.current = null;
          setBusy(false);
          panelDispatch({ type: 'turn-end' });
        }
        if (!controller.signal.aborted) {
          refreshSessions();
          refreshActivity();
          turnsFromHistory(sid)
            .then((result) => {
              if (!controller.signal.aborted && visibleSession.current === sid && !controllerRef.current) setTurns(result.turns);
            })
            .catch(() => undefined);
        }
      }
    },
    [patchLast, refreshSessions, refreshActivity, turnsFromHistory, panelDispatch],
  );

  const rejoin = useCallback(
    async (sid: string) => {
      const controller = new AbortController();
      controllerRef.current = controller;
      let joined = false;
      try {
        for await (const event of resumeTurn(sid, {
          signal: controller.signal,
          onRunId: (id) => {
            if (controller.signal.aborted || controllerRef.current !== controller) return;
            runIdRef.current = id;
          },
        })) {
          if (controller.signal.aborted || controllerRef.current !== controller) break;
          if (!joined) {
            // Only once there IS something to show: an empty rejoin must not
            // leave a ghost bubble at the end of the conversation.
            joined = true;
            setBusy(true);
            pinnedRef.current = true;
            panelDispatch({ type: 'turn-start' });
            setTurns((list) => [...(list ?? []), blankTurn('assistant')]);
            setNotice({ text: t('This conversation was still working — picking it up live.'), tone: 'info' });
          }
          patchLast((t) => apply(t, event));
          panelDispatch({ type: 'event', event, busy: true });
        }
      } catch {
        /* aborted (the session changed) or the run ended mid-read */
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          runIdRef.current = null;
          setBusy(false);
        }
        if (joined && !controller.signal.aborted) {
          panelDispatch({ type: 'turn-end' });
          refreshSessions();
          refreshActivity();
          // The turn is saved now: re-read it so ids, harness and metadata
          // are the server's and not this screen's reconstruction.
          turnsFromHistory(sid)
            .then((result) => {
              if (!controller.signal.aborted && visibleSession.current === sid && !controllerRef.current) setTurns(result.turns);
            })
            .catch(() => undefined);
        } else if (!controller.signal.aborted) {
          // No live run for this session — the ordinary case. But an
          // unacknowledged send (this tab reloaded, or the connection
          // dropped, between the POST and its response) is still sitting in
          // the outbox, and THAT is worth retrying with its own id.
          void reconcileOutbox(sid);
        }
      }
    },
    [patchLast, refreshSessions, refreshActivity, turnsFromHistory, reconcileOutbox],
  );
  rejoinRef.current = (sid: string) => void rejoin(sid);

  const ensureSession = useCallback(
    async (name: string): Promise<string | null> => {
      if (sessionId) return sessionId;
      try {
        // Nobody mode: the session is named so the list hides it, and it is
        // remembered so it can be deleted when the mode ends.
        const sid = await createSession(knobs.incognito ? 'Incognito' : name.slice(0, 60), route);
        if (knobs.incognito) rememberIncognito(sid);
        // A slow create must not pull the user back from another conversation.
        if (visibleSession.current !== sessionId) return null;
        setTitle(knobs.incognito ? t('Incognito') : name.slice(0, 60));
        freshRef.current = sid;
        const next = new URLSearchParams(params);
        next.set('s', sid);
        setParams(next, { replace: true });
        return sid;
      } catch {
        if (visibleSession.current === sessionId) say(t('Could not create the conversation. Your draft is still here; try again.'), 'danger');
        return null;
      }
    },
    [sessionId, route, params, setParams, say, knobs.incognito],
  );

  /* ── Slash commands ── */
  const runCommand = useCallback(
    async (typed: string, rest: string): Promise<boolean> => {
      const resolved = resolveCommand(typed, rest);
      if (!resolved) {
        say(t('I do not know /{name}. Type /help to see the commands.', { name: typed }), 'warning');
        return true;
      }
      const { command, args } = resolved;
      const name = command.name;
      // Every `/setup <provider>` lands on the same handler; the sub only
      // exists so the suggestions can offer the providers by name.
      const path = name === 'setup' ? 'setup' : resolved.path;
      if (command.route) {
        if (name === 'research' && args) navigate(`/research?q=${encodeURIComponent(args)}`);
        else navigate(command.route);
        return true;
      }
      switch (path) {
        case 'help':
          report(helpMarkdown(args));
          return true;
        case 'models':
          setModelSignal((n) => n + 1);
          return true;
        case 'temp':
        case 'maxtokens':
        case 'topp':
        case 'think':
        case 'gen': {
          const next = genFromArgs(name, args, gen);
          setGen(next);
          say(Object.keys(next).length ? t('Generation settings applied to this chat.') : t('Generation settings removed.'));
          return true;
        }
        case 'remember': {
          if (!workspace) {
            say(t('Saving a rule needs a working folder.'), 'warning');
            return true;
          }
          try {
            const result = await rememberRule(workspace, args);
            say(result.duplicate ? t('That rule was already in the project instructions.') : t('Rule saved in {where}.', { where: result.path ?? t('the project instructions') }));
          } catch (error) {
            say(`${t('Could not save the rule')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'compact': {
          if (!sessionId) return true;
          try {
            const result = await compactSession(sessionId);
            say(result.compacted ? t('History compacted.') : result.detail ?? t('There was not enough to compact.'));
            const refreshed = await turnsFromHistory(sessionId);
            setTurns(refreshed.turns);
          } catch (error) {
            say(`${t('Could not compact')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.truncate':
        case 'truncate': {
          const keep = Number.parseInt(args, 10);
          if (!sessionId || !keep || keep < 1) {
            say(t('Usage: /truncate N — keeps the first N messages and deletes the rest.'), 'warning');
            return true;
          }
          try {
            await truncateSession(sessionId, keep, 'truncate');
            const refreshed = await turnsFromHistory(sessionId);
            setTurns(refreshed.turns);
            say(t('Kept the first {n} messages. /versions recovers the deleted ones.', { n: keep }));
          } catch (error) {
            say(`${t('Could not truncate')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'versions': {
          if (!sessionId) return true;
          try {
            const versions = await listVersions(sessionId);
            report(
              versions.length
                ? [
                    `| ${t('Version')} | ${t('When')} | ${t('Why')} | ${t('Messages')} |`,
                    '| --- | --- | --- | ---: |',
                    ...versions.map(
                      (v) =>
                        `| \`/restore ${v.id}\` | ${relativeTime(v.createdAt) || String(v.createdAt)} | ${v.reason || t('edit')} | ${v.removed} |`,
                    ),
                    '',
                    versions[0]?.preview ? `> ${versions[0].preview.replace(/\s+/g, ' ').slice(0, 200)}` : '',
                  ].join('\n')
                : t('No versions yet: one is saved every time an edit or a regenerate removes messages.'),
            );
          } catch (error) {
            say(`${t('Could not read the versions')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'restore': {
          if (!sessionId || !args) {
            say(t('Usage: /restore ID (the IDs come from /versions).'), 'warning');
            return true;
          }
          try {
            await restoreVersion(sessionId, args.trim());
            const refreshed = await turnsFromHistory(sessionId);
            setTurns(refreshed.turns);
            say(t('Version restored.'));
          } catch (error) {
            say(`${t('Could not restore')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'checkpoints': {
          if (!workspace) {
            say(t('Checkpoints go with the working folder: pick one first.'), 'warning');
            return true;
          }
          try {
            const list = await listCheckpoints(workspace);
            say(
              list.length
                ? list.map((c) => `${c.sha.slice(0, 10)}  ·  ${c.createdAt ? relativeTime(c.createdAt) || c.createdAt : ''}  ·  ${c.reason ?? ''}`).join('\n') +
                    '\n\n' + t('To go back to one, use "Back to before this turn" in the turn summary.')
                : t('No checkpoints in this folder yet.'),
            );
          } catch (error) {
            say(`${t('Could not read the checkpoints')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.export':
        case 'export': {
          if (!sessionId) return true;
          const fmt = (args.trim().toLowerCase() || 'md') as ExportFormat;
          if (!EXPORT_FORMATS.includes(fmt)) {
            say(t('Formats: {list}.', { list: EXPORT_FORMATS.join(', ') }), 'warning');
            return true;
          }
          try {
            await downloadExport(sessionId, fmt, sessions?.find((x) => x.id === sessionId)?.name);
          } catch (error) {
            // A blank tab is what this used to be. The server explains itself
            // — a format it does not know, a missing PDF dependency — so say
            // what it said.
            say(`${t('Could not export')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.rename':
        case 'rename': {
          if (!sessionId || !args) {
            say(t('Usage: /rename new name'), 'warning');
            return true;
          }
          try {
            await renameSession(sessionId, args);
            setTitle(args);
            refreshSessions();
          } catch (error) {
            say(`${t('Could not rename')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'agents': {
          const parsed = parseDelegation(args);
          if (typeof parsed === 'string') {
            say(parsed, 'warning');
            return true;
          }
          if (busy) {
            say(t('Wait for the current turn to finish before delegating.'), 'warning');
            return true;
          }
          const label = delegationLabel(parsed);
          const sid = await ensureSession(label);
          if (sid) void run(sid, label, { delegation: parsed });
          return true;
        }
        case 'doc': {
          if (args) {
            try {
              const doc = await createDoc({ title: args, sessionId });
              panelDispatch({ type: 'doc', doc: { streaming: false, id: doc.id, title: doc.title, language: doc.language, content: doc.content, version: doc.versionCount, suggestions: [] } });
            } catch (error) {
              say(`${t('Could not create the document')}: ${(error as Error).message}`, 'danger');
            }
          } else {
            panelDispatch({ type: 'open', tab: 'doc' });
          }
          return true;
        }
        case 'browser':
          panelDispatch({ type: 'open', tab: 'browser' });
          return true;
        case 'open': {
          if (!workspace) {
            say(t('Opening a file needs a working folder.'), 'warning');
            return true;
          }
          if (!args) {
            say(t('Usage: /open path/to/file (relative to the working folder).'), 'warning');
            return true;
          }
          panelDispatch({ type: 'file', workspace, path: args });
          return true;
        }
        case 'usage': {
          const v = args.toLowerCase();
          const { isUsageVisible, setUsageVisible } = await import('../adapters/usage');
          const next = v === 'on' ? true : v === 'off' ? false : !isUsageVisible();
          setUsageVisible(next);
          say(next ? t('Usage is back in the header.') : t('Usage hidden. /usage on brings it back.'));
          return true;
        }
        case 'incognito': {
          const v = args.toLowerCase();
          setKnobs((k) => ({ ...k, incognito: v === 'on' ? true : v === 'off' ? false : !k.incognito }));
          return true;
        }
        case 'preset': {
          if (!args) {
            setPresetSignal((n) => n + 1);
            return true;
          }
          if (/^(off|no|none|ninguno)$/i.test(args)) {
            setPreset(null);
            say(t('No preset.'));
            return true;
          }
          try {
            const { listPresets } = await import('../adapters/presets');
            const all = await listPresets();
            const hit = all.find((p) => p.name.toLowerCase() === args.toLowerCase() || p.id.toLowerCase() === args.toLowerCase()) ?? all.find((p) => p.name.toLowerCase().includes(args.toLowerCase()));
            if (!hit) say(t('There is no preset called "{name}". The ones there are: {list}', { name: args, list: all.map((p) => p.name).join(', ') }), 'warning');
            else {
              setPreset({ id: hit.id, name: hit.name });
              say(`Preset: ${hit.name}.`);
            }
          } catch (error) {
            say(`${t('Could not read the presets')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.fork':
        case 'fork':
          extrasRef.current.fork();
          return true;
        case 'tts':
          extrasRef.current.tts();
          return true;
        case 'stats':
        case 'chats.info': {
          const list = turns ?? [];
          const out = list.filter((turn) => turn.role === 'assistant' && turn.metrics);
          const tokens = out.reduce((n, turn) => n + (turn.metrics?.outputTokens ?? 0), 0);
          const seconds = out.reduce((n, turn) => n + (turn.metrics?.responseTime ?? 0), 0);
          const here = [
            `### ${current?.name ?? t('This conversation')}`,
            '',
            `${tn(list.length, '{n} message', '{n} messages')} · ${t('{n} tokens generated', { n: tokens })} · ${t('{s} s of model', { s: seconds.toFixed(1) })} · \`${current?.model ?? route?.model ?? t('no model')}\``,
          ].join('\n');
          void cmd
            .dbStats()
            .then((db) => report(db ? `${here}\n\n${db}` : here))
            .catch(() => report(here));
          return true;
        }

        /* ── Conversations ── */
        case 'chats.new':
          laterRef.current.openSession(null);
          if (args) setDraft(args);
          return true;
        case 'chats.delete': {
          if (!sessionId) return true;
          try {
            await deleteSession(sessionId);
            refreshSessions();
            navigate('/studio');
            say(t('Conversation deleted.'));
          } catch (error) {
            say(`${t('Could not delete')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.archive': {
          if (!sessionId) return true;
          try {
            await archiveSession(sessionId);
            refreshSessions();
            navigate('/studio');
            say(t('Archived. The Library keeps it under "Archive".'));
          } catch (error) {
            say(`${t('Could not archive')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.favorite':
        case 'chats.unfavorite': {
          if (!sessionId) return true;
          const on = path === 'chats.favorite';
          try {
            await setSessionImportant(sessionId, on);
            refreshSessions();
            say(on ? t('Marked as a favourite.') : t('No longer a favourite.'));
          } catch (error) {
            say(`${t('Could not change it')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.switch': {
          const needle = args.toLowerCase();
          if (!needle) {
            say(t('Usage: /chats switch name (or part of it).'), 'warning');
            return true;
          }
          const hit = (sessions ?? []).find((session) => session.id === args) ?? (sessions ?? []).find((session) => (session.name ?? '').toLowerCase().includes(needle));
          if (!hit) {
            say(t('No conversation matches "{q}".', { q: args }), 'warning');
            return true;
          }
          laterRef.current.openSession(hit.id);
          return true;
        }
        case 'chats.sort': {
          say(t('Sorting the conversations into folders…'));
          try {
            const result = await autoSortSessions(false);
            refreshSessions();
            say(t('{n} conversations filed into {f} folders.', { n: String(result.updated), f: String(result.folders.length) }));
          } catch (error) {
            say(`${t('Could not sort them')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'chats.clear':
          setTurns([]);
          say(t('Cleared here. The conversation is still on the server: reload to see it again.'));
          return true;
        case 'chats.export-all': {
          const parts = args.split(/\s+/).filter(Boolean);
          const format = (parts.find((word) => (EXPORT_FORMATS as readonly string[]).includes(word.toLowerCase())) ?? 'md') as ExportFormat;
          const folder = parts.filter((word) => word.toLowerCase() !== format).join(' ');
          try {
            await downloadExportZip(format, folder ? { folder } : {});
          } catch (error) {
            say(`${t('Could not export')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }

        /* ── Switches ── */
        case 'toggle.web':
        case 'toggle.bash':
        case 'toggle.plan':
        case 'toggle.rag':
        case 'toggle.research': {
          const key = path.slice('toggle.'.length) as 'web' | 'bash' | 'plan' | 'rag' | 'research';
          const wanted = /^(on|off)$/i.test(args) ? args.toLowerCase() === 'on' : !knobs[key];
          setKnobs((k) => ({ ...k, [key]: wanted }));
          say(wanted ? t('{what}: on', { what: t(SWITCH_LABEL[key]) }) : t('{what}: off', { what: t(SWITCH_LABEL[key]) }));
          return true;
        }
        case 'toggle.doc':
          if (panel.open && panel.tab === 'doc') panelDispatch({ type: 'close' });
          else panelDispatch({ type: 'open', tab: 'doc' });
          return true;
        case 'toggle.sidebar':
          laterRef.current.toggleSidebar();
          return true;
        case 'toggle._show': {
          const rows = (['web', 'bash', 'plan', 'rag', 'research', 'incognito'] as const).map((key) =>
            `| ${t(SWITCH_LABEL[key])} | ${knobs[key] ? t('on') : t('off')} |`,
          );
          report([
            `| ${t('Switch')} | ${t('State')} |`,
            '| --- | --- |',
            ...rows,
            `| ${t('Mode')} | ${knobs.mode === 'agent' ? t('Agent') : t('Chat')} |`,
            `| ${t('Working folder')} | ${workspace ? `\`${workspace}\`` : t('none')} |`,
          ].join('\n'));
          return true;
        }

        /* ── The working folder ── */
        case 'workspace': {
          const word = args.split(/\s+/)[0]?.toLowerCase() ?? '';
          const tail = args.slice(word.length).trim();
          if (!args || word === 'show' || word === 'status' || word === 'info') {
            report(workspace ? t('Working folder: `{ws}`', { ws: workspace }) : t('No working folder. `/workspace pick`, or `/workspace <path>`.'));
            return true;
          }
          if (word === 'clear' || word === 'off' || word === 'none' || word === 'unset') {
            setWorkspace('');
            say(t('Working folder cleared.'));
            return true;
          }
          if (word === 'pick' || word === 'browse' || word === 'open') {
            void pickWorkspace();
            return true;
          }
          const wanted = word === 'set' || word === 'cd' || word === 'use' ? tail : args;
          if (!wanted) {
            say(t('Usage: /workspace <path> · pick · clear'), 'warning');
            return true;
          }
          try {
            const vetted = await vetWorkspace(wanted);
            if (!vetted) {
              report(t('The server will not bind `{path}` as a working folder. If Faustus runs in Docker, use the path inside the container.', { path: wanted }), 'warning');
              return true;
            }
            setWorkspace(vetted);
            say(t('Working folder: {ws}', { ws: vetted }));
          } catch (error) {
            say((error as Error).message, 'danger');
          }
          return true;
        }
        case 'sh': {
          if (!args) {
            say(t('Usage: /sh command'), 'warning');
            return true;
          }
          report(t('Running `{cmd}`…', { cmd: args }));
          await reportFrom(() => cmd.shellExec(args));
          return true;
        }

        /* ── Reports ── */
        case 'backup': {
          const word = args.split(/\s+/)[0]?.toLowerCase() ?? '';
          if (word === 'now') {
            say(t('Taking a snapshot of the data folder…'));
            await reportFrom(() => cmd.backupNow());
          } else if (word === 'verify') {
            await reportFrom(() => cmd.backupVerify(Number.parseInt(args.split(/\s+/)[1] ?? '1', 10) || 1));
          } else {
            await reportFrom(() => cmd.backupList());
          }
          return true;
        }
        case 'scorecard': {
          const words = args.toLowerCase().split(/\s+/).filter(Boolean);
          if (words[0] === 'clear' || words[0] === 'reset') {
            await reportFrom(() => cmd.scorecardClear());
            return true;
          }
          const here = words.some((word) => word === 'here' || word === 'project' || word === 'workspace');
          if (here && !workspace) {
            say(t('Pick a working folder first to see the scorecard for this project.'), 'warning');
            return true;
          }
          const days = Number.parseFloat(words.find((word) => Number.isFinite(Number.parseFloat(word))) ?? '30') || 30;
          await reportFrom(() => cmd.scorecard(days, here ? workspace : undefined));
          return true;
        }
        case 'researchfit': {
          const words = args.toLowerCase().split(/\s+/).filter(Boolean);
          if (words[0] === 'apply') await reportFrom(() => cmd.researchFitApply(words.includes('fixes')));
          else await reportFrom(() => cmd.researchFit());
          return true;
        }
        case 'agentsmd': {
          if (!workspace) {
            say(t('An AGENTS.md belongs to a folder: pick a working folder first.'), 'warning');
            return true;
          }
          await reportFrom(() => cmd.agentsMd(workspace, args.toLowerCase().includes('write')));
          return true;
        }
        case 'project': {
          const list = projects ?? [];
          // The project of this conversation is the one whose folder is bound.
          const here = workspace ? list.find((p) => p.workspace === workspace) : undefined;
          const lines = here
            ? [`### [${here.name}](/projects/${here.id})`, '', t('Folder: `{ws}`', { ws: workspace }), '', here.instructions ? t('It has standing instructions.') : t('No standing instructions yet: `/agentsmd` drafts them.'), '']
            : [`### ${t('No project bound')}`, '', workspace ? t('The folder `{ws}` does not belong to a project.', { ws: workspace }) : t('No working folder either.'), ''];
          lines.push(
            list.length
              ? [`#### ${tn(list.length, '{n} project', '{n} projects')}`, '', ...list.map((p) => `- [${p.name}](/projects/${p.id})${p.workspace ? ` — \`${p.workspace}\`` : ''}`)].join('\n')
              : t('No projects yet. [Projects](/projects) is where they start.'),
          );
          report(lines.join('\n'));
          return true;
        }
        case 'ping':
          say(t('Asking the endpoints…'));
          await reportFrom(() => cmd.ping());
          return true;
        case 'probe': {
          let endpointId: string | undefined;
          if (args) {
            const found = await cmd.endpointIdByName(args);
            if (!found) {
              report(t('No endpoint matches "{q}". /ping lists them.', { q: args }), 'warning');
              return true;
            }
            endpointId = found;
          }
          report(cmd.probeMarkdown([], false));
          try {
            const rows = await cmd.probe(endpointId, (partial) => report(cmd.probeMarkdown(partial, false)));
            report(cmd.probeMarkdown(rows, true));
          } catch (error) {
            say(`${t('Could not probe')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'find': {
          if (!args) {
            say(t('Usage: /find text'), 'warning');
            return true;
          }
          await reportFrom(() => cmd.findInChats(args));
          return true;
        }
        case 'search': {
          if (!args) {
            setKnobs((k) => ({ ...k, web: !k.web }));
            say(t('Web search: {state}', { state: knobs.web ? t('off') : t('on') }));
            return true;
          }
          setKnobs((k) => ({ ...k, web: true }));
          const sid = await ensureSession(args);
          if (sid) void run(sid, args);
          return true;
        }

        /* ── Memory, skills, indexed folders ── */
        case 'memory.list': {
          await reportFrom(async () => {
            const list = await listMemories();
            if (!list.length) return t('No memories yet. `/memory add text` saves one.');
            return [
              `| ${t('Id')} | ${t('Memory')} | ${t('Category')} |`,
              '| --- | --- | --- |',
              ...list.slice(0, 40).map((m) => `| \`${m.id}\` | ${m.text.replace(/\s+/g, ' ').slice(0, 120)} | ${m.category || '—'} |`),
              '',
              t('`/memory delete id` removes one; [Memory](/memory) is the screen.'),
            ].join('\n');
          });
          return true;
        }
        case 'memory.add': {
          if (!args) {
            say(t('Usage: /memory add what to remember'), 'warning');
            return true;
          }
          try {
            await addMemory(args, 'general', sessionId);
            say(t('Saved in the memory.'));
          } catch (error) {
            say(`${t('Could not save it')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'memory.delete': {
          if (!args) {
            report(t('Usage: /memory delete id (the ids come from /memory list).'), 'warning');
            return true;
          }
          try {
            await deleteMemory(args);
            say(t('Deleted.'));
          } catch (error) {
            say(`${t('Could not delete it')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'memory.search': {
          if (!args) {
            say(t('Usage: /memory search text'), 'warning');
            return true;
          }
          await reportFrom(async () => {
            const needle = args.toLowerCase();
            const hits = (await listMemories()).filter((m) => m.text.toLowerCase().includes(needle));
            if (!hits.length) return t('Nothing in the memory matches "{q}".', { q: args });
            return [`| ${t('Id')} | ${t('Memory')} |`, '| --- | --- |', ...hits.slice(0, 40).map((m) => `| \`${m.id}\` | ${m.text.replace(/\s+/g, ' ').slice(0, 160)} |`)].join('\n');
          });
          return true;
        }
        case 'note': {
          if (!args) {
            say(t('Usage: /note what you want to remember'), 'warning');
            return true;
          }
          try {
            await createNote({ content: args });
            report(t('Saved in [Notes](/notes).'));
          } catch (error) {
            say(`${t('Could not save the note')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'todo': {
          if (!args) {
            say(t('Usage: /todo what has to be done'), 'warning');
            return true;
          }
          try {
            await createNote({ noteType: 'checklist', title: args.slice(0, 60), items: [{ text: args, done: false }] });
            report(t('Added to [Notes](/notes).'));
          } catch (error) {
            say(`${t('Could not add it')}: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'event': {
          if (!args) {
            navigate('/calendar');
            return true;
          }
          try {
            const { createEvent, quickParse } = await import('../adapters/calendar');
            const parsed = await quickParse(args);
            if (!parsed?.dtstart) {
              say(t('I did not understand that as an event. The Calendar has a form.'), 'warning');
              navigate('/calendar');
              return true;
            }
            await createEvent({
              summary: parsed.summary || args,
              dtstart: parsed.allDay ? parsed.dtstart.slice(0, 10) : parsed.dtstart,
              dtend: parsed.allDay ? null : parsed.dtend || null,
              allDay: parsed.allDay,
              location: parsed.location,
              description: parsed.description,
            });
            say(`${t('Created')}: ${parsed.summary || args}${parsed.confidence < 0.6 ? t(' (check it)') : ''}`);
          } catch (error) {
            say(`${(error as Error).message}. ${t('The Calendar has a form.')}`, 'warning');
          }
          return true;
        }
        case 'skills':
          await reportFrom(() => cmd.skillsMarkdown(args));
          return true;
        case 'reload-skills':
          await reportFrom(() => cmd.reloadSkills());
          return true;
        case 'rag.list':
          await reportFrom(() => cmd.ragList());
          return true;
        case 'rag.add': {
          if (!args) {
            say(t('Usage: /rag add /path/to/folder'), 'warning');
            return true;
          }
          report(t('Indexing `{dir}`…', { dir: args }));
          await reportFrom(() => cmd.ragAdd(args));
          return true;
        }
        case 'rag.remove': {
          if (!args) {
            say(t('Usage: /rag remove /path/to/folder'), 'warning');
            return true;
          }
          await reportFrom(() => cmd.ragRemove(args));
          return true;
        }

        /* ── Look and feel ── */
        case 'model': {
          if (!args) {
            say(route ? t('Model: {name} ({where})', { name: route.model, where: route.endpointName ?? route.kind ?? '' }) : t('No model yet.'));
            return true;
          }
          const needle = args.toLowerCase();
          const hit = routes.find((r) => r.model.toLowerCase() === needle) ?? routes.find((r) => r.model.toLowerCase().includes(needle));
          if (!hit) {
            report(t('No model matches "{q}". /models opens the list.', { q: args }), 'warning');
            return true;
          }
          setRouteId(hit.id);
          say(t('Model: {name}', { name: hit.model }));
          return true;
        }
        case 'theme': {
          const wanted = args.toLowerCase().trim();
          if (!wanted) {
            navigate('/settings?s=appearance');
            return true;
          }
          if (wanted === 'dark' || wanted === 'light' || wanted === 'system') {
            setMode(wanted as ThemeChoice);
            say(t('Appearance: {mode}', { mode: t(wanted === 'dark' ? 'dark' : wanted === 'light' ? 'light' : 'system') }));
            return true;
          }
          navigate(`/settings?s=appearance&theme=${encodeURIComponent(args.trim())}`);
          return true;
        }
        case 'shortcuts': {
          const binds = await getKeybinds();
          report(
            [
              `| ${t('Shortcut')} | ${t('What it does')} |`,
              '| --- | --- |',
              ...Object.entries(binds)
                .filter(([, combo]) => combo)
                .map(([action, combo]) => `| \`${combo}\` | ${t(KEYBIND_LABELS[action] ?? action)} |`),
              '',
              t('[Settings](/settings?s=shortcuts) is where they change.'),
            ].join('\n'),
          );
          return true;
        }
        case 'setup': {
          const provider = (resolved.sub?.name ?? args.split(/\s+/)[0] ?? '').toLowerCase();
          navigate('/settings?s=models');
          // A credential does not travel in a command: Settings is where the
          // endpoint is added, and the key goes in its own field.
          if (provider) report(t('Add **{provider}** here, in "Endpoints". If you typed the key, do not paste it into a command: it stays in the conversation.', { provider }), 'warning');
          return true;
        }

        /* ── Tours ── */
        case 'demo':
        case 'tour-compare':
        case 'tour-cookbook':
        case 'tour-research':
        case 'tour-library':
        case 'tour-theme':
        case 'tour-settings':
        case 'tour-gallery':
        case 'tour-brain':
        case 'tour-task-1':
        case 'tour-task-2':
          setNotice(null);
          startTour(path);
          return true;
        case 'tours': {
          if (args.toLowerCase().startsWith('reset')) {
            resetTours();
            say(t('The tours will offer themselves again.'));
            return true;
          }
          const seen = seenTours();
          report(
            [
              `| ${t('Tour')} | ${t('Where')} | ${t('Steps')} |`,
              '| --- | --- | ---: |',
              ...TOURS.map((tour) => `| \`/${tour.id}\` | ${t(tour.title)} | ${tour.steps.length}${seen.includes(tour.id) ? ' ·' : ''} |`),
              '',
              t('A dot marks the ones you have already seen. `/tours reset` offers them again.'),
            ].join('\n'),
          );
          return true;
        }

        /* ── The hidden ones ── */
        case 'flip':
        case 'roll':
        case '8ball':
        case 'fortune':
        case 'odyssey':
        case 'ascii':
        case 'matrix':
        case 'cowsay':
        case 'wisdom':
        case 'uptime':
        case 'color':
          setNotice({ text: '', tone: 'info', egg: makeEgg(path as EggKind, args, OPENED_AT) });
          return true;

        default:
          return false;
      }
    },
    [
      say,
      report,
      reportFrom,
      navigate,
      gen,
      workspace,
      setWorkspace,
      pickWorkspace,
      sessionId,
      sessions,
      turnsFromHistory,
      refreshSessions,
      turns,
      current,
      route,
      routes,
      setRouteId,
      projects,
      busy,
      ensureSession,
      run,
      knobs,
      setKnobs,
      panel.open,
      panel.tab,
    ],
  );

  /** A worker's task delegated again (its "Repetir…" button). */
  const rerunWorker = useCallback(
    (task: DelegationTask) => {
      if (busy) {
        say(t('Wait for the delegation to finish before repeating a worker.'), 'warning');
        return;
      }
      const delegation: Delegation = { tasks: [task], parallel: false, reviewer: false };
      const label = delegationLabel(delegation);
      void ensureSession(label).then((sid) => {
        if (sid) void run(sid, label, { delegation });
      });
    },
    [busy, say, ensureSession, run],
  );

  /** A new conversation with everything up to and including this reply. */
  const forkFrom = useCallback(
    async (turn: Turn) => {
      if (!sessionId || !turns) return;
      const index = turns.findIndex((t) => t.id === turn.id);
      if (index === -1) return;
      const keep = (turn.historyIndex ?? index) + 1;
      try {
        const copy = await forkSession(sessionId, keep);
        refreshSessions();
        say(t('Forked as "{name}".', { name: copy.name }));
        const next = new URLSearchParams(params);
        next.set('s', copy.id);
        setParams(next);
      } catch (error) {
        say(`${t('Could not fork')}: ${(error as Error).message}`, 'danger');
      }
    },
    [sessionId, turns, refreshSessions, say, params, setParams],
  );

  /** Selected text from a reply, as a quote at the end of the draft. */
  const quote = useCallback(
    (text: string) => {
      const block = text
        .split('\n')
        .map((l) => `> ${l}`)
        .join('\n');
      setDraft((d) => (d.trim() ? `${d.trimEnd()}\n\n${block}\n\n` : `${block}\n\n`));
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(el.value.length, el.value.length);
      });
    },
    [],
  );

  const refreshModels = useCallback(() => {
    setRefreshingModels(true);
    listModels(undefined, true)
      .then((list) => {
        setRoutes(list);
        say(`${list.length} modelos en ${new Set(list.map((r) => r.endpointId)).size} endpoints.`);
      })
      .catch(() => say(t('Could not refresh the models.'), 'danger'))
      .finally(() => setRefreshingModels(false));
  }, [say]);

  const lastSent = useMemo(() => {
    for (let i = (turns?.length ?? 0) - 1; i >= 0; i--) {
      const t = turns?.[i];
      if (t?.role === 'user' && t.text) return t.text;
    }
    return undefined;
  }, [turns]);

  /* ── Send ── */
  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if ((!message && attachments.length === 0) || busy || sendingMessage.current) return;

      const parsed = message ? parseCommand(message) : null;
      if (parsed) {
        setDraft('');
        await runCommand(parsed.name, parsed.args);
        return;
      }

      // `#regla` on its own line: a standing rule for the project, not a message.
      if (/^#(?!#)\s*\S/.test(message) && !message.includes('\n')) {
        setDraft('');
        await runCommand('remember', message.replace(/^#\s*/, ''));
        return;
      }

      sendingMessage.current = true;
      setPreparingMessage(true);
      try {
        const sent = attachments;
        const sid = await ensureSession(message || sent.map((a) => a.name).join(', '));
        if (!sid) return; // Keep the draft and attachments available for retry.
        setDraft(current => current.trim() === message ? '' : current);
        // A fresh chat becomes a session on its first send; the message left
        // through `new`, so `new` must not offer it again to the next chat.
        if (!sessionId) writeDraftFor(null, '');
        const sentIds = new Set(sent.map(item => item.id));
        setAttachments(list => list.filter(item => !sentIds.has(item.id)));
        setNotice(null);
        void run(sid, withImageReferences(message, sent), { attachments: sent });
      } finally {
        sendingMessage.current = false;
        setPreparingMessage(false);
      }
    },
    [attachments, busy, runCommand, ensureSession, run, sessionId],
  );

  /* Notas → "Resolver con el agente": sends as soon as a route is known and
     links the new conversation back to the note (`agent_session_id`). */
  useEffect(() => {
    const pending = autoSendRef.current;
    if (!pending || busy || !route) return;
    if (pending.mode && knobs.mode !== pending.mode) return;
    autoSendRef.current = null;
    void (async () => {
      setDraft('');
      const sid = await ensureSession(pending.text.split('\n').find((l) => l.trim() && !l.startsWith('Ayúdame') && !l.startsWith('Help me')) ?? pending.text);
      if (!sid) return;
      if (pending.noteId) {
        void import('../adapters/notes')
          .then((m) => m.updateNote(pending.noteId as string, { agentSessionId: sid }))
          .catch(() => undefined);
      }
      void run(sid, pending.text, { attachments: [] });
    })();
  }, [busy, route, ensureSession, run, knobs.mode]);

  const stop = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    // Closing our stream stops nothing: the run is detached. The server only
    // cancels it when it hears the run's own id back, so a Stop without one
    // was a button that did nothing but hide the evidence.
    const runId = runIdRef.current;
    runIdRef.current = null;
    if (sessionId) {
      void stopChat(sessionId, runId).then((stopped) => {
        refreshActivity();
        if (!stopped) say(t('I could not stop it: it is finishing on the server. It will save what it has.'), 'warning');
      });
    }
    patchLast((t) => apply(t, { type: 'done' }));
    setBusy(false);
  }, [sessionId, patchLast, say]);

  // TASK-04/UX-04: the Stop menu's harsher scopes ("Cancel task"/"Cancel all
  // work") and the softer "Stop generation" (also what Escape now sends —
  // see Composer.tsx). Same local bookkeeping as the original `stop` above;
  // only which server scope is asked for differs.
  const stopScoped = useCallback((scope: 'generation' | 'task' | 'work') => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    const runId = runIdRef.current;
    runIdRef.current = null;
    if (sessionId) {
      void stopChat(sessionId, runId, scope).then((ok) => {
        refreshActivity();
        if (!ok) say(t('I could not stop it: it is finishing on the server. It will save what it has.'), 'warning');
      });
    }
    patchLast((t) => apply(t, { type: 'done' }));
    setBusy(false);
  }, [sessionId, patchLast, say]);

  const pauseGeneration = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    const runId = runIdRef.current;
    runIdRef.current = null;
    if (sessionId) {
      void pauseChat(sessionId, runId).then((ok) => {
        refreshActivity();
        if (!ok) say(t('I could not stop it: it is finishing on the server. It will save what it has.'), 'warning');
      });
    }
    patchLast((t) => apply(t, { type: 'done' }));
    setBusy(false);
  }, [sessionId, patchLast, say]);

  const cancelTask = useCallback(() => stopScoped('task'), [stopScoped]);
  const cancelWork = useCallback(() => stopScoped('work'), [stopScoped]);

  // "Dirigir…" — injected into the live turn at its next safe point; does
  // NOT stop or replace the current stream.
  const steerLive = useCallback((text: string) => {
    if (!sessionId) return;
    void steerChat(sessionId, text, { runId: runIdRef.current, mode: 'steer' }).then((ok) => {
      if (!ok) say(t('I could not deliver that — nothing is running right now.'), 'warning');
    });
  }, [sessionId, say]);

  // "Enviar después" — held server-side, delivered as a new turn once this
  // one ends; never touches the live turn.
  const queueSend = useCallback((text: string) => {
    if (!sessionId) return;
    void steerChat(sessionId, text, { runId: runIdRef.current, mode: 'queue' }).then((ok) => {
      say(
        ok
          ? t('Queued — it will be sent once this turn finishes.')
          : t('I could not queue that — nothing is running right now.'),
        ok ? 'info' : 'warning',
      );
    });
  }, [sessionId, say]);

  /* ── Message actions ── */
  const regenerateFrom = useCallback(
    async (turn: Turn, text?: string) => {
      if (!sessionId || !turns) return;
      const index = turns.findIndex((t) => t.id === turn.id);
      if (index === -1) return;
      // The server counts its own messages, which may include ones the list
      // hides; prefer the position it gave us, fall back to the list's.
      const keep = turn.historyIndex ?? index;
      try {
        await truncateSession(sessionId, keep, text === undefined ? 'regenerate' : 'edit');
      } catch (error) {
        say(`${t('Could not prepare the regeneration')}: ${(error as Error).message}`, 'danger');
        return;
      }
      setTurns(turns.slice(0, index));
      void run(sessionId, text ?? turn.text, { attachments: turn.attachments });
    },
    [sessionId, turns, run, say],
  );

  const onEdit = useCallback(
    async (turn: Turn, text: string, regenerate: boolean) => {
      if (!sessionId) return;
      if (regenerate) {
        await regenerateFrom(turn, text);
        return;
      }
      if (!turn.dbId) return;
      try {
        await editMessage(sessionId, turn.dbId, text);
        setTurns((list) => list?.map((t) => (t.id === turn.id ? { ...t, text, edited: true } : t)) ?? list);
      } catch (error) {
        say(`${t('Could not save the edit')}: ${(error as Error).message}`, 'danger');
      }
    },
    [sessionId, regenerateFrom, say],
  );

  /**
   * Deleting a message is final: the server drops the row and, unlike
   * truncating, keeps no version to restore. So it asks first and then says
   * plainly that it is gone — the previous interface deleted on the first
   * click with nothing but a toast afterwards.
   */
  const [confirmDelete, setConfirmDelete] = useState<Turn | null>(null);

  const onDelete = useCallback((turn: Turn) => setConfirmDelete(turn), []);

  const deleteNow = useCallback(
    async (turn: Turn) => {
      setConfirmDelete(null);
      if (!sessionId || !turn.dbId) return;
      try {
        await deleteMessages(sessionId, [turn.dbId]);
        setTurns((list) => list?.filter((t) => t.id !== turn.id) ?? list);
        say(t('Message deleted.'));
      } catch (error) {
        say(`${t('Could not delete the message')}: ${(error as Error).message}`, 'danger');
      }
    },
    [sessionId, say],
  );

  const openSession = (id: string | null) => {
    // A group chat's parent lives in Group chat, where the table is; the
    // transcript is still readable here from the sessions list's menu.
    if (id && knownGroupParents().has(id)) {
      navigate(`/group?s=${encodeURIComponent(id)}`);
      return;
    }
    const next = new URLSearchParams(params);
    if (id) next.set('s', id);
    else next.delete('s');
    setParams(next);
    setDrawerOpen(false);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  extrasRef.current = {
    fork: () => {
      const last = [...(turns ?? [])].reverse().find((t) => t.role === 'assistant' && t.dbId);
      if (last) void forkFrom(last);
      else say(t('Nothing to fork yet.'), 'warning');
    },
    tts: () => {
      const last = [...(turns ?? [])].reverse().find((t) => t.role === 'assistant' && t.text);
      if (last) void speak(last.text).catch(() => say(t('No voice available.'), 'warning'));
      else say(t('There is no reply to read.'), 'warning');
    },
  };

  /* ── Keyboard shortcuts: the previous interface's, with its keybinds ── */
  const shortcutsRef = useRef<Record<string, () => void>>({});
  const deleteArmedRef = useRef(0);
  const narrow = () => window.matchMedia('(max-width: 1023px)').matches;
  const toggleSidebar = () => {
    if (narrow()) {
      setDrawerOpen((v) => !v);
      return;
    }
    setPaneHidden((v) => {
      writeJson(PANE_KEY, { hidden: !v });
      return !v;
    });
  };
  laterRef.current = { openSession, toggleSidebar };

  shortcutsRef.current = {
    search: () => {
      if (narrow()) setDrawerOpen(true);
      else setPaneHidden(false);
      requestAnimationFrame(() => searchRef.current?.focus());
    },
    toggle_sidebar: toggleSidebar,
    new_session: () => openSession(null),
    fav_session: () => {
      if (!sessionId || !current) return;
      setSessionImportant(sessionId, !current.isImportant)
        .then(() => {
          refreshSessions();
          say(current.isImportant ? t('Removed from favourites.') : t('Marked as favourite.'));
        })
        .catch(() => say(t('Could not change the favourite.'), 'danger'));
    },
    delete_session: () => {
      if (!sessionId) return;
      const now = Date.now();
      if (now - deleteArmedRef.current > 4000) {
        deleteArmedRef.current = now;
        say(t('Press the shortcut again within 4 s to delete this conversation.'), 'warning');
        return;
      }
      deleteArmedRef.current = 0;
      deleteSession(sessionId)
        .then(() => {
          refreshSessions();
          openSession(null);
          say(t('Conversation deleted.'));
        })
        .catch(() => say(t('Could not delete it.'), 'danger'));
    },
    cancel: () => {
      if (busy) stop();
      stopSpeaking();
    },
    tts: () => {
      const last = [...(turns ?? [])].reverse().find((t) => t.role === 'assistant' && t.text);
      if (last) void speak(last.text).catch(() => say(t('No voice available.'), 'warning'));
    },
    incognito: () => setKnobs((k) => ({ ...k, incognito: !k.incognito })),
    settings: () => navigate('/settings'),
    focus_input: () => textareaRef.current?.focus(),
    open_calendar: () => navigate('/calendar'),
    open_compare: () => navigate('/compare'),
    open_cookbook: () => navigate('/cookbook'),
    open_research: () => navigate('/research'),
    open_gallery: () => navigate('/library?type=imagen'),
    open_library: () => navigate('/library'),
    open_memory: () => navigate('/memory'),
    open_notes: () => navigate('/notes'),
    open_tasks: () => navigate('/automations'),
    open_theme: () => navigate('/settings?s=appearance'),
  };
  useEffect(() => {
    let binds: Record<string, string> | null = null;
    void getKeybinds().then((b) => {
      binds = b;
    });
    const onKey = (e: KeyboardEvent) => {
      if (!binds) return;
      const target = e.target as HTMLElement | null;
      const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);
      for (const [action, combo] of Object.entries(binds)) {
        if (!combo || !matchesCombo(e, combo)) continue;
        // Plain Escape inside the composer is its own affair (stop / close
        // the pickers); the global one only runs outside text fields.
        if (action === 'cancel' && typing) return;
        // With a dialog, menu or popover open, Escape belongs to it (Radix
        // listens on the document, below this capture listener).
        if (action === 'cancel' && document.querySelector('#fs-overlay-root [data-state="open"], #fs-overlay-root [role="dialog"]')) return;
        // Ctrl+K is the shell palette's; leave it alone.
        if (action === 'search' && combo === 'ctrl+k') return;
        // The previous shell's keyboard-shortcuts.js still listens on the
        // document underneath the pilot and would act twice (Ctrl+Alt+N made
        // it create an empty session of its own). This listener runs in the
        // capture phase on window, so stopping here keeps the key ours.
        e.stopPropagation();
        const fn = shortcutsRef.current[action];
        if (!fn) return;
        e.preventDefault();
        fn();
        return;
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, []);

  const pending = turns?.length ? Boolean(turns[turns.length - 1].ask) : false;
  const isEmpty = !sessionId && turns !== null && turns.length === 0;

  return (
    <div className="fs-studio" style={{['--fs-panel-width' as string]:`${panel.width}px`}} data-testid="studio" data-voice={Boolean(voiceSession && voiceSession === sessionId) || undefined} data-drawer={drawerOpen || undefined} data-pane={paneHidden ? 'hidden' : undefined} data-panel={panel.open || undefined} data-incognito={knobs.incognito || undefined}>
      <SessionsPane
        sessions={sessions}
        currentId={sessionId}
        filter={filter}
        setFilter={setFilter}
        searchRef={searchRef}
        onOpen={openSession}
        onChanged={refreshSessions}
        onNotice={say}
      />
      {drawerOpen && <button type="button" className="fs-studio__scrim" aria-label={t('Close the conversation list')} onClick={() => setDrawerOpen(false)} />}

      <section className="fs-studio__stage">
        <header className="fs-studio__head">
          <button type="button" className="fs-studio__chip fs-studio__drawer-btn" aria-expanded={drawerOpen || !paneHidden} title={t('Show or hide the conversations (Ctrl+B)')} onClick={toggleSidebar} data-testid="studio-drawer">
            <MessageSquare size={13} aria-hidden="true" />
            <span>{t('Conversations')}</span>
          </button>
          <h1 className="fs-studio__title" title={title || undefined}>
            {sessionId ? stripGroupPrefix(current?.name || title || t('Conversation')) : t('New conversation')}
          </h1>
          {project && (
            <Link to={`/projects/${encodeURIComponent(project.id)}`} className="fs-studio__chip fs-studio__project" title={t('Open the project {name}', { name: project.name })} data-testid="studio-project">
              <FolderKanban size={13} aria-hidden="true" />
              <span>{project.name}</span>
            </Link>
          )}
          <Vitals busy={busy} />
          <div className="fs-studio__head-actions">
            <IconButton
              icon={PanelRight}
              label={panel.open ? t('Close the side panel') : t('Side panel: browser, document, file')}
              size="sm"
              onClick={() => panelDispatch(panel.open ? { type: 'close' } : { type: 'open' })}
            />
          </div>
        </header>

        <div className="fs-studio__transcript-stage" data-message-nav={turns&&turns.length>1||undefined}>
        <div className="fs-studio__scroll" ref={scrollRef} onScroll={onScroll} data-testid="studio-transcript">
          {turns === null && !loadError && (
            <div className="fs-studio__loading">
              <Skeleton label={t('Loading the conversation')} count={4} height="64px" />
            </div>
          )}
          {loadError && (
            <p className="fs-notice" data-tone="danger">
              {loadError}
            </p>
          )}

          {isEmpty && display.welcome && (
            <div className="fs-studio__hero fs-spot" onMouseMove={spotlight}>
              <span className="fs-watermark" aria-hidden="true">
                <BrandMark size={320} />
              </span>
              <p className="fs-studio__kicker">Studio</p>
              <h2 className="fs-home__title fs-studio__hero-title">
                {t('What are we')} <em>{t('doing')}</em> {t('today?')}
              </h2>
              <p className="fs-prose">
                {t('Type below. In agent mode it uses tools and shows you every step in the rail; in chat mode it only talks.')}
                <br />
                <code>{t('@file')}</code> {t('mentions a file in the workspace,')} <code>{t('#rule')}</code> {t('saves a standing instruction,')}{' '}
                <code>{t('/command')}</code> {t('does the rest.')}
              </p>
              <div className="fs-studio__suggestions">
                {SUGGESTIONS.map((key) => t(key)).map((text, i) => (
                  <button
                    key={text}
                    type="button"
                    className="fs-tile fs-studio__suggestion fs-enter"
                    style={{ ['--i' as string]: i + 2 }}
                    onClick={() => {
                      setDraft(text);
                      textareaRef.current?.focus();
                    }}
                  >
                    {text}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns && turns.length > 0 && (
            <Transcript
              turns={turns}
              busy={busy}
              onApproval={(turn, decision) => {
                if (sessionId && turn.ask?.approvalId) void run(sessionId, '', { approval: { id: turn.ask.approvalId, decision } });
              }}
              onAnswer={(turn, text, optionIds) => {
                // CALL-07/TASK-04: answers a specific ask_user question — like
                // onApproval above, this calls run() directly rather than
                // send() (which manages the composer draft); a question
                // answer never touches `draft`, so a 409 rejection (shown as
                // an error bubble via sendTurn's questionRejectionMessage)
                // leaves whatever the user was mid-typing untouched.
                if (sessionId) void run(sessionId, text, { questionId: turn.ask?.questionId, optionIds });
              }}
              onEdit={onEdit}
              onRegenerate={(turn) => void regenerateFrom(turn)}
              onDelete={onDelete}
              onNotice={say}
              onOpenFile={workspace ? (path) => panelDispatch({ type: 'file', workspace, path }) : undefined}
              onOpenDoc={(docId) => panelDispatch({ type: 'doc', doc: { streaming: false, id: docId, title: '', language: '', content: '', version: 0, suggestions: [] } })}
              onOpenEvidence={setEvidenceRef}
              onRerun={rerunWorker}
              onFork={knobs.incognito ? undefined : (turn) => void forkFrom(turn)}
              onQuote={quote}
            />
          )}
        </div>

        {turns&&<MessageNavigator key={sessionId||'new'} turns={turns} scrollRef={scrollRef} onJump={()=>{pinnedRef.current=false;}}/>}
        {/* OBJ-1 in the chat: the model of this turn does not fit next to what is loaded. */}
        <VramAdmissionDialog
          blocked={turns?.map((tn) => tn.vram).find((v) => v && v.ticket !== vramAnswered) ?? null}
          onDone={() => setVramAnswered(turns?.map((tn) => tn.vram).find(Boolean)?.ticket ?? null)}
          say={(text, tone) => say(text, tone === 'warn' ? 'warning' : 'info')}
        />
        {/* BENCH-03 (Lote 50 wiring): a tool card's "ver evidencia" button. */}
        <Dialog
          open={evidenceRef !== null}
          onOpenChange={(open) => { if (!open) setEvidenceRef(null); }}
          title={t('Evidence')}
          testId="evidence-dialog"
        >
          {evidenceRef && (
            <Suspense fallback={<Skeleton label={t('Loading evidence')} />}>
              <EvidenceInspector evidence={evidenceRef} workspace={workspace} />
            </Suspense>
          )}
        </Dialog>
        </div>

        {notice && (
          <div className="fs-notice fs-studio__notice" data-tone={notice.tone} data-wide={notice.rich || notice.egg ? '' : undefined} role="status" data-testid="studio-notice">
            {notice.egg ? (
              <Egg data={notice.egg} />
            ) : notice.rich ? (
              <div className="fs-studio__report">
                <Rich text={notice.text} />
              </div>
            ) : (
              <pre className="fs-studio__notice-text">{notice.text}</pre>
            )}
            <IconButton icon={X} label={t('Dismiss notice')} size="sm" onClick={() => setNotice(null)} />
          </div>
        )}

        {voiceSession && voiceSession === sessionId && <Suspense fallback={<div className="fs-studio__notice" role="status">{t('Opening voice mode…')}</div>}>
          <VoicePanel key={voiceSession} sessionName={project?.name || title || t('Conversation')} busy={busy}
            turn={turns?.slice().reverse().find(turn => turn.role === 'assistant')}
            onSend={text => void send(text)} onStop={stop} onClose={() => setVoiceSession(null)} />
        </Suspense>}
        <Composer
          draft={draft}
          setDraft={setDraft}
          busy={busy}
          pending={pending}
          preparing={preparingMessage}
          knobs={knobs}
          setKnobs={setKnobs}
          workspace={workspace}
          onPickWorkspace={() => void pickWorkspace()}
          onClearWorkspace={() => setWorkspace('')}
          gen={gen}
          onClearGen={() => setGen({})}
          attachments={attachments}
          setAttachments={(update) => setAttachments(update)}
          sessionId={sessionId}
          onSend={(text) => void send(text)}
          onStop={stop}
          onPauseGeneration={pauseGeneration}
          onCancelTask={cancelTask}
          onCancelWork={cancelWork}
          onSteer={steerLive}
          onQueueSend={queueSend}
          onVoice={() => {
            if (voiceSession) { setVoiceSession(null); return; }
            void ensureSession(t('Voice conversation')).then(sid => {
              if (sid && (visibleSession.current === sid || freshRef.current === sid)) setVoiceSession(sid);
            });
          }}
          voiceActive={Boolean(voiceSession)}
          onNotice={say}
          modelPicker={<ModelPicker routes={routes} current={route} onPick={(r) => setRouteId(r.id)} onRefresh={refreshModels} refreshing={refreshingModels} openSignal={modelSignal} />}
          presetChip={<><PresetPicker current={preset} onPick={(p) => setPreset(p ? { id: p.id, name: p.name } : null)} onNotice={say} openSignal={presetSignal} />{!knobs.incognito&&<ChatTeam sessionId={sessionId} routes={routes} coordinator={route} busy={busy} ensureSession={()=>ensureSession(t('Conversation'))} onEnabled={setTeamEnabled}/>}</>}
          lastSent={lastSent}
          extraControls={<><LocalVideo/><StyleLab key={'style:'+sessionId} model={route?route.model+'@'+route.endpointName:''} onSaved={saved=>setPreset({id:saved.id,name:saved.name})}/>{project&&!knobs.incognito?<ProjectVisualReferences key={sessionId||project.id} projectId={project.id} onUse={async(file,referenceRole)=>{
            const sid=sessionId;
            const uploaded=await uploadFiles([file],sid);
            if(visibleSession.current!==sid)return;
            if(!uploaded.length)throw new Error(t('The gallery did not return an image.'));
            setAttachments(items=>[...items,...uploaded.map(item=>({...item,referenceRole}))]);
          }}/>:null}</>}
          textareaRef={textareaRef}
        />
      </section>

      {panel.open && (
        <Suspense fallback={<aside className="fs-panel" aria-busy="true" />}>
          <SidePanel state={panel} dispatch={panelDispatch} onNotice={say} turns={turns||[]} workspace={workspace} project={project} busy={busy} onRerun={rerunWorker} onVisualSelection={async selection=>{
            const sid=sessionId;
            const file=await selectionImage(selection);
            if(visibleSession.current!==sid)return;
            const uploaded=await uploadFiles([file],sid);
            if(visibleSession.current!==sid)return;
            if(!uploaded.length)throw new Error(t('Could not prepare the capture.'));
            setAttachments(items=>[...items,...uploaded]);
            setDraft(text=>[text.trimEnd(),selectionPrompt(selection)].filter(Boolean).join('\n\n'));
            say(t('Capture and change request added to your draft.'));
          }}/>
        </Suspense>
      )}

      {wsOpen && (
        <Suspense fallback={null}>
          <WorkspaceDialog open initial={workspace} onClose={() => setWsOpen(false)} onPick={setWorkspace} />
        </Suspense>
      )}

      {confirmDelete && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setConfirmDelete(null);
          }}
          title={t('Delete this message?')}
          description={t('It does not go anywhere you can get it back from: the conversation loses it for good.')}
          testId="confirm-delete-message"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirmDelete(null)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} onClick={() => void deleteNow(confirmDelete)} />
            </>
          }
        >
          <p className="fs-prose">{confirmDelete.text.slice(0, 240) || t('(no text)')}</p>
        </Dialog>
      )}
    </div>
  );
}
