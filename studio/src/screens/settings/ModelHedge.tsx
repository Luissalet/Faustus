import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components';
import { getHedgeStatus, updateHedge, type HedgePatch, type HedgeRace, type HedgeStatus } from '../../adapters/modelHedge';
import { SETTINGS_SAVED_EVENT } from '../../adapters/settings';
import { t } from '../../i18n';
import { Field, Select, Text, Toggle } from './fields';

/** "answered by hedge (Spark B) after 23 s" for one recorded race. */
export function raceLine(race: HedgeRace): string {
  const seconds = Math.round(race.after_s ?? 0);
  if (race.outcome === 'skipped') {
    return race.skip_reason === 'saturated'
      ? t('Re-send skipped after {s} s: the server had {n} requests waiting', { s: seconds, n: race.server_waiting ?? '?' })
      : t('Re-send skipped after {s} s: this turn already used its re-sends', { s: seconds });
  }
  if (race.winner === 'hedge' && race.outcome === 'first_token' && race.hedge_kind === 'reissue') {
    return t('Answered by a re-sent request after {s} s', { s: seconds });
  }
  if (race.winner === 'hedge' && race.outcome === 'first_token') {
    return t('Answered by the hedge ({endpoint}) after {s} s', {
      endpoint: race.hedge_endpoint_label || race.hedge_model || '?', s: seconds,
    });
  }
  if (race.outcome === 'first_token') {
    return t('The primary answered first ({endpoint}) after {s} s', {
      endpoint: race.primary_endpoint_label || race.primary_model, s: seconds,
    });
  }
  return t('Neither answered; the primary error was kept ({s} s)', { s: seconds });
}

function overridesText(value: Record<string, number> | undefined): string {
  return value && Object.keys(value).length ? JSON.stringify(value) : '';
}

/**
 * Settings > Models: hedged first token. Toggle, delay, hedge target (any
 * enabled endpoint and model, or the automatic choices) and per-model delays,
 * plus the hedge the default model gets right now and the recent races.
 */
export function ModelHedgeSection({ say }: { say?: (msg: string) => void }) {
  const [s, setS] = useState<HedgeStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [delay, setDelay] = useState('');
  const [target, setTarget] = useState('auto');
  const [overrides, setOverrides] = useState('');
  const [reissueMax, setReissueMax] = useState('1');
  const [reissueOverrides, setReissueOverrides] = useState('');
  const [busy, setBusy] = useState(false);

  const adopt = useCallback((d: HedgeStatus) => {
    setS(d);
    setErr(null);
    setDelay(String(d.delay_s));
    setTarget(d.target || 'auto');
    setOverrides(overridesText(d.delay_overrides));
    setReissueMax(String(d.reissue_max ?? 1));
    setReissueOverrides(overridesText(d.reissue_overrides));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = () => getHedgeStatus()
      .then((d) => { if (!cancelled) adopt(d); })
      .catch((e: Error) => { if (!cancelled) setErr(e.message); });
    load();
    window.addEventListener(SETTINGS_SAVED_EVENT, load);
    return () => { cancelled = true; window.removeEventListener(SETTINGS_SAVED_EVENT, load); };
  }, [adopt]);

  const save = async (patch: HedgePatch) => {
    setBusy(true);
    try {
      adopt(await updateHedge(patch));
      say?.(t('Hedge settings saved'));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveForm = () => {
    let parsed: Record<string, number> = {};
    if (overrides.trim()) {
      try {
        parsed = JSON.parse(overrides) as Record<string, number>;
      } catch {
        setErr(t('Per-model delays must be a JSON object like {"model": 15}'));
        return;
      }
    }
    let perModel: Record<string, number> = {};
    if (reissueOverrides.trim()) {
      try {
        perModel = JSON.parse(reissueOverrides) as Record<string, number>;
      } catch {
        setErr(t('Re-sends per model must be a JSON object like {"model": 1}'));
        return;
      }
    }
    void save({
      delay_s: Number(delay), target, delay_overrides: parsed,
      reissue_max: Number(reissueMax), reissue_overrides: perModel,
    });
  };

  const dirty = !!s && (String(s.delay_s) !== delay || (s.target || 'auto') !== target
    || overridesText(s.delay_overrides) !== overrides.trim()
    || String(s.reissue_max ?? 1) !== reissueMax
    || overridesText(s.reissue_overrides) !== reissueOverrides.trim());
  const options = (s?.targets ?? []).map((o) => ({ value: o.value, label: o.endpoint_id ? o.label : t(o.label) }));
  const current = s?.current;

  return (
    <div className="fs-set__card" data-testid="model-hedge-card">
      <h3 className="fs-set__card-title">{t('Hedged first token')}</h3>
      <p className="fs-set__help">
        {t('If the chat model has not started answering (text, reasoning or a tool call) after the delay, the same request also goes to the hedge target and whichever answers first is used. The other request is closed so its server stops. Nothing is sent twice once a token has been streamed.')}
      </p>
      {err && <p className="fs-set__err" role="alert">{err}</p>}
      {!s && !err && <p className="fs-set__help">{t('Loading')}</p>}
      {s && (
        <>
          <Field label={t('Hedge a slow first token')} htmlFor="hedge-enabled">
            <Toggle id="hedge-enabled" checked={s.enabled} disabled={busy}
              onChange={(v) => void save({ enabled: v })} />
          </Field>
          <Field label={t('Hedge after (seconds without a first token)')} htmlFor="hedge-delay"
            help={t('Between {min} and {max} seconds.', { min: s.min_delay_s, max: s.max_delay_s })}>
            <Text id="hedge-delay" type="number" value={delay} onChange={setDelay} disabled={!s.enabled} />
          </Field>
          <Field label={t('Hedge target')} htmlFor="hedge-target"
            help={t('Only the global LLM service (the Sparks) is offered; a local text model is never the hedge. Automatic uses the same model on another of its endpoints when one lists it, else the first fallback of the chain served there.')}>
            <Select id="hedge-target" value={target} options={options} onChange={setTarget} />
          </Field>
          <p className="fs-set__help" data-testid="hedge-reissue-help">
            {t('Re-send: after the delay the same request goes again to the same server and model, the silent one is closed; skipped when the server reports requests waiting. Automatic ends here when there is no other route.')}
          </p>
          <Field label={t('Hedge delay per model')} htmlFor="hedge-overrides"
            help={t('Optional JSON object {model: seconds} that replaces the delay for those models.')}>
            <Text id="hedge-overrides" value={overrides} onChange={setOverrides} placeholder='{"deepseek-v4.1-flash": 15}' />
          </Field>
          <Field label={t('Re-sends per turn')} htmlFor="hedge-reissue-max"
            help={t('Between 0 and {max}; 0 never re-sends to the same server.', { max: s.max_reissue_max ?? 5 })}>
            <Text id="hedge-reissue-max" type="number" value={reissueMax} onChange={setReissueMax} disabled={!s.enabled} />
          </Field>
          <Field label={t('Re-sends per model')} htmlFor="hedge-reissue-overrides"
            help={t('Optional JSON object {model: re-sends per turn} that replaces the number above for those models.')}>
            <Text id="hedge-reissue-overrides" value={reissueOverrides} onChange={setReissueOverrides} placeholder='{"deepseek-v4.1-flash": 1}' />
          </Field>
          <div className="fs-set__row-actions">
            <Button variant="primary" size="sm" label={t('Save')} disabled={!dirty || busy} loading={busy}
              onClick={saveForm} testId="hedge-save" />
          </div>
          <p className="fs-set__help" data-testid="hedge-current">
            {!current ? t('No default chat model is set.')
              : current.hedge?.kind === 'reissue'
                ? t('{model} at {endpoint}: the same request is re-sent there after {s} s without a first token (up to {n} per turn).', {
                  model: current.model, endpoint: current.endpoint_label || '', s: current.delay_s,
                  n: current.reissue_max ?? s.reissue_max,
                })
              : current.hedge
                ? t('{model} at {endpoint}: hedge {hedge} at {hedgeEndpoint} after {s} s without a first token.', {
                  model: current.model, endpoint: current.endpoint_label || '', hedge: current.hedge.model,
                  hedgeEndpoint: current.hedge.endpoint_label || '', s: current.delay_s,
                })
                : t('{model} at {endpoint}: no hedge target available. Add another endpoint serving it, a fallback model, or pick a target above.', {
                  model: current.model, endpoint: current.endpoint_label || '',
                })}
          </p>
          <p className="fs-set__help" data-testid="hedge-stats">
            {t('Races recorded: {n}; answered by the hedge: {h}.', { n: s.stats.races, h: s.stats.hedge_wins })}
            {s.stats.avg_hedge_answer_s != null && ` ${t('Average answer through the hedge: {s} s.', { s: s.stats.avg_hedge_answer_s })}`}
            {` ${t('Answered by a re-sent request: {w}; re-sends skipped: {k}.', { w: s.stats.reissue_wins ?? 0, k: s.stats.reissue_skips ?? 0 })}`}
          </p>
          {s.recent.length > 0 && (
            <table className="fs-autonomy-table" data-testid="hedge-recent">
              <thead>
                <tr>
                  <th>{t('When')}</th>
                  <th>{t('Result')}</th>
                  <th>{t('Model')}</th>
                </tr>
              </thead>
              <tbody>
                {s.recent.slice(0, 10).map((race) => (
                  <tr key={`${race.ts}-${race.winner ?? race.outcome}`}>
                    <td>{new Date(race.ts * 1000).toLocaleString()}</td>
                    <td>{raceLine(race)}</td>
                    <td>{race.answered_by}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
