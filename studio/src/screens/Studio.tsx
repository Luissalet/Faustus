import {
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  ExternalLink,
  Globe,
  ListTodo,
  MessageSquare,
  Plus,
  Search,
  Square,
  Terminal,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import { Link, useSearchParams } from 'react-router';
import { Button, IconButton, Skeleton, type RunStatus } from '../components';
import {
  createSession,
  listModels,
  listSessions,
  loadHistory,
  metricsFrom,
  sendTurn,
  stopChat,
  type AskUser,
  type ChatEvent,
  type ChatSession,
  type ModelRoute,
  type TurnMetrics,
  type WebSource,
} from '../adapters/chat';
import { relativeTime } from '../adapters/home';
import { BrandMark } from '../shell/BrandMark';
import { useSpotlight } from '../shell/useSpotlight';
import { ModelPicker } from './ModelPicker';
import { Rich } from './rich';
import './projects.css';
import './home.css';
import './studio.css';

/* ── Model ── */

interface Step {
  id: string;
  tool: string;
  label: string;
  state: RunStatus;
  meta?: string;
  command?: string;
  output?: string;
  round: number;
}

interface Turn {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  thinking: string;
  steps: Step[];
  rounds: number;
  metrics?: TurnMetrics;
  sources: WebSource[];
  images: string[];
  ask?: AskUser;
  note?: string;
  error?: string;
  streaming: boolean;
}

type Mode = 'chat' | 'agent';

interface Knobs {
  mode: Mode;
  web: boolean;
  bash: boolean;
  plan: boolean;
}

const ROUTE_KEY = 'faustus_studio_route';
const KNOBS_KEY = 'faustus_studio_knobs';

let counter = 0;
const uid = (prefix: string) => `${prefix}-${Date.now().toString(36)}-${(counter++).toString(36)}`;

function blankTurn(role: Turn['role'], text = ''): Turn {
  return {
    id: uid(role),
    role,
    text,
    thinking: '',
    steps: [],
    rounds: 1,
    sources: [],
    images: [],
    streaming: role === 'assistant',
  };
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

/** A tool name the model uses → the words a person reads on the rail. */
const TOOL_WORDS: Record<string, string> = {
  bash: 'Terminal',
  python: 'Python',
  read_file: 'Leer',
  write_file: 'Escribir',
  edit_file: 'Editar',
  list_files: 'Listar',
  search: 'Buscar',
  web_search: 'Buscar en la web',
  fetch_url: 'Abrir URL',
  browser: 'Navegador',
  create_document: 'Crear documento',
  update_document: 'Actualizar documento',
  generate_image: 'Generar imagen',
  delegate_agents: 'Delegar',
};

function stepLabel(tool: string, command: string): string {
  const word = TOOL_WORDS[tool] ?? tool.replace(/_/g, ' ');
  const brief = command.trim().split('\n')[0].slice(0, 96);
  return brief ? `${word} · ${brief}` : word;
}

function formatMetrics(m: TurnMetrics): string {
  const parts: string[] = [];
  if (m.model) parts.push(m.model);
  if (m.outputTokens !== undefined) parts.push(`${m.outputTokens} tok`);
  if (m.tokensPerSecond !== undefined) parts.push(`${m.tokensPerSecond.toFixed(1)} tok/s`);
  if (m.responseTime !== undefined) parts.push(`${m.responseTime.toFixed(1)} s`);
  if (m.contextPercent !== undefined) parts.push(`contexto ${Math.round(m.contextPercent)}%`);
  return parts.join(' · ');
}

function lastRunning(steps: Step[], tool: string): number {
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].state === 'running' && steps[i].tool === tool) return i;
  }
  return -1;
}

/** Applies one stream event to the assistant turn at the end of the list. */
function apply(turn: Turn, event: ChatEvent): Turn {
  switch (event.type) {
    case 'delta':
      return event.thinking
        ? { ...turn, thinking: turn.thinking + event.text }
        : { ...turn, text: turn.text + event.text };
    case 'tool_start': {
      // After an approval the server replays the same tool's start: the
      // step that was waiting becomes the one that runs, not a twin.
      const held = turn.steps.findIndex((s) => s.state === 'waiting' && s.tool === event.tool);
      if (held !== -1) {
        const steps = turn.steps.slice();
        steps[held] = { ...steps[held], state: 'running', meta: undefined };
        return { ...turn, steps };
      }
      return {
        ...turn,
        rounds: Math.max(turn.rounds, event.round),
        steps: [
          ...turn.steps,
          {
            id: uid('step'),
            tool: event.tool,
            label: stepLabel(event.tool, event.command),
            state: 'running',
            command: event.fullCommand ?? event.command,
            round: event.round,
          },
        ],
      };
    }
    case 'tool_progress': {
      const index = lastRunning(turn.steps, event.tool);
      if (index === -1) return turn;
      const steps = turn.steps.slice();
      steps[index] = { ...steps[index], meta: event.message.slice(0, 60) };
      return { ...turn, steps };
    }
    case 'tool_output': {
      const index = lastRunning(turn.steps, event.tool);
      const finished: Step = {
        id: index === -1 ? uid('step') : turn.steps[index].id,
        tool: event.tool,
        label: index === -1 ? stepLabel(event.tool, event.command) : turn.steps[index].label,
        state: event.exitCode === null || event.exitCode === 0 ? 'succeeded' : 'failed',
        meta: event.exitCode !== null && event.exitCode !== 0 ? `exit ${event.exitCode}` : undefined,
        command: index === -1 ? event.command : turn.steps[index].command,
        output: event.output,
        round: index === -1 ? turn.rounds : turn.steps[index].round,
      };
      const steps = turn.steps.slice();
      if (index === -1) steps.push(finished);
      else steps[index] = finished;
      return { ...turn, steps };
    }
    case 'round':
      return { ...turn, rounds: Math.max(turn.rounds, event.round) };
    case 'ask_user':
      return {
        ...turn,
        ask: event.ask,
        steps: turn.steps.map((s) => (s.state === 'running' ? { ...s, state: 'waiting' } : s)),
      };
    case 'metrics':
      return { ...turn, metrics: { ...turn.metrics, ...event.metrics } };
    case 'sources':
      return { ...turn, sources: event.sources };
    case 'image':
      return { ...turn, images: [...turn.images, event.url] };
    case 'fallback':
      return {
        ...turn,
        note: `${event.selected || 'El modelo elegido'} no ha respondido; ha contestado ${event.answeredBy}.`,
      };
    case 'terminal':
      return event.failed ? { ...turn, error: event.message ?? 'El modelo ha fallado.' } : turn;
    case 'error':
      return { ...turn, error: event.message };
    case 'done':
      return {
        ...turn,
        streaming: false,
        steps: turn.steps.map((s) => (s.state === 'running' ? { ...s, state: 'cancelled' } : s)),
      };
  }
}

/* ── Pieces ── */

function ToolRail({ steps, live }: { steps: Step[]; live: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const leadingDone = steps.findIndex((s) => s.state !== 'succeeded');
  const doneCount = leadingDone === -1 ? steps.length : leadingDone;
  const collapse = !expanded && !live && doneCount > 3;
  const visible = collapse ? steps.slice(doneCount) : steps;

  return (
    <div className="fs-trace fs-studio__trace" data-testid="studio-trace">
      {collapse && (
        <button
          type="button"
          className="fs-trace__collapsed"
          onClick={() => setExpanded(true)}
          aria-expanded={false}
          data-testid="trace-expand"
        >
          <span aria-hidden="true" />
          <span>
            <ChevronDown size={13} aria-hidden="true" /> {doneCount} pasos completados
          </span>
        </button>
      )}
      {visible.map((step) =>
        step.output || step.command ? (
          <details key={step.id} className="fs-trace__step fs-studio__step" data-state={step.state}>
            <summary>
              <span className="fs-trace__node" aria-hidden="true" />
              <span className="fs-trace__label">{step.label}</span>
              {step.meta && <span className="fs-trace__meta">{step.meta}</span>}
            </summary>
            {step.command && step.command !== step.label && (
              <pre className="fs-studio__cmd">{step.command}</pre>
            )}
            {step.output && <pre className="fs-studio__out">{step.output.slice(0, 6000)}</pre>}
          </details>
        ) : (
          <div key={step.id} className="fs-trace__step" data-state={step.state}>
            <span className="fs-trace__node" aria-hidden="true" />
            <span className="fs-trace__label">{step.label}</span>
            {step.meta && <span className="fs-trace__meta">{step.meta}</span>}
          </div>
        ),
      )}
    </div>
  );
}

function AskCard({
  ask,
  busy,
  onApproval,
  onAnswer,
}: {
  ask: AskUser;
  busy: boolean;
  onApproval: (decision: 'approve' | 'approve_task' | 'deny') => void;
  onAnswer: (text: string) => void;
}) {
  if (ask.kind === 'tool_approval') {
    return (
      <div className="fs-studio__ask" data-testid="studio-approval">
        <p className="fs-studio__ask-title">Necesita tu permiso</p>
        {ask.question && <p className="fs-prose">{ask.question}</p>}
        <div className="fs-studio__ask-actions">
          <Button variant="primary" icon={Check} label="Aprobar" disabled={busy} onClick={() => onApproval('approve')} />
          <Button label="Aprobar toda la tarea" disabled={busy} onClick={() => onApproval('approve_task')} />
          <Button variant="danger" icon={X} label="Denegar" disabled={busy} onClick={() => onApproval('deny')} />
        </div>
      </div>
    );
  }
  return (
    <div className="fs-studio__ask" data-testid="studio-question">
      <p className="fs-studio__ask-title">Te pregunta</p>
      <p className="fs-prose">{ask.question}</p>
      {ask.options.length > 0 && (
        <div className="fs-studio__ask-actions">
          {ask.options.map((option) => (
            <Button key={option} label={option} size="sm" disabled={busy} onClick={() => onAnswer(option)} />
          ))}
        </div>
      )}
    </div>
  );
}

function AssistantTurn({
  turn,
  busy,
  onApproval,
  onAnswer,
}: {
  turn: Turn;
  busy: boolean;
  onApproval: (decision: 'approve' | 'approve_task' | 'deny') => void;
  onAnswer: (text: string) => void;
}) {
  const waiting = turn.streaming && !turn.text && turn.steps.length === 0;
  return (
    <article className="fs-turn fs-turn--assistant" data-streaming={turn.streaming || undefined} data-testid="turn-assistant">
      <span className="fs-turn__node" aria-hidden="true" />
      <div className="fs-turn__body">
        {turn.thinking && (
          <details className="fs-studio__thinking">
            <summary>
              Razonamiento {turn.streaming && !turn.text ? <span className="fs-studio__pulse" /> : null}
            </summary>
            <p className="fs-prose">{turn.thinking}</p>
          </details>
        )}
        {turn.steps.length > 0 && <ToolRail steps={turn.steps} live={turn.streaming} />}
        {waiting && !turn.thinking && (
          <p className="fs-studio__waiting" aria-live="polite">
            <span className="fs-studio__pulse" /> Pensando
          </p>
        )}
        {turn.text && <Rich text={turn.text} />}
        {turn.streaming && turn.text && <span className="fs-studio__cursor" aria-hidden="true" />}
        {turn.images.map((url) => (
          <img key={url} className="fs-studio__image" src={url} alt="Imagen generada" loading="lazy" />
        ))}
        {turn.sources.length > 0 && (
          <p className="fs-studio__sources">
            {turn.sources.slice(0, 6).map((s) => (
              <a key={s.url} className="fs-link" href={s.url} target="_blank" rel="noreferrer">
                {s.title.slice(0, 48)}
              </a>
            ))}
          </p>
        )}
        {turn.ask && <AskCard ask={turn.ask} busy={busy} onApproval={onApproval} onAnswer={onAnswer} />}
        {turn.note && <p className="fs-notice" data-tone="warning">{turn.note}</p>}
        {turn.error && <p className="fs-notice" data-tone="danger">{turn.error}</p>}
        {turn.metrics && !turn.streaming && (
          <p className="fs-turn__metrics">{formatMetrics(turn.metrics)}</p>
        )}
      </div>
    </article>
  );
}

const SUGGESTIONS = [
  'Explícame este repositorio como si acabara de llegar al equipo',
  'Busca en la web qué ha cambiado esta semana en el tema que te diga',
  'Escribe un script que ordene mis descargas por tipo',
  'Revisa el último commit y dime qué puede romperse',
];

/**
 * Studio (UI-030/031/032).
 *
 * Where the work happens. One transcript, one composer, and the execution
 * rail threaded through every agent turn so the tools the model ran are
 * visible in the reply instead of buried behind a "show details" toggle.
 */
export function StudioScreen() {
  const [params, setParams] = useSearchParams();
  const sessionId = params.get('s');

  const [sessions, setSessions] = useState<ChatSession[] | null>(null);
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [routeId, setRouteId] = useState<string | null>(() => readJson<{ id: string | null }>(ROUTE_KEY, { id: null }).id);
  const [knobs, setKnobs] = useState<Knobs>(() =>
    readJson<Knobs>(KNOBS_KEY, { mode: 'agent', web: false, bash: true, plan: false }),
  );
  const [turns, setTurns] = useState<Turn[] | null>(sessionId ? null : []);
  const [title, setTitle] = useState('');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);

  const controllerRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const pinnedRef = useRef(true);
  const freshRef = useRef<string | null>(null);
  const spotlight = useSpotlight();

  const route = useMemo(
    () => routes.find((r) => r.id === routeId) ?? routes[0] ?? null,
    [routes, routeId],
  );
  const current = useMemo(() => sessions?.find((s) => s.id === sessionId) ?? null, [sessions, sessionId]);

  /* Sessions and models: once. */
  useEffect(() => {
    const controller = new AbortController();
    listSessions(controller.signal).then(setSessions).catch(() => setSessions([]));
    listModels(controller.signal).then(setRoutes).catch(() => setRoutes([]));
    return () => controller.abort();
  }, []);

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
    if (!sessionId) {
      setTurns([]);
      setTitle('');
      return;
    }
    setTurns(null);
    const controller = new AbortController();
    loadHistory(sessionId, controller.signal)
      .then((result) => {
        setTitle(result.name);
        setTurns(
          result.history.map((m) => {
            const turn = blankTurn(m.role, m.content);
            turn.streaming = false;
            if (m.role === 'assistant') turn.metrics = metricsFrom(m.metadata);
            return turn;
          }),
        );
        pinnedRef.current = true;
        if (result.model) {
          const match = (r: ModelRoute) => r.model === result.model;
          setRouteId((id) => (routes.find(match)?.id ?? id));
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

  const run = useCallback(
    async (
      sid: string,
      message: string,
      approval?: { id: string; decision: 'approve' | 'approve_task' | 'deny' },
    ) => {
      const controller = new AbortController();
      controllerRef.current = controller;
      setBusy(true);
      pinnedRef.current = true;
      if (approval) {
        patchLast((t) => ({
          ...t,
          ask: undefined,
          streaming: true,
          text: t.text.trim() ? `${t.text.trimEnd()}\n\n` : '',
        }));
      } else {
        setTurns((list) => [...(list ?? []), blankTurn('user', message), blankTurn('assistant')]);
      }
      try {
        for await (const event of sendTurn({
          sessionId: sid,
          message,
          mode: knobs.mode,
          planMode: knobs.mode === 'agent' && knobs.plan,
          allowBash: knobs.mode === 'agent' && knobs.bash,
          allowWebSearch: knobs.web,
          workspace: current?.folder ?? undefined,
          route,
          approval,
          signal: controller.signal,
        })) {
          patchLast((t) => apply(t, event));
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          patchLast((t) => apply(t, { type: 'error', message: (error as Error).message }));
        }
        patchLast((t) => apply(t, { type: 'done' }));
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          setBusy(false);
        }
        listSessions().then(setSessions).catch(() => undefined);
      }
    },
    [knobs, current, route, patchLast],
  );

  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || busy) return;
      setDraft('');
      let sid = sessionId;
      if (!sid) {
        try {
          sid = await createSession(message.slice(0, 60), route);
        } catch {
          setLoadError('No he podido crear la conversación. ¿Está el servidor de modelos configurado?');
          return;
        }
        setTitle(message.slice(0, 60));
        freshRef.current = sid;
        const next = new URLSearchParams(params);
        next.set('s', sid);
        setParams(next, { replace: true });
      }
      void run(sid, message);
    },
    [busy, sessionId, route, params, setParams, run],
  );

  const stop = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    if (sessionId) void stopChat(sessionId);
    patchLast((t) => apply(t, { type: 'done' }));
    setBusy(false);
  }, [sessionId, patchLast]);

  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send(draft);
    }
    if (event.key === 'Escape' && busy) stop();
  };

  const openSession = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set('s', id);
    else next.delete('s');
    setParams(next);
    setDrawerOpen(false);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const pending = turns?.length ? turns[turns.length - 1].ask : undefined;
  const isEmpty = !sessionId && turns !== null && turns.length === 0;

  const needle = filter.trim().toLowerCase();
  const visibleSessions = needle
    ? sessions?.filter((s) => `${s.name} ${s.model}`.toLowerCase().includes(needle))
    : sessions;

  const sessionList = (
    <div className="fs-studio__list" role="list">
      {!sessions && <Skeleton label="Cargando conversaciones" count={6} height="44px" />}
      {visibleSessions?.map((s, i) => (
        <Link
          key={s.id}
          to={`/studio?s=${encodeURIComponent(s.id)}`}
          role="listitem"
          className="fs-studio__session fs-enter"
          style={{ ['--i' as string]: Math.min(i, 8) }}
          aria-current={s.id === sessionId ? 'page' : undefined}
          data-testid="studio-session"
          onClick={(event) => {
            event.preventDefault();
            openSession(s.id);
          }}
        >
          <span className="fs-studio__session-name">{s.name}</span>
          <span className="fs-studio__session-meta">
            {s.mode === 'agent' && <Bot size={11} aria-label="Agente" />}
            {s.model ? s.model.split('/').pop() : 'sin modelo'}
            {s.lastMessageAt && ` · ${relativeTime(s.lastMessageAt)}`}
          </span>
        </Link>
      ))}
      {sessions && sessions.length === 0 && (
        <p className="fs-studio__hint">Todavía no hay conversaciones. La primera la empiezas abajo.</p>
      )}
      {sessions && sessions.length > 0 && visibleSessions?.length === 0 && (
        <p className="fs-studio__hint">Ninguna conversación se llama así.</p>
      )}
    </div>
  );

  const modelMenu = (
    <ModelPicker routes={routes} current={route} onPick={(r) => setRouteId(r.id)} />
  );

  return (
    <div className="fs-studio" data-testid="studio" data-drawer={drawerOpen || undefined}>
      <aside className="fs-studio__sessions" aria-label="Conversaciones">
        <div className="fs-studio__sessions-head">
          <span className="fs-panel__label" style={{ margin: 0 }}>Conversaciones</span>
          <IconButton icon={Plus} label="Nueva conversación" size="sm" onClick={() => openSession(null)} />
        </div>
        <label className="fs-search fs-studio__search">
          <Search size={14} aria-hidden="true" />
          <input
            ref={searchRef}
            type="search"
            value={filter}
            placeholder="Buscar…"
            aria-label="Buscar conversaciones"
            onChange={(event) => setFilter(event.target.value)}
            data-testid="studio-search"
          />
        </label>
        {sessionList}
      </aside>
      {drawerOpen && (
        <button
          type="button"
          className="fs-studio__scrim"
          aria-label="Cerrar la lista de conversaciones"
          onClick={() => setDrawerOpen(false)}
        />
      )}

      <section className="fs-studio__stage">
        <header className="fs-studio__head">
          <button
            type="button"
            className="fs-studio__chip fs-studio__drawer-btn"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen((v) => !v)}
            data-testid="studio-drawer"
          >
            <MessageSquare size={13} aria-hidden="true" />
            <span>Conversaciones</span>
          </button>
          <h1 className="fs-studio__title" title={title || undefined}>
            {sessionId ? title || current?.name || 'Conversación' : 'Nueva conversación'}
          </h1>
          <div className="fs-studio__head-actions">
            {modelMenu}
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
          {loadError && <p className="fs-notice" data-tone="danger">{loadError}</p>}

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
                Escribe abajo. En modo agente usa herramientas y te enseña cada paso en el carril;
                en modo chat solo conversa.
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
            <div className="fs-studio__turns">
              {turns.map((turn) =>
                turn.role === 'user' ? (
                  <article key={turn.id} className="fs-turn fs-turn--user" data-testid="turn-user">
                    <div className="fs-turn__bubble">{turn.text}</div>
                  </article>
                ) : (
                  <AssistantTurn
                    key={turn.id}
                    turn={turn}
                    busy={busy}
                    onApproval={(decision) => {
                      if (sessionId && turn.ask?.approvalId) {
                        void run(sessionId, '', { id: turn.ask.approvalId, decision });
                      }
                    }}
                    onAnswer={(text) => void send(text)}
                  />
                ),
              )}
            </div>
          )}
        </div>

        <form
          className="fs-studio__composer fs-panel"
          onSubmit={(event) => {
            event.preventDefault();
            void send(draft);
          }}
          data-testid="studio-composer"
        >
          <textarea
            ref={textareaRef}
            className="fs-studio__input"
            rows={1}
            value={draft}
            placeholder={
              pending
                ? 'Responde arriba, o escribe para seguir…'
                : knobs.mode === 'agent'
                  ? 'Dime qué quieres que haga…'
                  : 'Escribe un mensaje…'
            }
            aria-label="Mensaje"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKey}
            onInput={(event) => {
              const el = event.currentTarget;
              el.style.blockSize = 'auto';
              el.style.blockSize = `${Math.min(el.scrollHeight, 220)}px`;
            }}
            data-testid="studio-input"
          />
          <div className="fs-studio__bar">
            <div className="fs-studio__seg" role="radiogroup" aria-label="Modo">
              <span className="fs-studio__seg-thumb" data-mode={knobs.mode} aria-hidden="true" />
              <button
                type="button"
                role="radio"
                aria-checked={knobs.mode === 'chat'}
                onClick={() => setKnobs((k) => ({ ...k, mode: 'chat' }))}
                data-testid="studio-mode-chat"
              >
                <MessageSquare size={13} aria-hidden="true" /> Chat
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={knobs.mode === 'agent'}
                onClick={() => setKnobs((k) => ({ ...k, mode: 'agent' }))}
                data-testid="studio-mode-agent"
              >
                <Bot size={13} aria-hidden="true" /> Agente
              </button>
            </div>
            <div className="fs-studio__knobs">
              <button
                type="button"
                className="fs-studio__chip"
                aria-pressed={knobs.web}
                onClick={() => setKnobs((k) => ({ ...k, web: !k.web }))}
                data-testid="studio-knob-web"
              >
                <Globe size={13} aria-hidden="true" /> Web
              </button>
              {knobs.mode === 'agent' && (
                <>
                  <button
                    type="button"
                    className="fs-studio__chip"
                    aria-pressed={knobs.bash}
                    onClick={() => setKnobs((k) => ({ ...k, bash: !k.bash }))}
                    data-testid="studio-knob-bash"
                  >
                    <Terminal size={13} aria-hidden="true" /> Terminal
                  </button>
                  <button
                    type="button"
                    className="fs-studio__chip"
                    aria-pressed={knobs.plan}
                    onClick={() => setKnobs((k) => ({ ...k, plan: !k.plan }))}
                    data-testid="studio-knob-plan"
                  >
                    <ListTodo size={13} aria-hidden="true" /> Plan
                  </button>
                </>
              )}
            </div>
            <div className="fs-studio__send">
              {busy ? (
                <IconButton icon={Square} label="Parar" onClick={stop} testId="studio-stop" />
              ) : (
                <button
                  type="submit"
                  className="fs-studio__go"
                  disabled={!draft.trim()}
                  aria-label="Enviar"
                  data-testid="studio-send"
                >
                  <ArrowUp size={18} aria-hidden="true" />
                </button>
              )}
            </div>
          </div>
        </form>
      </section>
    </div>
  );
}
