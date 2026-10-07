import type { Turn } from './model';
import { t } from '../../i18n';
import './turn-activity.css';

const n = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
const s = (value: unknown): string => typeof value === 'string' ? value : '';
const duration = (ms?: number): string => ms === undefined ? t('not reported') : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
const count = (value?: number): string => value === undefined ? t('not reported') : Math.round(value).toLocaleString();

function timingBar(label: string, value: number | undefined, max: number, source: string, color: string) {
  if (value === undefined) return null;
  const pct = max > 0 ? Math.max(2, Math.min(100, value / max * 100)) : 0;
  return (
    <div className="fs-turn-activity__metric" key={label} data-source={source}>
      <span>{t(label)}</span>
      <span className="fs-turn-activity__track" aria-hidden="true">
        <span style={{ width: `${pct}%`, background: color }} />
      </span>
      <strong>{duration(value)}</strong>
      <small>{source === 'engine' ? t('engine') : t('observed')}</small>
    </div>
  );
}

export function TurnActivity({ turn }: { turn: Turn }) {
  const rounds = turn.metrics?.round_activity ?? [];
  const workers = turn.workers;
  const tools = turn.steps;
  const advisors = turn.advice ?? [];
  const auxiliaries = [
    ...(turn.metrics?.recovery_usage ?? []),
    ...(turn.metrics?.compaction_usage ?? []),
  ];
  const models = new Set([
    s(turn.metrics?.model),
    ...rounds.map((item) => item.model).filter((value): value is string => Boolean(value)),
    ...workers.map((item) => item.model).filter(Boolean),
    ...advisors.map((item) => item.model).filter((value): value is string => Boolean(value)),
    ...auxiliaries.map((item) => s(item.model)).filter(Boolean),
  ].filter(Boolean));
  const latestRound = rounds.reduce((max, item) => Math.max(max, item.round), 0);
  const principalWaiting = turn.streaming && turn.rounds > latestRound;
  const hasLegacyPrincipalMetrics = rounds.length === 0 && Boolean(
    turn.metrics?.model || turn.metrics?.inputTokens !== undefined || turn.metrics?.outputTokens !== undefined || turn.metrics?.responseTime !== undefined,
  );
  const hasActivity = rounds.length || workers.length || tools.length || advisors.length || auxiliaries.length || principalWaiting || hasLegacyPrincipalMetrics;
  if (!hasActivity) return null;

  const allRounds = [...rounds, ...workers.flatMap((worker) => worker.roundActivity)];
  const widestRound = Math.max(1, ...allRounds.map((r) => Math.max(r.promptMs ?? 0, r.decodeMs ?? 0)));
  const widestRequest = Math.max(1, ...allRounds.map((r) => r.requestDurationMs ?? 0));
  const summary = [
    rounds.length ? t('{n} model rounds', { n: rounds.length }) : '',
    workers.length ? t('{n} sub-agents', { n: workers.length }) : '',
    tools.length ? t('{n} tools', { n: tools.length }) : '',
  ].filter(Boolean).join(' · ');

  return (
    <details className="fs-turn-activity" data-testid="turn-activity">
      <summary>{t('Activity')}{summary ? ` · ${summary}` : ''}</summary>
      <div className="fs-turn-activity__body">
        <p className="fs-turn-activity__key">
          {t('Models')}: {models.size}
          <span>{t('GPU attribution appears only when the runtime reports it.')}</span>
        </p>
        {hasLegacyPrincipalMetrics && (
          <section className="fs-turn-activity__actor" data-testid="activity-legacy-principal">
            <h4>{t('Principal')} · {t('Rounds')}: {turn.rounds || t('not reported')}</h4>
            {turn.metrics?.model && <p>{t('Model')}: <code>{turn.metrics.model}</code></p>}
            {(turn.metrics?.inputTokens !== undefined || turn.metrics?.outputTokens !== undefined) && (
              <p className="fs-turn-activity__tokens">
                {t('Prompt tokens')}: {count(turn.metrics.inputTokens)} · {t('Generated tokens')}: {count(turn.metrics.outputTokens)}
              </p>
            )}
            {turn.metrics?.responseTime !== undefined && (
              <p className="fs-turn-activity__tokens">{t('Observed')}: {duration(turn.metrics.responseTime * 1000)}</p>
            )}
          </section>
        )}
        {principalWaiting && (
          <section className="fs-turn-activity__actor" data-state="running">
            <h4>{t('Principal')} · {t('Round {n}', { n: turn.rounds })} · {t('running')}</h4>
            {turn.metrics?.model && <p>{t('Model')}: <code>{turn.metrics.model}</code></p>}
          </section>
        )}
        {rounds.map((round) => (
          <section className="fs-turn-activity__actor" key={`round-${round.round}`} data-testid={`activity-round-${round.round}`}>
            <h4>{t('Principal')} · {t('Round {n}', { n: round.round })}{round.model ? ` · ${round.model}` : ''}</h4>
            <div className="fs-turn-activity__metrics">
              {timingBar('Prefill', round.promptMs, widestRound, 'engine', '#6e9de6')}
              {timingBar('Decode', round.decodeMs, widestRound, 'engine', '#9a79db')}
              {round.requestDurationMs !== undefined && timingBar('Request stream', round.requestDurationMs, widestRequest, 'observed', '#70b7a1')}
            </div>
            <p className="fs-turn-activity__tokens">
              {t('Prompt tokens')}: {count(round.inputTokens)} · {t('Generated tokens')}: {count(round.outputTokens)} · {t('Cached tokens')}: {count(round.cachedTokens)}
              {round.finishReason && ` · ${t('Finish')}: ${round.finishReason}`}
            </p>
          </section>
        ))}
        {workers.length > 0 && (
          <section className="fs-turn-activity__group">
            <h3>{t('Sub-agents')}</h3>
            {workers.map((worker) => (
              <article className="fs-turn-activity__actor" key={worker.id} data-state={worker.status}>
                <h4>{worker.name || worker.role || t('Sub-agent')} · {t(worker.status)}</h4>
                {worker.model && <p>{t('Model')}: <code>{worker.model}</code></p>}
                {worker.instruction && <p>{t('Task')}: {worker.instruction}</p>}
                <p className="fs-turn-activity__tokens">
                  {worker.rounds !== null && `${t('Rounds')}: ${worker.rounds}`}
                  {worker.toolCalls > 0 && ` · ${t('Tools')}: ${worker.toolCalls}`}
                  {worker.inTok !== null && ` · ${t('Prompt tokens')}: ${count(worker.inTok)}`}
                  {worker.outTok !== null && ` · ${t('Generated tokens')}: ${count(worker.outTok)}`}
                </p>
                {worker.roundActivity.map((round) => (
                  <div className="fs-turn-activity__subround" key={`${worker.id}-round-${round.round}`} data-testid={`worker-round-${worker.id}-${round.round}`}>
                    <strong>{t('Round {n}', { n: round.round })}{round.model ? ` · ${round.model}` : ''}</strong>
                    <div className="fs-turn-activity__metrics">
                      {timingBar('Prefill', round.promptMs, widestRound, 'engine', '#6e9de6')}
                      {timingBar('Decode', round.decodeMs, widestRound, 'engine', '#9a79db')}
                      {round.requestDurationMs !== undefined && timingBar('Request stream', round.requestDurationMs, widestRequest, 'observed', '#70b7a1')}
                    </div>
                    <small>{t('Prompt tokens')}: {count(round.inputTokens)} · {t('Generated tokens')}: {count(round.outputTokens)} · {t('Cached tokens')}: {count(round.cachedTokens)}</small>
                  </div>
                ))}
              </article>
            ))}
          </section>
        )}
        {advisors.length > 0 && (
          <section className="fs-turn-activity__group">
            <h3>{t('Advisor')}</h3>
            {advisors.map((item, index) => (
              <article className="fs-turn-activity__actor" key={`${item.round ?? 0}-${item.trigger}-${index}`} data-state={item.ok ? 'succeeded' : 'failed'}>
                <h4>{item.trigger} · {item.ok ? t('completed') : t('failed')}{item.round ? ` · ${t('Round {n}', { n: item.round })}` : ''}</h4>
                {item.model && <p>{t('Model')}: <code>{item.model}</code></p>}
                <p className="fs-turn-activity__tokens">
                  {item.tokensIn !== undefined && `${t('Prompt tokens')}: ${count(item.tokensIn)}`}
                  {item.tokensOut !== undefined && ` · ${t('Generated tokens')}: ${count(item.tokensOut)}`}
                  {item.latencyMs !== undefined && ` · ${t('Observed')}: ${duration(item.latencyMs)}`}
                </p>
              </article>
            ))}
          </section>
        )}
        {auxiliaries.length > 0 && (
          <section className="fs-turn-activity__group">
            <h3>{t('Runtime helpers')}</h3>
            {auxiliaries.map((item, index) => (
              <article className="fs-turn-activity__actor" key={`${s(item.phase)}-${index}`} data-state={s(item.status) || 'observed'}>
                <h4>{s(item.phase) || t('Helper')}{n(item.round) !== undefined ? ` · ${t('Round {n}', { n: n(item.round) })}` : ''}</h4>
                {s(item.model) && <p>{t('Model')}: <code>{s(item.model)}</code></p>}
                {(n(item.input_tokens) !== undefined || n(item.output_tokens) !== undefined) && (
                  <p className="fs-turn-activity__tokens">{t('Prompt tokens')}: {count(n(item.input_tokens))} · {t('Generated tokens')}: {count(n(item.output_tokens))}</p>
                )}
              </article>
            ))}
          </section>
        )}
        {tools.length > 0 && (
          <section className="fs-turn-activity__group">
            <h3>{t('Hoard tools')}</h3>
            {tools.map((step) => (
              <article className="fs-turn-activity__tool" key={step.id} data-state={step.state}>
                <span className="fs-turn-activity__dot" aria-hidden="true" />
                <span><strong>{step.label || step.tool}</strong><small>{step.tool} · {t('Round {n}', { n: step.round })}</small></span>
                <span>{step.durationMs !== undefined ? duration(step.durationMs) : t(step.state)}</span>
              </article>
            ))}
          </section>
        )}
      </div>
    </details>
  );
}
