import { useEffect, useState } from 'react';
import { getSpendCap, SETTINGS_SAVED_EVENT, type SpendCapStatus } from '../../adapters/settings';
import { t } from '../../i18n';

function usd(value: number): string {
  return `$${value.toFixed(value >= 1 ? 2 : 4)}`;
}

/** Share of a limit used, 0..100; null when the limit is off (0). */
export function capShare(spent: number, limit: number): number | null {
  if (!(limit > 0)) return null;
  return Math.min(100, Math.round((spent / limit) * 100));
}

/**
 * Today's paid-provider spend against the daily hard cap. The toggle and the
 * two limits are ordinary schema fields in the Agent list above this card
 * (`paid_spend_cap_*`); this card only shows what the day has used so far.
 * Calls without a reported cost count tokens only and are said so.
 */
export function SpendCapSection() {
  const [s, setS] = useState<SpendCapStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Refetch on mount, after any settings save, when the tab regains focus and
  // every 15 s: the limit shown here must never lag the control above it.
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      getSpendCap()
        .then((d) => { if (!cancelled) { setS(d); setErr(null); } })
        .catch((e: Error) => { if (!cancelled) setErr(e.message); });
    };
    const onVisible = () => { if (document.visibilityState !== 'hidden') load(); };
    load();
    window.addEventListener(SETTINGS_SAVED_EVENT, load);
    window.addEventListener('focus', load);
    document.addEventListener('visibilitychange', onVisible);
    const timer = window.setInterval(load, 15000);
    return () => {
      cancelled = true;
      window.removeEventListener(SETTINGS_SAVED_EVENT, load);
      window.removeEventListener('focus', load);
      document.removeEventListener('visibilitychange', onVisible);
      window.clearInterval(timer);
    };
  }, []);

  const usdShare = s ? capShare(s.spent_usd, s.limit_usd) : null;
  const tokensCounted = s ? (s.tokens_counted_for_cap ?? s.tokens_today) : 0;
  const tokShare = s ? capShare(tokensCounted, s.limit_tokens) : null;

  return (
    <div className="fs-set__card" data-testid="spend-cap-card">
      <h3 className="fs-set__card-title">{t('Daily paid spend cap')}</h3>
      <p className="fs-set__help">
        {t("Today's spend on paid providers against the daily cap. Change the limits in the list above.")}
      </p>
      {err && <p className="fs-set__err">{err}</p>}
      {!s && !err && <p className="fs-set__help">{t('Loading')}</p>}
      {s && (
        <>
          {!s.enabled && <p className="fs-set__help">{t('Daily paid spend cap is off.')}</p>}
          {s.enabled && s.reached && (
            <p className="fs-set__err" role="alert">
              {t('Cap reached: new paid calls are refused until {time}. Local models keep working.', {
                time: new Date(s.resets_at).toLocaleString(),
              })}
            </p>
          )}
          <p className="fs-set__help" data-testid="spend-cap-usd">
            {t('Spent today')}: <strong>{usd(s.spent_usd)}</strong>
            {' / '}{s.limit_usd > 0 ? usd(s.limit_usd) : t('no limit')}
            {usdShare !== null && ` (${usdShare}%)`}
          </p>
          <p className="fs-set__help" data-testid="spend-cap-tokens">
            {t('Tokens today')}: <strong>{tokensCounted.toLocaleString()}</strong>
            {' / '}{s.limit_tokens > 0 ? s.limit_tokens.toLocaleString() : t('no limit')}
            {tokShare !== null && ` (${tokShare}%)`}
          </p>
          <p className="fs-set__help">{t('Resets')}: {new Date(s.resets_at).toLocaleString()}</p>
          {s.unknown_cost_calls > 0 && (
            <p className="fs-set__help">
              {t('{n} paid calls reported no cost: they count tokens only, never dollars.', { n: s.unknown_cost_calls })}
            </p>
          )}
          {s.by_provider.length === 0 ? (
            <p className="fs-set__help">{t('No paid-provider calls today.')}</p>
          ) : (
            <table className="fs-autonomy-table">
              <thead>
                <tr>
                  <th>{t('Provider')}</th>
                  <th>{t('Calls')}</th>
                  <th>{t('Tokens today')}</th>
                  <th>USD</th>
                  <th>{t('Unknown cost')}</th>
                </tr>
              </thead>
              <tbody>
                {s.by_provider.map((p) => (
                  <tr key={p.provider}>
                    <td>{p.provider}</td>
                    <td>{p.calls}</td>
                    <td>{p.tokens.toLocaleString()}</td>
                    <td>{usd(p.usd)}</td>
                    <td>{p.unknown_cost_calls}</td>
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
