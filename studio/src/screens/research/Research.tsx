import {
  Check,
  ChevronDown,
  Copy,
  Cpu,
  Download,
  ExternalLink,
  Library,
  ListPlus,
  MessageSquare,
  Pencil,
  Play,
  RefreshCw,
  Telescope,
  Trash2,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton, Toast } from '../../components';
import { listEndpoints, type ModelEndpoint } from '../../adapters/settings';
import {
  activeResearch,
  applyResearchFit,
  cancelResearch,
  CATEGORIES,
  deleteResearch,
  discussResearch,
  exportFormats,
  exportUrl,
  followResearch,
  loadResearchLibrary,
  phaseLabel,
  reportUrl,
  researchFit,
  researchResult,
  searchProviders,
  startResearch,
  type ResearchFit,
  type ResearchItem,
  type ResearchProgress,
  type ResearchResult,
  type ResearchSettings,
  type SearchProvider,
} from '../../adapters/research';
import { relativeTime } from '../../adapters/home';
import { t, tn } from '../../i18n';
import { Rich } from '../rich';
import '../research.css';

/**
 * Deep Research: a question goes in, the agent searches, reads and
 * reflects for a few rounds, and a report with sources comes out. The
 * screen is the queue (what you want researched), what is running (with
 * its phase and clock), and what finished (report, sources, and what to
 * do with it). The library keeps every report; this is the workbench.
 */

type JobStatus = 'queued' | 'running' | 'done' | 'error' | 'cancelled';

interface Job {
  id: string;
  sessionId: string | null;
  query: string;
  settings: ResearchSettings;
  status: JobStatus;
  progress: ResearchProgress | null;
  startedAt: number;
  finishedAt: number;
  result: ResearchResult | null;
  error: string;
  sourceCount: number;
}

const QUEUE_KEY = 'fs-research-queue';
const SETTINGS_KEY = 'fs-research-settings';
const DISMISSED_KEY = 'fs-research-dismissed';

const DEFAULT_SETTINGS: ResearchSettings = { maxRounds: 0, category: '', searchProvider: '', endpointId: '', model: '' };

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode */
  }
}

function uid(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function elapsed(from: number, to: number): string {
  const s = Math.max(0, Math.floor(((to || Date.now()) - from) / 1000));
  const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}

const HINTS = [
  'Compare the three best e-bikes under 2000 € sold in Spain',
  'What changed in the EU AI Act for open-weight models in 2026?',
  'How do I run a Postgres read replica on a Raspberry Pi 5?',
  'Is intermittent fasting worth it for someone who lifts three times a week?',
];

/* ── Pieces ── */

function Clock({ from }: { from: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <span className="fs-rs__clock">{elapsed(from, 0)}</span>;
}

function Orbit({ progress, maxRounds }: { progress: ResearchProgress | null; maxRounds: number }) {
  const round = progress?.round ?? 0;
  const total = maxRounds || Math.max(round, 3);
  const phases = ['planning', 'searching', 'reading', 'analyzing', 'writing'];
  const at = phases.indexOf(progress?.phase ?? '');
  return (
    <div className="fs-rs__orbit" aria-hidden="true">
      {Array.from({ length: Math.min(total, 8) }, (_, i) => (
        <span key={i} className="fs-rs__dot" data-done={i + 1 < round || progress?.phase === 'writing' || undefined} data-now={i + 1 === round && progress?.phase !== 'writing' ? true : undefined} />
      ))}
      <span className="fs-rs__phase-bar">
        {phases.map((p, i) => (
          <span key={p} className="fs-rs__phase" data-on={i <= at || undefined} />
        ))}
      </span>
    </div>
  );
}

function ResultCard({ job, formats, onDiscuss, onDelete, onDismiss, say }: { job: Job; formats: string[]; onDiscuss: () => void; onDelete: () => void; onDismiss: () => void; say: (m: string, tone?: 'ok' | 'warn') => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const r = job.result;
  const copy = async () => {
    if (!r) return;
    try {
      await navigator.clipboard.writeText(r.result);
      say(t('Report copied.'));
    } catch {
      say(t('Could not copy.'), 'warn');
    }
  };
  const sources = r?.sources ?? [];
  return (
    <article className="fs-rs__job" data-status="done" data-testid="research-done">
      <header className="fs-rs__job-head">
        <Check size={14} className="fs-rs__ok" aria-hidden="true" />
        <div className="fs-rs__job-text">
          <h3 className="fs-rs__query">{job.query}</h3>
          <p className="fs-rs__meta">
            {job.startedAt && job.finishedAt ? `${elapsed(job.startedAt, job.finishedAt)} · ` : ''}
            {tn(sources.length || job.sourceCount, '{n} source', '{n} sources')}
            {r?.category ? ` · ${r.category}` : ''}
            {sources.length === 0 && job.sourceCount === 0 ? ` · ${t('nothing could be read: check the search provider')}` : ''}
          </p>
        </div>
        <IconButton icon={X} label={t('Clear from the list')} size="sm" onClick={onDismiss} />
      </header>
      {r && (
        <>
          <Button variant="ghost" size="sm" icon={ChevronDown} label={open ? t('Hide the report') : t('Show the report')} onClick={() => setOpen((o) => !o)} testId="research-toggle-report" />
          {open && (
            <div className="fs-rs__report">
              <div className="fs-prose fs-rs__prose">
                <Rich text={r.result} />
              </div>
              {sources.length > 0 && (
                <details className="fs-rs__sources">
                  <summary>{tn(sources.length, '{n} source', '{n} sources')}</summary>
                  <ol>
                    {sources.map((s, i) => (
                      <li key={`${s.url}-${i}`}>
                        <a href={s.url} target="_blank" rel="noopener noreferrer">
                          {s.title || s.url}
                        </a>
                      </li>
                    ))}
                  </ol>
                </details>
              )}
            </div>
          )}
        </>
      )}
      <footer className="fs-rs__actions">
        {job.sessionId && (
          <a className="fs-btn" data-variant="secondary" data-size="sm" href={reportUrl(job.sessionId)} target="_blank" rel="noopener">
            <ExternalLink size={16} aria-hidden="true" />
            <span>{t('Visual report')}</span>
          </a>
        )}
        <Button variant="secondary" size="sm" icon={MessageSquare} label={t('Discuss')} loading={busy === 'discuss'} onClick={() => { setBusy('discuss'); onDiscuss(); }} title={t('A chat that starts with this report as context')} />
        {job.sessionId && (
          <Menu
            trigger={<Button variant="ghost" size="sm" icon={Download} label={t('Export')} />}
            items={formats.map((f) => ({ label: f.toUpperCase(), onSelect: () => window.open(exportUrl(job.sessionId as string, f), '_blank', 'noopener') }))}
          />
        )}
        <IconButton icon={Copy} label={t('Copy the report')} size="sm" onClick={() => void copy()} />
        <span className="fs-spacer" />
        <Button variant="ghost" size="sm" icon={Trash2} label={t('Delete')} onClick={onDelete} />
      </footer>
    </article>
  );
}

function FitCard({ say }: { say: (m: string, tone?: 'ok' | 'warn') => void }) {
  const [fit, setFit] = useState<ResearchFit | null | 'loading'>(null);
  const [busy, setBusy] = useState(false);
  const load = () => {
    setFit('loading');
    researchFit()
      .then(setFit)
      .catch((err: Error) => {
        setFit(null);
        say(err.message || t('Could not read the recommendation.'), 'warn');
      });
  };
  const apply = async (fixes: boolean) => {
    setBusy(true);
    try {
      const r = await applyResearchFit(fixes);
      say(tn(r.applied.length, 'Profile «{tier}» applied: {n} setting changed.', 'Profile «{tier}» applied: {n} settings changed.', { tier: r.tier }));
      load();
    } catch (err) {
      say((err as Error).message || t('Could not apply the profile.'), 'warn');
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="fs-rs__fit" aria-labelledby="rs-fit">
      <header className="fs-rs__fit-head">
        <Cpu size={14} aria-hidden="true" />
        <h2 id="rs-fit">{t('Fit for this machine')}</h2>
        <span className="fs-spacer" />
        {fit === null && <Button variant="ghost" size="sm" label={t('Check')} onClick={load} testId="research-fit" />}
        {fit && fit !== 'loading' && <IconButton icon={RefreshCw} label={t('Check again')} size="sm" onClick={load} />}
      </header>
      {fit === null && <p className="fs-muted">{t('Faustus can pick rounds, timeouts and concurrency for the GPU it finds, and fix a search provider that would leave it with nothing to read.')}</p>}
      {fit === 'loading' && <Skeleton label={t('Checking the hardware')} count={2} height="20px" />}
      {fit && fit !== 'loading' && (
        <div className="fs-rs__fit-body">
          <p>
            <b>{t('Profile «{tier}»', { tier: fit.tier })}</b>
            {fit.gpuName || fit.vramGb ? ` · ${fit.gpuName}${fit.vramGb ? ` ${fit.vramGb} GB VRAM` : ''}${fit.ramGb ? ` · ${fit.ramGb} GB RAM` : ''}` : ''}
          </p>
          <p className="fs-muted">{fit.note}</p>
          {fit.changes.length > 0 && (
            <ul className="fs-rs__changes">
              {fit.changes.map((c) => (
                <li key={c.key}>
                  <code>{c.key}</code>: {String(c.from ?? '—')} → <b>{String(c.to)}</b>
                </li>
              ))}
            </ul>
          )}
          {fit.blockers.length > 0 && (
            <ul className="fs-rs__blockers">
              {fit.blockers.map((b) => (
                <li key={b.key}>
                  {b.text}
                  {b.fixLabel ? <i> — {b.fixLabel}</i> : null}
                </li>
              ))}
            </ul>
          )}
          <div className="fs-inline">
            {fit.alreadyApplied ? <span className="fs-muted">{t('Already applied.')}</span> : <Button variant="secondary" size="sm" label={t('Apply')} loading={busy} onClick={() => void apply(false)} />}
            {fit.blockers.some((b) => b.hasFix) && <Button variant="primary" size="sm" label={t('Apply and fix the search')} loading={busy} onClick={() => void apply(true)} />}
          </div>
        </div>
      )}
    </section>
  );
}

/* ── Screen ── */

export function ResearchScreen() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(() => params.get('q') ?? '');
  const [settings, setSettings] = useState<ResearchSettings>(() => ({ ...DEFAULT_SETTINGS, ...readJson<Partial<ResearchSettings>>(SETTINGS_KEY, {}) }));
  const [showSettings, setShowSettings] = useState(false);
  const [jobs, setJobs] = useState<Job[]>(() => readJson<Job[]>(QUEUE_KEY, []).filter((j) => j.status === 'queued'));
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set(readJson<string[]>(DISMISSED_KEY, [])));
  const [recent, setRecent] = useState<ResearchItem[] | null>(null);
  const [providers, setProviders] = useState<SearchProvider[]>([]);
  const [endpoints, setEndpoints] = useState<ModelEndpoint[]>([]);
  const [formats, setFormats] = useState<string[]>(['md']);
  const [editing, setEditing] = useState<Job | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Job | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [hint] = useState(() => HINTS[Math.floor(Math.random() * HINTS.length)]);
  const followers = useRef<Map<string, AbortController>>(new Map());
  const queryRef = useRef<HTMLTextAreaElement>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);

  useEffect(() => writeJson(SETTINGS_KEY, settings), [settings]);
  useEffect(() => writeJson(QUEUE_KEY, jobs.filter((j) => j.status === 'queued')), [jobs]);
  useEffect(() => writeJson(DISMISSED_KEY, [...dismissed]), [dismissed]);

  useEffect(() => {
    if (params.get('q')) {
      const next = new URLSearchParams(params);
      next.delete('q');
      setParams(next, { replace: true });
      queryRef.current?.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const patch = useCallback((id: string, p: Partial<Job>) => setJobs((cur) => cur.map((j) => (j.id === id ? { ...j, ...p } : j))), []);

  const follow = useCallback(
    (job: Job) => {
      if (!job.sessionId || followers.current.has(job.id)) return;
      const c = new AbortController();
      followers.current.set(job.id, c);
      let lastMessage = '';
      void followResearch(
        job.sessionId,
        (p) => {
          if (p.message) lastMessage = p.message;
          patch(job.id, { progress: p });
        },
        c.signal,
      ).then(async (status) => {
        followers.current.delete(job.id);
        if (status === 'aborted') return;
        let outcome = status;
        if (outcome !== 'done' && outcome !== 'completed' && outcome !== 'warning' && outcome !== 'cancelled') {
          // The stream can close before the final event, and the in-memory
          // status disappears once the report is on disk: ask for the report.
          try {
            const saved = await researchResult(job.sessionId as string);
            if (saved.result) outcome = 'done';
          } catch {
            /* really failed */
          }
        }
        if (outcome === 'done' || outcome === 'completed' || outcome === 'warning') {
          try {
            const result = await researchResult(job.sessionId as string);
            patch(job.id, { status: 'done', finishedAt: Date.now(), result, sourceCount: result.sources.length });
            if (document.hidden && 'Notification' in window && Notification.permission === 'granted') new Notification(t('Research finished'), { body: job.query.slice(0, 120) });
          } catch (err) {
            patch(job.id, { status: 'error', finishedAt: Date.now(), error: (err as Error).message || t('The report could not be read.') });
          }
        } else if (outcome === 'cancelled') patch(job.id, { status: 'cancelled', finishedAt: Date.now() });
        else patch(job.id, { status: 'error', finishedAt: Date.now(), error: lastMessage || t('The research failed.') });
        loadResearchLibrary({ limit: 8 }).then((r) => setRecent(r.items)).catch(() => undefined);
      });
    },
    [patch],
  );

  /* Adopt jobs still running on the server (a reload, another tab). */
  useEffect(() => {
    const c = new AbortController();
    activeResearch(c.signal)
      .then((active) => {
        setJobs((cur) => {
          const known = new Set(cur.map((j) => j.sessionId));
          const adopted: Job[] = active
            .filter((a) => !known.has(a.id))
            .map((a) => ({ id: uid(), sessionId: a.id, query: a.query, settings: { ...DEFAULT_SETTINGS }, status: 'running', progress: a.progress, startedAt: a.startedAt ? a.startedAt * 1000 : Date.now(), finishedAt: 0, result: null, error: '', sourceCount: 0 }));
          return adopted.length ? [...adopted, ...cur] : cur;
        });
      })
      .catch(() => undefined);
    loadResearchLibrary({ limit: 8 }, c.signal).then((r) => setRecent(r.items)).catch(() => setRecent([]));
    searchProviders(c.signal).then(setProviders);
    listEndpoints(c.signal).then((l) => setEndpoints(l.filter((e) => e.enabled))).catch(() => undefined);
    exportFormats().then(setFormats);
    if ('Notification' in window && Notification.permission === 'default') void Notification.requestPermission();
    return () => c.abort();
  }, []);

  useEffect(() => {
    for (const j of jobs) if (j.status === 'running' && j.sessionId) follow(j);
  }, [jobs, follow]);

  useEffect(() => {
    const map = followers.current;
    return () => {
      for (const c of map.values()) c.abort();
    };
  }, []);

  const launch = async (job: Job) => {
    patch(job.id, { status: 'running', startedAt: Date.now(), progress: null, error: '' });
    try {
      const sessionId = await startResearch(job.query, job.settings);
      patch(job.id, { sessionId });
    } catch (err) {
      patch(job.id, { status: 'error', finishedAt: Date.now(), error: (err as Error).message || t('Could not start.') });
    }
  };

  const makeJob = (q: string): Job => ({ id: uid(), sessionId: null, query: q, settings: { ...settings }, status: 'queued', progress: null, startedAt: 0, finishedAt: 0, result: null, error: '', sourceCount: 0 });

  const addToQueue = () => {
    const q = query.trim();
    if (!q) {
      queryRef.current?.focus();
      return;
    }
    setJobs((cur) => [...cur, makeJob(q)]);
    setQuery('');
  };

  const startNow = () => {
    const q = query.trim();
    if (q) {
      const job = makeJob(q);
      setJobs((cur) => [job, ...cur]);
      setQuery('');
      void launch(job);
      return;
    }
    const queued = jobs.filter((j) => j.status === 'queued');
    if (queued.length === 1) void launch(queued[0]);
    else queryRef.current?.focus();
  };

  const startAll = async (sequential: boolean) => {
    const queued = jobs.filter((j) => j.status === 'queued');
    if (!sequential) {
      for (const j of queued) void launch(j);
      return;
    }
    for (const j of queued) {
      await launch(j);
      await new Promise<void>((resolve) => {
        const check = () => {
          const running = followers.current.has(j.id);
          if (!running) resolve();
          else window.setTimeout(check, 1500);
        };
        window.setTimeout(check, 1500);
      });
    }
  };

  const cancel = async (job: Job) => {
    if (job.sessionId) await cancelResearch(job.sessionId).catch(() => undefined);
    followers.current.get(job.id)?.abort();
    followers.current.delete(job.id);
    patch(job.id, { status: 'cancelled', finishedAt: Date.now() });
  };

  const remove = (job: Job) => {
    followers.current.get(job.id)?.abort();
    followers.current.delete(job.id);
    setJobs((cur) => cur.filter((j) => j.id !== job.id));
  };

  const discuss = async (sessionId: string) => {
    try {
      const out = await discussResearch(sessionId);
      navigate(`/studio?s=${encodeURIComponent(out.sessionId)}`);
    } catch (err) {
      say((err as Error).message || t('Could not open the chat.'), 'warn');
    }
  };

  const doDelete = async (job: Job) => {
    setConfirmDelete(null);
    if (job.sessionId) {
      try {
        await deleteResearch(job.sessionId);
      } catch (err) {
        say((err as Error).message, 'warn');
        return;
      }
    }
    remove(job);
    setRecent((cur) => (cur ? cur.filter((r) => r.id !== job.sessionId) : cur));
    say(t('Deleted.'));
  };

  const queued = jobs.filter((j) => j.status === 'queued');
  const running = jobs.filter((j) => j.status === 'running');
  const finished = jobs.filter((j) => j.status === 'done' || j.status === 'error' || j.status === 'cancelled');
  const sessionIds = useMemo(() => new Set(jobs.map((j) => j.sessionId).filter(Boolean)), [jobs]);
  const recentOthers = (recent ?? []).filter((r) => !sessionIds.has(r.id) && !dismissed.has(r.id));
  const endpoint = endpoints.find((e) => e.id === settings.endpointId);

  const submit = (e: React.KeyboardEvent) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      startNow();
    }
  };

  return (
    <div className="fs-screen fs-rs" data-testid="research">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Deep Research')}</h1>
          <p className="fs-prose fs-rs__lede">{t('Ask something worth several searches. Faustus plans, reads, reflects and writes a report with its sources.')}</p>
        </div>
        <div className="fs-inline">
          <Link className="fs-btn" data-variant="ghost" data-size="sm" to="/library?type=research">
            <Library size={16} aria-hidden="true" />
            <span>{t('All reports')}</span>
          </Link>
        </div>
      </header>

      <section className="fs-rs__ask" aria-label={t('New research')}>
        <textarea ref={queryRef} className="fs-rs__query" rows={3} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={submit} placeholder={hint} autoFocus data-testid="research-query" />
        <div className="fs-rs__ask-row">
          <button type="button" className="fs-rs__settings-toggle" aria-expanded={showSettings} onClick={() => setShowSettings((s) => !s)}>
            <ChevronDown size={12} aria-hidden="true" data-open={showSettings || undefined} />
            {t('Rounds {r} · {f} · {p} · {m}', {
              r: settings.maxRounds || t('auto'),
              f: t(CATEGORIES.find((c) => c.value === settings.category)?.label ?? 'Auto'),
              p: providers.find((p) => p.id === settings.searchProvider)?.label ?? t('default search'),
              m: settings.model || endpoint?.name || t('default model'),
            })}
          </button>
          <span className="fs-spacer" />
          <Button variant="secondary" size="sm" icon={ListPlus} label={t('Queue')} onClick={addToQueue} testId="research-queue" />
          <Button variant="primary" size="sm" icon={Play} label={t('Start')} onClick={startNow} testId="research-start" title="Ctrl+Enter" />
        </div>
        {showSettings && (
          <div className="fs-rs__settings">
            <label className="fs-rs__setting">
              <span>{t('Rounds')}</span>
              <select className="fs-field" value={settings.maxRounds} onChange={(e) => setSettings((s) => ({ ...s, maxRounds: Number(e.target.value) }))}>
                <option value={0}>{t('Auto')}</option>
                {Array.from({ length: 20 }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
              <small>{t('Search → read → reflect cycles. Auto lets the agent stop when it has enough (up to 20).')}</small>
            </label>
            <label className="fs-rs__setting">
              <span>{t('Format')}</span>
              <select className="fs-field" value={settings.category} onChange={(e) => setSettings((s) => ({ ...s, category: e.target.value }))}>
                {CATEGORIES.map((c) => (
                  <option key={c.value} value={c.value}>
                    {t(c.label)}
                  </option>
                ))}
              </select>
              <small>{t('Auto lets the model pick the shape of the report.')}</small>
            </label>
            <label className="fs-rs__setting">
              <span>{t('Search')}</span>
              <select className="fs-field" value={settings.searchProvider} onChange={(e) => setSettings((s) => ({ ...s, searchProvider: e.target.value }))}>
                <option value="">{t('Default')}</option>
                {providers.map((p) => (
                  <option key={p.id} value={p.id} disabled={!p.available}>
                    {p.label}
                    {p.available ? '' : ` · ${t('not set up')}`}
                  </option>
                ))}
              </select>
            </label>
            <label className="fs-rs__setting">
              <span>{t('Endpoint')}</span>
              <select className="fs-field" value={settings.endpointId} onChange={(e) => setSettings((s) => ({ ...s, endpointId: e.target.value, model: '' }))}>
                <option value="">{t('Default')}</option>
                {endpoints.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="fs-rs__setting">
              <span>{t('Model')}</span>
              <select className="fs-field" value={settings.model} disabled={!endpoint} onChange={(e) => setSettings((s) => ({ ...s, model: e.target.value }))}>
                <option value="">{t('Default')}</option>
                {(endpoint?.models ?? []).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
      </section>

      {queued.length > 0 && (
        <section className="fs-rs__group" aria-label={t('Queued')}>
          <header className="fs-rs__group-head">
            <h2>{tn(queued.length, '{n} queued', '{n} queued')}</h2>
            <span className="fs-spacer" />
            {queued.length > 1 && (
              <Menu
                trigger={<Button variant="secondary" size="sm" icon={Play} label={t('Start all')} testId="research-start-all" />}
                align="end"
                items={[
                  { label: t('All at once'), onSelect: () => void startAll(false) },
                  { label: t('One after another'), onSelect: () => void startAll(true) },
                ]}
              />
            )}
          </header>
          <ul className="fs-rs__list">
            {queued.map((job) => (
              <li key={job.id} className="fs-rs__job" data-status="queued" data-testid="research-queued">
                <div className="fs-rs__job-head">
                  <div className="fs-rs__job-text">
                    <h3 className="fs-rs__query">{job.query}</h3>
                    <p className="fs-rs__meta">
                      {t('Rounds {r} · {f}', { r: job.settings.maxRounds || t('auto'), f: t(CATEGORIES.find((c) => c.value === job.settings.category)?.label ?? 'Auto') })}
                    </p>
                  </div>
                  <Button variant="primary" size="sm" icon={Play} label={t('Start')} onClick={() => void launch(job)} />
                  <IconButton icon={Pencil} label={t('Edit the question')} size="sm" onClick={() => setEditing(job)} />
                  <IconButton icon={X} label={t('Remove')} size="sm" onClick={() => remove(job)} />
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {running.length > 0 && (
        <section className="fs-rs__group" aria-label={t('Running')}>
          <header className="fs-rs__group-head">
            <h2>{tn(running.length, '{n} running', '{n} running')}</h2>
          </header>
          <ul className="fs-rs__list">
            {running.map((job) => (
              <li key={job.id} className="fs-rs__job" data-status="running" aria-live="polite" data-testid="research-running">
                <div className="fs-rs__job-head">
                  <div className="fs-rs__job-text">
                    <h3 className="fs-rs__query">{job.query}</h3>
                    <p className="fs-rs__meta fs-rs__meta--live">
                      <Telescope size={12} aria-hidden="true" />
                      {phaseLabel(job.progress, job.settings.maxRounds)} · <Clock from={job.startedAt} />
                    </p>
                  </div>
                  <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={() => void cancel(job)} />
                </div>
                <Orbit progress={job.progress} maxRounds={job.settings.maxRounds} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {(finished.length > 0 || recentOthers.length > 0 || recent === null) && (
        <section className="fs-rs__group" aria-label={t('Finished')}>
          <header className="fs-rs__group-head">
            <h2>{t('Finished')}</h2>
          </header>
          <ul className="fs-rs__list">
            {finished.map((job) =>
              job.status === 'done' ? (
                <li key={job.id}>
                  <ResultCard job={job} formats={formats} onDiscuss={() => void discuss(job.sessionId as string)} onDelete={() => setConfirmDelete(job)} onDismiss={() => remove(job)} say={say} />
                </li>
              ) : (
                <li key={job.id} className="fs-rs__job" data-status={job.status} data-testid={`research-${job.status}`}>
                  <div className="fs-rs__job-head">
                    <div className="fs-rs__job-text">
                      <h3 className="fs-rs__query">{job.query}</h3>
                      <p className="fs-rs__meta">{job.status === 'cancelled' ? t('Cancelled.') : job.error || t('The research failed.')}</p>
                    </div>
                    <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Retry')} onClick={() => void launch({ ...job, sessionId: null })} />
                    <IconButton icon={Pencil} label={t('Edit and retry')} size="sm" onClick={() => setEditing(job)} />
                    <IconButton icon={X} label={t('Dismiss')} size="sm" onClick={() => remove(job)} />
                  </div>
                </li>
              ),
            )}
            {recent === null && finished.length === 0 && <Skeleton label={t('Loading recent reports')} count={3} height="64px" />}
            {recentOthers.map((r) => (
              <li key={r.id} className="fs-rs__job" data-status="done" data-testid="research-recent">
                <div className="fs-rs__job-head">
                  {r.thumbnail && <img className="fs-rs__thumb" src={r.thumbnail} alt="" loading="lazy" />}
                  <div className="fs-rs__job-text">
                    <h3 className="fs-rs__query">{r.query}</h3>
                    <p className="fs-rs__meta">
                      {r.completedAt ? relativeTime(r.completedAt) : ''}
                      {r.duration ? ` · ${r.duration}` : ''} · {tn(r.sourceCount, '{n} source', '{n} sources')}
                      {r.category ? ` · ${r.category}` : ''}
                    </p>
                  </div>
                  <a className="fs-btn" data-variant="secondary" data-size="sm" href={reportUrl(r.id)} target="_blank" rel="noopener">
                    <ExternalLink size={16} aria-hidden="true" />
                    <span>{t('Visual report')}</span>
                  </a>
                  <Button variant="ghost" size="sm" icon={MessageSquare} label={t('Discuss')} onClick={() => void discuss(r.id)} />
                  <IconButton icon={X} label={t('Clear from the list')} size="sm" onClick={() => setDismissed((d) => new Set(d).add(r.id))} />
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {jobs.length === 0 && recent && recentOthers.length === 0 && (
        <EmptyState icon={Telescope} title={t('Nothing researched yet')} body={t('Write a question above and press Start. Reports land in the Library too.')} />
      )}

      <FitCard say={say} />

      {editing && (
        <Dialog
          open
          onOpenChange={(o) => !o && setEditing(null)}
          title={t('Edit the question')}
          testId="research-edit"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setEditing(null)} />
              <Button
                variant="primary"
                size="sm"
                label={editing.status === 'queued' ? t('Save') : t('Retry')}
                onClick={() => {
                  const q = editing.query.trim();
                  if (!q) return;
                  if (editing.status === 'queued') patch(editing.id, { query: q });
                  else void launch({ ...editing, query: q, sessionId: null });
                  setEditing(null);
                }}
              />
            </>
          }
        >
          <textarea className="fs-field fs-rs__edit" rows={4} value={editing.query} onChange={(e) => setEditing({ ...editing, query: e.target.value })} autoFocus />
        </Dialog>
      )}

      {confirmDelete && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirmDelete(null)}
          title={t('Delete this report?')}
          description={t('The report and its sources are removed from the library. This cannot be undone.')}
          testId="research-delete"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Keep it')} onClick={() => setConfirmDelete(null)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} onClick={() => void doDelete(confirmDelete)} />
            </>
          }
        />
      )}

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <X size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
