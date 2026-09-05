import { ChevronDown, ChevronUp, Copy, Crown, Merge, Play, Plus, RotateCcw, Square, Trophy, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Skeleton, StatusBadge, Toast, type RunStatus } from '../../components';
import { listModels, type ModelRoute } from '../../adapters/chat';
import { relativeTime } from '../../adapters/home';
import {
  answersByEntry,
  AXES,
  cancelTournament,
  detailOfEntry,
  followRun,
  getRun,
  LIVE,
  listRuns,
  loadSetup,
  mergePromptFor,
  saveSetup,
  startTournament,
  stateOfEntry,
  tournamentConfig,
  winnerOf,
  type Answer,
  type ProgressRow,
  type Run,
  type TournamentConfig,
} from '../../adapters/tournament';
import ModelPalette from '../ModelPalette';
import { Rich } from '../rich';
import { t, tn } from '../../i18n';

/**
 * Tournament: the same prompt to N models blind and in parallel; then rounds
 * where every model sees the others' answers anonymised and is told to
 * weave a hybrid; then a judged, ranked table. `?run=<id>` opens a run.
 *
 * The previous interface kept this in a modal with a checkbox list of
 * models; here the setup is Studio's model palette per slot, the board is
 * one card per entry with its rounds as chips, and the table is the
 * finish line — with "Merge into the composer" as the way out.
 */

const BADGE: Record<string, RunStatus> = { queued: 'queued', running: 'running', judging: 'running', cancelling: 'running', done: 'succeeded', error: 'failed', cancelled: 'cancelled' };
const STATUS_LABEL: Record<string, string> = { queued: 'Queued', running: 'Answering', judging: 'Judging', cancelling: 'Stopping', done: 'Finished', error: 'Failed', cancelled: 'Cancelled' };

function stoppedBy(run: Run): string {
  if (run.stoppedBy === 'convergence') return t('Stopped early: the rounds converged ({score})', { score: (run.convergence?.score ?? 0).toFixed(2) });
  if (run.stoppedBy === 'cancelled') return t('Stopped: cancelled');
  if (run.stoppedBy === 'rounds') return tn(run.roundsRun, 'Ran all {n} round', 'Ran all {n} rounds');
  return '';
}

function rankingNote(run: Run): { kind: 'judge' | 'mixed' | 'deterministic'; text: string } {
  if (run.ranking === 'judge') return { kind: 'judge', text: t('Ranked by the judge') };
  if (run.ranking === 'mixed') return { kind: 'mixed', text: run.rankingNote || t('The judge scored only some of the answers') };
  return { kind: 'deterministic', text: run.rankingNote || t('No judge available — ranked by a deterministic tiebreak') };
}

/** Round 0 is the blind answer; every later round is a hybrid of the others'. */
function roundLabel(n: number): string {
  return n === 0 ? t('Blind answer') : t('Hybrid {n}', { n });
}

function formatElapsed(s: number): string {
  const n = Math.max(0, Math.round(s));
  return n < 60 ? `${n}s` : `${Math.floor(n / 60)}m ${String(n % 60).padStart(2, '0')}s`;
}

const STATE_LABEL: Record<ProgressRow['state'], string> = { queued: 'Queued', running: 'Writing', answered: 'Answered', error: 'Failed', cancelled: 'Stopped' };

export function Tournament() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const runId = params.get('run');
  const [config, setConfig] = useState<TournamentConfig | null>(null);
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const say = useCallback((m: string) => {
    setNotice(m);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const saved = useMemo(() => loadSetup(), []);
  const [prompt, setPrompt] = useState(saved.prompt ?? '');
  const [models, setModels] = useState<string[]>(saved.models ?? []);
  const [rounds, setRounds] = useState(saved.rounds ?? 3);
  const [judge, setJudge] = useState(saved.judge ?? '');
  const [pick, setPick] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);

  const refreshRuns = useCallback(async (signal?: AbortSignal) => {
    try {
      const out = await listRuns(signal);
      if (!signal?.aborted) setRuns(out.runs);
    } catch (e) {
      if (!signal?.aborted) setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    const ac = new AbortController();
    tournamentConfig(ac.signal)
      .then((c) => {
        setConfig(c);
        if (!saved.rounds) setRounds(c.defaultRounds);
      })
      .catch((e: Error) => setError(e.message));
    listModels(ac.signal).then(setRoutes).catch(() => setRoutes([]));
    void refreshRuns(ac.signal);
    return () => ac.abort();
  }, [refreshRuns, saved.rounds]);

  useEffect(() => {
    saveSetup({ prompt, models, rounds, judge });
  }, [prompt, models, rounds, judge]);

  const maxModels = config?.maxModels ?? 8;
  const minModels = config?.minModels ?? 2;
  const canRun = Boolean(prompt.trim()) && models.length >= minModels && (config?.enabled ?? true) && !starting;

  const start = async () => {
    if (!canRun) return;
    setStarting(true);
    setError(null);
    try {
      const run = await startTournament({ prompt: prompt.trim(), models, rounds, judgeModel: judge || undefined });
      const p = new URLSearchParams(params);
      p.set('run', run.id);
      setParams(p);
      void refreshRuns();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const open = (id: string | null) => {
    const p = new URLSearchParams(params);
    if (id) p.set('run', id);
    else p.delete('run');
    setParams(p);
  };

  if (runId) {
    return (
      <Board
        id={runId}
        fusion={config?.fusionInstruction ?? ''}
        onBack={() => {
          open(null);
          void refreshRuns();
        }}
        onRerun={(run) => {
          setPrompt(run.prompt);
          setModels(run.models);
          setRounds(run.rounds);
          setJudge(run.judgeModel);
          open(null);
        }}
        onMerge={(text) => navigate(`/studio?draft=${encodeURIComponent(text)}`)}
        say={say}
      />
    );
  }

  const chosenRoutes = models.map((m) => routes.find((r) => r.model === m || r.id === m) ?? null);

  return (
    <div className="fs-trn" data-testid="tournament">
      {config && !config.enabled && (
        <p className="fs-notice" data-tone="warning">
          {t('The tournament is switched off in Settings → Agent & automation.')}
        </p>
      )}
      <section className="fs-trn__setup" aria-label={t('New tournament')}>
        <label className="fs-trn__prompt">
          <span className="fs-trn__label">{t('The task')}</span>
          <textarea className="fs-field fs-trn__textarea" rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t('The same prompt goes to every model, blind and in parallel…')} data-testid="tournament-prompt" />
        </label>
        <div className="fs-trn__slots" role="group" aria-label={t('Models')}>
          <span className="fs-trn__label">{tn(models.length, '{n} model', '{n} models')} · {t('at least {n}', { n: minModels })}</span>
          <div className="fs-trn__chips">
            {models.map((m, i) => (
              <span key={`${m}-${i}`} className="fs-chip fs-trn__chip" data-on>
                <button type="button" className="fs-trn__chip-name" onClick={() => setPick(i)} title={t('Swap model')}>
                  {chosenRoutes[i]?.model ?? m}
                </button>
                <IconButton icon={X} size="sm" label={t('Remove {name}', { name: m })} onClick={() => setModels((cur) => cur.filter((_, j) => j !== i))} />
              </span>
            ))}
            {models.length < maxModels && <Button variant="ghost" size="sm" icon={Plus} label={t('Add model')} onClick={() => setPick(models.length)} testId="tournament-add-model" />}
          </div>
        </div>
        <div className="fs-trn__knobs">
          <label className="fs-trn__knob">
            <span className="fs-trn__label">{t('Rounds')}</span>
            <div className="fs-seg" role="radiogroup" aria-label={t('Rounds')}>
              {Array.from({ length: config?.maxRounds ?? 6 }, (_, i) => i + 1).map((n) => (
                <button key={n} type="button" role="radio" aria-checked={rounds === n} onClick={() => setRounds(n)}>
                  {n}
                </button>
              ))}
            </div>
          </label>
          <label className="fs-trn__knob">
            <span className="fs-trn__label">{t('Judge')}</span>
            <select className="fs-field" value={judge} onChange={(e) => setJudge(e.target.value)} aria-label={t('Judge')}>
              <option value="">{t('The biggest model of the entrants')}</option>
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
          <span className="fs-spacer" />
          <Button variant="primary" icon={Play} label={t('Run the tournament')} disabled={!canRun} loading={starting} onClick={() => void start()} testId="tournament-run" />
        </div>
        {config?.fusionInstruction && <p className="fs-trn__fusion">{t('Between rounds, every model is told:')} <q>{config.fusionInstruction}</q></p>}
        {error && (
          <p className="fs-notice" data-tone="danger" role="alert">
            {error}
          </p>
        )}
      </section>

      <section className="fs-trn__past" aria-label={t('Recent tournaments')}>
        <h3 className="fs-trn__h">{t('Recent tournaments')}</h3>
        {!runs && <Skeleton label={t('Loading tournaments')} count={3} height="52px" radius="panel" />}
        {runs && !runs.length && <EmptyState icon={Trophy} title={t('No tournaments yet')} body={t('Pick two or more models, give them the same task and see which answer wins — or take the best of all of them.')} headingLevel={3} />}
        {runs && runs.length > 0 && (
          <ul className="fs-trn__list">
            {runs.map((r) => (
              <li key={r.id}>
                <button type="button" className="fs-trn__row" onClick={() => open(r.id)} data-testid="tournament-past">
                  <StatusBadge status={BADGE[r.status] ?? 'queued'} label={t(STATUS_LABEL[r.status] ?? r.status)} />
                  <span className="fs-trn__row-prompt">{r.prompt || t('Untitled')}</span>
                  <span className="fs-trn__row-meta">
                    {tn(r.models.length, '{n} model', '{n} models')}
                    {r.winner ? ` · ${t('winner')} ${r.winner}` : ''}
                    {r.created ? ` · ${relativeTime(new Date(r.created * 1000).toISOString())}` : ''}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <ModelPalette
        open={pick !== null}
        onOpenChange={(o) => !o && setPick(null)}
        routes={routes}
        current={pick !== null ? (chosenRoutes[pick] ?? null) : null}
        onPick={(r) => {
          if (pick === null) return;
          setModels((cur) => {
            const next = cur.slice();
            next[pick] = r.model;
            return next;
          });
          setPick(null);
        }}
      />
      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}

function Board({ id, fusion, onBack, onRerun, onMerge, say }: { id: string; fusion: string; onBack: () => void; onRerun: (run: Run) => void; onMerge: (text: string) => void; say: (m: string) => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [openRound, setOpenRound] = useState<Record<number, number>>({});
  const [eventsOpen, setEventsOpen] = useState(false);
  const [tick, setTick] = useState(0);
  const fetchedAt = useRef(Date.now());

  useEffect(() => {
    const ac = new AbortController();
    setRun(null);
    setError(null);
    const take = (r: Run) => {
      fetchedAt.current = Date.now();
      setRun(r);
    };
    getRun(id, ac.signal)
      .then((first) => {
        take(first);
        if (LIVE.includes(first.status)) void followRun(id, take, ac.signal);
      })
      .catch((e: Error) => {
        if (!ac.signal.aborted) setError(e.message);
      });
    return () => ac.abort();
  }, [id]);

  const stop = async () => {
    setStopping(true);
    try {
      await cancelTournament(id);
      say(t('Stopping…'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setStopping(false);
    }
  };

  const byEntry = useMemo(() => (run ? answersByEntry(run) : new Map<number, Answer[]>()), [run]);
  const winner = run ? winnerOf(run) : null;
  const live = run ? LIVE.includes(run.status) : false;
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  const elapsed = run ? run.durationS + (live ? (Date.now() - fetchedAt.current) / 1000 : 0) : 0;
  void tick;
  const merge = run ? mergePromptFor(run, fusion) : '';

  if (error) {
    return <EmptyState icon={Trophy} title={t('Could not open this tournament')} body={error} primaryAction={{ label: t('Back to the list'), onClick: onBack }} />;
  }
  if (!run) return <Skeleton label={t('Loading the tournament')} count={4} height="80px" radius="panel" />;

  return (
    <div className="fs-trn fs-trn--board" data-testid="tournament-board" data-live={live || undefined}>
      <header className="fs-trn__board-head">
        <div className="fs-trn__board-title">
          <StatusBadge status={BADGE[run.status] ?? 'queued'} label={t(STATUS_LABEL[run.status] ?? run.status)} size="md" />
          <span className="fs-trn__board-meta">
            {tn(run.models.length, '{n} model', '{n} models')} · {tn(run.rounds, '{n} round', '{n} rounds')} · {t('judge')} {run.judgeModel || '—'} · {formatElapsed(elapsed)}
          </span>
        </div>
        <div className="fs-inline">
          {live && <Button variant="danger" size="sm" icon={Square} label={t('Stop')} loading={stopping} onClick={() => void stop()} testId="tournament-stop" />}
          {!live && <Button variant="ghost" size="sm" icon={RotateCcw} label={t('Run again')} onClick={() => onRerun(run)} />}
          <Button variant="ghost" size="sm" label={t('Back')} onClick={onBack} />
        </div>
      </header>
      <blockquote className="fs-trn__task">{run.prompt}</blockquote>
      {run.error && (
        <p className="fs-notice" data-tone="danger" role="alert">
          {run.error}
        </p>
      )}
      {run.degraded && (
        <p className="fs-notice" data-tone="warning">
          {t('Degraded: some answers or the judge failed; the table says which.')}
        </p>
      )}

      <div className="fs-trn__board" role="list" aria-label={t('Entrants')}>
        {run.models.map((model, entry) => {
          const rows = byEntry.get(entry) ?? [];
          const state = stateOfEntry(run, entry, rows);
          const detail = detailOfEntry(run, entry);
          const chosenRound = openRound[entry] ?? (rows.length ? rows[rows.length - 1].round : 0);
          const shown = rows.find((r) => r.round === chosenRound) ?? rows[rows.length - 1];
          const finalRow = run.final.find((f) => f.entry === entry);
          const isWinner = winner?.entry === entry;
          return (
            <article key={`${entry}-${model}`} className="fs-trn__card" role="listitem" data-state={state} data-winner={isWinner || undefined}>
              <header className="fs-trn__card-head">
                {isWinner && <Crown size={14} aria-label={t('Winner')} />}
                <strong className="fs-trn__card-name">{model}</strong>
                <span className="fs-trn__state" data-state={state}>
                  {t(STATE_LABEL[state])}
                </span>
                {finalRow?.rank !== null && finalRow?.rank !== undefined && <span className="fs-trn__rank">#{finalRow.rank}</span>}
              </header>
              {detail && <p className="fs-trn__detail">{detail}</p>}
              {rows.length > 0 && (
                <div className="fs-trn__rounds" role="group" aria-label={t('Rounds')}>
                  {rows.map((a) => (
                    <button key={a.round} type="button" className="fs-chip" data-on={a.round === chosenRound || undefined} onClick={() => setOpenRound((cur) => ({ ...cur, [entry]: a.round }))}>
                      {roundLabel(a.round)}
                    </button>
                  ))}
                </div>
              )}
              {shown ? (
                <div className="fs-trn__answer">
                  <Rich text={shown.text} />
                  <p className="fs-trn__answer-meta">
                    {shown.elapsedS !== null ? `${shown.elapsedS.toFixed(1)}s` : ''}
                    {shown.tokens !== null ? ` · ${shown.tokens} tok${shown.tokensSource ? ` (${shown.tokensSource})` : ''}` : ''}
                    {` · ${shown.text.length} ${t('chars')}`}
                  </p>
                </div>
              ) : (
                <p className="fs-trn__waiting">{state === 'running' ? t('Writing…') : state === 'queued' ? t('Waiting for its turn') : ''}</p>
              )}
            </article>
          );
        })}
      </div>

      {run.final.length > 0 && (
        <section className="fs-trn__results" aria-label={t('Results')}>
          <div className="fs-trn__results-head">
            <h3 className="fs-trn__h">
              <Trophy size={14} aria-hidden="true" /> {t('Results')}
            </h3>
            <span className="fs-trn__note" data-kind={rankingNote(run).kind}>
              {rankingNote(run).text}
            </span>
            {stoppedBy(run) && <span className="fs-trn__note">{stoppedBy(run)}</span>}
            <span className="fs-spacer" />
            {winner && <Button variant="ghost" size="sm" icon={Copy} label={t('Copy the winner')} onClick={() => void navigator.clipboard.writeText(winner.text).then(() => say(t('Copied')))} />}
            {merge && <Button variant="primary" size="sm" icon={Merge} label={t('Merge into the composer')} onClick={() => onMerge(merge)} testId="tournament-merge" />}
          </div>
          <div className="fs-trn__table-wrap">
            <table className="fs-trn__table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>{t('Model')}</th>
                  <th>{t('Round')}</th>
                  {AXES.map((axis) => (
                    <th key={axis}>{t(axis === 'correctness' ? 'Correct' : axis === 'completeness' ? 'Complete' : 'Sophisticated')}</th>
                  ))}
                  <th>{t('Total')}</th>
                  <th>{t('Tiebreak')}</th>
                  <th>{t('Outcome')}</th>
                </tr>
              </thead>
              <tbody>
                {run.final
                  .slice()
                  .sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99))
                  .map((row) => (
                    <tr key={`${row.entry}-${row.round}`} data-winner={row.rank === 1 || undefined}>
                      <td>{row.rank ?? '—'}</td>
                      <td className="fs-trn__td-model">{row.model}</td>
                      <td>{row.round}</td>
                      {AXES.map((axis) => (
                        <td key={axis} data-null={row.scores[axis] === null || undefined} title={row.scores[axis] === null ? t('The judge did not score this') : undefined}>
                          {row.scores[axis] ?? '—'}
                        </td>
                      ))}
                      <td>{row.total ?? '—'}</td>
                      <td>{row.tiebreak === null ? '—' : row.tiebreak.toFixed(3)}</td>
                      <td className="fs-trn__td-outcome">
                        {row.outcome}
                        {row.note ? <span className="fs-muted"> · {row.note}</span> : null}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {run.judge && !run.judge.ok && <p className="fs-muted">{t('Judge {model} failed', { model: run.judge.model })}{run.judge.error ? `: ${run.judge.error}` : ''}</p>}
        </section>
      )}

      {run.events.length > 0 && (
        <section className="fs-trn__events">
          <button type="button" className="fs-trn__events-toggle" onClick={() => setEventsOpen((v) => !v)} aria-expanded={eventsOpen}>
            {eventsOpen ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
            {tn(run.events.length, '{n} event', '{n} events')}
          </button>
          {eventsOpen && (
            <ol className="fs-trn__event-list">
              {run.events.map((ev, i) => (
                <li key={i}>
                  <code>{ev.event}</code>
                  {ev.model ? ` ${ev.model}` : ''}
                  {ev.round !== null ? ` · ${roundLabel(ev.round)}` : ''}
                  {ev.detail ? ` — ${ev.detail}` : ''}
                </li>
              ))}
            </ol>
          )}
        </section>
      )}
    </div>
  );
}
