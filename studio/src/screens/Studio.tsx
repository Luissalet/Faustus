import { ExternalLink, MessageSquare, PanelRight, X } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { IconButton, Skeleton } from '../components';
import {
  createSession,
  listModels,
  listSessions,
  loadHistory,
  metricsFrom,
  sendTurn,
  stopChat,
  type ChatSession,
  type Delegation,
  type DelegationTask,
  type ModelRoute,
} from '../adapters/chat';
import { createDoc } from '../adapters/documents';
import {
  attachmentsFromMetadata,
  getRagActive,
  getWorkspace,
  pickNative,
  rememberRule,
  setRagActive,
  setWorkspace as persistWorkspace,
  type Attachment,
  type GenOverrides,
} from '../adapters/composer';
import {
  compactSession,
  deleteMessages,
  deleteSession,
  editMessage,
  exportUrl,
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
import { getKeybinds, matchesCombo } from '../adapters/settings';
import { listCheckpoints } from '../adapters/workspace';
import { BrandMark } from '../shell/BrandMark';
import { useSpotlight } from '../shell/useSpotlight';
import { ModelPicker } from './ModelPicker';
import { PresetPicker } from './PresetPicker';
import { COMMANDS, delegationLabel, genFromArgs, parseCommand, parseDelegation } from './studio/commands';
import { Composer, type Knobs } from './studio/Composer';
import { apply, blankTurn, cleanUserText, restoreFromMetadata, type Turn } from './studio/model';
import { initialPanel, panelReducer } from './studio/panel';
import { SessionsPane } from './studio/SessionsPane';
import { Transcript, type Decision } from './studio/Transcript';
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

const SUGGESTIONS = [
  'Explain this repository to me as if I had just joined the team',
  'Search the web for what changed this week on the topic I give you',
  'Write a script that sorts my downloads by type',
  'Review the last commit and tell me what could break',
];

interface Notice {
  text: string;
  tone: 'info' | 'warning' | 'danger';
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
    ...readJson<Knobs>(KNOBS_KEY, { mode: 'agent', web: false, bash: true, plan: false, rag: false, incognito: false }),
    rag: getRagActive(),
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
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(false);
  const display = useDisplay();
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Wide layouts hide the conversations column instead of sliding a drawer
  // (the previous shell's Ctrl+Alt+B); remembered across visits.
  const [paneHidden, setPaneHidden] = useState(() => readJson<{ hidden?: boolean }>(PANE_KEY, {}).hidden === true);
  const [filter, setFilter] = useState('');
  const [workspace, setWorkspaceState] = useState(() => getWorkspace());
  const [wsOpen, setWsOpen] = useState(false);
  const [gen, setGen] = useState<GenOverrides>({});
  const [modelSignal, setModelSignal] = useState(0);
  const [panel, panelDispatch] = useReducer(panelReducer, initialPanel);

  const controllerRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const pinnedRef = useRef(true);
  const freshRef = useRef<string | null>(null);
  const spotlight = useSpotlight();

  const route = useMemo(() => routes.find((r) => r.id === routeId) ?? routes[0] ?? null, [routes, routeId]);
  const current = useMemo(() => sessions?.find((s) => s.id === sessionId) ?? null, [sessions, sessionId]);

  const say = useCallback((text: string, tone: Notice['tone'] = 'info') => {
    setNotice({ text, tone });
  }, []);

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
          return !(next && next.role === 'assistant' && text.length < 160 && text.endsWith('?'));
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
    setAttachments([]);
    panelDispatch({ type: 'session-switch' });
    if (!sessionId) {
      setTurns([]);
      setTitle('');
      return;
    }
    setTurns(null);
    const controller = new AbortController();
    turnsFromHistory(sessionId, controller.signal)
      .then((result) => {
        setTitle(result.name);
        setTurns(result.turns);
        pinnedRef.current = true;
        if (result.model) {
          const match = (r: ModelRoute) => r.model === result.model;
          setRouteId((id) => routes.find(match)?.id ?? id);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoadError(t('Could not open this conversation.'));
      });
    return () => controller.abort();
    // routes is read once at load on purpose: the picker must not jump later.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

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
      try {
        const result = await turnsFromHistory(sid);
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
      options: { approval?: { id: string; decision: Decision }; attachments?: Attachment[]; delegation?: Delegation } = {},
    ) => {
      const controller = new AbortController();
      controllerRef.current = controller;
      setBusy(true);
      pinnedRef.current = true;
      panelDispatch({ type: 'turn-start' });
      if (options.approval) {
        patchLast((t) => {
          // The server also streams the permission question as text; once
          // answered it is noise above the real answer, so drop it.
          let text = t.text.trimEnd();
          const q = (t.ask?.question ?? '').trim();
          if (q && text.endsWith(q)) text = text.slice(0, -q.length).trimEnd();
          return { ...t, ask: undefined, streaming: true, text: text ? `${text}\n\n` : '' };
        });
      } else {
        const user = blankTurn('user', message);
        user.attachments = options.attachments ?? [];
        setTurns((list) => [...(list ?? []), user, blankTurn('assistant')]);
      }
      try {
        for await (const event of sendTurn({
          sessionId: sid,
          message,
          // A delegation only makes sense with tools: it forces agent mode.
          mode: options.delegation ? 'agent' : knobs.mode,
          planMode: knobs.mode === 'agent' && knobs.plan,
          allowBash: (knobs.mode === 'agent' || Boolean(options.delegation)) && knobs.bash,
          allowWebSearch: knobs.web,
          useRag: knobs.rag,
          workspace: knobs.mode === 'agent' || options.delegation ? workspace || undefined : undefined,
          route,
          attachments: options.attachments?.map((a) => a.id),
          genOverrides: Object.keys(gen).length ? (gen as Record<string, number | boolean>) : undefined,
          approval: options.approval,
          delegateTasks: options.delegation,
          incognito: knobs.incognito,
          presetId: preset?.id,
          activeDocId: panel.doc && !panel.doc.streaming ? panel.doc.id ?? undefined : undefined,
          signal: controller.signal,
        })) {
          patchLast((t) => apply(t, event));
          panelDispatch({ type: 'event', event, busy: true });
        }
      } catch (error) {
        if (!controller.signal.aborted) patchLast((t) => apply(t, { type: 'error', message: (error as Error).message }));
        patchLast((t) => apply(t, { type: 'done' }));
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          setBusy(false);
        }
        panelDispatch({ type: 'turn-end' });
        refreshSessions();
        void syncIds(sid);
      }
    },
    [knobs, workspace, route, gen, patchLast, refreshSessions, syncIds, preset, panel.doc],
  );

  const ensureSession = useCallback(
    async (name: string): Promise<string | null> => {
      if (sessionId) return sessionId;
      try {
        // Nobody mode: the session is named so the list hides it, and it is
        // remembered so it can be deleted when the mode ends.
        const sid = await createSession(knobs.incognito ? 'Incognito' : name.slice(0, 60), route);
        if (knobs.incognito) rememberIncognito(sid);
        setTitle(knobs.incognito ? t('Incognito') : name.slice(0, 60));
        freshRef.current = sid;
        const next = new URLSearchParams(params);
        next.set('s', sid);
        setParams(next, { replace: true });
        return sid;
      } catch {
        say(t('Could not create the conversation. Is the model server configured?'), 'danger');
        return null;
      }
    },
    [sessionId, route, params, setParams, say, knobs.incognito],
  );

  /* ── Slash commands ── */
  const runCommand = useCallback(
    async (name: string, args: string): Promise<boolean> => {
      const command = COMMANDS.find((c) => c.name === name);
      if (!command) {
        say(t('I do not know /{name}. Type /help to see the commands.', { name }), 'warning');
        return true;
      }
      if (command.route) {
        if (command.legacy) window.location.href = command.route.includes('?') ? command.route : `${command.route}?shell=legacy`;
        else navigate(command.route);
        return true;
      }
      switch (name) {
        case 'help':
          say(COMMANDS.map((c) => `${t(c.usage)} — ${t(c.help)}`).join('\n'));
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
            say(
              versions.length
                ? versions
                    .map((v) => `${v.id}  ·  ${relativeTime(v.createdAt) || v.createdAt}  ·  ${v.reason || t('edit')}  ·  ${tn(v.removed, '{n} message', '{n} messages')}\n   /restore ${v.id}`)
                    .join('\n')
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
        case 'export': {
          if (!sessionId) return true;
          const fmt = (args.trim().toLowerCase() || 'md') as ExportFormat;
          if (!EXPORT_FORMATS.includes(fmt)) {
            say(`Formatos: ${EXPORT_FORMATS.join(', ')}.`, 'warning');
            return true;
          }
          window.open(exportUrl(sessionId, fmt), '_blank', 'noopener');
          return true;
        }
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
        case 'fork':
          extrasRef.current.fork();
          return true;
        case 'tts':
          extrasRef.current.tts();
          return true;
        case 'stats': {
          const list = turns ?? [];
          const out = list.filter((t) => t.role === 'assistant' && t.metrics);
          const tokens = out.reduce((n, t) => n + (t.metrics?.outputTokens ?? 0), 0);
          const seconds = out.reduce((n, t) => n + (t.metrics?.responseTime ?? 0), 0);
          say(`${tn(list.length, '{n} message', '{n} messages')} · ${t('{n} tokens generated', { n: tokens })} · ${t('{s} s of model', { s: seconds.toFixed(1) })} · ${current?.model ?? route?.model ?? t('no model')}`);
          return true;
        }
        default:
          return false;
      }
    },
    [say, navigate, gen, workspace, sessionId, turnsFromHistory, refreshSessions, turns, current, route, busy, ensureSession, run, setKnobs],
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
      if ((!message && attachments.length === 0) || busy) return;

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

      setDraft('');
      const sent = attachments;
      setAttachments([]);
      setNotice(null);
      const sid = await ensureSession(message || sent.map((a) => a.name).join(', '));
      if (!sid) return;
      void run(sid, message, { attachments: sent });
    },
    [attachments, busy, runCommand, ensureSession, run],
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
    if (sessionId) void stopChat(sessionId);
    patchLast((t) => apply(t, { type: 'done' }));
    setBusy(false);
  }, [sessionId, patchLast]);

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

  const onDelete = useCallback(
    async (turn: Turn) => {
      if (!sessionId || !turn.dbId) return;
      try {
        await deleteMessages(sessionId, [turn.dbId]);
        setTurns((list) => list?.filter((t) => t.id !== turn.id) ?? list);
      } catch (error) {
        say(`${t('Could not delete the message')}: ${(error as Error).message}`, 'danger');
      }
    },
    [sessionId, say],
  );

  const openSession = (id: string | null) => {
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
    open_compare: () => {
      window.location.href = '/?shell=legacy';
    },
    open_cookbook: () => {
      window.location.href = '/?shell=legacy';
    },
    open_research: () => {
      window.location.href = '/?shell=legacy';
    },
    open_gallery: () => navigate('/library?type=imagen'),
    open_library: () => navigate('/library'),
    open_memory: () => navigate('/memory'),
    open_notes: () => navigate('/notes'),
    open_tasks: () => navigate('/automations'),
    open_theme: () => {
      window.location.href = '/?shell=legacy';
    },
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
    <div className="fs-studio" data-testid="studio" data-drawer={drawerOpen || undefined} data-pane={paneHidden ? 'hidden' : undefined} data-panel={panel.open || undefined} data-incognito={knobs.incognito || undefined}>
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
            {sessionId ? current?.name || title || t('Conversation') : t('New conversation')}
          </h1>
          <Vitals busy={busy} />
          <div className="fs-studio__head-actions">
            <IconButton
              icon={PanelRight}
              label={panel.open ? t('Close the side panel') : t('Side panel: browser, document, file')}
              size="sm"
              onClick={() => panelDispatch(panel.open ? { type: 'close' } : { type: 'open' })}
            />
            {sessionId && (
              <IconButton
                icon={ExternalLink}
                label={t('Open in the previous interface')}
                size="sm"
                onClick={() => {
                  window.location.href = `/?shell=legacy#${sessionId}`;
                }}
              />
            )}
          </div>
        </header>

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
              onAnswer={(text) => void send(text)}
              onEdit={onEdit}
              onRegenerate={(turn) => void regenerateFrom(turn)}
              onDelete={onDelete}
              onNotice={say}
              onOpenFile={workspace ? (path) => panelDispatch({ type: 'file', workspace, path }) : undefined}
              onOpenDoc={(docId) => panelDispatch({ type: 'doc', doc: { streaming: false, id: docId, title: '', language: '', content: '', version: 0, suggestions: [] } })}
              onRerun={rerunWorker}
              onFork={knobs.incognito ? undefined : (turn) => void forkFrom(turn)}
              onQuote={quote}
            />
          )}
        </div>

        {notice && (
          <div className="fs-notice fs-studio__notice" data-tone={notice.tone} role="status" data-testid="studio-notice">
            <pre className="fs-studio__notice-text">{notice.text}</pre>
            <IconButton icon={X} label={t('Dismiss notice')} size="sm" onClick={() => setNotice(null)} />
          </div>
        )}

        <Composer
          draft={draft}
          setDraft={setDraft}
          busy={busy}
          pending={pending}
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
          onNotice={say}
          modelPicker={<ModelPicker routes={routes} current={route} onPick={(r) => setRouteId(r.id)} onRefresh={refreshModels} refreshing={refreshingModels} openSignal={modelSignal} />}
          presetChip={<PresetPicker current={preset} onPick={(p) => setPreset(p ? { id: p.id, name: p.name } : null)} onNotice={say} openSignal={presetSignal} />}
          lastSent={lastSent}
          textareaRef={textareaRef}
        />
      </section>

      {panel.open && (
        <Suspense fallback={<aside className="fs-panel" aria-busy="true" />}>
          <SidePanel state={panel} dispatch={panelDispatch} onNotice={say} />
        </Suspense>
      )}

      {wsOpen && (
        <Suspense fallback={null}>
          <WorkspaceDialog open initial={workspace} onClose={() => setWsOpen(false)} onPick={setWorkspace} />
        </Suspense>
      )}
    </div>
  );
}
