import {
  Check,
  ChevronDown,
  Columns3,
  Copy,
  Dices,
  Download,
  ExternalLink,
  Eye,
  EyeOff,
  FileText,
  Maximize2,
  Minimize2,
  Plus,
  Printer,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  Square,
  Trophy,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Toast } from '../../components';
import { createSession, listModels, sendTurn, stopChat, type ModelRoute } from '../../adapters/chat';
import { deleteSession } from '../../adapters/sessions';
import {
  clearVotes,
  DEFAULT_OPTIONS,
  eligibleRoutes,
  EVAL_PROMPTS,
  formatMs,
  getExcluded,
  gradeAnswer,
  loadOptions,
  loadVotes,
  MAX_PANES,
  metricsLine,
  MODE_HELP,
  MODE_LABEL,
  probeRoutes,
  routeLabel,
  saveOptions,
  saveVote,
  scoreboard,
  searchWith,
  setExcluded,
  slotChar,
  synthesisPrompt,
  type CompareMode,
  type CompareOptions,
  type SearchHit,
} from '../../adapters/compare';
import { searchProviders, type SearchProvider } from '../../adapters/research';
import { apply, blankTurn, type Turn } from '../studio/model';
import { AskCard, type Decision } from '../studio/Transcript';
import ModelPalette from '../ModelPalette';
import { Rich } from '../rich';
import { t, tn } from '../../i18n';
import { safeExternal } from '../../lib/markdown';
import '../compare.css';

/**
 * Compare: one prompt, several panes. Each pane is its own session (so a
 * conversation can go on), streamed with `compare_mode` so memory and
 * documents stay out and the modes stay fair. Blind by default: names
 * appear after the vote.
 */

interface Pane {
  id: string;
  route: ModelRoute | null;
  provider: SearchProvider | null;
  sessionId: string | null;
  turns: Turn[];
  streaming: boolean;
  error: string;
  finishedOrder: number;
  elapsedMs: number;
  expanded: boolean;
  preview: boolean;
  grade: 'pass' | 'fail' | null;
  hits: SearchHit[];
  synth: string;
}

const uid = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;

function newPane(route: ModelRoute | null, provider: SearchProvider | null = null): Pane {
  return { id: uid(), route, provider, sessionId: null, turns: [], streaming: false, error: '', finishedOrder: 0, elapsedMs: 0, expanded: false, preview: false, grade: null, hits: [], synth: '' };
}

function htmlBlock(text: string): string | null {
  const m = text.match(/```html\s*\n([\s\S]*?)```/i);
  if (m) return m[1];
  if (/^\s*<!doctype html|^\s*<html/i.test(text)) return text;
  return null;
}

/* ── Pane ── */

function PaneView({
  pane,
  index,
  blind,
  parallel,
  revealed,
  mode,
  routes,
  onSwap,
  onRemove,
  onStop,
  onReroll,
  onToggleExpand,
  onTogglePreview,
  onApproval,
  onAnswer,
  say,
}: {
  pane: Pane;
  index: number;
  blind: boolean;
  parallel: boolean;
  revealed: boolean;
  mode: CompareMode;
  routes: ModelRoute[];
  onSwap: (route: ModelRoute) => void;
  onRemove: () => void;
  onStop: () => void;
  onReroll: () => void;
  onToggleExpand: () => void;
  onTogglePreview: () => void;
  onApproval: (decision: Decision) => void;
  onAnswer: (answer: string) => void;
  say: (m: string, tone?: 'ok' | 'warn') => void;
}) {
  const navigate = useNavigate();
  const [swapOpen, setSwapOpen] = useState(false);
  const name = mode === 'search' ? pane.provider?.label ?? t('Provider') : routeLabel(pane.route) || t('Pick a model');
  const title = blind && !revealed && mode !== 'search' ? t('Model {slot}', { slot: slotChar(index, parallel) }) : name;
  const last = pane.turns.length ? pane.turns[pane.turns.length - 1] : null;
  const html = last && last.role === 'assistant' && !last.streaming ? htmlBlock(last.text) : null;
  const copy = async () => {
    if (!last) return;
    try {
      await navigator.clipboard.writeText(mode === 'search' ? pane.synth || pane.hits.map((h) => `${h.title}\n${h.url}`).join('\n\n') : last.text);
      say(t('Copied.'));
    } catch {
      say(t('Could not copy.'), 'warn');
    }
  };
  return (
    <section className="fs-cmp__pane" data-expanded={pane.expanded || undefined} data-streaming={pane.streaming || undefined} data-testid="compare-pane" aria-label={title}>
      <header className="fs-cmp__pane-head">
        <span className="fs-cmp__slot">{slotChar(index, parallel)}</span>
        {mode === 'search' ? (
          <span className="fs-cmp__pane-title">{title}</span>
        ) : (
          <button type="button" className="fs-cmp__pane-title" onClick={() => setSwapOpen(true)} title={t('Swap the model')} disabled={pane.streaming}>
            {title} <ChevronDown size={12} aria-hidden="true" />
          </button>
        )}
        {pane.finishedOrder > 0 && (
          <span className="fs-cmp__badge" data-first={pane.finishedOrder === 1 || undefined} title={t('Finished {n}th', { n: pane.finishedOrder })}>
            #{pane.finishedOrder} · {formatMs(pane.elapsedMs)}
          </span>
        )}
        {pane.grade && (
          <span className="fs-cmp__grade" data-grade={pane.grade}>
            {pane.grade === 'pass' ? t('Correct') : t('Missed')}
          </span>
        )}
        <span className="fs-spacer" />
        <IconButton icon={pane.expanded ? Minimize2 : Maximize2} label={pane.expanded ? t('Shrink') : t('Expand')} size="sm" onClick={onToggleExpand} />
        <IconButton icon={X} label={t('Remove this pane')} size="sm" onClick={onRemove} disabled={pane.streaming} />
      </header>
      <div className="fs-cmp__pane-body">
        {mode === 'search' && (pane.hits.length > 0 || pane.streaming || pane.finishedOrder > 0) && (
          <div className="fs-cmp__hits">
            {pane.hits.length === 0 && pane.streaming && <p className="fs-cmp__waiting"><span className="fs-studio__pulse" /> {t('Searching…')}</p>}
            {pane.hits.length === 0 && !pane.streaming && !pane.error && <p className="fs-muted">{t('No results.')}</p>}
            {pane.hits.map((h) => (
              // A search hit is somebody else's URL: only http(s) becomes a
              // link, the rest stays as text you can read but not click.
              <a key={h.url} className="fs-cmp__hit" href={safeExternal(h.url) ?? undefined} target="_blank" rel="noopener noreferrer" data-dead={safeExternal(h.url) ? undefined : true}>
                <b>{h.title}</b>
                <small>{h.url}</small>
                {h.snippet && <span>{h.snippet}</span>}
              </a>
            ))}
            {pane.synth && (
              <div className="fs-cmp__synth">
                <span className="fs-cmp__role">{t('Analysis')}</span>
                <Rich text={pane.synth} />
              </div>
            )}
          </div>
        )}
        {pane.turns.map((turn) => (
          <div key={turn.id} className="fs-cmp__msg" data-role={turn.role}>
            <span className="fs-cmp__role">{turn.role === 'user' ? t('You') : t('Answer')}</span>
            {turn.role === 'user' ? (
              <p className="fs-cmp__user">{turn.text}</p>
            ) : (
              <>
                {turn.thinking && (
                  <details className="fs-cmp__thinking">
                    <summary>{t('Reasoning')}</summary>
                    <pre>{turn.thinking}</pre>
                  </details>
                )}
                {turn.steps.length > 0 && <p className="fs-cmp__steps">{tn(turn.steps.length, '{n} tool call', '{n} tool calls')}{turn.rounds > 1 ? ` · ${tn(turn.rounds, '{n} round', '{n} rounds')}` : ''}</p>}
                {turn.streaming && !turn.text && !turn.thinking && (
                  <p className="fs-cmp__waiting">
                    <span className="fs-studio__pulse" /> {!parallel && index > 0 && pane.turns.length === 1 && !pane.sessionId ? t('Waiting for {slot}…', { slot: slotChar(index - 1, parallel) }) : t('Thinking…')}
                  </p>
                )}
                {turn.text && (pane.preview && html && turn === last ? <iframe className="fs-cmp__preview" title={t('Preview')} sandbox="allow-scripts" srcDoc={html} /> : <Rich text={turn.text} />)}
                {turn.ask && turn.streaming === false && <AskCard ask={turn.ask} busy={pane.streaming} onApproval={onApproval} onAnswer={onAnswer} />}
                {turn.error && <p className="fs-notice" data-tone="danger">{turn.error}</p>}
                {!turn.streaming && (turn.metrics || pane.elapsedMs > 0) && turn === last && <p className="fs-cmp__metrics">{metricsLine(turn.metrics, pane.elapsedMs)}</p>}
              </>
            )}
          </div>
        ))}
        {pane.error && <p className="fs-notice" data-tone="danger">{pane.error}</p>}
      </div>
      <footer className="fs-cmp__pane-foot">
        {pane.streaming ? (
          <Button variant="danger" size="sm" icon={Square} label={t('Stop')} onClick={onStop} />
        ) : (
          pane.turns.length > 0 && mode !== 'search' && <IconButton icon={RotateCcw} label={t('Run again')} size="sm" onClick={onReroll} />
        )}
        {last && !pane.streaming && <IconButton icon={Copy} label={t('Copy the answer')} size="sm" onClick={() => void copy()} />}
        {html && <IconButton icon={pane.preview ? FileText : Eye} label={pane.preview ? t('Show the text') : t('Preview the HTML')} size="sm" data-on={pane.preview || undefined} onClick={onTogglePreview} />}
        <span className="fs-spacer" />
        {pane.sessionId && !pane.streaming && mode !== 'search' && (
          <Button variant="ghost" size="sm" icon={ExternalLink} label={t('Continue in Studio')} onClick={() => navigate(`/studio?s=${encodeURIComponent(pane.sessionId as string)}`)} />
        )}
      </footer>
      <ModelPalette open={swapOpen} onOpenChange={setSwapOpen} routes={routes} current={pane.route} onPick={(r) => { setSwapOpen(false); onSwap(r); }} />
    </section>
  );
}

/* ── Screen ── */

export function CompareScreen() {
  const [opts, setOpts] = useState<CompareOptions>(loadOptions);
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [providers, setProviders] = useState<SearchProvider[]>([]);
  const [panes, setPanes] = useState<Pane[]>([]);
  const [prompt, setPrompt] = useState('');
  const [expected, setExpected] = useState('');
  const [running, setRunning] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [voted, setVoted] = useState<string | null>(null);
  const [lastPrompt, setLastPrompt] = useState('');
  const [pickSlot, setPickSlot] = useState<number | null>(null);
  const [synthPick, setSynthPick] = useState(false);
  const [poolOpen, setPoolOpen] = useState(false);
  const [excluded, setExcludedState] = useState<string[]>(getExcluded);
  const [scoreOpen, setScoreOpen] = useState(false);
  const [scoreMode, setScoreMode] = useState<CompareMode | 'all'>('all');
  const [probes, setProbes] = useState<Record<string, boolean>>({});
  const [probing, setProbing] = useState(false);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const controllers = useRef<Map<string, AbortController>>(new Map());
  const finishCounter = useRef(0);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const noticeTimer = useRef<number | null>(null);
  const paneCount = panes.length;

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);

  useEffect(() => saveOptions(opts), [opts]);

  /* Models and providers; then the panes the last visit had for this mode. */
  useEffect(() => {
    const c = new AbortController();
    Promise.all([listModels(c.signal).catch(() => [] as ModelRoute[]), searchProviders(c.signal)]).then(([r, p]) => {
      setRoutes(r);
      setProviders(p);
    });
    return () => c.abort();
  }, []);

  const eligible = useMemo(() => eligibleRoutes(routes, excluded), [routes, excluded]);
  const synthRoute = useMemo(() => routes.find((r) => r.id === opts.synthRoute) ?? routes[0] ?? null, [routes, opts.synthRoute]);

  useEffect(() => {
    if (!routes.length && !providers.length) return;
    setPanes((cur) => {
      if (cur.length) return cur;
      const saved = opts.slots[opts.mode] ?? [];
      if (opts.mode === 'search') {
        const chosen = saved.map((id) => providers.find((p) => p.id === id)).filter((p): p is SearchProvider => Boolean(p));
        const list = chosen.length ? chosen : providers.filter((p) => p.available).slice(0, 2);
        return list.map((p) => newPane(null, p));
      }
      const chosen = saved.map((id) => routes.find((r) => r.id === id)).filter((r): r is ModelRoute => Boolean(r));
      if (chosen.length) return chosen.map((r) => newPane(r));
      return [newPane(eligible[0] ?? null), newPane(eligible[1] ?? null)];
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes, providers]);

  /* Remember the slots per mode. */
  useEffect(() => {
    const ids = panes.map((p) => (opts.mode === 'search' ? p.provider?.id : p.route?.id)).filter((x): x is string => Boolean(x));
    setOpts((o) => (JSON.stringify(o.slots[o.mode] ?? []) === JSON.stringify(ids) ? o : { ...o, slots: { ...o.slots, [o.mode]: ids } }));
  }, [panes, opts.mode]);

  const patch = useCallback((id: string, p: Partial<Pane> | ((pane: Pane) => Partial<Pane>)) => setPanes((cur) => cur.map((x) => (x.id === id ? { ...x, ...(typeof p === 'function' ? p(x) : p) } : x))), []);

  const setMode = (mode: CompareMode) => {
    if (running) return;
    setOpts((o) => ({ ...o, mode }));
    setPanes(() => {
      const saved = opts.slots[mode] ?? [];
      if (mode === 'search') {
        const chosen = saved.map((id) => providers.find((p) => p.id === id)).filter((p): p is SearchProvider => Boolean(p));
        return (chosen.length ? chosen : providers.filter((p) => p.available).slice(0, 2)).map((p) => newPane(null, p));
      }
      const chosen = saved.map((id) => routes.find((r) => r.id === id)).filter((r): r is ModelRoute => Boolean(r));
      return (chosen.length ? chosen : [eligible[0] ?? null, eligible[1] ?? null]).map((r) => newPane(r));
    });
    setRevealed(false);
    setVoted(null);
    setLastPrompt('');
  };

  const addPane = () => {
    if (panes.length >= MAX_PANES) return;
    if (opts.mode === 'search') {
      const used = new Set(panes.map((p) => p.provider?.id));
      const next = providers.find((p) => p.available && !used.has(p.id)) ?? providers.find((p) => !used.has(p.id)) ?? null;
      setPanes((cur) => [...cur, newPane(null, next)]);
      return;
    }
    const used = new Set(panes.map((p) => p.route?.id));
    const next = eligible.find((r) => !used.has(r.id)) ?? null;
    setPanes((cur) => [...cur, newPane(next)]);
  };

  const shuffle = () => {
    if (running || opts.mode === 'search') return;
    const pool = [...eligible].sort(() => Math.random() - 0.5);
    setPanes((cur) => cur.map((p, i) => ({ ...newPane(pool[i % pool.length] ?? p.route), id: p.id })));
    say(t('Shuffled from {n} models.', { n: pool.length }));
  };

  const probe = async () => {
    const list = panes.map((p) => p.route).filter((r): r is ModelRoute => Boolean(r));
    if (!list.length) return;
    setProbing(true);
    const out = await probeRoutes(list);
    setProbes(out);
    setProbing(false);
    const bad = list.filter((r) => out[r.id] === false);
    say(bad.length ? t('{n} of {m} did not answer the probe.', { n: bad.length, m: list.length }) : t('All {n} answered.', { n: list.length }));
  };

  /* ── Running ── */

  const ensureSession = async (pane: Pane): Promise<string> => {
    if (pane.sessionId) return pane.sessionId;
    const name = t('Compare: {model}', { model: routeLabel(pane.route) });
    const id = await createSession(name, pane.route);
    patch(pane.id, { sessionId: id });
    return id;
  };

  const runPane = async (pane: Pane, message: string, approval?: { id: string; decision: Decision }) => {
    if (!pane.route && opts.mode !== 'search') {
      patch(pane.id, { error: t('Pick a model for this pane.') });
      return;
    }
    const c = new AbortController();
    controllers.current.set(pane.id, c);
    const t0 = performance.now();
    const noLimit = opts.mode === 'research' || opts.timeout === 0;
    const timeout = noLimit ? 0 : opts.mode === 'agent' ? Math.max(opts.timeout, 300) : opts.timeout;
    const timer = timeout ? window.setTimeout(() => c.abort(), timeout * 1000) : null;
    patch(pane.id, (p) => ({
      streaming: true,
      error: '',
      grade: null,
      finishedOrder: 0,
      turns: approval ? p.turns.map((x, i) => (i === p.turns.length - 1 ? { ...x, ask: undefined, streaming: true } : x)) : [...p.turns, blankTurn('user', message), blankTurn('assistant')],
    }));
    try {
      const sessionId = await ensureSession(pane);
      for await (const event of sendTurn({
        sessionId,
        message,
        mode: opts.mode === 'agent' ? 'agent' : 'chat',
        allowBash: opts.mode === 'agent',
        allowWebSearch: opts.mode === 'agent',
        useRag: false,
        useResearch: opts.mode === 'research',
        route: pane.route,
        approval,
        compare: true,
        signal: c.signal,
      })) {
        if (event.type === 'done') break;
        patch(pane.id, (p) => ({ turns: p.turns.map((x, i) => (i === p.turns.length - 1 ? apply(x, event) : x)) }));
      }
    } catch (err) {
      const aborted = c.signal.aborted;
      patch(pane.id, (p) => ({ error: aborted ? (timeout && performance.now() - t0 >= timeout * 1000 ? t('Stopped after {s} s (timeout).', { s: timeout }) : t('Stopped.')) : (err as Error).message || t('The model did not answer.'), turns: p.turns.map((x, i) => (i === p.turns.length - 1 ? { ...x, streaming: false } : x)) }));
    } finally {
      if (timer) window.clearTimeout(timer);
      controllers.current.delete(pane.id);
      finishCounter.current += 1;
      const order = finishCounter.current;
      const elapsed = performance.now() - t0;
      patch(pane.id, (p) => {
        const last = p.turns[p.turns.length - 1];
        const stillAsking = Boolean(last?.ask);
        return {
          streaming: false,
          elapsedMs: elapsed,
          finishedOrder: stillAsking ? 0 : order,
          grade: expected && last ? gradeAnswer(last.text, expected) : null,
          turns: p.turns.map((x, i) => (i === p.turns.length - 1 ? { ...x, streaming: false } : x)),
        };
      });
    }
  };

  const runSearchPane = async (pane: Pane, message: string) => {
    if (!pane.provider) return;
    const c = new AbortController();
    controllers.current.set(pane.id, c);
    const t0 = performance.now();
    patch(pane.id, { streaming: true, error: '', hits: [], synth: '', finishedOrder: 0, turns: [] });
    let tempSession: string | null = null;
    try {
      const r = await searchWith(pane.provider.id, message, c.signal);
      if (r.error) throw new Error(r.error);
      patch(pane.id, { hits: r.hits });
      if (synthRoute && r.hits.length) {
        tempSession = await createSession(t('Compare: analysis'), synthRoute);
        let text = '';
        for await (const event of sendTurn({ sessionId: tempSession, message: synthesisPrompt(message, r.hits), mode: 'chat', route: synthRoute, compare: true, signal: c.signal })) {
          if (event.type === 'delta' && !event.thinking) {
            text += event.text;
            patch(pane.id, { synth: text });
          } else if (event.type === 'error') throw new Error(event.message);
          else if (event.type === 'done') break;
        }
      }
    } catch (err) {
      patch(pane.id, { error: c.signal.aborted ? t('Stopped.') : (err as Error).message || t('The search failed.') });
    } finally {
      if (tempSession) void deleteSession(tempSession).catch(() => undefined);
      controllers.current.delete(pane.id);
      finishCounter.current += 1;
      patch(pane.id, { streaming: false, elapsedMs: performance.now() - t0, finishedOrder: finishCounter.current });
    }
  };

  const send = async () => {
    const message = prompt.trim();
    if (!message || running || !panes.length) return;
    setPrompt('');
    setLastPrompt(message);
    setRunning(true);
    setRevealed(false);
    setVoted(null);
    finishCounter.current = 0;
    const snapshot = panes;
    const run = (p: Pane) => (opts.mode === 'search' ? runSearchPane(p, message) : runPane(p, message));
    if (opts.parallel) await Promise.all(snapshot.map(run));
    else for (const p of snapshot) await run(p);
    setRunning(false);
    promptRef.current?.focus();
  };

  const stopAll = () => {
    for (const [id, c] of controllers.current) {
      c.abort();
      const pane = panes.find((p) => p.id === id);
      if (pane?.sessionId) void stopChat(pane.sessionId);
    }
  };

  const stopPane = (pane: Pane) => {
    controllers.current.get(pane.id)?.abort();
    if (pane.sessionId) void stopChat(pane.sessionId);
  };

  const reroll = (pane: Pane) => {
    if (!lastPrompt || running) return;
    setRunning(true);
    patch(pane.id, (p) => ({ turns: p.turns.slice(0, Math.max(0, p.turns.length - 2)) }));
    void runPane({ ...pane, turns: pane.turns.slice(0, Math.max(0, pane.turns.length - 2)) }, lastPrompt).then(() => setRunning(false));
  };

  const removePane = async (pane: Pane) => {
    setPanes((cur) => cur.filter((p) => p.id !== pane.id));
    if (pane.sessionId && !opts.keepSessions) await deleteSession(pane.sessionId).catch(() => undefined);
  };

  const swap = (pane: Pane, route: ModelRoute) => {
    if (pane.sessionId && !opts.keepSessions) void deleteSession(pane.sessionId).catch(() => undefined);
    patch(pane.id, { ...newPane(route), id: pane.id });
  };

  const reset = async () => {
    stopAll();
    const ids = panes.map((p) => p.sessionId).filter((x): x is string => Boolean(x));
    if (!opts.keepSessions) for (const id of ids) void deleteSession(id).catch(() => undefined);
    else if (ids.length) say(tn(ids.length, '{n} chat kept in Studio.', '{n} chats kept in Studio.'));
    setPanes((cur) => cur.map((p) => ({ ...newPane(p.route, p.provider), id: p.id })));
    setRevealed(false);
    setVoted(null);
    setLastPrompt('');
    setExpected('');
    finishCounter.current = 0;
  };

  useEffect(() => {
    const map = controllers.current;
    return () => {
      for (const c of map.values()) c.abort();
    };
  }, []);

  /* ── Vote ── */

  const names = panes.map((p, i) => (opts.mode === 'search' ? p.provider?.label ?? `#${i + 1}` : routeLabel(p.route) || `#${i + 1}`));
  const allDone = !running && lastPrompt !== '' && panes.length >= 2 && panes.every((p) => !p.streaming && (p.turns.length > 0 || p.hits.length > 0 || p.error));
  const vote = (winnerIndex: number) => {
    const winner = winnerIndex < 0 ? 'tie' : names[winnerIndex];
    saveVote({ models: names, winner, prompt: lastPrompt, blind: opts.blind, mode: opts.mode, timestamp: Date.now() });
    setVoted(winner);
    setRevealed(true);
    say(winnerIndex < 0 ? t('Tie recorded.') : t('{name} wins.', { name: winner }));
  };

  /* ── Export ── */

  const markdown = () => {
    const lines = [`# ${t('Comparison')}: ${lastPrompt}`, '', `_${MODE_LABEL[opts.mode]} · ${new Date().toLocaleString()}${voted ? ` · ${t('winner')}: ${voted}` : ''}_`, ''];
    panes.forEach((p, i) => {
      const last = p.turns[p.turns.length - 1];
      lines.push(`## ${slotChar(i, opts.parallel)} — ${names[i]}`, '');
      if (opts.mode === 'search') {
        p.hits.forEach((h, j) => lines.push(`${j + 1}. [${h.title}](${h.url}) — ${h.snippet}`));
        if (p.synth) lines.push('', `**${t('Analysis')}**`, '', p.synth);
      } else if (last) lines.push(last.text, '', `_${metricsLine(last.metrics, p.elapsedMs)}${p.grade ? ` · ${p.grade}` : ''}_`);
      if (p.error) lines.push(`> ${p.error}`);
      lines.push('');
    });
    return lines.join('\n');
  };
  const exportCopy = async () => {
    try {
      await navigator.clipboard.writeText(markdown());
      say(t('Comparison copied as Markdown.'));
    } catch {
      say(t('Could not copy.'), 'warn');
    }
  };
  const exportDownload = () => {
    const blob = new Blob([markdown()], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `compare-${new Date().toISOString().slice(0, 10)}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const evalPrompts = EVAL_PROMPTS[opts.mode];
  const evalGroups = useMemo(() => {
    const groups = new Map<string, typeof evalPrompts>();
    for (const e of evalPrompts) groups.set(e.sub, [...(groups.get(e.sub) ?? []), e]);
    return [...groups.entries()];
  }, [evalPrompts]);

  const votes = useMemo(() => (scoreOpen ? loadVotes() : []), [scoreOpen, voted]);
  const rows = useMemo(() => scoreboard(votes, scoreMode), [votes, scoreMode]);

  const noModels = routes.length === 0 && providers.length === 0;
  /* Blind means blind: once a race has started the chips hide the names too. */
  const hideNames = opts.blind && opts.mode !== 'search' && lastPrompt !== '' && !revealed;

  return (
    <div className="fs-screen fs-cmp" data-testid="compare" data-running={running || undefined} data-panes={paneCount}>
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Compare')}</h1>
          <p className="fs-prose fs-cmp__lede">{t(MODE_HELP[opts.mode])}</p>
        </div>
        <div className="fs-inline">
          <Button variant="ghost" size="sm" icon={Trophy} label={t('Scoreboard')} onClick={() => setScoreOpen(true)} testId="compare-scoreboard" />
          {lastPrompt && (
            <Menu
              trigger={<Button variant="ghost" size="sm" icon={Download} label={t('Export')} />}
              align="end"
              items={[
                { label: t('Copy as Markdown'), icon: Copy, onSelect: () => void exportCopy() },
                { label: t('Download .md'), icon: Download, onSelect: exportDownload },
                { label: t('Print'), icon: Printer, onSelect: () => window.print() },
              ]}
            />
          )}
          {(lastPrompt || panes.some((p) => p.turns.length)) && <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Reset')} onClick={() => void reset()} />}
        </div>
      </header>

      <section className="fs-cmp__setup" aria-label={t('Set up the comparison')}>
        <div className="fs-seg" role="radiogroup" aria-label={t('Mode')}>
          {(['chat', 'agent', 'search', 'research'] as CompareMode[]).map((m) => (
            <button key={m} type="button" role="radio" aria-checked={opts.mode === m} onClick={() => setMode(m)} disabled={running} data-testid={`compare-mode-${m}`}>
              {t(MODE_LABEL[m])}
            </button>
          ))}
        </div>
        <div className="fs-cmp__slots">
          {panes.map((p, i) => (
            <span key={p.id} className="fs-cmp__chip" data-probe={p.route && probes[p.route.id] !== undefined ? (probes[p.route.id] ? 'ok' : 'fail') : undefined}>
              <b>{slotChar(i, opts.parallel)}</b>
              {opts.mode === 'search' ? (
                <select className="fs-cmp__chip-select" value={p.provider?.id ?? ''} onChange={(e) => patch(p.id, { provider: providers.find((x) => x.id === e.target.value) ?? null })} disabled={running} aria-label={t('Provider for slot {slot}', { slot: slotChar(i, opts.parallel) })}>
                  {providers.map((x) => (
                    <option key={x.id} value={x.id} disabled={!x.available}>
                      {x.label}
                      {x.available ? '' : ` · ${t('not set up')}`}
                    </option>
                  ))}
                </select>
              ) : (
                <button type="button" className="fs-cmp__chip-pick" onClick={() => setPickSlot(i)} disabled={running} data-testid="compare-slot">
                  {hideNames ? t('Model {slot}', { slot: slotChar(i, opts.parallel) }) : routeLabel(p.route) || t('Pick a model')} <ChevronDown size={11} aria-hidden="true" />
                </button>
              )}
              <IconButton icon={X} label={t('Remove slot {slot}', { slot: slotChar(i, opts.parallel) })} size="sm" onClick={() => void removePane(p)} disabled={running || panes.length <= 1} />
            </span>
          ))}
          {panes.length < MAX_PANES && <Button variant="ghost" size="sm" icon={Plus} label={t('Add')} onClick={addPane} disabled={running} testId="compare-add" />}
          {opts.mode !== 'search' && <IconButton icon={Dices} label={t('Shuffle the models')} size="sm" onClick={shuffle} disabled={running} />}
          {opts.mode !== 'search' && <Button variant="ghost" size="sm" label={probing ? t('Probing…') : t('Probe')} onClick={() => void probe()} disabled={running || probing} title={t('A one-token request to each model, so a dead endpoint shows before the race')} />}
          {opts.mode !== 'search' && <IconButton icon={Settings2} label={t('Model pool')} size="sm" onClick={() => setPoolOpen(true)} />}
        </div>
        <div className="fs-cmp__options">
          {opts.mode === 'search' && (
            <button type="button" className="fs-chip" onClick={() => setSynthPick(true)} title={t('The model that summarises each provider’s results')}>
              {t('Analysis by {model}', { model: routeLabel(synthRoute) || t('default') })}
            </button>
          )}
          <button type="button" className="fs-chip" data-on={opts.blind || undefined} onClick={() => setOpts((o) => ({ ...o, blind: !o.blind }))} disabled={running} title={t('Names stay hidden until you vote')}>
            {opts.blind ? <EyeOff size={12} aria-hidden="true" /> : <Eye size={12} aria-hidden="true" />} {t('Blind')}
          </button>
          <button type="button" className="fs-chip" data-on={!opts.parallel || undefined} onClick={() => setOpts((o) => ({ ...o, parallel: !o.parallel }))} disabled={running} title={t('One pane at a time, so a single GPU is not shared')}>
            <Columns3 size={12} aria-hidden="true" /> {opts.parallel ? t('All at once') : t('One after another')}
          </button>
          <label className="fs-cmp__timeout">
            <span>{t('Timeout')}</span>
            <select className="fs-field" value={opts.timeout} onChange={(e) => setOpts((o) => ({ ...o, timeout: Number(e.target.value) }))} disabled={running}>
              <option value={60}>60 s</option>
              <option value={120}>2 min</option>
              <option value={300}>5 min</option>
              <option value={600}>10 min</option>
              <option value={0}>{t('None')}</option>
            </select>
          </label>
          <label className="fs-switch">
            <input type="checkbox" checked={opts.keepSessions} onChange={(e) => setOpts((o) => ({ ...o, keepSessions: e.target.checked }))} />
            <span>{t('Keep the chats')}</span>
          </label>
        </div>
      </section>

      {noModels ? (
        <EmptyState icon={Columns3} title={t('No models to compare')} body={t('Add an endpoint in Settings → Models and the models appear here.')} />
      ) : (
        <>
          <section className="fs-cmp__prompt" aria-label={t('Prompt')}>
            <textarea
              ref={promptRef}
              className="fs-cmp__textarea"
              rows={2}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={lastPrompt ? t('Follow up in every pane…') : t('The same prompt for every pane…')}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              data-testid="compare-prompt"
            />
            <div className="fs-cmp__prompt-row">
              <Menu
                trigger={<Button variant="ghost" size="sm" icon={FileText} label={t('Test prompts')} />}
                items={evalGroups.flatMap(([sub, list], gi) => [
                  ...(gi > 0 ? [null] : []),
                  ...list.map((e) => ({
                    label: `${sub} · ${e.label}${e.answer ? ' ✓' : ''}`,
                    onSelect: () => {
                      setPrompt(e.prompt);
                      setExpected(e.answer ?? '');
                      promptRef.current?.focus();
                    },
                  })),
                ])}
              />
              {expected && (
                <span className="fs-chip" data-on title={t('The answer the prompt expects; panes get graded against it')}>
                  <Check size={11} aria-hidden="true" /> {t('Expected: {answer}', { answer: expected })}
                  <IconButton icon={X} label={t('Forget the expected answer')} size="sm" onClick={() => setExpected('')} />
                </span>
              )}
              <span className="fs-spacer" />
              {running ? (
                <Button variant="danger" size="sm" icon={Square} label={t('Stop all')} onClick={stopAll} testId="compare-stop" />
              ) : (
                <Button variant="primary" size="sm" icon={Send} label={lastPrompt ? t('Send to all') : t('Compare')} onClick={() => void send()} disabled={!prompt.trim() || !panes.length} testId="compare-send" />
              )}
            </div>
          </section>

          <div className="fs-cmp__grid" style={{ '--fs-cmp-n': Math.min(4, Math.max(1, paneCount)) } as React.CSSProperties}>
            {panes.map((p, i) => (
              <PaneView
                key={p.id}
                pane={p}
                index={i}
                blind={opts.blind}
                parallel={opts.parallel}
                revealed={revealed}
                mode={opts.mode}
                routes={eligible}
                onSwap={(r) => swap(p, r)}
                onRemove={() => void removePane(p)}
                onStop={() => stopPane(p)}
                onReroll={() => reroll(p)}
                onToggleExpand={() => patch(p.id, { expanded: !p.expanded })}
                onTogglePreview={() => patch(p.id, { preview: !p.preview })}
                onApproval={(decision) => {
                  const last = p.turns[p.turns.length - 1];
                  if (last?.ask?.approvalId) void runPane(p, '', { id: last.ask.approvalId, decision });
                }}
                onAnswer={(answer) => void runPane(p, answer)}
                say={say}
              />
            ))}
          </div>

          {allDone && (
            <section className="fs-cmp__vote" aria-label={t('Vote')} data-testid="compare-vote">
              {voted ? (
                <p className="fs-cmp__verdict">
                  <Trophy size={14} aria-hidden="true" /> {voted === 'tie' ? t('Tie.') : t('{name} wins.', { name: voted })}
                  {opts.blind && ` · ${t('Names revealed.')}`}
                </p>
              ) : (
                <>
                  <span className="fs-cmp__vote-label">{t('Which answer is better?')}</span>
                  {panes.map((p, i) => (
                    <Button key={p.id} variant="secondary" size="sm" label={opts.blind && opts.mode !== 'search' ? t('Model {slot}', { slot: slotChar(i, opts.parallel) }) : names[i]} onClick={() => vote(i)} />
                  ))}
                  <Button variant="ghost" size="sm" label={t('Tie')} onClick={() => vote(-1)} />
                  {opts.blind && <Button variant="ghost" size="sm" icon={Eye} label={t('Reveal without voting')} onClick={() => setRevealed(true)} />}
                </>
              )}
            </section>
          )}
        </>
      )}

      <ModelPalette open={pickSlot !== null} onOpenChange={(o) => !o && setPickSlot(null)} routes={eligible} current={pickSlot !== null ? (panes[pickSlot]?.route ?? null) : null} onPick={(r) => { if (pickSlot !== null) swap(panes[pickSlot], r); setPickSlot(null); }} />
      <ModelPalette open={synthPick} onOpenChange={setSynthPick} routes={routes} current={synthRoute} onPick={(r) => { setOpts((o) => ({ ...o, synthRoute: r.id })); setSynthPick(false); }} />

      {poolOpen && (
        <Dialog open onOpenChange={(o) => !o && setPoolOpen(false)} title={t('Model pool')} description={t('Unticked models stay out of Shuffle and the pickers.')} testId="compare-pool" footer={<Button variant="primary" size="sm" label={t('Done')} onClick={() => setPoolOpen(false)} />}>
          <ul className="fs-cmp__pool">
            {routes.map((r) => (
              <li key={r.id}>
                <label className="fs-switch">
                  <input
                    type="checkbox"
                    checked={!excluded.includes(r.id)}
                    onChange={(e) => {
                      const next = e.target.checked ? excluded.filter((x) => x !== r.id) : [...excluded, r.id];
                      setExcludedState(next);
                      setExcluded(next);
                    }}
                  />
                  <span>{routeLabel(r)}</span>
                </label>
              </li>
            ))}
          </ul>
        </Dialog>
      )}

      {scoreOpen && (
        <Dialog
          open
          onOpenChange={(o) => !o && setScoreOpen(false)}
          title={t('Scoreboard')}
          description={tn(votes.length, '{n} vote in this browser.', '{n} votes in this browser.')}
          testId="compare-score"
          footer={
            <>
              <Button variant="danger" size="sm" label={t('Clear the votes')} disabled={!votes.length} onClick={() => { clearVotes(); setScoreOpen(false); say(t('Votes cleared.')); }} />
              <span className="fs-spacer" />
              <Button variant="primary" size="sm" label={t('Close')} onClick={() => setScoreOpen(false)} />
            </>
          }
        >
          <div className="fs-seg" role="radiogroup" aria-label={t('Mode')}>
            {(['all', 'chat', 'agent', 'search', 'research'] as const).map((m) => (
              <button key={m} type="button" role="radio" aria-checked={scoreMode === m} onClick={() => setScoreMode(m)}>
                {m === 'all' ? t('All') : t(MODE_LABEL[m])}
              </button>
            ))}
          </div>
          {rows.length === 0 ? (
            <p className="fs-muted">{t('No votes yet. Compare something and pick a winner.')}</p>
          ) : (
            <table className="fs-cmp__table">
              <thead>
                <tr>
                  <th>{t('Model')}</th>
                  <th>{t('Wins')}</th>
                  <th>{t('Losses')}</th>
                  <th>{t('Ties')}</th>
                  <th>{t('Win rate')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.model}>
                    <td>{r.model}</td>
                    <td>{r.wins}</td>
                    <td>{r.losses}</td>
                    <td>{r.ties}</td>
                    <td>{r.matches ? `${Math.round((r.wins / r.matches) * 100)}%` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Dialog>
      )}

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
