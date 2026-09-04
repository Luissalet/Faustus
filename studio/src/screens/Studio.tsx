import { ExternalLink, MessageSquare, X } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
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
  type ModelRoute,
} from '../adapters/chat';
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
  editMessage,
  exportUrl,
  listVersions,
  renameSession,
  restoreVersion,
  truncateSession,
  type ExportFormat,
  EXPORT_FORMATS,
} from '../adapters/sessions';
import { relativeTime } from '../adapters/home';
import { listCheckpoints } from '../adapters/workspace';
import { BrandMark } from '../shell/BrandMark';
import { useSpotlight } from '../shell/useSpotlight';
import { ModelPicker } from './ModelPicker';
import { COMMANDS, genFromArgs, parseCommand } from './studio/commands';
import { Composer, type Knobs } from './studio/Composer';
import { apply, blankTurn, cleanUserText, type Turn } from './studio/model';
import { SessionsPane } from './studio/SessionsPane';
import { Transcript, type Decision } from './studio/Transcript';
import './projects.css';
import './home.css';
import './studio.css';

/* Rare, and the eager bundle has a budget: the folder picker arrives when opened. */
const WorkspaceDialog = lazy(() => import('./studio/WorkspaceDialog'));

const ROUTE_KEY = 'faustus_studio_route';
const KNOBS_KEY = 'faustus_studio_knobs';
const GEN_KEY = 'faustus_studio_gen';

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
  'Explícame este repositorio como si acabara de llegar al equipo',
  'Busca en la web qué ha cambiado esta semana en el tema que te diga',
  'Escribe un script que ordene mis descargas por tipo',
  'Revisa el último commit y dime qué puede romperse',
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
    ...readJson<Knobs>(KNOBS_KEY, { mode: 'agent', web: false, bash: true, plan: false, rag: false }),
    rag: getRagActive(),
  }));
  const [turns, setTurns] = useState<Turn[] | null>(sessionId ? null : []);
  const [title, setTitle] = useState('');
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const [workspace, setWorkspaceState] = useState(() => getWorkspace());
  const [wsOpen, setWsOpen] = useState(false);
  const [gen, setGen] = useState<GenOverrides>({});
  const [modelSignal, setModelSignal] = useState(0);

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

  const setKnobs = useCallback((update: (k: Knobs) => Knobs) => {
    setKnobsState((k) => {
      const next = update(k);
      if (next.rag !== k.rag) setRagActive(next.rag);
      return next;
    });
  }, []);

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
        if (m.role === 'assistant') turn.metrics = metricsFrom(m.metadata);
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
        if (!controller.signal.aborted) setLoadError('No he podido abrir esta conversación.');
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

  /* Inicio's quick starts arrive with the sentence begun: ?draft=… */
  useEffect(() => {
    const draftParam = params.get('draft');
    if (draftParam === null) return;
    setDraft(draftParam);
    const next = new URLSearchParams(params);
    next.delete('draft');
    setParams(next, { replace: true });
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    });
  }, [params, setParams]);

  useEffect(() => writeJson(KNOBS_KEY, knobs), [knobs]);
  useEffect(() => {
    if (routeId) writeJson(ROUTE_KEY, { id: routeId });
  }, [routeId]);

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
      options: { approval?: { id: string; decision: Decision }; attachments?: Attachment[] } = {},
    ) => {
      const controller = new AbortController();
      controllerRef.current = controller;
      setBusy(true);
      pinnedRef.current = true;
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
          mode: knobs.mode,
          planMode: knobs.mode === 'agent' && knobs.plan,
          allowBash: knobs.mode === 'agent' && knobs.bash,
          allowWebSearch: knobs.web,
          useRag: knobs.rag,
          workspace: knobs.mode === 'agent' ? workspace || undefined : undefined,
          route,
          attachments: options.attachments?.map((a) => a.id),
          genOverrides: Object.keys(gen).length ? (gen as Record<string, number | boolean>) : undefined,
          approval: options.approval,
          signal: controller.signal,
        })) {
          patchLast((t) => apply(t, event));
        }
      } catch (error) {
        if (!controller.signal.aborted) patchLast((t) => apply(t, { type: 'error', message: (error as Error).message }));
        patchLast((t) => apply(t, { type: 'done' }));
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          setBusy(false);
        }
        refreshSessions();
        void syncIds(sid);
      }
    },
    [knobs, workspace, route, gen, patchLast, refreshSessions, syncIds],
  );

  const ensureSession = useCallback(
    async (name: string): Promise<string | null> => {
      if (sessionId) return sessionId;
      try {
        const sid = await createSession(name.slice(0, 60), route);
        setTitle(name.slice(0, 60));
        freshRef.current = sid;
        const next = new URLSearchParams(params);
        next.set('s', sid);
        setParams(next, { replace: true });
        return sid;
      } catch {
        say('No he podido crear la conversación. ¿Está el servidor de modelos configurado?', 'danger');
        return null;
      }
    },
    [sessionId, route, params, setParams, say],
  );

  /* ── Slash commands ── */
  const runCommand = useCallback(
    async (name: string, args: string): Promise<boolean> => {
      const command = COMMANDS.find((c) => c.name === name);
      if (!command) {
        say(`No conozco /${name}. Escribe /help para ver los comandos.`, 'warning');
        return true;
      }
      if (command.route) {
        if (command.legacy) window.location.href = command.route.includes('?') ? command.route : `${command.route}?shell=legacy`;
        else navigate(command.route);
        return true;
      }
      switch (name) {
        case 'help':
          say(COMMANDS.map((c) => `${c.usage} — ${c.help}`).join('\n'));
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
          say(Object.keys(next).length ? 'Ajustes de generación aplicados a este chat.' : 'Ajustes de generación retirados.');
          return true;
        }
        case 'remember': {
          if (!workspace) {
            say('Para guardar una regla hace falta una carpeta de trabajo.', 'warning');
            return true;
          }
          try {
            const result = await rememberRule(workspace, args);
            say(result.duplicate ? 'Esa regla ya estaba en las instrucciones del proyecto.' : `Regla guardada en ${result.path ?? 'las instrucciones del proyecto'}.`);
          } catch (error) {
            say(`No he podido guardar la regla: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'compact': {
          if (!sessionId) return true;
          try {
            const result = await compactSession(sessionId);
            say(result.compacted ? 'Historial compactado.' : result.detail ?? 'No había bastante que compactar.');
            const refreshed = await turnsFromHistory(sessionId);
            setTurns(refreshed.turns);
          } catch (error) {
            say(`No he podido compactar: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'truncate': {
          const keep = Number.parseInt(args, 10);
          if (!sessionId || !keep || keep < 1) {
            say('Uso: /truncate N — conserva los N primeros mensajes y borra el resto.', 'warning');
            return true;
          }
          try {
            await truncateSession(sessionId, keep, 'truncate');
            const refreshed = await turnsFromHistory(sessionId);
            setTurns(refreshed.turns);
            say(`Conservados los ${keep} primeros mensajes. /versions recupera los borrados.`);
          } catch (error) {
            say(`No he podido truncar: ${(error as Error).message}`, 'danger');
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
                    .map((v) => `${v.id}  ·  ${relativeTime(v.createdAt) || v.createdAt}  ·  ${v.reason || 'edición'}  ·  ${v.removed} mensajes\n   /restore ${v.id}`)
                    .join('\n')
                : 'Todavía no hay versiones: se guarda una cada vez que una edición o un regenerar quita mensajes.',
            );
          } catch (error) {
            say(`No he podido leer las versiones: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'restore': {
          if (!sessionId || !args) {
            say('Uso: /restore ID (los ID salen con /versions).', 'warning');
            return true;
          }
          try {
            await restoreVersion(sessionId, args.trim());
            const refreshed = await turnsFromHistory(sessionId);
            setTurns(refreshed.turns);
            say('Versión restaurada.');
          } catch (error) {
            say(`No he podido restaurar: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'checkpoints': {
          if (!workspace) {
            say('Los puntos de control van con la carpeta de trabajo: elige una primero.', 'warning');
            return true;
          }
          try {
            const list = await listCheckpoints(workspace);
            say(
              list.length
                ? list.map((c) => `${c.sha.slice(0, 10)}  ·  ${c.createdAt ? relativeTime(c.createdAt) || c.createdAt : ''}  ·  ${c.reason ?? ''}`).join('\n') +
                    '\n\nPara volver a uno, usa «Volver a antes de este turno» en el resumen del turno.'
                : 'No hay puntos de control en esta carpeta todavía.',
            );
          } catch (error) {
            say(`No he podido leer los puntos de control: ${(error as Error).message}`, 'danger');
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
            say('Uso: /rename nombre nuevo', 'warning');
            return true;
          }
          try {
            await renameSession(sessionId, args);
            setTitle(args);
            refreshSessions();
          } catch (error) {
            say(`No he podido renombrar: ${(error as Error).message}`, 'danger');
          }
          return true;
        }
        case 'stats': {
          const list = turns ?? [];
          const out = list.filter((t) => t.role === 'assistant' && t.metrics);
          const tokens = out.reduce((n, t) => n + (t.metrics?.outputTokens ?? 0), 0);
          const seconds = out.reduce((n, t) => n + (t.metrics?.responseTime ?? 0), 0);
          say(`${list.length} mensajes · ${tokens} tokens generados · ${seconds.toFixed(1)} s de modelo · ${current?.model ?? route?.model ?? 'sin modelo'}`);
          return true;
        }
        default:
          return false;
      }
    },
    [say, navigate, gen, workspace, sessionId, turnsFromHistory, refreshSessions, turns, current, route],
  );

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
        say(`No he podido preparar la regeneración: ${(error as Error).message}`, 'danger');
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
        say(`No he podido guardar la edición: ${(error as Error).message}`, 'danger');
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
        say(`No he podido borrar el mensaje: ${(error as Error).message}`, 'danger');
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

  const pending = turns?.length ? Boolean(turns[turns.length - 1].ask) : false;
  const isEmpty = !sessionId && turns !== null && turns.length === 0;

  return (
    <div className="fs-studio" data-testid="studio" data-drawer={drawerOpen || undefined}>
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
      {drawerOpen && <button type="button" className="fs-studio__scrim" aria-label="Cerrar la lista de conversaciones" onClick={() => setDrawerOpen(false)} />}

      <section className="fs-studio__stage">
        <header className="fs-studio__head">
          <button type="button" className="fs-studio__chip fs-studio__drawer-btn" aria-expanded={drawerOpen} onClick={() => setDrawerOpen((v) => !v)} data-testid="studio-drawer">
            <MessageSquare size={13} aria-hidden="true" />
            <span>Conversaciones</span>
          </button>
          <h1 className="fs-studio__title" title={title || undefined}>
            {sessionId ? current?.name || title || 'Conversación' : 'Nueva conversación'}
          </h1>
          <div className="fs-studio__head-actions">
            {sessionId && (
              <IconButton
                icon={ExternalLink}
                label="Abrir en la interfaz anterior"
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
              <Skeleton label="Cargando la conversación" count={4} height="64px" />
            </div>
          )}
          {loadError && (
            <p className="fs-notice" data-tone="danger">
              {loadError}
            </p>
          )}

          {isEmpty && (
            <div className="fs-studio__hero fs-spot" onMouseMove={spotlight}>
              <span className="fs-watermark" aria-hidden="true">
                <BrandMark size={320} />
              </span>
              <p className="fs-studio__kicker">Studio</p>
              <h2 className="fs-home__title fs-studio__hero-title">
                ¿Qué <em>hacemos</em> hoy?
              </h2>
              <p className="fs-prose">
                Escribe abajo. En modo agente usa herramientas y te enseña cada paso en el carril; en modo chat solo conversa.
                <br />
                <code>@fichero</code> menciona un fichero del workspace, <code>#regla</code> guarda una instrucción permanente,{' '}
                <code>/comando</code> hace el resto.
              </p>
              <div className="fs-studio__suggestions">
                {SUGGESTIONS.map((text, i) => (
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
            />
          )}
        </div>

        {notice && (
          <div className="fs-notice fs-studio__notice" data-tone={notice.tone} role="status" data-testid="studio-notice">
            <pre className="fs-studio__notice-text">{notice.text}</pre>
            <IconButton icon={X} label="Cerrar aviso" size="sm" onClick={() => setNotice(null)} />
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
          modelPicker={<ModelPicker routes={routes} current={route} onPick={(r) => setRouteId(r.id)} openSignal={modelSignal} />}
          textareaRef={textareaRef}
        />
      </section>

      {wsOpen && (
        <Suspense fallback={null}>
          <WorkspaceDialog open initial={workspace} onClose={() => setWsOpen(false)} onPick={setWorkspace} />
        </Suspense>
      )}
    </div>
  );
}
